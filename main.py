import asyncio
import json
import logging
import os

from aiohttp import web
from aiohttp_cors import setup as cors_setup, ResourceOptions
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from google import genai

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ai-assistant")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-domain.example")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

# АКТУАЛЬНАЯ МОДЕЛЬ
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if not BOT_TOKEN or not GEMINI_API_KEY:
    raise RuntimeError("Set BOT_TOKEN and GEMINI_API_KEY environment variables.")

client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# Функция для вывода доступных моделей в логи (для отладки)
async def log_available_models():
    try:
        models = await asyncio.to_thread(client.models.list)
        log.info("--- ДОСТУПНЫЕ МОДЕЛИ ---")
        for m in models:
            if "generateContent" in m.supported_generation_methods:
                log.info(f"Модель: {m.name}")
        log.info("------------------------")
    except Exception as e:
        log.error(f"Не удалось получить список моделей: {e}")


async def generate_answer(prompt: str) -> str:
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                client.models.generate_content,
                model=GEMINI_MODEL,
                contents=prompt
            ),
            timeout=25.0
        )
        answer = getattr(response, "text", None)
        if not answer:
            raise RuntimeError("Gemini вернул пустой ответ")
        return answer.strip()
    except asyncio.TimeoutError:
        log.error("Google не ответил за 25 секунд")
        raise RuntimeError("Превышено время ожидания. Попробуйте позже.")
    except Exception as e:
        log.error(f"Ошибка при обращении к Gemini: {e}")
        raise


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(
                text="Открыть AI Assistant",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        ]],
        resize_keyboard=True,
    )
    await message.answer(
        "Привет! Нажми кнопку ниже, чтобы открыть AI Assistant.",
        reply_markup=keyboard,
    )


async def chat_handler(request: web.Request) -> web.Response:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="Expected application/json")

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="Invalid JSON")

    text = payload.get("message", "")
    init_data = payload.get("initData", "")

    if not isinstance(text, str) or not text.strip():
        raise web.HTTPBadRequest(text="Сообщение не может быть пустым")

    try:
        answer = await generate_answer(text.strip())
    except Exception as e:
        return web.json_response(
            {"error": str(e)},
            status=502,
        )

    return web.json_response({"reply": answer})


async def health_handler(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def main() -> None:
    # Выводим список моделей при старте
    await log_available_models()

    app = web.Application(client_max_size=16 * 1024)
    
    cors = cors_setup(app, defaults={
        "https://i8103719-ship-it.github.io": ResourceOptions(
            allow_credentials=False,
            expose_headers="*",
            allow_headers="*",
            allow_methods="*"
        )
    })
    
    app.router.add_post("/api/chat", chat_handler)
    app.router.add_get("/health", health_handler)

    for route in list(app.router.routes()):
        cors.add(route)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    log.info("HTTP server listening on %s:%s", HOST, PORT)

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
