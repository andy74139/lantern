"""本機 AI 控制台：只聽 127.0.0.1。"""
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from urllib.parse import quote

from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import claude_sessions, claude_usage, db, google, kb, home, music, ollama, recordings, system, tasks, todos

STATIC = db.ROOT / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}

app = FastAPI(title="本機 AI 控制台", docs_url=None, redoc_url=None, openapi_url=None)
db.init()
tasks.reconcile()
todos.init()
music.init()
recordings.start_scheduler()
home.start_refresher()
todos.start_sync()
claude_usage.start_poller()
kb.start()


@app.middleware("http")
async def guard(request: Request, call_next):
    # 只接受從本機網址進來的請求；會改變狀態的請求必須帶自訂標頭，
    # 這樣其他網站就算知道連接埠也無法從你的瀏覽器觸發工作。
    host = re.sub(r":\d+$", "", request.headers.get("host", ""))
    if host not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    if request.method not in ("GET", "HEAD") and request.headers.get("x-local-controller") != "1":
        return JSONResponse({"detail": "missing header"}, status_code=403)
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _fail(e):
    if isinstance(e, KeyError):
        raise HTTPException(404, "找不到")
    raise HTTPException(400, str(e))


# ---------- 狀態 ----------
@app.get("/api/system")
async def api_system():
    return await run_in_threadpool(system.status)


# ---------- Ollama ----------
class ModelReq(BaseModel):
    model: str
    keep_alive: str | None = None


class TestReq(BaseModel):
    model: str
    prompt: str
    think: bool | None = None      # 只有支援思考的模型才會帶這個欄位


@app.get("/api/ollama")
async def api_ollama():
    return await ollama.status()


@app.post("/api/ollama/start")
async def api_ollama_start():
    return await run_in_threadpool(ollama.start, tasks.run_env())


@app.post("/api/ollama/load")
async def api_ollama_load(req: ModelReq):
    return await ollama.load(req.model, req.keep_alive or "30m")


@app.post("/api/ollama/unload")
async def api_ollama_unload(req: ModelReq):
    return await ollama.unload(req.model)


@app.post("/api/ollama/delete")
async def api_ollama_delete(req: ModelReq):
    return await ollama.delete(req.model)


@app.post("/api/ollama/pull")
async def api_ollama_pull(req: ModelReq):
    return ollama.stream("/api/pull", {"model": req.model, "stream": True})


@app.post("/api/ollama/test")
async def api_ollama_test(req: TestReq):
    payload = {"model": req.model, "prompt": req.prompt, "stream": True}
    if req.think is not None:
        payload["think"] = req.think
    # 模型已經載入時沿用它目前的上下文長度，否則 Ollama 會為了這次測試重新載入模型，
    # 打斷正在用這個模型的工作（例如掃描）。
    st = await ollama.status()
    cur = next((m for m in st["loaded"] if m["name"] == req.model), None)
    if cur and cur.get("context_length"):
        payload["options"] = {"num_ctx": cur["context_length"]}
    return ollama.stream("/api/generate", payload)


# ---------- Claude ----------
class SessionReq(BaseModel):
    project: str
    id: str


@app.get("/api/claude/sessions")
async def api_claude_sessions():
    return await run_in_threadpool(claude_sessions.list_sessions)


@app.get("/api/version")
async def api_version():
    """前端用來判斷程式有沒有更新：static/ 與 app/ 檔案的最後修改時間。"""
    files = [*STATIC.glob("*"), *(db.ROOT / "app").glob("*.py")]
    return {"version": int(max(f.stat().st_mtime for f in files if f.is_file()))}


@app.get("/api/claude/usage")
async def api_claude_usage():
    claude_usage.mark_viewed()                 # 首頁開著才會定時查詢用量上限
    return await run_in_threadpool(claude_usage.summary)


@app.post("/api/claude/usage/refresh")
async def api_claude_usage_refresh():
    r = await run_in_threadpool(claude_usage.poll_limits)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "查詢失敗")
    return await run_in_threadpool(claude_usage.summary)


@app.get("/api/claude/sessions/{project}/{sid}")
async def api_claude_detail(project: str, sid: str):
    try:
        d = await run_in_threadpool(claude_sessions.detail, project, sid)
        try:
            d["command"] = claude_sessions.resume_command(project, sid)[1]
        except Exception:
            d["command"] = None
        return d
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "找不到這個對話")


@app.post("/api/claude/resume")
async def api_claude_resume(req: SessionReq):
    try:
        return await run_in_threadpool(claude_sessions.resume_in_terminal, req.project, req.id,
                                       tasks.run_env()["PATH"])
    except Exception as e:
        raise HTTPException(400, str(e))


# ---------- 常用工作 ----------
class ActionReq(BaseModel):
    inputs: dict[str, str] = {}


class PickReq(BaseModel):
    kind: str = "file"


@app.get("/api/tasks")
async def api_tasks():
    return await run_in_threadpool(tasks.list_tasks)


@app.get("/api/tasks/{task_id}/log")
async def api_task_log(task_id: str):
    try:
        return await run_in_threadpool(tasks.task_log, task_id)
    except Exception as e:
        _fail(e)


@app.post("/api/tasks/{task_id}/actions/{action_id}")
async def api_task_action(task_id: str, action_id: str, req: ActionReq):
    try:
        return await run_in_threadpool(tasks.run_action, task_id, action_id, req.inputs)
    except Exception as e:
        _fail(e)


@app.get("/api/runs")
async def api_runs(limit: int = 30):
    return await run_in_threadpool(tasks.list_runs, min(max(limit, 1), 200))


@app.get("/api/runs/{run_id}")
async def api_run(run_id: int):
    try:
        return await run_in_threadpool(tasks.get_run, run_id)
    except Exception as e:
        _fail(e)


@app.post("/api/pick")
async def api_pick(req: PickReq):
    try:
        return await run_in_threadpool(tasks.pick_path, req.kind)
    except Exception as e:
        raise HTTPException(400, str(e))


# ---------- 錄音整理 ----------
class RedoReq(BaseModel):
    start_from: str = "summarize"
    model: str | None = None
    hold: bool = False
    range: dict | None = None
    asr: str | None = None


class TranscriptEdit(BaseModel):
    segments: list[dict]


class SummaryEdit(BaseModel):
    summary: str
    title: str | None = None


@app.get("/api/recordings")
async def api_recordings():
    return await run_in_threadpool(recordings.list_all)


@app.post("/api/recordings")
async def api_recording_create(request: Request, name: str, model: str = "", language: str = "", hold: bool = False, asr: str = ""):
    try:
        return await recordings.create(name, request.stream(), model, language, hold, asr)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/recordings/{rid}")
async def api_recording(rid: str):
    try:
        return await run_in_threadpool(recordings.detail, rid)
    except KeyError:
        raise HTTPException(404, "找不到這筆錄音")


@app.get("/api/recordings/{rid}/file/{kind}")
async def api_recording_file(rid: str, kind: str):
    try:
        path, label = recordings.file_path(rid, kind)
    except KeyError:
        raise HTTPException(404, "找不到檔案")
    if kind == "audio":
        return FileResponse(path, media_type="audio/mp4")
    return FileResponse(path, filename=label, media_type="text/plain; charset=utf-8")


@app.post("/api/recordings/{rid}/edit/transcript")
async def api_recording_edit_transcript(rid: str, req: TranscriptEdit):
    try:
        return await run_in_threadpool(recordings.save_transcript, rid, req.segments)
    except Exception as e:
        _fail(e)


@app.post("/api/recordings/{rid}/edit/summary")
async def api_recording_edit_summary(rid: str, req: SummaryEdit):
    try:
        return await run_in_threadpool(recordings.save_summary, rid, req.summary, req.title)
    except Exception as e:
        _fail(e)


@app.post("/api/recordings/{rid}/{op}")
async def api_recording_op(rid: str, op: str, req: RedoReq):
    try:
        if op == "redo":
            return await run_in_threadpool(recordings.redo, rid, req.start_from, req.model, req.hold, req.range, req.asr)
        if op == "start-now":
            return await run_in_threadpool(recordings.start_now, rid)
        if op == "cancel":
            return await run_in_threadpool(recordings.cancel, rid)
        if op == "delete":
            return await run_in_threadpool(recordings.delete, rid)
        if op == "todos":
            return await run_in_threadpool(recording_todos, rid)
    except Exception as e:
        _fail(e)
    raise HTTPException(404, "找不到")


def recording_todos(rid):
    """把錄音摘要「待辦事項」段落裡的項目加進待辦清單（同一場錄音的同一項不會重複加）。"""
    d = recordings.detail(rid)
    lines, inside = [], False
    for line in (d.get("summary") or "").splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().startswith("待辦")
            continue
        m = re.match(r"^\s*[-*]\s*(\[[xX]\]\s*)?(?:\[ ?\]\s*)?(.+)$", line) if inside else None
        if m and not m.group(1) and m.group(2).strip() not in ("無", "无"):       # 已勾選的不加
            lines.append(re.sub(r"\s*\[\d{1,2}:\d{2}(?::\d{2})?\]", "", m.group(2)).strip())
    have = {r["title"] for r in db.query("select title from todos where deleted=0 and source like ?", (f'%"{rid}"%',))}
    title = d.get("title") or d.get("name")
    added = 0
    for t in lines:
        if t and t not in have:
            todos.create({"title": t, "notes": f"來自錄音「{title}」"},
                         {"type": "recording", "id": rid, "title": title})
            added += 1
    return {"added": added, "found": len(lines)}


# ---------- 知識庫 ----------
class KbAsk(BaseModel):
    q: str
    model: str | None = None


class KbFolder(BaseModel):
    path: str


class KbModel(BaseModel):
    model: str


@app.get("/api/kb")
async def api_kb():
    return {**(await run_in_threadpool(kb.status)), "history": await run_in_threadpool(kb.history)}


@app.post("/api/kb/ask")
async def api_kb_ask(req: KbAsk):
    try:
        return await run_in_threadpool(kb.ask, req.q, req.model)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/kb/reindex")
async def api_kb_reindex():
    kb.kick()
    return await run_in_threadpool(kb.status)


@app.post("/api/kb/folders/add")
async def api_kb_folder_add(req: KbFolder):
    try:
        return await run_in_threadpool(kb.add_folder, req.path)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/kb/folders/remove")
async def api_kb_folder_remove(req: KbFolder):
    return await run_in_threadpool(kb.remove_folder, req.path)


@app.post("/api/kb/model")
async def api_kb_model(req: KbModel):
    return await run_in_threadpool(kb.set_answer_model, req.model)


@app.post("/api/kb/open/{doc_id}")
async def api_kb_open(doc_id: int):
    try:
        return await run_in_threadpool(kb.open_doc, doc_id)
    except Exception as e:
        _fail(e)


# ---------- 首頁：天氣、行事曆 ----------
@app.get("/api/home")
async def api_home():
    cfg = home.config()
    return {"place": cfg["location"]["name"], "timezone": cfg["timezone"],
            "weather": await run_in_threadpool(home.weather),
            "calendar": await run_in_threadpool(home.calendar),
            "google": google.status()}


@app.post("/api/home/refresh")
async def api_home_refresh():
    await run_in_threadpool(home.refresh_weather, True)
    await run_in_threadpool(home.refresh_calendar, True)
    return await api_home()


# ---------- Google 連結 ----------
@app.get("/api/google")
async def api_google():
    return google.status()


@app.post("/api/google/client")
async def api_google_client(request: Request):
    try:
        await run_in_threadpool(google.save_client, await request.json())
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(400, "讀不懂這個檔案，請確認是從 Google Cloud 下載的 JSON。")
    return google.status()


@app.post("/api/google/connect")
async def api_google_connect():
    try:
        return {"url": google.auth_url()}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/google/disconnect")
async def api_google_disconnect():
    return await run_in_threadpool(google.disconnect)


# ---------- 待辦事項 ----------
class TodoReq(BaseModel):
    title: str | None = None
    notes: str | None = None
    due_date: str | None = None
    due_time: str | None = None
    status: str | None = None
    pinned: bool | None = None
    repeat: str | None = None
    list_id: str | None = None
    sort: float | None = None


@app.get("/api/todos")
async def api_todos():
    return await run_in_threadpool(todos.list_all)


@app.post("/api/todos")
async def api_todo_create(req: TodoReq):
    try:
        return await run_in_threadpool(todos.create, req.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/todos/sync")
async def api_todo_sync():
    return await run_in_threadpool(todos.sync)


class ListReq(BaseModel):
    title: str | None = None


@app.post("/api/todos/lists")
async def api_list_create(req: ListReq):
    try:
        return await run_in_threadpool(todos.create_list, req.title)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/todos/lists/{lid}/rename")
async def api_list_rename(lid: str, req: ListReq):
    try:
        return await run_in_threadpool(todos.rename_list, lid, req.title)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/todos/lists/{lid}/delete")
async def api_list_delete(lid: str):
    try:
        return await run_in_threadpool(todos.delete_list, lid)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/todos/{tid}")
async def api_todo_update(tid: str, req: TodoReq):
    try:
        return await run_in_threadpool(todos.update, tid, req.model_dump(exclude_unset=True))
    except Exception as e:
        _fail(e)


@app.post("/api/todos/{tid}/delete")
async def api_todo_delete(tid: str):
    try:
        return await run_in_threadpool(todos.delete, tid)
    except Exception as e:
        _fail(e)


@app.post("/api/todos/{tid}/restore")
async def api_todo_restore(tid: str):
    try:
        return await run_in_threadpool(todos.restore, tid)
    except Exception as e:
        _fail(e)


# ---------- 音樂 ----------
class MusicReq(BaseModel):
    url: str | None = None
    title: str | None = None
    pinned: bool | None = None


@app.get("/api/music")
async def api_music():
    return await run_in_threadpool(music.list_all)


@app.post("/api/music")
async def api_music_add(req: MusicReq):
    try:
        return await run_in_threadpool(music.add, req.url or "")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/music/{mid}/{op}")
async def api_music_op(mid: str, op: str, req: MusicReq):
    try:
        if op == "played":
            return await run_in_threadpool(music.played, mid, req.title)
        if op == "update":
            return await run_in_threadpool(music.update, mid, req.pinned, req.title)
        if op == "delete":
            return await run_in_threadpool(music.delete, mid)
    except Exception as e:
        _fail(e)
    raise HTTPException(404, "找不到")


# ---------- 前端 ----------
@app.get("/")
async def index(request: Request, state: str | None = None, code: str | None = None, error: str | None = None):
    home_url = f"http://localhost:{request.url.port or 8765}/"
    if state:                              # Google 授權完成後會帶著 code 回到這裡
        try:
            await run_in_threadpool(google.finish, state, code, error)
            msg = "ok"
            await run_in_threadpool(todos.kick)
            await run_in_threadpool(home.refresh_calendar, True)
        except Exception as e:
            msg = str(e)
        return RedirectResponse(home_url + "?google=" + quote(msg), status_code=303)
    if request.url.hostname == "127.0.0.1":
        # YouTube 拒絕在 IP 網址的頁面內嵌播放（錯誤 150），所以首頁一律用 localhost 開
        return RedirectResponse(home_url, status_code=307)
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
