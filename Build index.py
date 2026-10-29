"""Run this on YOUR PC (it uses your personal session). It reads every channel in batches.json and
writes index.json. The server (which only uses the bot) then shows these lectures.
Run it again whenever you add new lectures:   python build_index.py
"""
import asyncio
import json
import os
import re
import time

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from common import scan

load_dotenv()
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
OUT = os.environ.get("INDEX_FILE", "index.json")


async def main():
    with open("batches.json", encoding="utf-8") as f:
        batches = json.load(f)
    items, ents = {}, {}
    async with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        for b in batches:
            for s in b.get("subjects") or [{"slug": "all"}]:
                ch = str(s.get("channel") or b["channel"])
                if ch not in ents:
                    ents[ch] = await client.get_entity(int(ch) if re.fullmatch(r"-?\d+", ch) else ch)
                recs = await scan(client, ents[ch], s)
                recs.sort(key=lambda r: r["id"])
                items[f"{b['slug']}/{s['slug']}"] = recs
                print(f"{b['slug']}/{s['slug']}: {len(recs)} videos")
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "items": items}, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    print(f"\nSaved {OUT}. Restart is NOT needed; the site picks it up within ~10 minutes.")


asyncio.run(main())
