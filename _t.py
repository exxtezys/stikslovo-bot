import asyncio
from telegram import Bot

async def check():
    bot = Bot("8633435008:AAFMk3_db1xwJ3wDZ5KMGvWTh3NpXJpeeSc")
    info = await bot.get_webhook_info()
    print(f"URL: {info.url or 'NOT SET'}")
    print(f"Pending: {info.pending_update_count}")
    print(f"Last error: {info.last_error_message or 'none'}")
    
    # Test if bot is alive on Render
    try:
        import urllib.request
        resp = urllib.request.urlopen("https://sticker-bot-2kmt.onrender.com/", timeout=5)
        print(f"Render status: HTTP {resp.status}")
    except Exception as e:
        print(f"Render: {e}")

asyncio.run(check())
