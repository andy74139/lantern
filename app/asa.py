"""SOA ASA 準備進度：考試項目、各科讀書計畫，以及依每天可讀時數自動排程。

排程每次都從「今天」往後重算，所以提早或延後完成某個單元，後面的計畫會自動往前或往後移。
資料存在 data/controller.db（不會進 git）。
"""
import datetime as dt
import json
import re
import time

from . import db, home

WEEK_DEFAULT = [1.5, 1.5, 1.5, 1.5, 1.5, 3.0, 3.0]        # 週一到週日，每天可讀幾小時
STATUSES = ("todo", "studying", "passed", "waived")

# 預設的 ASA 項目（SOA 會調整，請對照官網；頁面上可以自由增刪）
DEFAULT_REQS = [
    ("Exam P", "exam"), ("Exam FM", "exam"), ("Exam FAM", "exam"), ("Exam SRM", "exam"), ("Exam PA", "exam"),
    ("Exam ALTAM", "exam"), ("ATPA", "exam"),
    ("VEE 數理統計", "vee"), ("VEE 經濟", "vee"), ("VEE 會計與財務", "vee"),
    ("FAP（Fundamentals of Actuarial Practice）", "course"), ("APC（專業操守課程）", "course"),
]

# 官方 syllabus 的 Topic（SOA：FAM 2026 年 2 月、ALTAM 2026 年 10 月、PA 2026 年 4 月版；SOA 改版時請更新）
SYLLABUS = {
    "FAM": ("Fundamentals of Actuarial Mathematics", "2026-02", [
        ("Short-Term Insurance and Reinsurance Coverages", "5-10%"),
        ("Severity, Frequency, and Aggregate Models", "12.5-17.5%"),
        ("Parametric Estimation", "2.5-7.5%"),
        ("Introduction to Credibility", "2.5-5%"),
        ("Pricing and Reserving for Short-Term Insurance Coverages", "10-15%"),
        ("Option Pricing Fundamentals", "2.5-7.5%"),
        ("Long-Term Insurance Coverages and Retirement Financial Security Programs", "2.5-5%"),
        ("Mortality Models", "10-15%"),
        ("Present Value Random Variables for Long-Term Insurance Coverages", "12.5-20%"),
        ("Premium and Policy Value Calculation for Long-Term Insurance Coverages", "15-22.5%"),
    ]),
    "ALTAM": ("Advanced Long-Term Actuarial Mathematics", "2026-10", [
        ("Survival Models for Contingent Cash Flows", "10-20%"),
        ("Premium and Policy Valuation for Long-Term State-Dependent Coverages", "12-20%"),
        ("Joint Life Insurance and Annuities", "8-16%"),
        ("Profit Analysis", "10-20%"),
        ("Pension Plans and Retirement Benefits", "10-18%"),
        ("Universal Life Insurance", "10-18%"),
        ("Embedded Options in Life Insurance and Annuity Products", "10-18%"),
    ]),
    "PA": ("Predictive Analytics", "2026-04", [
        ("Predictive Analytics Problem Definition", "10-20%"),
        ("Data Exploration and Visualization", "20-30%"),
        ("Data Transformations and Unsupervised Learning Techniques", "10-20%"),
        ("Generalized Linear Models", "25-35%"),
        ("Tree-Based Models", "10-20%"),
    ]),
}
DEFAULT_PLANS = {k: (v[0], [(t, 10) for t, _ in v[2]]) for k, v in SYLLABUS.items()}
EXAM_WEIGHT = 0.70          # 完整進度：考試佔 70%，其他項目（VEE、ATPA、FAP、APC…）佔 30%


def init():
    db.execute("""create table if not exists asa_reqs (
        id integer primary key, ord real, name text, kind text, status text default 'todo', date text, note text)""")
    db.execute("""create table if not exists asa_exams (
        code text primary key, name text, exam_date text, start_date text, hours text, review_days integer default 21,
        active integer default 1, ord integer)""")
    db.execute("""create table if not exists asa_items (
        id integer primary key, exam text, ord real, title text, hours real, done_at text)""")
    cols = {r["name"] for r in db.query("pragma table_info(asa_items)")}
    if "target_date" not in cols:          # 自己設定的預定完成日
        db.execute("alter table asa_items add column target_date text")
    if "shift_days" not in cols:           # 完成時把後面日期平移了幾天（取消完成時要移回來）
        db.execute("alter table asa_items add column shift_days integer")
    if "weight" not in cols:               # 官方 syllabus 的權重範圍
        db.execute("alter table asa_items add column weight text")
    if "kind" not in cols:                 # topic＝官方 syllabus 的 Topic；task＝自己加的（刷題、模擬考…）
        db.execute("alter table asa_items add column kind text")
        db.execute("update asa_items set kind=case when weight is not null then 'topic' else 'task' end")
    if "link" not in cols:                 # 這一項的學習頁連結（例如 Claude artifact）
        db.execute("alter table asa_items add column link text")
    ecols = {r["name"] for r in db.query("pragma table_info(asa_exams)")}
    if "link" not in ecols:                # 考科的學習頁／題庫連結與顯示文字
        db.execute("alter table asa_exams add column link text")
        db.execute("alter table asa_exams add column link_label text")
    db.execute("create table if not exists asa_meta (k text primary key, v text)")
    _migrate_syllabus()
    if not db.query("select 1 from asa_reqs limit 1"):
        for i, (name, kind) in enumerate(DEFAULT_REQS):
            st = "studying" if name.startswith(("Exam FAM", "Exam ALTAM", "Exam PA")) else "todo"
            db.execute("insert into asa_reqs(ord, name, kind, status) values(?,?,?,?)", (i, name, "other" if name == "ATPA" else kind, st))
    if not db.query("select 1 from asa_exams limit 1"):
        today = _today()
        fam_exam = today + dt.timedelta(days=120)
        dates = {"FAM": (today, fam_exam), "ALTAM": (fam_exam + dt.timedelta(days=1), fam_exam + dt.timedelta(days=150)),
                 "PA": (today, None)}
        for k, (code, (name, items)) in enumerate(DEFAULT_PLANS.items()):
            start, exam = dates[code]
            db.execute("insert into asa_exams(code, name, exam_date, start_date, hours, review_days, active, ord) values(?,?,?,?,?,?,1,?)",
                       (code, name, exam.isoformat() if exam else None, start.isoformat(), json.dumps(WEEK_DEFAULT), 21, k))
            for i, (title, w) in enumerate(SYLLABUS[code][2]):
                db.execute("insert into asa_items(exam, ord, title, hours, weight, kind) values(?,?,?,?,?,'topic')", (code, i, title, 10, w))
        db.execute("insert into asa_meta(k, v) values('syllabus', '2026') on conflict(k) do update set v=excluded.v")


def _migrate_syllabus():
    """改用官方 syllabus 的 Topic（只做一次）；已經有進度（完成或排了日期）的考科不動。也補上 PA。"""
    if db.query("select 1 from asa_meta where k='syllabus' and v='2026'"):
        return
    if not db.query("select 1 from asa_exams limit 1"):
        return                                    # 全新安裝：等下面的預設流程建立
    for code, (name, _ver, topics) in SYLLABUS.items():
        if not db.query("select 1 from asa_exams where code=?", (code,)):
            n = db.query("select coalesce(max(ord), 0) + 1 n from asa_exams")[0]["n"]
            db.execute("insert into asa_exams(code, name, exam_date, start_date, hours, review_days, active, ord) values(?,?,?,?,?,?,1,?)",
                       (code, name, None, _today().isoformat(), json.dumps(WEEK_DEFAULT), 21, n))
        busy = db.query("select 1 from asa_items where exam=? and (done_at is not null or target_date is not null) limit 1", (code,))
        if busy:
            continue
        db.execute("delete from asa_items where exam=?", (code,))
        for i, (title, w) in enumerate(topics):
            db.execute("insert into asa_items(exam, ord, title, hours, weight, kind) values(?,?,?,?,?,'topic')", (code, i, title, 10, w))
    db.execute("update asa_reqs set kind='other' where name like 'ATPA%' and kind='exam'")
    db.execute("update asa_reqs set status='studying' where name='Exam PA' and status='todo'")
    db.execute("insert into asa_meta(k, v) values('syllabus', '2026') on conflict(k) do update set v=excluded.v")


def _today():
    return dt.datetime.now(home.tz()).date()


def _d(s):
    return dt.date.fromisoformat(s) if s else None


# ---------------- 排程 ----------------
def schedule(exam, items, today=None):
    """把還沒完成的單元依序排進每天可讀的時數裡；回傳每個單元的預定起訖與整體狀態。"""
    today = today or _today()
    hours = json.loads(exam["hours"] or "null") or WEEK_DEFAULT
    start = max(today, _d(exam["start_date"]) or today)
    exam_date = _d(exam["exam_date"])
    review_start = exam_date - dt.timedelta(days=exam["review_days"] or 0) if exam_date else None
    day, used = start, 0.0
    limit = start + dt.timedelta(days=3 * 365)
    plan = []
    for it in items:
        p = dict(it)
        if it["done_at"]:
            p.update(plan_start=None, plan_end=None, late=False)
            plan.append(p)
            continue
        need, first = float(it["hours"] or 0), None
        while need > 1e-9 and day <= limit:
            cap = hours[day.weekday()] - used
            if cap <= 1e-9:
                day, used = day + dt.timedelta(days=1), 0.0
                continue
            first = first or day
            take = min(cap, need)
            need -= take
            used += take
            if need <= 1e-9:
                break
        p.update(plan_start=(first or day).isoformat(), plan_end=day.isoformat(),
                 late=bool(review_start and day >= review_start))
        plan.append(p)
        if used >= hours[day.weekday()] - 1e-9:
            day, used = day + dt.timedelta(days=1), 0.0

    total = sum(float(i["hours"] or 0) for i in items)
    done = sum(float(i["hours"] or 0) for i in items if i["done_at"])
    remaining = total - done
    pending = [p for p in plan if not p["done_at"]]
    finish = _d(pending[-1]["plan_end"]) if pending else None

    def capacity(a, b):          # a（含）到 b（不含）之間可讀的時數
        n, d = 0.0, a
        while d < b:
            n += hours[d.weekday()]
            d += dt.timedelta(days=1)
        return n

    st = {"total": total, "done": done, "remaining": remaining, "weekly": sum(hours),
          "finish": finish.isoformat() if finish else None, "days_left": (exam_date - today).days if exam_date else None,
          "review_start": review_start.isoformat() if review_start else None}
    if review_start:
        avail = capacity(start, review_start)
        st["buffer_days"] = (review_start - finish).days - 1 if finish else None
        st["short_hours"] = max(0.0, remaining - avail)
        weeks = max((review_start - start).days, 1) / 7
        st["need_weekly"] = remaining / weeks if remaining else 0
        # 跟「從開始日平均讀到總複習」比：現在應該讀完多少
        s0 = _d(exam["start_date"]) or today
        full = capacity(s0, review_start)
        elapsed = capacity(s0, min(today, review_start)) if today > s0 else 0
        st["expected_done"] = total * (elapsed / full) if full else 0
        st["ahead_hours"] = done - st["expected_done"]
    # 本週（今天到週日）要讀的
    week_end = today + dt.timedelta(days=6 - today.weekday())
    st["this_week"] = [p["id"] for p in pending if p["plan_start"] <= week_end.isoformat()]
    return plan, st


def topic_stats(e, items, today=None):
    """依 Topic 數計算進度，以及你自己設定的日期是否來得及。"""
    today = today or _today()
    exam_date = _d(e["exam_date"])
    review_start = exam_date - dt.timedelta(days=e["review_days"] or 0) if exam_date else None
    pending = [i for i in items if not i["done_at"]]
    dated = [i for i in pending if i["target_date"]]
    last = max((i["target_date"] for i in dated), default=None)
    overdue = [i for i in pending if i["target_date"] and i["target_date"] < today.isoformat()]
    week_end = (today + dt.timedelta(days=6 - today.weekday())).isoformat()
    return {"done": len(items) - len(pending), "total": len(items), "overdue": len(overdue),
            "undated": len(pending) - len(dated), "last": last,
            "days_left": (exam_date - today).days if exam_date else None,
            "review_start": review_start.isoformat() if review_start else None,
            "buffer_days": (review_start - _d(last)).days if review_start and last else None,
            "next": next(({"id": i["id"], "title": i["title"], "target_date": i["target_date"]} for i in pending), None),
            "this_week": [i["id"] for i in pending if i["target_date"] and i["target_date"] <= week_end]}


def overview():
    reqs = db.query("select * from asa_reqs order by ord, id")
    exams = []
    for e in db.query("select * from asa_exams order by ord, code"):
        items = db.query("select * from asa_items where exam=? order by ord, id", (e["code"],))
        e["hours"] = json.loads(e["hours"] or "null") or WEEK_DEFAULT
        exams.append({**e, "items": items, "stats": topic_stats(e, items)})
    passed = sum(1 for r in reqs if r["status"] in ("passed", "waived"))
    plans = {e["code"]: e["stats"] for e in exams}

    def progress(r):
        if r["status"] in ("passed", "waived"):
            return 1.0
        for code, st in plans.items():               # 有讀書計畫的考科：依 Topic 完成比例
            if re.search(rf"\b{re.escape(code)}\b", r["name"]) and st["total"]:
                return st["done"] / st["total"]
        return 0.0
    ex = [r for r in reqs if r["kind"] == "exam"]
    other = [r for r in reqs if r["kind"] != "exam"]
    for r in reqs:
        r["progress"] = progress(r)
    ex_p = sum(r["progress"] for r in ex) / len(ex) if ex else 0
    ot_p = sum(r["progress"] for r in other) / len(other) if other else 0
    w = EXAM_WEIGHT if ex and other else (1.0 if ex else 0.0)
    overall = ex_p * w + ot_p * (1 - w)
    return {"reqs": reqs, "exams": exams, "passed": passed, "total": len(reqs), "today": _today().isoformat(),
            "overall": overall, "exam_part": {"done": sum(1 for r in ex if r["progress"] >= 1), "total": len(ex), "pct": ex_p, "weight": w},
            "other_part": {"done": sum(1 for r in other if r["progress"] >= 1), "total": len(other), "pct": ot_p, "weight": 1 - w},
            "syllabus": {k: v[1] for k, v in SYLLABUS.items()}}


# ---------------- 修改 ----------------
def _link(v):
    """只接受 https 連結（避免 javascript: 之類的網址被放進頁面）。"""
    v = (v or "").strip()
    if not v:
        return None
    if not re.match(r"^https://[^\s<>\"']+$", v) or len(v) > 500:
        raise ValueError("連結要是 https:// 開頭的網址")
    return v


def update_req(rid, data):
    f = {k: data[k] for k in ("name", "status", "date", "note", "ord") if k in data}
    if "status" in f and f["status"] not in STATUSES:
        raise ValueError("狀態不對")
    if "name" in f and not (f["name"] or "").strip():
        raise ValueError("名稱不能是空的")
    if f:
        db.execute(f"update asa_reqs set {', '.join(k + '=?' for k in f)} where id=?", (*f.values(), rid))
    return overview()


def add_req(name, kind="exam"):
    if not (name or "").strip():
        raise ValueError("名稱不能是空的")
    n = db.query("select coalesce(max(ord), 0) + 1 n from asa_reqs")[0]["n"]
    db.execute("insert into asa_reqs(ord, name, kind) values(?,?,?)", (n, name.strip()[:80], kind))
    return overview()


def delete_req(rid):
    db.execute("delete from asa_reqs where id=?", (rid,))
    return overview()


def update_exam(code, data):
    f = {}
    for k in ("exam_date", "start_date"):
        if k in data:
            f[k] = dt.date.fromisoformat(data[k]).isoformat() if data[k] else None
    if "hours" in data:
        h = [max(0.0, min(16.0, float(x))) for x in data["hours"]]
        if len(h) != 7:
            raise ValueError("要有週一到週日 7 天的時數")
        f["hours"] = json.dumps(h)
    if "review_days" in data:
        f["review_days"] = max(0, min(120, int(data["review_days"])))
    if "active" in data:
        f["active"] = 1 if data["active"] else 0
    if "link" in data:
        f["link"] = _link(data["link"])
    if "link_label" in data:
        f["link_label"] = (data["link_label"] or "").strip()[:20] or None
    if f:
        db.execute(f"update asa_exams set {', '.join(k + '=?' for k in f)} where code=?", (*f.values(), code))
    return overview()


def add_item(code, title, hours):
    if not (title or "").strip():
        raise ValueError("單元名稱不能是空的")
    n = db.query("select coalesce(max(ord), 0) + 1 n from asa_items where exam=?", (code,))[0]["n"]
    db.execute("insert into asa_items(exam, ord, title, hours, kind) values(?,?,?,?,'task')", (code, n, title.strip()[:120], max(0.5, float(hours or 5))))
    return overview()


def update_item(iid, data):
    f = {}
    if "title" in data:
        if not (data["title"] or "").strip():
            raise ValueError("單元名稱不能是空的")
        f["title"] = data["title"].strip()[:120]
    if "hours" in data:
        f["hours"] = max(0.5, min(500.0, float(data["hours"])))
    if "target_date" in data:
        f["target_date"] = dt.date.fromisoformat(data["target_date"]).isoformat() if data["target_date"] else None
    if "done" in data:
        f["done_at"] = _today().isoformat() if data["done"] else None
    if "ord" in data:
        f["ord"] = float(data["ord"])
    if "link" in data:
        f["link"] = _link(data["link"])
    if f:
        db.execute(f"update asa_items set {', '.join(k + '=?' for k in f)} where id=?", (*f.values(), iid))
    return overview()


def _shift(ids, days):
    for r in db.query(f"select id, target_date from asa_items where id in ({','.join('?' * len(ids))})", ids):
        db.execute("update asa_items set target_date=? where id=?",
                   ((_d(r["target_date"]) + dt.timedelta(days=days)).isoformat(), r["id"]))


def spread(code):
    """把還沒完成的 Topic 平均排到「考前總複習」之前（之後可以再自己改）。"""
    e = db.query("select * from asa_exams where code=?", (code,))
    if not e or not e[0]["exam_date"]:
        raise ValueError("先設定考試日")
    e = e[0]
    pending = db.query("select id from asa_items where exam=? and done_at is null order by ord, id", (code,))
    if not pending:
        return overview()
    start = max(_today(), _d(e["start_date"]) or _today())
    end = _d(e["exam_date"]) - dt.timedelta(days=e["review_days"] or 0)
    span = max((end - start).days, len(pending))
    for k, r in enumerate(pending, 1):
        db.execute("update asa_items set target_date=? where id=?",
                   ((start + dt.timedelta(days=round(span * k / len(pending)))).isoformat(), r["id"]))
    return overview()


def delete_item(iid):
    db.execute("delete from asa_items where id=?", (iid,))
    return overview()
