"""市場分頁：股價（Yahoo Finance 的公開圖表資料）與 AI／財經新聞（Google 新聞 RSS）。

只讀取公開資料，不需要帳號或金鑰；送出的只有股票代號與新聞關鍵字。
追蹤的代號與新聞關鍵字在 config/market.json（個人設定，不進 git；範本是 market.example.json）。
"""
import hashlib
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import httpx

from . import db

CONFIG = db.ROOT / "config" / "market.json"
NEWS_CACHE = db.ROOT / "data" / "cache" / "market_news.json"   # 重開服務後不用馬上再抓（Google 新聞抓太密會回 429）
EXAMPLE = db.ROOT / "config" / "market.example.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) lantern-home/1.0"}
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
NEWS = "https://news.google.com/rss/search"
BING = "https://www.bing.com/news/search"        # 前幾則要有圖片：Google 新聞 RSS 沒有圖，Bing 新聞 RSS 有縮圖
BING_IMG = "https://www.bing.com/th"
IMG_DIR = db.ROOT / "data" / "cache" / "news_img"
IMG_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{4,120}$")
FEATURED = 3
SYMBOL_RE = re.compile(r"^[A-Za-z0-9.^=\-]{1,20}$")
MAX_BYTES = 3_000_000

# 區間 → 怎麼抓。日線以上一律抓 10 年日線（快取共用），前端再切出區間並算均線
RANGES = {
    "1d": ("1d", "5m"),
    "5d": ("5d", "15m"),
    "1mo": None, "3mo": None, "6mo": None, "ytd": None, "1y": None, "5y": None,
}
TTL_QUOTE = 60
TTL_INTRADAY = 60
TTL_DAILY = 600
TTL_NEWS = 900
RETRY_AFTER_FAIL = 300

_cache = {}
_lock = threading.Lock()


def config():
    for p in (CONFIG, EXAMPLE):
        try:
            cfg = json.loads(p.read_text())
            break
        except FileNotFoundError:
            continue
    else:
        cfg = {}
    ok = lambda s: isinstance(s, dict) and SYMBOL_RE.match(str(s.get("symbol", "")))
    syms = []
    for s in cfg.get("symbols", []):
        if ok(s):
            syms.append({**s, "ref": s["ref"] if ok(s.get("ref")) else None})
    news = [n for n in cfg.get("news", []) if isinstance(n, dict) and n.get("key") and n.get("query")]
    return {"symbols": syms[:12], "news": news[:4]}


def _cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    try:
        val = fn()
    except Exception:
        if hit:                      # 連不上時先用舊資料
            return hit[1]
        raise
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def _get(url, params):
    with httpx.Client(headers=UA, timeout=15, follow_redirects=True) as c:
        r = c.get(url, params=params)
    r.raise_for_status()
    if len(r.content) > MAX_BYTES:
        raise RuntimeError("回應太大")
    return r


def _chart(symbol, rng, interval):
    j = _get(CHART + symbol, {"range": rng, "interval": interval, "includePrePost": "false"}).json()
    err = (j.get("chart") or {}).get("error")
    if err:
        raise RuntimeError(err.get("description") or "查不到這個代號")
    res = j["chart"]["result"][0]
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    pts = [[t, round(c, 4)] for t, c in zip(res.get("timestamp") or [], q.get("close") or []) if c is not None]
    return res["meta"], pts


def _symbol(sym):
    if not SYMBOL_RE.match(sym or ""):
        raise ValueError("代號格式不對")
    return sym.upper()


def _quote(s):
    sym = s["symbol"].upper()
    try:
        meta, pts = _cached(("intra", sym, "1d"), TTL_QUOTE, lambda: _chart(sym, "1d", "5m"))
    except Exception as e:
        return {"symbol": sym, "name": s.get("name") or sym, "error": str(e)[:200]}
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    per = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    now = time.time()
    return {
        "symbol": sym, "name": s.get("name") or meta.get("longName") or meta.get("shortName") or sym,
        "full_name": meta.get("longName") or meta.get("shortName"),
        "currency": meta.get("currency"), "price": price, "prev_close": prev,
        "change": round(price - prev, 4) if price is not None and prev else None,
        "pct": round((price / prev - 1) * 100, 2) if price is not None and prev else None,
        "time": meta.get("regularMarketTime"), "tz": meta.get("exchangeTimezoneName"),
        "open": bool(per.get("start") and per["start"] <= now < per["end"]),
        "spark": [p[1] for p in pts][::2],
    }


def quotes():
    """每一檔可以設一個參考指標（ref，例如槓桿 ETF 對應的原型 ETF），一起回傳。"""
    syms = config()["symbols"]
    refs = [s["ref"] for s in syms if s.get("ref")]
    with ThreadPoolExecutor(max_workers=8) as ex:
        main = list(ex.map(_quote, syms))
        got = dict(zip([r["symbol"].upper() for r in refs], ex.map(_quote, refs)))
    for q, s in zip(main, syms):
        if s.get("ref"):
            r = dict(got[s["ref"]["symbol"].upper()])
            r.pop("spark", None)
            q["ref"] = r
    return {"quotes": main, "at": time.time()}


def chart(symbol, rng):
    sym = _symbol(symbol)
    if rng not in RANGES:
        raise ValueError("區間不對")
    if RANGES[rng]:
        r, iv = RANGES[rng]
        meta, pts = _cached(("intra", sym, rng), TTL_INTRADAY, lambda: _chart(sym, r, iv))
        return {"symbol": sym, "range": rng, "intraday": True, "points": pts,
                "prev_close": meta.get("chartPreviousClose") if rng == "1d" else None,
                "currency": meta.get("currency"), "tz": meta.get("exchangeTimezoneName")}
    meta, pts = _cached(("daily", sym), TTL_DAILY, lambda: _chart(sym, "10y", "1d"))
    return {"symbol": sym, "range": rng, "intraday": False, "points": pts,
            "currency": meta.get("currency"), "tz": meta.get("exchangeTimezoneName")}


# ---------------- 新聞 ----------------
def _feed(n):
    r = _get(NEWS, {"q": f"{n['query']} when:3d", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"})
    root = ET.fromstring(r.content)        # 系統的 expat 2.6 會擋實體展開攻擊
    items, seen = [], set()
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = (it.findtext("source") or "").strip()
        if not title or not link.startswith("https://"):
            continue
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        if title in seen:
            continue
        seen.add(title)
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except Exception:
            ts = None
        items.append({"title": title[:300], "link": link[:2000], "source": src[:80], "time": ts})
    items.sort(key=lambda x: x["time"] or 0, reverse=True)
    return items[:20]


_news_lock = threading.Lock()


def _news_error(e):
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429:
        return "Google 新聞暫時限制抓取次數，幾分鐘後會自動再試"
    if isinstance(e, httpx.HTTPError):
        return "連不上 Google 新聞"
    return "新聞格式讀不懂"


def _norm(t):
    return re.sub(r"[\W_]+", "", t or "").lower()[:14]


def _bing(term):
    r = _get(BING, {"q": term, "format": "rss", "mkt": "zh-TW"})
    out = []
    for it in ET.fromstring(r.content).iter("item"):
        img = next((ch.text for ch in it if ch.tag.endswith("}Image") and ch.text), None)
        src = next((ch.text for ch in it if ch.tag.endswith("}Source") and ch.text), "")
        img_id = (parse_qs(urlparse(img).query).get("id") or [""])[0] if img else ""
        link = (parse_qs(urlparse(it.findtext("link") or "").query).get("url") or [""])[0]   # 直接連到原文，不經過 Bing
        title = (it.findtext("title") or "").strip()
        if not (title and IMG_ID_RE.match(img_id) and link.startswith(("https://", "http://"))):
            continue
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except Exception:
            ts = None
        out.append({"title": title[:300], "link": link[:2000], "source": src[:80], "time": ts, "img": img_id,
                    "summary": (it.findtext("description") or "").strip()[:160]})
    return out


def _featured(n):
    """搜尋字用 OR 分開的話（Bing 的 RSS 不支援 OR），每個字各查一次，取最新、有圖的幾則。"""
    terms = [t.strip() for t in re.split(r"\s+OR\s+", n.get("image_query") or n["query"]) if t.strip()][:3]
    items, seen, ok = [], set(), False
    for i, t in enumerate(terms):
        if i:
            time.sleep(1)
        try:
            got = _bing(t)
            ok = True
        except Exception:
            continue
        for it in got:
            k = _norm(it["title"])
            if k not in seen and (it["time"] or 0) > time.time() - 3 * 86400:
                seen.add(k)
                items.append(it)
    if not ok:
        raise RuntimeError("連不上 Bing 新聞")
    items.sort(key=lambda x: x["time"] or 0, reverse=True)
    return items[:FEATURED]


def news_image(img_id):
    """新聞縮圖由這台電腦代抓並快取，瀏覽器不用直接連到 Bing。"""
    if not IMG_ID_RE.match(img_id or ""):
        raise ValueError("圖片代號不對")
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    f = IMG_DIR / (hashlib.sha1(img_id.encode()).hexdigest() + ".jpg")
    if f.exists():
        return f
    r = _get(BING_IMG, {"id": img_id, "pid": "News", "w": "480", "h": "270", "c": "14"})
    if not r.headers.get("content-type", "").startswith("image/") or len(r.content) > 1_000_000:
        raise RuntimeError("不是圖片")
    f.write_bytes(r.content)
    old = time.time() - 3 * 86400                     # 3 天前的縮圖清掉
    for x in IMG_DIR.glob("*.jpg"):
        if x.stat().st_mtime < old:
            x.unlink(missing_ok=True)
    return f


def news():
    """一次只抓一個關鍵字；成功的結果存到磁碟 15 分鐘，失敗後 5 分鐘內不再試（沿用舊結果）。"""
    with _news_lock:
        try:
            store = json.loads(NEWS_CACHE.read_text())
        except Exception:
            store = {}
        out, changed, now = [], False, time.time()
        for n in config()["news"]:
            ent = store.get(n["query"]) or {}
            title = n.get("title") or n["key"]
            fresh = now - ent.get("at", 0) < TTL_NEWS
            backoff = now - ent.get("failed_at", 0) < RETRY_AFTER_FAIL
            err = ent.get("error") if backoff else None
            if not fresh and not backoff:
                if changed:
                    time.sleep(1.5)
                try:
                    new = {"at": now, "items": _feed(n), "featured": ent.get("featured", [])}
                    err = None
                    try:
                        time.sleep(1)
                        new["featured"] = _featured(n)
                    except Exception:
                        pass                                  # 沒有圖片也沒關係，沿用上次的
                    ent = new
                except Exception as e:
                    err = _news_error(e)
                    ent = {**ent, "failed_at": now, "error": err}
                store[n["query"]] = ent
                changed = True
            feat = ent.get("featured", [])
            keys = {_norm(f["title"]) for f in feat}
            items = [x for x in ent.get("items", []) if _norm(x["title"]) not in keys]
            out.append({"key": n["key"], "title": title, "featured": feat, "items": items, "error": err, "at": ent.get("at")})
        if changed:
            NEWS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            tmp = NEWS_CACHE.with_suffix(".tmp")
            tmp.write_text(json.dumps(store, ensure_ascii=False))
            tmp.replace(NEWS_CACHE)
    return {"feeds": out}
