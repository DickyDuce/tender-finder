"""Minimal Telegram Bot API client (just sendMessage). Token and chat id come from the environment."""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

API = "https://api.telegram.org/bot{token}/{method}"


def configured():
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send(text, chat_id=None):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (see README: Telegram setup)")
    r = requests.post(
        API.format(token=token, method="sendMessage"),
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    )
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not body.get("ok"):
        # Never echo the token: report only Telegram's description.
        raise RuntimeError(f"Telegram error: {body.get('description', r.status_code)}")
    return body["result"]["message_id"]


def find_chat_ids():
    """After you message your bot once, this lists the chats it can post to (helper for setup)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    r = requests.get(API.format(token=token, method="getUpdates"), timeout=30).json()
    chats = {}
    for u in r.get("result", []):
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title") or chat.get("first_name") or chat.get("username")
    return chats


if __name__ == "__main__":
    print(find_chat_ids() or "No chats yet: send any message to your bot, then run this again.")
