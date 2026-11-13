"""首頁資料：設定、天氣、行事曆。都會快取在 data/cache，離線時顯示上次的資料。"""
import datetime as dt
import json
import threading
import time
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from . import db, google

CONFIG = db.ROOT / "config" / "home.json"
CACHE = db.DATA / "cache"
CACHE.mkdir(exist_ok=True)
WEATHER_EVERY = 30 * 60
CALENDAR_EVERY = 10 * 60
DAYS = 8                                  # 今天＋接下來 7 天

DEFAULTS = {
    "location": {"name": "台北市", "lat": 25.0375, "lon": 121.5637},
    "timezone": "Asia/Taipei",
    "calendars": [],
    "open_on_login": True,
}


def config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG.read_text()))
    except FileNotFoundError:
        pass
    return cfg


def tz():
    return ZoneInfo(config()["timezone"])


def _load(name):
    try:
        return json.loads((CACHE / name).read_text())
    except Exception:
        return None


def _save(name, data):
    tmp = CACHE / (name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(CACHE / name)


# ---------------- 天氣（Open-Meteo，免金鑰） ----------------
_wlock = threading.Lock()


def refresh_weather(force=False):
    with _wlock:
        c = _load("weather.json")
        if c and not force and time.time() - c.get("fetched_at", 0) < WEATHER_EVERY and len(c.get("days", [])) >= 7:
            return c
        cfg = config()
        loc = cfg["location"]
        try:
            r = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=15, params={
                "latitude": loc["lat"], "longitude": loc["lon"], "timezone": cfg["timezone"], "forecast_days": 7,
                "current": "temperature_2m,apparent_temperature,weather_code,relative_humidity_2m",
                "hourly": "precipitation_probability",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max",
            })
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            if c:
                c["error"] = f"無法更新：{e}"
                return c
            return {"error": f"無法取得天氣：{e}", "place": loc["name"]}
        cur, day, hr = d.get("current", {}), d.get("daily", {}), d.get("hourly", {})
        days = []
        for i, date in enumerate(day.get("time", [])):
            # 白天（8–20 點）每小時降雨機率的最大值，比全天最大值更貼近出門時的感受
            hours = [p for t, p in zip(hr.get("time", []), hr.get("precipitation_probability", []))
                     if t.startswith(date) and 8 <= int(t[11:13]) <= 20 and p is not None]
            days.append({"date": date, "code": day["weather_code"][i],
                         "max": day["temperature_2m_max"][i], "min": day["temperature_2m_min"][i],
                         "rain": day["precipitation_probability_max"][i],
                         "rain_day": max(hours) if hours else None,
                         "uv": (day.get("uv_index_max") or [None] * (i + 1))[i]})
        c = {"place": loc["name"], "fetched_at": time.time(), "error": None,
             "current": {"temp": cur.get("temperature_2m"), "feels": cur.get("apparent_temperature"),
                         "code": cur.get("weather_code"), "humidity": cur.get("relative_humidity_2m")},
             "days": days}
        _save("weather.json", c)
        return c


# ---------------- 行事曆 ----------------
_clock = threading.Lock()


def _window():
    z = tz()
    start = dt.datetime.now(z).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + dt.timedelta(days=DAYS)


def _event(cal, title, start, end, all_day, location=None, link=None, uid=None):
    return {"title": title or "（沒有標題）", "start": start, "end": end, "all_day": all_day,
            "location": location or None, "link": link, "cal": cal["name"], "role": cal.get("role", "main"),
            "uid": uid}


def _from_google(cal, start, end):
    url = f"https://www.googleapis.com/calendar/v3/calendars/{quote(cal['google_id'], safe='')}/events"
    out, token = [], None
    while True:
        params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                  "orderBy": "startTime", "maxResults": 250, "timeZone": config()["timezone"]}
        if token:
            params["pageToken"] = token
        r = google.request("GET", url, params=params)
        if r.status_code == 403:
            raise google.NeedAuth("連結 Google 時沒有勾選「查看日曆」的權限，請重新連結並勾選。")
        if r.status_code == 404:
            raise RuntimeError("找不到這個日曆（確認 config/home.json 裡的日曆 ID）。")
        r.raise_for_status()
        d = r.json()
        for ev in d.get("items", []):
            if ev.get("status") == "cancelled":
                continue
            me = next((a for a in ev.get("attendees", []) if a.get("self")), None)
            if me and me.get("responseStatus") == "declined":
                continue
            s, e = ev.get("start", {}), ev.get("end", {})
            all_day = "date" in s
            out.append(_event(cal, ev.get("summary"), s.get("dateTime") or s.get("date"),
                              e.get("dateTime") or e.get("date"), all_day, ev.get("location"),
                              ev.get("htmlLink"), ev.get("id")))
        token = d.get("nextPageToken")
        if not token:
            return out


def _from_ics(cal, start, end):
    import icalendar
    import recurring_ical_events
    import x_wr_timezone
    try:
        r = httpx.get(cal["ics_url"], timeout=20, follow_redirects=True)
    except httpx.TransportError as e:
        raise google.Offline(str(e))
    if r.status_code == 404:
        raise RuntimeError("網址讀不到。Google 日曆的「公開網址」只有在日曆設為公開時才能用，請改用「私人網址」或連結 Google。")
    r.raise_for_status()
    ical = x_wr_timezone.to_standard(icalendar.Calendar.from_ical(r.content))
    z = tz()
    out = []
    for ev in recurring_ical_events.of(ical).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        s = ev.decoded("DTSTART")
        e = ev.decoded("DTEND") if ev.get("DTEND") else None
        if isinstance(s, dt.datetime):
            s = s if s.tzinfo else s.replace(tzinfo=z)
            e = e if isinstance(e, dt.datetime) else s + (ev.decoded("DURATION") if ev.get("DURATION") else dt.timedelta(hours=1))
            e = e if e.tzinfo else e.replace(tzinfo=z)
            out.append(_event(cal, str(ev.get("SUMMARY", "")), s.astimezone(z).isoformat(), e.astimezone(z).isoformat(),
                              False, str(ev.get("LOCATION", "")) or None, None, str(ev.get("UID", ""))))
        else:
            e = e if isinstance(e, dt.date) else s + dt.timedelta(days=1)
            out.append(_event(cal, str(ev.get("SUMMARY", "")), s.isoformat(), e.isoformat(), True,
                              str(ev.get("LOCATION", "")) or None, None, str(ev.get("UID", ""))))
    return out


def refresh_calendar(force=False):
    with _clock:
        c = _load("calendar.json") or {}
        if c and not force and time.time() - c.get("fetched_at", 0) < CALENDAR_EVERY:
            return c
        start, end = _window()
        old = {x["name"]: x for x in c.get("calendars", [])}
        old_events = c.get("events", [])
        events, cals = [], []
        for cal in config()["calendars"]:
            st = {"name": cal["name"], "role": cal.get("role", "main"), "state": "ok", "error": None,
                  "fetched_at": time.time()}
            try:
                if cal.get("ics_url"):
                    evs = _from_ics(cal, start, end)
                elif cal.get("google_id"):
                    evs = _from_google(cal, start, end)
                else:
                    raise RuntimeError("沒有設定日曆來源。")
                events += evs
            except Exception as e:
                st["state"] = ("need_auth" if isinstance(e, google.NeedAuth) else
                               "offline" if isinstance(e, google.Offline) else "error")
                st["error"] = str(e)
                st["fetched_at"] = (old.get(cal["name"]) or {}).get("fetched_at")
                events += [x for x in old_events if x.get("cal") == cal["name"]]      # 沿用上次成功的資料
            cals.append(st)
        events.sort(key=lambda x: (x["start"][:10], not x["all_day"], x["start"]))
        c = {"fetched_at": time.time(), "events": events, "calendars": cals}
        _save("calendar.json", c)
        return c


def calendar():
    c = _load("calendar.json") or refresh_calendar(force=True)
    start, end = _window()
    lo, hi = start.date().isoformat(), end.date().isoformat()
    c = dict(c)
    c["events"] = [e for e in c.get("events", []) if e["start"][:10] < hi and (e["end"] or e["start"])[:10] >= lo]
    return c


def weather():
    return _load("weather.json") or refresh_weather(force=True)


def _loop():
    while True:
        for fn in (refresh_weather, refresh_calendar):
            try:
                fn()
            except Exception as e:
                print("home refresh:", e, flush=True)
        time.sleep(60)


def start_refresher():
    threading.Thread(target=_loop, daemon=True).start()
