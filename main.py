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
from google.genai import errors

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ai-assistant")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-domain.example")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

# АКТУАЛЬНАЯ СТАБИЛЬНАЯ МОДЕЛЬ
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

if not BOT_TOKEN or not GEMINI_API_KEY:
    raise RuntimeError("Set BOT_TOKEN and GEMINI_API_KEY environment variables.")

client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


async def generate_answer(prompt: str) -> str:
    # Делаем до 3 попыток с нарастающей задержкой при перегрузке (503)
    for attempt in range(3):
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
        except (errors.APIError, errors.ServerError) as e:
            # Если сервер перегружен (503), пробуем еще раз
            if getattr(e, 'code', None) == 503 or "UNAVAILABLE" in str(e):
                log.warning(f"Попытка {attempt+1} не удалась (503). Ждем...")
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
            log.error(f"Ошибка API Gemini: {e}")
            raise RuntimeError("Нейросеть временно перегружена. Попробуйте через минуту.")
        except asyncio.TimeoutError:
            log.error("Google не ответил за 25 секунд")
            raise RuntimeError("Превышено время ожидания. Попробуйте позже.")
        except Exception as e:
            log.error(f"Неизвестная ошибка: {e}")
            raise
    raise RuntimeError("Не удалось получить ответ после нескольких попыток.")


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
    if not isinstance(text, str) or not text.strip():
        raise web.HTTPBadRequest(text="Сообщение не может быть пустым")

    try:
        answer = await generate_answer(text.strip())
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)

    return web.json_response({"reply": answer})


async def health_handler(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def main() -> None:
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
