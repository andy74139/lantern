"""讀取 Claude Code 的對話紀錄（~/.claude/projects，唯讀）。

紀錄檔是只會往後追加的 JSONL，所以每個檔案記住讀到哪裡，下次只讀新增的部分。
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from .db import DATA

ROOT = Path.home() / ".claude" / "projects"
LIVE = Path.home() / ".claude" / "sessions"
CACHE_FILE = DATA / "claude_cache.json"
CACHE_VERSION = 2
MARKERS = (b'"type":"user"', b'"type":"assistant"', b'"type":"custom-title"',
           b'"type":"agent-name"', b'"type":"last-prompt"')
SID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
MAX_SESSIONS = 150
TAIL_BYTES = 4 * 1024 * 1024

_lock = threading.Lock()
_state = None


def _load_state():
    global _state
    if _state is None:
        try:
            raw = json.loads(CACHE_FILE.read_text())
            _state = raw["files"] if raw.get("version") == CACHE_VERSION else {}
        except Exception:
            _state = {}
    return _state


def _clip(text, n):
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n] + "…"


def human_text(d):
    """使用者真的打的字；工具結果、系統插入的訊息回傳 None。"""
    if d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
        return None
    c = (d.get("message") or {}).get("content")
    if isinstance(c, str):
        parts = [c]
    elif isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return None
    parts = [p.strip() for p in parts]
    parts = [p for p in parts if p and not p.startswith("<") and not p.startswith("[Request interrupted")]
    return "\n".join(parts) or None


def assistant_text(d):
    if d.get("isSidechain"):
        return None
    c = (d.get("message") or {}).get("content")
    if not isinstance(c, list):
        return None
    t = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text").strip()
    return t or None


def _new_agg():
    return {"title_custom": None, "title_agent": None, "first_prompt": None, "last_prompt": None,
            "cwd": None, "started": None, "n_prompts": 0, "n_replies": 0, "out_tokens": 0,
            "context_tokens": 0, "model": None, "last_msg_id": None, "last_ts": None}


def _feed(agg, d):
    t = d.get("type")
    if t == "custom-title":
        agg["title_custom"] = d.get("customTitle") or agg["title_custom"]
    elif t == "agent-name":
        agg["title_agent"] = d.get("agentName") or agg["title_agent"]
    elif t == "user":
        if d.get("cwd") and not d.get("isSidechain"):
            agg["cwd"] = agg["cwd"] or d["cwd"]
        if not agg["started"] and d.get("timestamp"):
            agg["started"] = d["timestamp"]
        text = human_text(d)
        if text:
            agg["last_ts"] = d.get("timestamp") or agg["last_ts"]
            agg["n_prompts"] += 1
            agg["last_prompt"] = _clip(text, 200)
            if not agg["first_prompt"]:
                agg["first_prompt"] = _clip(text, 200)
    elif t == "assistant" and not d.get("isSidechain"):
        m = d.get("message") or {}
        mid = m.get("id")
        agg["last_ts"] = d.get("timestamp") or agg["last_ts"]
        if mid and mid != agg["last_msg_id"]:      # 同一則回覆會拆成多行，只算一次
            agg["last_msg_id"] = mid
            agg["n_replies"] += 1
            u = m.get("usage") or {}
            its = [i for i in (u.get("iterations") or []) if isinstance(i, dict)]
            out = max(u.get("output_tokens") or 0, sum(i.get("output_tokens") or 0 for i in its))
            agg["out_tokens"] += out
            src = its[-1] if its and not (u.get("input_tokens") or u.get("cache_read_input_tokens")) else u
            ctx = sum(src.get(k) or 0 for k in
                      ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            if ctx:
                agg["context_tokens"] = ctx
            if m.get("model") and m["model"] != "<synthetic>":
                agg["model"] = m["model"]


def _update(path, st):
    """回傳 (agg, 是否有變動)。"""
    state = _load_state()
    key = str(path)
    s = state.get(key)
    if s and s["size"] == st.st_size and s["mtime"] == st.st_mtime:
        return s["agg"], False
    if not s or st.st_size < s["offset"]:
        s = {"offset": 0, "agg": _new_agg()}
    with open(path, "rb") as f:
        f.seek(s["offset"])
        data = f.read()
    end = data.rfind(b"\n") + 1
    for line in data[:end].split(b"\n"):
        if not line or not any(m in line for m in MARKERS):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if isinstance(d, dict):
            _feed(s["agg"], d)
    s["offset"] += end
    s["size"], s["mtime"] = st.st_size, st.st_mtime
    state[key] = s
    return s["agg"], True


def _ts(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def live_sessions():
    """目前開著的 Claude 工作階段：sessionId -> {status, name, pid}。"""
    out = {}
    if not LIVE.is_dir():
        return out
    for f in LIVE.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            os.kill(int(d["pid"]), 0)
        except Exception:
            continue
        out[d.get("sessionId")] = {"status": d.get("status") or "idle", "name": d.get("name"),
                                   "pid": d.get("pid"), "cwd": d.get("cwd")}
    return out


def list_sessions():
    files = []
    if ROOT.is_dir():
        for p in ROOT.glob("*/*.jsonl"):
            try:
                files.append((p, p.stat()))
            except OSError:
                pass
    files.sort(key=lambda x: x[1].st_mtime, reverse=True)
    live = live_sessions()
    out, dirty = [], False
    with _lock:
        for p, st in files[:MAX_SESSIONS]:
            try:
                agg, changed = _update(p, st)
            except OSError:
                continue
            dirty = dirty or changed
            sid = p.stem
            lv = live.get(sid)
            if not agg["n_prompts"] and not lv:
                continue
            title = (lv or {}).get("name") or agg["title_custom"] or agg["title_agent"] \
                or agg["first_prompt"] or "（未命名）"
            cwd = agg["cwd"] or (lv or {}).get("cwd")
            out.append({
                "id": sid, "project": p.parent.name, "title": _clip(title, 80),
                "cwd": cwd, "project_name": Path(cwd).name if cwd else p.parent.name,
                "first_prompt": agg["first_prompt"], "last_prompt": agg["last_prompt"],
                "updated": agg["last_ts"] or datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                "started": agg["started"], "n_prompts": agg["n_prompts"], "n_replies": agg["n_replies"],
                "out_tokens": agg["out_tokens"], "context_tokens": agg["context_tokens"],
                "model": agg["model"], "size": st.st_size,
                "live": lv["status"] if lv else None,
            })
        out.sort(key=lambda x: _ts(x["updated"]), reverse=True)
        if dirty:
            try:
                tmp = CACHE_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps({"version": CACHE_VERSION, "files": _state}, ensure_ascii=False))
                tmp.replace(CACHE_FILE)
            except OSError:
                pass
    return {"sessions": out, "live_count": len(live),
            "busy_count": sum(1 for v in live.values() if v["status"] == "busy")}


def _path(project, sid):
    if not SID_RE.match(sid) or "/" in project or project.startswith("."):
        raise ValueError("bad session id")
    p = (ROOT / project / f"{sid}.jsonl").resolve()
    if ROOT.resolve() not in p.parents or not p.is_file():
        raise FileNotFoundError(sid)
    return p


def detail(project, sid, limit=40):
    """最後幾則對話的文字（只取檔案尾端，略過工具呼叫）。"""
    p = _path(project, sid)
    size = p.stat().st_size
    with open(p, "rb") as f:
        if size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
            f.readline()
        data = f.read()
    msgs = []
    for line in data.split(b"\n"):
        if b'"type":"user"' not in line and b'"type":"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        t = d.get("type")
        text = human_text(d) if t == "user" else assistant_text(d) if t == "assistant" else None
        if not text:
            continue
        if msgs and msgs[-1]["role"] == t and t == "assistant":
            msgs[-1]["text"] += "\n\n" + text
        else:
            msgs.append({"role": t, "text": text, "ts": d.get("timestamp")})
    msgs = msgs[-limit:]
    for m in msgs:
        if len(m["text"]) > 6000:
            m["text"] = m["text"][:6000] + "\n…（已截斷）"
    return {"messages": msgs, "truncated": size > TAIL_BYTES}


def resume_command(project, sid):
    _path(project, sid)
    with _lock:
        s = _load_state().get(str(ROOT / project / f"{sid}.jsonl"))
    cwd = (s or {}).get("agg", {}).get("cwd")
    if not cwd or not Path(cwd).is_dir():
        raise FileNotFoundError(f"找不到這個對話的工作資料夾：{cwd}")
    return cwd, f"cd {shlex.quote(cwd)} && claude --resume {sid}"


def resume_in_terminal(project, sid, search_path):
    """開一個終端機視窗接續對話（用 .command 檔，不需要自動化權限）。"""
    if sid in live_sessions():
        raise RuntimeError("這個對話目前正開著，請直接到 Claude 視窗繼續。")
    cwd, _ = resume_command(project, sid)
    claude = shutil.which("claude", path=search_path) or "claude"
    tmp = DATA / "tmp"
    tmp.mkdir(exist_ok=True)
    script = tmp / f"resume-{sid}.command"
    script.write_text(f"#!/bin/zsh\ncd {shlex.quote(cwd)} || exit 1\nexec {shlex.quote(claude)} --resume {sid}\n")
    script.chmod(0o700)
    subprocess.run(["open", str(script)], check=True, timeout=10)
    return {"ok": True}
