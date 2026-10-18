"""Find your Telegram chat id and verify the bot works.

Usage:
  1. In BotFather send /newbot, follow the prompts, copy the token.
  2. Put it in .env as TELEGRAM_BOT_TOKEN=...
  3. Open your new bot in Telegram and send it any message, e.g. "hi".
  4. Run: .venv/bin/python scripts/telegram_setup.py

It reads the chat id from the message you sent, writes it to .env, and sends
a confirmation message back so you know the round trip works.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"


def read_env() -> dict[str, str]:
    if not ENV.exists():
        sys.exit(".env not found")
    out = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def write_env(key: str, value: str) -> None:
    text = ENV.read_text()
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    ENV.write_text(text)


def main() -> int:
    env = read_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        sys.exit(
            "TELEGRAM_BOT_TOKEN is empty in .env\n"
            "  1. Open BotFather in Telegram\n"
            "  2. Send /newbot and follow the prompts\n"
            "  3. Paste the token into .env as TELEGRAM_BOT_TOKEN=...\n"
            "  4. Message your new bot, then run this again"
        )

    api = f"https://api.telegram.org/bot{token}"

    me = requests.get(f"{api}/getMe", timeout=20).json()
    if not me.get("ok"):
        sys.exit(f"token rejected by Telegram: {me.get('description')}")
    bot_name = me["result"]["username"]
    print(f"bot verified: @{bot_name}")

    updates = requests.get(f"{api}/getUpdates", timeout=20).json()
    if not updates.get("ok"):
        sys.exit(f"getUpdates failed: {updates.get('description')}")

    chats = {}
    for u in updates.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            who = chat.get("username") or chat.get("first_name") or chat.get("title")
            chats[chat["id"]] = who

    if not chats:
        sys.exit(
            f"no messages found.\n"
            f"  Open https://t.me/{bot_name} in Telegram, press Start, send any\n"
            f"  message, then run this script again."
        )

    if len(chats) > 1:
        print("\nmultiple chats found:")
        for cid, who in chats.items():
            print(f"  {cid}  {who}")
        chat_id = str(next(iter(chats)))
        print(f"using the first: {chat_id}")
    else:
        chat_id = str(next(iter(chats)))

    who = chats[int(chat_id)]
    print(f"chat id: {chat_id}  ({who})")

    write_env("TELEGRAM_CHAT_ID", chat_id)
    print("written to .env")

    sent = requests.post(
        f"{api}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": (
                "job-agent connected.\n\n"
                "This is where job matches will arrive for approval. "
                "Nothing is submitted without you tapping approve."
            ),
        },
        timeout=20,
    ).json()
    print("round trip OK, check Telegram" if sent.get("ok")
          else f"send failed: {sent.get('description')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
