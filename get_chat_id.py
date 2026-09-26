"""Print Telegram chat IDs that have messaged this bot.

First open https://t.me/cita_zarwal_bot and send /start or any message.
Then set TELEGRAM_TOKEN in your shell and run this script.
"""

from __future__ import annotations

import os
import sys

import requests


token = os.getenv("TELEGRAM_TOKEN", "").strip()
if not token or token.casefold() in {"your new telegram token", "your telegram token"}:
    sys.exit("Set TELEGRAM_TOKEN first; do not put the token in this file.")

response = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30)
if response.status_code in {401, 404}:
    sys.exit("Telegram did not accept this token. Replace the example text with the real token from BotFather.")
response.raise_for_status()
payload = response.json()
if not payload.get("ok"):
    sys.exit("Telegram rejected the token.")

chat_ids = {}
for update in payload.get("result", []):
    message = update.get("message") or update.get("channel_post")
    if not message or "chat" not in message:
        continue
    chat = message["chat"]
    chat_ids[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name") or "private chat"

if not chat_ids:
    sys.exit("No messages found. Send /start to @cita_zarwal_bot, then run this again.")

for chat_id, label in chat_ids.items():
    print(f"CHAT_ID={chat_id} ({label})")
