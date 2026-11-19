"""常用工作：定義在 config/tasks.json，網頁只能執行這裡列出的指令。"""
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import db

CONFIG = db.ROOT / "config" / "tasks.json"
OUTPUT_LIMIT = 200_000
_status_cache = {}
_status_lock = threading.Lock()
_env = None


def run_env():
    """子程序用的環境：PATH 取登入 shell 的設定，避免從 launchd 啟動時找不到 python3／ollama／claude。"""
    global _env
    if _env is None:
        env = dict(os.environ)
        env.pop("VIRTUAL_ENV", None)
        paths = []
        try:
            out = subprocess.run(["/bin/zsh", "-lc", "print -r -- $PATH"], capture_output=True,
                                 text=True, timeout=10).stdout.strip().splitlines()
            paths += out[-1].split(":") if out else []
        except Exception:
            pass
        paths += env.get("PATH", "").split(":")
        paths += ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin"),
                  "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        venv_bin = str(db.ROOT / ".venv" / "bin")
        seen, clean = set(), []
        for p in paths:
            if p and p != venv_bin and p not in seen:
                seen.add(p)
                clean.append(p)
        env["PATH"] = ":".join(clean)
        env["PYTHONUNBUFFERED"] = "1"
        _env = env
    return _env


def load_config():
    if not CONFIG.exists():
        return []                      # 還沒建立 config/tasks.json（可以從 tasks.example.json 複製）
    data = json.loads(CONFIG.read_text())
    tasks = data.get("tasks", [])
    for t in tasks:
        if not t.get("id") or not t.get("name") or not t.get("cwd"):
            raise ValueError("每個工作都要有 id、name、cwd")
        for a in t.get("actions", []):
            if not a.get("id") or not isinstance(a.get("cmd"), list) or not all(isinstance(x, str) for x in a["cmd"]):
                raise ValueError(f"工作 {t['id']} 的動作 {a.get('id')} 需要 id 和 cmd（字串陣列）")
    return tasks


def _find(task_id):
    for t in load_config():
        if t["id"] == task_id:
            return t
    raise KeyError(task_id)


def _cwd(task):
    return Path(task["cwd"]).expanduser()


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def tail(path, max_bytes=64_000):
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _file(task, path, exclude=None):
    """記錄檔路徑；可以用萬用字元（例如 logs/*.log），會取最近修改的那個，exclude 是要排除的檔名規則。"""
    if not any(ch in path for ch in "*?["):
        return _cwd(task) / path
    files = [f for f in _cwd(task).glob(path) if f.is_file() and not (exclude and re.search(exclude, f.name))]
    return max(files, key=lambda f: f.stat().st_mtime) if files else _cwd(task) / path


def running_pid(task):
    proc = (task.get("running") or {}).get("process")
    if proc:                                     # 用程序名稱判斷（沒有 PID 檔的工作，例如模型訓練）
        try:
            r = subprocess.run(["pgrep", "-f", proc], capture_output=True, text=True, timeout=5)
            pids = [int(x) for x in r.stdout.split() if x.strip().isdigit() and int(x) != os.getpid()]
            return pids[0] if pids else None
        except Exception:
            return None
    pidfile = (task.get("running") or {}).get("pidfile")
    if pidfile:
        try:
            pid = int((_cwd(task) / pidfile).read_text().strip())
        except Exception:
            return None
        return pid if _alive(pid) else None
    rows = db.query("select pid from runs where task_id=? and is_long=1 and ended_at is null", (task["id"],))
    for r in rows:
        if r["pid"] and _alive(r["pid"]):
            return r["pid"]
    return None


def _progress(task):
    cfg = task.get("progress")
    if not cfg:
        return None
    try:
        f = _file(task, cfg["file"], cfg.get("exclude"))
        ms = list(re.finditer(cfg["regex"], tail(f)))
        if not ms:
            return None
        m = ms[-1]
        done, total = int(m.group(cfg.get("done", 1))), int(m.group(cfg.get("total", 2)))
        if cfg.get("state"):                    # 分段接續的工作：JSON 裡的 offset 是之前各段完成的量、total 是全部的量
            try:
                st = json.loads((_cwd(task) / cfg["state"]).read_text())
                done, total = done + int(st.get("offset", 0)), int(st.get("total", total))
            except (OSError, ValueError):
                pass
        if cfg.get("note_format"):              # 例如 "剩 {3}・loss {4}"：{n} 是第 n 個擷取群組
            note = re.sub(r"\{(\d+)\}", lambda x: m.group(int(x.group(1))) or "", cfg["note_format"])
        else:
            note = m.group(cfg["note"]) if cfg.get("note") else None
        return {"done": done, "total": total, "unit": cfg.get("unit", ""), "note": note,
                "prefix": cfg.get("note_prefix", "預估剩 "), "file": f.name}
    except Exception:
        return None


def _status(task, force=False):
    cfg = task.get("status")
    if not cfg:
        return None
    with _status_lock:
        c = _status_cache.get(task["id"])
    if c and not force and time.time() - c["at"] < cfg.get("every", 30):
        return c
    try:
        r = subprocess.run(cfg["cmd"], cwd=_cwd(task), env=run_env(), capture_output=True,
                           text=True, timeout=cfg.get("timeout", 30))
        c = {"at": time.time(), "ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()}
    except Exception as e:
        c = {"at": time.time(), "ok": False, "output": f"無法取得狀態：{e}"}
    with _status_lock:
        _status_cache[task["id"]] = c
    return c


def list_tasks():
    try:
        tasks = load_config()
    except Exception as e:
        return {"tasks": [], "error": f"config/tasks.json 讀取失敗：{e}"}
    stats = {r["task_id"]: r for r in db.query(
        "select task_id, max(started_at) last_run, count(*) n from runs group by task_id")}
    out = []
    for t in tasks:
        pid = running_pid(t)
        prog = _progress(t)
        if prog and not pid:
            prog["note"] = None                  # 沒在跑時「預估剩多久」沒有意義
        out.append({
            "id": t["id"], "name": t["name"], "description": t.get("description", ""), "cwd": t["cwd"],
            "cwd_ok": _cwd(t).is_dir(),
            "running": bool(pid), "pid": pid, "progress": prog, "status": _status(t),
            "has_log": bool(t.get("log")),
            "last_run": (stats.get(t["id"]) or {}).get("last_run"),
            "run_count": (stats.get(t["id"]) or {}).get("n", 0),
            "actions": [{k: a.get(k) for k in ("id", "label", "when", "confirm", "long", "style", "inputs", "hint")}
                        for a in t.get("actions", [])],
        })
    out.sort(key=lambda x: (not x["running"], -(x["last_run"] or 0)))
    return {"tasks": out}


def task_log(task_id):
    t = _find(task_id)
    if not t.get("log"):
        return {"text": ""}
    text = tail(_file(t, t["log"], t.get("log_exclude")), 32_000)
    # 進度條會用 \r 一直覆寫同一行：只保留每一段最後的狀態
    lines = [ln.split("\r")[-1] for ln in text.replace("\r\n", "\n").split("\n")]
    lines = [ln for ln in lines if ln.strip()]
    return {"text": "\n".join(lines[-120:])}


def _watch(run_id, proc, logf):
    code = proc.wait()
    logf.close()
    db.execute("update runs set ended_at=?, exit_code=? where id=?", (time.time(), code, run_id))


def run_action(task_id, action_id, inputs):
    t = _find(task_id)
    a = next((x for x in t.get("actions", []) if x["id"] == action_id), None)
    if not a:
        raise KeyError(action_id)
    cwd = _cwd(t)
    if not cwd.is_dir():
        raise RuntimeError(f"找不到工作資料夾：{cwd}")
    pid = running_pid(t)
    if a.get("when") == "stopped" and pid:
        raise RuntimeError(f"已經在執行中（PID {pid}）")
    if a.get("when") == "running" and not pid:
        raise RuntimeError("目前沒有在執行")
    values = {}
    for spec in a.get("inputs") or []:
        v = str((inputs or {}).get(spec["id"], spec.get("default", ""))).strip()
        if not v and not spec.get("optional"):
            raise RuntimeError(f"請填寫「{spec.get('label', spec['id'])}」")
        values[spec["id"]] = v
    cmd = []
    for arg in a["cmd"]:
        for k, v in values.items():
            arg = arg.replace("{" + k + "}", v)
        cmd.append(arg)
    run_id = db.execute(
        "insert into runs(task_id, action_id, task_name, action_label, cmd, inputs, is_long, started_at)"
        " values(?,?,?,?,?,?,?,?)",
        (t["id"], a["id"], t["name"], a.get("label", a["id"]), json.dumps(cmd, ensure_ascii=False),
         json.dumps(values, ensure_ascii=False), 1 if a.get("long") else 0, time.time()))
    try:
        if a.get("long"):
            log_path = db.DATA / "runs" / f"{run_id}.log"
            logf = open(log_path, "wb")
            proc = subprocess.Popen(cmd, cwd=cwd, env=run_env(), stdin=subprocess.DEVNULL, stdout=logf,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            db.execute("update runs set pid=?, log_path=? where id=?", (proc.pid, str(log_path), run_id))
            threading.Thread(target=_watch, args=(run_id, proc, logf), daemon=True).start()
            time.sleep(1.2)          # 馬上失敗的話（例如指令打錯）直接把訊息帶回去
            result = {"run_id": run_id, "long": True, "exit_code": proc.poll(),
                      "output": tail(log_path, OUTPUT_LIMIT)}
        else:
            r = subprocess.run(cmd, cwd=cwd, env=run_env(), capture_output=True, text=True,
                               timeout=a.get("timeout", 120), stdin=subprocess.DEVNULL)
            output = (r.stdout + r.stderr)[-OUTPUT_LIMIT:]
            db.execute("update runs set ended_at=?, exit_code=?, output=? where id=?",
                       (time.time(), r.returncode, output, run_id))
            result = {"run_id": run_id, "long": False, "exit_code": r.returncode, "output": output}
    except subprocess.TimeoutExpired:
        db.execute("update runs set ended_at=?, exit_code=?, output=? where id=?",
                   (time.time(), -1, f"超過 {a.get('timeout', 120)} 秒，已中止。", run_id))
        raise RuntimeError(f"執行超過 {a.get('timeout', 120)} 秒，已中止")
    except Exception as e:
        db.execute("update runs set ended_at=?, exit_code=?, output=? where id=?",
                   (time.time(), -1, f"無法執行：{e}", run_id))
        raise RuntimeError(f"無法執行：{e}")
    finally:
        with _status_lock:
            _status_cache.pop(t["id"], None)
    return result


def list_runs(limit=30):
    rows = db.query("select id, task_id, action_id, task_name, action_label, is_long, started_at, ended_at,"
                    " exit_code, pid, inputs from runs order by id desc limit ?", (limit,))
    for r in rows:
        r["running"] = bool(r["ended_at"] is None and r["pid"] and _alive(r["pid"]))
    return {"runs": rows}


def get_run(run_id):
    rows = db.query("select * from runs where id=?", (run_id,))
    if not rows:
        raise KeyError(run_id)
    r = rows[0]
    if r["is_long"] and r["log_path"]:
        r["output"] = tail(Path(r["log_path"]), OUTPUT_LIMIT)
    r["running"] = bool(r["ended_at"] is None and r["pid"] and _alive(r["pid"]))
    return r


def reconcile():
    """控制台重啟後，把已經不在的長時間工作標成結束（結果未知）。"""
    for r in db.query("select id, pid from runs where ended_at is null"):
        if not r["pid"] or not _alive(r["pid"]):
            db.execute("update runs set ended_at=? where id=?", (time.time(), r["id"]))


def pick_path(kind):
    """叫出 macOS 的選擇檔案／資料夾視窗，回傳路徑。"""
    what = "choose folder" if kind == "folder" else "choose file"
    r = subprocess.run(["osascript", "-e", "activate", "-e", f"POSIX path of ({what})"],
                       capture_output=True, text=True, timeout=300)
    return {"path": r.stdout.strip() or None}
