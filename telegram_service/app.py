from typing import Optional

from fastapi import FastAPI, HTTPException
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from pydantic import BaseModel

import logging
import os
from datetime import datetime

class MessageRequest(BaseModel):
    text: str
    chat_id: Optional[str] = None

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Инициализация FastAPI
app = FastAPI(
    title="Telegram Bot Service",
    description="Микросервис для отправки сообщений в Telegram",
)

# Получаем токен из переменных окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "default_token")
DEFAULT_CHAT_ID = os.getenv("DEFAULT_CHAT_ID", "default_chat_id")

# Инициализация бота и диспетчера
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()  # В aiogram 3.x создаем без параметров

@app.on_event("startup")
async def startup():
    logger.info("Сервис запущен")

@app.on_event("shutdown")
async def shutdown():
    logger.info("Сервис остановлен")
    await bot.session.close()

# @app.post("/send_message")
# async def send_message(text: str, chat_id: str = None):
#     try:
#         target_chat_id = chat_id or DEFAULT_CHAT_ID
#         await bot.send_message(
#             chat_id=target_chat_id,
#             text=text
#         )
#         return {"status": "success", "message": "Сообщение отправлено"}
#     except Exception as e:
#         logger.error(f"Ошибка при отправке сообщения: {str(e)}")
#         raise HTTPException(status_code=500, detail=str(e))


@app.post("/send_message")
async def send_message(message: MessageRequest):
    try:
        target_chat_id = message.chat_id or DEFAULT_CHAT_ID
        await bot.send_message(
            chat_id=target_chat_id,
            text=message.text
        )
        return {
            "status": "success",
            "message": "Сообщение отправлено",
            "text": message.text,
            "chat_id": target_chat_id
        }
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}