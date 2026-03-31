# MONKEY PATCH: Remove this block when twikit is updated to fix ON_DEMAND_FILE_REGEX
import re
_tx_mod = __import__('twikit.x_client_transaction.transaction', fromlist=['ClientTransaction'])
_tx_mod.ON_DEMAND_FILE_REGEX = re.compile(
    r""",(\d+):["']ondemand\.s["']""", flags=(re.VERBOSE | re.MULTILINE))
_tx_mod.ON_DEMAND_HASH_PATTERN = r',{}:"([0-9a-f]+)"'

async def _patched_get_indices(self, home_page_response, session, headers):
    key_byte_indices = []
    response = self.validate_response(home_page_response) or self.home_page_response
    on_demand_file_index = _tx_mod.ON_DEMAND_FILE_REGEX.search(str(response)).group(1)
    regex = re.compile(_tx_mod.ON_DEMAND_HASH_PATTERN.format(on_demand_file_index))
    filename = regex.search(str(response)).group(1)
    on_demand_file_url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{filename}a.js"
    on_demand_file_response = await session.request(method="GET", url=on_demand_file_url, headers=headers)
    key_byte_indices_match = _tx_mod.INDICES_REGEX.finditer(str(on_demand_file_response.text))
    for item in key_byte_indices_match:
        key_byte_indices.append(item.group(2))
    if not key_byte_indices:
        raise Exception("Couldn't get KEY_BYTE indices")
    key_byte_indices = list(map(int, key_byte_indices))
    return key_byte_indices[0], key_byte_indices[1:]

_tx_mod.ClientTransaction.get_indices = _patched_get_indices
# END MONKEY PATCH

import asyncio
from datetime import datetime
from twikit import Client
import redis
import requests
import os
import time

REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = os.environ.get('REDIS_PORT', 6379)
REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD', None)
DISCORD_WEBHOOK = os.environ.get('DISCORD_WEBHOOK', '')
INTERVAL = os.environ.get('INTERVAL', 60)
X_AUTH_TOKEN = os.environ.get('X_AUTH_TOKEN', '')
X_CT0_TOKEN = os.environ.get('X_CT0_TOKEN', '')
ACCOUNTS = os.environ.get('ACCOUNTS', 'hitmapsdotcom||iointeractive||hitman')

client = Client('en-US')
redisClient = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD)

async def check_account(screen_name: str):
    lastPosts = redisClient.hkeys(f'x-poster:{screen_name}')
    user = await client.get_user_by_screen_name(screen_name)
    posts = await client.get_user_tweets(user.id, 'Tweets', 1)
    post = posts[0]

    if not lastPosts or post.id.encode() not in lastPosts and post.retweeted_tweet == None and post.in_reply_to == None:
        thumbnail = None
        if post.media != None and post.media.count:
            thumbnail = {
                'url': post.media[0]['media_url_https']
            }
        data = {
            'embeds': [
                {
                    'description': post.full_text,
                    'title': f'{user.name} on X',
                    'url': f'https://x.com/{screen_name}/status/{post.id}',
                    'timestamp': post.created_at_datetime.isoformat(),
                    'image': thumbnail
                }
            ]
        }
        requests.post(DISCORD_WEBHOOK, json = data)

        redisClient.hset(f'x-poster:{screen_name}', post.id, "1")

async def main():
    client.set_cookies({ "auth_token": X_AUTH_TOKEN, "ct0": X_CT0_TOKEN })

    while True:
        accounts = ACCOUNTS.split('||')
        for account in accounts:
            await check_account(account)

        print(f'{datetime.now()} | Checked Socials')
        time.sleep(INTERVAL)

asyncio.run(main())