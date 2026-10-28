import asyncio
import os
import re

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from dotenv import load_dotenv
load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

dp = Dispatcher()


CADASTRAL_RE = re.compile(
    r"^\d{1,2}:\d{1,2}:\d+:\d+$"
)


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Отправьте кадастровый номер участка.\n"
        "Например: 50:21:0030210:123"
    )


@dp.message(F.text)
async def cadastral_number_handler(message: Message):
    text = message.text.strip()

    if not CADASTRAL_RE.fullmatch(text):
        await message.answer(
            "Не удалось распознать кадастровый номер.\n"
            "Пример: 50:21:0030210:123"
        )
        return

    print(
        f"CADASTRAL NUMBER: {text}",
        flush=True,
    )

    await message.answer(
        f"Получен кадастровый номер:\n{text}"
    )


async def main():
    bot = Bot(token=TOKEN)

    print("Telegram bot started", flush=True)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
