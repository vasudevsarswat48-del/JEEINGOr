# Telegram lecture streamer (multi-batch)

Streams videos from several Telegram channels/groups (one per batch) straight to the browser. Nothing is stored on your server; bytes are fetched from Telegram on demand, with seeking.

## Setup

1. Get `API_ID` and `API_HASH` at https://my.telegram.org -> API development tools.
2. `pip install -r requirements.txt`
3. Create `.env`:

```
API_ID=123456
API_HASH=your_api_hash
SESSION_STRING=
SITE_PASSWORD=choose-a-password   # leave empty to disable the login page
COOKIE_SECRET=any-long-random-string
```

4. Run `python gen_session.py`, log in with your phone number, and paste the printed `SESSION_STRING=...` into `.env`.
5. Edit `batches.json`, one entry per channel/group:

```json
[
  {"slug": "neet-2026", "name": "NEET 2026 Batch", "channel": "-1001234567890"},
  {"slug": "jee-2026",  "name": "JEE 2026 Batch",  "channel": "my_channel_username"}
]
```

`channel` can be a public username or the numeric id (`-100...`) for private channels. The logged-in account must be a member of each one.

6. `uvicorn server:app --host 0.0.0.0 --port 8000` and open http://localhost:8000

## Features

- Batch dropdown, search by caption, oldest-first (lecture order) or newest-first.
- Resume where you left off, "Watched" badge, progress bar (saved in the viewer's browser).
- Autoplay next lecture, playback speed control.
- Optional shared password login.

## Notes

- Use a secondary Telegram account for the session; the session string gives full access to it. Never commit `.env`.
- The Bot API can't be used: it limits downloads to 20 MB.
- Browsers play H.264/AAC MP4 and WebM. MKV/AVI/HEVC may not play.
- Needs a long-running Python host (VPS, Railway, Render, Fly.io), not a static host.
- Only stream content you own or have permission to distribute.
