"""錄音整理：收檔、排隊、查詢。實際處理由 jobs/transcribe.py 在獨立的程序裡做。

狀態都存在每筆錄音自己的 meta.json，所以控制台重啟不會影響正在跑的工作。
"""
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import db, tasks

DIR = db.DATA / "recordings"
DIR.mkdir(exist_ok=True)
JOB = db.ROOT / "jobs" / "transcribe.py"
ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
LANGS = {"zh", "en", "ja", "ko", "auto"}
_lock = threading.Lock()
_proc = None                # 這次啟動後由控制台開始的工作：(id, Popen)


def _tx():
    """jobs/transcribe.py（轉錄程式）裡的共用函式。"""
    sys.path.insert(0, str(JOB.parent))
    try:
        import transcribe
        return transcribe
    finally:
        sys.path.pop(0)


def config():
    return _tx().load_config()


def _folder(rid):
    if not ID_RE.match(rid):
        raise KeyError(rid)
    d = DIR / rid
    if not (d / "meta.json").is_file():
        raise KeyError(rid)
    return d


def _read(d):
    for _ in range(3):                       # 剛好碰到另一邊在寫檔就再讀一次
        try:
            return json.loads((d / "meta.json").read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            time.sleep(0.05)
    return None


def _write(d, meta):
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    tmp.replace(d / "meta.json")


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _all():
    out = []
    for d in DIR.iterdir():
        if d.is_dir() and ID_RE.match(d.name):
            m = _read(d)
            if m:
                out.append((d, m))
    out.sort(key=lambda x: x[1].get("created", 0))
    return out


def readiness():
    cfg = config()
    problems = []
    env = tasks.run_env()
    for tool in (cfg["whisper_cli"], "ffmpeg", "ffprobe"):
        if not shutil.which(tool, path=env["PATH"]):
            problems.append(f"找不到 {tool}")
    models = cfg.get("asr_models") or {}
    usable = {k: v for k, v in models.items() if Path(v["model"]).exists()}
    if models and not usable:
        problems.append("找不到任何語音模型檔")
    if not models and not Path(cfg["whisper_model"]).exists():
        problems.append(f"找不到語音模型 {Path(cfg['whisper_model']).name}")
    return {"ready": not problems, "problems": problems, "summary_model": cfg["summary_model"],
            "claude": bool(shutil.which("claude", path=env["PATH"])),
            "asr": cfg.get("asr"), "asr_models": [{"id": k, "label": v.get("label", k), "hint": v.get("hint", "")} for k, v in usable.items()] if models else [],
            "language": cfg["language"]}


def busy_tasks():
    """正在執行的常用工作（例如掃描）；設定檔有問題時當作沒有。"""
    try:
        return [t["name"] for t in tasks.load_config() if tasks.running_pid(t)]
    except Exception:
        return []


def list_all():
    items = []
    busy = busy_tasks()
    for d, m in reversed(_all()):
        m["waiting_for"] = busy if m.get("status") == "queued" and m.get("hold") else []
        m["has_summary"] = (d / "summary.md").exists()
        m["has_transcript"] = (d / "transcript.json").exists()
        items.append(m)
    return {"recordings": items, **readiness()}


async def create(name, stream, model, language, hold=False, asr=None):
    """把瀏覽器上傳的檔案存下來並排進佇列。"""
    name = Path(name or "錄音").name[:200] or "錄音"
    ext = re.sub(r"[^a-z0-9]", "", Path(name).suffix.lower())[:6] or "bin"
    rid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    d = DIR / rid
    d.mkdir()
    size = 0
    try:
        with open(d / f"source.{ext}", "wb") as f:
            async for chunk in stream:
                f.write(chunk)
                size += len(chunk)
        if not size:
            raise ValueError("檔案是空的")
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    meta = {"id": rid, "name": name, "title": None, "created": time.time(), "size": size,
            "status": "queued", "stage": None, "progress": None, "detail": None, "error": None,
            "source_file": f"source.{ext}", "summary_model": model or None,
            "language": language if language in LANGS else None, "hold": bool(hold), "asr": asr or None}
    _write(d, meta)
    return meta


def create_from_file(path, name, model, language, hold=False, asr=None, extra=None):
    """把本機的音檔（例如語音備忘錄的複本）搬進錄音資料夾並排進佇列。"""
    path = Path(path)
    name = Path(name or path.name).name[:200] or "錄音"
    ext = re.sub(r"[^a-z0-9]", "", path.suffix.lower())[:6] or "bin"
    size = path.stat().st_size
    if not size:
        raise ValueError("檔案是空的")
    rid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    d = DIR / rid
    d.mkdir()
    try:
        shutil.move(str(path), d / f"source.{ext}")
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    meta = {"id": rid, "name": name, "title": None, "created": time.time(), "size": size,
            "status": "queued", "stage": None, "progress": None, "detail": None, "error": None,
            "source_file": f"source.{ext}", "summary_model": model or None,
            "language": language if language in LANGS else None, "hold": bool(hold), "asr": asr or None,
            **(extra or {})}
    _write(d, meta)
    return meta


def detail(rid):
    d = _folder(rid)
    m = _read(d)
    m["summary"] = (d / "summary.md").read_text() if (d / "summary.md").exists() else None
    try:
        m["segments"] = json.loads((d / "transcript.json").read_text())
    except Exception:
        m["segments"] = None
    m["has_audio"] = (d / "audio.m4a").exists()
    return m


def file_path(rid, kind):
    d = _folder(rid)
    name = {"audio": "audio.m4a", "summary": "summary.md", "transcript": "transcript.txt"}.get(kind)
    if not name or not (d / name).exists():
        raise KeyError(kind)
    m = _read(d)
    base = re.sub(r'[\\/:*?"<>|\n\r]', "_", m.get("title") or Path(m.get("name", rid)).stem)[:80]
    label = {"audio": f"{base}.m4a", "summary": f"{base}－摘要.md", "transcript": f"{base}－逐字稿.txt"}[kind]
    return d / name, label


def _is_running(m):
    return m.get("status") == "running" and m.get("pid") and _alive(m["pid"])


def redo(rid, start_from, model, hold=False, rng=None, asr=None):
    d = _folder(rid)
    with _lock:
        m = _read(d)
        if _is_running(m) or m.get("status") == "queued":
            raise RuntimeError("這筆錄音正在處理或排隊中")
        if start_from == "range":
            if not rng or not (d / "transcript.json").exists() or not (d / "audio.m4a").exists():
                raise RuntimeError("需要逐字稿和音檔才能重新辨識一段")
            start, end = float(rng.get("start", 0)), float(rng.get("end", 0))
            if not 0 <= start < end:
                raise RuntimeError("時間範圍不對：結束時間要比開始時間晚")
            if (m.get("duration") or 0) and start >= m["duration"]:
                raise RuntimeError("開始時間超過錄音長度")
            lang = rng.get("lang") if rng.get("lang") in LANGS else "auto"
            m["range"] = {"start": start, "end": min(end, m.get("duration") or end), "lang": lang}
        if start_from == "summarize" and not (d / "transcript.json").exists():
            start_from = "transcribe"
        if start_from == "transcribe" and not (d / "audio.m4a").exists() and not m.get("source_file"):
            raise RuntimeError("音檔已經不在了，無法重新轉錄")
        m.update(status="queued", redo_from=start_from, error=None, stage=None, progress=None,
                 detail=None, queued_at=time.time(), hold=bool(hold))
        if model:
            m["summary_model"] = model
        if asr:
            m["asr"] = asr
        _write(d, m)
    return {"ok": True}


def start_now(rid):
    """不等其他工作了，輪到就開始。"""
    d = _folder(rid)
    with _lock:
        m = _read(d)
        if m.get("status") == "queued":
            m["hold"] = False
            _write(d, m)
    return {"ok": True}


def _editable(d):
    m = _read(d)
    if _is_running(m) or m.get("status") == "queued":
        raise RuntimeError("這筆錄音正在處理或排隊中，等它完成再修改")
    return m


def save_transcript(rid, segments):
    """手動修改逐字稿：保留每句的時間，只改文字；清空的句子會刪掉。"""
    d = _folder(rid)
    transcribe = _tx()
    with _lock:
        m = _editable(d)
        try:
            before = json.loads((d / "transcript.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            before = []
        orig = d / "transcript.orig.json"
        if before and not orig.exists():                 # 第一次手動修改：留一份修改前的原稿（之後要評估或訓練時用得到）
            orig.write_text(json.dumps(before, ensure_ascii=False))
        clean = []
        for x in segments or []:
            text = " ".join(str(x.get("text", "")).split())
            if text:
                clean.append({"start": float(x["start"]), "end": float(x["end"]), "text": text[:2000]})
        transcribe.write_transcript(d, clean)
        m.update(chars=sum(len(x["text"]) for x in clean), summary_stale=True, transcript_edited=time.time())
        _write(d, m)
        try:
            learned = transcribe.learn_corrections(before, clean, source=rid)
        except Exception as e:                           # 學不到不影響存檔
            print(f"學習修正失敗：{e}", flush=True)
            learned = []
    return {"ok": True, "segments": len(clean),
            "learned": [{k: c.get(k) for k in ("from", "to", "count", "status")} for c in learned]}


def vocab():
    """詞彙表、修正字典，以及已經人工校正過的錄音有多少（將來要不要訓練模型的參考）。"""
    tx = _tx()
    cfg, v = tx.load_config(), tx.load_vocab()
    n = secs = 0
    for d, m in _all():
        if m.get("transcript_edited"):
            n += 1
            secs += m.get("duration") or 0
    return {**v, "glossary": cfg.get("glossary") or [], "prompt": tx.prompt_terms(cfg, v),
            "prompt_max": tx.PROMPT_TERMS_MAX, "auto_on": tx.AUTO_ON_COUNT,
            "verified": {"count": n, "seconds": round(secs)}}


def save_vocab(terms=None, corrections=None):
    tx = _tx()
    with _lock:
        v = tx.load_vocab()
        if terms is not None:
            seen, out = set(), []
            for t in terms:
                t = " ".join(str(t).split())[:40]
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
            v["terms"] = out[:300]
        if corrections is not None:
            old = {c["from"]: c for c in v["corrections"]}
            out, seen, now = [], set(), time.time()
            for c in corrections:
                frm, to = str(c.get("from", "")).strip()[:40], str(c.get("to", "")).strip()[:40]
                if not frm or not to or frm == to or frm in seen:
                    continue
                seen.add(frm)
                status = c.get("status") if c.get("status") in ("on", "pending", "off") else "on"
                prev = old.get(frm) or {"from": frm, "count": 0, "source": "manual", "added": now}
                out.append({**prev, "to": to, "status": status,
                            "updated": now if (prev.get("to"), prev.get("status")) != (to, status) else prev.get("updated", now)})
            v["corrections"] = out[:1000]
        tx.save_vocab(v)
    return vocab()


def save_summary(rid, text, title=None):
    """手動修改摘要（Markdown）與標題。"""
    d = _folder(rid)
    with _lock:
        m = _editable(d)
        (d / "summary.md").write_text((text or "").rstrip() + "\n")
        if title is not None and title.strip():
            m["title"] = title.strip()[:60]
        m.update(summary_edited=time.time(), summary_stale=False, truncated=False)
        _write(d, m)
    return {"ok": True}


def cancel(rid):
    d = _folder(rid)
    with _lock:
        m = _read(d)
        if _is_running(m):
            try:
                os.killpg(int(m["pid"]), signal.SIGTERM)     # 連同 whisper／ffmpeg 一起停
            except Exception:
                pass
        elif m.get("status") != "queued":
            return {"ok": True}
        m.update(status="cancelled", error=None, pid=None, stage=None, progress=None, detail=None,
                 finished=time.time())
        _write(d, m)
    return {"ok": True}


def delete(rid):
    d = _folder(rid)
    cancel(rid)
    shutil.rmtree(d)
    return {"ok": True}


# ---------------- 排隊：一次只處理一筆 ----------------
def _tick():
    global _proc
    with _lock:
        items = _all()
        if _proc and _proc[1].poll() is not None:
            _proc = None
        busy = _proc is not None
        for d, m in items:
            if m.get("status") != "running":
                continue
            if m.get("pid") and _alive(m["pid"]):
                busy = True
            elif not (_proc and _proc[0] == m["id"]):
                log = tasks.tail(d / "job.log", 2000).strip().splitlines()
                m.update(status="error", pid=None, finished=time.time(),
                         error="處理中斷了。" + (log[-1] if log else ""))
                _write(d, m)
        if busy:
            return
        queued = [(d, m) for d, m in items if m.get("status") == "queued"]
        if any(m.get("hold") for _, m in queued) and busy_tasks():
            queued = [(d, m) for d, m in queued if not m.get("hold")]     # 選了「等其他工作結束」的先不動
        if not queued:
            return
        d, m = min(queued, key=lambda x: x[1].get("queued_at") or x[1].get("created", 0))
        m["status"] = "running"
        _write(d, m)
        logf = open(d / "job.log", "ab")
        proc = subprocess.Popen([sys.executable, str(JOB), str(d)], cwd=db.ROOT, env=tasks.run_env(),
                                stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                start_new_session=True)
        logf.close()
        _proc = (m["id"], proc)


def _loop():
    while True:
        try:
            _tick()
        except Exception as e:                    # 排隊不能因為單一筆資料壞掉就停
            print("recordings scheduler:", e, flush=True)
        time.sleep(2)


def start_scheduler():
    threading.Thread(target=_loop, daemon=True).start()
