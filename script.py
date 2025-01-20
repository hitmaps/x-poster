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
    lastPost = redisClient.get(f'x-poster:{screen_name}')
    user = await client.get_user_by_screen_name(screen_name)
    posts = await client.get_user_tweets(user.id, 'Tweets', 1)
    post = posts[0]

    if post.id != lastPost.decode():
        thumbnail = None
        if post.media.count:
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

        redisClient.set(f'x-poster:{screen_name}', post.id)

async def main():
    client.set_cookies({ "auth_token": X_AUTH_TOKEN, "ct0": X_CT0_TOKEN })

    while True:
        accounts = ACCOUNTS.split('||')
        for account in accounts:
            await check_account(account)
            
        print(f'{datetime.now()} | Checked Socials')
        time.sleep(INTERVAL)

asyncio.run(main())