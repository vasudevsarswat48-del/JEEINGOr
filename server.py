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
from telethon import TelegramClient
from telethon.tl.types import InputMessagesFilterVideo, InputMessagesFilterDocument

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]  # Using Bot Token instead of SESSION_STRING
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")  # empty = no login
SECRET = os.environ.get("COOKIE_SECRET", BOT_TOKEN[:32])

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

# Initialize TelegramClient using the Bot Token session name instead of StringSession
client = TelegramClient('bot_session', API_ID, API_HASH)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Safe serverless lifespan: connect bot safely on demand
    yield
    try:
        if client.is_connected():
            await client.disconnect()
    except Exception:
        pass

# Defined prominently at the top level so Vercel detects it instantly
app = FastAPI(lifespan=lifespan)

entities: dict[str, object] = {}
msg_cache: dict[tuple, object] = {}
index_cache: dict[tuple, dict] = {}
index_locks: dict[tuple, asyncio.Lock] = {}


def chan_of(batch: dict, subject: dict) -> str:
    return str(subject.get("channel") or batch["channel"])


async def ensure_connected():
    if not client.is_connected():
        # Connect using the bot token authentication flow
        await client.start(bot_token=BOT_TOKEN)


async def resolve_channel(ch: str):
    await ensure_connected()
    if ch not in entities:
        entities[ch] = await client.get_entity(int(ch) if re.fullmatch(r"-?\d+", ch) else ch)
    return entities[ch]


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


VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts", ".3gp")


def doc_name(doc) -> str:
    for a in doc.attributes:
        if getattr(a, "file_name", None):
            return a.file_name
    return ""


def is_video_doc(doc) -> bool:
    mt = (doc.mime_type or "").lower()
    return mt.startswith("video/") or doc_name(doc).lower().endswith(VIDEO_EXT)


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
        "hay": norm((m.message or "") + " " + (fname or "")),   # whole caption + file name, used for chapter matching
    }


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
        
        try:
            ent = await resolve_channel(chan_of(b, s))
        except Exception as e:
            print(f"CRITICAL: Failed to resolve channel for {bslug}/{sslug}: {e}")
            raise HTTPException(500, f"Channel resolution error: {e}")

        line_n = int(s.get("name_line") or b.get("name_line") or 1)
        pattern = s.get("chapter_regex") or b.get("chapter_regex")
        try:
            custom = re.compile(pattern, re.I) if pattern else None
        except re.error:
            custom = None

        kwargs = dict(limit=None)
        if s.get("search"):
            kwargs["search"] = s["search"]
        if s.get("topic_id"):
            kwargs["reply_to"] = int(s["topic_id"])

        vids, seen = [], set()
        await ensure_connected()
        
        try:
            for flt in (InputMessagesFilterVideo, InputMessagesFilterDocument):
                async for m in client.iter_messages(ent, filter=flt, **kwargs):
                    if not m.document or m.id in seen or not is_video_doc(m.document):
                        continue
                    seen.add(m.id)
                    vids.append(video_info(m, line_n, custom))
        except Exception as e:
            print(f"CRITICAL: Failed to iterate messages for {bslug}/{sslug}: {e}")
            raise HTTPException(500, f"Telegram fetch error: {e}")

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
    return {k: v[k] for k in ("id", "title", "chapter", "lecture", "size", "duration", "date
