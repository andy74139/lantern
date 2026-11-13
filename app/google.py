"""Google 帳號連結（OAuth「電腦版應用程式」＋PKCE），給待辦事項（Google Tasks）和行事曆用。

用戶端設定放在 config/google_client.json（從 Google Cloud 下載的 JSON），
授權後的權杖存在 data/google_token.json，兩個檔案都只有你自己讀得到。
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
from urllib.parse import urlencode

import httpx

from . import db

CLIENT_FILE = db.ROOT / "config" / "google_client.json"
TOKEN_FILE = db.DATA / "google_token.json"
REDIRECT = "http://127.0.0.1:8765/"
SCOPES = ["https://www.googleapis.com/auth/tasks",
          "https://www.googleapis.com/auth/calendar.readonly"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

_lock = threading.RLock()
_pending = {}          # state -> (code_verifier, 建立時間)


class Offline(Exception):
    """連不上 Google（沒有網路）。"""


class NeedAuth(Exception):
    """還沒連結，或授權已失效，需要重新連結。"""


def _write_private(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def client():
    try:
        d = json.loads(CLIENT_FILE.read_text())
    except Exception:
        return None
    d = d.get("installed") or d
    return d if d.get("client_id") and d.get("client_secret") else None


def save_client(data):
    if not isinstance(data, dict):
        raise ValueError("檔案格式不對")
    if "web" in data:
        raise ValueError("這是「網頁應用程式」類型的用戶端；請改建立「電腦版應用程式」類型，再下載 JSON。")
    d = data.get("installed")
    if not d or not d.get("client_id") or not d.get("client_secret"):
        raise ValueError("這不是 Google OAuth 用戶端的 JSON 檔（要在「憑證」頁下載的那個檔案）。")
    _write_private(CLIENT_FILE, data)


def _token():
    try:
        return json.loads(TOKEN_FILE.read_text())
    except Exception:
        return None


def status():
    t = _token() or {}
    granted = set((t.get("scope") or "").split())
    return {
        "configured": client() is not None,
        "connected": bool(t.get("refresh_token")) and not t.get("error"),
        "error": t.get("error"),
        "tasks": SCOPES[0] in granted,
        "calendar": SCOPES[1] in granted,
    }


def auth_url():
    c = client()
    if not c:
        raise ValueError("還沒有放 Google 用戶端設定檔。")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    with _lock:
        now = time.time()
        for k in [k for k, v in _pending.items() if now - v[1] > 900]:
            _pending.pop(k, None)
        _pending[state] = (verifier, now)
    return AUTH_URL + "?" + urlencode({
        "client_id": c["client_id"], "redirect_uri": REDIRECT, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })


def finish(state, code, error=None):
    with _lock:
        p = _pending.pop(state, None)
    if not p:
        raise ValueError("這個登入連結已經失效，請回到首頁重新按「連結 Google」。")
    if error:
        raise ValueError("你取消了授權。" if error == "access_denied" else f"Google 回報：{error}")
    c = client()
    try:
        r = httpx.post(TOKEN_URL, timeout=20, data={
            "code": code, "client_id": c["client_id"], "client_secret": c["client_secret"],
            "redirect_uri": REDIRECT, "grant_type": "authorization_code", "code_verifier": p[0]})
    except httpx.TransportError as e:
        raise ValueError(f"連不上 Google：{e}")
    if r.status_code != 200:
        raise ValueError("換取授權失敗：" + r.text[:300])
    t = r.json()
    if not t.get("refresh_token"):
        raise ValueError("Google 沒有給長期授權。請到 Google 帳戶的「第三方存取權」移除這個應用程式後再連結一次。")
    t["expires_at"] = time.time() + t.get("expires_in", 3600) - 60
    with _lock:
        _write_private(TOKEN_FILE, t)
    return status()


def disconnect():
    with _lock:
        t = _token()
        if t and t.get("refresh_token"):
            try:
                httpx.post(REVOKE_URL, data={"token": t["refresh_token"]}, timeout=10)
            except Exception:
                pass
        TOKEN_FILE.unlink(missing_ok=True)
    return status()


def _access_token(force=False):
    with _lock:
        t = _token()
        if not t or not t.get("refresh_token"):
            raise NeedAuth("尚未連結 Google。")
        if t.get("error"):
            raise NeedAuth(t["error"])
        if not force and t.get("access_token") and t.get("expires_at", 0) > time.time():
            return t["access_token"]
        c = client()
        if not c:
            raise NeedAuth("找不到 Google 用戶端設定檔。")
        try:
            r = httpx.post(TOKEN_URL, timeout=20, data={
                "client_id": c["client_id"], "client_secret": c["client_secret"],
                "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
        except httpx.TransportError as e:
            raise Offline(str(e) or type(e).__name__)
        if r.status_code in (400, 401):
            t["error"] = "Google 的授權已失效（可能過期或被撤銷），請重新連結。"
            _write_private(TOKEN_FILE, t)
            raise NeedAuth(t["error"])
        if r.status_code != 200:
            raise Offline(f"Google 暫時無法回應（{r.status_code}）")
        n = r.json()
        t.update(access_token=n["access_token"], expires_at=time.time() + n.get("expires_in", 3600) - 60)
        if n.get("scope"):
            t["scope"] = n["scope"]
        _write_private(TOKEN_FILE, t)
        return t["access_token"]


def request(method, url, **kw):
    """呼叫 Google API；權杖過期會自動更新。沒網路時丟 Offline。"""
    force = False
    for _ in range(2):
        tok = _access_token(force)
        try:
            r = httpx.request(method, url, headers={"Authorization": f"Bearer {tok}"}, timeout=20, **kw)
        except httpx.TransportError as e:
            raise Offline(str(e) or type(e).__name__)
        if r.status_code == 401:
            force = True
            continue
        if r.status_code >= 500:
            raise Offline(f"Google 暫時無法回應（{r.status_code}）")
        return r
    raise NeedAuth("Google 拒絕了授權，請重新連結。")
