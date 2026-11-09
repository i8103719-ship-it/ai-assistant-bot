import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import parse_qsl

from aiohttp import web
from aiohttp_cors import setup as cors_setup, ResourceOptions # Импорт для CORS
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
import google.generativeai as genai

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ai-assistant")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-domain.example")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
MAX_INIT_DATA_AGE = int(os.getenv("MAX_INIT_DATA_AGE", "86400"))

if not BOT_TOKEN or not GEMINI_API_KEY:
    raise RuntimeError("Set BOT_TOKEN and GEMINI_API_KEY environment variables.")

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel(GEMINI_MODEL)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def validate_telegram_init_data(init_data: str) -> dict:
    """Validate Telegram Mini App initData and return its decoded fields."""
    if not init_data:
        raise web.HTTPUnauthorized(text="Missing Telegram initData")

    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash")
    except (ValueError, KeyError):
        raise web.HTTPUnauthorized(text="Invalid Telegram initData")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(pairs.items())
    )
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=BOT_TOKEN.encode(),
        digestmod=hashlib.sha256,
    ).digest()
    calculated_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise web.HTTPUnauthorized(text="Telegram initData signature mismatch")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        raise web.HTTPUnauthorized(text="Invalid auth_date")

    if auth_date <= 0 or time.time() - auth_date > MAX_INIT_DATA_AGE:
        raise web.HTTPUnauthorized(text="Telegram session expired")

    user = {}
    if pairs.get("user"):
        try:
            user = json.loads(pairs["user"])
        except json.JSONDecodeError:
            raise web.HTTPUnauthorized(text="Invalid Telegram user data")

    return {"user": user, "auth_date": auth_date}


async def generate_answer(prompt: str) -> str:
    response = await asyncio.to_thread(gemini_model.generate_content, prompt)
    answer = getattr(response, "text", None)
    if not answer:
        raise RuntimeError("Gemini returned an empty response")
    return answer.strip()


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
        raise web.HTTPBadRequest(text="Message cannot be empty")
    if len(text) > 8000:
        raise web.HTTPRequestEntityTooLarge(
            max_size=8000, actual_size=len(text), text="Message is too long"
        )

    telegram_data = validate_telegram_init_data(init_data)
    user = telegram_data["user"]
    log.info("Chat request from Telegram user_id=%s", user.get("id", "unknown"))

    try:
        answer = await generate_answer(text.strip())
    except Exception:
        log.exception("Gemini request failed")
        return web.json_response(
            {"error": "Не удалось получить ответ от AI. Попробуйте ещё раз."},
            status=502,
        )

    return web.json_response({"reply": answer})


async def health_handler(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def main() -> None:
    app = web.Application(client_max_size=16 * 1024)
    
    # --- НАСТРОЙКА CORS (Чтобы сайт с GitHub мог делать запросы) ---
    cors = cors_setup(app, defaults={
        "*": ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods="*"
        )
    })
    
    app.router.add_post("/api/chat", chat_handler)
    app.router.add_get("/health", health_handler)

    # Применяем CORS ко всем маршрутам
    for route in list(app.router.routes()):
        cors.add(route)
    # -------------------------------------------------------------

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
