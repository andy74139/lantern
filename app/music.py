"""YouTube Music 常播清單：貼連結加入，從首頁播放時記錄次數，排出「最近常播」。"""
import math
import re
import time
from urllib.parse import parse_qs, urlparse

import httpx

from . import db

SCHEMA = """create table if not exists music (
  id text primary key, kind text, ref text, title text, author text,
  plays integer default 0, last_played real, pinned integer default 0, added real)"""
VID = re.compile(r"^[A-Za-z0-9_-]{11}$")
LIST = re.compile(r"^[A-Za-z0-9_-]{10,64}$")


def init():
    db.execute(SCHEMA)


def parse(url):
    """music.youtube.com／youtube.com／youtu.be 的歌曲或播放清單連結。"""
    url = url.strip()
    if VID.match(url):
        return "video", url
    u = urlparse(url if "://" in url else "https://" + url)
    host = (u.hostname or "").lower()
    if not (host.endswith("youtube.com") or host == "youtu.be"):
        raise ValueError("請貼 YouTube Music 或 YouTube 的連結")
    q = parse_qs(u.query)
    if host == "youtu.be" and VID.match(u.path.strip("/")):
        return "video", u.path.strip("/")
    if q.get("v") and VID.match(q["v"][0]):
        return "video", q["v"][0]
    m = re.match(r"^/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})", u.path)
    if m:
        return "video", m.group(1)
    if q.get("list") and LIST.match(q["list"][0]):
        return "playlist", q["list"][0]
    raise ValueError("看不出這是哪首歌或哪個播放清單；請在 YouTube Music 按「分享 → 複製連結」再貼上")


def _oembed(kind, ref):
    page = f"https://www.youtube.com/watch?v={ref}" if kind == "video" else f"https://www.youtube.com/playlist?list={ref}"
    try:
        r = httpx.get("https://www.youtube.com/oembed", params={"url": page, "format": "json"}, timeout=10)
        if r.status_code == 200:
            d = r.json()
            return d.get("title"), d.get("author_name")
    except Exception:
        pass
    return None, None


def _score(r, now):
    if not r["plays"]:
        return 0.0
    days = (now - (r["last_played"] or now)) / 86400
    return r["plays"] * math.pow(0.5, days / 14)           # 兩週沒播，分數減半


def list_all():
    rows = db.query("select * from music")
    now = time.time()
    for r in rows:
        r["score"] = round(_score(r, now), 3)
    rows.sort(key=lambda r: (-r["pinned"], -r["score"], -(r["added"] or 0)))
    return {"music": rows}


def add(url):
    kind, ref = parse(url)
    mid = f"{kind}:{ref}"
    if db.query("select 1 from music where id=?", (mid,)):
        return {"id": mid, "existed": True}
    title, author = _oembed(kind, ref)
    db.execute("insert into music(id, kind, ref, title, author, added) values(?,?,?,?,?,?)",
               (mid, kind, ref, title or ("播放清單" if kind == "playlist" else ref), author, time.time()))
    return {"id": mid, "existed": False}


def played(mid, title=None):
    n = db.execute("update music set plays=plays+1, last_played=? where id=?", (time.time(), mid))
    if title:
        db.execute("update music set title=? where id=? and (title is null or title=ref or title='播放清單')", (title, mid))
    return {"ok": True}


def update(mid, pinned=None, title=None):
    if not db.query("select 1 from music where id=?", (mid,)):
        raise KeyError(mid)
    if pinned is not None:
        db.execute("update music set pinned=? where id=?", (1 if pinned else 0, mid))
    if title:
        db.execute("update music set title=? where id=?", (title.strip()[:200], mid))
    return {"ok": True}


def delete(mid):
    db.execute("delete from music where id=?", (mid,))
    return {"ok": True}
