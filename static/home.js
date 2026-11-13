'use strict';
// 首頁：天氣、行程、待辦事項、Claude 用量、音樂。共用 app.js 的 $、esc、api、toast、setHTML、safe、ask、openDlg 等。

const H = { home: null, todos: null, usage: null, music: [], cur: null, player: null, playerReady: false, counted: null,
            loop: lsGet('music-loop') !== '0', view: lsGet('todo-view') || 'all', listFilter: lsGet('todo-list') || '',
            q: '', dock: 'off', hideMini: lsGet('music-mini') === 'hide' };
const WDN = ['日', '一', '二', '三', '四', '五', '六'];
const pad2 = n => String(n).padStart(2, '0');
const ymd = d => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
const today0 = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d; };
const hm = d => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
const parseYmd = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };

function dayLabel(s, withRel = true) {
  const d = parseYmd(s), diff = Math.round((d - today0()) / 86400000);
  const rel = { 0: '今天', 1: '明天', 2: '後天', [-1]: '昨天' }[diff];
  const base = `${d.getFullYear() !== new Date().getFullYear() ? d.getFullYear() + '/' : ''}${d.getMonth() + 1}/${d.getDate()}（${WDN[d.getDay()]}）`;
  return withRel && rel ? `${rel} ${base}` : base;
}
function untilText(ms) {
  const min = Math.round(ms / 60000);
  if (min < 1) return '馬上開始';
  if (min < 60) return `還有 ${min} 分鐘`;
  const h = Math.floor(min / 60), m = min % 60;
  return `還有 ${h} 小時${m ? ` ${m} 分` : ''}`;
}

// ---------- 標題列：日期、時鐘、現在天氣 ----------
function renderClock() {
  const n = new Date();
  $('#today-date').textContent = `${n.getMonth() + 1} 月 ${n.getDate()} 日 星期${WDN[n.getDay()]}`;
  $('#clock').textContent = hm(n);
}

// ---------- 天氣 ----------
const WX = [
  [[0], '☀️', '晴'], [[1], '🌤️', '大致晴朗'], [[2], '⛅', '多雲'], [[3], '☁️', '陰'],
  [[45, 48], '🌫️', '霧'], [[51, 53, 55], '🌦️', '毛毛雨'], [[56, 57], '🌧️', '凍雨'],
  [[61], '🌧️', '小雨'], [[63], '🌧️', '中雨'], [[65], '🌧️', '大雨'], [[66, 67], '🌧️', '凍雨'],
  [[71, 73, 75, 77], '🌨️', '雪'], [[80], '🌦️', '陣雨'], [[81], '🌧️', '較強陣雨'], [[82], '⛈️', '豪大陣雨'],
  [[85, 86], '🌨️', '陣雪'], [[95], '⛈️', '雷雨'], [[96, 99], '⛈️', '雷雨伴隨冰雹'],
];
const wx = code => { const w = WX.find(([cs]) => cs.includes(code)); return w ? { icon: w[1], text: w[2] } : { icon: '🌡️', text: '—' }; };
const deg = v => v == null ? '—' : Math.round(v) + '°';
function rainNote(p) {
  if (p == null) return '';
  if (p >= 70) return '記得帶傘';
  if (p >= 40) return '可能下雨，建議帶傘';
  return '';
}

function renderNowWeather() {
  const w = H.home && H.home.weather, el = $('#wx-now');
  if (!w || !w.current) { setHTML(el, ''); return; }
  const c = w.current, x = wx(c.code);
  const stale = !w.fetched_at || Date.now() / 1000 - w.fetched_at > 3 * 3600 || w.error;
  el.title = `濕度 ${c.humidity ?? '—'}%${w.fetched_at ? '・' + rel(w.fetched_at * 1000) + '更新' : ''}`;
  setHTML(el, `<span aria-hidden="true">${x.icon}</span><span class="wx-t">${deg(c.temp)}</span>
    <span class="muted">${x.text}・體感 ${deg(c.feels)}・${esc(H.home.place || '')}${stale ? '（離線）' : ''}</span>`);
}

function dayWeather(date, withNote) {
  const w = H.home && H.home.weather;
  const d = w && (w.days || []).find(x => x.date === date);
  if (!d) return '';
  const x = wx(d.code), rain = d.rain_day ?? d.rain, note = withNote ? rainNote(rain) : '';
  return `<div class="day-wx" title="${x.text}・白天降雨機率 ${rain ?? '—'}%"><span aria-hidden="true">${x.icon}</span>
    <span>${deg(d.min)}–${deg(d.max)}</span><span class="rain ${rain >= 40 ? 'hi' : ''}">☂ ${rain ?? '—'}%</span></div>
    ${note ? `<div class="rain-note">${note}</div>` : ''}`;
}

// ---------- 行程（一週橫向） ----------
function eventDays(e) {
  if (e.all_day) {
    const out = [];
    for (let d = parseYmd(e.start); ymd(d) < e.end; d = addDays(d, 1)) out.push(ymd(d));
    return out.length ? out : [e.start];
  }
  const s = new Date(e.start), en = new Date(new Date(e.end || e.start) - 1);
  const out = [];
  for (let d = new Date(s.getFullYear(), s.getMonth(), s.getDate()); d <= en || !out.length; d = addDays(d, 1)) out.push(ymd(d));
  return out;
}

function itemsOn(date) {
  const cal = (H.home && H.home.calendar && H.home.calendar.events) || [];
  const evs = cal.filter(e => eventDays(e).includes(date)).map(e => ({ kind: 'event', e }));
  const tds = ((H.todos && H.todos.todos) || []).filter(t => t.status === 'open' && t.due_date === date && t.due_time)
    .map(t => ({ kind: 'todo', t }));
  const key = x => x.kind === 'event' ? (x.e.all_day ? '' : (eventDays(x.e)[0] < date ? '00:00' : hm(new Date(x.e.start)))) : x.t.due_time;
  return [...evs, ...tds].sort((a, b) => key(a).localeCompare(key(b)));
}

function evHTML(x, date, now) {
  if (x.kind === 'todo') {
    return `<li class="ev todo-ev"><span class="ev-time">${esc(x.t.due_time)}</span>
      <span class="ev-title"><button class="ev-check" data-act="todo-toggle" data-id="${esc(x.t.id)}" aria-label="完成 ${esc(x.t.title)}">☐</button>${esc(x.t.title)}</span></li>`;
  }
  const e = x.e, opt = e.role === 'optional';
  let time = '', past = false;
  if (!e.all_day) {
    const s = new Date(e.start), en = new Date(e.end);
    const startsHere = ymd(s) === date, endsHere = ymd(new Date(en - 1)) === date;
    time = `${startsHere ? hm(s) : '…'}–${endsHere ? hm(en) : '…'}`;
    past = en < now;
  }
  const title = e.link ? `<a href="${esc(e.link)}" target="_blank" rel="noopener">${esc(e.title)}</a>` : esc(e.title);
  return `<li class="ev ${e.all_day ? 'allday' : ''} ${opt ? 'opt' : ''} ${past ? 'past' : ''}" title="${esc(e.title + (e.location ? '・' + e.location : '') + (opt ? '（提醒，不一定參加）' : ''))}">
    ${e.all_day ? '' : `<span class="ev-time">${time}</span>`}
    <span class="ev-title">${title}${opt ? '<span class="tag">提醒</span>' : ''}</span>
    ${e.location && !e.all_day ? `<span class="ev-loc">${esc(e.location)}</span>` : ''}</li>`;
}

function calendarNotice() {
  const c = H.home && H.home.calendar, g = H.home && H.home.google;
  if (!c) return '';
  const bad = (c.calendars || []).filter(x => x.state !== 'ok');
  if (!bad.length) return '';
  if (bad.some(x => x.state === 'need_auth')) {
    return `<div class="notice">${g && g.error ? esc(g.error) : '行事曆要連結 Google 才能讀取。'}
      <button class="btn sm primary" data-act="google-setup">${g && g.connected ? '設定' : '連結 Google'}</button></div>`;
  }
  if (bad.every(x => x.state === 'offline')) return '';
  return `<div class="notice">${bad.map(x => `${esc(x.name)}：${esc(x.error || '讀取失敗')}`).join('<br>')}</div>`;
}

function renderSchedule() {
  const c = H.home && H.home.calendar;
  if (!c) return;
  const now = new Date(), td = ymd(now);
  const offline = (c.calendars || []).some(x => x.state === 'offline');
  const ok = (c.calendars || []).filter(x => x.fetched_at);
  const oldest = ok.length ? Math.min(...ok.map(x => x.fetched_at)) : null;
  $('#cal-meta').textContent = oldest ? `${rel(oldest * 1000)}更新${offline ? '（離線）' : ''}` : '';
  setHTML($('#cal-notice'), calendarNotice());

  const timed = (c.events || []).filter(e => !e.all_day && new Date(e.end) > now).sort((a, b) => a.start.localeCompare(b.start));
  const nxt = timed.find(e => e.role !== 'optional') || timed[0];
  let banner = '';
  if (nxt) {
    const s = new Date(nxt.start), en = new Date(nxt.end);
    banner = s <= now
      ? `<div class="next live"><span class="dot ok pulse"></span>進行中：<strong>${esc(nxt.title)}</strong><span class="muted">・到 ${hm(en)}</span></div>`
      : `<div class="next">下一個：<strong>${esc(nxt.title)}</strong><span class="muted">・${ymd(s) === td ? '' : dayLabel(ymd(s)) + ' '}${hm(s)}・${untilText(s - now)}</span>${nxt.role === 'optional' ? '<span class="tag">提醒</span>' : ''}</div>`;
  }
  setHTML($('#next-event'), banner);

  const open = ((H.todos && H.todos.todos) || []).filter(t => t.status === 'open');
  let h = '';
  for (let i = 0; i < 7; i++) {
    const d = ymd(addDays(today0(), i)), dt = parseYmd(d), wd = dt.getDay();
    const items = itemsOn(d);
    const dayTodos = open.filter(t => t.due_date === d && !t.due_time);
    const name = i === 0 ? '今天' : i === 1 ? '明天' : '週' + WDN[wd];
    h += `<div class="day ${i === 0 ? 'is-today' : ''} ${wd === 0 || wd === 6 ? 'weekend' : ''} ${items.length || dayTodos.length ? '' : 'is-empty'}">
      <div class="day-head"><span class="day-name">${name}</span><span class="day-date">${dt.getMonth() + 1}/${dt.getDate()}${i < 2 ? `（${WDN[wd]}）` : ''}</span></div>
      ${dayWeather(d, i < 2)}
      ${items.length ? `<ul class="evs">${items.map(x => evHTML(x, d, now)).join('')}</ul>` : ''}
      ${dayTodos.length ? `<ul class="day-todos">${dayTodos.map(t => `<li><button class="ev-check" data-act="todo-toggle" data-id="${esc(t.id)}" aria-label="完成 ${esc(t.title)}">☐</button><span>${esc(t.title)}</span></li>`).join('')}</ul>` : ''}
      ${items.length || dayTodos.length ? '' : '<p class="day-empty">沒有行程</p>'}
    </div>`;
  }
  setHTML($('#week'), h);
}

// ---------- 待辦：快速輸入的日期解析 ----------
const CN = { 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 日: 0, 天: 0 };
function parseQuick(text) {
  let s = ' ' + text + ' ', date = null, time = null, repeat = null, pm = null;
  const t0 = today0();
  const take = (re, fn) => { const m = s.match(re); if (m && fn(m) !== false) s = s.replace(m[0], ' '); };
  const nextWd = (wd, nextWeek) => {
    if (nextWeek) {                                    // 下週X：下週一起算的那一週
      const mon = addDays(t0, ((8 - t0.getDay()) % 7) || 7);
      return addDays(mon, (wd + 6) % 7);
    }
    return addDays(t0, (wd - t0.getDay() + 7) % 7);    // 這週X：今天以後最近的那天（含今天）
  };

  take(/每天|每日/, () => { repeat = 'daily'; });
  take(/平日|每個?工作天/, () => { repeat = 'weekdays'; });
  take(/每(?:週|周|星期|禮拜)([一二三四五六日天])?/, m => { repeat = 'weekly'; if (m[1]) date = nextWd(CN[m[1]]); });
  take(/每個?月\s*(\d{1,2})\s*[號日]?/, m => {
    repeat = 'monthly';
    let d = new Date(t0.getFullYear(), t0.getMonth(), +m[1]);
    if (d < t0) d = new Date(t0.getFullYear(), t0.getMonth() + 1, +m[1]);
    date = d;
  });
  take(/每年/, () => { repeat = 'yearly'; });

  take(/大後天/, () => { date = addDays(t0, 3); });
  take(/後天/, () => { date = addDays(t0, 2); });
  take(/明天|明日|明早|明晚/, m => { date = addDays(t0, 1); if (m[0] === '明晚') pm = true; });
  take(/今天|今日|今晚/, m => { date = t0; if (m[0] === '今晚') pm = true; });
  take(/(下下|下)?(?:週|周|星期|禮拜)([一二三四五六日天])/, m => {
    let d = nextWd(CN[m[2]], !!m[1]);
    if (m[1] === '下下') d = addDays(d, 7);
    date = d;
  });
  take(/(?<!\d)(\d{1,2})\s*[\/月]\s*(\d{1,2})\s*[日號]?(?!\d)/, m => {
    const mo = +m[1], da = +m[2];
    if (mo < 1 || mo > 12 || da < 1 || da > 31) return false;
    let d = new Date(t0.getFullYear(), mo - 1, da);
    if (d < addDays(t0, -60)) d = new Date(t0.getFullYear() + 1, mo - 1, da);   // 兩個月前以上才當成明年
    date = d;
  });
  take(/(早上|上午|中午|下午|傍晚|晚上|凌晨)?\s*(\d{1,2})\s*(?:[:：]\s*(\d{2})|點\s*(?:(\d{1,2})\s*分?|(半))?)/, m => {
    let h = +m[2], mi = m[3] ? +m[3] : m[4] ? +m[4] : m[5] ? 30 : 0;
    if (h > 23 || mi > 59) return false;
    const p = m[1];
    if ((p === '下午' || p === '晚上' || p === '傍晚' || (pm && !p)) && h < 12) h += 12;
    if (p === '中午' && h < 11) h += 12;
    if (p === '凌晨' && h === 12) h = 0;
    if (!p && !pm && h >= 1 && h <= 6) h += 12;        // 沒說上午下午的「3點」通常是下午
    time = `${pad2(h)}:${pad2(mi)}`;
  });
  if (repeat && !date && repeat !== 'weekly') {       // 重複的事從今天（平日：最近的工作天）開始
    date = t0;
    if (repeat === 'weekdays') while (date.getDay() === 0 || date.getDay() === 6) date = addDays(date, 1);
  }
  if (repeat === 'weekly' && !date) date = t0;
  if (time && !date) {                                 // 只有時間：今天還沒過就是今天，否則明天
    const [h, mi] = time.split(':').map(Number), n = new Date();
    date = (h * 60 + mi > n.getHours() * 60 + n.getMinutes()) ? t0 : addDays(t0, 1);
  }
  const title = s.replace(/\s+/g, ' ').replace(/^[\s，,、：:]+|[\s，,、]+$/g, '').trim();
  return { title, due_date: date ? ymd(date) : null, due_time: time, repeat };
}

const REPEAT_LABEL = { daily: '每天', weekdays: '平日', weekly: '每週', monthly: '每月', yearly: '每年' };
function dueLabel(t) {
  if (!t.due_date) return '';
  return dayLabel(t.due_date) + (t.due_time ? ' ' + t.due_time : '');
}

function renderPreview() {
  const v = $('#todo-input').value.trim();
  if (!v) { setHTML($('#todo-preview'), ''); return; }
  const p = parseQuick(v);
  const chips = [];
  if (p.due_date) chips.push(`📅 ${dueLabel(p)}`);
  if (p.repeat) chips.push(`↻ ${REPEAT_LABEL[p.repeat]}`);
  setHTML($('#todo-preview'), chips.length
    ? `<span class="muted">按 Enter 新增「${esc(p.title || '…')}」</span>${chips.map(c => `<span class="chip-s">${esc(c)}</span>`).join('')}`
    : `<span class="muted">按 Enter 新增</span>`);
}

// ---------- 待辦：分頁、清單、搜尋 ----------
const VIEWS = [['focus', '今天'], ['upcoming', '接下來'], ['all', '全部'], ['done', '已完成']];
const lists = () => (H.todos && H.todos.lists) || [];
const effList = t => t.list_id || (lists()[0] || {}).id || '';
const listTitle = id => (lists().find(x => x.id === id) || {}).title || '';
const enc = encodeURIComponent;

function groupOf(t, td) {
  if (t.status === 'done') return 'done';
  if (!t.due_date) return 'none';
  if (t.due_date < td) return 'overdue';
  if (t.due_date === td) return 'today';
  const tm = ymd(addDays(today0(), 1)), wk = ymd(addDays(today0(), 7));
  if (t.due_date === tm) return 'tomorrow';
  return t.due_date <= wk ? 'week' : 'later';
}
const GROUPS = [['overdue', '已過期'], ['today', '今天'], ['tomorrow', '明天'], ['week', '這 7 天'], ['later', '之後'], ['none', '沒有期限']];
const byOrder = (a, b) => (b.pinned - a.pinned) || (a.due_date || '9').localeCompare(b.due_date || '9')
  || (a.due_time || '99').localeCompare(b.due_time || '99') || a.created_at - b.created_at;

function todoHTML(t, showDate) {
  const done = t.status === 'done', td = ymd(new Date());
  const late = !done && t.due_date && t.due_date < td;
  const meta = [];
  if (t.due_date && showDate) meta.push(`<span class="${late ? 'late' : ''}">📅 ${dueLabel(t)}</span>`);
  else if (t.due_time) meta.push(`<span>🕘 ${t.due_time}</span>`);
  if (t.repeat) meta.push(`<span title="重複">↻ ${REPEAT_LABEL[t.repeat]}</span>`);
  if (!H.listFilter && lists().length > 1) meta.push(`<span class="ltag">${esc(listTitle(effList(t)))}</span>`);
  if (t.source && t.source.type === 'recording') meta.push(`<button class="link-s" data-act="rec-open" data-id="${esc(t.source.id)}" title="${esc(t.source.title || '')}">🎙 錄音</button>`);
  if (H.todos.sync.connected && t.dirty) meta.push('<span class="pend" title="還沒同步到 Google Tasks">↑ 待同步</span>');
  const note = t.notes && !(t.source && t.notes.startsWith('來自錄音')) ? t.notes.split('\n')[0] : '';
  return `<li class="td ${done ? 'done' : ''} ${t.pinned ? 'pinned' : ''}">
    <button class="td-check" data-act="todo-toggle" data-id="${esc(t.id)}" role="checkbox" aria-checked="${done}" aria-label="${done ? '取消完成' : '完成'}：${esc(t.title)}">${done ? '☑' : '☐'}</button>
    <div class="td-main" data-act="todo-edit" data-id="${esc(t.id)}">
      <div class="td-title">${t.pinned ? '<span class="pin" aria-label="已釘選">★</span>' : ''}${esc(t.title)}</div>
      ${note ? `<div class="td-note">${esc(note)}</div>` : ''}
      ${meta.length ? `<div class="td-meta">${meta.join('')}</div>` : ''}
    </div>
    <div class="td-quick">
      ${done ? '' : `${t.due_date !== td ? `<button class="btn sm" data-act="todo-when" data-id="${esc(t.id)}" data-when="0">今天</button>` : ''}
      ${t.due_date !== ymd(addDays(today0(), 1)) ? `<button class="btn sm" data-act="todo-when" data-id="${esc(t.id)}" data-when="1">明天</button>` : ''}`}
      <button class="icon more" data-act="todo-menu" data-id="${esc(t.id)}" aria-label="更多動作" aria-haspopup="menu">⋯</button>
    </div>
  </li>`;
}

function renderTodos() {
  if (!H.todos) return;
  const td = ymd(new Date());
  if (H.listFilter && !lists().some(L => L.id === H.listFilter)) H.listFilter = '';
  const all = H.todos.todos.filter(t => !H.listFilter || effList(t) === H.listFilter);
  const open = all.filter(t => t.status === 'open');
  const doneL = all.filter(t => t.status === 'done').sort((a, b) => b.completed_at - a.completed_at);
  const overdue = open.filter(t => t.due_date && t.due_date < td).sort(byOrder);
  const counts = { focus: open.filter(t => t.due_date && t.due_date <= td).length,
                   upcoming: open.filter(t => t.due_date && t.due_date > td).length, all: open.length, done: doneL.length };

  setHTML($('#todo-tabs'), VIEWS.map(([v, l]) => `<button class="tab ${H.view === v && !H.q ? 'on' : ''}" role="tab" aria-selected="${H.view === v}" data-act="todo-view" data-view="${v}">${l}<span class="n">${counts[v] || ''}</span></button>`).join(''));
  setHTML($('#todo-lists'), lists().length > 1
    ? [['', '所有清單'], ...lists().map(L => [L.id, L.title])].map(([id, title]) => `<button class="lchip ${H.listFilter === id ? 'on' : ''}" data-act="todo-list-filter" data-list="${esc(id)}">${esc(title)}</button>`).join('')
    : '');
  const sel = $('#todo-add-list');
  sel.hidden = lists().length < 2;
  const opts = lists().map(L => `<option value="${esc(L.id)}">${esc(L.title)}</option>`).join('');
  if (sel.dataset.opts !== opts) { sel.innerHTML = opts; sel.dataset.opts = opts; }
  if (H.listFilter && document.activeElement !== sel) sel.value = H.listFilter;

  let groups = [], empty = '';
  const q = H.q.trim().toLowerCase();
  if (q) {
    const hits = all.filter(t => (t.title + ' ' + (t.notes || '')).toLowerCase().includes(q)).sort(byOrder);
    groups = [['search', `搜尋「${H.q.trim()}」`, hits, true]];
    empty = '找不到符合的待辦（只搜尋未完成，以及最近 7 天完成的）。';
  } else if (H.view === 'done') {
    groups = [['done', '最近 7 天完成', doneL, true]];
    empty = '最近 7 天沒有完成的項目。';
  } else if (H.view === 'upcoming') {
    const up = open.filter(t => t.due_date && t.due_date > td).sort(byOrder);
    const wk = ymd(addDays(today0(), 7));
    const days = [...new Set(up.filter(t => t.due_date <= wk).map(t => t.due_date))];
    groups = days.map(d => ['d' + d, dayLabel(d), up.filter(t => t.due_date === d), false]);
    groups.push(['later', '之後', up.filter(t => t.due_date > wk), true]);
    empty = '接下來沒有排定日期的待辦。';
  } else if (H.view === 'focus') {
    groups = [['overdue', '已過期', overdue, true], ['today', '今天', open.filter(t => t.due_date === td).sort(byOrder), false]];
    empty = '今天沒有要做的事 🎉';
  } else {
    const by = {};
    for (const t of open) (by[groupOf(t, td)] ||= []).push(t);
    groups = GROUPS.map(([g, l]) => [g, l, (by[g] || []).sort(byOrder), !['today', 'tomorrow'].includes(g)]);
    empty = '沒有待辦事項。在上面輸入一件事，按 Enter 就會加入。';
  }
  groups = groups.filter(g => g[2].length);
  let h = groups.map(([g, label, items, showDate]) => `<div class="td-group g-${g}">
      <h3>${esc(label)}<span class="muted"> ${items.length}</span>
        ${g === 'overdue' ? '<button class="btn link" data-act="todo-overdue-today">全部移到今天</button>' : ''}</h3>
      <ul>${items.map(t => todoHTML(t, showDate)).join('')}</ul></div>`).join('');
  setHTML($('#todo-list'), h || `<p class="muted small empty">${empty}</p>`);

  const tday = open.filter(t => t.due_date === td).length;
  $('#todo-count').textContent = `${open.length} 項` + (tday ? `・今天 ${tday}` : '') + (overdue.length ? `・過期 ${overdue.length}` : '');
  const urgent = tday + overdue.length;
  document.title = urgent ? `(${urgent}) 首頁` : '首頁';
  renderSync();
}

function renderSync() {
  const s = H.todos.sync;
  let h;
  if (!s.connected) {
    h = `<span class="muted">${s.auth_error ? esc(s.auth_error) : '只存在這台電腦，尚未連結 Google Tasks。'}</span>
      <button class="btn link" data-act="google-setup">${s.auth_error ? '重新連結' : '連結 Google'}</button>`;
  } else if (s.state === 'offline') {
    h = `<span class="dot warn"></span><span>離線${s.pending ? `：${s.pending} 項等連線後同步` : '，修改會在連線後同步'}</span>`;
  } else if (s.state === 'error' || s.state === 'need_auth') {
    h = `<span class="dot bad"></span><span>同步失敗：${esc(s.error || '')}</span>
      ${s.state === 'need_auth' ? '<button class="btn link" data-act="google-connect">重新連結</button>' : ''}`;
  } else if (s.pending) {
    h = `<span class="dot"></span><span>同步中…（${s.pending} 項）</span>`;
  } else {
    h = `<span class="dot ok"></span><span class="muted">已和 Google Tasks 同步${s.at ? '・' + rel(s.at * 1000) : ''}</span>`;
  }
  if (s.connected) h += `<button class="btn link" data-act="todo-sync">立即同步</button><button class="btn link" data-act="google-setup">設定</button>`;
  h += '<span class="muted kbd-hint">快捷鍵：N 新增・/ 搜尋</span>';
  setHTML($('#todo-sync'), h);
}

// ---------- 待辦：動作（完成、刪除可以復原） ----------
function toastUndo(msg, undo) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = `<span>${esc(msg)}</span><button class="toast-btn">復原</button>`;
  el.querySelector('button').onclick = async () => {
    el.remove();
    try { await undo(); } catch (e) { toast(e.message, true); }
    safe(loadTodos);
  };
  $('#toasts').append(el);
  setTimeout(() => el.remove(), 7000);
}

async function todoChange(id, data, msg) {
  try {
    await api('/api/todos/' + enc(id), data);
    if (msg) toast(msg);
  } catch (e) { toast(e.message, true); }
  await safe(loadTodos);
}

async function toggleTodo(id) {
  const t = H.todos.todos.find(x => x.id === id);
  if (!t) return;
  const done = t.status !== 'done';
  try {
    const r = await api('/api/todos/' + enc(id), { status: done ? 'done' : 'open' });
    if (done) {
      let msg = `完成「${t.title}」`;
      if (r.next_id) {
        const n = (await api('/api/todos')).todos.find(x => x.id === r.next_id);
        msg += `，下一次：${n ? dueLabel(n) : ''}`;
      }
      toastUndo(msg, async () => {
        await api('/api/todos/' + enc(id), { status: 'open', repeat: t.repeat || null });
        if (r.next_id) await api(`/api/todos/${enc(r.next_id)}/delete`, {});
      });
    }
  } catch (e) { toast(e.message, true); }
  safe(loadTodos);
}

async function deleteTodo(id) {
  const t = H.todos.todos.find(x => x.id === id);
  if (!t) return;
  try { await api(`/api/todos/${enc(id)}/delete`, {}); } catch (e) { toast(e.message, true); return; }
  toastUndo(`已刪除「${t.title}」`, async () => {
    try { await api(`/api/todos/${enc(id)}/restore`, {}); }
    catch {                                  // 已經同步刪掉了：照原本的內容重新建立
      await api('/api/todos', { title: t.title, notes: t.notes, due_date: t.due_date, due_time: t.due_time,
                                repeat: t.repeat, pinned: !!t.pinned, list_id: t.list_id });
    }
  });
  safe(loadTodos);
}

function closeMenu() { const m = $('#td-menu'); if (m) m.remove(); }
function openMenu(btn, id) {
  closeMenu();
  const t = H.todos.todos.find(x => x.id === id);
  if (!t) return;
  const nextMon = ymd(addDays(today0(), ((8 - today0().getDay()) % 7) || 7));
  const m = document.createElement('div');
  m.id = 'td-menu'; m.className = 'menu'; m.setAttribute('role', 'menu');
  m.innerHTML = `
    ${t.status === 'open' ? `
    <button role="menuitem" data-act="todo-set" data-id="${esc(id)}" data-field="due_date" data-value="${ymd(today0())}">今天</button>
    <button role="menuitem" data-act="todo-set" data-id="${esc(id)}" data-field="due_date" data-value="${ymd(addDays(today0(), 1))}">明天</button>
    <button role="menuitem" data-act="todo-set" data-id="${esc(id)}" data-field="due_date" data-value="${nextMon}">下週一（${parseYmd(nextMon).getMonth() + 1}/${parseYmd(nextMon).getDate()}）</button>
    <label class="menu-date">選擇日期<input type="date" data-change="todo-date" data-id="${esc(id)}" value="${esc(t.due_date || '')}"></label>
    ${t.due_date ? `<button role="menuitem" data-act="todo-clear-date" data-id="${esc(id)}">清除日期</button>` : ''}
    <hr>` : ''}
    <button role="menuitem" data-act="todo-set" data-id="${esc(id)}" data-field="pinned" data-value="${t.pinned ? '' : '1'}">${t.pinned ? '取消釘選' : '★ 釘選在最上面'}</button>
    ${lists().length > 1 ? lists().filter(L => L.id !== effList(t)).map(L => `<button role="menuitem" data-act="todo-set" data-id="${esc(id)}" data-field="list_id" data-value="${esc(L.id)}">移到「${esc(L.title)}」</button>`).join('') : ''}
    <button role="menuitem" data-act="todo-edit" data-id="${esc(id)}">編輯…</button>
    <hr>
    <button role="menuitem" class="danger" data-act="todo-del" data-id="${esc(id)}">刪除</button>`;
  document.body.append(m);
  const r = btn.getBoundingClientRect(), w = m.offsetWidth, hgt = m.offsetHeight;
  const left = Math.max(8, Math.min(r.right - w, innerWidth - w - 8));
  const top = r.bottom + 4 + hgt > innerHeight ? r.top - hgt - 4 : r.bottom + 4;
  m.style.left = left + scrollX + 'px';
  m.style.top = Math.max(8, top) + scrollY + 'px';
  m.querySelector('button, input').focus();
}

async function editTodo(id) {
  closeMenu();
  const t = H.todos.todos.find(x => x.id === id);
  if (!t) return;
  const opts = [['', '不重複'], ...Object.entries(REPEAT_LABEL)].map(([v, l]) => `<option value="${v}" ${(t.repeat || '') === v ? 'selected' : ''}>${l}</option>`).join('');
  const listSel = lists().length > 1 ? `<div class="field"><label for="te-list">清單</label><select id="te-list" data-input="list_id">${lists().map(L => `<option value="${esc(L.id)}" ${effList(t) === L.id ? 'selected' : ''}>${esc(L.title)}</option>`).join('')}</select></div>` : '';
  const body = `
    <div class="field"><label for="te-title">待辦事項</label><input type="text" id="te-title" data-input="title" value="${esc(t.title)}"></div>
    <div class="field-row">
      <div class="field"><label for="te-date">日期</label><input type="date" id="te-date" data-input="due_date" value="${esc(t.due_date || '')}"></div>
      <div class="field"><label for="te-time">時間（只存在這台電腦）</label><input type="time" id="te-time" data-input="due_time" value="${esc(t.due_time || '')}"></div>
      <div class="field"><label for="te-rep">重複</label><select id="te-rep" data-input="repeat">${opts}</select></div>
    </div>
    ${listSel}
    <div class="field"><label for="te-notes">備註</label><textarea id="te-notes" rows="3" data-input="notes">${esc(t.notes || '')}</textarea></div>
    <label class="check"><input type="checkbox" data-input="pinned" ${t.pinned ? 'checked' : ''}> 釘選在最上面</label>
    <p class="muted small" style="margin-top:10px">有時間的待辦，到時會跳出 macOS 通知。</p>`;
  const p = ask('編輯待辦', '', '儲存', false, body);
  $('#dlg-foot').insertAdjacentHTML('afterbegin', `<button class="btn danger" data-act="todo-delete" data-id="${esc(id)}" style="margin-right:auto">刪除</button>`);
  if (!await p) return;
  const v = dlgValues;
  if (!v.title.trim()) { toast('待辦事項不能是空的', true); return; }
  if (v.due_time && !v.due_date) v.due_date = ymd(new Date());
  const data = { title: v.title, notes: v.notes, due_date: v.due_date || null, due_time: v.due_time || null,
                 repeat: v.repeat || null, pinned: !!v.pinned };
  if (v.list_id) data.list_id = v.list_id;
  await todoChange(id, data);
}

// ---------- Claude 用量上限（顯示在狀態列） ----------
function resetText(ts) {
  if (!ts) return '';
  const t = new Date(ts * 1000), min = Math.round((ts * 1000 - Date.now()) / 60000);
  const left = min < 60 ? `${Math.max(1, min)} 分鐘後` : min < 24 * 60 ? `${Math.floor(min / 60)} 小時 ${min % 60} 分後` : '';
  return `${ymd(t) === ymd(new Date()) ? '' : dayLabel(ymd(t)) + ' '}${hm(t)} 重置${left ? `（${left}）` : ''}`;
}
function limitSegsHTML() {
  // Claude 用量上限：接在狀態列「Claude」那一格後面
  const u = H.usage;
  if (!u) return '';
  const p = u.plan, L = u.limit, now = Date.now() / 1000;
  const win = (key, label) => {
    const w = p && p[key];
    if (!w || w.used == null) return '';
    const pct = Math.min(100, Math.max(0, w.used));
    const cls = pct >= 90 ? 'bad' : pct >= 70 ? 'warn' : '';
    const stale = w.at && now - w.at > 1800;
    const tip = `Claude ${label}用量上限\n已用 ${pct.toFixed(1)}%${w.resets_at ? '，' + resetText(w.resets_at) : ''}`
      + `${w.expired ? '\n已過重置時間，視為 0%' : ''}`
      + `${w.at ? '\n資料時間：' + rel(w.at * 1000) + (stale ? '（可能不是最新）' : '') : ''}`
      + `\n首頁開著時每 5 分鐘自動更新${p.polling ? '（更新中…）' : '・點一下立即更新'}${p.error ? '\n上次更新失敗：' + p.error : ''}`;
    return `<span class="seg lim ${cls} ${stale ? 'is-stale' : ''} ${p.polling ? 'is-polling' : ''}" tabindex="0" role="button" data-act="claude-limits-refresh" data-tip="${esc(tip)}">${label}
      <span class="pct">${Math.round(pct)}%</span><span class="mini use ${cls}"><i style="width:${pct.toFixed(0)}%"></i></span></span>`;
  };
  let h = win('five_hour', '5 小時') + win('seven_day', '本週');
  if (L.active) {
    const kind = { five_hour: '5 小時', seven_day: '每週' }[L.type] || '';
    h += `<span class="seg limit-hit" tabindex="0" data-tip="${esc(`Claude 已達${kind}用量上限\n${resetText(L.resets_at)}`)}">⛔ ${hm(new Date(L.resets_at * 1000))} 解除</span>`;
  }
  if (!h) {
    h = `<span class="seg" tabindex="0" role="button" data-act="claude-limits-refresh" data-tip="${esc('Claude 用量上限\n首頁開著時每 5 分鐘自動查詢・點一下立即查詢' + (p && p.error ? '\n上次查詢失敗：' + p.error : ''))}">用量 —</span>`;
  }
  return h;
}

// ---------- Google 連結 ----------
async function googleSetup() {
  let g;
  try { g = await api('/api/google'); } catch (e) { toast(e.message, true); return; }
  if (g.connected) {
    const lacks = [!g.tasks && 'Google Tasks', !g.calendar && '日曆'].filter(Boolean);
    openDlg('Google 連結', `<p>已連結。待辦事項會和 Google Tasks 雙向同步，行事曆每 10 分鐘更新一次。</p>
      ${lacks.length ? `<p class="notice" style="margin-top:12px">連結時沒有勾選 ${lacks.join('、')} 的權限，相關功能無法使用。按「重新連結」並勾選全部權限。</p>` : ''}`,
      `<button class="btn danger" data-act="google-disconnect" style="margin-right:auto">中斷連結</button>
       <button class="btn" data-act="google-connect">重新連結</button><button class="btn primary" data-act="dlg-close">完成</button>`);
    return;
  }
  if (g.configured) {
    openDlg('連結 Google', `<p>接下來會開啟 Google 的授權頁。請登入你的帳號，並<strong>勾選所有權限</strong>（Google Tasks、查看日曆），完成後會自動回到這一頁。</p>
      <p class="muted small" style="margin-top:10px">如果看到「Google 尚未驗證這個應用程式」，這是因為應用程式是你自己建立的：按「進階 → 前往（不安全）」繼續即可。</p>
      ${g.error ? `<p class="notice" style="margin-top:12px">${esc(g.error)}</p>` : ''}`,
      `<button class="btn" data-act="dlg-close">取消</button><button class="btn primary" data-act="google-connect">前往 Google 授權</button>`);
    return;
  }
  openDlg('連結 Google（第一次設定）', `
    <p>要讓這個頁面讀寫你的 Google Tasks 和日曆，需要先在 Google Cloud 建立一個只給你自己用的「用戶端」。只要做一次，約 5 分鐘。</p>
    <ol class="steps">
      <li><a href="https://console.cloud.google.com/projectcreate" target="_blank" rel="noopener">建立專案</a>：專案名稱填「首頁」，機構選「無機構」，按「建立」。完成後確認畫面最上方選的是這個專案。</li>
      <li>啟用兩個 API（各按一次「啟用」）：<a href="https://console.cloud.google.com/apis/library/tasks.googleapis.com" target="_blank" rel="noopener">Google Tasks API</a>、<a href="https://console.cloud.google.com/apis/library/calendar-json.googleapis.com" target="_blank" rel="noopener">Google Calendar API</a>。</li>
      <li><a href="https://console.cloud.google.com/auth/overview" target="_blank" rel="noopener">Google Auth Platform</a> 按「開始使用」：應用程式名稱「首頁」、使用者支援電子郵件選你自己、目標對象選「外部」、聯絡資訊填你自己的電子郵件，勾選同意政策後按「建立」。</li>
      <li><a href="https://console.cloud.google.com/auth/audience" target="_blank" rel="noopener">目標對象</a>：發布狀態維持「測試中」，在「測試使用者」按「新增使用者」，填你自己的 Gmail，儲存。<span class="muted">（測試中的授權每 7 天失效，到時首頁會提示「重新連結」；待辦事項照常能用，連結後自動補同步。）</span></li>
      <li><a href="https://console.cloud.google.com/auth/clients" target="_blank" rel="noopener">用戶端</a>：按「建立用戶端」，應用程式類型選<strong>「電腦版應用程式」</strong>，名稱「首頁」，按「建立」。<strong>在跳出的視窗馬上按「下載 JSON」</strong>，之後就看不到密鑰了。</li>
      <li>把下載的 JSON 檔選到這裡，選完可以把「下載」資料夾裡那份刪掉：</li>
    </ol>
    <div class="row"><input type="file" id="gclient-file" accept=".json,application/json"><span id="gclient-msg" class="small"></span></div>
    <p class="muted small" style="margin-top:8px">檔案只會存在這台電腦的 config/google_client.json。</p>`,
    `<button class="btn" data-act="dlg-close">取消</button>`);
  $('#gclient-file').addEventListener('change', async ev => {
    const f = ev.target.files[0];
    if (!f) return;
    try {
      const data = JSON.parse(await f.text());
      await api('/api/google/client', data);
      toast('已儲存用戶端設定');
      googleSetup();
    } catch (e) {
      $('#gclient-msg').textContent = e instanceof SyntaxError ? '這不是 JSON 檔' : e.message;
      $('#gclient-msg').style.color = 'var(--bad)';
    }
  });
}

// ---------- 音樂（可以收到旁邊） ----------
function renderMusic() {
  const cur = H.cur && H.cur.id;
  setHTML($('#music-list'), H.music.length ? `<ul>${H.music.map(m => `
    <li class="mu ${m.id === cur ? 'on' : ''}">
      <button class="mu-play" data-act="music-play" data-id="${esc(m.id)}" aria-label="播放 ${esc(m.title)}">${m.id === cur ? '♪' : '▶'}</button>
      <div class="mu-main" data-act="music-play" data-id="${esc(m.id)}">
        <div class="mu-title">${m.kind === 'playlist' ? '<span class="tag">清單</span> ' : ''}${esc(m.title || m.ref)}</div>
        <div class="mu-meta">${m.author ? esc(m.author.replace(/ - Topic$/, '')) + '・' : ''}${m.plays ? `播放 ${m.plays} 次` : '還沒播過'}${m.last_played ? '・' + rel(m.last_played * 1000) : ''}</div>
      </div>
      <button class="icon ${m.pinned ? 'pinned' : ''}" data-act="music-pin" data-id="${esc(m.id)}" aria-pressed="${!!m.pinned}" title="${m.pinned ? '取消釘選' : '釘選在最上面'}">${m.pinned ? '★' : '☆'}</button>
      <button class="icon" data-act="music-del" data-id="${esc(m.id)}" title="從清單移除" aria-label="移除 ${esc(m.title)}">✕</button>
    </li>`).join('')}</ul>`
    : `<p class="muted small empty">在 YouTube Music 按「分享 → 複製連結」，貼到上面就會加入。之後從這裡播放的次數會排出你最常聽的。</p>`);
  const b = $('#loop-btn');
  b.setAttribute('aria-pressed', String(H.loop));
  b.classList.toggle('on', H.loop);
}

function loadYT() {
  if (window.YT && window.YT.Player) return Promise.resolve();
  if (loadYT.p) return loadYT.p;
  loadYT.p = new Promise((res, rej) => {
    window.onYouTubeIframeAPIReady = res;
    const s = document.createElement('script');
    s.src = 'https://www.youtube.com/iframe_api';
    s.onerror = () => { loadYT.p = null; rej(new Error('載入不了 YouTube 播放器（沒有網路？）')); };
    document.head.append(s);
  });
  return loadYT.p;
}

const isPlaying = () => !!(H.playerReady && H.player.getPlayerState && H.player.getPlayerState() === 1);

function onPlayerState(ev) {
  const S_ = window.YT.PlayerState;
  if (ev.data === S_.PLAYING && H.cur && H.counted !== H.cur.id) {
    H.counted = H.cur.id;                      // 每次選歌只算一次，單曲循環不重複計數
    const title = H.cur.kind === 'video' ? (H.player.getVideoData() || {}).title : null;
    api(`/api/music/${enc(H.cur.id)}/played`, { title }).then(() => safe(loadMusic)).catch(() => {});
  }
  if (ev.data === S_.ENDED && H.loop && H.cur && H.cur.kind === 'video') {
    H.player.seekTo(0, true);
    H.player.playVideo();
  }
  showNow();
}

function showNow(err) {
  const btn = $('#music-toggle');
  btn.classList.toggle('playing', isPlaying());
  btn.textContent = !H.cur ? '♪ 音樂' : isPlaying() ? '♪ 播放中' : '♪ 已暫停';
  $('#mini-pp').textContent = isPlaying() ? '⏸' : '▶';
  $('#mini-pp').setAttribute('aria-label', isPlaying() ? '暫停' : '播放');
  if (err) { setHTML($('#now-playing'), err); setHTML($('#mini-title'), err); return; }
  if (!H.cur) { setHTML($('#now-playing'), ''); setHTML($('#mini-title'), ''); return; }
  const d = H.playerReady && H.player.getVideoData ? H.player.getVideoData() : null;
  const title = (d && d.title) || H.cur.title || '';
  setHTML($('#now-playing'), `<span class="muted">${H.cur.kind === 'playlist' ? '播放清單' : isPlaying() ? '正在播放' : '已暫停'}：</span>${esc(title)}`);
  setHTML($('#mini-title'), esc(title));
  $('#mini-title').title = title;
}

async function playMusic(id, autoplay = true) {
  const m = H.music.find(x => x.id === id);
  if (!m) return;
  H.cur = m; H.counted = null;
  $('#player-empty').hidden = true;
  renderMusic(); showNow(); setDock();
  try { await loadYT(); } catch (e) { toast(e.message, true); return; }
  if (H.player && H.playerReady) {
    if (m.kind === 'video') autoplay ? H.player.loadVideoById(m.ref) : H.player.cueVideoById(m.ref);
    else autoplay ? H.player.loadPlaylist({ list: m.ref, listType: 'playlist' }) : H.player.cuePlaylist({ list: m.ref, listType: 'playlist' });
    H.player.setLoop(H.loop);
    return;
  }
  H.player = new YT.Player('yt-player', {
    ...(m.kind === 'video' ? { videoId: m.ref } : {}),
    playerVars: { autoplay: autoplay ? 1 : 0, playsinline: 1, rel: 0, ...(m.kind === 'playlist' ? { list: m.ref, listType: 'playlist' } : {}) },
    events: {
      onReady: () => { H.playerReady = true; H.player.setLoop(H.loop); showNow(); },
      onStateChange: onPlayerState,
      onError: e => {
        const c = H.cur;
        const msg = { 2: '連結有誤', 5: '瀏覽器無法播放', 100: '影片已被移除或設為私人', 101: '這首歌不允許在其他網頁播放', 150: '這首歌不允許在其他網頁播放' }[e.data] || `播放失敗（${e.data}）`;
        const url = c.kind === 'video' ? `https://music.youtube.com/watch?v=${c.ref}` : `https://music.youtube.com/playlist?list=${c.ref}`;
        showNow(`<span style="color:var(--bad)">${esc(msg)}</span>・<a href="${url}" target="_blank" rel="noopener">在 YouTube Music 開啟</a>`);
      },
    },
  });
}

// 音樂面板：open＝從右邊滑出；mini＝角落的小播放器（有在播時）；off＝完全收起
function setDock(want) {
  if (want) H.dock = want;
  if (H.dock !== 'open') H.dock = !H.cur ? 'off' : H.hideMini ? 'hidden' : 'mini';
  lsSet('music-dock', H.dock === 'open' ? 'open' : 'closed');
  const d = $('#music-dock');
  d.className = 'dock is-' + (H.dock === 'hidden' ? 'off' : H.dock);   // hidden：看不到，但歌繼續播
  d.setAttribute('aria-hidden', String(H.dock === 'off' || H.dock === 'hidden'));
  $('#mini-pref').checked = !H.hideMini;
  $('#music-toggle').setAttribute('aria-expanded', String(H.dock === 'open'));
}

// ---------- 載入 ----------
async function loadHome() { H.home = await api('/api/home'); renderNowWeather(); renderSchedule(); }
async function loadTodos() { H.todos = await api('/api/todos'); renderTodos(); renderSchedule(); }
async function loadUsage() { H.usage = await api('/api/claude/usage'); renderChips(); }
async function loadMusic() {
  H.music = (await api('/api/music')).music;
  if (H.cur) H.cur = H.music.find(x => x.id === H.cur.id) || H.cur;
  renderMusic();
}

const syncHeadHeight = () => document.documentElement.style.setProperty('--head-h', $('header.top').offsetHeight + 'px');
syncHeadHeight();
new ResizeObserver(syncHeadHeight).observe($('header.top'));       // 狀態列載入後會變高
renderClock();
setInterval(() => {
  const before = $('#today-date').textContent;
  renderClock();
  if ($('#today-date').textContent !== before) { safe(loadHome); safe(loadTodos); }   // 過午夜：重新分組
  else renderSchedule();                                                              // 更新「還有幾分鐘」
}, 30000);
for (const [fn, ms] of [[loadHome, 60000], [loadTodos, 15000], [loadUsage, 15000]]) {
  safe(fn);
  setInterval(() => { if (!document.hidden) safe(fn); }, ms);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) { renderClock(); safe(loadHome); safe(loadTodos); safe(loadUsage); } });
setDock(lsGet('music-dock') === 'open' ? 'open' : 'off');
safe(loadMusic);

// Google 授權完成回到這頁
(() => {
  const q = new URLSearchParams(location.search);
  if (!q.has('google')) return;
  const r = q.get('google');
  history.replaceState(null, '', '/');
  if (r === 'ok') { toast('已連結 Google，正在同步待辦事項與行事曆'); setTimeout(() => { safe(loadTodos); safe(loadHome); }, 3000); }
  else toast('連結 Google 失敗：' + r, true);
})();

// ---------- 事件 ----------
$('#todo-input').addEventListener('input', renderPreview);
$('#mini-pref').addEventListener('change', e => { H.hideMini = !e.target.checked; lsSet('music-mini', H.hideMini ? 'hide' : 'show'); });
$('#todo-search').addEventListener('input', e => { H.q = e.target.value; renderTodos(); });
$('#todo-add').addEventListener('submit', async ev => {
  ev.preventDefault();
  const v = $('#todo-input').value.trim();
  if (!v) return;
  const p = parseQuick(v);
  if (!p.title) { toast('只有日期，沒有要做的事', true); return; }
  if (!$('#todo-add-list').hidden) p.list_id = $('#todo-add-list').value;
  try {
    await api('/api/todos', p);
    $('#todo-input').value = ''; renderPreview();
  } catch (e) { toast(e.message, true); }
  safe(loadTodos);
});
$('#music-add').addEventListener('submit', async ev => {
  ev.preventDefault();
  const url = $('#music-url').value.trim();
  if (!url) return;
  try {
    const r = await api('/api/music', { url });
    $('#music-url').value = '';
    await safe(loadMusic);
    if (r.existed) toast('已經在清單裡了');
    playMusic(r.id);
  } catch (e) { toast(e.message, true); }
});

document.addEventListener('change', ev => {
  const el = ev.target.closest('[data-change="todo-date"]');
  if (el && el.value) { closeMenu(); todoChange(el.dataset.id, { due_date: el.value }); }
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeMenu();
    if (e.target.id === 'todo-search' && e.target.value) { e.target.value = ''; H.q = ''; renderTodos(); }
    if (H.dock === 'open' && !dlg.open) setDock('closed');
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey || dlg.open) return;
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
  if (e.key === 'n' || e.key === 'N') { e.preventDefault(); $('#todo-input').focus(); }
  else if (e.key === '/') { e.preventDefault(); $('#todo-search').focus(); }
  else if (e.key === 'm' || e.key === 'M') { e.preventDefault(); setDock(H.dock === 'open' ? 'closed' : 'open'); }
});

document.addEventListener('click', async ev => {
  if (!ev.target.closest('#td-menu') && !ev.target.closest('[data-act="todo-menu"]')) closeMenu();
  if (H.dock === 'open' && !ev.target.closest('#music-dock') && !ev.target.closest('#music-toggle') && !ev.target.closest('dialog')
      && !ev.target.closest('.toasts')) setDock('closed');                 // 點旁邊就收起來
  const el = ev.target.closest('[data-act]');
  if (!el) return;
  const d = el.dataset;
  switch (d.act) {
    case 'todo-toggle': toggleTodo(d.id); break;
    case 'todo-when': todoChange(d.id, { due_date: ymd(addDays(today0(), +d.when)) }); break;
    case 'todo-set': {
      closeMenu();
      const v = d.field === 'pinned' ? !!d.value : d.value;
      const msg = d.field === 'list_id' ? `已移到「${listTitle(d.value)}」` : null;
      todoChange(d.id, { [d.field]: v }, msg);
      break;
    }
    case 'todo-clear-date': closeMenu(); todoChange(d.id, { due_date: null, due_time: null }); break;
    case 'todo-menu': ($('#td-menu') && $('#td-menu').dataset.for === d.id) ? closeMenu() : (openMenu(el, d.id), $('#td-menu') && ($('#td-menu').dataset.for = d.id)); break;
    case 'todo-edit': editTodo(d.id); break;
    case 'todo-del': closeMenu(); deleteTodo(d.id); break;
    case 'todo-delete': closeDlg(''); deleteTodo(d.id); break;
    case 'todo-view': H.view = d.view; H.q = ''; $('#todo-search').value = ''; lsSet('todo-view', d.view); renderTodos(); break;
    case 'todo-list-filter': H.listFilter = d.list; lsSet('todo-list', d.list); renderTodos(); break;
    case 'todo-overdue-today': {
      const td = ymd(new Date());
      const late = H.todos.todos.filter(x => x.status === 'open' && x.due_date && x.due_date < td && (!H.listFilter || effList(x) === H.listFilter));
      for (const x of late) { try { await api('/api/todos/' + enc(x.id), { due_date: td }); } catch (e) { toast(e.message, true); break; } }
      toast(`已把 ${late.length} 項移到今天`);
      safe(loadTodos);
      break;
    }
    case 'todo-sync':
      try { H.todos.sync = await api('/api/todos/sync', {}); renderSync(); } catch (e) { toast(e.message, true); }
      safe(loadTodos);
      break;
    case 'google-setup': googleSetup(); break;
    case 'google-connect':
      try { location.href = (await api('/api/google/connect', {})).url; } catch (e) { toast(e.message, true); }
      break;
    case 'google-disconnect':
      closeDlg('');
      if (!await ask('中斷 Google 連結？', '待辦事項會保留在這台電腦，但不再同步；行事曆會停止更新。', '中斷連結', true)) break;
      try { await api('/api/google/disconnect', {}); toast('已中斷連結'); } catch (e) { toast(e.message, true); }
      safe(loadTodos); safe(loadHome);
      break;
    case 'rec-todos':
      try {
        const r = await api(`/api/recordings/${enc(d.id)}/todos`, {});
        toast(r.added ? `已加入 ${r.added} 項待辦` : '這些待辦都已經加過了');
      } catch (e) { toast(e.message, true); }
      safe(loadTodos);
      break;
    case 'claude-limits-refresh':
      if (H.usage && H.usage.plan && H.usage.plan.polling) break;
      if (H.usage && H.usage.plan) H.usage.plan.polling = true;
      renderChips();
      try { H.usage = await api('/api/claude/usage/refresh', {}); toast('Claude 用量已更新'); }
      catch (e) { toast('更新失敗：' + e.message, true); }
      safe(loadUsage);
      break;
    case 'music-toggle': setDock(H.dock === 'open' ? 'closed' : 'open'); break;
    case 'music-close': setDock('closed'); break;
    case 'music-hide-mini':
      H.hideMini = true; lsSet('music-mini', 'hide'); setDock('closed');
      toast('小播放器已隱藏，音樂繼續播放。要再看到它，點右上角的「♪ 播放中」。');
      break;
    case 'music-pp':
      if (!H.playerReady) break;
      isPlaying() ? H.player.pauseVideo() : H.player.playVideo();
      break;
    case 'music-stop':
      if (H.playerReady) H.player.stopVideo();
      H.cur = null;
      renderMusic(); showNow(); setDock('closed');
      break;
    case 'music-play': playMusic(d.id); break;
    case 'music-loop':
      H.loop = !H.loop; lsSet('music-loop', H.loop ? '1' : '0');
      if (H.player && H.playerReady) H.player.setLoop(H.loop);
      renderMusic();
      toast(H.loop ? '單曲循環：開' : '單曲循環：關');
      break;
    case 'music-pin': {
      const m = H.music.find(x => x.id === d.id);
      try { await api(`/api/music/${enc(d.id)}/update`, { pinned: !(m && m.pinned) }); } catch (e) { toast(e.message, true); }
      safe(loadMusic);
      break;
    }
    case 'music-del': {
      const m = H.music.find(x => x.id === d.id);
      if (!await ask('從清單移除？', `「${m ? m.title : ''}」和它的播放次數會一起移除。`, '移除', true)) break;
      try { await api(`/api/music/${enc(d.id)}/delete`, {}); } catch (e) { toast(e.message, true); }
      safe(loadMusic);
      break;
    }
  }
});

// ---------- 目錄 ----------
(() => {
  const toc = $('#toc'), links = [...toc.querySelectorAll('a[href^="#"]')];
  const setOpen = open => { toc.classList.toggle('open', open); toc.querySelector('.toc-btn').setAttribute('aria-expanded', String(open)); };
  toc.addEventListener('click', e => {
    if (e.target.closest('[data-act="toc-toggle"]')) { setOpen(!toc.classList.contains('open')); return; }
    const a = e.target.closest('a');
    if (a) {
      setOpen(false);
      links.forEach(x => x.classList.toggle('on', x === a));        // 並排的區塊（Ollama／Claude）以點的那個為準
      hold = Date.now() + 1200;
    }
  });
  document.addEventListener('click', e => { if (!e.target.closest('#toc')) setOpen(false); });
  const seen = new Map();
  let hold = 0;
  const io = new IntersectionObserver(entries => {
    for (const en of entries) seen.set(en.target.id, en.isIntersecting ? en.boundingClientRect.top : null);
    const visible = [...seen.entries()].filter(([, top]) => top !== null).sort((a, b) => a[1] - b[1]);
    const cur = visible.length ? visible[0][0] : null;
    if (cur && Date.now() > hold) links.forEach(a => a.classList.toggle('on', a.getAttribute('href') === '#' + cur));
  }, { rootMargin: '-80px 0px -55% 0px' });
  links.forEach(a => { const el = document.getElementById(a.getAttribute('href').slice(1)); if (el) io.observe(el); });
})();

// ---------- 程式更新後自動重新整理 ----------
(() => {
  let ver = null, asked = false;
  const busy = () => isPlaying() || dlg.open || ['input', 'textarea', 'select'].includes((document.activeElement.tagName || '').toLowerCase());
  const check = async () => {
    try {
      const v = (await api('/api/version')).version;
      if (ver === null) { ver = v; return; }
      if (v === ver) return;
      if (!busy()) { location.reload(); return; }
      if (asked) return;
      asked = true;
      const el = document.createElement('div');
      el.className = 'toast';
      el.innerHTML = '<span>首頁有更新</span><button class="toast-btn">重新整理</button>';
      el.querySelector('button').onclick = () => location.reload();
      $('#toasts').append(el);
    } catch { /* 服務重啟中，下次再試 */ }
  };
  check();
  setInterval(() => { if (!document.hidden) check(); }, 60000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
})();
