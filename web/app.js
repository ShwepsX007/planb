/* PlanB web panel */
'use strict';

const $ = (id) => document.getElementById(id);
const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? '—' :
  Number(v).toLocaleString('ru-RU', { minimumFractionDigits: d, maximumFractionDigits: d });
const money = (v, d = 4) => (v === null || v === undefined || isNaN(v)) ? '—' :
  (v >= 0 ? '+' : '') + Number(v).toFixed(d) + ' $';
const pct = (v, d = 3) => (v === null || v === undefined || isNaN(v)) ? '—' : Number(v).toFixed(d) + ' %';
const cls = (v) => v > 0 ? 'pos' : v < 0 ? 'neg' : 'neutral';
const ts = (ms) => ms ? new Date(ms).toLocaleString('ru-RU', { hour12: false }) : '—';
const timeOnly = (ms) => ms ? new Date(ms).toLocaleTimeString('ru-RU', { hour12: false }) : '—';

const app = {
  snapshot: null, settings: null, logs: [], ws: null, wsTimer: null,
  chart: null, series: null, priceLines: [], lastChartUpdate: 0, chartTabActive: false,
  btChart: null, btSeries: null, btEquity: null, btEquitySeries: null, btLines: [],
  openPositions: [], cycles: [], trades: [],
};

/* ------------------------------------------------------------------ helpers */
async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
  if (!res.ok) {
    const err = data && data.error ? (typeof data.error === 'string' ? data.error : JSON.stringify(data.error)) : res.statusText;
    throw new Error(err);
  }
  return data;
}

function toast(message, kind = 'ok') {
  const el = $('toast');
  el.textContent = message;
  el.className = 'toast ' + kind;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), 5000);
}

/* ---------------------------------------------------------------------- ws */
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  app.ws = ws;
  ws.onmessage = (ev) => {
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === 'snapshot') { app.snapshot = msg.snapshot; renderSnapshot(msg.snapshot); }
    else if (msg.type === 'log') { pushLog(msg.log); }
  };
  ws.onclose = () => {
    clearTimeout(app.wsTimer);
    app.wsTimer = setTimeout(connectWS, 2500);
  };
  ws.onerror = () => ws.close();
}

/* ------------------------------------------------------------------ render */
function renderSnapshot(s) {
  if (!s) return;
  const st = s.status;
  const badge = $('statusBadge');
  const labels = { stopped: 'остановлен', running: 'работает', paused: 'пауза',
    error: 'ошибка', target_reached: 'цель достигнута', stopped_by_stop_loss: 'стоп-лосс' };
  badge.textContent = labels[st] || st;
  badge.className = 'badge badge-' + st;

  $('modeBadge').textContent = s.trade_mode === 'live' ? 'LIVE' : 'PAPER';
  $('modeBadge').className = 'badge ' + (s.trade_mode === 'live' ? 'badge-error' : 'badge-mode');
  $('demoBadge').classList.toggle('hidden', s.data_source !== 'demo');
  $('pairInfo').textContent = `${s.symbol} · ${s.timeframe}` + (s.market_type === 'futures' ? ' · фьюч.' : ' · спот');
  $('priceInfo').textContent = s.price ? fmt(s.price, s.price > 100 ? 2 : 5) : '—';
  $('btnStart').disabled = st === 'running';
  $('btnPause').disabled = st !== 'running';
  $('btnStop').disabled = st === 'stopped' || st === 'error';
  $('btnCloseAll').disabled = !(s.engine && s.engine.cycle);

  const e = s.engine || {};
  const c = e.cycle;

  $('cPnl').textContent = money(e.realized_pnl, 4);
  $('cPnl').className = 'card-value ' + cls(e.realized_pnl);
  const target = s.target_total_profit || 0;
  const prog = target ? Math.max(0, Math.min(100, (e.realized_pnl / target) * 100)) : 0;
  $('cProgressBar').style.width = prog + '%';
  $('cProgressText').textContent = target ? `цель: ${fmt(target, 2)} $ · ${prog.toFixed(1)} %` : 'цель не задана';

  $('cUnreal').textContent = c ? money(c.unrealized_pnl, 4) : '—';
  $('cUnreal').className = 'card-value ' + (c ? cls(c.unrealized_pnl) : '');
  $('cUnrealHint').textContent = c ? `оценка по цене ${fmt(s.price, s.price > 100 ? 2 : 5)}` : 'нет открытых позиций';

  $('cAvg').textContent = c ? fmt(c.avg_entry, 2) : '—';
  $('cAvgHint').textContent = c ? `позиций: ${c.positions.length} · объём ${fmt(c.total_volume_usd, 2)} $` : 'позиций: 0';

  $('cTarget').textContent = c ? fmt(c.target, 2) : '—';
  $('cTargetHint').textContent = c ? `уровень ${c.goal_pct}% · сетка ${fmt(c.swing_low, 2)}–${fmt(c.swing_high, 2)}` : 'уровень —';

  $('cProfitToTarget').textContent = c ? pct(c.profit_pct_to_target) : '—';
  $('cProfitToTarget').className = 'card-value ' + (c ? (c.profit_pct_to_target >= (app.settings?.min_profit_pct ?? 0) ? 'pos' : 'neg') : '');
  $('cProfitToTargetHint').textContent = c ? `мин. профит ${app.settings?.min_profit_pct ?? '—'} % · оценка ${money(c.expected_pnl_at_target, 4)}` : 'мин. профит —';

  $('cCycles').textContent = e.cycles_total ?? 0;
  $('cCyclesHint').textContent = `прибыльных ${e.wins ?? 0} · убыточных ${e.losses ?? 0} · сигналов ${e.signals_found ?? 0}`;

  const b = s.balance || {};
  $('cBalance').textContent = b.total !== undefined ? fmt(b.total, 2) + ' $' : '—';
  $('cBalanceHint').textContent = b.mode === 'paper' ? `виртуальный (старт ${fmt(b.start_balance, 2)} $)` :
    (b.free !== undefined ? `свободно ${fmt(b.free, 2)} ${b.quote || ''}` : '—');

  $('cNextAdd').textContent = c && c.next_position_volume_usd ? fmt(c.next_position_volume_usd, 2) + ' $' : '—';
  $('cNextAddHint').textContent = c ? (c.next_position_idx ? `позиция #${c.next_position_idx} · осталось ${c.positions_left}` : 'лимит позиций достигнут') : '—';

  renderCycleBox(c, e, s);
  renderCurrentPositions(c);
  renderEngineBox(e, s, c);
  renderLogic(s);
  if (app.chartTabActive) maybeUpdateChart();
}

function kv(rows) {
  return rows.map(([k, v, klass]) => `<div class="k">${k}</div><div class="v ${klass || ''}">${v}</div>`).join('');
}

function renderCycleBox(c, e, s) {
  const box = $('cycleBox');
  if (!c) { box.innerHTML = kv([['Статус', '<span class="muted">нет активного цикла — ждём сигнал</span>'],
    ['Проверка', e.last_check || '—']]); return; }
  box.innerHTML = kv([
    ['Цикл', c.id],
    ['Сторона', c.side === 'long' ? 'лонг (коррекция вверх)' : 'шорт (коррекция вниз)'],
    ['Открыт', ts(c.opened_ts)],
    ['Свечей в импульсе', c.note || '—'],
    ['Сетка Фибо', `${fmt(c.swing_low, 2)} … ${fmt(c.swing_high, 2)}`],
    ['Цель (' + c.goal_pct + '%)', `<b>${fmt(c.target, 2)}</b>`],
    ['Средняя входа', `<b>${fmt(c.avg_entry, 2)}</b>`],
    ['Профит до цели', `<b>${pct(c.profit_pct_to_target)}</b> (мин. ${app.settings?.min_profit_pct ?? '—'}%)`],
    ['Ожидаемый профит', `${money(c.expected_pnl_at_target, 4)} при закрытии по цели`],
    ['Позиций', `${c.positions.length} из ${app.settings?.max_positions ?? '—'} (макс. одновр. ${c.max_positions_used})`],
    ['Вложено', `${fmt(c.total_volume_usd, 2)} $`],
    ['До цели по цене', c.distance_to_target_pct !== undefined ? pct(c.distance_to_target_pct) : '—'],
  ]);
}

function renderEngineBox(e, s, c) {
  const rows = [
    ['Состояние', e.stopped ? `<span class="neg">остановлен: ${e.stop_reason || ''}</span>` : 'активен'],
    ['Последняя проверка', `<span class="muted">${e.last_check || '—'}</span>`],
    ['Цена', fmt(s.price, s.price > 100 ? 2 : 5)],
    ['Realized P/L', `<span class="${cls(e.realized_pnl)}">${money(e.realized_pnl, 4)}</span>`],
    ['Комиссия/слиппедж', `${app.settings?.fee_pct ?? '—'} % / ${app.settings?.slippage_pct ?? '—'} %`],
    ['Обновление', timeOnly(s.updated_at)],
  ];
  if (s.error) rows.push(['Ошибка', `<span class="neg">${s.error}</span>`]);
  $('engineBox').innerHTML = kv(rows);
}

function renderCurrentPositions(c) {
  const tbody = $('currentPositions').querySelector('tbody');
  if (!c || !c.positions || !c.positions.length) {
    tbody.innerHTML = '<tr class="empty"><td colspan="7">позиций нет</td></tr>';
    return;
  }
  const price = app.snapshot?.price || 0;
  tbody.innerHTML = c.positions.map((p) => {
    const pnl = c.side === 'long' ? (price - p.price) * p.qty : (p.price - price) * p.qty;
    return `<tr><td><b>${p.idx}</b></td><td>${ts(p.ts)}</td><td>${fmt(p.price, 2)}</td>
      <td>${fmt(p.volume_usd, 2)}</td><td>${p.qty}</td>
      <td>${p.multiplier && p.multiplier > 1 ? `<b class="pos">x${p.multiplier}</b>` : 'x1'}</td>
      <td class="${cls(pnl)}">${money(pnl, 4)}</td></tr>`;
  }).join('');
}

function renderLogic(s) {
  const st = app.settings || {};
  $('lgCandles').textContent = st.candles_count ?? '—';
  $('lgTf').textContent = s.timeframe || '—';
  $('lgMove').textContent = st.min_move_pct ?? '—';
  $('lgGoal').textContent = st.fib_goal_pct ?? '—';
  $('lgMinProfit').textContent = st.min_profit_pct ?? '—';
  $('lgMult').textContent = st.volume_multiplier ?? '—';
  $('lgTotal').textContent = st.total_profit_target_usd ?? '—';
  const schedule = s.volume_schedule || st.volume_schedule || [];
  const html = schedule.map((v, i) => `<span class="chip">#${i + 1}: <b>${fmt(v, 2)} $</b></span>`).join('');
  $('scheduleBox').innerHTML = html;
  $('planTable').innerHTML = html;
  $('chartPair').textContent = `${s.symbol} · ${s.timeframe}`;
}

/* -------------------------------------------------------------------- logs */
function pushLog(log) {
  app.logs.push(log);
  if (app.logs.length > 800) app.logs = app.logs.slice(-800);
  const el = $('logs');
  const line = document.createElement('div');
  line.className = 'log-' + (log.level || 'info');
  line.innerHTML = `<span class="t">${timeOnly(log.ts)}</span>${escapeHtml(log.message)}`;
  el.appendChild(line);
  while (el.childNodes.length > 800) el.removeChild(el.firstChild);
  if ($('chkAutoscroll').checked) el.scrollTop = el.scrollHeight;
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

/* ------------------------------------------------------------------- chart */
function initChart() {
  if (app.chart) return;
  const LWC = window.LightweightCharts;
  app.chart = LWC.createChart($('chart'), {
    layout: { background: { type: 'solid', color: '#0b1017' }, textColor: '#c6d2df', fontSize: 11 },
    grid: { vertLines: { color: '#131b25' }, horzLines: { color: '#131b25' } },
    rightPriceScale: { borderColor: '#243146' },
    timeScale: { borderColor: '#243146', timeVisible: true, secondsVisible: false },
    crosshair: { mode: LWC.CrosshairMode.Normal },
  });
  app.series = app.chart.addCandlestickSeries({
    upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
    wickUpColor: '#26a69a', wickDownColor: '#ef5350',
  });
  new ResizeObserver(() => app.chart && app.chart.applyOptions({
    width: $('chart').clientWidth, height: $('chart').clientHeight })).observe($('chart'));
}

async function updateChart(force = false) {
  const now = Date.now();
  if (!force && now - app.lastChartUpdate < 4000) return;
  app.lastChartUpdate = now;
  initChart();
  let data;
  try { data = await api('GET', '/api/candles?limit=400'); } catch (e) { return; }
  const LWC = window.LightweightCharts;
  const candles = (data.candles || []).map((c) => ({
    time: Math.floor(c[0] / 1000), open: c[1], high: c[2], low: c[3], close: c[4],
  }));
  const uniq = [];
  const seen = new Set();
  for (const c of candles) { if (!seen.has(c.time)) { seen.add(c.time); uniq.push(c); } }
  uniq.sort((a, b) => a.time - b.time);
  if (!uniq.length) return;
  app.series.setData(uniq);
  app.timeIndex = uniq.map((c) => c.time);

  app.priceLines.forEach((l) => { try { app.series.removePriceLine(l); } catch (e) {} });
  app.priceLines = [];

  if ($('chkFib').checked) {
    (data.levels || []).forEach((lv) => {
      const hl = lv.highlight || lv.label === 'цель';
      const color = hl ? '#ffffff' : (lv.level === 0.5 ? '#f0b90b' : '#3f6ea8');
      app.priceLines.push(app.series.createPriceLine({
        price: lv.price, color, lineWidth: hl ? 2 : 1,
        lineStyle: hl ? LWC.LineStyle.Solid : (lv.level === 1 || lv.level === 0 ? LWC.LineStyle.Dashed : LWC.LineStyle.Dotted),
        axisLabelVisible: true, title: lv.label === 'цель' ? 'цель' : String(lv.level),
      }));
    });
    if (data.avg_entry) {
      app.priceLines.push(app.series.createPriceLine({
        price: data.avg_entry, color: '#4da3ff', lineWidth: 2,
        lineStyle: LWC.LineStyle.Solid, axisLabelVisible: true, title: 'средняя',
      }));
    }
    if (data.target) {
      app.priceLines.push(app.series.createPriceLine({
        price: data.target, color: '#ffffff', lineWidth: 2,
        lineStyle: LWC.LineStyle.Solid, axisLabelVisible: true, title: 'цель',
      }));
    }
  }

  if ($('chkMarks').checked && data.markers && data.markers.length) {
    const idx = app.timeIndex;
    const nearest = (ms) => {
      const sec = Math.floor(ms / 1000);
      let lo = 0, hi = idx.length - 1, res = idx[0];
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (idx[mid] <= sec) { res = idx[mid]; lo = mid + 1; } else { hi = mid - 1; }
      }
      return res;
    };
    const markers = data.markers.map((m) => {
      const isEntry = m.kind === 'entry';
      const isExit = m.kind === 'exit';
      const long = m.side === 'long';
      return {
        time: nearest(m.ts),
        position: isExit ? (long ? 'aboveBar' : 'belowBar') : (long ? 'belowBar' : 'aboveBar'),
        color: isExit ? '#2ecc71' : (m.kind === 'closed_entry' ? '#5d6b7d' : '#26a69a'),
        shape: isExit ? 'arrowUp' : 'circle',
        text: m.text,
      };
    });
    markers.sort((a, b) => a.time - b.time);
    app.series.setMarkers(markers);
  } else {
    app.series.setMarkers([]);
  }
  if (force) app.chart.timeScale().fitContent();
}

function maybeUpdateChart() { updateChart(false); }

/* --------------------------------------------------------------- positions */
async function loadPositions() {
  const s = app.snapshot;
  const tbody = $('openPositionsTable').querySelector('tbody');
  const c = s && s.engine ? s.engine.cycle : null;
  if (!c) { tbody.innerHTML = '<tr class="empty"><td colspan="8">нет открытых позиций</td></tr>'; }
  else {
    tbody.innerHTML = c.positions.map((p) => `<tr>
      <td>${c.id}</td><td>${c.side}</td><td><b>${p.idx}</b></td><td>${ts(p.ts)}</td>
      <td>${fmt(p.price, 2)}</td><td>${fmt(p.volume_usd, 2)}</td><td>${p.qty}</td>
      <td>${p.multiplier && p.multiplier > 1 ? 'x' + p.multiplier : 'x1'}</td></tr>`).join('');
  }
}

/* ----------------------------------------------------------------- cycles */
async function loadHistory() {
  try {
    const [cy, pos] = await Promise.all([api('GET', '/api/cycles?limit=200'), api('GET', '/api/positions?limit=300')]);
    app.cycles = cy.cycles || []; app.trades = pos.positions || [];
    renderCycles(); renderTrades();
  } catch (e) { toast('Не удалось загрузить историю: ' + e.message, 'err'); }
}

function renderCycles() {
  const tbody = $('cyclesTable').querySelector('tbody');
  if (!app.cycles.length) { tbody.innerHTML = '<tr class="empty"><td colspan="10">циклов пока нет</td></tr>'; return; }
  tbody.innerHTML = app.cycles.map((c) => `<tr>
    <td>${c.id}</td><td>${c.side}</td><td>${c.positions_count}</td>
    <td>${fmt(c.avg_entry, 2)}</td><td>${fmt(c.target, 2)}</td><td>${fmt(c.close_price, 2)}</td>
    <td>${fmt(c.volume_usd, 2)}</td><td class="${cls(c.pnl)}"><b>${money(c.pnl, 4)}</b></td>
    <td class="${cls(c.pnl_pct)}">${pct(c.pnl_pct)}</td><td class="muted">${c.close_reason || ''}</td></tr>`).join('');
}

function renderTrades() {
  const tbody = $('tradesTable').querySelector('tbody');
  if (!app.trades.length) { tbody.innerHTML = '<tr class="empty"><td colspan="9">сделок пока нет</td></tr>'; return; }
  tbody.innerHTML = app.trades.map((p) => `<tr>
    <td>${p.cycle_id}</td><td><b>${p.idx}</b></td><td>${ts(p.ts)}</td><td>${fmt(p.price, 2)}</td>
    <td>${fmt(p.volume_usd, 2)}</td><td>${p.qty}</td>
    <td>${p.multiplier && p.multiplier > 1 ? 'x' + p.multiplier : 'x1'}</td>
    <td>${p.close_price ? fmt(p.close_price, 2) : '<span class="muted">открыта</span>'}</td>
    <td class="${cls(p.pnl)}">${p.close_ts ? money(p.pnl, 4) : '—'}</td></tr>`).join('');
}

function exportCyclesCsv() {
  const head = ['id', 'side', 'positions', 'avg_entry', 'target', 'close_price', 'volume_usd', 'pnl', 'pnl_pct', 'reason'];
  const rows = app.cycles.map((c) => [c.id, c.side, c.positions_count, c.avg_entry, c.target, c.close_price,
    c.volume_usd, c.pnl, c.pnl_pct, c.close_reason]);
  const csv = [head, ...rows].map((r) => r.map((v) => `"${v ?? ''}"`).join(';')).join('\n');
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'planb_cycles.csv';
  a.click();
}

/* ----------------------------------------------------------------- settings */
const FIELDS = [
  { group: 'Биржа и подключение', fields: [
    { k: 'market_type', l: 'Тип рынка', t: 'select', o: [['futures', 'Фьючерсы USDT-M (можно шорт)'], ['spot', 'Спот (только лонг)']] },
    { k: 'symbol', l: 'Торговая пара', t: 'text', h: 'например BTC/USDT' },
    { k: 'data_source', l: 'Источник данных', t: 'select', o: [['gate', 'Gate.io — реальные свечи'], ['demo', 'DEMO — синтетические свечи']] },
    { k: 'trade_mode', l: 'Режим торговли', t: 'select', o: [['paper', 'Paper — виртуальные сделки'], ['live', 'LIVE — реальные ордера']] },
    { k: 'api_key', l: 'API key', t: 'text', h: 'пусто = не менять' },
    { k: 'api_secret', l: 'API secret', t: 'password', h: 'хранится только на сервере' },
    { k: 'sandbox', l: 'Testnet', t: 'bool' },
    { k: 'leverage', l: 'Плечо (фьючерсы)', t: 'number', step: 1, min: 1, max: 100, int: true },
    { k: 'margin_mode', l: 'Маржа', t: 'select', o: [['isolated', 'Изолированная'], ['cross', 'Кросс']] },
  ] },
  { group: 'Данные и сигнал', fields: [
    { k: 'timeframe', l: 'Таймфрейм', t: 'tf' },
    { k: 'candles_count', l: 'Количество свечей в одну сторону', t: 'number', step: 1, min: 1, max: 50, int: true },
    { k: 'min_move_pct', l: 'Минимальное движение цены, %', t: 'number', step: 0.1, min: 0.01 },
    { k: 'fib_goal_pct', l: 'Цель: уровень коррекции по Фибо, %', t: 'number', step: 1, min: 1, h: '50 % — середина сетки' },
    { k: 'min_profit_pct', l: 'Минимальный профит, %', t: 'number', step: 0.05, min: 0.01 },
    { k: 'direction', l: 'Направление сделок', t: 'select', o: [['both', 'Оба направления'], ['long', 'Только лонг'], ['short', 'Только шорт']] },
    { k: 'move_measure', l: 'Как измеряется движение', t: 'select', o: [['range', 'Максимум − минимум свечей'], ['close', 'От открытия до закрытия']] },
    { k: 'candle_direction_mode', l: 'Направление свечи', t: 'select', o: [['body', 'Тело свечи (закр. ниже откр.)'], ['close', 'Последовательные закрытия']] },
    { k: 'update_swing_in_cycle', l: 'Тянуть сетку и по новым максимумам', t: 'bool' },
  ] },
  { group: 'Объёмы и сетка усреднения', fields: [
    { k: 'base_volume_usd', l: 'Объём первой сделки, $', t: 'number', step: 1, min: 0.01 },
    { k: 'max_positions', l: 'Максимум позиций в цикле', t: 'number', step: 1, min: 1, max: 20, int: true },
    { k: 'volume_multiplier', l: 'Множитель объёма', t: 'number', step: 0.5, min: 1, max: 10 },
    { k: 'multiplier_start_position', l: 'С какой позиции умножать', t: 'number', step: 1, min: 1, max: 20, int: true, h: '1,1,2,4,8… при множителе 2 и старте 3' },
  ] },
  { group: 'Цель по общему профиту и риск', fields: [
    { k: 'total_profit_target_usd', l: 'Общий профит для остановки, $', t: 'number', step: 1, min: 0, h: '0 — без ограничения' },
    { k: 'stop_after_total_target', l: 'Останавливать бота при достижении', t: 'bool' },
    { k: 'cycle_stop_loss_pct', l: 'Аварийный стоп цикла, %', t: 'number', step: 0.5, min: 0, h: '0 — выключен' },
    { k: 'require_new_extreme_for_add', l: 'Долив только после нового экстремума', t: 'bool' },
    { k: 'tp_requires_min_profit', l: 'Закрывать по цели только при профите ≥ минимума', t: 'bool' },
    { k: 'close_at_target_when_maxed', l: 'При лимите позиций закрывать по цели', t: 'bool' },
  ] },
  { group: 'Исполнение и учёт', fields: [
    { k: 'fee_pct', l: 'Комиссия за сделку, %', t: 'number', step: 0.01, min: 0 },
    { k: 'slippage_pct', l: 'Слиппедж, %', t: 'number', step: 0.01, min: 0 },
    { k: 'paper_balance_usd', l: 'Виртуальный баланс для paper, $', t: 'number', step: 100, min: 1 },
    { k: 'auto_min_volume', l: 'Поднимать объём до минимума биржи', t: 'bool' },
    { k: 'poll_interval_sec', l: 'Период опроса, сек', t: 'number', step: 0.5, min: 1, max: 60 },
    { k: 'candle_lookback', l: 'Свечей в истории', t: 'number', step: 50, min: 60, max: 1500, int: true },
    { k: 'demo_speed', l: 'Ускорение DEMO-рынка', t: 'number', step: 1, min: 1, max: 600, h: '1 = реальное время, 60 = 1 минута за секунду' },
  ] },
  { group: 'Уведомления (Telegram, необязательно)', fields: [
    { k: 'notify_telegram_token', l: 'Токен бота', t: 'text' },
    { k: 'notify_telegram_chat', l: 'Chat ID', t: 'text' },
  ] },
];

function renderSettingsForm(settings) {
  const host = $('settingsForm');
  host.innerHTML = FIELDS.map((g) => `<div class="settings-group"><h3>${g.group}</h3>
    <div class="fields-grid">${g.fields.map((f) => fieldHtml(f, settings[f.k])).join('')}</div></div>`).join('');
}

function fieldHtml(f, value) {
  const id = 'set_' + f.k;
  const hint = f.h ? `<div class="hint">${f.h}</div>` : '';
  if (f.t === 'bool') {
    return `<div class="field"><label>${f.l}</label>
      <label class="switch"><input type="checkbox" id="${id}" ${value ? 'checked' : ''}> включено</label>${hint}</div>`;
  }
  if (f.t === 'select') {
    const opts = f.o.map(([v, t]) => `<option value="${v}" ${String(value) === v ? 'selected' : ''}>${t}</option>`).join('');
    return `<div class="field"><label>${f.l}</label><select id="${id}">${opts}</select>${hint}</div>`;
  }
  if (f.t === 'tf') {
    const tfs = (app.settings && app.settings.timeframes) || ['1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d'];
    const opts = tfs.map((v) => `<option value="${v}" ${value === v ? 'selected' : ''}>${v}</option>`).join('');
    return `<div class="field"><label>${f.l}</label><select id="${id}">${opts}</select>${hint}</div>`;
  }
  const type = f.t === 'password' ? 'password' : (f.t === 'number' ? 'number' : 'text');
  const attrs = [f.step ? `step="${f.step}"` : '', f.min !== undefined ? `min="${f.min}"` : '',
    f.max !== undefined ? `max="${f.max}"` : ''].join(' ');
  return `<div class="field"><label>${f.l}</label>
    <input type="${type}" id="${id}" value="${value ?? ''}" ${attrs} autocomplete="off">${hint}</div>`;
}

function collectSettings() {
  const out = {};
  FIELDS.forEach((g) => g.fields.forEach((f) => {
    const el = $('set_' + f.k);
    if (!el) return;
    if (f.t === 'bool') out[f.k] = el.checked;
    else if (f.t === 'number') {
      const v = el.value.trim();
      if (v === '') return;
      out[f.k] = f.int ? parseInt(v, 10) : parseFloat(v);
    } else out[f.k] = el.value.trim();
  }));
  return out;
}

/* ---------------------------------------------------------------- backtest */
function renderBacktestForm(settings) {
  const fields = [
    ['source', 'Источник данных', 'select', [['demo', 'DEMO — синтетика'], ['gate', 'Gate.io — история с биржи']]],
    ['limit_candles', 'Свечей для теста', 'number', {}],
    ['seed', 'Seed (демо)', 'number', {}],
    ['timeframe', 'Таймфрейм', 'tf', {}],
    ['candles_count', 'Свечей в одну сторону', 'number', {}],
    ['min_move_pct', 'Мин. движение, %', 'number', {}],
    ['fib_goal_pct', 'Цель по Фибо, %', 'number', {}],
    ['min_profit_pct', 'Мин. профит, %', 'number', {}],
    ['base_volume_usd', 'Объём 1-й сделки, $', 'number', {}],
    ['volume_multiplier', 'Множитель объёма', 'number', {}],
    ['multiplier_start_position', 'Умножать с позиции', 'number', {}],
    ['max_positions', 'Максимум позиций', 'number', {}],
    ['total_profit_target_usd', 'Цель общего профита, $', 'number', {}],
    ['paper_balance_usd', 'Стартовый баланс, $', 'number', {}],
    ['fee_pct', 'Комиссия, %', 'number', {}],
    ['slippage_pct', 'Слиппедж, %', 'number', {}],
  ];
  const defaults = { source: settings.data_source === 'demo' ? 'demo' : 'gate', limit_candles: 3000, seed: 7 };
  $('btForm').innerHTML = fields.map(([k, l, t, o]) => {
    const value = defaults[k] !== undefined ? defaults[k] : (settings[k] !== undefined ? settings[k] : '');
    if (t === 'select') {
      const opts = o.map(([v, txt]) => `<option value="${v}" ${value === v ? 'selected' : ''}>${txt}</option>`).join('');
      return `<div class="field"><label>${l}</label><select id="bt_${k}">${opts}</select></div>`;
    }
    if (t === 'tf') {
      const tfs = settings.timeframes || ['1m', '5m', '15m', '1h'];
      return `<div class="field"><label>${l}</label><select id="bt_${k}">${tfs.map((v) => `<option value="${v}" ${value === v ? 'selected' : ''}>${v}</option>`).join('')}</select></div>`;
    }
    return `<div class="field"><label>${l}</label><input type="number" step="any" id="bt_${k}" value="${value}"></div>`;
  }).join('');
}

function collectBacktestParams() {
  const params = {};
  document.querySelectorAll('[id^="bt_"]').forEach((el) => {
    const key = el.id.slice(3);
    const v = el.value;
    if (v === '') return;
    if (el.tagName === 'SELECT' && key === 'timeframe') params[key] = v;
    else if (el.type === 'number') params[key] = parseFloat(v);
    else params[key] = v;
  });
  return params;
}

async function runBacktest() {
  $('btStatus').textContent = 'выполняется…';
  $('btnRunBacktest').disabled = true;
  try {
    const result = await api('POST', '/api/backtest', collectBacktestParams());
    if (!result.ok) throw new Error(result.error || 'ошибка');
    renderBacktestResult(result);
    $('btStatus').textContent = `готово: ${result.candles} свечей, ${result.cycles} циклов`;
  } catch (e) {
    $('btStatus').textContent = '';
    toast('Бэктест: ' + e.message, 'err');
  } finally {
    $('btnRunBacktest').disabled = false;
  }
}

function renderBacktestResult(r) {
  const cards = [
    ['Итоговый баланс', fmt(r.final_equity, 2) + ' $', `старт ${fmt(r.initial_balance, 2)} $`],
    ['Общий профит', money(r.total_pnl, 4), pct(r.return_pct) + ' к балансу'],
    ['Циклов', r.cycles, `прибыльных ${r.wins} · убыточных ${r.losses}`],
    ['Win rate', fmt(r.win_rate, 1) + ' %', `средний цикл ${money(r.avg_cycle_pnl, 4)}`],
    ['Максимум позиций', r.max_positions_used, `возможных ${(app.settings && app.settings.max_positions) || '—'}`],
    ['Макс. просадка', fmt(r.max_drawdown, 4) + ' $', pct(r.max_drawdown_pct)],
    ['Лучший цикл', money(r.best_cycle, 4), `худший ${money(r.worst_cycle, 4)}`],
    ['Прибыль/убыток', r.profit_factor === null ? '∞' : fmt(r.profit_factor, 2), `комиссии ${fmt(r.fees_total, 4)} $`],
  ];
  $('btStats').innerHTML = cards.map(([l, v, h]) => `<div class="card"><div class="card-label">${l}</div>
    <div class="card-value">${v}</div><div class="card-hint">${h}</div></div>`).join('');

  const tbody = $('btCycles').querySelector('tbody');
  const list = (r.cycles_list || []).slice().reverse();
  tbody.innerHTML = list.length ? list.map((c, i) => `<tr>
    <td>${i + 1}</td><td>${c.side}</td><td>${c.positions}</td><td>${fmt(c.avg_entry, 2)}</td>
    <td>${fmt(c.target, 2)}</td><td>${fmt(c.close_price, 2)}</td><td>${fmt(c.volume_usd, 2)}</td>
    <td class="${cls(c.pnl)}"><b>${money(c.pnl, 4)}</b></td><td class="${cls(c.pnl_pct)}">${pct(c.pnl_pct)}</td>
    <td class="muted">${c.reason || ''}</td></tr>`).join('')
    : '<tr class="empty"><td colspan="10">циклов не было — попробуйте уменьшить мин. движение или взять больше свечей</td></tr>';

  renderEquityCurve(r.equity_curve || []);
  renderBtChart(r.trades || []);
}

function renderEquityCurve(curve) {
  const LWC = window.LightweightCharts;
  if (!app.btEquity) {
    app.btEquity = LWC.createChart($('btEquity'), {
      layout: { background: { type: 'solid', color: '#0b1017' }, textColor: '#c6d2df', fontSize: 11 },
      grid: { vertLines: { color: '#131b25' }, horzLines: { color: '#131b25' } },
      rightPriceScale: { borderColor: '#243146' },
      timeScale: { borderColor: '#243146', timeVisible: true, secondsVisible: false },
    });
    app.btEquitySeries = app.btEquity.addLineSeries({ color: '#2ecc71', lineWidth: 2 });
    new ResizeObserver(() => app.btEquity && app.btEquity.applyOptions({
      width: $('btEquity').clientWidth, height: $('btEquity').clientHeight })).observe($('btEquity'));
  }
  const seen = new Set();
  const data = [];
  curve.forEach((p) => {
    const t = Math.floor(p.ts / 1000);
    if (seen.has(t)) return;
    seen.add(t);
    data.push({ time: t, value: p.equity });
  });
  data.sort((a, b) => a.time - b.time);
  app.btEquitySeries.setData(data);
  app.btEquity.timeScale().fitContent();
}

function renderBtChart(trades) {
  const LWC = window.LightweightCharts;
  if (!app.btChart) {
    app.btChart = LWC.createChart($('btChart'), {
      layout: { background: { type: 'solid', color: '#0b1017' }, textColor: '#c6d2df', fontSize: 11 },
      grid: { vertLines: { color: '#131b25' }, horzLines: { color: '#131b25' } },
      rightPriceScale: { borderColor: '#243146' },
      timeScale: { borderColor: '#243146', timeVisible: true, secondsVisible: false },
    });
    app.btChartSeries = app.btChart.addLineSeries({ color: '#4da3ff', lineWidth: 1, priceLineVisible: false });
    new ResizeObserver(() => app.btChart && app.btChart.applyOptions({
      width: $('btChart').clientWidth, height: $('btChart').clientHeight })).observe($('btChart'));
  }
  const fills = trades.filter((t) => t.kind === 'entry' || t.kind === 'add');
  const seen = new Set();
  const data = [];
  fills.forEach((t) => {
    const tm = Math.floor(t.ts / 1000);
    if (seen.has(tm)) return;
    seen.add(tm);
    data.push({ time: tm, value: t.price });
  });
  app.btChartSeries.setData(data);
  const markers = fills.map((t) => ({
    time: Math.floor(t.ts / 1000),
    position: t.side === 'long' ? 'belowBar' : 'aboveBar',
    color: t.kind === 'entry' ? '#26a69a' : '#f0b90b',
    shape: 'circle',
    text: `${t.idx}`,
  }));
  app.btChartSeries.setMarkers(markers.slice(-200));
  app.btChart.timeScale().fitContent();
}

/* ------------------------------------------------------------------ actions */
async function loadSettings() {
  app.settings = await api('GET', '/api/settings');
  renderSettingsForm(app.settings);
  renderBacktestForm(app.settings);
}

async function saveSettings() {
  try {
    const body = collectSettings();
    const res = await api('PUT', '/api/settings', body);
    app.settings = res.settings;
    renderSettingsForm(app.settings);
    toast('Настройки сохранены' + (res.restarted ? ', бот перезапущен' : ''), 'ok');
  } catch (e) { toast('Ошибка сохранения: ' + e.message, 'err'); }
}

async function testApi() {
  try {
    const res = await api('POST', '/api/settings/test-connection', collectSettings());
    if (res.ok && res.demo) toast('DEMO-режим: биржа не нужна', 'ok');
    else if (res.ok) toast(`Подключение успешно. Баланс: ${JSON.stringify(res.balance)}`, 'ok');
    else toast('Ошибка: ' + (res.error || 'неизвестно'), 'err');
  } catch (e) { toast('Ошибка: ' + e.message, 'err'); }
}

async function botAction(path, label) {
  try {
    const res = await api('POST', path);
    if (res && res.ok === false) toast(label + ': ' + (res.error || 'ошибка'), 'err');
    else toast(label + ': выполнено', 'ok');
  } catch (e) { toast(label + ': ' + e.message, 'err'); }
}

/* --------------------------------------------------------------------- init */
function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('active', p.id === 'tab-' + name));
  app.chartTabActive = name === 'chart';
  if (app.chartTabActive) { setTimeout(() => { updateChart(true); }, 50); }
  if (name === 'history') loadHistory();
  if (name === 'positions') loadPositions();
  if (app.chart) app.chart.applyOptions({ width: $('chart').clientWidth, height: $('chart').clientHeight });
  if (app.btEquity) app.btEquity.applyOptions({ width: $('btEquity').clientWidth });
  if (app.btChart) app.btChart.applyOptions({ width: $('btChart').clientWidth });
}

async function init() {
  document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => switchTab(t.dataset.tab)));
  $('btnStart').onclick = () => botAction('/api/bot/start', 'Старт');
  $('btnPause').onclick = () => {
    const paused = app.snapshot && app.snapshot.paused;
    botAction(paused ? '/api/bot/resume' : '/api/bot/pause', paused ? 'Продолжение' : 'Пауза');
  };
  $('btnStop').onclick = () => botAction('/api/bot/stop', 'Стоп');
  $('btnCloseAll').onclick = () => {
    if (confirm('Закрыть все открытые позиции по рынку?')) botAction('/api/bot/close-all', 'Закрытие');
  };
  $('btnSaveSettings').onclick = saveSettings;
  $('btnTestApi').onclick = testApi;
  $('btnRefreshPos').onclick = loadPositions;
  $('btnRefreshCycles').onclick = loadHistory;
  $('btnExportCycles').onclick = exportCyclesCsv;
  $('btnRunBacktest').onclick = runBacktest;
  $('btnFit').onclick = () => app.chart && app.chart.timeScale().fitContent();
  $('chkFib').onchange = () => updateChart(true);
  $('chkMarks').onchange = () => updateChart(true);
  $('btnClearLogs').onclick = () => { $('logs').innerHTML = ''; app.logs = []; };

  try {
    await loadSettings();
    app.snapshot = await api('GET', '/api/state');
    renderSnapshot(app.snapshot);
  } catch (e) { toast('Не удалось загрузить состояние: ' + e.message, 'err'); }

  const logs = await api('GET', '/api/logs?limit=200').catch(() => ({ logs: [] }));
  (logs.logs || []).forEach(pushLog);
  connectWS();
  loadHistory();
  // страховка: если WebSocket недоступен (прокси/авторизация) — опрашиваем API
  setInterval(async () => {
    if (app.ws && app.ws.readyState === 1) return;
    try {
      app.snapshot = await api('GET', '/api/state');
      renderSnapshot(app.snapshot);
    } catch (e) { /* сервер недоступен */ }
  }, 3000);
}

document.addEventListener('DOMContentLoaded', init);
