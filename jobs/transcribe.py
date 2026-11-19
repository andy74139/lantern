#!/usr/bin/env python3
"""錄音整理：轉檔（ffmpeg）→ 轉錄（whisper.cpp）→ 整理重點（Ollama）。全部在本機執行。

用法：transcribe.py <錄音資料夾>
資料夾裡要有 meta.json；進度與結果都寫回這個資料夾，控制台只負責顯示。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULTS = {
    "whisper_cli": "whisper-cli",
    "whisper_model": "models/ggml-large-v3-turbo.bin",
    "vad_model": "models/ggml-silero-v5.1.2.bin",
    "language": "zh",
    "traditional_chinese": True,
    "initial_prompt": "以下是繁體中文的錄音逐字稿，請使用正確的標點符號。",
    "glossary": [],
    "summary_model": "qwen2.5:7b",
    "num_ctx": 16384,
    "asr": "turbo",
    "asr_models": {},
}
OUTPUT_TOKENS = 1500        # 每次整理最多產生的 token 數
THINKING_TOKENS = 4000      # 會思考的模型額外保留的額度
PROMPT_OVERHEAD = 700       # 系統提示與格式說明大約佔的 token 數

SYSTEM = ("你是錄音與會議的整理助手。只根據逐字稿的內容整理，不要加入逐字稿沒有提到的資訊；"
          "不確定的地方要標示「（不確定）」。逐字稿由語音辨識產生，可能有同音錯字，請依上下文判斷正確的用詞。"
          "一律使用繁體中文（台灣用語）。"
          "整理時要用自己的話歸納：把同一件事的多句話合併成一點、去掉口語贅字與重複，不要一句一句照抄逐字稿。")

NOTES_PROMPT = """以下是錄音逐字稿的第 {i}/{n} 段，方括號裡是時間。
請條列這一段的重點，每點一行，開頭附上對應的時間，例如「- [12:34] ……」。用自己的話歸納，同一件事合併成一點，不要逐句照抄。
要涵蓋：討論的主題、提到的事實與數字、做出的決定、有人答應要做的事（誰、做什麼、期限）、還沒解決的問題。
不要寫前言或結語。

{text}"""

CONDENSE_PROMPT = """以下是一段錄音的重點筆記（第 {i}/{n} 部分）。請在不遺漏決定、待辦事項、數字的前提下濃縮成更短的條列，保留時間標記。
不要寫前言或結語。

{text}"""

FINAL_PROMPT = """以下是一段錄音的{kind}，方括號裡是時間。請整理成下面的格式，直接輸出 Markdown，不要加任何其他說明：

# （用一行寫出這段錄音的主題，20 字以內）

## 摘要
（用 3 到 5 句話、用自己的話說明這段錄音在談什麼、結論是什麼，不要照抄原句）

## 重點
- （3 到 8 點，每點是一個歸納過的重點，把相關的內容合併成一句完整的話，不要逐句照抄{kind}；可以在句尾附上它在{kind}裡的時間，寫成 [12:34] 這種方括號格式，只能照抄{kind}裡出現過的時間，不要自己推算）

## 決議
- （錄音裡說到「決定」「確定」「就這樣做」的事，連同開始適用的時間；真的沒有才只寫「無」）

## 待辦事項
- [ ] （每項一行，寫成「人名：要做的事（期限）」，例如「- [ ] 陳小姐：寄出合約（週三前）」；沒提到人名或期限就省略那一部分；沒有待辦就只寫「無」）

## 待確認
- （還沒有結論、需要再確認的問題；沒有就只寫「無」）

以下是{kind}：

{text}"""


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads((ROOT / "config" / "recordings.json").read_text()))
    except FileNotFoundError:
        pass
    for k in ("whisper_model", "vad_model"):
        if cfg.get(k) and not os.path.isabs(cfg[k]):
            cfg[k] = str(ROOT / cfg[k])
    for v in (cfg.get("asr_models") or {}).values():
        if v.get("model") and not os.path.isabs(v["model"]):
            v["model"] = str(ROOT / v["model"])
    return cfg


def asr_choice(cfg, name):
    """這筆錄音要用哪個轉錄模型：回傳 (名稱, 模型檔, 是否補標點)。找不到設定就用 whisper_model。"""
    models = cfg.get("asr_models") or {}
    name = name if name in models else cfg.get("asr") if cfg.get("asr") in models else None
    if not name:
        return "whisper", cfg["whisper_model"], False
    m = models[name]
    return name, m["model"], bool(m.get("add_punct"))


_CJK_SPACE = None


def add_punct(text):
    """Breeze 只用空格斷句、沒有標點：中文字之間的空格換成逗號，讀起來順一點（英文和數字旁的空格不動）。"""
    global _CJK_SPACE
    if _CJK_SPACE is None:
        _CJK_SPACE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")
    return _CJK_SPACE.sub("，", text.strip())


# ---------------- 詞彙表與修正字典 ----------------
# 存在 data/asr_vocab.json（個人資料，不進 git）：
#   terms：專有名詞，轉錄時提示給 whisper，整理摘要時也會告訴模型正確寫法
#   corrections：「辨識成 from → 其實是 to」。手動修改逐字稿時自動學到；
#                同一組改過 2 次才自動套用（status: pending → on），避免偶然的修改被當成規則
VOCAB = ROOT / "data" / "asr_vocab.json"
PROMPT_TERMS_MAX = 150        # whisper 的提示最多約 224 個 token；中文一個字約 1～2 個 token，超過的詞不放進提示
AUTO_ON_COUNT = 2
_MARKS = "，。！？、；：,.!?;:「」『』（）()\"'… \t"


def load_vocab():
    try:
        v = json.loads(VOCAB.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        v = {}
    return {"terms": list(v.get("terms") or []), "corrections": list(v.get("corrections") or [])}


def save_vocab(v):
    VOCAB.parent.mkdir(parents=True, exist_ok=True)
    tmp = VOCAB.with_suffix(".tmp")
    tmp.write_text(json.dumps({"terms": v["terms"], "corrections": v["corrections"]}, ensure_ascii=False, indent=1))
    tmp.replace(VOCAB)


def all_terms(cfg, vocab=None):
    vocab = vocab or load_vocab()
    seen, out = set(), []
    for t in list(cfg.get("glossary") or []) + vocab["terms"]:
        t = str(t).strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def prompt_terms(cfg, vocab=None):
    """放進 whisper 提示的詞（依清單順序，放到長度上限為止）。"""
    out, n = [], 0
    for t in all_terms(cfg, vocab):
        if n + len(t) + 1 > PROMPT_TERMS_MAX:
            continue
        out.append(t)
        n += len(t) + 1
    return out


def _ascii_word(ch):
    return ch.isascii() and ch.isalnum()


def _correction_re(frm):
    """英文開頭／結尾的要對齊單字邊界（避免把 AI 換進 MAIL 裡面）。"""
    pat = re.escape(frm)
    if _ascii_word(frm[0]):
        pat = r"(?<![A-Za-z0-9])" + pat
    if _ascii_word(frm[-1]):
        pat += r"(?![A-Za-z0-9])"
    return re.compile(pat)


def apply_corrections(text, vocab):
    """套用已啟用的修正，回傳 (文字, 換了幾處)。長的先換，避免短規則先把長的拆掉。"""
    n = 0
    for c in sorted((c for c in vocab["corrections"] if c.get("status") == "on" and c.get("from")),
                    key=lambda c: -len(c["from"])):
        text, k = _correction_re(c["from"]).subn(lambda m, to=c["to"]: to, text)
        n += k
    return text, n


def _diff_pairs(a, b):
    """比對一句的原文 a 與修改後 b，找出像「換了一個詞」的修改。整句改寫、只改標點的都不算。"""
    import difflib
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.ratio() < 0.5:
        return []
    ops = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if ops and i1 - ops[-1][1] <= 1:                      # 中間只隔一個字：當成同一處修改
            ops[-1] = (ops[-1][0], i2, ops[-1][2], j2)
        else:
            ops.append((i1, i2, j1, j2))
    pairs = []
    for i1, i2, j1, j2 in ops:
        # 往外擴到完整的英文單字
        while i1 > 0 and j1 > 0 and _ascii_word(a[i1 - 1]) and a[i1 - 1] == b[j1 - 1] and (
                (i1 < len(a) and _ascii_word(a[i1])) or (j1 < len(b) and _ascii_word(b[j1]))):
            i1, j1 = i1 - 1, j1 - 1
        while i2 < len(a) and j2 < len(b) and _ascii_word(a[i2]) and a[i2] == b[j2] and (
                (i2 > i1 and _ascii_word(a[i2 - 1])) or (j2 > j1 and _ascii_word(b[j2 - 1]))):
            i2, j2 = i2 + 1, j2 + 1
        # 中文至少要兩個字（只記一個字太容易誤換），優先帶上前一個字
        while i2 - i1 < 2 or j2 - j1 < 1:
            if i1 > 0 and j1 > 0 and a[i1 - 1] == b[j1 - 1] and a[i1 - 1] not in _MARKS:
                i1, j1 = i1 - 1, j1 - 1
            elif i2 < len(a) and j2 < len(b) and a[i2] == b[j2] and a[i2] not in _MARKS:
                i2, j2 = i2 + 1, j2 + 1
            else:
                break
        frm, to = a[i1:i2].strip(), b[j1:j2].strip()
        if len(frm) < 2 or not to or frm == to or len(frm) > 16 or len(to) > 16:
            continue
        if any(ch in _MARKS.strip() for ch in frm + to):     # 跨標點的修改不當成詞的修正
            continue
        pairs.append((frm, to))
    return pairs


def learn_corrections(old_segments, new_segments, source=None):
    """從手動修改學修正：比對同一句（時間相同）的前後文字。回傳這次學到／更新的修正。"""
    old = {(round(x["start"], 2), round(x["end"], 2)): x["text"] for x in old_segments}
    found = []
    for x in new_segments:
        a = old.get((round(x["start"], 2), round(x["end"], 2)))
        if a and a != x["text"]:
            found += _diff_pairs(a, x["text"])
    if not found:
        return []
    vocab = load_vocab()
    by_from = {c["from"]: c for c in vocab["corrections"]}
    changed, now = [], time.time()
    for frm, to in found:
        back = by_from.get(to)
        if back and back["to"] == frm:                     # 你把自動修正改回去了：這條規則不對，停用
            back.update(status="off", count=0, updated=now)
            changed.append(back)
            continue
        c = by_from.get(frm)
        if c and c["to"] == to:
            c["count"] = c.get("count", 0) + 1
            if c.get("status") == "pending" and c["count"] >= AUTO_ON_COUNT:
                c["status"] = "on"
            c["updated"] = now
        elif c:                                            # 同一個錯字這次改成別的：以最新的為準，重新計數
            c.update(to=to, count=1, status="pending", updated=now)
        else:
            c = {"from": frm, "to": to, "count": 1, "status": "pending", "source": "learned",
                 "added": now, "updated": now, "example": source}
            vocab["corrections"].append(c)
            by_from[frm] = c
        changed.append(c)
    save_vocab(vocab)
    return changed


class Job:
    def __init__(self, folder):
        self.dir = Path(folder)
        self.meta = json.loads((self.dir / "meta.json").read_text())
        self._saved = 0.0

    def save(self, **kw):
        self.meta.update(kw)
        tmp = self.dir / "meta.json.tmp"
        tmp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=1))
        tmp.replace(self.dir / "meta.json")
        self._saved = time.time()

    def progress(self, pct=None, detail=None):
        """進度更新得很頻繁，最多每秒寫一次檔。"""
        if time.time() - self._saved >= 1.0:
            self.save(progress=pct, detail=detail)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL)
    if r.returncode:
        raise RuntimeError(f"{Path(cmd[0]).name} 執行失敗：{(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout


def fmt_time(sec, hours=False):
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if hours or h else f"{m:02d}:{s:02d}"


# ---------------- 1. 轉檔 ----------------
def convert(job):
    job.save(stage="convert", progress=None, detail=None)
    d = job.dir
    src = d / job.meta["source_file"] if job.meta.get("source_file") else None
    audio, wav = d / "audio.m4a", d / "audio.wav"
    if src and src.exists():
        probe = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                     "-of", "csv=p=0", str(src)]).strip()
        if not probe:
            raise RuntimeError("這個檔案裡沒有聲音，無法轉錄。")
        # 留一份體積小的音檔給網頁播放，原始上傳的檔案轉完就刪掉
        run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:a:0", "-vn", "-ac", "1",
             "-c:a", "aac", "-b:a", "64k", str(audio)])
        run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:a:0", "-vn", "-ac", "1",
             "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
        src.unlink()
        job.meta["source_file"] = None
    elif audio.exists():
        run(["ffmpeg", "-y", "-v", "error", "-i", str(audio), "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(wav)])
    else:
        raise RuntimeError("找不到音檔。")
    dur = float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)]).strip() or 0)
    job.save(duration=dur)
    return wav


# ---------------- 2. 轉錄 ----------------
_CJK = "\u4e00-\u9fff"
_PUNCT = {",": "，", "?": "？", "!": "！", ":": "：", ";": "；"}


def fullwidth_punct(text):
    """中文字旁邊的半形標點換成全形（數字裡的逗號、英文句子不動）。"""
    text = re.sub(f"(?<=[{_CJK}])[,?!:;]", lambda m: _PUNCT[m.group()], text)
    return re.sub(f"[,?!:;](?=[{_CJK}])", lambda m: _PUNCT[m.group()], text)


def split_sentences(start, end, text, max_chars=60):
    """whisper 有時一段就是半分鐘；照標點切成句子，時間依字數比例推算，方便點時間跳到那一句。"""
    if len(text) <= max_chars:
        return [{"start": round(start, 2), "end": round(end, 2), "text": text}]
    pieces = []
    for sent in re.findall(r".+?(?:[。！？!?；;]+|\.(?=\s)|$)\s*", text):
        if len(sent) <= max_chars:
            pieces.append(sent)
            continue
        cur = ""
        for part in re.findall(r".+?(?:[，,、]+|$)\s*", sent):     # 句子太長再照逗號切
            if cur and len(cur) + len(part) > max_chars:
                pieces.append(cur)
                cur = ""
            cur += part
        if cur:
            pieces.append(cur)
    out, pos, total = [], 0, sum(len(p) for p in pieces) or 1
    for p in pieces:
        a = start + (end - start) * pos / total
        pos += len(p)
        b = start + (end - start) * pos / total
        if p.strip():
            out.append({"start": round(a, 2), "end": round(b, 2), "text": p.strip()})
    return out


def run_whisper(job, cfg, wav, lang, out_base, offset=0.0):
    """跑 whisper，回傳 (句子清單, 偵測到的語言)。offset：這段音檔在整段錄音裡的起點（秒）。"""
    asr, model, punct = asr_choice(cfg, job.meta.get("asr") if hasattr(job, "meta") else None)
    if not Path(model).exists():
        raise RuntimeError(f"找不到語音模型：{model}")
    cmd = [cfg["whisper_cli"], "-m", model, "-f", str(wav), "-l", lang,
           "-oj", "-of", str(out_base), "-pp"]
    vocab = load_vocab()
    prompt = cfg.get("initial_prompt", "") if lang in ("zh", "auto") else ""
    terms = prompt_terms(cfg, vocab)
    if terms:                                          # 專有名詞放進提示：whisper 會傾向用這些寫法
        prompt += " 會提到：" + "、".join(terms) + "。"
    if prompt.strip():
        cmd += ["--prompt", prompt.strip()]
    if cfg.get("vad_model") and Path(cfg["vad_model"]).exists():
        cmd += ["--vad", "-vm", cfg["vad_model"]]       # 先切掉沒人講話的部分，減少靜音時亂生字
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", stdin=subprocess.DEVNULL)
    last = []
    for line in proc.stdout:
        last = (last + [line.rstrip()])[-25:]
        m = re.search(r"progress\s*=\s*(\d+)%", line)
        if m:
            job.progress(int(m.group(1)))
        else:
            print(line.rstrip(), flush=True)          # 其餘訊息留在 job.log，方便事後查問題
    if proc.wait() != 0:
        raise RuntimeError("語音轉錄失敗：" + "\n".join(last[-6:]))
    data = json.loads(out_base.with_suffix(".json").read_text(errors="replace"))
    out_base.with_suffix(".json").unlink(missing_ok=True)
    detected = (data.get("result") or {}).get("language") or lang
    cc = None
    if detected == "zh" and cfg.get("traditional_chinese"):
        try:
            from opencc import OpenCC
            cc = OpenCC("s2tw")                          # 只轉字形，不改用詞
        except Exception:
            cc = None
    segments, fixed = [], 0
    for seg in data.get("transcription", []):
        text = seg.get("text", "").strip()
        if not text:
            continue
        if cc:
            text = cc.convert(text)
        if punct:
            text = add_punct(text)
        if detected == "zh":
            text = fullwidth_punct(text)
        text, k = apply_corrections(text, vocab)
        fixed += k
        off = seg.get("offsets") or {}
        segments += split_sentences(offset + off.get("from", 0) / 1000, offset + off.get("to", 0) / 1000, text)
    job.auto_fixed = fixed                              # 這次自動套用了幾處修正
    return segments, detected


def write_transcript(folder, segments):
    segments = sorted(segments, key=lambda x: x["start"])
    (folder / "transcript.json").write_text(json.dumps(segments, ensure_ascii=False))
    hours = bool(segments and segments[-1]["end"] >= 3600)
    (folder / "transcript.txt").write_text("".join(f"[{fmt_time(x['start'], hours)}] {x['text']}\n" for x in segments))
    return segments


def transcribe(job, cfg, wav):
    job.save(stage="transcribe", progress=0, detail=None)
    lang = job.meta.get("language") or cfg["language"]
    t0 = time.time()
    segments, detected = run_whisper(job, cfg, wav, lang, job.dir / "whisper")
    segments = write_transcript(job.dir, segments)
    (job.dir / "transcript.orig.json").unlink(missing_ok=True)   # 重新轉錄：舊的「修改前原稿」不再對得上
    wav.unlink(missing_ok=True)
    job.save(auto_fixed=getattr(job, "auto_fixed", 0), transcript_edited=None, detected_language=detected, chars=sum(len(x["text"]) for x in segments), asr=asr_choice(cfg, job.meta.get("asr"))[0],
             transcribe_sec=round(time.time() - t0, 1), progress=100, summary_stale=False, fixed_ranges=[])
    return segments


def retranscribe_range(job, cfg, rng):
    """用指定的語言重新辨識其中一段（例如中文錄音裡有一段英文被轉成中文），換掉那段的逐字稿。"""
    start, end, lang = float(rng["start"]), float(rng["end"]), rng.get("lang") or "auto"
    if end <= start:
        raise RuntimeError("結束時間要比開始時間晚")
    audio = job.dir / "audio.m4a"
    if not audio.exists():
        raise RuntimeError("音檔已經不在了，無法重新辨識")
    job.save(stage="transcribe", progress=0, detail=f"{fmt_time(start)}–{fmt_time(end)}")
    wav = job.dir / "range.wav"
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", str(audio),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
    try:
        new, detected = run_whisper(job, cfg, wav, lang, job.dir / "whisper_range", offset=start)
    finally:
        wav.unlink(missing_ok=True)
    old = json.loads((job.dir / "transcript.json").read_text())
    inside = lambda x: start <= (x["start"] + x["end"]) / 2 < end                   # 句子中間點落在這段裡的就換掉
    keep = [x for x in old if not inside(x)]
    segments = write_transcript(job.dir, keep + new)
    orig = job.dir / "transcript.orig.json"
    if orig.exists():                                   # 修改前原稿也換掉這段，之後比對才對得上
        o = json.loads(orig.read_text())
        orig.write_text(json.dumps(sorted([x for x in o if not inside(x)] + new, key=lambda x: x["start"]), ensure_ascii=False))
    fixed = (job.meta.get("fixed_ranges") or []) + [{"start": start, "end": end, "lang": detected, "at": time.time()}]
    job.save(chars=sum(len(x["text"]) for x in segments), progress=100, summary_stale=True, fixed_ranges=fixed)
    return segments


# ---------------- 3. 整理 ----------------
def ollama(path, payload, timeout=1800):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", str(e))
        except Exception:
            msg = str(e)
        raise RuntimeError(f"Ollama 回報錯誤：{msg}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"連不上 Ollama（{e.reason}）。請確認 Ollama 有在執行。")


class Summarizer:
    def __init__(self, cfg, model):
        self.model = model
        self.num_ctx = int(cfg["num_ctx"])
        # 模型已經載入時沿用它目前的上下文長度，避免 Ollama 重新載入而打斷正在用它的其他工作
        try:
            with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=10) as r:
                for m in json.loads(r.read()).get("models", []):
                    if m.get("name") == model and m.get("context_length"):
                        self.num_ctx = int(m["context_length"])
        except Exception:
            pass
        try:
            caps = ollama("/api/show", {"model": model}, timeout=30).get("capabilities") or []
        except RuntimeError:
            caps = []
        self.thinking = "thinking" in caps
        self.truncated = False
        # 有些模型關不掉思考，思考的字也算在輸出額度裡，所以多留一些
        self.predict = OUTPUT_TOKENS + (THINKING_TOKENS if self.thinking else 0)
        # 中文大約一個字一個 token，留一點餘裕
        self.chunk_chars = max(1200, int((self.num_ctx - self.predict - PROMPT_OVERHEAD) * 0.75))

    def ask(self, prompt):
        predict = self.predict
        if self.thinking:
            # 思考的長度無法預測：上下文裡扣掉提示之後剩下的空間全部給它
            predict = max(predict, self.num_ctx - int(len(self.system + prompt) * 1.2) - 200)
        payload = {"model": self.model, "stream": False,
                   "messages": [{"role": "system", "content": self.system}, {"role": "user", "content": prompt}],
                   "options": {"temperature": 0.2, "num_ctx": self.num_ctx, "num_predict": predict}}
        if self.thinking:
            payload["think"] = False
        resp = ollama("/api/chat", payload)
        if resp.get("done_reason") == "length":
            self.truncated = True                 # 額度用完被切斷，結果可能不完整
        text = (resp.get("message") or {}).get("content", "")
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
        text = re.sub(r"^.*?</think>", "", text, flags=re.S) if "</think>" in text else text
        text = re.sub(r"<think>.*$", "", text, flags=re.S).strip()        # 思考到額度用完、沒有結尾標記
        if not text:
            raise RuntimeError(f"{self.model} 沒有產生內容" + ("，它把輸出額度都用在思考上了。請改用不會思考的模型（例如 qwen2.5:7b）再整理一次。"
                                                        if self.thinking else "。請換一個模型再整理一次。"))
        return text


def to_traditional(text, cfg):
    """小模型偶爾會夾雜簡體字，用字形轉換補上（不改用詞）。"""
    if not cfg.get("traditional_chinese"):
        return text
    try:
        from opencc import OpenCC
        return OpenCC("s2tw").convert(text)
    except Exception:
        return text


def _secs(stamp):
    n = [int(x) for x in stamp.split(":")]
    return n[0] * 60 + n[1] if len(n) == 2 else n[0] * 3600 + n[1] * 60 + n[2]


def ground_times(summary, lines):
    """模型有時會自己編時間：只留下逐字稿裡真的出現過的時間標記，其餘拿掉。"""
    summary = re.sub(r"[（(](\d{1,2}:\d{2}(?::\d{2})?)[）)]", r"[\1]", summary)     # 統一成方括號，頁面才能點
    allowed = {_secs(m) for m in re.findall(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]", "\n".join(lines), flags=re.M)}
    return re.sub(r"[ \t]*\[(\d{1,2}:\d{2}(?::\d{2})?)\]",
                  lambda m: m.group(0) if _secs(m.group(1)) in allowed else "", summary)


def paragraphs(segments, max_chars=30):
    """把很短的片段併成段落，每段一個時間標記；段落短一點，模型引用時間才準。"""
    hours = bool(segments and segments[-1]["end"] >= 3600)
    out, cur, start = [], "", 0
    for s in segments:
        if not cur:
            start = s["start"]
        cur += s["text"]
        if len(cur) >= max_chars:
            out.append(f"[{fmt_time(start, hours)}] {cur}")
            cur = ""
    if cur:
        out.append(f"[{fmt_time(start, hours)}] {cur}")
    return out


def pack(lines, limit):
    chunks, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    return chunks


CLAUDE_EXTRA = """
補充說明：
- 逐字稿是語音辨識的結果，可能有同音錯字，或某一段的語言辨識錯了（例如英文內容被轉成意思不通的中文）；請依上下文判斷，看不懂的地方不要硬湊，標「（不確定）」。
- 方括號裡的時間只能照抄逐字稿裡出現過的。
- 直接輸出 Markdown，第一行是「# 主題」，不要加任何前言或結語。"""


def claude_summary(cfg, system, lines):
    """把逐字稿交給本機的 Claude Code（claude -p）整理。會用到你 Claude 方案的用量。"""
    import shutil
    exe = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    prompt = FINAL_PROMPT.format(kind="逐字稿", text="\n".join(lines)) + "\n" + CLAUDE_EXTRA
    cmd = [exe, "-p", "--output-format", "text", "--tools", "", "--no-session-persistence", "--strict-mcp-config",
           "--model", cfg.get("claude_model") or "sonnet", "--system-prompt", system]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=900, cwd=str(ROOT / "data"))
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude 超過 15 分鐘沒有回覆，請稍後再試。")
    except FileNotFoundError:
        raise RuntimeError("找不到 claude 指令（Claude Code）。")
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        msg = (r.stderr or r.stdout or "").strip()[-400:]
        if re.search(r"log ?in|auth|credential", msg, re.I):
            msg = "Claude Code 沒有登入，請在終端機執行 claude 登入後再試。" + ("\n" + msg if msg else "")
        elif re.search(r"limit|usage", msg, re.I):
            msg = "Claude 用量已達上限，請等重置後再試，或改用本機模型。" + ("\n" + msg if msg else "")
        raise RuntimeError("Claude 整理失敗：" + (msg or f"結束代碼 {r.returncode}"))
    return out


def summarize(job, cfg, segments):
    model = job.meta.get("summary_model") or cfg["summary_model"]
    job.save(stage="summarize", progress=None, detail="傳給 Claude 整理" if model == "claude" else None, summary_model=model)
    t0 = time.time()
    if sum(len(s["text"]) for s in segments) < 20:
        (job.dir / "summary.md").write_text("## 摘要\n這段錄音沒有辨識到足夠的語音內容。\n")
        job.save(summarize_sec=0, summary_stale=False)
        return
    if model == "claude":
        system = SYSTEM + ("\n以下專有名詞可能出現在錄音裡，請使用這些寫法：" + "、".join(all_terms(cfg)) if all_terms(cfg) else "")
        lines = paragraphs(segments, max_chars=60)
        summary = re.sub(r"^```(?:markdown)?\s*\n|\n```\s*$", "", claude_summary(cfg, system, lines).strip())
        summary = ground_times(to_traditional(summary, cfg), lines)
        title = None
        m = re.match(r"#\s+(.+)\n", summary + "\n")
        if m:
            title = m.group(1).strip().strip("（）()")[:60]
            summary = summary[m.end():].lstrip("\n")
        (job.dir / "summary.md").write_text(summary.rstrip() + "\n")
        job.save(title=title or job.meta.get("title"), truncated=False, summarize_sec=round(time.time() - t0, 1),
                 summary_stale=False, summary_edited=False)
        return
    sm = Summarizer(cfg, model)
    sm.system = SYSTEM + ("\n以下專有名詞可能出現在錄音裡，請使用這些寫法：" + "、".join(all_terms(cfg)) if all_terms(cfg) else "")
    lines = paragraphs(segments)
    chunks = pack(lines, sm.chunk_chars)
    if len(chunks) == 1:
        text, kind = chunks[0], "逐字稿"
    else:
        notes = []
        for i, c in enumerate(chunks, 1):
            job.save(progress=round(100 * (i - 1) / (len(chunks) + 1)), detail=f"第 {i}/{len(chunks)} 段")
            notes.append(sm.ask(NOTES_PROMPT.format(i=i, n=len(chunks), text=c)))
        text = "\n".join(notes)
        for _ in range(3):                       # 筆記還是太長就再濃縮
            if len(text) <= sm.chunk_chars:
                break
            parts = pack(text.splitlines(), sm.chunk_chars)
            job.save(detail="濃縮筆記")
            text = "\n".join(sm.ask(CONDENSE_PROMPT.format(i=i, n=len(parts), text=p)) for i, p in enumerate(parts, 1))
        text, kind = text[:sm.chunk_chars], "重點筆記"
        job.save(progress=round(100 * len(chunks) / (len(chunks) + 1)), detail="彙整")
    summary = sm.ask(FINAL_PROMPT.format(kind=kind, text=text))
    summary = re.sub(r"^```(?:markdown)?\s*\n|\n```\s*$", "", summary.strip())
    summary = ground_times(to_traditional(summary, cfg), lines)
    title = None
    m = re.match(r"#\s+(.+)\n", summary + "\n")
    if m:
        title = m.group(1).strip().strip("（）()")[:60]
        summary = summary[m.end():].lstrip("\n")
    if sm.truncated:
        summary = ("**注意：這份摘要可能不完整。**模型的輸出額度用完了" +
                   ("（它會先思考，思考也佔額度）。建議按「重新整理」改用不會思考的模型。" if sm.thinking else "。建議按「重新整理」換一個模型。") +
                   "\n\n" + summary)
    (job.dir / "summary.md").write_text(summary.rstrip() + "\n")
    job.save(title=title or job.meta.get("title"), truncated=sm.truncated, summarize_sec=round(time.time() - t0, 1), num_ctx=sm.num_ctx,
             summary_stale=False, summary_edited=False)


def main():
    job = Job(sys.argv[1])
    cfg = load_config()
    start_from = job.meta.pop("redo_from", None)
    rng = job.meta.pop("range", None)
    job.save(status="running", pid=os.getpid(), started=time.time(), finished=None, error=None,
             stage=None, progress=None, detail=None)
    try:
        if start_from == "range" and rng and (job.dir / "transcript.json").exists():
            retranscribe_range(job, cfg, rng)          # 只換掉那一段；摘要等你決定要不要重新整理
        else:
            if start_from == "summarize" and (job.dir / "transcript.json").exists():
                segments = json.loads((job.dir / "transcript.json").read_text())
            else:
                segments = transcribe(job, cfg, convert(job))
            summarize(job, cfg, segments)
        job.save(status="done", finished=time.time(), stage=None, progress=None, detail=None, pid=None)
    except Exception as e:
        print(f"失敗：{e}", flush=True)
        job.save(status="error", error=str(e), finished=time.time(), pid=None)
        sys.exit(1)


if __name__ == "__main__":
    main()
