#!/usr/bin/env python3
"""Claude Code 的狀態列指令：把 5 小時／每週用量上限的百分比存給首頁用。

Claude Code 每次更新狀態列時，會把工作階段的資料（JSON）從 stdin 傳進來；
其中 rate_limits 只有 Pro／Max 訂閱才有，而且要在該工作階段收到第一次回覆之後才出現。
這裡只存百分比和重置時間（data/claude_limits.json），不讀任何登入資訊。
印出來的那一行會顯示在 Claude Code 底部。
"""
import json
import os
import sys
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent / "data" / "claude_limits.json"
WINDOWS = ("five_hour", "seven_day")


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        d = {}
    rl = d.get("rate_limits") or {}
    now = time.time()
    try:   # 診斷用：記下最近一次被呼叫的時間、Claude Code 版本、收到哪些欄位（只有欄位名稱，沒有內容）
        seen = OUT.with_name("claude_statusline_seen.json")
        tmp = seen.with_name(f".seen.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"at": now, "version": d.get("version"), "keys": sorted(d.keys()),
                                   "has_rate_limits": bool(rl)}))
        os.replace(tmp, seen)
    except OSError:
        pass
    if any(rl.get(k) for k in WINDOWS):
        try:
            rec = json.loads(OUT.read_text())
        except Exception:
            rec = {}
        for k in WINDOWS:
            w = rl.get(k)
            if isinstance(w, dict) and w.get("used_percentage") is not None:
                rec[k] = {"used_percentage": float(w["used_percentage"]), "resets_at": w.get("resets_at"), "at": now}
        rec["at"] = now
        try:
            OUT.parent.mkdir(exist_ok=True)
            tmp = OUT.with_name(f".claude_limits.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(rec))
            os.replace(tmp, OUT)
        except OSError:
            pass

    parts = []
    model = (d.get("model") or {}).get("display_name")
    if model:
        parts.append(model)
    for k, label in (("five_hour", "5h"), ("seven_day", "週")):
        w = rl.get(k)
        if isinstance(w, dict) and w.get("used_percentage") is not None:
            parts.append(f"{label} {float(w['used_percentage']):.0f}%")
    ctx = (d.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"context {float(ctx):.0f}%")
    print(" · ".join(parts))


if __name__ == "__main__":
    main()
