"""待辦事項：先存在本機（離線也能用），連得上 Google 時和 Google Tasks 雙向同步。

- 每次修改都標成「待同步」，背景每 5 分鐘、或修改後幾秒同步一次。
- 同一項兩邊都改過時，以修改時間較晚的為準。
- Google Tasks 只存日期，所以「時間」「重複」「釘選」只存在這台電腦。
"""
import calendar as _cal
import datetime as dt
import json
import subprocess
import threading
import time
import uuid

from . import db, google, home

API = "https://tasks.googleapis.com/tasks/v1"
SYNC_EVERY = 5 * 60
REPEATS = (None, "daily", "weekdays", "weekly", "monthly", "yearly")
EDITABLE = ("title", "notes", "due_date", "due_time", "status", "pinned", "repeat", "list_id", "sort")
LOCAL_ONLY = {"sort", "pinned", "due_time", "repeat"}     # 只存在這台電腦、不用同步到 Google 的欄位

SCHEMA = [
    """create table if not exists todos (
         id text primary key, google_id text unique, list_id text,
         title text not null, notes text default '',
         due_date text, due_time text,
         status text default 'open', completed_at real,
         pinned integer default 0, repeat text, source text,
         created_at real, updated_at real,
         deleted integer default 0, dirty integer default 0,
         remote_updated text)""",
    "create index if not exists todos_dirty on todos(dirty)",
    "create table if not exists todo_lists (id text primary key, title text, position integer)",
    "create table if not exists kv (k text primary key, v text)",
]

_sync_lock = threading.Lock()
_kick = threading.Event()


def init():
    for s in SCHEMA:
        db.execute(s)
    cols = {r["name"] for r in db.query("pragma table_info(todos)")}
    if "remote_list" not in cols:          # 這項目前在 Google 的哪個清單（用來處理「移到別的清單」）
        db.execute("alter table todos add column remote_list text")
        db.execute("update todos set remote_list=list_id where google_id is not null")
    if "notified_at" not in cols:          # 到期提醒送出的時間
        db.execute("alter table todos add column notified_at real")
    if "sort" not in cols:                 # 看板上的手動排序（只存在這台電腦）
        db.execute("alter table todos add column sort real")


def _kv(k, default=None):
    r = db.query("select v from kv where k=?", (k,))
    return json.loads(r[0]["v"]) if r else default


def _set_kv(k, v):
    db.execute("insert into kv(k, v) values(?, ?) on conflict(k) do update set v=excluded.v", (k, json.dumps(v)))


def _today():
    return dt.datetime.now(home.tz()).date()


def _rfc3339(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# ---------------- 本機操作 ----------------
def _clean(data):
    out = {}
    for k in EDITABLE:
        if k not in data:
            continue
        v = data[k]
        if k == "title":
            v = (v or "").strip()
            if not v:
                raise ValueError("待辦事項不能是空的")
            v = v[:500]
        elif k == "notes":
            v = (v or "")[:5000]
        elif k == "due_date":
            v = dt.date.fromisoformat(v).isoformat() if v else None
        elif k == "due_time":
            v = dt.time.fromisoformat(v).strftime("%H:%M") if v else None
        elif k == "status":
            if v not in ("open", "done"):
                raise ValueError("狀態不對")
        elif k == "pinned":
            v = 1 if v else 0
        elif k == "repeat":
            v = v or None
            if v not in REPEATS:
                raise ValueError("重複方式不對")
        elif k == "sort":
            v = float(v) if v is not None else None
        elif k == "list_id":
            v = v or None
            if v and not db.query("select 1 from todo_lists where id=?", (v,)):
                raise ValueError("找不到這個清單")
        out[k] = v
    return out


def get(tid):
    r = db.query("select * from todos where id=? and deleted=0", (tid,))
    if not r:
        raise KeyError(tid)
    return r[0]


def list_all():
    since = time.time() - 7 * 86400
    rows = db.query("select id, title, notes, due_date, due_time, status, completed_at, pinned, repeat, source,"
                    " list_id, sort, created_at, updated_at, dirty, google_id from todos"
                    " where deleted=0 and (status='open' or completed_at > ?)", (since,))
    for r in rows:
        r["source"] = json.loads(r["source"]) if r["source"] else None
    lists = db.query("select id, title from todo_lists order by position")
    return {"todos": rows, "sync": sync_status(), "lists": lists}


def create(data, source=None):
    f = _clean(data)
    if "title" not in f:
        raise ValueError("待辦事項不能是空的")
    if f.get("due_time") and not f.get("due_date"):
        f["due_date"] = _today().isoformat()
    now = time.time()
    tid = uuid.uuid4().hex
    row = {"title": f["title"], "notes": f.get("notes", ""), "due_date": f.get("due_date"),
           "due_time": f.get("due_time"), "status": "open", "pinned": f.get("pinned", 0),
           "repeat": f.get("repeat"), "source": json.dumps(source, ensure_ascii=False) if source else None}
    db.execute("insert into todos(id, title, notes, due_date, due_time, status, pinned, repeat, source, list_id,"
               " created_at, updated_at, dirty) values(?,?,?,?,?,?,?,?,?,?,?,?,1)",
               (tid, row["title"], row["notes"], row["due_date"], row["due_time"], row["status"], row["pinned"],
                row["repeat"], row["source"], f.get("list_id"), now, now))
    kick()
    return get(tid)


def _next_due(due, repeat):
    """下一次的日期。每月／每年以原本的日期為準（31 號遇到小月排在月底，下個月再回到 31 號）。"""
    d = dt.date.fromisoformat(due) if due else _today()
    anchor = d.day
    today = _today()

    def step(n, k):
        if repeat == "daily":
            return d + dt.timedelta(days=k)
        if repeat == "weekly":
            return d + dt.timedelta(days=7 * k)
        if repeat == "weekdays":
            n += dt.timedelta(days=1)
            while n.weekday() >= 5:
                n += dt.timedelta(days=1)
            return n
        months = k * (12 if repeat == "yearly" else 1)
        y, m = divmod(d.month - 1 + months, 12)
        y, m = d.year + y, m + 1
        return dt.date(y, m, min(anchor, _cal.monthrange(y, m)[1]))

    n, k = d, 0
    while True:                      # 拖很久才勾，就排到今天以後最近的一次
        k += 1
        n = step(n, k)
        if n >= today or k > 5000:
            return n.isoformat()


def update(tid, data):
    cur = get(tid)
    f = _clean(data)
    if not f:
        return cur
    now = time.time()
    if f.get("status") == "done" and cur["status"] != "done":
        f["completed_at"] = now
        repeat = f.get("repeat", cur["repeat"])
        if repeat:
            # 重複的待辦：這一次完成，另外建立下一次
            nxt = create({"title": f.get("title", cur["title"]), "notes": f.get("notes", cur["notes"]),
                          "due_date": _next_due(f.get("due_date", cur["due_date"]), repeat),
                          "due_time": f.get("due_time", cur["due_time"]), "repeat": repeat,
                          "pinned": f.get("pinned", cur["pinned"]), "list_id": f.get("list_id", cur["list_id"])},
                         json.loads(cur["source"]) if cur["source"] else None)
            f["repeat"] = None
            f["next_id"] = nxt["id"]
    elif f.get("status") == "open":
        f["completed_at"] = None
    next_id = f.pop("next_id", None)
    local_only = set(f) <= LOCAL_ONLY          # 只改排序、釘選這類本機欄位：不用同步
    if "due_date" in f or "due_time" in f:
        f["notified_at"] = None                 # 改了時間就重新提醒
    sets = ", ".join(f"{k}=?" for k in f)
    db.execute(f"update todos set {sets}, updated_at=?, dirty={'dirty' if local_only else '1'} where id=?", (*f.values(), now, tid))
    if not local_only:
        kick()
    out = get(tid)
    out["next_id"] = next_id
    return out


def delete(tid):
    get(tid)
    # 先標記刪除（可以復原），同步時才真正移除
    db.execute("update todos set deleted=1, dirty=1, updated_at=? where id=?", (time.time(), tid))
    kick()
    return {"ok": True}


def restore(tid):
    if not db.query("select 1 from todos where id=? and deleted=1", (tid,)):
        raise KeyError(tid)                     # 已經同步刪掉了，由前端重新建立
    db.execute("update todos set deleted=0, dirty=1, updated_at=? where id=?", (time.time(), tid))
    kick()
    return get(tid)


# ---------------- 到期提醒（macOS 通知） ----------------
def _notify(title, text):
    subprocess.run(["osascript", "-e", "on run argv", "-e",
                    "display notification (item 2 of argv) with title (item 1 of argv) sound name \"Glass\"",
                    "-e", "end run", title, text], capture_output=True, timeout=10)


def check_due():
    if not home.config().get("todo_notifications", True):
        return
    now = dt.datetime.now(home.tz())
    rows = db.query("select id, title, due_date, due_time from todos where status='open' and deleted=0"
                    " and due_time is not null and notified_at is null and due_date <= ?", (now.date().isoformat(),))
    for r in rows:
        due = dt.datetime.fromisoformat(f"{r['due_date']}T{r['due_time']}").replace(tzinfo=home.tz())
        if due <= now:
            if now - due <= dt.timedelta(minutes=15):       # 太久以前的（例如電腦剛開機）就不補發
                _notify("待辦到期", f"{r['due_time']} {r['title']}")
            db.execute("update todos set notified_at=? where id=?", (time.time(), r["id"]))


# ---------------- 清單（看板的欄位）：直接在 Google Tasks 新增、改名、刪除 ----------------
def _need_google():
    if not google.status()["connected"]:
        raise ValueError("要先連結 Google Tasks 才能管理清單")


def create_list(title):
    _need_google()
    title = (title or "").strip()[:100]
    if not title:
        raise ValueError("清單名稱不能是空的")
    r = google.request("POST", f"{API}/users/@me/lists", json={"title": title})
    if r.status_code >= 400:
        raise RuntimeError(f"建立清單失敗（{r.status_code}）：{r.text[:200]}")
    _lists()
    return {"id": r.json()["id"], "lists": db.query("select id, title from todo_lists order by position")}


def rename_list(lid, title):
    _need_google()
    title = (title or "").strip()[:100]
    if not title:
        raise ValueError("清單名稱不能是空的")
    r = google.request("PATCH", f"{API}/users/@me/lists/{lid}", json={"title": title})
    if r.status_code >= 400:
        raise RuntimeError(f"改名失敗（{r.status_code}）：{r.text[:200]}")
    _lists()
    return {"lists": db.query("select id, title from todo_lists order by position")}


def delete_list(lid):
    _need_google()
    lists = db.query("select id from todo_lists order by position")
    if lists and lists[0]["id"] == lid:
        raise ValueError("Google Tasks 的預設清單不能刪除")
    left = db.query("select count(*) n from todos where deleted=0 and status='open' and list_id=?", (lid,))[0]["n"]
    if left:
        raise ValueError(f"這個清單還有 {left} 項未完成的待辦，先移到別的清單再刪除")
    r = google.request("DELETE", f"{API}/users/@me/lists/{lid}")
    if r.status_code not in (200, 204, 404):
        raise RuntimeError(f"刪除失敗（{r.status_code}）：{r.text[:200]}")
    db.execute("delete from todos where list_id=?", (lid,))       # 只剩已完成的，Google 那邊也一起刪了
    db.execute("delete from todo_lists where id=?", (lid,))
    return {"lists": db.query("select id, title from todo_lists order by position")}


# ---------------- 和 Google Tasks 同步 ----------------
def sync_status():
    g = google.status()
    pending = db.query("select count(*) n from todos where dirty=1")[0]["n"]
    return {"state": _kv("sync_state", "never"), "at": _kv("sync_at"), "error": _kv("sync_error"),
            "pending": pending, "configured": g["configured"], "connected": g["connected"],
            "auth_error": g["error"]}


def kick():
    _kick.set()


def _lists():
    r = google.request("GET", f"{API}/users/@me/lists", params={"maxResults": 100})
    if r.status_code == 403:
        raise google.NeedAuth("連結 Google 時沒有勾選「Google Tasks」的權限，請重新連結並勾選。")
    r.raise_for_status()
    items = r.json().get("items", [])
    if items:
        db.execute(f"delete from todo_lists where id not in ({','.join('?' * len(items))})", [L["id"] for L in items])
    for i, L in enumerate(items):
        db.execute("insert into todo_lists(id, title, position) values(?,?,?)"
                   " on conflict(id) do update set title=excluded.title, position=excluded.position",
                   (L["id"], L.get("title"), i))
    return items


def _apply_remote(item, list_id):
    gid = item["id"]
    rts = _ts(item["updated"])
    local = db.query("select * from todos where google_id=?", (gid,))
    local = local[0] if local else None
    if item.get("deleted"):
        if local and not (local["dirty"] and local["updated_at"] > rts):
            db.execute("delete from todos where id=?", (local["id"],))
        return
    title = (item.get("title") or "").strip()
    done = item.get("status") == "completed"
    due = item["due"][:10] if item.get("due") else None
    completed = _ts(item["completed"]) if item.get("completed") else (rts if done else None)
    if local is None:
        if not title:
            return
        db.execute("insert into todos(id, google_id, list_id, remote_list, title, notes, due_date, status, completed_at,"
                   " created_at, updated_at, dirty, remote_updated) values(?,?,?,?,?,?,?,?,?,?,?,0,?)",
                   (uuid.uuid4().hex, gid, list_id, list_id, title, item.get("notes") or "", due,
                    "done" if done else "open", completed, rts, rts, item["updated"]))
        return
    if local["dirty"] and local["updated_at"] >= rts:
        return                                      # 這台的修改比較新，等一下推上去
    due_time = local["due_time"] if due else None
    db.execute("update todos set list_id=?, remote_list=?, title=?, notes=?, due_date=?, due_time=?, status=?, completed_at=?,"
               " deleted=0, dirty=0, updated_at=?, remote_updated=? where id=?",
               (list_id, list_id, title or local["title"], item.get("notes") or "", due, due_time,
                "done" if done else "open", completed, rts, item["updated"], local["id"]))


def _pull(list_id, since):
    params = {"showCompleted": "true", "showHidden": "true", "maxResults": 100}
    if since:
        params.update(showDeleted="true", updatedMin=_rfc3339(since - 120))
    token = None
    while True:
        if token:
            params["pageToken"] = token
        r = google.request("GET", f"{API}/lists/{list_id}/tasks", params=params)
        if r.status_code == 404:
            return
        r.raise_for_status()
        d = r.json()
        for item in d.get("items", []):
            _apply_remote(item, list_id)
        token = d.get("nextPageToken")
        if not token:
            return


def _push(default_list):
    for row in db.query("select * from todos where dirty=1"):
        lid = row["list_id"] or default_list
        gid = row["google_id"]
        if row["deleted"]:
            if gid:
                r = google.request("DELETE", f"{API}/lists/{lid}/tasks/{gid}")
                if r.status_code not in (200, 204, 404, 410):
                    raise RuntimeError(f"Google Tasks 刪除失敗（{r.status_code}）：{r.text[:200]}")
            db.execute("delete from todos where id=? and updated_at=?", (row["id"], row["updated_at"]))
            continue
        body = {"title": row["title"], "notes": row["notes"] or "",
                "status": "completed" if row["status"] == "done" else "needsAction",
                "due": f"{row['due_date']}T00:00:00.000Z" if row["due_date"] else None}
        if row["status"] != "done":
            body["completed"] = None
        r = None
        old_list = row["remote_list"]
        if gid and old_list and old_list != lid:
            # 移到別的清單：在新清單建立、再刪掉舊清單裡的那一項
            g = google.request("DELETE", f"{API}/lists/{old_list}/tasks/{gid}")
            if g.status_code not in (200, 204, 404, 410):
                raise RuntimeError(f"Google Tasks 移動失敗（{g.status_code}）：{g.text[:200]}")
            gid = None
        if gid:
            r = google.request("PATCH", f"{API}/lists/{lid}/tasks/{gid}", json=body)
            if r.status_code == 404:                   # Google 那邊已經刪掉了，但這台又改過：重新建立
                r = None
        if r is None:
            r = google.request("POST", f"{API}/lists/{lid}/tasks", json={k: v for k, v in body.items() if v is not None})
        if r.status_code >= 400:
            raise RuntimeError(f"Google Tasks 回報錯誤（{r.status_code}）：{r.text[:200]}")
        item = r.json()
        # 同步途中又被修改的話保留「待同步」，下一輪再推
        db.execute("update todos set google_id=?, list_id=?, remote_list=?, remote_updated=?,"
                   " dirty=case when updated_at=? then 0 else dirty end where id=?",
                   (item["id"], lid, lid, item.get("updated"), row["updated_at"], row["id"]))


def sync():
    if not google.status()["connected"]:
        _set_kv("sync_state", "not_connected")
        return sync_status()
    with _sync_lock:
        started = time.time()
        try:
            lists = _lists()
            if not lists:
                raise RuntimeError("Google Tasks 沒有任何清單")
            since = _kv("sync_last", 0)
            for L in lists:
                _pull(L["id"], since)
            _push(lists[0]["id"])                      # 第一個清單就是 Google 的預設清單「我的工作」
            _set_kv("sync_last", started)
            _set_kv("sync_state", "ok")
            _set_kv("sync_error", None)
        except google.Offline as e:
            _set_kv("sync_state", "offline")
            _set_kv("sync_error", str(e))
        except google.NeedAuth as e:
            _set_kv("sync_state", "need_auth")
            _set_kv("sync_error", str(e))
        except Exception as e:
            _set_kv("sync_state", "error")
            _set_kv("sync_error", str(e) or type(e).__name__)
        _set_kv("sync_at", time.time())
    return sync_status()


def _loop():
    while True:
        triggered = _kick.wait(SYNC_EVERY)
        _kick.clear()
        if triggered:
            time.sleep(3)                     # 連續修改時合併成一次同步
            _kick.clear()
        try:
            sync()
        except Exception as e:
            print("todo sync:", e, flush=True)


def _due_loop():
    while True:
        try:
            check_due()
        except Exception as e:
            print("todo due:", e, flush=True)
        time.sleep(30)


def start_sync():
    threading.Thread(target=_loop, daemon=True).start()
    threading.Thread(target=_due_loop, daemon=True).start()
    kick()
