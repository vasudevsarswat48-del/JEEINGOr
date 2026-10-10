"""Run once locally to log in and print a SESSION_STRING for your .env"""
import os
from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

load_dotenv()
api_id = int(os.environ["API_ID"])
api_hash = os.environ["API_HASH"]

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nSESSION_STRING=" + client.session.save())
    print("\nPut the line above in your .env. Keep it secret: it grants access to your account.")
