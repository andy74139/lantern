"""Claude Code 的用量：從本機對話紀錄（~/.claude/projects/*.jsonl）累計 token，唯讀。

- 只算 Claude Code（終端機與桌面 App 的 Code 分頁）；claude.ai 網頁上的聊天不在這裡。
- 方案剩下多少 %（/usage 顯示的那個）不在本機紀錄裡，這裡不讀。
- 碰到用量上限時，紀錄裡會留下「何時解除」，用來顯示目前是否被限制。
"""
import datetime as dt
import json
import threading
import time
from collections import defaultdict
from pathlib import Path

from . import claude_sessions, home, tasks
from .db import DATA

ROOT = Path.home() / ".claude" / "projects"
CACHE_FILE = DATA / "cache" / "claude_usage.json"
CACHE_VERSION = 1
KEEP_DAYS = 35
_lock = threading.Lock()
_state = None


def _load():
    global _state
    if _state is None:
        try:
            d = json.loads(CACHE_FILE.read_text())
            _state = d["files"] if d.get("version") == CACHE_VERSION else {}
        except Exception:
            _state = {}
    return _state


def _usage(u):
    its = [i for i in (u.get("iterations") or []) if isinstance(i, dict)]

    def pick(k):
        return max(u.get(k) or 0, sum(i.get(k) or 0 for i in its))
    return {"out": pick("output_tokens"), "in": pick("input_tokens"),
            "cr": pick("cache_read_input_tokens"), "cw": pick("cache_creation_input_tokens")}


def _hour_key(ts):
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(home.tz())
    return t.strftime("%Y-%m-%dT%H")


def _scan(path, st):
    s = _load().get(str(path))
    if s and s["size"] == st.st_size and s["mtime"] == st.st_mtime:
        return False
    if not s or st.st_size < s["offset"]:
        s = {"offset": 0, "hours": {}, "limits": [], "last_id": None}
    with open(path, "rb") as f:
        f.seek(s["offset"])
        data = f.read()
    end = data.rfind(b"\n") + 1
    for line in data[:end].split(b"\n"):
        if b'"type":"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        m = d.get("message") or {}
        ts = d.get("timestamp")
        if not ts:
            continue
        info = (d.get("apiErrorParams") or {}).get("rate_limit_info")
        if isinstance(info, dict) and info.get("resetsAt"):
            s["limits"].append({"at": ts, "resets_at": info["resetsAt"], "type": info.get("rateLimitType"),
                                "status": info.get("status")})
        mid, u = m.get("id"), m.get("usage")
        if not mid or not u or mid == s["last_id"] or m.get("model") == "<synthetic>":
            continue                                   # 同一則回覆會拆成好幾行，只算一次
        s["last_id"] = mid
        x = _usage(u)
        if not any(x.values()):
            continue
        b = s["hours"].setdefault(_hour_key(ts), {"out": 0, "in": 0, "cr": 0, "cw": 0, "n": 0, "models": {}})
        for k, v in x.items():
            b[k] += v
        b["n"] += 1
        model = (m.get("model") or "?").replace("claude-", "")
        b["models"][model] = b["models"].get(model, 0) + x["out"]
    s["offset"] += end
    s["size"], s["mtime"] = st.st_size, st.st_mtime
    cutoff = (dt.datetime.now(home.tz()) - dt.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H")
    s["hours"] = {k: v for k, v in s["hours"].items() if k >= cutoff}
    s["limits"] = s["limits"][-20:]
    _load()[str(path)] = s
    return True


def summary():
    with _lock:
        changed = False
        files = []
        if ROOT.is_dir():
            for p in ROOT.glob("*/*.jsonl"):
                try:
                    files.append((p, p.stat()))
                except OSError:
                    pass
        for p, st in files:
            try:
                changed |= _scan(p, st)
            except OSError:
                pass
        state = _load()
        if changed:
            tmp = CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": CACHE_VERSION, "files": state}))
            tmp.replace(CACHE_FILE)

        now = dt.datetime.now(home.tz())
        today = now.date()
        h5 = (now - dt.timedelta(hours=5)).strftime("%Y-%m-%dT%H")
        days = {(today - dt.timedelta(days=i)).isoformat(): {"out": 0, "proc": 0, "n": 0} for i in range(7)}
        last5 = {"out": 0, "proc": 0, "n": 0}
        today_t = {"out": 0, "proc": 0, "n": 0}
        models = defaultdict(int)
        projects = defaultdict(int)
        limits = []
        for path, s in state.items():
            proj = Path(path).parent.name
            for hk, b in s["hours"].items():
                proc = b["in"] + b["cr"] + b["cw"]
                d = hk[:10]
                if d in days:
                    days[d]["out"] += b["out"]
                    days[d]["proc"] += proc
                    days[d]["n"] += b["n"]
                    for mname, v in b["models"].items():
                        models[mname] += v
                    projects[proj] += b["out"]
                if d == today.isoformat():
                    today_t["out"] += b["out"]
                    today_t["proc"] += proc
                    today_t["n"] += b["n"]
                if hk >= h5:
                    last5["out"] += b["out"]
                    last5["proc"] += proc
                    last5["n"] += b["n"]
            limits += s["limits"]
        limits.sort(key=lambda x: x["at"])
        last = limits[-1] if limits else None
        active = bool(last and last["resets_at"] > time.time())
        names = {}
        sess = claude_sessions._load_state()
        for path in state:                               # 用對話紀錄裡的工作資料夾名稱當專案名
            cwd = (sess.get(path) or {}).get("agg", {}).get("cwd")
            proj = Path(path).parent.name
            names.setdefault(proj, Path(cwd).name if cwd else proj.replace(f"-Users-{Path.home().name}-", "", 1))
        return {
            "plan": plan_limits(),
            "today": today_t, "last5h": last5,
            "days": [{"date": d, **v} for d, v in sorted(days.items())],
            "models": sorted(({"model": k, "out": v} for k, v in models.items()), key=lambda x: -x["out"]),
            "projects": sorted(({"project": names[k], "out": v} for k, v in projects.items()), key=lambda x: -x["out"])[:5],
            "limit": {"active": active, "type": last and last["type"], "resets_at": last and last["resets_at"],
                      "hit_at": last and last["at"]},
        }


LIMITS_FILE = DATA / "claude_limits.json"


def plan_limits():
    """5 小時與每週上限的使用百分比（由 claude_statusline.py 從 Claude Code 狀態列資料存下來）。"""
    try:
        d = json.loads(LIMITS_FILE.read_text())
    except Exception:
        return None
    now = time.time()
    out = {"at": d.get("at"), "error": _poll_error, "polling": _poll_lock.locked(),
           "next_poll": (_last_poll + POLL_EVERY) if _last_poll else None}
    for k in ("five_hour", "seven_day"):
        w = d.get(k)
        if not isinstance(w, dict):
            continue
        reset = w.get("resets_at")
        expired = bool(reset and now >= reset)          # 已經過了重置時間：這個時段重新計算，視為 0%
        out[k] = {"used": 0.0 if expired else w.get("used_percentage"), "resets_at": None if expired else reset,
                  "expired": expired, "at": w.get("at")}
    return out


# ---------------- 主動查詢：用一個極小的 claude 請求取得最新的上限百分比 ----------------
import shutil
import subprocess

POLL_EVERY = 5 * 60          # 首頁開著時多久查一次（秒）
VIEW_WINDOW = 10 * 60        # 首頁多久沒來要資料，就當作沒人在看、停止查詢
_poll_lock = threading.Lock()
_last_view = 0.0
_last_poll = 0.0
_poll_error = None


def mark_viewed():
    global _last_view
    _last_view = time.time()


def poll_limits():
    """跑一次 claude -p（Haiku、一句話、不開任何工具、不存對話），從回傳的 rate_limit_event 取得用量上限。"""
    global _last_poll, _poll_error
    if not _poll_lock.acquire(blocking=False):
        return {"ok": False, "error": "正在查詢中"}
    try:
        _last_poll = time.time()
        exe = shutil.which("claude", path=tasks.run_env()["PATH"])
        if not exe:
            raise RuntimeError("找不到 claude 指令")
        r = subprocess.run([exe, "-p", "--output-format", "stream-json", "--verbose", "--tools", "",
                            "--no-session-persistence", "--strict-mcp-config", "--model", "haiku",
                            "--system-prompt", "Reply with one word."],
                           input="OK", capture_output=True, text=True, timeout=90, cwd=str(DATA))
        rec = None
        for line in r.stdout.splitlines():
            if '"rate_limit_event"' not in line:
                continue
            try:
                info = json.loads(line).get("rate_limit_info") or {}
            except Exception:
                continue
            wins = info.get("unifiedWindows") or {}
            if wins:
                rec = wins
        if not rec:
            msg = (r.stderr or r.stdout or "").strip()[-200:]
            raise RuntimeError("Claude 沒有回傳用量資料" + (f"：{msg}" if msg else ""))
        try:
            d = json.loads(LIMITS_FILE.read_text())
        except Exception:
            d = {}
        now = time.time()
        for k in ("five_hour", "seven_day"):
            w = rec.get(k)
            if isinstance(w, dict) and w.get("utilization") is not None:
                d[k] = {"used_percentage": float(w["utilization"]) * 100, "resets_at": w.get("resetsAt"), "at": now}
        d["at"] = now
        d["source"] = "poll"
        tmp = LIMITS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(LIMITS_FILE)
        _poll_error = None
        return {"ok": True}
    except Exception as e:
        _poll_error = str(e) or type(e).__name__
        return {"ok": False, "error": _poll_error}
    finally:
        _poll_lock.release()


def _poll_loop():
    while True:
        try:
            if time.time() - _last_view < VIEW_WINDOW and time.time() - _last_poll >= POLL_EVERY:
                poll_limits()
        except Exception as e:
            print("claude limits poll:", e, flush=True)
        time.sleep(20)


def start_poller():
    threading.Thread(target=_poll_loop, daemon=True).start()
