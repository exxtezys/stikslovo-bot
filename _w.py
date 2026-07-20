import asyncio
from telegram import Bot

async def check():
    bot = Bot("8633435008:AAFMk3_db1xwJ3wDZ5KMGvWTh3NpXJpeeSc")
    info = await bot.get_webhook_info()
    print(f"URL: {info.url or 'НЕ УСТАНОВЛЕН'}")
    print(f"Pending: {info.pending_update_count}")
    print(f"Last error: {info.last_error_message or 'нет'}")
    print(f"Last error date: {info.last_error_date or 'нет'}")
    await bot.close()

asyncio.run(check())
