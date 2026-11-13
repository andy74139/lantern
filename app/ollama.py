"""Ollama 狀態與操作（透過本機 HTTP API）。"""
import asyncio
import json
import os
import subprocess

import httpx
from fastapi.responses import StreamingResponse

BASE = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")


async def _get(client, path):
    r = await client.get(BASE + path)
    r.raise_for_status()
    return r.json()


async def status():
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            ver, tags, ps = await asyncio.gather(
                _get(c, "/api/version"), _get(c, "/api/tags"), _get(c, "/api/ps"))
    except Exception as e:
        return {"up": False, "error": str(e) or type(e).__name__, "models": [], "loaded": []}
    models = [{
        "name": m["name"],
        "size": m.get("size", 0),
        "modified_at": m.get("modified_at"),
        "params": (m.get("details") or {}).get("parameter_size"),
        "quant": (m.get("details") or {}).get("quantization_level"),
        "capabilities": m.get("capabilities") or [],
    } for m in tags.get("models", [])]
    models.sort(key=lambda m: m["modified_at"] or "", reverse=True)
    loaded = [{
        "name": m["name"],
        "size": m.get("size", 0),
        "size_vram": m.get("size_vram", 0),
        "context_length": m.get("context_length"),
        "expires_at": m.get("expires_at"),
    } for m in ps.get("models", [])]
    return {"up": True, "version": ver.get("version"), "models": models, "loaded": loaded}


async def _generate(payload, timeout):
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(BASE + "/api/generate", json=payload)
        if r.status_code != 200:
            try:
                return {"ok": False, "error": r.json().get("error", r.text)}
            except Exception:
                return {"ok": False, "error": r.text}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e) or type(e).__name__}


async def load(model, keep_alive="30m"):
    return await _generate({"model": model, "keep_alive": keep_alive}, timeout=600)


async def unload(model):
    return await _generate({"model": model, "keep_alive": 0}, timeout=60)


async def delete(model):
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.request("DELETE", BASE + "/api/delete", json={"model": model})
        return {"ok": r.status_code == 200, "error": None if r.status_code == 200 else r.text}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def stream(path, payload):
    """把 Ollama 的逐行 JSON 串流原樣轉給瀏覽器。"""
    async def gen():
        try:
            async with httpx.AsyncClient(timeout=None) as c:
                async with c.stream("POST", BASE + path, json=payload) as r:
                    async for chunk in r.aiter_bytes():
                        yield chunk
        except Exception as e:
            yield (json.dumps({"error": str(e) or type(e).__name__}, ensure_ascii=False) + "\n").encode()
    return StreamingResponse(gen(), media_type="application/x-ndjson")


def start(env):
    """Ollama 沒在跑時，在背景啟動 ollama serve。"""
    subprocess.Popen(["ollama", "serve"], env=env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True}
