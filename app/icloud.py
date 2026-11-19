"""把首頁資料匯出到 iCloud Drive，給 iPhone／iPad 的 Lantern 讀（唯讀快照）。

- 只有這台 Mac 會寫 `state/` 裡的檔案，所以不會產生 iCloud 的衝突副本。
- 內容沒變就不寫，避免無謂的同步。
- 只匯出文字：行程、天氣、待辦、ASA、錄音的摘要與逐字稿。
  音檔、處理記錄、知識庫索引、Google 授權都不會放進 iCloud。
"""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from . import asa, db, google, home, recordings, todos

CONFIG = db.ROOT / "config" / "icloud.json"
TMP = db.DATA / "tmp"
SCHEMA = 1
HEARTBEAT = 15 * 60                       # 內容都沒變時，多久更新一次「Mac 還在」的時間
DEFAULTS = {
    "enabled": False,
    "folder": "~/Library/Mobile Documents/com~apple~CloudDocs/Lantern",
    "interval": 60,
    "budget_mb": 200,
    "export": {"home": True, "todos": True, "asa": True, "recordings": True},
}
# 這些欄位每次更新都會變，但不代表內容有變；比對時略過，才不會每幾分鐘就重寫一次
VOLATILE = {"fetched_at", "checked_at", "at"}

_lock = threading.Lock()
_hashes = {}                              # 相對路徑 -> 上次寫入內容的雜湊
_rec_sig = {}                             # 錄音編號 -> 檔案修改時間，沒變就不重讀
_status = {"enabled": False, "folder": None, "exported_at": None, "checked_at": None,
           "error": None, "usage_bytes": None, "files": 0}
_meta_written = 0
_last_error = None


def config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        user = json.loads(CONFIG.read_text())
    except FileNotFoundError:
        return cfg
    except Exception as e:
        raise ValueError(f"config/icloud.json 格式不對：{e}")
    exp = {**cfg["export"], **(user.get("export") or {})}
    cfg.update(user)
    cfg["export"] = exp
    return cfg


def status():
    with _lock:
        return dict(_status)


def _strip(v):
    if isinstance(v, dict):
        return {k: _strip(x) for k, x in v.items() if k not in VOLATILE}
    if isinstance(v, list):
        return [_strip(x) for x in v]
    return v


def _digest(obj):
    return hashlib.sha1(json.dumps(_strip(obj), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _put(root, rel, obj, force=False):
    """內容有變才寫。先寫在 iCloud 外面再搬進去，手機不會讀到寫到一半的檔案。"""
    target = root / rel
    h = _digest(obj)
    if not force:
        if rel not in _hashes and target.exists():
            try:                          # 重啟後第一次：和磁碟上的比，不要全部重寫
                _hashes[rel] = _digest(json.loads(target.read_text()))
            except Exception:
                pass
        if _hashes.get(rel) == h and target.exists():
            return False
    target.parent.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(exist_ok=True)
    tmp = TMP / f"icloud-{os.getpid()}-{threading.get_ident()}.json"
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    try:
        os.replace(tmp, target)
    except OSError:                       # 不在同一個磁碟區時不能直接搬
        target.write_text(tmp.read_text())
        tmp.unlink(missing_ok=True)
    _hashes[rel] = h
    return True


def _home():
    cfg = home.config()
    cal = home.calendar() or {}
    g = google.status()
    return {
        "place": cfg["location"]["name"], "timezone": cfg["timezone"],
        "weather": home.weather(),
        "calendar": {"fetched_at": cal.get("fetched_at"), "events": cal.get("events", []),
                     "calendars": cal.get("calendars", [])},
        "google": {"connected": g["connected"], "error": g["error"]},
    }


TODO_FIELDS = ("id", "title", "notes", "due_date", "due_time", "status", "completed_at", "pinned",
               "repeat", "list_id", "sort", "created_at", "updated_at")


def _todos():
    d = todos.list_all()
    s = d["sync"]
    return {
        "todos": [{k: t.get(k) for k in TODO_FIELDS} for t in d["todos"]],
        "lists": d["lists"],
        "sync": {"state": s["state"], "at": s["at"], "error": s["error"], "pending": s["pending"],
                 "connected": s["connected"]},
    }


REC_FIELDS = ("id", "name", "title", "created", "duration", "status", "stage", "error", "language",
              "chars", "summary_model", "summary_stale", "truncated")


def _recordings(root):
    """索引一個檔，每筆錄音的摘要＋逐字稿各一個檔。回傳是否有寫入。"""
    changed = False
    index, keep = [], set()
    for d, m in reversed(recordings._all()):
        rid = d.name
        item = {k: m.get(k) for k in REC_FIELDS}
        item["id"] = rid
        item["has_summary"] = (d / "summary.md").exists()
        item["has_transcript"] = (d / "transcript.json").exists()
        index.append(item)
        if not (item["has_summary"] or item["has_transcript"]):
            continue
        keep.add(f"{rid}.json")
        rel = f"state/recordings/{rid}.json"
        sig = tuple((d / n).stat().st_mtime_ns if (d / n).exists() else 0
                    for n in ("meta.json", "summary.md", "transcript.json"))
        if _rec_sig.get(rid) == sig and (root / rel).exists():
            continue
        try:
            segments = json.loads((d / "transcript.json").read_text()) if item["has_transcript"] else None
        except Exception:
            segments = None
        body = dict(item)
        body["summary"] = (d / "summary.md").read_text() if item["has_summary"] else None
        body["segments"] = [{"start": s.get("start"), "end": s.get("end"), "text": s.get("text", "")}
                            for s in segments] if segments else None
        changed |= _put(root, rel, body)
        _rec_sig[rid] = sig
    changed |= _put(root, "state/recordings.json", {"recordings": index})
    folder = root / "state" / "recordings"        # 錄音刪掉後，iCloud 上那份也刪
    if folder.is_dir():
        for f in folder.iterdir():
            if f.suffix == ".json" and f.name not in keep:
                f.unlink(missing_ok=True)
                _hashes.pop(f"state/recordings/{f.name}", None)
                changed = True
    return changed


def _usage(root):
    total = 0
    for base, _, names in os.walk(root):
        for n in names:
            try:
                total += os.lstat(os.path.join(base, n)).st_size
            except OSError:
                pass
    return total


def export_once(force=False):
    """匯出一次。回傳目前的狀態（也給 /api/icloud 用）。"""
    global _meta_written
    cfg = config()
    root = Path(cfg["folder"]).expanduser()
    with _lock:
        _status.update(enabled=bool(cfg["enabled"]), folder=str(root))
        if not cfg["enabled"]:
            _status["error"] = None
            return dict(_status)
        if not root.parent.is_dir():
            raise FileNotFoundError(f"找不到 {root.parent}（這台 Mac 沒有開 iCloud Drive？）")
        (root / "state").mkdir(parents=True, exist_ok=True)
        exp, changed, files = cfg["export"], False, {}
        parts = (("home", "state/home.json", _home), ("todos", "state/todos.json", _todos),
                 ("asa", "state/asa.json", asa.overview))
        for key, rel, fn in parts:
            if exp.get(key):
                changed |= _put(root, rel, fn(), force)
                files[key] = True
            elif (root / rel).exists():           # 關掉某一項時，把已經放上去的拿掉
                (root / rel).unlink()
                _hashes.pop(rel, None)
                changed = True
        if exp.get("recordings"):
            changed |= _recordings(root)
            files["recordings"] = True
        else:
            import shutil
            for p in (root / "state" / "recordings.json", root / "state" / "recordings"):
                if p.is_dir():
                    shutil.rmtree(p)
                    changed = True
                elif p.exists():
                    p.unlink()
                    changed = True
            _rec_sig.clear()
        now = time.time()
        if changed or not _status["exported_at"]:
            try:
                prev = json.loads((root / "state" / "meta.json").read_text())
            except Exception:
                prev = {}
            _status["exported_at"] = now if changed or not prev.get("exported_at") else prev["exported_at"]
        if changed or force or now - _meta_written >= HEARTBEAT:
            usage = _usage(root)
            _status.update(checked_at=now, usage_bytes=usage, files=len(files))
            _put(root, "state/meta.json", {
                "schema": SCHEMA, "source": "mac", "exported_at": _status["exported_at"], "checked_at": now,
                "files": files, "usage_bytes": usage, "budget_bytes": int(cfg["budget_mb"]) * 1000 * 1000,
            }, force=True)
            _meta_written = now
        _status["error"] = None
        return dict(_status)


def _explain(e):
    if isinstance(e, PermissionError):
        return ("macOS 不允許這個程式存取 iCloud Drive。請到「系統設定 → 隱私權與安全性 → 檔案與檔案夾」，"
                "打開 python 的「iCloud Drive」，再執行 ./autostart.sh restart。")
    return str(e) or e.__class__.__name__


def _loop():
    global _last_error
    time.sleep(5)                                 # 等天氣、行事曆先載入
    while True:
        wait = 60
        try:
            wait = max(15, int(config().get("interval") or 60))
            export_once()
            _last_error = None
        except Exception as e:
            msg = _explain(e)
            with _lock:
                _status["error"] = msg
            if msg != _last_error:                # 同一個錯誤只記一次
                print("icloud export:", msg, flush=True)
                _last_error = msg
        time.sleep(wait)


def start():
    threading.Thread(target=_loop, daemon=True).start()
