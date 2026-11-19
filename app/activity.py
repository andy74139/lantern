"""記錄這個頁面平常怎麼被使用，之後調整版面用。只存在本機（data/controller.db）。

- view：每個區塊「真的在看」的秒數。只在分頁在前景、視窗有焦點、而且最近 60 秒內有滑鼠／鍵盤／捲動時才算，
        依區塊佔畫面的比例分配；頁面只是掛著沒人動的時間不算。
- act： 按了哪些功能（按鈕的動作名稱與所在區塊），只記名稱和次數，不記內容（例如待辦的文字、錄音標題）。
- page：首頁／AI 工作各看了多久。
- env： 視窗大小（每天各出現幾次），用來決定版面要照哪種寬度設計。
"""
import re
import time
from datetime import date, timedelta

from . import db

KINDS = ("view", "act", "page", "env")
_KEY = re.compile(r"^[A-Za-z0-9_:.\-x]{1,80}$")


def init():
    db.execute("""create table if not exists activity (
        day text, kind text, key text, secs real default 0, n integer default 0, primary key (day, kind, key))""")


def record(data):
    """data: {"view": {key: 秒}, "page": {key: 秒}, "act": {key: 次數}, "env": {key: 次數}}"""
    day = date.today().isoformat()
    rows = 0
    for kind in KINDS:
        for key, v in ((data or {}).get(kind) or {}).items():
            if not _KEY.match(str(key)):
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if not (0 < v <= 3600):
                continue
            secs, n = (v, 0) if kind in ("view", "page") else (0, int(v))
            db.execute("insert into activity(day, kind, key, secs, n) values(?,?,?,?,?) "
                       "on conflict(day, kind, key) do update set secs=secs+excluded.secs, n=n+excluded.n",
                       (day, kind, key, secs, n))
            rows += 1
            if rows > 300:
                return {"ok": True}
    return {"ok": True}


def summary(days=30):
    since = (date.today() - timedelta(days=max(1, min(int(days), 365)) - 1)).isoformat()
    rows = db.query("select kind, key, sum(secs) secs, sum(n) n, count(distinct day) days from activity "
                    "where day >= ? group by kind, key", (since,))
    out = {k: [] for k in KINDS}
    for r in rows:
        out[r["kind"]].append({"key": r["key"], "min": round(r["secs"] / 60, 1), "n": r["n"], "days": r["days"]})
    for k in ("view", "page"):
        out[k].sort(key=lambda x: -x["min"])
    for k in ("act", "env"):
        out[k].sort(key=lambda x: -x["n"])
    active_days = db.query("select count(distinct day) d from activity where day >= ?", (since,))[0]["d"]
    return {"since": since, "days_with_use": active_days, **out, "generated": time.time()}
