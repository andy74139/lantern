'use strict';
// ---------- 市場分頁：股價、走勢圖（可切區間、開關均線）、AI／財經新聞 ----------
// 只有切到「市場」分頁、而且分頁在前景時才會抓資料（不在看就不輪詢，省電）。
(() => {
  const RANGES = [['1d', '1日'], ['5d', '5日'], ['1mo', '1月'], ['3mo', '3月'], ['6mo', '6月'], ['ytd', '今年'], ['1y', '1年'], ['5y', '5年']];
  // 台灣慣用的均線名稱；顏色跟著均線走（固定順序），不跟著排名
  const MAS = [['ma5', 5, '週線'], ['ma20', 20, '月線'], ['ma60', 60, '季線'], ['ma120', 120, '半年線'], ['ma240', 240, '年線']];
  const M = {
    quotes: null, news: null, data: {}, loading: 0,
    sel: lsGet('mk-sym') || '', range: lsGet('mk-range') || '6mo',
    mas: new Set((lsGet('mk-mas') ?? 'ma20,ma60').split(',').filter(Boolean)),
    hover: null, newsMore: {}, timers: [], started: false,
  };
  if (!RANGES.some(r => r[0] === M.range)) M.range = '6mo';
  const isMarket = () => document.body.dataset.page === 'market';

  // ---- 格式 ----
  const dec = v => (Math.abs(v) < 1 ? 4 : Math.abs(v) >= 1000 ? 1 : 2);
  const num = (v, d = dec(v)) => v == null ? '—' : Number(v).toLocaleString('zh-TW', { minimumFractionDigits: d, maximumFractionDigits: d });
  const sign = v => (v > 0 ? '+' : v < 0 ? '−' : '±');
  const dir = v => (v > 0 ? 'up' : v < 0 ? 'down' : 'flat');
  const arrow = v => (v > 0 ? '▲' : v < 0 ? '▼' : '—');
  const pctTxt = v => v == null ? '' : `${sign(v)}${Math.abs(v).toFixed(2)}%`;
  const fmt = (ts, opts, tz) => new Intl.DateTimeFormat('zh-TW', { ...opts, hour12: false, ...(tz ? { timeZone: tz } : {}) }).format(new Date(ts * 1000));
  const ago = ts => {
    const m = Math.round((Date.now() / 1000 - ts) / 60);
    if (m < 60) return `${Math.max(m, 1)} 分鐘前`;
    if (m < 60 * 24) return `${Math.round(m / 60)} 小時前`;
    return `${Math.round(m / 1440)} 天前`;
  };

  // ---- 報價卡 ----
  function sparkSVG(vals, prev) {
    if (!vals || vals.length < 2) return '';
    const all = prev ? [...vals, prev] : vals, lo = Math.min(...all), hi = Math.max(...all), W = 120, H = 32;
    const y = v => (hi === lo ? H / 2 : H - 2 - (v - lo) / (hi - lo) * (H - 4));
    const d = vals.map((v, i) => `${i ? 'L' : 'M'}${(i / (vals.length - 1) * W).toFixed(1)},${y(v).toFixed(1)}`).join('');
    return `<svg class="mk-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
      ${prev ? `<line x1="0" x2="${W}" y1="${y(prev).toFixed(1)}" y2="${y(prev).toFixed(1)}" class="mk-spark-prev"/>` : ''}
      <path d="${d}" vector-effect="non-scaling-stroke"/></svg>`;
  }
  const allQuotes = () => ((M.quotes && M.quotes.quotes) || []).flatMap(q => (q.ref ? [q, q.ref] : [q]));
  const short = sym => sym.replace(/\.TWO?$/, '');
  // 參考指標：小卡片，放在對應那一檔的下面
  function refHTML(r) {
    const on = r.symbol === M.sel;
    return `<button class="mk-ref ${on ? 'on' : ''}" data-act="mk-sym" data-sym="${esc(r.symbol)}" aria-pressed="${on}" title="參考指標：${esc(r.full_name || r.name)}">
      <span class="mk-ref-tag">參考</span><span class="mk-ref-name">${esc(r.name)}</span>
      ${r.error ? '<span class="muted small">抓不到</span>' : `<span class="mk-ref-price">${num(r.price)}</span><span class="mk-ref-chg ${dir(r.change)}">${arrow(r.change)} ${pctTxt(r.pct)}</span>`}
    </button>`;
  }
  function renderQuotes() {
    const qs = (M.quotes && M.quotes.quotes) || [];
    if (!qs.length) { setHTML($('#mk-quotes'), '<p class="muted small">還沒有設定追蹤的代號（config/market.json）。</p>'); return; }
    if (!allQuotes().some(q => q.symbol === M.sel)) M.sel = qs[0].symbol;
    setHTML($('#mk-quotes'), qs.map(q => `<div class="mk-qcol">${mainHTML(q)}${q.ref ? refHTML(q.ref) : ''}</div>`).join(''));
  }
  function mainHTML(q) {
    {
      const on = q.symbol === M.sel;
      if (q.error) return `<button class="mk-q ${on ? 'on' : ''}" data-act="mk-sym" data-sym="${esc(q.symbol)}" aria-pressed="${on}">
        <div class="mk-q-name">${esc(q.name)}</div><div class="muted small">${esc(q.symbol)}</div><div class="muted small">抓不到報價</div></button>`;
      const when = q.time ? fmt(q.time, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
      return `<button class="mk-q ${on ? 'on' : ''}" data-act="mk-sym" data-sym="${esc(q.symbol)}" aria-pressed="${on}" title="${esc(q.full_name || q.name)}">
        <div class="mk-q-top"><span class="mk-q-name">${esc(q.name)}</span><span class="mk-q-sym">${esc(short(q.symbol))}</span></div>
        <div class="mk-q-mid"><span class="mk-q-price">${num(q.price)}</span><span class="mk-q-cur">${esc(q.currency || '')}</span>${sparkSVG(q.spark, q.prev_close)}</div>
        <div class="mk-q-bot"><span class="mk-q-chg ${dir(q.change)}">${arrow(q.change)} ${q.change == null ? '' : num(Math.abs(q.change))} ${pctTxt(q.pct)}</span>
          <span class="mk-q-time">${q.open ? '<span class="mk-live">盤中</span>' : '收盤'} ${when}</span></div>
      </button>`;
    }
  }

  // ---- 走勢圖 ----
  const key = () => `${M.sel}|${['1d', '5d'].includes(M.range) ? M.range : 'daily'}`;
  function rangeStart(rng) {
    const d = new Date();
    if (rng === 'ytd') return new Date(d.getFullYear(), 0, 1).getTime() / 1000;
    const months = { '1mo': 1, '3mo': 3, '6mo': 6, '1y': 12, '5y': 60 }[rng];
    d.setMonth(d.getMonth() - months);
    return d.getTime() / 1000;
  }
  function series() {
    const d = M.data[key()];
    if (!d) return null;
    const pts = d.points, closes = pts.map(p => p[1]);
    let start = 0;
    if (!d.intraday) { const s = rangeStart(M.range); start = pts.findIndex(p => p[0] >= s); if (start < 0) start = pts.length - 1; }
    const mas = {};
    if (!d.intraday) for (const [id, n] of MAS) {
      if (!M.mas.has(id)) continue;
      const out = new Array(closes.length).fill(null); let sum = 0;
      for (let i = 0; i < closes.length; i++) { sum += closes[i]; if (i >= n) sum -= closes[i - n]; if (i >= n - 1) out[i] = sum / n; }
      mas[id] = out.slice(start);
    }
    return { d, pts: pts.slice(start), mas };
  }
  function niceTicks(lo, hi, n = 5) {
    const span = hi - lo || Math.abs(hi) || 1, raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n) || mag * 10;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  }
  function xLabel(ts, d) {
    if (M.range === '1d') return fmt(ts, { hour: '2-digit', minute: '2-digit' }, d.tz);
    if (M.range === '5d') return fmt(ts, { month: 'numeric', day: 'numeric' }, d.tz);
    if (['1y', '5y'].includes(M.range)) return fmt(ts, { year: '2-digit', month: 'numeric' }, d.tz).replace(/年/, '/').replace(/月/, '');
    return fmt(ts, { month: 'numeric', day: 'numeric' }, d.tz);
  }
  function tipTime(ts, d) {
    return d.intraday ? fmt(ts, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }, d.tz)
      : fmt(ts, { year: 'numeric', month: 'numeric', day: 'numeric', weekday: 'short' }, d.tz);
  }

  function renderControls() {
    setHTML($('#mk-ranges'), RANGES.map(([id, label]) =>
      `<button data-act="mk-range" data-range="${id}" aria-pressed="${id === M.range}">${label}</button>`).join(''));
    const S = series(), intraday = ['1d', '5d'].includes(M.range);
    setHTML($('#mk-mas'), `<span class="mk-key mk-key-price"><i></i>收盤價</span>` + MAS.map(([id, n, label], i) => {
      const on = M.mas.has(id) && !intraday;
      const last = S && S.mas[id] ? S.mas[id][S.mas[id].length - 1] : null;
      return `<button class="mk-ma c${i + 2}" data-act="mk-ma" data-ma="${id}" aria-pressed="${on}" ${intraday ? 'disabled title="1日、5日是盤中走勢，沒有均線"' : `title="${n} 日均線"`}>
        <i></i>${label}${on && last != null ? `<b>${num(last)}</b>` : ''}</button>`;
    }).join(''));
    const q = allQuotes().find(x => x.symbol === M.sel);
    $('#mk-title').textContent = q ? `${q.name}（${short(q.symbol)}）` : '走勢';
  }

  function renderChart() {
    renderControls();
    const box = $('#mk-chart'), S = series();
    const chg = $('#mk-range-chg');
    if (!S || S.pts.length < 2) {
      chg.textContent = '';
      if (!M.loading) setHTML(box, `<p class="muted small mk-empty">${M.err ? esc(M.err) : '沒有資料'}</p>`);
      box.classList.toggle('is-loading', !!M.loading);
      return;
    }
    box.classList.toggle('is-loading', !!M.loading);
    const { d, pts, mas } = S;
    const first = d.intraday && d.prev_close ? d.prev_close : pts[0][1], last = pts[pts.length - 1][1];
    const rc = (last / first - 1) * 100;
    chg.className = `mk-range-chg ${dir(rc)}`;
    chg.textContent = `${RANGES.find(r => r[0] === M.range)[1]} ${arrow(rc)} ${pctTxt(rc)}`;

    const W = Math.max(box.clientWidth, 280), H = W < 560 ? 240 : 340;
    const pad = { l: 8, r: 58, t: 12, b: 26 }, iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    let lo = Infinity, hi = -Infinity;
    const take = v => { if (v != null) { if (v < lo) lo = v; if (v > hi) hi = v; } };
    pts.forEach(p => take(p[1])); Object.values(mas).forEach(a => a.forEach(take));
    if (d.intraday && d.prev_close) take(d.prev_close);
    const padY = (hi - lo) * 0.06 || hi * 0.01 || 1; lo -= padY; hi += padY;
    const n = pts.length, X = i => pad.l + (n === 1 ? iw / 2 : i / (n - 1) * iw), Y = v => pad.t + (hi - v) / (hi - lo) * ih;
    const path = arr => { let s = '', pen = false; arr.forEach((v, i) => { if (v == null) { pen = false; return; } s += `${pen ? 'L' : 'M'}${X(i).toFixed(1)},${Y(v).toFixed(1)}`; pen = true; }); return s; };

    const yt = niceTicks(lo, hi, H < 300 ? 4 : 5);
    const yd = yt.length > 1 ? Math.min(4, Math.max(0, -Math.floor(Math.log10(yt[1] - yt[0] + 1e-12)) + ((yt[1] - yt[0]) / 10 ** Math.floor(Math.log10(yt[1] - yt[0])) % 1 ? 1 : 0))) : 2;
    const xn = Math.max(2, Math.min(7, Math.floor(iw / 110)));
    const xt = [...new Set(Array.from({ length: xn }, (_, k) => Math.round(k / (xn - 1) * (n - 1))))];
    const grid = yt.map(v => `<line class="mk-grid" x1="${pad.l}" x2="${pad.l + iw}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}"/>
      <text class="mk-ylab" x="${W - pad.r + 8}" y="${(Y(v) + 4).toFixed(1)}">${num(v, yd)}</text>`).join('');
    const xl = xt.map((i, k) => `<text class="mk-xlab" x="${X(i).toFixed(1)}" y="${H - 7}" text-anchor="${k === 0 ? 'start' : k === xt.length - 1 ? 'end' : 'middle'}">${esc(xLabel(pts[i][0], d))}</text>`).join('');
    const prevLine = d.intraday && d.prev_close ? `<line class="mk-prev" x1="${pad.l}" x2="${pad.l + iw}" y1="${Y(d.prev_close).toFixed(1)}" y2="${Y(d.prev_close).toFixed(1)}"/>
      <text class="mk-prev-lab" x="${pad.l + 4}" y="${(Y(d.prev_close) - 5).toFixed(1)}">昨收 ${num(d.prev_close)}</text>` : '';
    const maPaths = MAS.map(([id], i) => mas[id] ? `<path class="mk-line c${i + 2}" d="${path(mas[id])}"/>` : '').join('');
    const lastLab = `<rect class="mk-last-bg" x="${W - pad.r + 2}" y="${(Y(last) - 10).toFixed(1)}" width="${pad.r - 4}" height="20" rx="4"/>
      <text class="mk-last" x="${W - pad.r + 8}" y="${(Y(last) + 4).toFixed(1)}">${num(last)}</text>`;

    setHTML(box, `<svg class="mk-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" tabindex="0" role="img"
        aria-label="${esc($('#mk-title').textContent)}走勢，${esc(chg.textContent)}，最新 ${num(last)}。用左右方向鍵查看各點數值。">
      ${grid}${prevLine}${maPaths}<path class="mk-line mk-halo" d="${path(pts.map(p => p[1]))}"/><path class="mk-line c1" d="${path(pts.map(p => p[1]))}"/>${lastLab}${xl}
      <g class="mk-hover" style="display:none"><line class="mk-cross" y1="${pad.t}" y2="${pad.t + ih}"/><g class="mk-dots"></g></g>
      <rect class="mk-hit" x="${pad.l}" y="0" width="${iw}" height="${H}"/></svg>
      <div class="mk-tip" hidden></div>`);

    const svg = box.querySelector('svg'), hov = svg.querySelector('.mk-hover'), tip = box.querySelector('.mk-tip');
    const show = i => {
      if (i == null) { hov.style.display = 'none'; tip.hidden = true; M.hover = null; return; }
      i = Math.max(0, Math.min(n - 1, i)); M.hover = i;
      const x = X(i);
      hov.style.display = '';
      hov.querySelector('.mk-cross').setAttribute('x1', x); hov.querySelector('.mk-cross').setAttribute('x2', x);
      const rows = [['c1', '收盤價', pts[i][1]], ...MAS.map(([id, , label], k) => mas[id] ? [`c${k + 2}`, label, mas[id][i]] : null).filter(Boolean)];
      hov.querySelector('.mk-dots').innerHTML = rows.filter(r => r[2] != null).map(([c, , v]) => `<circle class="mk-dot ${c}" cx="${x}" cy="${Y(v).toFixed(1)}" r="4"/>`).join('');
      const vsFirst = (pts[i][1] / first - 1) * 100;
      tip.innerHTML = '';
      const h = document.createElement('div'); h.className = 'mk-tip-h'; h.textContent = tipTime(pts[i][0], d); tip.append(h);
      for (const [c, label, v] of rows) {
        const r = document.createElement('div'); r.className = 'mk-tip-r';
        r.innerHTML = `<i class="${c}"></i><b></b><span></span>`;
        r.querySelector('b').textContent = v == null ? '—' : num(v);
        r.querySelector('span').textContent = label;
        tip.append(r);
      }
      const f = document.createElement('div'); f.className = `mk-tip-f ${dir(vsFirst)}`;
      f.textContent = `${d.intraday && d.prev_close ? '比昨收' : '比區間起點'} ${pctTxt(vsFirst)}`; tip.append(f);
      tip.hidden = false;
      const bw = box.clientWidth, tw = tip.offsetWidth, sx = x * (svg.clientWidth / W);
      tip.style.left = (sx + 14 + tw > bw ? sx - 14 - tw : sx + 14) + 'px';
    };
    const idxAt = ev => { const r = svg.getBoundingClientRect(); const x = (ev.clientX - r.left) * (W / r.width); return Math.round((x - pad.l) / iw * (n - 1)); };
    svg.addEventListener('pointermove', ev => show(idxAt(ev)));
    svg.addEventListener('pointerleave', () => show(null));
    svg.addEventListener('blur', () => show(null));
    svg.addEventListener('keydown', ev => {
      const step = ev.shiftKey ? 10 : 1;
      if (ev.key === 'ArrowLeft') { ev.preventDefault(); show((M.hover ?? n) - step); }
      else if (ev.key === 'ArrowRight') { ev.preventDefault(); show((M.hover ?? -1) + step); }
      else if (ev.key === 'Escape') show(null);
    });
  }

  // ---- 新聞 ----
  function renderNews() {
    const feeds = (M.news && M.news.feeds) || [];
    if (!feeds.length) { setHTML($('#mk-news'), '<p class="muted small">還沒有設定新聞關鍵字（config/market.json）。</p>'); return; }
    setHTML($('#mk-news'), feeds.map(f => {
      const all = f.items || [], more = M.newsMore[f.key], items = more ? all : all.slice(0, 10);
      return `<section class="card mk-feed" aria-label="${esc(f.title)}新聞">
        <div class="card-head"><h2>${esc(f.title)} 新聞</h2><span class="muted small">Google／Bing 新聞・近 3 天</span></div>
        ${f.error ? `<p class="muted small">${esc(f.error)}${all.length ? '（先顯示上次抓到的）' : ''}</p>` : ''}
        ${(f.featured || []).length ? `<ul class="mk-feat">${f.featured.map(it => `<li><a href="${esc(it.link)}" target="_blank" rel="noopener noreferrer">
          <img src="/api/market/newsimg/${encodeURIComponent(it.img)}" alt="" loading="lazy" onerror="this.remove()">
          <span class="mk-feat-txt"><b>${esc(it.title)}</b>${it.summary ? `<span class="mk-feat-sum">${esc(it.summary)}</span>` : ''}
          <span class="muted small">${esc(it.source)}${it.time ? `・${ago(it.time)}` : ''}</span></span></a></li>`).join('')}</ul>` : ''}
        <ol class="mk-list">${items.map(it => `<li><a href="${esc(it.link)}" target="_blank" rel="noopener noreferrer">${esc(it.title)}</a>
          <div class="muted small">${esc(it.source)}${it.time ? `・${ago(it.time)}` : ''}</div></li>`).join('')}</ol>
        ${all.length > 10 ? `<button class="btn link" data-act="mk-news-more" data-key="${esc(f.key)}">${more ? '收起' : `再看 ${all.length - 10} 則`}</button>` : ''}
      </section>`;
    }).join(''));
  }

  // ---- 載入 ----
  async function loadQuotes() {
    try { M.quotes = await api('/api/market/quotes'); renderQuotes(); } catch (e) { if (!M.quotes) setHTML($('#mk-quotes'), `<p class="muted small">抓不到報價：${esc(e.message)}</p>`); }
  }
  async function loadChart(force = false) {
    if (!M.sel) return;
    const k = key(), rng = ['1d', '5d'].includes(M.range) ? M.range : '6mo';
    if (M.data[k] && !force) { renderChart(); return; }
    M.loading++; M.err = null; renderChart();
    try { M.data[k] = await api(`/api/market/chart/${encodeURIComponent(M.sel)}?range=${rng}`); }
    catch (e) { M.err = '抓不到走勢：' + e.message; }
    M.loading--;
    if (k === key()) renderChart();
  }
  async function loadNews() {
    try { M.news = await api('/api/market/news'); renderNews(); } catch (e) { if (!M.news) setHTML($('#mk-news'), `<p class="muted small">抓不到新聞：${esc(e.message)}</p>`); }
  }

  // 報價和盤中走勢每分鐘、日線每 10 分鐘、新聞每 15 分鐘；只在看「市場」分頁時更新
  let lastDaily = 0, lastNews = 0;
  async function tick(first = false) {
    if (!isMarket() || document.hidden) return;
    await loadQuotes();
    const intraday = ['1d', '5d'].includes(M.range);
    if (intraday && !first) await loadChart(true);
    else if (!intraday && Date.now() - lastDaily > 600000) { lastDaily = Date.now(); await loadChart(true); }
    else await loadChart();
    if (Date.now() - lastNews > 900000) { lastNews = Date.now(); loadNews(); }
  }
  function enter() {
    if (!M.started) { M.started = true; setInterval(() => tick(), 60000); }
    tick(true);
  }
  document.addEventListener('pagechange', e => { if (e.detail === 'market') enter(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && isMarket() && M.started) tick(); });
  if (isMarket()) enter();

  let rz;
  new ResizeObserver(() => { clearTimeout(rz); rz = setTimeout(() => { if (isMarket() && M.data[key()]) renderChart(); }, 120); }).observe($('#mk-chart'));

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-act^="mk-"]');
    if (!el) return;
    const d = el.dataset;
    if (d.act === 'mk-sym') { M.sel = d.sym; lsSet('mk-sym', M.sel); renderQuotes(); loadChart(); }
    else if (d.act === 'mk-range') { M.range = d.range; lsSet('mk-range', M.range); loadChart(); }
    else if (d.act === 'mk-ma') { M.mas.has(d.ma) ? M.mas.delete(d.ma) : M.mas.add(d.ma); lsSet('mk-mas', [...M.mas].join(',')); renderChart(); }
    else if (d.act === 'mk-news-more') { M.newsMore[d.key] = !M.newsMore[d.key]; renderNews(); }
  });
})();
