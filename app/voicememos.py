"""語音備忘錄自動偵測：同步小幫手（tools/voicememo_sync.swift）把新錄音複製到 data/voicememos/inbox/，
這裡列出等你決定的錄音，你選「開始整理」就交給錄音整理，選「略過」就刪掉收件匣裡的複本。

- 原始錄音在語音備忘錄 App 裡，這裡永遠不會動到。
- 小幫手每次執行都寫 heartbeat.json；從它可以知道有沒有安裝、有沒有權限。
"""
import json
import subprocess
import threading
import time
from pathlib import Path

from . import db, recordings

DIR = db.DATA / "voicememos"
INBOX = DIR / "inbox"
HEARTBEAT = DIR / "heartbeat.json"
BIN = db.ROOT / "bin" / "voicememo-sync"
PLIST = Path.home() / "Library" / "LaunchAgents" / "local.ai-controller.voicememos.plist"
STALE = 10 * 60          # 小幫手每分鐘跑一次；超過 10 分鐘沒消息就當作沒在跑
NOTIFY_KEY = "voicememo_notified"

_lock = threading.Lock()


def _safe_name(name):
    """只接受收件匣裡實際存在的檔名，避免 ../ 之類的路徑。"""
    p = INBOX / Path(name).name
    if p.name != name or not p.is_file() or p.suffix == ".json":
        raise KeyError(name)
    return p


def pending():
    out = []
    for j in sorted(INBOX.glob("*.json")):
        audio = j.with_suffix("")                   # 「xxx.m4a.json」→「xxx.m4a」
        if not audio.is_file():
            continue
        try:
            m = json.loads(j.read_text())
        except (OSError, ValueError):
            continue
        out.append({"file": audio.name, "title": m.get("title") or audio.stem,
                    "recorded_at": m.get("recorded_at"), "duration": m.get("duration"),
                    "size": audio.stat().st_size, "found_at": m.get("found_at")})
    out.sort(key=lambda x: x.get("recorded_at") or 0)
    return out


def status():
    """小幫手的狀態：not_installed／no_permission／stale／ok／error。"""
    try:
        hb = json.loads(HEARTBEAT.read_text())
    except (OSError, ValueError):
        hb = None
    installed = PLIST.exists() and BIN.exists()
    if not installed:
        state = "not_installed"
    elif not hb:
        state = "waiting"
    elif hb.get("error") == "no_permission":
        state = "no_permission"
    elif time.time() - (hb.get("at") or 0) > STALE:
        state = "stale"
    elif not hb.get("ok"):
        state = "error"
    else:
        state = "ok"
    return {"state": state, "bin": str(BIN), "last_run": hb.get("at") if hb else None,
            "error": hb.get("error") if hb and state == "error" else None,
            "total": hb.get("total") if hb else None, "pending": pending()}


def start(files, model, language, hold=False, asr=None):
    """把選的錄音交給錄音整理（搬進錄音資料夾，收件匣裡就不見了）。"""
    made = []
    with _lock:
        for name in files:
            p = _safe_name(name)
            try:
                m = json.loads(Path(str(p) + ".json").read_text())
            except (OSError, ValueError):
                m = {}
            title = (m.get("title") or p.stem).strip()
            meta = recordings.create_from_file(p, f"{title}{p.suffix}", model, language, hold, asr,
                                               extra={"source": "voice_memos", "recorded_at": m.get("recorded_at")})
            Path(str(p) + ".json").unlink(missing_ok=True)
            made.append(meta["id"])
    return {"ok": True, "ids": made}


def skip(files):
    """不整理：刪掉收件匣裡的複本（語音備忘錄 App 裡的原始錄音不受影響，之後也不會再問）。"""
    with _lock:
        for name in files:
            p = _safe_name(name)
            p.unlink(missing_ok=True)
            Path(str(p) + ".json").unlink(missing_ok=True)
    return {"ok": True}


# ---------------- macOS 通知：有新錄音時提醒一次 ----------------
def _notify(title, text):
    subprocess.run(["osascript", "-e", "on run argv", "-e",
                    "display notification (item 2 of argv) with title (item 1 of argv) sound name \"Glass\"",
                    "-e", "end run", title, text], capture_output=True, timeout=10)


def _check_notify():
    if not recordings.config().get("voice_memo_notify", True):
        return
    items = pending()
    r = db.query("select v from kv where k=?", (NOTIFY_KEY,))
    done = set(json.loads(r[0]["v"]) if r else [])
    new = [x for x in items if x["file"] not in done]
    if new:
        names = "、".join(f"「{x['title']}」" for x in new[:3]) + (f" 等 {len(new)} 筆" if len(new) > 3 else "")
        _notify("新的語音備忘錄", f"{names}：打開首頁選擇要不要整理")
    # 只留還在收件匣裡的，清單不會一直變長
    db.execute("insert into kv(k, v) values(?, ?) on conflict(k) do update set v=excluded.v",
               (NOTIFY_KEY, json.dumps(sorted({x["file"] for x in items}))))


def _loop():
    while True:
        try:
            if INBOX.is_dir():
                _check_notify()
        except Exception as e:
            print("voicememos:", e, flush=True)
        time.sleep(15)


def start_watcher():
    INBOX.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_loop, daemon=True).start()
