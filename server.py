import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from telethon import TelegramClient, utils
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetChannelsRequest
from telethon.tl.types import InputChannel, InputPeerChat, PeerChannel

from common import doc_name, make_video, norm

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ.get("SESSION_STRING", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BOT_MODE = bool(BOT_TOKEN)          # bot streams the videos; the list of lectures comes from index.json
if not BOT_MODE and not SESSION_STRING:
    raise SystemExit("Set BOT_TOKEN (recommended) or SESSION_STRING in .env")
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")  # empty = no login
SECRET = os.environ.get("COOKIE_SECRET", (BOT_TOKEN or SESSION_STRING)[:32])
INDEX_FILE = os.environ.get("INDEX_FILE", "index.json")

CHUNK = 1024 * 1024
INDEX_TTL = 600  # seconds; new uploads appear within ~10 minutes (or use ?refresh=1)

with open("batches.json", encoding="utf-8") as f:
    RAW = json.load(f)

BATCHES: dict[str, dict] = {}
for b in RAW:
    subs = b.get("subjects") or [{"slug": "all", "name": "All Lectures", "icon": ""}]
    b["subjects"] = subs
    b["_subs"] = {s["slug"]: s for s in subs}
    b["password"] = "" if b.get("password") in (None, "") else str(b["password"])   # optional per-batch password
    BATCHES[b["slug"]] = b

client = TelegramClient("bot" if BOT_MODE else StringSession(SESSION_STRING), API_ID, API_HASH)
entities: dict[str, object] = {}
msg_cache: dict[tuple, tuple] = {}      # key -> (time, message)
index_cache: dict[tuple, dict] = {}
index_locks: dict[tuple, asyncio.Lock] = {}


def chan_of(batch: dict, subject: dict) -> str:
    return str(subject.get("channel") or batch["channel"])


async def resolve_channel(ch: str):
    if ch in entities:
        return entities[ch]
    if BOT_MODE and re.fullmatch(r"-?\d+", ch):
        # a bot has no dialog list, so ask Telegram for the channel directly (works once the bot is a member/admin)
        real_id, peer = utils.resolve_id(int(ch))
        if peer is PeerChannel:
            res = await client(GetChannelsRequest([InputChannel(real_id, 0)]))
            ent = res.chats[0]
        else:
            ent = InputPeerChat(real_id)
    else:
        ent = await client.get_entity(int(ch) if re.fullmatch(r"-?\d+", ch) else ch)
    entities[ch] = ent
    return ent


@asynccontextmanager
async def lifespan(app: FastAPI):
    if BOT_MODE:
        await client.start(bot_token=BOT_TOKEN)
    else:
        await client.start()
    for b in BATCHES.values():
        for s in b["subjects"]:
            try:
                await resolve_channel(chan_of(b, s))
            except Exception as e:
                if not BOT_MODE:
                    raise
                print(f"WARNING: bot cannot open channel {chan_of(b, s)} ({e}). Is the bot added as admin there?")
    yield
    await client.disconnect()


app = FastAPI(lifespan=lifespan)


# ---------- password gate ----------
def token() -> str:
    return hmac.new(SECRET.encode(), SITE_PASSWORD.encode(), hashlib.sha256).hexdigest()


@app.middleware("http")
async def gate(request: Request, call_next):
    if SITE_PASSWORD and request.url.path not in ("/login", "/logout"):
        if request.cookies.get("auth") != token():
            if request.url.path.startswith(("/api", "/stream")):
                return Response("Unauthorized", status_code=401)
            return RedirectResponse("/login")
    return await call_next(request)


@app.get("/login")
async def login_page():
    return FileResponse("login.html")


@app.post("/login")
async def login(password: str = Form(...)):
    if not hmac.compare_digest(password, SITE_PASSWORD):
        return RedirectResponse("/login?bad=1", status_code=303)
    r = RedirectResponse("/", status_code=303)
    # session cookie (no max_age): browser forgets it when closed
    r.set_cookie("auth", token(), httponly=True, samesite="lax")
    return r


@app.get("/logout")
async def logout(request: Request):
    r = RedirectResponse("/login" if SITE_PASSWORD else "/", status_code=303)
    r.delete_cookie("auth")
    for k in request.cookies:            # also forget every unlocked batch
        if k.startswith("bk_"):
            r.delete_cookie(k)
    return r


# ---------- helpers ----------
def get_batch(slug: str) -> dict:
    if slug not in BATCHES:
        raise HTTPException(404, "Unknown batch")
    return BATCHES[slug]


# ---------- per-batch password ----------
def batch_key(slug: str) -> str:
    return "bk_" + hashlib.md5(slug.encode()).hexdigest()[:12]


def batch_token(b: dict) -> str:
    # changes automatically when the batch password is changed, so old unlocks stop working
    return hmac.new(SECRET.encode(), f"batch:{b['slug']}:{b['password']}".encode(), hashlib.sha256).hexdigest()


def batch_unlocked(request: Request, b: dict) -> bool:
    if not b.get("password"):
        return True
    return hmac.compare_digest(request.cookies.get(batch_key(b["slug"]), ""), batch_token(b))


def require_batch(request: Request, bslug: str) -> dict:
    b = get_batch(bslug)
    if not batch_unlocked(request, b):
        raise HTTPException(403, "Batch locked")
    return b


attempts: dict[str, list[float]] = {}


def get_subject(batch: dict, slug: str) -> dict:
    if slug not in batch["_subs"]:
        raise HTTPException(404, "Unknown subject")
    return batch["_subs"][slug]


def chapter_specs(s: dict) -> list[dict]:
    """Reads the optional "chapters" list of a subject in batches.json.
    Each item is either a plain name ("Kinematics") or
    {"name": "Kinematics", "match": ["kinematic", "motion in a straight line"]}."""
    out = []
    for i, c in enumerate(s.get("chapters") or [], 1):
        if isinstance(c, str):
            name, match = c, [c]
        elif isinstance(c, dict):
            name = c.get("name") or f"Chapter {i}"
            match = c.get("match") or [name]
            if isinstance(match, str):
                match = [match]
        else:
            continue
        out.append({"id": f"c{i}", "num": i, "name": name,
                    "match": [norm(m) for m in match if norm(str(m))]})
    return out


def assign_chapter(v: dict, specs: list[dict]):
    """Chapter whose name is found in the caption. If several are found, the longest (most specific) wins."""
    hay = f" {v['hay']} "
    best, best_len = None, 0
    for sp in specs:
        for m in sp["match"]:
            if f" {m} " in hay and len(m) > best_len:
                best, best_len = sp, len(m)
    return best


_index_file = {"mtime": None, "items": {}}


def load_index_file() -> dict:
    try:
        mt = os.path.getmtime(INDEX_FILE)
    except OSError:
        return {}
    if mt != _index_file["mtime"]:
        with open(INDEX_FILE, encoding="utf-8") as f:
            _index_file.update(mtime=mt, items=json.load(f).get("items", {}))
    return _index_file["items"]


async def scan_channel(b: dict, s: dict) -> list[dict]:
    from common import scan
    return await scan(client, await resolve_channel(chan_of(b, s)), s)


async def get_index(bslug: str, sslug: str, refresh: bool = False) -> dict:
    key = (bslug, sslug)
    c = index_cache.get(key)
    if c and not refresh and time.time() - c["ts"] < INDEX_TTL:
        return c
    lock = index_locks.setdefault(key, asyncio.Lock())
    async with lock:
        c = index_cache.get(key)
        if c and not refresh and time.time() - c["ts"] < INDEX_TTL:
            return c
        b = get_batch(bslug)
        s = get_subject(b, sslug)
        line_n = int(s.get("name_line") or b.get("name_line") or 1)
        pattern = s.get("chapter_regex") or b.get("chapter_regex")
        try:
            custom = re.compile(pattern, re.I) if pattern else None
        except re.error:
            custom = None

        if BOT_MODE:
            # list of lectures was prepared by build_index.py (a bot cannot list/search a channel)
            recs = load_index_file().get(f"{bslug}/{sslug}", [])
            vids = [make_video(r, line_n, custom) for r in recs]
        else:
            vids = [make_video(r, line_n, custom) for r in await scan_channel(b, s)]
        vids.sort(key=lambda v: v["id"])

        specs = chapter_specs(s)
        order = lambda v: (v["lecture"] is None, v["lecture"] or 0, v["id"])
        chapters = []
        if specs:
            groups = {sp["id"]: {**sp, "videos": []} for sp in specs}
            other = []
            for v in vids:
                sp = assign_chapter(v, specs)
                v["chapter"] = sp["name"] if sp else None
                (groups[sp["id"]]["videos"] if sp else other).append(v)
            chapters = list(groups.values())
            if other and s.get("show_other", True):
                chapters.append({"id": "other", "num": None, "name": "Other lectures", "videos": other})
            for g in chapters:
                g["videos"].sort(key=order)
        c = {"ts": time.time(), "videos": vids, "chapters": chapters,
             "by_id": {g["id"]: g for g in chapters}}
        index_cache[key] = c
        return c


def public(v: dict) -> dict:
    return {k: v[k] for k in ("id", "title", "chapter", "lecture", "size", "duration", "date")}


async def get_msg(bslug: str, sslug: str, msg_id: int):
    key = (bslug, sslug, msg_id)
    hit = msg_cache.get(key)
    if hit and time.time() - hit[0] < 1800:        # file references expire, so re-fetch every 30 min
        return hit[1]
    b = get_batch(bslug)
    s = get_subject(b, sslug)
    ent = await resolve_channel(chan_of(b, s))
    m = await client.get_messages(ent, ids=msg_id)
    if not m or not m.document:
        raise HTTPException(404, "No media in that message")
    if len(msg_cache) > 1000:
        msg_cache.clear()
    msg_cache[key] = (time.time(), m)
    return m


# ---------- API ----------
@app.get("/api/batches")
async def batches(request: Request):
    return [
        {
            "slug": s,
            "name": b["name"],
            "description": b.get("description", ""),
            "icon": b.get("icon", ""),
            "subject_count": len(b["subjects"]),
            "locked": bool(b["password"]),
            "unlocked": batch_unlocked(request, b),
        }
        for s, b in BATCHES.items()
    ]


@app.post("/api/batches/{bslug}/unlock")
async def unlock(bslug: str, request: Request):
    b = get_batch(bslug)
    if not b["password"]:
        return {"ok": True}
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    key = f"{ip}:{bslug}"
    now = time.time()
    hist = [t for t in attempts.get(key, []) if now - t < 300]
    if len(hist) >= 8:
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
    try:
        body = await request.json()
    except Exception:
        body = {}
    pw = str(body.get("password", "")) if isinstance(body, dict) else ""
    if not hmac.compare_digest(pw.encode(), b["password"].encode()):
        hist.append(now)
        attempts[key] = hist
        raise HTTPException(403, "Wrong password")
    attempts.pop(key, None)
    r = JSONResponse({"ok": True})
    r.set_cookie(batch_key(bslug), batch_token(b), httponly=True, samesite="lax")   # session cookie
    return r


@app.get("/api/batches/{bslug}")
async def batch_detail(request: Request, bslug: str):
    b = require_batch(request, bslug)
    return {
        "slug": bslug,
        "name": b["name"],
        "description": b.get("description", ""),
        "subjects": [
            {"slug": s["slug"], "name": s["name"], "icon": s.get("icon", ""),
             "description": s.get("description", ""), "has_chapters": bool(s.get("chapters"))}
            for s in b["subjects"]
        ],
    }


@app.get("/api/batches/{bslug}/subjects/{sslug}/chapters")
async def chapters(request: Request, bslug: str, sslug: str, refresh: int = 0):
    require_batch(request, bslug)
    idx = await get_index(bslug, sslug, bool(refresh))
    return {
        "total": len(idx["videos"]),
        "chapters": [
            {
                "id": g["id"],
                "num": g.get("num"),
                "name": g["name"],
                "count": len(g["videos"]),
                "duration": sum(v["duration"] or 0 for v in g["videos"]),
                "video_ids": [v["id"] for v in g["videos"]],
            }
            for g in idx["chapters"]
        ],
    }


@app.get("/api/batches/{bslug}/subjects/{sslug}/videos")
async def videos(request: Request, bslug: str, sslug: str, chapter: str | None = None, q: str | None = None,
                 offset: int = 0, limit: int = 1000, order: str = "old"):
    require_batch(request, bslug)
    idx = await get_index(bslug, sslug)
    if chapter:
        g = idx["by_id"].get(chapter)
        if not g:
            raise HTTPException(404, "Unknown chapter")
        items = g["videos"]
        name = g["name"]
    else:
        items = idx["videos"] if order == "old" else list(reversed(idx["videos"]))
        name = None
    if q:
        ql = q.lower()
        items = [v for v in items if ql in v["title"].lower()]
    page = items[offset: offset + limit]
    return {"chapter": name, "total": len(items), "videos": [public(v) for v in page]}


@app.get("/api/batches/{bslug}/subjects/{sslug}/debug")
async def debug(request: Request, bslug: str, sslug: str, n: int = 60):
    """Open in browser to check how captions are being read."""
    require_batch(request, bslug)
    idx = await get_index(bslug, sslug, True)
    return [
        {"raw_caption": v["raw"], "title": v["title"], "chapter": v["chapter"] or "(no chapter matched)", "lecture": v["lecture"]}
        for v in idx["videos"][:n]
    ]


@app.get("/api/batches/{bslug}/subjects/{sslug}/thumb/{msg_id}")
async def thumb(request: Request, bslug: str, sslug: str, msg_id: int):
    require_batch(request, bslug)
    m = await get_msg(bslug, sslug, msg_id)
    data = await client.download_media(m, file=bytes, thumb=-1)
    if not data:
        raise HTTPException(404, "No thumbnail")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/stream/{bslug}/{sslug}/{msg_id}")
async def stream(bslug: str, sslug: str, msg_id: int, request: Request):
    require_batch(request, bslug)
    m = await get_msg(bslug, sslug, msg_id)
    doc = m.document
    size = doc.size
    mime = doc.mime_type or "video/mp4"
    if not mime.startswith("video/"):          # files sent as documents often say application/octet-stream
        mime = "video/webm" if doc_name(doc).lower().endswith(".webm") else "video/mp4"

    start, end, status = 0, size - 1, 200
    rng = request.headers.get("range")
    if rng:
        match = re.match(r"bytes=(\d*)-(\d*)", rng)
        if match:
            s, e = match.groups()
            if s:
                start = int(s)
                end = int(e) if e else size - 1
            elif e:
                start = max(0, size - int(e))
            if start >= size or end < start:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            end = min(end, size - 1)
            status = 206

    length = end - start + 1
    aligned = start - (start % CHUNK)
    skip = start - aligned

    async def body():
        remaining, to_skip = length, skip
        try:
            async for chunk in client.iter_download(doc, offset=aligned, request_size=CHUNK, file_size=size):
                if to_skip:
                    chunk, to_skip = chunk[to_skip:], 0
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                remaining -= len(chunk)
                yield bytes(chunk)
                if remaining <= 0:
                    break
        except Exception:
            return

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length), "Cache-Control": "private, max-age=3600"}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(body(), status_code=status, media_type=mime, headers=headers)


@app.get("/")
async def index():
    with open("index.html", encoding="utf-8") as f:
        html = f.read().replace("__PROTECTED__", "1" if SITE_PASSWORD else "0")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})
