"""Helpers shared by server.py and build_index.py (caption parsing, video detection)."""
import re

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


def extract(m) -> dict:
    """Plain, JSON-able record of a Telegram message that holds a video (used for index.json)."""
    doc = m.document
    duration = None
    for a in doc.attributes:
        if hasattr(a, "duration"):
            duration = int(a.duration)
    return {
        "id": m.id,
        "text": m.message or "",
        "fname": doc_name(doc),
        "size": doc.size,
        "duration": duration,
        "mime": doc.mime_type,
        "date": m.date.isoformat(),
    }


def make_video(rec: dict, line_n: int, custom) -> dict:
    title, chapter, lecture = parse_caption(rec["text"], rec["fname"] or None, line_n, custom)
    return {
        "id": rec["id"],
        "title": (title or f"Lecture {rec['id']}")[:160],
        "chapter": chapter,
        "lecture": lecture,
        "size": rec["size"],
        "duration": rec["duration"],
        "mime": rec["mime"],
        "date": rec["date"],
        "raw": rec["text"][:300],
        "hay": norm(rec["text"] + " " + (rec["fname"] or "")),   # whole caption + file name, used for chapter matching
    }


async def scan(client, ent, subject: dict) -> list[dict]:
    """Lists every video of a channel/topic (needs a USER session, a bot cannot list history)."""
    from telethon.tl.types import InputMessagesFilterDocument, InputMessagesFilterVideo
    kwargs = dict(limit=None)
    if subject.get("search"):
        kwargs["search"] = subject["search"]
    if subject.get("topic_id"):
        kwargs["reply_to"] = int(subject["topic_id"])
    out, seen = [], set()
    for flt in (InputMessagesFilterVideo, InputMessagesFilterDocument):     # videos sent as video AND as file (abc.mp4)
        async for m in client.iter_messages(ent, filter=flt, **kwargs):
            if m.document and m.id not in seen and is_video_doc(m.document):
                seen.add(m.id)
                out.append(extract(m))
    return out
