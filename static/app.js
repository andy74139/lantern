'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const S = { sys: null, ollama: null, recs: null, rec: null, tasks: [], tasksError: null, claude: null, runs: [],
            open: new Set(), logs: {}, busy: new Set(), claudeLimit: 8, runsAll: false, offline: false };

// ---------- 小工具 ----------
const lastHTML = new WeakMap();
function setHTML(el, html) {
  if (lastHTML.get(el) === html) return false;
  lastHTML.set(el, html);
  el.innerHTML = html;
  return true;
}

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Local-Controller': '1' },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* 不是 JSON */ }
    throw new Error(msg);
  }
  return r.json();
}

async function* ndjson(path, body, signal) {
  const r = await fetch(path, {
    method: 'POST', signal,
    headers: { 'Content-Type': 'application/json', 'X-Local-Controller': '1' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(r.statusText);
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (line) yield JSON.parse(line);
    }
  }
  if (buf.trim()) yield JSON.parse(buf);
}

const gb = n => (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + ' GB';          // 模型大小（和 ollama list 一致）
const gib = n => (n / 2 ** 30).toFixed(1);                               // 記憶體
const kfmt = n => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n);

function rel(ms) {
  const s = (Date.now() - ms) / 1000;
  if (s < 60) return '剛剛';
  if (s < 3600) return Math.floor(s / 60) + ' 分鐘前';
  if (s < 86400) return Math.floor(s / 3600) + ' 小時前';
  if (s < 86400 * 7) return Math.floor(s / 86400) + ' 天前';
  const d = new Date(ms);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}
function clock(ms) {
  const d = new Date(ms), p = n => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function dur(sec) {
  if (sec < 60) return sec.toFixed(sec < 10 ? 1 : 0) + ' 秒';
  if (sec < 3600) return Math.floor(sec / 60) + ' 分 ' + Math.floor(sec % 60) + ' 秒';
  return Math.floor(sec / 3600) + ' 小時 ' + Math.floor((sec % 3600) / 60) + ' 分';
}

function toast(msg, bad = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (bad ? ' bad' : '');
  el.textContent = msg;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), bad ? 8000 : 4000);
}

// ---------- 對話框 ----------
const dlg = $('#dlg');
let dlgResolve = null, dlgValues = {};
function openDlg(title, bodyHTML, footHTML = '', wide = false) {
  dlg.classList.toggle('wide', wide);
  $('#dlg-title').textContent = title;
  $('#dlg-body').innerHTML = bodyHTML;
  $('#dlg-foot').innerHTML = footHTML;
  dlg.returnValue = '';
  if (!dlg.open) dlg.showModal();
  $('#dlg-body').scrollTop = 0;
}
dlg.addEventListener('close', () => {
  // close 事件是延後觸發的：如果這時對話框已經又被打開，這是上一次關閉留下的事件，不能拿來回答新的詢問
  if (dlg.open) return;
  if (dlgResolve) { const r = dlgResolve; dlgResolve = null; r(dlg.returnValue === 'ok'); }
});
/** 關閉對話框並立刻回覆等待中的詢問（不依賴 close 事件：分頁在背景時瀏覽器可能延後送出它）。 */
function closeDlg(value = '') {
  const r = dlgResolve;
  dlgResolve = null;
  if (dlg.open) dlg.close(value);
  if (r) r(value === 'ok');
}
function ask(title, text, okLabel = '確定', danger = false, bodyHTML = null) {
  return new Promise(res => {
    openDlg(title, bodyHTML ?? `<p style="white-space:pre-wrap">${esc(text)}</p>`,
      `<button class="btn" data-act="dlg-close">取消</button>
       <button class="btn ${danger ? 'danger' : 'primary'}" data-act="dlg-ok">${esc(okLabel)}</button>`);
    dlgResolve = res;
  });
}
function showOutput(title, text) {
  openDlg(title, `<pre>${esc(text || '（沒有輸出）')}</pre>`, `<button class="btn" data-act="dlg-close">關閉</button>`);
}

// ---------- 狀態列 ----------
function renderChips() {
  // 狀態列保持一列：文字精簡；游標停在任何一格上會立刻顯示它是什麼（data-tip，第一行是名稱）
  const o = S.ollama, m = S.sys, c = S.claude;
  let h = '';
  const meter = (label, pct, tip, cls = '') => `<span class="chip" tabindex="0" data-tip="${esc(tip)}">${label}
      <span class="pct">${Math.round(pct)}%</span><span class="mini use ${cls}"><i style="width:${Math.min(100, pct).toFixed(0)}%"></i></span></span>`;
  if (S.offline) h += `<span class="chip" tabindex="0" data-tip="${esc('首頁服務沒有回應\n本機的控制台服務可能正在重新啟動')}"><span class="dot bad"></span>服務沒有回應</span>`;
  if (m && m.cpu != null) h += meter('CPU', m.cpu, `CPU 使用率\n${m.ncpu} 核心的平均使用率`);
  if (m && m.gpu) h += meter('GPU', m.gpu.percent,
    `GPU 使用率\n${m.gpu.name || 'GPU'}${m.gpu.memory ? '・GPU 使用中的記憶體 ' + gib(m.gpu.memory) + ' GB' : ''}`);
  if (m) {
    const cls = m.pressure === 'critical' ? 'bad' : m.pressure === 'warn' ? 'warn' : '';
    const press = { normal: '正常', warn: '偏高', critical: '過高' }[m.pressure] || '—';
    h += meter('記憶體', m.used / m.total * 100,
      `記憶體使用量\n已用 ${gib(m.used)} / ${gib(m.total)} GB・壓力${press}\nSwap 已用 ${gib(m.swap_used)} GB`, cls);
  }
  if (o) {
    h += o.up
      ? `<span class="chip" tabindex="0" data-tip="${esc(`Ollama（本機 AI 模型）\n運作中・已載入 ${o.loaded.length} 個模型${o.loaded.length ? '\n' + o.loaded.map(x => x.name).join('、') : ''}`)}"><span class="dot ok"></span>Ollama ${o.loaded.length}</span>`
      : `<span class="chip" tabindex="0" data-tip="${esc('Ollama（本機 AI 模型）\n沒有在執行')}"><span class="dot bad"></span>Ollama 停止</span>`;
  }
  // Claude：對話數和用量上限放在同一格，看得出用量是 Claude 的
  let cl = '';
  if (c) {
    cl += `<span class="seg" tabindex="0" data-tip="${esc(`Claude Code 對話\n開啟 ${c.live_count} 個・工作中 ${c.busy_count} 個${c.busy_count ? '\n顯示為「工作中／開啟」' : ''}`)}"><span class="dot ${c.busy_count ? 'ok pulse' : ''}"></span>Claude ${c.busy_count ? c.busy_count + '/' : ''}${c.live_count}</span>`;
  }
  if (typeof limitSegsHTML === 'function') cl += limitSegsHTML();
  if (cl) h += `<span class="chip chip-group">${cl}</span>`;
  if (setHTML($('#chips'), h) && typeof syncHeadHeight === 'function') syncHeadHeight();   // 狀態列高度變了，目錄跳轉的位置跟著調
}

// ---------- 說明小框：游標停留（或用 Tab 鍵移到）有 data-tip 的元素時立刻顯示 ----------
(() => {
  const tip = document.createElement('div');
  tip.id = 'tip'; tip.setAttribute('role', 'tooltip'); tip.hidden = true;
  document.body.append(tip);
  let cur = null;
  const show = el => {
    const text = el.dataset.tip;
    if (!text) return;
    cur = el;
    const [name, ...rest] = text.split('\n');
    tip.innerHTML = `<strong>${esc(name)}</strong>${rest.length ? '<br>' + rest.map(esc).join('<br>') : ''}`;
    tip.hidden = false;
    const r = el.getBoundingClientRect(), w = tip.offsetWidth, hgt = tip.offsetHeight;
    let left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), innerWidth - w - 8);
    let top = r.bottom + 8;
    if (top + hgt > innerHeight - 8) top = r.top - hgt - 8;
    tip.style.left = left + 'px'; tip.style.top = top + 'px';
  };
  const hide = () => { cur = null; tip.hidden = true; };
  document.addEventListener('mouseover', e => { const el = e.target.closest('[data-tip]'); if (el && el !== cur) show(el); else if (!el && cur) hide(); });
  document.addEventListener('focusin', e => { const el = e.target.closest('[data-tip]'); el ? show(el) : hide(); });
  document.addEventListener('focusout', hide);
  addEventListener('scroll', hide, true);
  // 狀態列每幾秒會重畫：正在看的那一格被換掉時，跟著更新內容
  new MutationObserver(() => {
    if (cur && !cur.isConnected) {
      const again = document.querySelector('#chips [data-tip]:hover');
      again ? show(again) : hide();
    }
  }).observe(document.body, { childList: true, subtree: true });
})();

// ---------- 常用工作 ----------
function visibleActions(t) {
  return t.actions.filter(a => !(a.when === 'running' && !t.running) && !(a.when === 'stopped' && t.running));
}

// 「AI 工作」分頁上的數字：正在跑的常用工作與錄音
function updateAiBadge() {
  const n = (S.tasks || []).filter(t => t.running).length
    + ((S.recs && S.recs.recordings) || []).filter(r => r.status === 'running').length;
  const b = $('#ai-badge');
  if (!b) return;
  b.hidden = !n;
  b.textContent = n ? `${n} 執行中` : '';
}

function renderTasks() {
  updateAiBadge();
  const el = $('#tasks');
  if (S.tasksError) { setHTML(el, `<div class="banner">${esc(S.tasksError)}</div>`); return; }
  if (!S.tasks.length) { setHTML(el, `<p class="muted">還沒有定義任何工作。</p>`); return; }
  const html = S.tasks.map(t => {
    const p = t.progress;
    const pct = p && p.total ? Math.min(100, p.done / p.total * 100) : null;
    const headline = t.status ? (t.status.output.split('\n')[0] || '') : '';
    const open = S.open.has(t.id);
    const acts = visibleActions(t).map(a => {
      const busy = S.busy.has(t.id + ':' + a.id);
      return `<button class="btn ${esc(a.style || '')}" data-act="task-action" data-task="${esc(t.id)}" data-action="${esc(a.id)}"
                ${a.hint ? `title="${esc(a.hint)}"` : ''} ${busy ? 'disabled' : ''}>${busy ? '執行中…' : esc(a.label || a.id)}</button>`;
    }).join('');
    return `<article class="card ${t.running ? 'is-running' : ''} ${open ? 'open' : ''}">
      <div class="task-head">
        <div><h3>${esc(t.name)}</h3><p class="muted small">${esc(t.description)}</p></div>
        <span class="badge ${t.running ? 'on' : ''}">${t.running ? `<span class="dot ok pulse"></span>執行中` : '未執行'}</span>
      </div>
      ${t.cwd_ok ? '' : `<div class="banner">找不到資料夾 ${esc(t.cwd)}</div>`}
      ${pct === null ? '' : `<div>
        <div class="bar"><i style="width:${pct.toFixed(1)}%"></i></div>
        <div class="bar-label"><span>${p.done.toLocaleString()} / ${p.total.toLocaleString()} ${esc(p.unit)}（${pct.toFixed(1)}%）</span>
          <span>${p.note ? esc(p.prefix ?? '預估剩 ') + esc(p.note) : ''}</span></div>
      </div>`}
      ${headline ? `<div class="headline ${t.status.ok ? '' : 'muted'}">${esc(headline)}</div>` : ''}
      <div class="actions">${acts}</div>
      <div>
        <button class="btn link" data-act="task-toggle" data-task="${esc(t.id)}">${open ? '收合詳細狀態 ▴' : '詳細狀態與記錄 ▾'}</button>
        ${open ? `<div class="detail">
          ${t.status ? `<h4>狀態（${rel(t.status.at * 1000)}更新）</h4><pre class="table">${esc(t.status.output)}</pre>` : ''}
          ${t.has_log ? `<h4>最近記錄</h4><pre class="log" data-log="${esc(t.id)}">${esc(S.logs[t.id] ?? '載入中…')}</pre>` : ''}
        </div>` : ''}
      </div>
    </article>`;
  }).join('');
  if (setHTML(el, html)) el.querySelectorAll('pre.log').forEach(p => { p.scrollTop = p.scrollHeight; });
}

async function askInputs(t, a) {
  const fields = a.inputs.map(i => `<div class="field">
      <label for="in-${esc(i.id)}">${esc(i.label || i.id)}</label>
      <div class="row" style="margin:0">
        <input type="text" id="in-${esc(i.id)}" data-input="${esc(i.id)}" value="${esc(i.default || '')}" placeholder="${esc(i.placeholder || '')}">
        ${i.type === 'file' || i.type === 'folder' ? `<button class="btn" data-act="pick" data-kind="${i.type}" data-target="in-${esc(i.id)}">選擇…</button>` : ''}
      </div></div>`).join('');
  const ok = await ask(`${t.name}：${a.label}`, '', '執行', a.style === 'danger',
    (a.confirm ? `<p style="margin-bottom:12px">${esc(a.confirm)}</p>` : '') + fields);
  return ok ? dlgValues : null;
}

async function runAction(taskId, actionId) {
  const t = S.tasks.find(x => x.id === taskId);
  const a = t && t.actions.find(x => x.id === actionId);
  if (!a) { toast('這個工作或動作已經不在設定檔裡了', true); return; }
  let inputs = {};
  if (a.inputs && a.inputs.length) {
    inputs = await askInputs(t, a);
    if (!inputs) return;
  } else if (a.confirm && !await ask(`${t.name}：${a.label}`, a.confirm, a.label, a.style === 'danger')) return;
  const key = taskId + ':' + actionId, title = `${t.name}：${a.label}`;
  S.busy.add(key); renderTasks();
  try {
    const r = await api(`/api/tasks/${encodeURIComponent(taskId)}/actions/${encodeURIComponent(actionId)}`, { inputs });
    const out = (r.output || '').trim();
    if (r.long && r.exit_code === null) toast(`已在背景開始：${a.label}`);
    else if (r.exit_code !== 0) showOutput(`${title}（失敗，代碼 ${r.exit_code}）`, out);
    else if (out.split('\n').length > 2) showOutput(title, out);
    else toast(out || '完成');
  } catch (e) {
    toast(`${title}：${e.message}`, true);
  } finally {
    S.busy.delete(key);
    await Promise.all([loadTasks(), loadRuns()]);
  }
}

// ---------- Ollama ----------
function expiresText(iso) {
  if (!iso) return '';
  const min = (new Date(iso) - Date.now()) / 60000;
  if (min > 60 * 24 * 365) return '不會自動卸載';
  if (min <= 0) return '即將卸載';
  return min < 1 ? '不到 1 分鐘後自動卸載' : `${Math.round(min)} 分鐘後自動卸載`;
}

function renderOllama() {
  const o = S.ollama;
  if (!o) return;
  $('#ollama-ver').textContent = o.up ? 'v' + o.version : '';
  setHTML($('#ollama-banner'), o.up ? '' :
    `<div class="banner">Ollama 沒有在執行，以下功能暫時無法使用。<button class="btn" data-act="ollama-start">啟動 Ollama</button></div>`);
  const loadedNames = new Set(o.loaded.map(m => m.name));
  setHTML($('#ollama-loaded'), o.loaded.length ? o.loaded.map(m => `<div class="item">
      <span class="dot ok"></span>
      <div class="grow"><div class="name">${esc(m.name)}</div>
        <div class="meta">佔用 ${gb(m.size_vram || m.size)}・上下文 ${(m.context_length || 0).toLocaleString()}・${expiresText(m.expires_at)}</div></div>
      <button class="btn sm" data-act="ollama-unload" data-model="${esc(m.name)}" ${S.busy.has('unload:' + m.name) ? 'disabled' : ''}>卸載</button>
    </div>`).join('') : `<p class="muted pad small">${o.up ? '目前沒有模型載入，GPU 記憶體是空的。' : '—'}</p>`);
  $('#ollama-models-n').textContent = o.models.length ? `${o.models.length} 個` : '';
  setHTML($('#ollama-models'), o.models.length ? o.models.map(m => {
    const loading = S.busy.has('load:' + m.name);
    return `<div class="item">
      <div class="grow"><div class="name">${esc(m.name)}</div>
        <div class="meta">${gb(m.size)}${m.params ? '・' + esc(m.params) : ''}${m.quant ? '・' + esc(m.quant) : ''}
          ${m.capabilities.map(c => `<span class="tag">${esc(c)}</span>`).join(' ')}</div></div>
      ${loadedNames.has(m.name) ? `<span class="badge on">已載入</span>`
        : `<button class="btn sm" data-act="ollama-load" data-model="${esc(m.name)}" ${loading ? 'disabled' : ''}>${loading ? '載入中…' : '載入'}</button>`}
      <button class="icon" data-act="ollama-delete" data-model="${esc(m.name)}" title="刪除模型" aria-label="刪除 ${esc(m.name)}">🗑</button>
    </div>`;
  }).join('') : `<p class="muted pad small">${o.up ? '還沒有安裝任何模型。' : '—'}</p>`);
  const sel = $('#test-model'), names = o.models.map(m => m.name).join('|');
  if (sel.dataset.names !== names) {
    const cur = sel.value;
    sel.innerHTML = o.models.map(m => `<option>${esc(m.name)}</option>`).join('');
    sel.dataset.names = names;
    const prefer = o.models.find(m => m.name === cur) || o.models.find(m => loadedNames.has(m.name));
    if (prefer) sel.value = prefer.name;
  }
  syncRecModel();
}

/** 載入前檢查記憶體：已經有別的模型，或載入後會吃緊，就先問。 */
async function memoryGuard(name, title = null, okLabel = '仍要載入') {
  const o = S.ollama, m = S.sys;
  if (!o || o.loaded.some(x => x.name === name)) return true;
  const model = o.models.find(x => x.name === name);
  const need = (model ? model.size : 0) * 1.1;
  const tight = m && (m.used + need > m.total * 0.9);
  if (!o.loaded.length && !tight) return true;
  const lines = [`載入 ${name} 大約需要 ${gb(need)}。`];
  if (o.loaded.length) lines.push(`目前已載入：${o.loaded.map(x => `${x.name}（${gb(x.size_vram || x.size)}）`).join('、')}。`);
  if (m) lines.push(`記憶體已用 ${gib(m.used)} / ${gib(m.total)} GB。`);
  const running = S.tasks.filter(t => t.running).map(t => `「${t.name}」`);
  if (running.length) lines.push(`${running.join('、')}正在執行，同時載入可能讓它變慢，或把它的模型擠出記憶體。`);
  else lines.push('同時載入可能會變慢，或把原本的模型擠出記憶體。');
  return ask(title || `要載入 ${name} 嗎？`, lines.join('\n'), okLabel);
}

async function ollamaOp(kind, model) {
  if (kind === 'load' && !await memoryGuard(model)) return;
  if (kind === 'delete' && !await ask(`刪除 ${model}？`, '模型檔會從硬碟移除，之後要用必須重新下載。', '刪除', true)) return;
  const key = kind + ':' + model;
  S.busy.add(key); renderOllama();
  try {
    const r = await api('/api/ollama/' + kind, { model });
    if (!r.ok) throw new Error(r.error || '失敗');
    toast({ load: '已載入 ', unload: '已卸載 ', delete: '已刪除 ' }[kind] + model);
  } catch (e) {
    toast(`${model}：${e.message}`, true);
  } finally {
    S.busy.delete(key);
    await loadOllama();
  }
}

async function pullModel(ev) {
  ev.preventDefault();
  const model = $('#pull-name').value.trim(), out = $('#pull-progress'), btn = $('#pull-btn');
  if (!model) return;
  btn.disabled = true;
  try {
    for await (const j of ndjson('/api/ollama/pull', { model })) {
      if (j.error) throw new Error(j.error);
      out.textContent = j.status + (j.total ? `　${(100 * (j.completed || 0) / j.total).toFixed(1)}%（${gb(j.completed || 0)} / ${gb(j.total)}）` : '');
    }
    out.textContent = `${model} 下載完成。`;
    $('#pull-name').value = '';
  } catch (e) {
    out.textContent = '下載失敗：' + e.message;
  } finally {
    btn.disabled = false;
    loadOllama();
  }
}

let testCtl = null;
async function testSend() {
  if (testCtl) { testCtl.abort(); return; }
  const model = $('#test-model').value, prompt = $('#test-prompt').value.trim();
  const out = $('#test-out'), btn = $('#test-send'), stats = $('#test-stats');
  if (!model || !prompt) return;
  if (!await memoryGuard(model)) return;
  const caps = (S.ollama.models.find(m => m.name === model) || {}).capabilities || [];
  const body = { model, prompt };
  if (caps.includes('thinking')) body.think = $('#test-think').checked;
  testCtl = new AbortController();
  btn.textContent = '停止'; stats.textContent = '';
  out.hidden = false; out.innerHTML = '<span class="muted">等待模型回應…</span>';
  let think = '', resp = '';
  try {
    for await (const j of ndjson('/api/ollama/test', body, testCtl.signal)) {
      if (j.error) throw new Error(j.error);
      think += j.thinking || ''; resp += j.response || '';
      out.innerHTML = (think ? `<div class="think">${esc(think)}</div>` : '') + `<div>${esc(resp)}</div>`;
      out.scrollTop = out.scrollHeight;
      if (j.done && j.eval_count) {
        stats.textContent = `${j.eval_count} tokens・${(j.eval_count / (j.eval_duration / 1e9)).toFixed(1)} tokens/秒・共 ${dur(j.total_duration / 1e9)}`;
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') stats.textContent = '已停止';
    else out.innerHTML += `<div style="color:var(--bad)">錯誤：${esc(e.message)}</div>`;
  } finally {
    testCtl = null; btn.textContent = '送出';
    loadOllama();
  }
}

// ---------- Claude ----------
function renderClaude() {
  const c = S.claude;
  if (!c) return;
  const q = $('#claude-q').value.trim().toLowerCase();
  let list = c.sessions;
  if (q) list = list.filter(s => [s.title, s.project_name, s.cwd, s.first_prompt, s.last_prompt].some(v => (v || '').toLowerCase().includes(q)));
  const shown = list.slice(0, S.claudeLimit);
  let h = shown.map(s => `<button class="session" data-act="session-open" data-project="${esc(s.project)}" data-id="${esc(s.id)}">
      <div class="s-top"><span class="s-title">${esc(s.title)}</span>
        ${s.live === 'busy' ? '<span class="badge busy"><span class="dot ok pulse"></span>工作中</span>' : s.live ? '<span class="badge">開啟中</span>' : ''}
        <time>${rel(Date.parse(s.updated))}</time></div>
      <div class="s-meta"><span class="tag">${esc(s.project_name)}</span>
        <span>${s.n_prompts} 次提問</span>
        ${s.model ? `<span>${esc(s.model.replace(/^claude-/, ''))}</span>` : ''}
        ${s.context_tokens ? `<span title="最後一次請求送出的上下文大小">上下文 ${kfmt(s.context_tokens)}</span>` : ''}</div>
      ${s.last_prompt ? `<div class="s-last">最後提問：${esc(s.last_prompt)}</div>` : ''}
    </button>`).join('');
  if (!shown.length) h = `<p class="muted pad">${q ? '沒有符合的對話。' : '還沒有對話紀錄。'}</p>`;
  if (list.length > shown.length) h += `<div class="pad"><button class="btn" data-act="claude-more">再顯示 20 筆（共 ${list.length} 筆）</button></div>`;
  setHTML($('#claude-list'), h);
}

async function openSession(project, id) {
  const s = S.claude.sessions.find(x => x.id === id && x.project === project);
  if (!s) return;
  openDlg(s.title, '<p class="muted">載入中…</p>');
  try {
    const d = await api(`/api/claude/sessions/${encodeURIComponent(project)}/${encodeURIComponent(id)}`);
    const meta = `<p class="muted small" style="margin-bottom:14px">${esc(s.cwd || '')}・${s.n_prompts} 次提問・輸出約 ${kfmt(s.out_tokens)} tokens
        ${d.truncated ? '・只顯示最後一段' : ''}</p>`;
    const body = d.messages.map(m => `<div class="msg ${m.role}">
        <div class="who">${m.role === 'user' ? '你' : 'Claude'}${m.ts ? '・' + clock(Date.parse(m.ts)) : ''}</div>
        <div class="text">${esc(m.text)}</div></div>`).join('') || '<p class="muted">這段範圍內沒有文字訊息。</p>';
    const foot = (s.live ? `<span class="note muted small">這個對話正開著，請直接到 Claude 視窗繼續。</span>` : '')
      + (d.command ? `<button class="btn" data-act="copy" data-text="${esc(d.command)}">複製接續指令</button>` : '')
      + (d.command && !s.live ? `<button class="btn primary" data-act="session-resume" data-project="${esc(project)}" data-id="${esc(id)}">在終端機接續</button>` : '');
    openDlg(s.title, meta + body, foot);
    const b = $('#dlg-body'); b.scrollTop = b.scrollHeight;
  } catch (e) {
    openDlg(s.title, `<p>讀取失敗：${esc(e.message)}</p>`);
  }
}

// ---------- 執行紀錄 ----------
function runResult(r) {
  if (r.running) return '<span class="badge on"><span class="dot ok pulse"></span>執行中</span>';
  if (r.exit_code === 0) return '<span class="badge">成功</span>';
  if (r.exit_code === null) return '<span class="badge">已結束（結果未知）</span>';
  return `<span class="badge bad">失敗（${r.exit_code}）</span>`;
}
function renderRuns() {
  if (!S.runs.length) { setHTML($('#runs'), '<p class="muted pad small">從這裡執行過的工作會列在這裡，可以查看輸出或再執行一次。</p>'); return; }
  setHTML($('#runs'), `<div class="table-wrap"><table>
    <tr><th>時間</th><th>工作</th><th>耗時</th><th>結果</th><th></th></tr>
    ${(S.runsAll ? S.runs : S.runs.slice(0, 5)).map(r => `<tr>
      <td class="num">${clock(r.started_at * 1000)}</td>
      <td class="wide">${esc(r.task_name)}・${esc(r.action_label)}</td>
      <td class="num">${r.ended_at ? dur(r.ended_at - r.started_at) : r.running ? dur(Date.now() / 1000 - r.started_at) : '—'}</td>
      <td>${runResult(r)}</td>
      <td><button class="btn sm" data-act="run-show" data-id="${r.id}">輸出</button>
          <button class="btn sm" data-act="task-action" data-task="${esc(r.task_id)}" data-action="${esc(r.action_id)}">再執行</button></td>
    </tr>`).join('')}</table></div>
    ${S.runs.length > 5 ? `<div class="pad"><button class="btn sm" data-act="runs-all">${S.runsAll ? '只顯示最近 5 筆' : `顯示全部（${S.runs.length} 筆）`}</button></div>` : ''}`);
}

// ---------- 錄音整理 ----------
const STAGE = { convert: '轉檔中', transcribe: '轉錄中', summarize: '整理中' };
const lsGet = k => { try { return localStorage.getItem(k); } catch { return null; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch { /* 無法儲存就算了 */ } };
const hms = sec => {
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60, p = n => String(n).padStart(2, '0');
  return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
};

/** 很小的 Markdown 顯示：標題、條列、粗體、勾選框；[12:34] 變成可以點的時間。先跳脫再加標籤。 */
function md(text) {
  const inline = s => s
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]/g, (m, a, b, c) =>
      `<button class="ts" data-act="seek" data-t="${c === undefined ? a * 60 + +b : a * 3600 + b * 60 + +c}">${m}</button>`);
  let html = '', list = false;
  const close = () => { if (list) { html += '</ul>'; list = false; } };
  for (const raw of esc(text).split('\n')) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^#{1,6}\s+(.*)/))) { close(); html += `<h4>${inline(m[1])}</h4>`; }
    else if ((m = line.match(/^\s*[-*]\s+(.*)/))) {
      if (!list) { html += '<ul>'; list = true; }
      html += `<li>${inline(m[1].replace(/^\[ \]\s*/, '☐ ').replace(/^\[x\]\s*/i, '☑ '))}</li>`;
    }
    else if (!line.trim()) close();
    else { close(); html += `<p>${inline(line)}</p>`; }
  }
  close();
  return html;
}

const recModelLabel = m => m.name + (m.capabilities.includes('thinking') ? '（會思考，較慢）' : '');
const CLAUDE_LABEL = 'Claude（較好）';
const modelName = v => v === 'claude' ? 'Claude' : v;

/** 整理用模型的選項：Claude（有安裝 Claude Code 時）＋本機的 Ollama 模型 */
function modelOptions(selected) {
  const o = S.ollama ? S.ollama.models : [];
  return (S.recs && S.recs.claude ? `<option value="claude" ${selected === 'claude' ? 'selected' : ''}>${CLAUDE_LABEL}</option>` : '')
    + o.map(m => `<option value="${esc(m.name)}" ${m.name === selected ? 'selected' : ''}>${esc(recModelLabel(m))}</option>`).join('');
}

const asrOptions = selected => ((S.recs && S.recs.asr_models) || [])
  .map(a => `<option value="${esc(a.id)}" title="${esc(a.hint || '')}" ${a.id === selected ? 'selected' : ''}>${esc(a.label)}</option>`).join('');
const asrName = id => ({ breeze: 'Breeze', turbo: 'turbo' }[id] || id);

function syncRecModel() {
  const asel = $('#rec-asr');
  if (S.recs && asel.dataset.n !== String((S.recs.asr_models || []).length)) {
    asel.innerHTML = asrOptions(lsGet('rec-asr') || S.recs.asr);
    asel.dataset.n = String((S.recs.asr_models || []).length);
    asel.closest('label').hidden = (S.recs.asr_models || []).length < 2;
    asel.title = (S.recs.asr_models || []).map(a => `${a.label}：${a.hint}`).join('\n');
  }
  const sel = $('#rec-model'), o = S.ollama;
  if (!o || !S.recs) return;
  const names = (S.recs.claude ? 'claude|' : '') + o.models.map(m => m.name).join('|');
  if (sel.dataset.names !== names) {
    const cur = sel.value;
    sel.innerHTML = modelOptions(cur);
    sel.dataset.names = names;
    if (cur) sel.value = cur;
  }
  if (!sel.dataset.init && (o.models.length || S.recs.claude)) {
    // 設定檔的預設改了的話，以新的預設為準（不沿用之前在這個瀏覽器選過的）
    const def = S.recs.summary_model, saved = lsGet('rec-model');
    const want = saved && lsGet('rec-model-def') === def ? saved : def;
    lsSet('rec-model-def', def);
    if ([...sel.options].some(x => x.value === want)) sel.value = want;
    const lang = lsGet('rec-lang') || S.recs.language;
    if ([...$('#rec-lang').options].some(x => x.value === lang)) $('#rec-lang').value = lang;
    sel.dataset.init = '1';
  }
}

/** 有其他工作在用 GPU 時，問要現在開始還是等它結束。回傳 null＝取消，否則 { hold }。 */
async function askWhen() {
  const running = S.tasks.filter(t => t.running).map(t => `「${t.name}」`);
  if (!running.length) return { hold: false };
  const ok = await ask('現在開始，還是等其他工作結束？', '', '確定', false, `
    <p style="margin-bottom:12px">${esc(running.join('、'))}正在執行。同時轉錄會互相搶 GPU 和記憶體，兩邊都會慢很多。</p>
    <div class="field"><label for="when-sel">什麼時候開始</label>
      <select id="when-sel" data-input="when">
        <option value="hold">等其他工作結束後自動開始</option>
        <option value="now">現在就開始</option></select></div>`);
  return ok ? { hold: dlgValues.when === 'hold' } : null;
}

function recBadge(r) {
  if (r.status === 'queued' && r.waiting_for && r.waiting_for.length) return `<span class="badge" title="等 ${esc(r.waiting_for.join('、'))} 結束">等其他工作結束</span>`;
  if (r.status === 'queued') return '<span class="badge">排隊中</span>';
  if (r.status === 'running') {
    const label = (STAGE[r.stage] || '準備中') + (r.progress != null ? ` ${r.progress}%` : '') + (r.detail ? `・${esc(r.detail)}` : '');
    return `<span class="badge on"><span class="dot ok pulse"></span>${label}</span>`;
  }
  if (r.status === 'error') return '<span class="badge bad">失敗</span>';
  if (r.status === 'cancelled') return '<span class="badge">已取消</span>';
  if (r.status === 'done' && r.truncated) return '<span class="badge bad" title="模型的輸出額度用完，摘要可能不完整">摘要不完整</span>';
  if (r.status === 'done' && r.summary_stale) return '<span class="badge" title="逐字稿改過了，摘要還是舊的">摘要待更新</span>';
  return '';
}

function renderRecs() {
  const R = S.recs;
  if (!R) return;
  setHTML($('#rec-banner'), R.ready ? '' : `<div class="banner">還不能轉錄：${esc(R.problems.join('、'))}</div>`);
  syncRecModel();
  const html = R.recordings.map(r => {
    const active = r.status === 'running' || r.status === 'queued';
    return `<div class="rec" data-act="rec-open" data-id="${esc(r.id)}">
      <div class="grow">
        <div class="title">${esc(r.title || r.name)}</div>
        <div class="meta">${r.title ? `<span>${esc(r.name)}</span>` : ''}
          ${r.duration ? `<span>長度 ${hms(r.duration)}</span>` : ''}<span>${rel(r.created * 1000)}</span>
          ${r.status === 'done' && r.summary_model ? `<span>${esc(modelName(r.summary_model))}${r.summary_edited ? '・手動修改過' : ''}</span>` : ''}</div>
        ${r.status === 'error' ? `<div class="err">${esc(r.error || '')}</div>` : ''}
        ${r.status === 'running' && r.progress != null ? `<div class="bar"><i style="width:${r.progress}%"></i></div>` : ''}
      </div>
      ${recBadge(r)}
      ${r.status === 'queued' && r.waiting_for && r.waiting_for.length ? `<button class="btn sm" data-act="rec-now" data-id="${esc(r.id)}">現在開始</button>` : ''}
      ${active ? `<button class="btn sm" data-act="rec-cancel" data-id="${esc(r.id)}">取消</button>` : ''}
      ${r.status === 'error' || r.status === 'cancelled' ? `<button class="btn sm" data-act="rec-retry" data-id="${esc(r.id)}">重試</button>` : ''}
    </div>`;
  }).join('');
  setHTML($('#rec-list'), html || '<p class="muted pad small">還沒有錄音。拖一個音檔或影片進來，會依序轉成逐字稿、整理出摘要、決議與待辦事項。</p>');
}

async function uploadRecordings(files) {
  files = [...files];
  if (!files.length) return;
  if (!S.recs || !S.recs.ready) { toast('還不能轉錄：' + ((S.recs && S.recs.problems.join('、')) || '控制台沒有回應'), true); return; }
  const model = $('#rec-model').value, lang = $('#rec-lang').value, asr = $('#rec-asr').value;
  if (model && model !== 'claude' && !await memoryGuard(model, `要用 ${model} 整理錄音嗎？`, '仍要使用')) return;
  const when = await askWhen();
  if (!when) return;
  lsSet('rec-model', model); lsSet('rec-lang', lang); lsSet('rec-asr', asr);
  for (const f of files) {
    try {
      const q = new URLSearchParams({ name: f.name, model, language: lang, hold: when.hold, asr });
      const r = await fetch('/api/recordings?' + q, {
        method: 'POST', body: f,
        headers: { 'X-Local-Controller': '1', 'Content-Type': 'application/octet-stream' },
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      toast(`已加入：${f.name}`);
    } catch (e) {
      toast(`${f.name}：${e.message}`, true);
    }
  }
  safe(loadRecordings);
}

// ---------- 錄音詳細：摘要／逐字稿，可以手動修改 ----------
const parseHms = v => {
  const p = String(v).trim().split(':').map(Number);
  if (!p.length || p.some(isNaN)) return NaN;
  return p.reduce((a, x) => a * 60 + x, 0);
};
const recBusy = d => d.status === 'running' || d.status === 'queued';

function recPane(tab) {
  const d = S.rec, pane = $('#rec-pane');
  if (!d || !pane) return;
  S.recTab = tab;
  document.querySelectorAll('#dlg-body .tab').forEach(t => t.classList.toggle('on', t.dataset.tab === tab));
  const busy = recBusy(d);
  const lock = busy ? '<span class="muted small">處理中，完成後才能修改</span>' : '';
  if (tab === 'summary') {
    if (S.recEdit === 'summary') {
      pane.innerHTML = `<div class="field"><label for="rec-title-in">標題</label><input type="text" id="rec-title-in" value="${esc(d.title || '')}"></div>
        <div class="field"><label for="rec-sum-in">摘要（Markdown：## 標題、- 條列、- [ ] 待辦）</label>
          <textarea id="rec-sum-in" class="edit-area" rows="18">${esc(d.summary || '')}</textarea></div>
        <div class="row"><button class="btn primary" data-act="rec-save-summary">儲存</button><button class="btn" data-act="rec-edit-cancel">取消</button></div>`;
      return;
    }
    const stale = d.summary_stale ? `<div class="notice">逐字稿改過了，這份摘要還是舊的。
        <button class="btn sm primary" data-act="rec-claude">用 Claude 重新整理</button>
        <button class="btn sm" data-act="rec-redo" data-id="${esc(d.id)}">用其他模型…</button></div>` : '';
    const src = d.summary ? `<span class="muted small">由 ${esc(modelName(d.summary_model || '?'))} 整理${d.summary_edited ? '・手動修改過' : ''}</span>` : '';
    pane.innerHTML = `<div class="pane-tools">${src}${lock || (d.summary ? '<button class="btn sm" data-act="rec-edit" data-what="summary">✎ 編輯摘要</button>' : '')}</div>${stale}`
      + (d.summary ? `<div class="md">${md(d.summary)}</div>`
        : `<p class="muted">${d.status === 'error' ? '處理失敗：' + esc(d.error || '') : d.status === 'done' ? '沒有摘要。' : '還在處理中，完成後這裡會出現摘要。'}</p>`);
    return;
  }
  const segs = d.segments || [];
  const hours = segs.length && segs[segs.length - 1].end >= 3600;
  const ts = s => hours ? hms(s).padStart(7, '0') : hms(s);
  if (S.recEdit === 'transcript') {
    pane.innerHTML = `<p class="muted small" style="margin-bottom:8px">直接修改文字；清空的句子會刪掉。時間不變，點時間可以聽那一句。</p>
      <div class="seg-edit">${segs.map((s, i) => `<div class="seg"><button class="ts" data-act="seek" data-t="${s.start}">${ts(s.start)}</button>
        <textarea data-i="${i}" rows="1">${esc(s.text)}</textarea></div>`).join('')}</div>
      <div class="row sticky-save"><button class="btn primary" data-act="rec-save-transcript">儲存逐字稿</button><button class="btn" data-act="rec-edit-cancel">取消</button></div>`;
    pane.querySelectorAll('textarea').forEach(t => { t.style.height = 'auto'; t.style.height = t.scrollHeight + 2 + 'px'; });
    pane.addEventListener('input', e => { if (e.target.tagName === 'TEXTAREA') { e.target.style.height = 'auto'; e.target.style.height = e.target.scrollHeight + 2 + 'px'; } });
    return;
  }
  const fixed = (d.fixed_ranges || []).map(r => `${hms(r.start)}–${hms(r.end)}（${{ en: '英文', zh: '中文', ja: '日文' }[r.lang] || r.lang}）`);
  pane.innerHTML = `<div class="pane-tools">${fixed.length ? `<span class="muted small">已重新辨識：${esc(fixed.join('、'))}</span>` : '<span></span>'}
      ${lock || (segs.length ? `<span><button class="btn sm" data-act="rec-range">重新辨識一段…</button>
        <button class="btn sm" data-act="rec-edit" data-what="transcript">✎ 編輯逐字稿</button></span>` : '')}</div>`
    + (segs.length ? segs.map(s => `<div class="seg"><button class="ts" data-act="seek" data-t="${s.start}">${ts(s.start)}</button><span>${esc(s.text)}</span></div>`).join('')
      : '<p class="muted">還沒有逐字稿。</p>');
}

async function openRecording(id, tab = 'summary') {
  let d;
  try { d = await api('/api/recordings/' + id); } catch (e) { toast(e.message, true); return; }
  S.rec = d; S.recEdit = null;
  const base = '/api/recordings/' + encodeURIComponent(id);
  const info = [d.title ? d.name : null, d.duration ? '長度 ' + hms(d.duration) : null,
    d.transcribe_sec ? '轉錄花了 ' + dur(d.transcribe_sec) + (d.asr ? `（${asrName(d.asr)}）` : '') : null,
    d.auto_fixed ? `自動修正 ${d.auto_fixed} 處` : null,
    d.summarize_sec ? '整理花了 ' + dur(d.summarize_sec) : null].filter(Boolean).map(esc).join('・');
  const audioPos = $('#rec-audio') && dlg.open ? $('#rec-audio').currentTime : 0;
  const body = `<p class="muted small" style="margin-bottom:10px">${info}</p>
    ${d.has_audio ? `<audio id="rec-audio" controls preload="metadata" src="${base}/file/audio"></audio>` : ''}
    <div class="tabs"><button class="tab" data-act="rec-tab" data-tab="summary">摘要</button>
      <button class="tab" data-act="rec-tab" data-tab="transcript">逐字稿</button></div>
    <div id="rec-pane"></div>`;
  const busy = recBusy(d);
  const foot = `<button class="btn danger" data-act="rec-delete" data-id="${esc(id)}" style="margin-right:auto">刪除</button>
    ${busy ? '' : `<button class="btn" data-act="rec-redo" data-id="${esc(id)}">重新整理…</button>`}
    ${busy || !d.segments || !S.recs.claude ? '' : `<button class="btn" data-act="rec-claude" title="把逐字稿傳給 Claude 整理摘要（會用到你 Claude 方案的用量）">✦ 用 Claude 整理</button>`}
    ${d.segments ? `<a class="btn" href="${base}/file/transcript" download>下載逐字稿</a>` : ''}
    ${d.summary && /##\s*待辦[^\n]*\n+\s*[-*]\s*\[ \]/.test(d.summary) ? `<button class="btn" data-act="rec-todos" data-id="${esc(id)}">加入待辦</button>` : ''}
    ${d.summary ? `<a class="btn" href="${base}/file/summary" download>下載摘要</a>
       <button class="btn primary" data-act="rec-copy">複製摘要</button>` : ''}`;
  openDlg(d.title || d.name, body, foot, true);
  if (audioPos && $('#rec-audio')) $('#rec-audio').addEventListener('loadedmetadata', () => { $('#rec-audio').currentTime = audioPos; }, { once: true });
  recPane(tab);
}

async function sendToClaude(id) {
  const ok = await ask('用 Claude 整理摘要？', '', '傳給 Claude', false,
    `<p>會把這筆錄音的<strong>逐字稿</strong>（不含音檔）傳給 Anthropic 的 Claude 整理成摘要、重點、決議、待辦和待確認，大約 1 分鐘。</p>
     <p class="muted small" style="margin-top:8px">會用到你 Claude 方案的用量（每次約一般對話一兩則的量）。原本的摘要會被取代；之後也可以再手動修改。</p>`);
  if (!ok) return;
  try {
    await api(`/api/recordings/${encodeURIComponent(id)}/redo`, { start_from: 'summarize', model: 'claude' });
    toast('已傳給 Claude 整理，完成後摘要會自動更新');
    closeDlg('');
  } catch (e) { toast(e.message, true); }
  safe(loadRecordings);
}

// ---------- 錄音：詞彙與修正 ----------
const VOCAB_STATUS = { on: '自動套用', pending: '待確認', off: '停用' };
function vocabRowHTML(c = { from: '', to: '', count: 0, status: 'on' }) {
  return `<div class="vocab-row ${c.status === 'off' ? 'is-off' : ''}" data-vrow>
    <input type="text" data-k="from" value="${esc(c.from)}" placeholder="辨識成" aria-label="辨識成">
    <span class="muted">→</span>
    <input type="text" data-k="to" value="${esc(c.to)}" placeholder="改成" aria-label="改成">
    <span class="cnt muted small" data-tip="${esc('你手動改過幾次')}">${c.count || 0} 次</span>
    <select data-k="status" aria-label="狀態">${Object.entries(VOCAB_STATUS).map(([k, l]) =>
      `<option value="${k}" ${k === c.status ? 'selected' : ''}>${l}</option>`).join('')}</select>
    <button class="icon" data-act="vocab-del" title="刪除這組" aria-label="刪除">🗑</button>
  </div>`;
}
async function vocabDialog() {
  let v;
  try { v = await api('/api/recordings/vocab'); } catch (e) { toast(e.message, true); return; }
  const used = v.prompt.reduce((n, t) => n + t.length + 1, 0);
  const all = v.glossary.length + v.terms.length;
  const cs = [...v.corrections].sort((a, b) => ({ pending: 0, on: 1, off: 2 }[a.status] - { pending: 0, on: 1, off: 2 }[b.status]) || (b.updated || 0) - (a.updated || 0));
  const mins = Math.round(v.verified.seconds / 60);
  openDlg('詞彙與修正', `
    <div class="vocab-sec">
      <h3>專有名詞</h3>
      <p class="muted small">一行一個（人名、公司、科目、術語）。轉錄時會提示給語音模型，整理摘要時也會告訴模型正確寫法。常用的放前面：提示有長度上限，放不下的只用在摘要。</p>
      <textarea id="vocab-terms" placeholder="例如：&#10;理賠&#10;ALTAM&#10;實發金額">${esc(v.terms.join('\n'))}</textarea>
      ${v.glossary.length ? `<p class="muted small">另外 config/recordings.json 的 glossary 也有：${esc(v.glossary.join('、'))}</p>` : ''}
      <div class="vocab-meter small muted"><span>轉錄提示已用 ${used} / ${v.prompt_max} 字</span>
        <span class="mini use ${used > v.prompt_max * .9 ? 'warn' : ''}"><i style="width:${Math.min(100, used / v.prompt_max * 100).toFixed(0)}%"></i></span>
        ${v.prompt.length < all ? `<span>（${all - v.prompt.length} 個放不下，只用在摘要）</span>` : ''}</div>
    </div>
    <div class="vocab-sec">
      <h3>自動修正</h3>
      <p class="muted small">你手動修改逐字稿時，系統會記下「辨識成 X → 其實是 Y」。同一組改過 ${v.auto_on} 次就變成「自動套用」，之後轉錄完直接換掉；
        如果你把自動修正改回去，那組會自動停用。只改一兩個字的會帶上前後的字，避免誤換。</p>
      <div class="vocab-rows" id="vocab-rows">
        ${cs.length ? `<div class="vocab-row vocab-head small"><span>辨識成</span><span></span><span>改成</span><span class="cnt">次數</span><span>狀態</span><span></span></div>` : ''}
        ${cs.map(vocabRowHTML).join('') || '<p class="muted small" id="vocab-empty">還沒有學到修正。修改逐字稿並儲存後就會出現在這裡，也可以自己新增。</p>'}
      </div>
      <button class="btn sm" data-act="vocab-add" style="margin-top:8px">＋ 新增一組</button>
    </div>
    <div class="vocab-sec">
      <h3>人工校正過的錄音</h3>
      <p class="small">${v.verified.count} 筆、約 ${mins} 分鐘。
        <span class="muted">每筆第一次手動修改前的原稿會另外保留，將來累積到幾個小時，可以評估要不要用來微調語音模型。</span></p>
    </div>`,
    `<button class="btn" data-act="dlg-close">取消</button><button class="btn primary" data-act="vocab-save">儲存</button>`, true);
}
async function saveVocab() {
  const terms = $('#vocab-terms').value.split('\n').map(x => x.trim()).filter(Boolean);
  const corrections = [...document.querySelectorAll('#vocab-rows [data-vrow]')].map(r => ({
    from: r.querySelector('[data-k=from]').value.trim(), to: r.querySelector('[data-k=to]').value.trim(),
    status: r.querySelector('[data-k=status]').value })).filter(c => c.from || c.to);
  const bad = corrections.find(c => !c.from || !c.to || c.from === c.to);
  if (bad) { toast('每一組修正都要填「辨識成」和「改成」，而且兩邊不能一樣', true); return; }
  try {
    await api('/api/recordings/vocab', { terms, corrections });
    toast('已儲存，下一次轉錄開始生效');
    closeDlg('');
  } catch (e) { toast(e.message, true); }
}

async function rangeDialog(id) {
  const d = S.rec, a = $('#rec-audio');
  const cur = a ? Math.floor(a.currentTime) : 0;
  const ok = await ask('重新辨識一段', '', '開始重新辨識', false, `
    <p style="margin-bottom:12px">適合用在<strong>某一段說的是另一種語言</strong>（例如中文錄音裡夾了一段英文，被轉成意思不通的中文）。
      這段會用指定的語言重新轉錄，換掉原本的逐字稿；其他部分不變。</p>
    <div class="field-row">
      <div class="field"><label for="rg-start">開始（分:秒）</label>
        <div class="row" style="margin:0"><input type="text" id="rg-start" data-input="start" value="${hms(cur)}"><button class="btn sm" data-act="rec-use-time" data-target="rg-start" title="用播放器目前的位置">目前位置</button></div></div>
      <div class="field"><label for="rg-end">結束（分:秒）</label>
        <div class="row" style="margin:0"><input type="text" id="rg-end" data-input="end" value="${hms(Math.min(cur + 60, d.duration || cur + 60))}"><button class="btn sm" data-act="rec-use-time" data-target="rg-end" title="用播放器目前的位置">目前位置</button></div></div>
      <div class="field"><label for="rg-lang">這段的語言</label>
        <select id="rg-lang" data-input="lang"><option value="en">英文</option><option value="zh">中文</option><option value="ja">日文</option><option value="auto">自動偵測</option></select></div>
    </div>
    <p class="muted small">提示：先在上面的播放器找到這段的開頭和結尾，按「目前位置」帶入。重新辨識完，摘要會標成「待更新」，可以再用 Claude 整理一次。</p>`);
  if (!ok) return;
  const v = dlgValues, start = parseHms(v.start), end = parseHms(v.end);
  if (!(start >= 0) || !(end > start)) { toast('時間格式不對，或結束時間沒有比開始晚（例如 12:30 到 14:05）', true); return; }
  const when = await askWhen();
  if (!when) return;
  try {
    await api(`/api/recordings/${encodeURIComponent(id)}/redo`, { start_from: 'range', range: { start, end, lang: v.lang }, hold: when.hold, asr: S.recs.asr });
    toast(`已排入佇列：重新辨識 ${hms(start)}–${hms(end)}`);
    closeDlg('');
  } catch (e) { toast(e.message, true); }
  safe(loadRecordings);
}

async function redoRecording(id) {
  const r = S.recs.recordings.find(x => x.id === id);
  if (!r) return;
  const sel = r.truncated ? S.recs.summary_model : r.summary_model || $('#rec-model').value;
  const ok = await ask(`重新整理「${r.title || r.name}」`, '', '開始', false, `
    <div class="field"><label for="redo-from">要重做的範圍</label>
      <select id="redo-from" data-input="start_from">
        <option value="summarize">只重新整理摘要（沿用現有的逐字稿）</option>
        <option value="transcribe">重新轉錄，再整理摘要（會蓋掉手動修改過的逐字稿）</option></select></div>
    <div class="field" id="redo-asr-field"><label for="redo-asr">轉錄模型（重新轉錄時用）</label>
      <select id="redo-asr" data-input="asr">${asrOptions(r.asr || S.recs.asr)}</select></div>
    <div class="field"><label for="redo-model">整理用的模型</label>
      <select id="redo-model" data-input="model">${modelOptions(sel)}</select></div>
    <p class="muted small">選 Claude 會把逐字稿傳給 Anthropic 整理，並用到你 Claude 方案的用量；本機模型則完全不離開這台電腦。</p>`);
  if (!ok) return;
  const v = dlgValues;
  if (v.start_from !== 'transcribe') delete v.asr;
  if (v.model && v.model !== 'claude' && !await memoryGuard(v.model, `要用 ${v.model} 整理錄音嗎？`, '仍要使用')) return;
  const when = v.start_from === 'summarize' && v.model === 'claude' ? { hold: false } : await askWhen();
  if (!when) return;
  try { await api(`/api/recordings/${encodeURIComponent(id)}/redo`, { ...v, hold: when.hold }); toast('已排入佇列'); }
  catch (e) { toast(e.message, true); }
  safe(loadRecordings);
}

// ---------- 載入資料 ----------
async function loadSystem() { S.sys = await api('/api/system'); renderChips(); }
async function loadOllama() { S.ollama = await api('/api/ollama'); renderOllama(); renderChips(); }
async function loadTasks() {
  const d = await api('/api/tasks');
  S.tasks = d.tasks; S.tasksError = d.error || null;
  await Promise.all(S.tasks.filter(t => S.open.has(t.id) && t.has_log).map(async t => {
    try { S.logs[t.id] = (await api(`/api/tasks/${encodeURIComponent(t.id)}/log`)).text; } catch { /* 下次再試 */ }
  }));
  renderTasks();
}
async function loadClaude() { S.claude = await api('/api/claude/sessions'); renderClaude(); renderChips(); }
async function loadRecordings() { S.recs = await api('/api/recordings'); renderRecs(); updateAiBadge(); }
async function loadRuns() { S.runs = (await api('/api/runs')).runs; renderRuns(); }

const LOOPS = [[loadSystem, 5000], [loadOllama, 5000], [loadTasks, 10000], [loadRecordings, 4000], [loadClaude, 30000], [loadRuns, 15000]];
async function safe(fn) {
  try { await fn(); if (S.offline) { S.offline = false; renderChips(); } }
  catch { if (!S.offline) { S.offline = true; renderChips(); } }
}
for (const [fn, ms] of LOOPS) {
  safe(fn);
  setInterval(() => { if (!document.hidden) safe(fn); }, ms);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) LOOPS.forEach(([fn]) => safe(fn)); });

// ---------- 事件 ----------
document.addEventListener('click', async ev => {
  const el = ev.target.closest('[data-act]');
  if (!el) return;
  const d = el.dataset;
  switch (d.act) {
    case 'dlg-close': closeDlg(''); break;
    case 'dlg-ok':
      dlgValues = Object.fromEntries([...document.querySelectorAll('#dlg-body [data-input]')]
        .map(i => [i.dataset.input, i.type === 'checkbox' ? i.checked : i.value]));
      closeDlg('ok'); break;
    case 'task-action': runAction(d.task, d.action); break;
    case 'task-toggle':
      if (S.open.has(d.task)) S.open.delete(d.task); else S.open.add(d.task);
      renderTasks(); safe(loadTasks); break;
    case 'pick':
      try { const r = await api('/api/pick', { kind: d.kind }); if (r.path) document.getElementById(d.target).value = r.path; }
      catch (e) { toast(e.message, true); }
      break;
    case 'ollama-load': ollamaOp('load', d.model); break;
    case 'ollama-unload': ollamaOp('unload', d.model); break;
    case 'ollama-delete': ollamaOp('delete', d.model); break;
    case 'ollama-start':
      try { await api('/api/ollama/start', {}); toast('已送出啟動指令，幾秒後會更新狀態'); setTimeout(() => safe(loadOllama), 2500); }
      catch (e) { toast(e.message, true); }
      break;
    case 'test-send': testSend(); break;
    case 'session-open': openSession(d.project, d.id); break;
    case 'session-resume':
      try { await api('/api/claude/resume', { project: d.project, id: d.id }); closeDlg(''); toast('已在終端機開啟這個對話'); }
      catch (e) { toast(e.message, true); }
      break;
    case 'copy':
      try { await navigator.clipboard.writeText(d.text); toast('已複製'); } catch { toast('無法複製，請手動選取', true); }
      break;
    case 'claude-more': S.claudeLimit += 20; renderClaude(); break;
    case 'runs-all': S.runsAll = !S.runsAll; renderRuns(); break;
    case 'rec-pick': $('#rec-file').click(); break;
    case 'rec-open': openRecording(d.id); break;
    case 'rec-tab': S.recEdit = null; recPane(d.tab); break;
    case 'seek': {
      const a = $('#rec-audio');
      if (a) { a.currentTime = +d.t; a.play().catch(() => {}); }
      break;
    }
    case 'rec-copy':
      try { await navigator.clipboard.writeText(`# ${S.rec.title || S.rec.name}\n\n${S.rec.summary}`); toast('已複製摘要'); }
      catch { toast('無法複製，請手動選取', true); }
      break;
    case 'rec-cancel':
      try { await api(`/api/recordings/${encodeURIComponent(d.id)}/cancel`, {}); toast('已取消'); } catch (e) { toast(e.message, true); }
      safe(loadRecordings); break;
    case 'rec-now':
      try { await api(`/api/recordings/${encodeURIComponent(d.id)}/start-now`, {}); toast('輪到就會開始'); } catch (e) { toast(e.message, true); }
      safe(loadRecordings); break;
    case 'rec-retry':
      try { await api(`/api/recordings/${encodeURIComponent(d.id)}/redo`, { start_from: 'summarize' }); toast('已重新排入佇列'); }
      catch (e) { toast(e.message, true); }
      safe(loadRecordings); break;
    case 'rec-redo': redoRecording(d.id || (S.rec && S.rec.id)); break;
    case 'rec-claude': sendToClaude(S.rec.id); break;
    case 'rec-range': rangeDialog(S.rec.id); break;
    case 'rec-vocab': vocabDialog(); break;
    case 'vocab-add': {
      $('#vocab-empty')?.remove();
      const rows = $('#vocab-rows');
      if (!rows.querySelector('.vocab-head')) rows.insertAdjacentHTML('afterbegin', '<div class="vocab-row vocab-head small"><span>辨識成</span><span></span><span>改成</span><span class="cnt">次數</span><span>狀態</span><span></span></div>');
      rows.insertAdjacentHTML('beforeend', vocabRowHTML());
      rows.lastElementChild.querySelector('input').focus();
      rows.scrollTop = rows.scrollHeight;
      break;
    }
    case 'vocab-del': el.closest('[data-vrow]').remove(); break;
    case 'vocab-save': saveVocab(); break;
    case 'rec-use-time': { const a = $('#rec-audio'); if (a) document.getElementById(d.target).value = hms(Math.floor(a.currentTime)); break; }
    case 'rec-edit': S.recEdit = d.what; recPane(d.what); break;
    case 'rec-edit-cancel': S.recEdit = null; recPane(S.recTab); break;
    case 'rec-save-summary':
      try {
        await api(`/api/recordings/${encodeURIComponent(S.rec.id)}/edit/summary`, { summary: $('#rec-sum-in').value, title: $('#rec-title-in').value });
        toast('摘要已儲存'); openRecording(S.rec.id, 'summary'); safe(loadRecordings);
      } catch (e) { toast(e.message, true); }
      break;
    case 'rec-save-transcript': {
      const segs = S.rec.segments.map((x, i) => ({ ...x, text: document.querySelector(`#rec-pane textarea[data-i="${i}"]`).value }));
      try {
        const r = await api(`/api/recordings/${encodeURIComponent(S.rec.id)}/edit/transcript`, { segments: segs });
        const on = r.learned.filter(c => c.status === 'on'), off = r.learned.filter(c => c.status === 'off');
        const note = r.learned.length ? `；學到 ${r.learned.length} 組修正${on.length ? `，${on.map(c => `${c.from}→${c.to}`).join('、')} 之後會自動套用` : ''}${off.length ? `，停用 ${off.length} 組被你改回去的` : ''}` : '';
        toast(`逐字稿已儲存（${r.segments} 句）${note}`); openRecording(S.rec.id, 'transcript'); safe(loadRecordings);
      } catch (e) { toast(e.message, true); }
      break;
    }
    case 'rec-delete':
      if (!await ask('刪除這筆錄音？', '音檔、逐字稿和摘要都會刪除，無法復原。', '刪除', true)) break;
      try { await api(`/api/recordings/${encodeURIComponent(d.id)}/delete`, {}); toast('已刪除'); } catch (e) { toast(e.message, true); }
      safe(loadRecordings); break;
    case 'run-show':
      try { const r = await api('/api/runs/' + d.id); showOutput(`${r.task_name}・${r.action_label}（${clock(r.started_at * 1000)}）`, r.output); }
      catch (e) { toast(e.message, true); }
      break;
  }
});
$('#pull-form').addEventListener('submit', pullModel);
$('#claude-q').addEventListener('input', () => { S.claudeLimit = 8; renderClaude(); });

// 錄音：拖放與選檔
const drop = $('#rec-drop');
['dragenter', 'dragover'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => uploadRecordings(e.dataTransfer.files));
$('#rec-file').addEventListener('change', e => { uploadRecordings(e.target.files); e.target.value = ''; });
// 拖到區塊外面時，不要讓瀏覽器直接開啟那個檔案
['dragover', 'drop'].forEach(t => window.addEventListener(t, e => e.preventDefault()));
// 詞彙與修正：狀態改成「停用」時把那一列變淡
document.addEventListener('change', ev => {
  const s = ev.target.closest('#vocab-rows [data-k=status]');
  if (s) s.closest('[data-vrow]').classList.toggle('is-off', s.value === 'off');
});
