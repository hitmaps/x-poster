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

# Discord: 10 embeds per webhook message, 1 image each. Webhook file uploads
# are capped by the destination server (10 MiB unboosted is the safe default).
DISCORD_MAX_EMBED_IMAGES = 10
DISCORD_MAX_UPLOAD_BYTES = int(
    os.environ.get("DISCORD_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))
)
VIDEO_MEDIA_TYPES = {"video", "animated_gif"}


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


def attached_media(
    event_data: dict[str, Any], tweet: dict[str, Any]
) -> list[dict[str, Any]]:
    """Media objects for this post, preserving attachment order when keys exist."""
    includes = event_data.get("includes") or {}
    media_list = list(includes.get("media") or [])
    media_keys = list((tweet.get("attachments") or {}).get("media_keys") or [])
    if not media_keys:
        return media_list

    by_key = {m.get("media_key"): m for m in media_list if m.get("media_key")}
    ordered = [by_key[k] for k in media_keys if k in by_key]
    return ordered or media_list


def mp4_variant_urls(media: dict[str, Any]) -> list[str]:
    """MP4 URLs for a video/GIF, highest bitrate first."""
    variants = list(media.get("variants") or [])
    mp4s: list[dict[str, Any]] = []
    for variant in variants:
        url = variant.get("url")
        if not url:
            continue
        content_type = (variant.get("content_type") or "").lower()
        path = url.split("?", 1)[0].lower()
        if content_type == "video/mp4" or path.endswith(".mp4"):
            mp4s.append(variant)
    mp4s.sort(key=lambda v: v.get("bit_rate") or v.get("bitrate") or 0, reverse=True)
    urls = [v["url"] for v in mp4s if v.get("url")]

    direct = media.get("url")
    if direct and direct not in urls:
        path = direct.split("?", 1)[0].lower()
        if path.endswith(".mp4") or media.get("type") in VIDEO_MEDIA_TYPES:
            urls.append(direct)
    return urls


def fetch_tweet_media(client: httpx.Client, tweet_id: str) -> list[dict[str, Any]]:
    """Look up media expansions when the stream payload omitted variants/URLs."""
    resp = client.get(
        f"{API_BASE}/tweets/{tweet_id}",
        params={
            "expansions": "attachments.media_keys",
            "media.fields": "alt_text,media_key,preview_image_url,type,url,variants",
        },
    )
    if resp.status_code != 200:
        log(f"Failed to fetch media for post {tweet_id}: {resp.status_code} {resp.text}")
        return []
    return list((resp.json().get("includes") or {}).get("media") or [])


def post_media(
    client: httpx.Client, event_data: dict[str, Any], tweet: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """
    Return (photo_urls, video_mp4_urls).

    Video URLs are only returned when the post has no photos, matching X's
    usual exclusive media types (images vs a single video/GIF).
    """
    media_list = attached_media(event_data, tweet)
    media_keys = list((tweet.get("attachments") or {}).get("media_keys") or [])
    needs_lookup = bool(media_keys and not media_list)
    if not needs_lookup:
        for media in media_list:
            if media.get("type") in VIDEO_MEDIA_TYPES and not mp4_variant_urls(media):
                needs_lookup = True
                break

    if needs_lookup and tweet.get("id"):
        looked_up = fetch_tweet_media(client, tweet["id"])
        if looked_up:
            media_list = attached_media({"includes": {"media": looked_up}}, tweet)

    photo_urls: list[str] = []
    video_urls: list[str] = []
    for media in media_list:
        media_type = media.get("type")
        if media_type == "photo":
            url = media.get("url") or media.get("preview_image_url")
            if url:
                photo_urls.append(url)
        elif media_type in VIDEO_MEDIA_TYPES and not video_urls:
            video_urls = mp4_variant_urls(media)

    if photo_urls:
        return photo_urls, []
    return [], video_urls


def download_within_limit(
    client: httpx.Client, url: str, max_bytes: int
) -> bytes | None:
    """Download url if it fits max_bytes; otherwise return None."""
    try:
        with client.stream("GET", url, follow_redirects=True, timeout=60.0) as resp:
            if resp.status_code >= 400:
                log(f"Video download failed: {resp.status_code} {url}")
                return None
            length = resp.headers.get("content-length")
            if length and int(length) > max_bytes:
                return None
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    resp.close()
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError as exc:
        log(f"Video download error: {exc}")
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


def build_discord_embeds(
    *,
    display_name: str,
    username: str,
    tweet_id: str,
    text: str,
    created_at: str | None,
    image_urls: list[str],
) -> list[dict[str, Any]]:
    """One embed per image. Discord groups embeds that share the same url."""
    post_url = f"https://x.com/{username}/status/{tweet_id}"
    images = image_urls[:DISCORD_MAX_EMBED_IMAGES]
    first: dict[str, Any] = {
        "description": text,
        "title": f"{display_name} on X",
        "url": post_url,
    }
    if created_at:
        first["timestamp"] = created_at
    if images:
        first["image"] = {"url": images[0]}

    embeds = [first]
    for image_url in images[1:]:
        embeds.append({"url": post_url, "image": {"url": image_url}})
    return embeds


def attach_video_for_discord(
    client: httpx.Client, video_urls: list[str]
) -> tuple[str, bytes] | None:
    """Pick the highest-quality MP4 that fits the webhook upload cap."""
    for url in video_urls:
        data = download_within_limit(client, url, DISCORD_MAX_UPLOAD_BYTES)
        if data:
            return url, data
    return None


def push_to_discord(
    client: httpx.Client,
    *,
    display_name: str,
    username: str,
    tweet_id: str,
    text: str,
    created_at: str | None,
    image_urls: list[str],
    video_urls: list[str],
) -> None:
    embeds = build_discord_embeds(
        display_name=display_name,
        username=username,
        tweet_id=tweet_id,
        text=text,
        created_at=created_at,
        image_urls=image_urls,
    )
    files: dict[str, tuple[str, bytes, str]] | None = None

    if video_urls and not image_urls:
        attached = attach_video_for_discord(client, video_urls)
        if attached:
            _src_url, video_bytes = attached
            files = {"files[0]": ("video.mp4", video_bytes, "video/mp4")}
        else:
            log(
                f"Could not attach video under {DISCORD_MAX_UPLOAD_BYTES} bytes "
                f"for post {tweet_id}; linking instead"
            )
            link = video_urls[0]
            extra = f"\n\n[Video]({link})"
            description = embeds[0].get("description") or ""
            embeds[0]["description"] = (description + extra).strip()

    post_url = f"https://x.com/{username}/status/{tweet_id}"
    if files:
        resp = client.post(
            DISCORD_WEBHOOK,
            data={"payload_json": json.dumps({"embeds": embeds})},
            files=files,
            timeout=60.0,
        )
    else:
        resp = client.post(DISCORD_WEBHOOK, json={"embeds": embeds})

    if resp.status_code >= 400:
        log(f"Discord webhook failed: {resp.status_code} {resp.text}")
    else:
        log(f"Posted to Discord: {post_url}")


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
    image_urls, video_urls = post_media(client, data, tweet)

    push_to_discord(
        client,
        display_name=display_name,
        username=username,
        tweet_id=tweet["id"],
        text=text,
        created_at=tweet.get("created_at"),
        image_urls=image_urls,
        video_urls=video_urls,
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
