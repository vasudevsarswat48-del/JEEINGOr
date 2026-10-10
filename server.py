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
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import InputMessagesFilterVideo

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")  # empty = no login
SECRET = os.environ.get("COOKIE_SECRET", SESSION_STRING[:32])

CHUNK = 1024 * 1024
INDEX_TTL = 600  # seconds; new uploads appear within ~10 minutes (or use ?refresh=1)

with open("batches.json", encoding="utf-8") as f:
    RAW = json.load(f)

BATCHES: dict[str, dict] = {}
for b in RAW:
    subs = b.get("subjects") or [{"slug": "all", "name": "All Lectures", "icon": ""}]
    b["subjects"] = subs
    b["_subs"] = {s["slug"]: s for s in subs}
    BATCHES[b["slug"]] = b

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
entities: dict[str, object] = {}
msg_cache: dict[tuple, object] = {}
index_cache: dict[tuple, dict] = {}
index_locks: dict[tuple, asyncio.Lock] = {}


def chan_of(batch: dict, subject: dict) -> str:
    return str(subject.get("channel") or batch["channel"])


async def resolve_channel(ch: str):
    if ch not in entities:
        entities[ch] = await client.get_entity(int(ch) if re.fullmatch(r"-?\d+", ch) else ch)
    return entities[ch]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await client.start()
    for b in BATCHES.values():
        for s in b["subjects"]:
            await resolve_channel(chan_of(b, s))
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
async def logout():
    r = RedirectResponse("/login" if SITE_PASSWORD else "/", status_code=303)
    r.delete_cookie("auth")
    return r


# ---------- caption parsing ----------
_WORDS = r"(?:lecture|lect|lec|class|lesson|part|video|session|episode|ep|l)"
TRAIL = re.compile(rf"[\s\-–—:|,.(\[#_]*\b{_WORDS}(?![A-Za-z])[\s.\-–—:#_]*(\d+)[\s)\]]*$", re.I)
LEAD = re.compile(rf"^[\s\[(#]*\b{_WORDS}(?![A-Za-z])[\s.\-–—:#_]*(\d+)[\s)\]]*[\-–—:|,.\s_]+", re.I)
STRIP = " -–—:|,._#[]()"


def parse_caption(text: str | None, fname: str | None, line_n: int, custom):
    """Returns (title, chapter, lecture_no)."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if lines:
        line = lines[line_n - 1] if 0 < line_n <= len(lines) else lines[0]
    else:
        line = re.sub(r"\.\w{2,4}$", "", fname or "")
    line = re.sub(r"\s+", " ", line).strip()

    chapter, lecture = None, None
    if custom is not None:
        mm = custom.search(line)
        if mm:
            gd = mm.groupdict()
            chapter = (gd.get("chapter") or "").strip() or None
            if (gd.get("lecture") or "").isdigit():
                lecture = int(gd["lecture"])
    if chapter is None:
        mt = TRAIL.search(line)
        if mt:
            lecture, chapter = int(mt.group(1)), line[: mt.start()]
        else:
            ml = LEAD.match(line)
            if ml:
                lecture, chapter = int(ml.group(1)), line[ml.end():]
            else:
                chapter = line
    chapter = chapter.strip(STRIP) or "Other"
    return line, chapter, lecture


def norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", s.lower()).strip()


def video_info(m, line_n, custom):
    doc = m.document
    fname, duration = None, None
    for a in doc.attributes:
        if getattr(a, "file_name", None):
            fname = a.file_name
        if hasattr(a, "duration"):
            duration = int(a.duration)
    title, chapter, lecture = parse_caption(m.message, fname, line_n, custom)
    return {
        "id": m.id,
        "title": (title or f"Lecture {m.id}")[:160],
        "chapter": chapter,
        "lecture": lecture,
        "size": doc.size,
        "duration": duration,
        "mime": doc.mime_type,
        "date": m.date.isoformat(),
        "raw": (m.message or "")[:300],
    }


# ---------- helpers ----------
def get_batch(slug: str) -> dict:
    if slug not in BATCHES:
        raise HTTPException(404, "Unknown batch")
    return BATCHES[slug]


def get_subject(batch: dict, slug: str) -> dict:
    if slug not in batch["_subs"]:
        raise HTTPException(404, "Unknown subject")
    return batch["_subs"][slug]


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
        ent = await resolve_channel(chan_of(b, s))
        line_n = int(s.get("name_line") or b.get("name_line") or 1)
        pattern = s.get("chapter_regex") or b.get("chapter_regex")
        try:
            custom = re.compile(pattern, re.I) if pattern else None
        except re.error:
            custom = None

        kwargs = dict(limit=None, filter=InputMessagesFilterVideo)
        if s.get("search"):
            kwargs["search"] = s["search"]
        if s.get("topic_id"):
            kwargs["reply_to"] = int(s["topic_id"])

        vids = []
        async for m in client.iter_messages(ent, **kwargs):
            if m.document:
                vids.append(video_info(m, line_n, custom))
        vids.sort(key=lambda v: v["id"])

        groups: dict[str, dict] = {}
        for v in vids:
            k = norm(v["chapter"]) or "other"
            g = groups.get(k)
            if g is None:
                g = groups[k] = {
                    "id": hashlib.md5(k.encode()).hexdigest()[:8],
                    "name": v["chapter"],
                    "first": v["id"],
                    "videos": [],
                }
            g["videos"].append(v)
        chapters = []
        for g in sorted(groups.values(), key=lambda g: g["first"]):
            g["videos"].sort(key=lambda v: (v["lecture"] is None, v["lecture"] or 0, v["id"]))
            chapters.append(g)

        c = {"ts": time.time(), "videos": vids, "chapters": chapters,
             "by_id": {g["id"]: g for g in chapters}}
        index_cache[key] = c
        return c


def public(v: dict) -> dict:
    return {k: v[k] for k in ("id", "title", "chapter", "lecture", "size", "duration", "date")}


async def get_msg(bslug: str, sslug: str, msg_id: int):
    key = (bslug, sslug, msg_id)
    m = msg_cache.get(key)
    if m is None:
        b = get_batch(bslug)
        s = get_subject(b, sslug)
        ent = await resolve_channel(chan_of(b, s))
        m = await client.get_messages(ent, ids=msg_id)
        if not m or not m.document:
            raise HTTPException(404, "No media in that message")
        if len(msg_cache) > 1000:
            msg_cache.clear()
        msg_cache[key] = m
    return m


# ---------- API ----------
@app.get("/api/batches")
async def batches():
    return [
        {
            "slug": s,
            "name": b["name"],
            "description": b.get("description", ""),
            "icon": b.get("icon", ""),
            "subject_count": len(b["subjects"]),
        }
        for s, b in BATCHES.items()
    ]


@app.get("/api/batches/{bslug}")
async def batch_detail(bslug: str):
    b = get_batch(bslug)
    return {
        "slug": bslug,
        "name": b["name"],
        "description": b.get("description", ""),
        "subjects": [
            {"slug": s["slug"], "name": s["name"], "icon": s.get("icon", ""),
             "description": s.get("description", ""), "chapters": s.get("chapters", False)}
            for s in b["subjects"]
        ],
    }


@app.get("/api/batches/{bslug}/subjects/{sslug}/chapters")
async def chapters(bslug: str, sslug: str, refresh: int = 0):
    idx = await get_index(bslug, sslug, bool(refresh))
    return {
        "total": len(idx["videos"]),
        "chapters": [
            {
                "id": g["id"],
                "name": g["name"],
                "count": len(g["videos"]),
                "duration": sum(v["duration"] or 0 for v in g["videos"]),
                "video_ids": [v["id"] for v in g["videos"]],
            }
            for g in idx["chapters"]
        ],
    }


@app.get("/api/batches/{bslug}/subjects/{sslug}/videos")
async def videos(bslug: str, sslug: str, chapter: str | None = None, q: str | None = None,
                 offset: int = 0, limit: int = 1000, order: str = "old"):
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
async def debug(bslug: str, sslug: str, n: int = 30):
    """Open in browser to check how captions are being read."""
    idx = await get_index(bslug, sslug, True)
    return [
        {"raw_caption": v["raw"], "title": v["title"], "chapter": v["chapter"], "lecture": v["lecture"]}
        for v in idx["videos"][:n]
    ]


@app.get("/api/batches/{bslug}/subjects/{sslug}/thumb/{msg_id}")
async def thumb(bslug: str, sslug: str, msg_id: int):
    m = await get_msg(bslug, sslug, msg_id)
    data = await client.download_media(m, file=bytes, thumb=-1)
    if not data:
        raise HTTPException(404, "No thumbnail")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/stream/{bslug}/{sslug}/{msg_id}")
async def stream(bslug: str, sslug: str, msg_id: int, request: Request):
    m = await get_msg(bslug, sslug, msg_id)
    doc = m.document
    size = doc.size
    mime = doc.mime_type or "video/mp4"

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