"""知識庫：把錄音（逐字稿、摘要）和指定資料夾裡的文件切成段落、建立索引，問問題時找出相關段落再請模型回答並附出處。

- 索引存在 data/kb.db，只在這台電腦。
- 搜尋同時用「語意」（Ollama 的向量模型）與「關鍵字」（SQLite FTS5 trigram，適合人名、術語），再合併排序。
- 回答可以用 Claude（品質好，相關段落會傳給 Anthropic）或本機的 Ollama 模型（完全不離開這台電腦）。
"""
import datetime as dt
import html as _html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import httpx
import numpy as np

from . import db, ollama, tasks

CONFIG = db.ROOT / "config" / "kb.json"
DB_PATH = db.DATA / "kb.db"
REC_DIR = db.DATA / "recordings"
DEFAULTS = {
    "folders": [],
    "embed_model": "embeddinggemma",
    "answer_model": "claude",
    "local_model": "gemma4:12b",
    "claude_model": "sonnet",
    "extensions": [".md", ".txt", ".pdf", ".docx", ".pptx", ".html", ".htm", ".csv"],
    "max_file_mb": 30,
    "reindex_every": 600,
}
CHUNK = 500          # 每段大約幾個字
OVERLAP = 80         # 相鄰段落重疊的字數，避免一句話被切斷後找不到
TOP_K = 8
# 向量模型要求的提示格式：(問題前綴, 段落前綴)。沒列到的模型不加前綴。
EMBED_PROMPTS = {
    "embeddinggemma": ("task: search result | query: ", "title: none | text: "),
    "qwen3-embedding": ("Instruct: Given a question, retrieve passages that help answer it\nQuery: ", ""),
}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".idea", ".Trash", "Library"}

_lock = threading.RLock()
_kick = threading.Event()
_matrix = None            # (ids, 向量矩陣) 快取；索引變動時清掉
_status = {"state": "idle", "done": 0, "total": 0, "current": None, "last_run": None, "error": None}


# ---------------- 設定與資料庫 ----------------
def config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        cfg.update(json.loads(CONFIG.read_text()))
    except FileNotFoundError:
        pass
    return cfg


def save_config(cfg):
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: cfg[k] for k in cfg if k in DEFAULTS}, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(CONFIG)


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.executescript("""
        create table if not exists docs (
          id integer primary key, source text, ref text unique, title text, path text,
          mtime real, indexed_at real, chunks integer default 0, error text);
        create table if not exists chunks (
          id integer primary key, doc_id integer, ord integer, text text, loc text, emb blob);
        create index if not exists chunks_doc on chunks(doc_id);
        create virtual table if not exists chunks_fts using fts5(text, content='chunks', content_rowid='id', tokenize='trigram');
        create table if not exists qa (id integer primary key, q text, answer text, sources text, model text, at real);
        create table if not exists meta (k text primary key, v text);
        """)
        # 舊版的索引沒有記錄向量模型：有資料的話就是當時的預設 qwen3-embedding:0.6b
        if not c.execute("select 1 from meta where k='embed_model'").fetchone() \
                and c.execute("select 1 from chunks where emb is not null limit 1").fetchone():
            c.execute("insert into meta values ('embed_model', 'qwen3-embedding:0.6b')")


def _index_model():
    """目前索引裡的向量是哪個模型產生的（None＝還沒有向量）。"""
    with _conn() as c:
        r = c.execute("select v from meta where k='embed_model'").fetchone()
    return r["v"] if r else None


# ---------------- 取出文字 ----------------
def _clean(t):
    t = t.replace(" ", " ").replace("\r", "")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _pdf_pages(path):
    exe = shutil.which("pdftotext", path=tasks.run_env()["PATH"])
    if exe:
        r = subprocess.run([exe, "-enc", "UTF-8", str(path), "-"], capture_output=True, timeout=180)
        if r.returncode == 0:
            return [_clean(p.decode("utf-8", "replace")) for p in r.stdout.split(b"\f")]
    from pypdf import PdfReader
    return [_clean(p.extract_text() or "") for p in PdfReader(str(path)).pages]


def _xml_text(xml, para_tag, text_tag):
    out = []
    for para in re.findall(rf"<{para_tag}[ >].*?</{para_tag}>", xml, flags=re.S):
        s = "".join(re.findall(rf"<{text_tag}(?: [^>]*)?>([^<]*)</{text_tag}>", para))
        if s.strip():
            out.append(_html.unescape(s))
    return "\n".join(out)


def extract(path):
    """回傳 [(位置, 文字)]：位置是 {"page": n}、{"slide": n} 或 {}。"""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return [({"page": i + 1}, t) for i, t in enumerate(_pdf_pages(path)) if t]
    if ext == ".docx":
        with zipfile.ZipFile(path) as z:
            return [({}, _clean(_xml_text(z.read("word/document.xml").decode("utf-8", "replace"), "w:p", "w:t")))]
    if ext == ".pptx":
        with zipfile.ZipFile(path) as z:
            names = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                           key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
            return [({"slide": i + 1}, _clean(_xml_text(z.read(n).decode("utf-8", "replace"), "a:p", "a:t")))
                    for i, n in enumerate(names)]
    raw = path.read_text(errors="replace")
    if ext in (".html", ".htm"):
        raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
        raw = _html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return [({}, _clean(raw))]


def chunk_text(text, loc):
    """依段落切成約 CHUNK 字的片段，片段之間重疊 OVERLAP 字。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    out, cur = [], ""
    for p in paras:
        while len(p) > CHUNK * 1.5:                     # 很長的一段先硬切
            head, p = p[:CHUNK], p[CHUNK - OVERLAP:]
            if cur:
                out.append(cur)
                cur = ""
            out.append(head)
        if cur and len(cur) + len(p) > CHUNK:
            out.append(cur)
            cur = cur[-OVERLAP:] + "\n" + p
        else:
            cur = (cur + "\n" + p) if cur else p
    if cur.strip():
        out.append(cur)
    return [(loc, c) for c in out]


def recording_chunks(folder):
    """錄音：逐字稿依時間切段（每段記住開始時間），摘要另外一段。"""
    meta = json.loads((folder / "meta.json").read_text())
    out = []
    if (folder / "summary.md").exists():
        s = (folder / "summary.md").read_text()
        out += chunk_text(s, {"part": "summary"})
    if (folder / "transcript.json").exists():
        cur, start = "", None
        for seg in json.loads((folder / "transcript.json").read_text()):
            if start is None:
                start = seg["start"]
            cur += seg["text"] + ("" if re.search(r"[一-鿿]$", seg["text"]) else " ")
            if len(cur) >= CHUNK:
                out.append(({"t": round(start, 1)}, cur.strip()))
                cur, start = "", None
        if cur.strip():
            out.append(({"t": round(start or 0, 1)}, cur.strip()))
    return meta.get("title") or meta.get("name") or folder.name, out


# ---------------- 向量 ----------------
def _embed_prompts(model):
    return EMBED_PROMPTS.get(model.split(":")[0], ("", ""))


def embed(texts, model, query=False):
    prefix = _embed_prompts(model)[0 if query else 1]
    texts = [prefix + t for t in texts]
    out = []
    for i in range(0, len(texts), 32):
        try:
            r = httpx.post(ollama.BASE + "/api/embed", timeout=300,
                           json={"model": model, "input": texts[i:i + 32], "keep_alive": "10m"})
        except httpx.TransportError as e:
            raise RuntimeError(f"連不上 Ollama：{e}")
        if r.status_code != 200:
            msg = r.text[:200]
            if "not found" in msg:
                msg = f"找不到向量模型 {model}，請先執行：ollama pull {model}"
            raise RuntimeError(msg)
        out += r.json()["embeddings"]
    a = np.asarray(out, dtype=np.float32)
    return a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-8)


# ---------------- 建立索引 ----------------
def _sources():
    """目前應該收錄的所有來源：{ref: (source, 路徑, mtime)}"""
    cfg = config()
    found = {}
    if REC_DIR.is_dir():
        for d in REC_DIR.iterdir():
            files = [d / "transcript.json", d / "summary.md"]
            if (d / "meta.json").exists() and any(f.exists() for f in files):
                found["rec:" + d.name] = ("recording", d, max(f.stat().st_mtime for f in files if f.exists()))
    exts, limit = set(cfg["extensions"]), cfg["max_file_mb"] * 1024 * 1024
    for folder in cfg["folders"]:
        root = Path(folder).expanduser()
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS and not x.startswith(".")]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() in exts and not fn.startswith((".", "~$")):
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    if st.st_size <= limit:
                        found["file:" + str(p)] = ("file", p, st.st_mtime)
    return found


def _store(c, ref, source, path, title, mtime, pieces, vecs, error=None):
    old = c.execute("select id from docs where ref=?", (ref,)).fetchone()
    if old:
        for r in c.execute("select id, text from chunks where doc_id=?", (old["id"],)).fetchall():
            c.execute("insert into chunks_fts(chunks_fts, rowid, text) values('delete', ?, ?)", (r["id"], r["text"]))
        c.execute("delete from chunks where doc_id=?", (old["id"],))
        c.execute("update docs set title=?, path=?, mtime=?, indexed_at=?, chunks=?, error=? where id=?",
                  (title, str(path), mtime, time.time(), len(pieces), error, old["id"]))
        doc_id = old["id"]
    else:
        doc_id = c.execute("insert into docs(source, ref, title, path, mtime, indexed_at, chunks, error) values(?,?,?,?,?,?,?,?)",
                           (source, ref, title, str(path), mtime, time.time(), len(pieces), error)).lastrowid
    for i, ((loc, text), v) in enumerate(zip(pieces, vecs)):
        cid = c.execute("insert into chunks(doc_id, ord, text, loc, emb) values(?,?,?,?,?)",
                        (doc_id, i, text, json.dumps(loc), v.astype(np.float32).tobytes())).lastrowid
        c.execute("insert into chunks_fts(rowid, text) values(?, ?)", (cid, text))


def _remove(c, ref):
    old = c.execute("select id from docs where ref=?", (ref,)).fetchone()
    if not old:
        return
    for r in c.execute("select id, text from chunks where doc_id=?", (old["id"],)).fetchall():
        c.execute("insert into chunks_fts(chunks_fts, rowid, text) values('delete', ?, ?)", (r["id"], r["text"]))
    c.execute("delete from chunks where doc_id=?", (old["id"],))
    c.execute("delete from docs where id=?", (old["id"],))


def reindex(force=False):
    """比對來源與索引：新增或改過的重新建立，已經不在的移除。"""
    global _matrix
    cfg = config()
    with _lock:
        if _status["state"] == "indexing":
            return status()
        _status.update(state="indexing", done=0, total=0, current=None, error=None)
    try:
        found = _sources()
        if _index_model() not in (None, cfg["embed_model"]):
            force = True                                # 換了向量模型：舊向量不能混用，全部重建
            _status.update(current="換向量模型，全部重建")
        with _conn() as c:
            have = {r["ref"]: r["mtime"] for r in c.execute("select ref, mtime from docs")}
        todo = [(ref, v) for ref, v in found.items() if force or have.get(ref) != v[2]]
        gone = [ref for ref in have if ref not in found]
        _status.update(total=len(todo))
        changed = bool(gone)
        with _conn() as c:
            for ref in gone:
                _remove(c, ref)
        for i, (ref, (source, path, mtime)) in enumerate(sorted(todo, key=lambda x: x[0])):
            _status.update(done=i, current=Path(path).name if source == "file" else ref)
            error = None
            try:
                if source == "recording":
                    title, pieces = recording_chunks(path)
                else:
                    title = path.name
                    pieces = [p for loc, t in extract(path) for p in chunk_text(t, loc)]
                pieces = [(loc, t) for loc, t in pieces if len(t.strip()) >= 5][:4000]
                vecs = embed([t for _, t in pieces], cfg["embed_model"]) if pieces else []
            except RuntimeError as e:
                if "Ollama" in str(e) or "向量模型" in str(e):
                    raise                               # 向量模型不能用：整批停下來
                title, pieces, vecs, error = Path(path).name, [], [], str(e)[:300]
            except Exception as e:                      # 單一檔案讀不了（加密的 PDF、壞掉的檔案）就略過
                title, pieces, vecs, error = Path(path).name, [], [], f"{type(e).__name__}: {e}"[:300]
            with _lock, _conn() as c:
                _store(c, ref, source, path, title, mtime, pieces, vecs, error)
            changed = True
        if force or changed:
            with _lock, _conn() as c:
                c.execute("insert or replace into meta values ('embed_model', ?)", (cfg["embed_model"],))
        if changed:
            _matrix = None
        _status.update(state="idle", done=len(todo), current=None, last_run=time.time())
    except Exception as e:
        _status.update(state="error", current=None, error=str(e) or type(e).__name__, last_run=time.time())
    return status()


def status():
    with _conn() as c:
        docs = {r["source"]: r["n"] for r in c.execute("select source, count(*) n from docs group by source")}
        chunks = c.execute("select count(*) from chunks").fetchone()[0]
        errors = [dict(r) for r in c.execute("select title, path, error from docs where error is not null limit 20")]
    cfg = config()
    return {**_status, "recordings": docs.get("recording", 0), "files": docs.get("file", 0), "chunks": chunks,
            "errors": errors, "folders": cfg["folders"], "answer_model": cfg["answer_model"],
            "embed_model": cfg["embed_model"], "index_model": _index_model()}


# ---------------- 搜尋 ----------------
def _load_matrix():
    global _matrix
    if _matrix is None:
        with _conn() as c:
            rows = c.execute("select id, emb from chunks where emb is not null").fetchall()
        if rows:                                  # 重建到一半時可能混著兩種長度的向量：只留最多的那種
            sizes = [len(r["emb"]) for r in rows]
            size = max(set(sizes), key=sizes.count)
            rows = [r for r in rows if len(r["emb"]) == size]
        ids = np.array([r["id"] for r in rows], dtype=np.int64)
        mat = np.frombuffer(b"".join(r["emb"] for r in rows), dtype=np.float32).reshape(len(rows), -1) if rows else np.zeros((0, 1), np.float32)
        _matrix = (ids, mat)
    return _matrix


def _fts_query(q):
    """把問題拆成關鍵字：英文單字、數字，以及三個字以上的中文片段（trigram 需要至少三個字）。"""
    terms = set()
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]{2,}", q):
        terms.add(w)
    for run in re.findall(r"[一-鿿]{3,}", q):
        for i in range(0, max(1, len(run) - 2), 2):      # 長的中文句子取幾個三字片段
            terms.add(run[i:i + 3])
    return " OR ".join('"' + t.replace('"', "") + '"' for t in list(terms)[:24])


def search(q, k=TOP_K):
    cfg = config()
    ranks = {}
    ids, mat = _load_matrix()
    if len(ids) and _index_model() == cfg["embed_model"]:     # 重建索引中（向量模型不一致）就先只用關鍵字
        qv = embed([q], cfg["embed_model"], query=True)[0]
        sims = mat @ qv
        for r, i in enumerate(np.argsort(-sims)[:30]):
            ranks.setdefault(int(ids[i]), []).append(r)
    fq = _fts_query(q)
    if fq:
        with _conn() as c:
            try:
                for r, row in enumerate(c.execute("select rowid from chunks_fts where chunks_fts match ? order by bm25(chunks_fts) limit 30", (fq,))):
                    ranks.setdefault(row["rowid"], []).append(r)
            except sqlite3.OperationalError:
                pass
    # 兩種排序合併（Reciprocal Rank Fusion）
    scored = sorted(ranks.items(), key=lambda kv: -sum(1 / (60 + r) for r in kv[1]))[:k]
    if not scored:
        return []
    with _conn() as c:
        rows = {r["id"]: r for r in c.execute(
            f"select chunks.id, chunks.text, chunks.loc, docs.id doc_id, docs.source, docs.ref, docs.title, docs.path "
            f"from chunks join docs on docs.id=chunks.doc_id where chunks.id in ({','.join('?' * len(scored))})",
            [cid for cid, _ in scored])}
    out = []
    for cid, _ in scored:
        r = rows.get(cid)
        if r:
            out.append({"chunk": cid, "doc_id": r["doc_id"], "source": r["source"], "ref": r["ref"], "title": r["title"],
                        "path": r["path"] if r["source"] == "file" else None, "loc": json.loads(r["loc"] or "{}"),
                        "text": r["text"]})
    return out


# ---------------- 回答 ----------------
SYSTEM = ("你是使用者個人知識庫的助理。只根據提供的資料回答；資料裡沒有的就直接說「資料裡沒有提到」，不要猜。"
          "每個說法後面用 [編號] 標出根據的資料，例如「退款流程要在週五前修好 [2]」。"
          "資料可能是語音辨識的逐字稿，會有錯字，請依上下文理解。一律使用繁體中文（台灣用語），簡潔、條理清楚。")


def _loc_text(s):
    loc = s["loc"]
    if "t" in loc:
        t = int(loc["t"])
        return f"錄音 {t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"錄音 {t // 60:02d}:{t % 60:02d}"
    if loc.get("part") == "summary":
        return "錄音摘要"
    if "page" in loc:
        return f"第 {loc['page']} 頁"
    if "slide" in loc:
        return f"第 {loc['slide']} 張投影片"
    return ""


def _prompt(q, sources):
    blocks = [f"[{i}] {s['title']}（{_loc_text(s) or '文件'}）\n{s['text']}" for i, s in enumerate(sources, 1)]
    return f"以下是從使用者的資料中找到、可能相關的 {len(sources)} 段內容：\n\n" + "\n\n---\n\n".join(blocks) + f"\n\n問題：{q}"


def _ask_claude(cfg, prompt):
    exe = shutil.which("claude", path=tasks.run_env()["PATH"]) or str(Path.home() / ".local" / "bin" / "claude")
    try:
        r = subprocess.run([exe, "-p", "--output-format", "text", "--tools", "", "--no-session-persistence",
                            "--strict-mcp-config", "--model", cfg["claude_model"], "--system-prompt", SYSTEM],
                           input=prompt, capture_output=True, text=True, timeout=300, cwd=str(db.DATA))
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude 超過 5 分鐘沒有回覆")
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError("Claude 回答失敗：" + (r.stderr or r.stdout or "").strip()[-300:])
    return r.stdout.strip()


def _ask_local(cfg, prompt):
    try:
        r = httpx.post(ollama.BASE + "/api/chat", timeout=600, json={
            "model": cfg["local_model"], "stream": False, "think": False,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "options": {"temperature": 0.2, "num_ctx": 8192}})
    except httpx.TransportError as e:
        raise RuntimeError(f"連不上 Ollama：{e}")
    if r.status_code != 200:
        raise RuntimeError(f"本機模型回答失敗：{r.text[:200]}")
    return ((r.json().get("message") or {}).get("content") or "").strip()


def ask(q, model=None):
    q = (q or "").strip()
    if not q:
        raise ValueError("請輸入問題")
    cfg = config()
    model = model or cfg["answer_model"]
    sources = search(q)
    if not sources:
        answer = "知識庫裡找不到相關的資料。" + ("（目前還沒有收錄任何資料）" if not status()["chunks"] else "")
    else:
        prompt = _prompt(q, sources)
        answer = _ask_claude(cfg, prompt) if model == "claude" else _ask_local(cfg, prompt)
    for s in sources:
        s["where"] = _loc_text(s)
        s["text"] = s["text"][:600]
    with _conn() as c:
        qid = c.execute("insert into qa(q, answer, sources, model, at) values(?,?,?,?,?)",
                        (q, answer, json.dumps(sources, ensure_ascii=False), model, time.time())).lastrowid
        c.execute("delete from qa where id not in (select id from qa order by id desc limit 50)")
    return {"id": qid, "q": q, "answer": answer, "sources": sources, "model": model}


def history(limit=10):
    with _conn() as c:
        rows = [dict(r) for r in c.execute("select * from qa order by id desc limit ?", (limit,))]
    for r in rows:
        r["sources"] = json.loads(r["sources"] or "[]")
    return rows


# ---------------- 資料夾與背景更新 ----------------
def add_folder(path):
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError("找不到這個資料夾")
    if p == Path.home() or len(p.parts) <= 2:
        raise ValueError("範圍太大了，請選比較小的資料夾（例如某個專案或文件資料夾）")
    cfg = config()
    if str(p) not in cfg["folders"]:
        cfg["folders"].append(str(p))
        save_config(cfg)
    _kick.set()
    return status()


def remove_folder(path):
    cfg = config()
    cfg["folders"] = [f for f in cfg["folders"] if f != path]
    save_config(cfg)
    _kick.set()
    return status()


def set_answer_model(model):
    cfg = config()
    cfg["answer_model"] = "claude" if model == "claude" else "local"
    save_config(cfg)
    return status()


def open_doc(doc_id):
    with _conn() as c:
        r = c.execute("select source, path from docs where id=?", (doc_id,)).fetchone()
    if not r or r["source"] != "file" or not Path(r["path"]).exists():
        raise KeyError(doc_id)
    subprocess.run(["open", r["path"]], timeout=10)       # 用預設的 App 開啟
    return {"ok": True}


def _loop():
    while True:
        _kick.wait(config()["reindex_every"])
        _kick.clear()
        try:
            reindex()
        except Exception as e:
            print("kb reindex:", e, flush=True)


def start():
    init()
    threading.Thread(target=_loop, daemon=True).start()
    _kick.set()


def kick():
    _kick.set()
