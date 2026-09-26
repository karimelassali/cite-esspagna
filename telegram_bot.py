"""Concise AI replies for the Cita Zarwal Telegram bot.

Run this continuously on a machine or hosting service. GitHub Actions is used
for scheduled appointment checks, not real-time chat polling.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

import requests
from openai import OpenAI

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("cita-zarwal-bot")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "").strip()
MODEL = os.getenv("NVIDIA_MODEL", "z-ai/glm-5.3").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
RTL_RE = re.compile(r"[\u0590-\u08FF\uFB1D-\uFDFD\uFE70-\uFEFC]")

SYSTEM_PROMPT = """You are Cita Zarwal, a helpful Moroccan AI assistant specialized in Spanish ICPPlus (Extranjería / Cita Previa) appointment questions.
Always respond in friendly, authentic Moroccan Darija (الدارجة المغربية).
Be accurate, practical, and concise: at most 3 short sentences or bullet points.
Never claim that an appointment is available unless confirmed.
Never ask for or store passwords, NIE numbers, or secret tokens.
"""


def require_configuration() -> None:
    missing = [name for name, value in (("TELEGRAM_TOKEN", TELEGRAM_TOKEN), ("NVIDIA_API_KEY", NVIDIA_API_KEY)) if not value]
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))


def telegram(method: str, payload: dict, timeout: int = 40) -> dict:
    response = requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data}")
    return data["result"]


def clean_reply(text: str) -> str:
    """Normalize a model response for compact, readable Telegram text."""
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    # Telegram has a 4096-char limit; leave a margin and make excessive output explicit.
    if len(text) > 3500:
        text = text[:3497].rsplit(" ", 1)[0] + "..."
    return ("\u200f" + text) if RTL_RE.search(text) else text


def ai_reply(user_text: str) -> str:
    client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=NVIDIA_API_KEY)
    completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        temperature=0.3,
        top_p=1,
        max_tokens=350,
        stream=False,
    )
    content = completion.choices[0].message.content
    if not content:
        raise RuntimeError("The AI service returned an empty response")
    return clean_reply(content)


def handle_message(message: dict) -> None:
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return

    if text.startswith("/start"):
        reply = "أهلاً بك فـ Cita Zarwal! 🇲🇦\nصيفط ليا أي سؤال عندك على مواعيد ICPPlus وغادي نجاوبك بالدارجة دغيا. أنا كانعلمك بالمواعيد وماكانحجزش بلاصتك."
    elif text.startswith("/status"):
        reply = "البوت خدام وكيصيفط تنبيه فور ما يلقى موعد متاح فـ ICPPlus إن شاء الله."
    else:
        try:
            reply = ai_reply(text)
        except Exception:
            LOG.exception("AI reply failed")
            reply = "سمح ليا، ما قدرتش نجاوب دابا. عاود صيفط ليا من بعد شوية عافاك."
    telegram("sendMessage", {"chat_id": chat_id, "text": reply, "disable_web_page_preview": True})


def main() -> None:
    require_configuration()
    offset = None
    LOG.info("Bot polling started with model %s", MODEL)
    while True:
        try:
            payload = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                payload["offset"] = offset
            updates = telegram("getUpdates", payload, timeout=45)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message")
                if message and not message.get("from", {}).get("is_bot"):
                    handle_message(message)
        except KeyboardInterrupt:
            LOG.info("Bot stopped")
            return
        except Exception:
            LOG.exception("Polling failed; retrying in 5 seconds")
            time.sleep(5)


if __name__ == "__main__":
    main()
