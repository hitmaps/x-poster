"""
X Activity API listener: subscribe to post.create for configured accounts
and forward original posts / quote posts to a Discord webhook.

Straight reposts and replies are skipped.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx

API_BASE = "https://api.x.com/2"

X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
ACCOUNTS = os.environ.get("ACCOUNTS", "hitmapsdotcom||iointeractive||hitman")

# Reconnect backoff (seconds)
RECONNECT_INITIAL = float(os.environ.get("RECONNECT_INITIAL", "5"))
RECONNECT_MAX = float(os.environ.get("RECONNECT_MAX", "300"))


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} | {message}", flush=True)


def require_config() -> None:
    missing = []
    if not X_BEARER_TOKEN:
        missing.append("X_BEARER_TOKEN")
    if not DISCORD_WEBHOOK:
        missing.append("DISCORD_WEBHOOK")
    if missing:
        log(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)


def parse_accounts() -> list[str]:
    return [a.strip().lstrip("@") for a in ACCOUNTS.split("||") if a.strip()]


def auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {X_BEARER_TOKEN}",
        "User-Agent": "x-poster/2.0",
    }


def resolve_users(client: httpx.Client, usernames: list[str]) -> dict[str, dict[str, str]]:
    """
    Resolve screen names to user objects.

    Returns mapping: user_id -> {id, name, username}
    """
    users_by_id: dict[str, dict[str, str]] = {}
    # Users lookup-by allows up to 100 usernames per request.
    for i in range(0, len(usernames), 100):
        batch = usernames[i : i + 100]
        resp = client.get(
            f"{API_BASE}/users/by",
            params={"usernames": ",".join(batch), "user.fields": "name,username"},
        )
        if resp.status_code != 200:
            log(f"Failed to resolve usernames {batch}: {resp.status_code} {resp.text}")
            resp.raise_for_status()

        body = resp.json()
        for user in body.get("data") or []:
            users_by_id[user["id"]] = {
                "id": user["id"],
                "name": user["name"],
                "username": user["username"],
            }

        for err in body.get("errors") or []:
            log(f"Username resolve warning: {err}")

    missing = set(u.lower() for u in usernames) - {
        u["username"].lower() for u in users_by_id.values()
    }
    if missing:
        log(f"Could not resolve usernames: {', '.join(sorted(missing))}")
        if not users_by_id:
            raise RuntimeError("No accounts could be resolved")

    return users_by_id


def list_subscriptions(client: httpx.Client) -> list[dict[str, Any]]:
    resp = client.get(f"{API_BASE}/activity/subscriptions")
    if resp.status_code != 200:
        log(f"Failed to list subscriptions: {resp.status_code} {resp.text}")
        resp.raise_for_status()
    return list((resp.json().get("data") or []))


def ensure_post_create_subscriptions(
    client: httpx.Client, users_by_id: dict[str, dict[str, str]]
) -> None:
    """Create post.create subscriptions for each user if not already present."""
    existing = list_subscriptions(client)
    already: set[str] = set()
    for sub in existing:
        if sub.get("event_type") != "post.create":
            continue
        uid = (sub.get("filter") or {}).get("user_id")
        if uid:
            already.add(uid)

    for user_id, user in users_by_id.items():
        if user_id in already:
            log(f"Subscription already exists for @{user['username']} ({user_id})")
            continue

        payload = {
            "event_type": "post.create",
            "filter": {"user_id": user_id},
            "tag": f"post.create:{user['username']}",
        }
        resp = client.post(f"{API_BASE}/activity/subscriptions", json=payload)
        if resp.status_code not in (200, 201):
            log(
                f"Failed to create subscription for @{user['username']}: "
                f"{resp.status_code} {resp.text}"
            )
            resp.raise_for_status()
        log(f"Created post.create subscription for @{user['username']} ({user_id})")


def referenced_types(tweet: dict[str, Any]) -> set[str]:
    refs = tweet.get("referenced_tweets") or []
    return {r.get("type") for r in refs if r.get("type")}


def should_notify(tweet: dict[str, Any]) -> bool:
    """Original posts and quote posts only; skip replies and straight reposts."""
    types = referenced_types(tweet)
    if "retweeted" in types:
        return False
    if "replied_to" in types:
        return False
    if tweet.get("in_reply_to_user_id"):
        return False
    return True


def post_text(tweet: dict[str, Any]) -> str:
    note = tweet.get("note_tweet") or {}
    if note.get("text"):
        return note["text"]
    return tweet.get("text") or ""


def media_image_url(event_data: dict[str, Any], tweet: dict[str, Any]) -> str | None:
    """Best-effort first image URL from stream includes or nested media metadata."""
    includes = event_data.get("includes") or {}
    media_list = list(includes.get("media") or [])

    # Some payloads may embed media under payload.attachments expansions style.
    attachments = tweet.get("attachments") or {}
    media_keys = set(attachments.get("media_keys") or [])

    for media in media_list:
        if media_keys and media.get("media_key") not in media_keys:
            continue
        if media.get("type") == "photo" and media.get("url"):
            return media["url"]
        if media.get("preview_image_url"):
            return media["preview_image_url"]
        if media.get("url"):
            return media["url"]

    # Fall back: any media in includes if keys were missing
    if not media_keys:
        for media in media_list:
            if media.get("type") == "photo" and media.get("url"):
                return media["url"]
            if media.get("preview_image_url"):
                return media["preview_image_url"]

    return None


def resolve_author(
    tweet: dict[str, Any],
    event_data: dict[str, Any],
    users_by_id: dict[str, dict[str, str]],
) -> tuple[str, str]:
    """Return (display_name, username)."""
    author_id = tweet.get("author_id") or (event_data.get("filter") or {}).get("user_id")

    if author_id and author_id in users_by_id:
        u = users_by_id[author_id]
        return u["name"], u["username"]

    includes = event_data.get("includes") or {}
    for user in includes.get("users") or []:
        if user.get("id") == author_id:
            return user.get("name") or user.get("username") or "Unknown", user.get(
                "username"
            ) or "unknown"

    username = tweet.get("username") or "unknown"
    return username, username


def push_to_discord(
    client: httpx.Client,
    *,
    display_name: str,
    username: str,
    tweet_id: str,
    text: str,
    created_at: str | None,
    image_url: str | None,
) -> None:
    embed: dict[str, Any] = {
        "description": text,
        "title": f"{display_name} on X",
        "url": f"https://x.com/{username}/status/{tweet_id}",
    }
    if created_at:
        embed["timestamp"] = created_at
    if image_url:
        embed["image"] = {"url": image_url}

    resp = client.post(DISCORD_WEBHOOK, json={"embeds": [embed]})
    if resp.status_code >= 400:
        log(f"Discord webhook failed: {resp.status_code} {resp.text}")
    else:
        log(f"Posted to Discord: https://x.com/{username}/status/{tweet_id}")


def handle_event(
    client: httpx.Client,
    event: dict[str, Any],
    users_by_id: dict[str, dict[str, str]],
) -> None:
    data = event.get("data") or {}
    event_type = data.get("event_type")

    if event_type != "post.create":
        return

    tweet = data.get("payload") or {}
    if not tweet.get("id"):
        log(f"post.create missing tweet id: {json.dumps(event)[:500]}")
        return

    if not should_notify(tweet):
        types = referenced_types(tweet)
        log(f"Skipped post {tweet['id']} (types={sorted(types) or 'none'})")
        return

    display_name, username = resolve_author(tweet, data, users_by_id)
    text = post_text(tweet)
    image_url = media_image_url(data, tweet)

    push_to_discord(
        client,
        display_name=display_name,
        username=username,
        tweet_id=tweet["id"],
        text=text,
        created_at=tweet.get("created_at"),
        image_url=image_url,
    )


def connect_stream(client: httpx.Client, users_by_id: dict[str, dict[str, str]]) -> None:
    log("Connecting to activity stream…")
    # timeout=None keeps the long-lived stream open; connect timeout still applies.
    with client.stream(
        "GET",
        f"{API_BASE}/activity/stream",
        timeout=httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0),
    ) as resp:
        if resp.status_code != 200:
            body = resp.read().decode("utf-8", errors="replace")
            log(f"Stream connect failed: {resp.status_code} {body}")
            resp.raise_for_status()

        log("Activity stream connected")
        for line in resp.iter_lines():
            if not line or not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log(f"Non-JSON stream line: {line[:200]}")
                continue

            if event.get("errors"):
                log(f"Stream error payload: {event['errors']}")
                # Provisioning / connection issues should bubble to reconnect loop
                for err in event["errors"]:
                    title = (err or {}).get("title") or ""
                    if "Provisioning" in title or "connection" in title.lower():
                        raise RuntimeError(f"Stream error: {err}")
                continue

            try:
                handle_event(client, event, users_by_id)
            except Exception as exc:  # noqa: BLE001 — keep stream alive on handler bugs
                log(f"Error handling event: {exc}")


def main() -> None:
    require_config()
    usernames = parse_accounts()
    if not usernames:
        log("ACCOUNTS is empty")
        sys.exit(1)

    log(f"Starting x-poster for accounts: {', '.join('@' + u for u in usernames)}")

    backoff = RECONNECT_INITIAL
    with httpx.Client(headers=auth_headers(), timeout=30.0) as client:
        users_by_id = resolve_users(client, usernames)
        for u in users_by_id.values():
            log(f"Resolved @{u['username']} -> {u['id']} ({u['name']})")

        ensure_post_create_subscriptions(client, users_by_id)

        while True:
            try:
                connect_stream(client, users_by_id)
                # Clean stream end — reconnect
                log("Stream ended; reconnecting…")
                backoff = RECONNECT_INITIAL
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                log(f"HTTP error on stream ({status}); retry in {backoff:.0f}s")
            except (httpx.HTTPError, OSError, RuntimeError) as exc:
                log(f"Stream error: {exc}; retry in {backoff:.0f}s")
            except Exception as exc:  # noqa: BLE001
                log(f"Unexpected error: {exc}; retry in {backoff:.0f}s")

            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)


if __name__ == "__main__":
    main()
