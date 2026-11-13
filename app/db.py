"""執行紀錄（SQLite）。"""
import sqlite3
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
(DATA / "runs").mkdir(exist_ok=True)
DB_PATH = DATA / "controller.db"
_lock = threading.Lock()

SCHEMA = """
create table if not exists runs (
  id integer primary key autoincrement,
  task_id text, action_id text, task_name text, action_label text,
  cmd text, inputs text, is_long integer default 0,
  started_at real, ended_at real, exit_code integer,
  pid integer, log_path text, output text
);
create index if not exists runs_started on runs(started_at desc);
"""


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.executescript(SCHEMA)


def execute(sql, args=()):
    with _lock, _conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def query(sql, args=()):
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
