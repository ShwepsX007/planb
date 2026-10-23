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
const duration = (ms) => {
  if (ms === null || ms === undefined || !Number.isFinite(Number(ms)) || Number(ms) < 0) return '—';
  let seconds = Math.floor(Number(ms) / 1000);
  const days = Math.floor(seconds / 86400); seconds %= 86400;
  const hours = Math.floor(seconds / 3600); seconds %= 3600;
  const mins = Math.floor(seconds / 60);
  return `${days ? `${days}д ` : ''}${String(hours).padStart(2, '0')}:${String(mins).padStart(2, '0')}`;
};

/* Префикс пути: панель работает и напрямую (http://host:8015/), и за
   nginx-префиксом (https://host/planb/) — все запросы относительные. */
const BASE = location.pathname.replace(/[^/]*$/, '') || '/';

const app = {
  snapshot: null, settings: null, logs: [], ws: null, wsTimer: null,
  chart: null, series: null, priceLines: [], lastChartUpdate: 0, chartTabActive: false,
  btChart: null, btSeries: null, btEquity: null, btEquitySeries: null, btLoad: null, btLoadSeries: null, btLines: [],
  openPositions: [], cycles: [], trades: [],
};

/* ------------------------------------------------------------------ helpers */
async function api(method, path, body) {
  let res;
  try {
    res = await fetch(BASE + path.replace(/^\//, ''), {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
      credentials: 'same-origin',
    });
  } catch (e) {
    const err = new Error('нет связи с сервером бота');
    err.network = true;
    throw err;
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
  if (!res.ok) {
    let msg = data && data.error
      ? (typeof data.error === 'string' ? data.error : JSON.stringify(data.error))
      : (res.statusText || ('HTTP ' + res.status));
    if (res.status === 401) msg = 'нужна авторизация (логин и пароль панели)';
    if (res.status === 404) msg = 'обработчик не найден — проверьте префикс пути (nginx)';
    const err = new Error(msg);
    err.status = res.status;
    throw err;
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
  let ws;
  try {
    ws = new WebSocket(`${proto}://${location.host}${BASE}ws`);
  } catch (e) {
    scheduleWSReconnect(false);
    return;
  }
  app.ws = ws;
  let opened = false;
  ws.onopen = () => { opened = true; app.wsFailures = 0; };
  ws.onmessage = (ev) => {
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === 'snapshot') { app.snapshot = msg.snapshot; renderSnapshot(msg.snapshot); }
    else if (msg.type === 'log') { pushLog(msg.log); }
  };
  ws.onclose = () => { scheduleWSReconnect(opened); };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}

/* Если сервер закрывает рукопожатие (например панель защищена паролем и браузер
   не передал заголовок авторизации) — не долбим сервер, а ждём дольше:
   панель в это время обновляется обычными запросами /api/state. */
function scheduleWSReconnect(opened) {
  if (opened) app.wsFailures = 0;
  app.wsFailures = (app.wsFailures || 0) + 1;
  const delay = Math.min(30000, 2500 * app.wsFailures);
  clearTimeout(app.wsTimer);
  app.wsTimer = setTimeout(connectWS, delay);
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
  const real = s.data_real !== false && s.data_source !== 'synthetic';
  const dataBadge = $('dataBadge');
  if (dataBadge) {
    dataBadge.textContent = real ? 'РЕАЛЬНЫЕ ДАННЫЕ GATE.IO' : 'СИНТЕТИКА (ВЫДУМАННЫЕ СВЕЧИ)';
    dataBadge.className = 'badge ' + (real ? 'badge-real' : 'badge-demo');
    dataBadge.title = s.data_source_label || '';
    dataBadge.classList.remove('hidden');
  }
  $('pairInfo').textContent = `${s.symbol} · ${s.timeframe}` + (s.market_type === 'futures' ? ' · фьюч.' : ' · спот');
  $('priceInfo').textContent = s.price ? fmt(s.price, s.price > 100 ? 2 : 5) : '—';
  $('btnStart').disabled = st === 'running';
  $('btnPause').disabled = st !== 'running';
  $('btnStop').disabled = st === 'stopped' || st === 'error';
  const e = s.engine || {};
  const activeCycles = Array.isArray(e.cycles) && e.cycles.length
    ? e.cycles : (e.cycle ? [e.cycle] : []);
  const c = e.cycle || activeCycles[0] || null;
  $('btnCloseAll').disabled = activeCycles.length === 0;

  $('cPnl').textContent = money(e.realized_pnl, 4);
  $('cPnl').className = 'card-value ' + cls(e.realized_pnl);
  const target = s.target_total_profit || 0;
  const prog = target ? Math.max(0, Math.min(100, (e.realized_pnl / target) * 100)) : 0;
  $('cProgressBar').style.width = prog + '%';
  $('cProgressText').textContent = target ? `цель: ${fmt(target, 2)} $ · ${prog.toFixed(1)} %` : 'цель не задана';

  const unrealizedTotal = activeCycles.reduce((sum, cycle) => sum + Number(cycle.unrealized_pnl || 0), 0);
  $('cUnreal').textContent = activeCycles.length ? money(unrealizedTotal, 4) : '—';
  $('cUnreal').className = 'card-value ' + (activeCycles.length ? cls(unrealizedTotal) : '');
  $('cUnrealHint').textContent = activeCycles.length
    ? `${activeCycles.length} активных цикла · оценка по цене ${fmt(s.price, s.price > 100 ? 2 : 5)}`
    : 'нет открытых позиций';

  $('cAvg').textContent = c ? fmt(c.avg_entry, 2) : '—';
  $('cAvgHint').textContent = c ? `позиций: ${c.positions.length} · объём ${fmt(c.total_volume_usd, 2)} $ · циклов ${activeCycles.length}` : 'позиций: 0';

  $('cTarget').textContent = c ? fmt(c.target, 2) : '—';
  $('cTargetHint').textContent = c
    ? `уровень ${c.goal_pct}% · сетка ${fmt(c.swing_low, 2)}–${fmt(c.swing_high, 2)}` +
      (c.move_side === 'up' ? ' (0 сверху)' : ' (0 снизу)')
    : 'уровень —';

  $('cProfitToTarget').textContent = c ? pct(c.profit_pct_to_target) : '—';
  $('cProfitToTarget').className = 'card-value ' + (c ? (c.profit_pct_to_target >= (app.settings?.min_profit_pct ?? 0) ? 'pos' : 'neg') : '');
  $('cProfitToTargetHint').textContent = c ? `мин. профит ${app.settings?.min_profit_pct ?? '—'} % · оценка ${money(c.expected_pnl_at_target, 4)}` : 'мин. профит —';

  $('cCycles').textContent = e.cycles_total ?? 0;
  $('cCyclesHint').textContent = `прибыльных ${e.wins ?? 0} · убыточных ${e.losses ?? 0} · сигналов ${e.signals_found ?? 0}`;

  const day = s.stats_24h || {};
  $('cDailyPnl').textContent = money(day.pnl ?? 0, 4);
  $('cDailyPnl').className = 'card-value ' + cls(day.pnl ?? 0);
  $('cDailySummary').textContent = `циклов ${day.cycles ?? 0} · прибыльных ${day.wins ?? 0} · убыточных ${day.losses ?? 0}`;
  $('cDailyWindow').textContent = day.reset_at ? 'с последнего сброса · не более 24 ч' : 'скользящие последние 24 часа';

  const b = s.balance || {};
  $('cBalance').textContent = b.total !== undefined ? fmt(b.total, 2) + ' $' : '—';
  $('cBalanceHint').textContent = b.mode === 'paper' ? `виртуальный (старт ${fmt(b.start_balance, 2)} $)` :
    (b.free !== undefined ? `свободно ${fmt(b.free, 2)} ${b.quote || ''}` : '—');

  $('cNextAdd').textContent = c && c.next_position_volume_usd ? fmt(c.next_position_volume_usd, 2) + ' $' : '—';
  $('cNextAddHint').textContent = c ? (c.next_position_idx ? `позиция #${c.next_position_idx} · осталось ${c.positions_left}` : 'лимит позиций достигнут') : '—';

  renderStateHint(s, e, c);
  renderCycleBox(c, e, s, activeCycles);
  renderCurrentPositions(activeCycles);
  renderPendingOrders(s.pending_orders || []);
  renderEngineBox(e, s, c);
  renderLogic(s);
  if (app.chartTabActive) maybeUpdateChart();
}

function renderStateHint(s, e, c) {
  const box = $('stateHint');
  if (!box) return;
  let html = '';
  let kind = '';
  if (s.status === 'stopped' || s.status === 'error') {
    kind = s.status === 'error' ? 'err' : 'warn';
    html = s.status === 'error'
      ? `<b>Бот не смог запуститься:</b> ${escapeHtml(s.error || 'см. журнал')}. ` +
        'Проверьте журнал ниже и настройки (источник данных, ключи биржи).'
      : '<b>Бот не запущен.</b> Нажмите <b>«Старт»</b> вверху страницы. ' +
        (s.data_real === false
          ? 'Сейчас выбран источник «синтетика» — свечи выдуманные. Чтобы работать на реальных данных, ' +
            'выберите на вкладке «Настройки» источник «Gate.io» (API-ключи для этого не нужны).'
          : 'Бот будет торговать виртуальными деньгами (paper) по реальным свечам Gate.io — ' +
            'API-ключи для этого не нужны.');
  } else if (s.status === 'running' && !s.price) {
    kind = '';
    html = 'Бот запущен и получает первые свечи — данные появятся в течение минуты.';
  } else if (s.status === 'running' && s.feed && s.feed.last_ok_ts
             && Date.now() - s.feed.last_ok_ts > 180000) {
    kind = 'err';
    html = '<b>Данные с биржи перестали приходить</b> (последний ответ больше 3 минут назад). ' +
      'Проверьте связь с биржей: <code>curl -s -m 10 https://api.gateio.ws/api/v4/spot/time</code>';
  } else if (s.status === 'running' && s.data_real !== false && !c && scanEmpty(s)
             && scanFresh(s)) {
    // бот действительно молчит: свечи идут, но входов нет — объясняем цифрами
    const scan = s.signal_scan || {};
    const hours = scan.minutes ? Math.max(1, Math.round(scan.minutes / 60)) : 0;
    kind = 'warn';
    html = `<b>Свечи идут, но входов нет.</b> За последние ${fmt(scan.windows, 0)} окон ` +
      `(${scan.candles} свечей ${scan.timeframe}${hours ? `, ≈ ${hours} ч` : ''}) стратегия не нашла ` +
      `ни одного сигнала: движения были от ${fmt(scan.move_median_pct, 2)} % (медиана) до ` +
      `${fmt(scan.move_max_pct, 2)} % (максимум), а порог входа — ${fmt(scan.threshold_pct, 2)} %. ` +
      (scan.last_reason ? `<br><b>Причина:</b> ${escapeHtml(scan.last_reason)}. ` : '') +
      scanAdvice(scan) +
      ` <button class="btn btn-small" id="btnScanSignals">Проверить сигналы заново</button>`;
  } else if (s.status === 'target_reached') {
    kind = 'ok';
    html = `<b>Цель по общему профиту достигнута</b> (${fmt(e.realized_pnl, 4)} $). ` +
      'Бот остановлен. Чтобы продолжить: увеличьте цель на вкладке «Настройки» ' +
      'или нажмите «Сбросить статистику», затем «Старт».';
  } else if (s.feed && s.feed.last_error) {
    kind = 'err';
    html = `<b>Нет данных с биржи:</b> ${escapeHtml(s.feed.last_error)}. ` +
      'Проверьте интернет на сервере (команда <code>curl -s -m 10 https://api.gateio.ws/api/v4/spot/time</code>) ' +
      'или переключитесь на синтетические свечи на вкладке «Настройки».';
  } else if (s.status === 'stopped_by_stop_loss') {
    kind = 'err';
    html = '<b>Цикл закрыт аварийным стоп-лоссом</b> — проверьте журнал и параметры риска ' +
      '(лимит позиций, минимальный профит, стоп цикла).';
  }
  box.className = 'hint-banner ' + kind + (html ? '' : ' hidden');
  box.innerHTML = html;
  const scanButton = $('btnScanSignals');
  if (scanButton) scanButton.onclick = scanSignalsNow;
}

function scanAdvice(scan) {
  // совет зависит от причины, которую назвал сам детектор, — без угадывания.
  // Порядок важен: «коррекция ... уже дошла до цели» содержит слово «коррекция»,
  // но совет там другой, поэтому специфичные причины идут первыми.
  const reason = String(scan.last_reason || '');
  const rules = [
    ['поздно', 'Цена уже прошла цель коррекции — ждите нового движения или уменьшите «Цель по Фибо».'],
    ['продолжается', 'Движение ещё не закончилось: ждём откат, входить не на чем.'],
    ['вернулась к началу', 'Цена откатилась к началу движения — ждите нового движения.'],
    ['свежести', 'Увеличьте «Экстремум не старше, свечей» или окно поиска движения.'],
    ['рано', 'Уменьшите «Мин. коррекция для входа» — цена ещё не отошла от экстремума.'],
    ['мало свечей', 'Бот ещё набирает историю — данные появятся в течение минуты.'],
    ['зигзаг', 'Движение не выражено: уменьшите «Минимальное движение цены» или берите таймфрейм крупнее.'],
    ['отключено', 'Включите нужное направление в настройках (long/short).'],
    ['профит до цели', 'Уменьшите «Минимальный профит» или «Цель по Фибо» — до цели слишком близко.'],
    ['порога', `Уменьшите «Минимальное движение цены» (например до ${suggestThreshold(scan)}) ` +
      'или возьмите таймфрейм крупнее — тогда входов станет больше.'],
  ];
  for (const [needle, advice] of rules) {
    if (reason.includes(needle)) return advice;
  }
  return 'Откройте «Настройки» и проверьте пороги входа.';
}

function scanFresh(s) {
  // скан старше 30 минут ничего не объясняет: числа могли устареть
  const scan = s.signal_scan || {};
  return !scan.updated_at || (Date.now() - scan.updated_at < 30 * 60 * 1000);
}

function scanEmpty(s) {
  const scan = s.signal_scan;
  return !!(scan && scan.enough_data && !scan.error && !scan.signals);
}

function suggestThreshold(scan) {
  // сервер считает то же самое — берём его значение, чтобы панель и журнал не расходились
  if (scan.suggested_threshold_pct) return fmt(scan.suggested_threshold_pct, 2) + ' %';
  const base = scan.move_median_pct || 0;
  const value = Math.max(0.05, Math.min((scan.threshold_pct || 0.6) * 0.6,
                                        (scan.move_max_pct || 0.2) * 0.8,
                                        Math.max(base * 2, 0.1)));
  return parseFloat(value.toFixed(2)) + ' %';
}

async function scanSignalsNow() {
  const button = $('btnScanSignals');
  if (button) { button.disabled = true; button.textContent = 'считаю…'; }
  try {
    const res = await api('GET', '/api/signals/scan?refresh=1&limit=1000');
    if (!res.ok) throw new Error(res.error || 'не удалось');
    if (res.signals > 0) {
      toast(`Нашлось сигналов: ${res.signals} за ${res.windows} окон — входы будут`, 'ok');
    } else {
      toast(`Сигналов нет: движения до ${fmt(res.move_max_pct, 2)} % при пороге ` +
            `${fmt(res.threshold_pct, 2)} %`, 'err');
    }
    app.snapshot = await api('GET', '/api/state');
    renderSnapshot(app.snapshot);
  } catch (e) {
    toast('Сканер сигналов: ' + e.message, 'err');
    if (button) { button.disabled = false; button.textContent = 'Проверить сигналы заново'; }
  }
}

function kv(rows) {
  return rows.map(([k, v, klass]) => `<div class="k">${k}</div><div class="v ${klass || ''}">${v}</div>`).join('');
}

function renderCycleBox(c, e, s, cycles = []) {
  const box = $('cycleBox');
  if (!cycles.length && c) cycles = [c];
  if (!cycles.length) { box.innerHTML = kv([['Статус', '<span class="muted">нет активного цикла — ждём сигнал</span>'],
    ['Проверка', e.last_check || '—']]); return; }
  const rows = [];
  cycles.forEach((cycle, index) => {
    if (index > 0) rows.push([`Встречный цикл (${cycle.side})`, `<b>${escapeHtml(cycle.id)}</b>`]);
    rows.push(
      [index === 0 ? 'Цикл' : `Цикл ${index + 1}`, escapeHtml(cycle.id)],
      ['Сторона', cycle.side === 'long' ? 'лонг (коррекция вверх)' : 'шорт (коррекция вниз)'],
      ['Открыт / длительность', `${ts(cycle.opened_ts)} · ${duration(Date.now() - cycle.opened_ts)}`],
      ['Свечей в импульсе', escapeHtml(cycle.note || '—')],
      ['Сетка Фибо', `${fmt(cycle.swing_low, 2)} … ${fmt(cycle.swing_high, 2)}`],
      ['Цель (' + cycle.goal_pct + '%)', `<b>${fmt(cycle.target, 2)}</b>`],
      ['Средняя входа', `<b>${fmt(cycle.avg_entry, 2)}</b>`],
      ['Профит до цели', `<b>${pct(cycle.profit_pct_to_target)}</b> (мин. ${app.settings?.min_profit_pct ?? '—'}%)`],
      ['Ожидаемый профит', `${money(cycle.expected_pnl_at_target, 4)} при закрытии по цели`],
      ['Плавающий P/L цикла', money(cycle.unrealized_pnl, 4)],
      ['Комиссии входа', money((cycle.positions || []).reduce((sum, position) =>
        sum + Number(position.fee || 0), 0), 4)],
      ['Позиций', `${cycle.positions.length} из ${app.settings?.max_positions ?? '—'} (макс. ${cycle.max_positions_used})`],
      ['Вложено', `${fmt(cycle.total_volume_usd, 2)} $`],
      ['Номинал / оценка маржи', `${fmt(Number(cycle.total_qty || 0) * Number(s.price || 0), 2)} $ / ` +
        `${fmt(Number(cycle.total_qty || 0) * Number(s.price || 0) /
          (s.market_type === 'futures' ? Math.max(1, Number(app.settings?.leverage || 1)) : 1), 2)} $`],
      ['До цели по цене', cycle.distance_to_target_pct !== undefined ? pct(cycle.distance_to_target_pct) : '—'],
    );
  });
  box.innerHTML = kv(rows);
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
  const account = s.account_metrics || {};
  rows.push(['Баланс total / свободно', `${fmt(account.total_balance, 2)} $ / ${fmt(account.free_balance, 2)} $`]);
  rows.push(['Активные циклы', fmt(account.active_cycles || 0, 0)]);
  rows.push(['Занято (reported)', `${fmt(account.exchange_used, 2)} $ · ${account.used_basis || '—'}`]);
  rows.push(['Экспозиция цикла (номинал)', `${fmt(account.cycle_notional, 2)} $`]);
  rows.push(['Оценка требуемой маржи', `${fmt(account.estimated_margin, 2)} $ (${pct(account.deposit_load_pct, 2)})`]);
  rows.push(['Как оценена маржа', account.margin_basis || '—']);
  if (c) {
    rows.push(['Время в цикле', duration(Date.now() - c.opened_ts)]);
    rows.push(['Худший плавающий P/L цикла', `${money(-Math.abs(Number(c.max_adverse_pnl_usd || 0)), 4)} (${pct(-Math.abs(Number(c.max_adverse_pnl_pct || 0)), 2)} от входного объёма)`]);
    rows.push(['Пиковая нагрузка цикла', `${fmt(c.peak_margin_usd, 2)} $ · ${pct(c.max_deposit_load_pct, 2)}`]);
  }
  const feed = s.feed || {};
  rows.push(['Источник данных', feed.label || (s.data_real ? 'реальные свечи' : 'синтетика')]);
  if (feed.live_ts) rows.push(['Текущая свеча', ts(feed.live_ts)]);
  if (e.last_candle_ts) rows.push(['Обработана свеча', ts(e.last_candle_ts)]);
  if (feed.last_ok_ts) rows.push(['Данные получены', timeOnly(feed.last_ok_ts)]);
  if (e.candles_seen) rows.push(['Свечей обработано за сессию', e.candles_seen]);

  // сканер сигналов: сколько раз стратегия сработала бы на последних свечах
  const scan = s.signal_scan;
  if (scan && !scan.error && scan.enough_data) {
    const hours = scan.minutes ? Math.max(1, Math.round(scan.minutes / 60)) : 0;
    const period = `${fmt(scan.windows, 0)} окон${hours ? ` ≈ ${hours} ч` : ''}`;
    rows.push(['Сигналов по истории', scan.signals > 0
      ? `<b class="pos">${scan.signals}</b> за ${period}`
      : `<b class="neg">0</b> за ${period}`]);
    rows.push(['Движение в окне настроек',
      `медиана ${fmt(scan.move_median_pct, 2)} % · макс ${fmt(scan.move_max_pct, 2)} % · порог ${fmt(scan.threshold_pct, 2)} %`]);
  }
  if (feed.last_error) rows.push(['Ошибка данных', `<span class="neg">${escapeHtml(feed.last_error)}</span>`]);
  if (s.error) rows.push(['Ошибка', `<span class="neg">${escapeHtml(s.error)}</span>`]);
  $('engineBox').innerHTML = kv(rows);
}

function renderCurrentPositions(cycles) {
  const tbody = $('currentPositions').querySelector('tbody');
  const active = Array.isArray(cycles) ? cycles : (cycles ? [cycles] : []);
  const positions = active.flatMap((cycle) => (cycle.positions || []).map((position) => ({
    ...position, side: cycle.side,
  })));
  if (!positions.length) {
    tbody.innerHTML = '<tr class="empty"><td colspan="12">позиций нет</td></tr>';
    return;
  }
  const price = app.snapshot?.price || 0;
  const now = Date.now();
  tbody.innerHTML = positions.map((p) => {
    const gross = p.side === 'long' ? (price - p.price) * p.qty : (p.price - price) * p.qty;
    const pnl = gross - Number(p.fee || 0);
    const markValue = price * p.qty;
    return `<tr><td><b>${p.idx}</b></td><td>${p.side === 'long' ? 'Лонг' : 'Шорт'}</td>
      <td>${ts(p.ts)}</td><td>${duration(now - p.ts)}</td><td>${fmt(p.price, 2)}</td>
      <td>${fmt(p.volume_usd, 2)}</td><td>${fmt(p.qty, 8)}</td>
      <td>${p.multiplier && p.multiplier > 1 ? `<b class="pos">x${p.multiplier}</b>` : 'x1'}</td>
      <td>${fmt(markValue, 2)}</td><td>${fmt(p.fee, 4)}</td>
      <td class="${cls(pnl)}">${money(pnl, 4)}</td><td class="${cls(pnl)}">${pct(p.volume_usd ? pnl / p.volume_usd * 100 : 0, 3)}</td></tr>`;
  }).join('');
}

function renderPendingOrders(orders) {
  const box = $('pendingOrdersBox');
  if (!box) return;
  if (!orders.length) {
    box.innerHTML = '<div class="k">Заявки</div><div class="v muted">нет активных заявок</div>';
    return;
  }
  box.innerHTML = orders.map((order) => {
    const isTp = order.role === 'tp';
    const label = isTp ? 'Тейк-профит' : `Долив #${order.idx ?? '—'}`;
    const detail = isTp
      ? `${fmt(order.qty, 6)} · цена ${fmt(order.price, 2)}`
      : `${fmt(order.volume_usd, 2)} $ · цена ${fmt(order.price, 2)}`;
    return `<div class="k">${label}</div><div class="v">${detail} <span class="muted">(${escapeHtml(order.status || 'активна')})</span></div>`;
  }).join('');
}

function renderLogic(s) {
  const st = app.settings || {};
  const swing = (st.entry_mode || 'swing') !== 'impulse';
  const modeEl = $('lgMode');
  if (modeEl) {
    modeEl.textContent = swing
      ? 'экстремумы движения определяются автоматически; фиксированного числа свечей нет'
      : `N свечей в одну сторону (${st.candles_count ?? '—'} шт.)`;
  }
  $('lgCandles').textContent = st.candles_count ?? '—';
  const lookEl = $('lgLookback');
  if (lookEl) lookEl.textContent = st.swing_lookback ?? '—';
  $('lgTf').textContent = s.timeframe || '—';
  $('lgMove').textContent = st.min_move_pct ?? '—';
  $('lgGoal').textContent = st.fib_goal_pct ?? '—';
  $('lgMinProfit').textContent = st.min_profit_pct ?? '—';
  $('lgMult').textContent = st.volume_multiplier ?? '—';
  $('lgTotal').textContent = st.total_profit_target_usd ?? '—';
  renderCostWarn(st);
  const schedule = s.volume_schedule || st.volume_schedule || [];
  const html = schedule.map((v, i) => `<span class="chip">#${i + 1}: <b>${fmt(v, 2)} $</b></span>`).join('');
  $('scheduleBox').innerHTML = html;
  $('planTable').innerHTML = html;
  $('chartPair').textContent = `${s.symbol} · ${s.timeframe}`;
}

function renderCostWarn(st) {
  // Минимальный профит меньше стоимости круга (комиссия + слиппедж туда-обратно)
  // означает гарантированный минус в каждом закрытом цикле — об этом надо сказать.
  const el = $('lgCostWarn');
  if (!el) return;
  const fee = Number(st.fee_pct || 0);
  const slip = Number(st.slippage_pct || 0);
  const minProfit = Number(st.min_profit_pct || 0);
  const cost = 2 * (fee + slip);
  if (minProfit > 0 && cost > 0 && minProfit < cost) {
    el.classList.remove('hidden');
    el.innerHTML = `<b>Минимальный профит ${fmt(minProfit, 3)} % меньше стоимости круга ` +
      `≈ ${fmt(cost, 3)} %</b> (комиссия ${fmt(fee, 3)} % + слиппедж ${fmt(slip, 3)} % туда и обратно). ` +
      `Даже цикл, закрытый по цели, уйдёт в минус — поставьте минимальный профит больше ` +
      `${fmt(cost, 3)} %.`;
  } else {
    el.classList.add('hidden');
  }
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
        lineStyle: LWC.LineStyle.Solid, axisLabelVisible: true, title: 'цель / TP',
      }));
    }
    (data.pending_orders || []).filter((order) => order.role === 'add').forEach((order) => {
      const amount = Number(order.volume_usd || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
      const qty = Number(order.qty || 0).toLocaleString(undefined, { maximumFractionDigits: 6 });
      app.priceLines.push(app.series.createPriceLine({
        price: order.price, color: '#f0b90b', lineWidth: 2,
        lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true,
        title: `лимит долива #${order.idx} · $${amount} / ${qty} ${order.base || ''}`,
      }));
    });
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
    const [cy, pos] = await Promise.all([api('GET', '/api/cycles?limit=5000'), api('GET', '/api/positions?limit=10000')]);
    app.cycles = cy.cycles || []; app.trades = pos.positions || [];
    renderCycles(); renderTrades(); renderRecentCycles();
  } catch (e) { toast('Не удалось загрузить историю: ' + e.message, 'err'); }
}

async function refreshRecentCycles() {
  try {
    const data = await api('GET', '/api/cycles?limit=5');
    app.recentCycles = data.cycles || [];
    renderRecentCycles();
  } catch (_) { /* keep the last rendered cycles during transient API failures */ }
}

function renderRecentCycles() {
  const table = $('chartRecentCycles');
  if (!table) return;
  const tbody = table.querySelector('tbody');
  const cycles = app.recentCycles || app.cycles.slice(0, 5);
  if (!cycles.length) {
    tbody.innerHTML = '<tr class="empty"><td colspan="5">завершённых циклов пока нет</td></tr>';
    return;
  }
  tbody.innerHTML = cycles.slice(0, 5).map((c) => `<tr>
    <td title="${escapeHtml(c.id)}">${escapeHtml(String(c.id).slice(-8))}</td>
    <td>${fmt(c.positions_count, 0)}</td><td>${fmt(c.avg_entry, 2)}</td>
    <td>${fmt(c.target, 2)}</td><td class="${cls(c.pnl)}"><b>${money(c.pnl, 4)}</b></td></tr>`).join('');
}

function renderCycles() {
  const tbody = $('cyclesTable').querySelector('tbody');
  if (!app.cycles.length) { tbody.innerHTML = '<tr class="empty"><td colspan="18">циклов пока нет</td></tr>'; return; }
  tbody.innerHTML = app.cycles.map((c) => {
    const closeTime = c.closed_ts ? ts(c.closed_ts) : '<span class="muted">открыт</span>';
    const span = (c.closed_ts || Date.now()) - (c.opened_ts || 0);
    return `<tr><td title="${escapeHtml(c.id)}">${escapeHtml(c.id)}</td><td>${escapeHtml(c.side || '')}</td>
      <td>${ts(c.opened_ts)}</td><td>${closeTime}</td><td>${duration(span)}</td>
      <td>${fmt(c.positions_count, 0)}</td><td>${fmt(c.avg_entry, 2)}</td><td>${fmt(c.target, 2)}</td>
      <td>${fmt(c.close_price, 2)}</td><td>${fmt(c.volume_usd, 2)}</td><td>${fmt(c.fees, 4)}</td>
      <td>${money(-Math.abs(Number(c.max_adverse_pnl_usd || 0)), 2)} / ${pct(-Math.abs(Number(c.max_adverse_pnl_pct || 0)), 2)}</td>
      <td>${fmt(c.peak_notional_usd, 2)}</td><td>${fmt(c.peak_margin_usd, 2)}</td>
      <td>${pct(c.max_deposit_load_pct, 2)}</td>
      <td class="${cls(c.pnl)}"><b>${money(c.pnl, 4)}</b></td>
      <td class="${cls(c.pnl_pct)}">${pct(c.pnl_pct)}</td><td class="muted">${escapeHtml(c.close_reason || '')}</td></tr>`;
  }).join('');
}

function renderTrades() {
  const tbody = $('tradesTable').querySelector('tbody');
  if (!app.trades.length) { tbody.innerHTML = '<tr class="empty"><td colspan="15">сделок пока нет</td></tr>'; return; }
  const now = Date.now();
  tbody.innerHTML = app.trades.map((p) => {
    const closed = Boolean(p.close_ts && p.close_price);
    const fees = Number(p.fee || 0) + Number(p.close_fee || 0);
    const pnlPct = p.volume_usd ? Number(p.pnl || 0) / p.volume_usd * 100 : 0;
    return `<tr><td>${escapeHtml(p.cycle_id || '')}</td><td><b>${p.idx}</b></td><td>${escapeHtml(p.side || '')}</td>
      <td>${ts(p.ts)}</td><td>${closed ? ts(p.close_ts) : '<span class="muted">открыта</span>'}</td>
      <td>${duration((closed ? p.close_ts : now) - p.ts)}</td><td>${fmt(p.price, 2)}</td>
      <td>${closed ? fmt(p.close_price, 2) : '—'}</td><td>${fmt(p.volume_usd, 2)}</td>
      <td>${closed ? fmt(Number(p.close_price) * Number(p.qty), 2) : '—'}</td><td>${fmt(p.qty, 8)}</td>
      <td>${p.multiplier && p.multiplier > 1 ? 'x' + p.multiplier : 'x1'}</td><td>${fmt(fees, 4)}</td>
      <td class="${cls(p.pnl)}">${closed ? money(p.pnl, 4) : '—'}</td>
      <td class="${cls(p.pnl)}">${closed ? pct(pnlPct, 3) : '—'}</td></tr>`;
  }).join('');
}

function exportCyclesCsv() {
  const head = ['id', 'side', 'opened_ts', 'closed_ts', 'duration_ms', 'positions', 'avg_entry', 'target',
    'close_price', 'volume_usd', 'max_adverse_pnl_usd', 'max_adverse_pnl_pct', 'peak_notional_usd',
    'peak_margin_usd', 'max_deposit_load_pct', 'pnl', 'pnl_pct', 'fees', 'reason'];
  const rows = app.cycles.map((c) => [c.id, c.side, c.opened_ts, c.closed_ts,
    c.closed_ts ? c.closed_ts - c.opened_ts : '', c.positions_count, c.avg_entry, c.target, c.close_price,
    c.volume_usd, c.max_adverse_pnl_usd, c.max_adverse_pnl_pct, c.peak_notional_usd,
    c.peak_margin_usd, c.max_deposit_load_pct, c.pnl, c.pnl_pct, c.fees, c.close_reason]);
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
    { k: 'data_source', l: 'Источник данных', t: 'select', h: 'реальные свечи нужны и в paper, и в live; ключи не требуются', o: [['gate', 'Gate.io — реальные свечи (интернет)'], ['synthetic', 'Синтетика — выдуманные свечи (без интернета)']] },
    { k: 'trade_mode', l: 'Режим торговли (деньги)', t: 'select', h: 'paper = виртуальные сделки, live = реальные ордера', o: [['paper', 'Paper — виртуальные сделки на реальных данных'], ['live', 'LIVE — реальные ордера (нужны API-ключи)']] },
    { k: 'api_key', l: 'API key', t: 'text', h: 'пусто = не менять' },
    { k: 'api_secret', l: 'API secret', t: 'password', h: 'хранится только на сервере' },
    { k: 'sandbox', l: 'Testnet', t: 'bool' },
    { k: 'leverage', l: 'Плечо (фьючерсы)', t: 'number', step: 1, min: 1, max: 100, int: true },
    { k: 'margin_mode', l: 'Маржа', t: 'select', o: [['isolated', 'Изолированная'], ['cross', 'Кросс']] },
  ] },
  { group: 'Данные и сигнал', fields: [
    { k: 'timeframe', l: 'Таймфрейм', t: 'tf' },
    { k: 'entry_mode', l: 'Метод поиска движения', t: 'select',
      o: [['swing', 'Автоматически по движению (без фиксированного N свечей)'],
          ['impulse', 'Фиксированное N свечей в одну сторону (старый метод)']],
      h: 'swing сам находит экстремумы, проверяет минимальный % движения и ждёт коррекцию до цели Фибо' },
    { k: 'swing_lookback', l: 'Макс. окно поиска, свечей', t: 'number', step: 10, min: 10, max: 500, int: true,
      h: 'максимальная глубина истории для поиска начала движения; фактическое число свечей определяется по экстремумам' },
    { k: 'swing_fresh_candles', l: 'Макс. возраст экстремума, свечей', t: 'number', step: 1, min: 2, max: 200, int: true,
      h: 'не входить, если последний максимум/минимум движения старше этого значения' },
    { k: 'min_correction_pct', l: 'Мин. коррекция для входа, % сетки', t: 'number', step: 1, min: 0, max: 95,
      h: 'вход только когда цена отошла от экстремума (0 — входить у экстремума, как раньше)' },
    { k: 'candles_count', l: 'N свечей (старый impulse-метод)', t: 'number', step: 1, min: 1, max: 50, int: true,
      h: 'Используется только в режиме «Фиксированное N свечей»; в автоматическом swing-режиме игнорируется.' },
    { k: 'min_move_pct', l: 'Минимальное движение цены, %', t: 'number', step: 0.1, min: 0.01 },
    { k: 'fib_goal_pct', l: 'Цель: уровень коррекции по Фибо, %', t: 'number', step: 1, min: 1, h: '50 % — середина сетки' },
    { k: 'min_profit_pct', l: 'Минимальный профит, %', t: 'number', step: 0.05, min: 0.01 },
    { k: 'direction', l: 'Направление сделок', t: 'select', o: [['both', 'Оба направления'], ['long', 'Только лонг'], ['short', 'Только шорт']] },
    { k: 'allow_opposite_cycles', l: 'Одновременно вести встречный цикл', t: 'bool',
      h: 'На фьючерсах PAPER/бэктест: независимый цикл LONG и SHORT. Для LIVE требуется подтверждённый hedge-mode Gate.io.' },
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
    { k: 'demo_speed', l: 'Ускорение синтетики', t: 'number', step: 1, min: 1, max: 600, h: 'только для синтетических свечей: 1 = реальное время, 60 = минута за секунду' },
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
  const mode = $('set_entry_mode');
  const countField = $('set_candles_count')?.closest('.field');
  const syncEntryMode = () => {
    if (countField) countField.classList.toggle('hidden', mode.value !== 'impulse');
  };
  if (mode) mode.onchange = syncEntryMode;
  if (mode && countField) syncEntryMode();
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
/* Бэктест на реальной истории. Данные скачивает сервер: страницами по 1000 свечей
   прямо с Gate.io, поэтому пользователю не нужно ничего искать и загружать самому.
   Большие периоды считаются в фоне — ход виден в полосе загрузки. */
let btJobId = null;
let btTimer = null;

const BT_STRATEGY_FIELDS = [
  ['direction', 'Направление сделок', 'select',
    [['both', 'Оба направления'], ['long', 'Только лонг'], ['short', 'Только шорт']]],
  ['leverage', 'Кредитное плечо (фьючерсы)', 'int'],
  ['allow_opposite_cycles', 'Одновременно вести встречный цикл', 'bool'],
  ['entry_mode', 'Метод входа', 'select',
    [['swing', 'Автоматически по движению (без фиксированного N)'], ['impulse', 'Фиксированное N свечей (старый метод)']]],
  ['swing_lookback', 'Макс. окно поиска движения, свечей', 'number'],
  ['swing_fresh_candles', 'Макс. возраст экстремума, свечей', 'number'],
  ['min_correction_pct', 'Мин. коррекция для входа, %', 'number'],
  ['candles_count', 'N свечей (только старый impulse)', 'number'],
  ['min_move_pct', 'Мин. движение, %', 'number'],
  ['fib_goal_pct', 'Цель по Фибо, %', 'number'],
  ['min_profit_pct', 'Мин. профит, %', 'number'],
  ['base_volume_usd', 'Объём 1-й сделки, $', 'number'],
  ['volume_multiplier', 'Множитель объёма', 'number'],
  ['multiplier_start_position', 'Умножать с позиции', 'number'],
  ['max_positions', 'Максимум позиций', 'number'],
  ['total_profit_target_usd', 'Цель общего профита, $', 'number'],
  ['paper_balance_usd', 'Стартовый баланс, $', 'number'],
  ['fee_pct', 'Комиссия, %', 'number'],
  ['slippage_pct', 'Слиппедж, %', 'number'],
];

function renderBacktestForm(settings) {
  const st = settings || {};
  const defs = {
    data_mode: 'archive',
    symbol: st.symbol || 'BTC/USDT',
    market_type: st.market_type || 'futures',
    direction: st.direction || 'both',
    leverage: st.leverage || 1,
    allow_opposite_cycles: Boolean(st.allow_opposite_cycles),
    timeframe: st.timeframe || '5m',
    archive_period: '2',
    period_mode: 'limit',
    limit_candles: 3000,
    date_from: '',
    date_to: '',
  };
  const head = [
    ['data_mode', 'Откуда данные', 'select',
      [['archive', 'Архив Gate.io с 2023 г. — годы истории (рекомендую)'],
       ['api', 'API Gate.io — последние ~10 000 свечей (быстро)']]],
    ['symbol', 'Пара', 'text'],
    ['market_type', 'Рынок', 'select', [['futures', 'Фьючерсы USDT-M'], ['spot', 'Спот']]],
    ['timeframe', 'Таймфрейм', 'tf'],
    ['archive_period', 'Период архива', 'select',
      [['1', 'последний год'], ['2', 'последние 2 года'], ['3', 'последние 3 года'],
       ['all', 'весь архив (с 2023 г.)'], ['custom', 'свой период']]],
    ['period_mode', 'Период API', 'select', [['limit', 'Последние N свечей'], ['dates', 'С даты по дату (UTC)']]],
    ['limit_candles', 'Сколько свечей', 'number'],
    ['date_from', 'С даты', 'text'],
    ['date_to', 'По дату', 'text'],
  ];
  const html = (fields) => fields.map(([k, l, t, o]) => {
    const id = `bt_${k}`;
    const value = defs[k] !== undefined ? defs[k]
      : (st[k] !== undefined && st[k] !== null ? st[k] : '');
    const fieldClass = k === 'candles_count' ? 'field bt-legacy-candles' : 'field';
    if (t === 'bool') {
      return `<div class="${fieldClass}"><label>${l}</label>` +
        `<label class="switch"><input type="checkbox" id="${id}" ${value ? 'checked' : ''}> включено</label></div>`;
    }
    if (t === 'int') {
      return `<div class="${fieldClass}"><label>${l}</label>` +
        `<input type="number" step="1" min="1" max="100" id="${id}" value="${value}" autocomplete="off"></div>`;
    }
    if (t === 'select') {
      return `<div class="${fieldClass}"><label>${l}</label><select id="${id}">` +
        o.map(([v, txt]) => `<option value="${v}" ${value === v ? 'selected' : ''}>${txt}</option>`).join('') +
        '</select></div>';
    }
    if (t === 'tf') {
      const tfs = st.timeframes || ['1m', '5m', '15m', '1h', '4h', '1d'];
      return `<div class="${fieldClass}"><label>${l}</label><select id="${id}">` +
        tfs.map((v) => `<option value="${v}" ${value === v ? 'selected' : ''}>${v}</option>`).join('') +
        '</select></div>';
    }
    return `<div class="${fieldClass}"><label>${l}</label>` +
      `<input type="${t}" step="any" id="${id}" value="${value}" autocomplete="off"></div>`;
  }).join('');
  $('btForm').innerHTML = html(head) + html(BT_STRATEGY_FIELDS);
  const entryMode = $('bt_entry_mode');
  const legacyCandles = $('bt_candles_count')?.closest('.field');
  const syncBtEntryMode = () => {
    if (legacyCandles) legacyCandles.classList.toggle('hidden', entryMode.value !== 'impulse');
  };
  if (entryMode) entryMode.onchange = syncBtEntryMode;
  if (entryMode && legacyCandles) syncBtEntryMode();
  const mode = $('bt_period_mode');
  if (mode) mode.onchange = syncBtPeriod;
  const dataMode = $('bt_data_mode');
  if (dataMode) dataMode.onchange = syncBtMode;
  const archivePeriod = $('bt_archive_period');
  if (archivePeriod) archivePeriod.onchange = syncBtArchivePeriod;
  syncBtMode();
  loadBtFiles();
  loadArchiveCache();
}

function syncBtMode() {
  const archive = ($('bt_data_mode') || {}).value !== 'api';
  const period = $('bt_archive_period');
  if (period) period.closest('.field').classList.toggle('hidden', !archive);
  const apiPeriod = $('bt_period_mode');
  if (apiPeriod) apiPeriod.closest('.field').classList.toggle('hidden', archive);
  if (archive) {
    syncBtArchivePeriod();
  } else {
    ['bt_date_from', 'bt_date_to'].forEach((id) => {
      const el = $(id);
      if (el) el.closest('.field').classList.add('hidden');
    });
  }
  syncBtPeriod();
  const run = $('btnRunBacktest');
  if (run) run.textContent = archive ? 'Скачать архивы и запустить' : 'Скачать и запустить';
}

function syncBtArchivePeriod() {
  const archive = ($('bt_data_mode') || {}).value !== 'api';
  const custom = ($('bt_archive_period') || {}).value === 'custom';
  ['bt_date_from', 'bt_date_to'].forEach((id) => {
    const el = $(id);
    if (el) el.closest('.field').classList.toggle('hidden', !(archive && custom));
  });
  const hint = $('btArchiveHint');
  if (hint) {
    hint.textContent = archive
      ? 'месяцы уже в кеше повторно не качаются; текущий месяц обновляется'
      : '';
  }
}

function btArchiveDates(period) {
  const now = new Date();
  const to = now.toISOString().slice(0, 10);
  if (period === 'all') return { from: '2023-01-01', to };
  const from = new Date(now.getTime());
  from.setUTCFullYear(from.getUTCFullYear() - Number(period || 2));
  return { from: from.toISOString().slice(0, 10), to };
}

function syncBtPeriod() {
  const archive = ($('bt_data_mode') || {}).value !== 'api';
  const dates = ($('bt_period_mode') || {}).value === 'dates';
  const limitField = $('bt_limit_candles');
  if (limitField) limitField.closest('.field').classList.toggle('hidden', archive || dates);
  if (archive) return;                    // датами управляет syncBtArchivePeriod
  ['bt_date_from', 'bt_date_to'].forEach((id) => {
    const el = $(id);
    if (el) el.closest('.field').classList.toggle('hidden', !dates);
  });
}

function collectBacktestParams() {
  const params = {};
  document.querySelectorAll('[id^="bt_"]').forEach((el) => {
    const key = el.id.slice(3);
    if (key === 'data_mode' || key === 'archive_period' || key === 'period_mode') return;
    const v = el.value;
    if (el.type === 'checkbox') params[key] = el.checked;
    else if (v === '') return;
    else if (el.type === 'number') params[key] = el.step === '1' ? parseInt(v, 10) : parseFloat(v);
    else params[key] = v.trim();
  });
  const mode = ($('bt_data_mode') || {}).value || 'archive';
  if (mode === 'archive') {
    // Глубокая история: сервер сам скачает месячные архивы Gate.io.
    delete params.limit_candles;
    const period = ($('bt_archive_period') || {}).value || '2';
    if (period !== 'custom') {
      const range = btArchiveDates(period);
      params.date_from = range.from;
      params.date_to = range.to;
    }
    params.source = 'archive';
    return params;
  }
  if (($('bt_period_mode') || {}).value === 'limit') {
    delete params.date_from;
    delete params.date_to;
  } else {
    delete params.limit_candles;
  }
  params.source = 'gate';
  return params;
}

/* ---- список своих CSV-файлов на сервере ---- */
async function loadBtFiles() {
  const host = $('btFiles');
  if (!host) return;
  let data = null;
  try {
    data = await api('GET', '/api/backtest/files');
  } catch (e) {
    host.innerHTML = `<p class="muted">не удалось получить список файлов: ${escapeHtml(e.message)}</p>`;
    return;
  }
  const files = (data.files || []).filter((f) => f.exists);
  if (!files.length) {
    host.innerHTML = '<p class="muted">на сервере пока нет своих файлов — можно просто скачать с биржи (слева)</p>';
    return;
  }
  host.innerHTML = files.map((f) => `
    <div class="bt-file">
      <label class="bt-file-pick">
        <input type="radio" name="btfile" value="${f.name}" ${app.btFile === f.name ? 'checked' : ''}>
        <span><b>${f.name}</b>
          <span class="muted">${f.ok ? `${fmt(f.count, 0)} свечей · ${f.from_utc} → ${f.to_utc} UTC · ${f.size_kb} КБ`
            : `не читается: ${escapeHtml(f.error || '')}`}</span></span>
      </label>
      <span class="bt-file-actions">
        <a class="btn btn-small" href="${BASE}api/backtest/files/${encodeURIComponent(f.name)}">Скачать</a>
        <button class="btn btn-small" data-del="${f.name}">Удалить</button>
      </span>
    </div>`).join('');
  host.querySelectorAll('input[name=btfile]').forEach((el) => {
    el.onchange = () => { app.btFile = el.value; };
  });
  host.querySelectorAll('button[data-del]').forEach((el) => {
    el.onclick = async () => {
      if (!confirm(`Удалить файл ${el.dataset.del}?`)) return;
      await api('DELETE', '/api/backtest/files/' + encodeURIComponent(el.dataset.del)).catch(() => null);
      if (app.btFile === el.dataset.del) app.btFile = null;
      loadBtFiles();
    };
  });
}

async function uploadBtCsv() {
  const input = $('btFile');
  const file = input && input.files && input.files[0];
  if (!file) { toast('Выберите файл со свечами (CSV или ZIP)', 'err'); return; }
  const url = BASE + `api/backtest/files?name=${encodeURIComponent(file.name)}`;
  $('btStatus').textContent = `загружаю ${file.name}…`;
  try {
    // отправляем файл как есть (бинарно): сервер сам распознает CSV, ZIP или GZIP
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: await file.arrayBuffer(),
      credentials: 'same-origin',
    });
    const data = await res.json().catch(() => null);
    if (!res.ok || !data || !data.ok) {
      throw new Error((data && data.error) || ('HTTP ' + res.status));
    }
    app.btFile = data.file.name;
    toast(`Загружено: ${data.file.name} — ${data.candles} свечей`);
    $('btStatus').textContent = data.message || `загружено свечей: ${data.candles}`;
    loadBtFiles();
  } catch (e) {
    $('btStatus').textContent = '';
    toast('Файл не принят: ' + e.message, 'err');
  }
}

/* ---- запуск и ход задания ---- */
async function startBacktest(payload, label) {
  const status = $('btStatus');
  status.textContent = label || 'запускаю…';
  setBtProgress(0);
  $('btnCancelBacktest').disabled = false;
  ['btnRunBacktest', 'btnRunCsv', 'btnRunSynthetic'].forEach((id) => { $(id).disabled = true; });
  try {
    const res = await api('POST', '/api/backtest/job', payload);
    btJobId = res.job.id;
    pollBacktest();
  } catch (e) {
    status.textContent = '';
    toast('Бэктест: ' + e.message, 'err');
    finishBacktest();
  }
}

function pollBacktest() {
  clearTimeout(btTimer);
  btTimer = setTimeout(async () => {
    if (!btJobId) return;
    let job = null;
    try {
      job = await api('GET', `/api/backtest/job/${btJobId}`);
    } catch (e) {
      $('btStatus').textContent = 'нет связи с сервером: ' + e.message;
      finishBacktest();
      return;
    }
    const p = job.progress || {};
    const total = p.total ? ` из ${fmt(p.total, 0)}` : '';
    $('btStatus').textContent = `${p.message || 'выполняется…'}${p.candles ? ` (${fmt(p.candles, 0)}${total})` : ''}`;
    setBtProgress(p.percent || (job.state === 'done' ? 100 : 0));
    if (job.state === 'running') { pollBacktest(); return; }
    if (job.state === 'done' && job.result) {
      renderBacktestResult(job.result);
      $('btStatus').textContent = `${p.message || 'готово'} · id ${job.result_id}`;
      loadArchiveCache();
    } else if (job.state === 'cancelled') {
      $('btStatus').textContent = 'остановлено';
    } else {
      $('btStatus').textContent = '';
      toast('Бэктест не удался: ' + (job.error || 'неизвестная ошибка'), 'err');
    }
    finishBacktest();
  }, 700);
}

function finishBacktest() {
  btJobId = null;
  clearTimeout(btTimer);
  ['btnRunBacktest', 'btnRunCsv', 'btnRunSynthetic'].forEach((id) => { $(id).disabled = false; });
  $('btnCancelBacktest').disabled = true;
}

function setBtProgress(percent) {
  const bar = $('btProgressBar');
  if (bar) bar.style.width = Math.max(0, Math.min(100, percent || 0)) + '%';
}

function runBacktestFromExchange() {
  const params = collectBacktestParams();
  const label = params.source === 'archive'
    ? 'скачиваю архивы Gate.io (первый раз — месяцами, дальше из кеша)…'
    : 'скачиваю историю с Gate.io…';
  startBacktest(params, label);
}

async function loadArchiveCache() {
  const host = $('btArchiveCache');
  if (!host) return;
  let data = null;
  try {
    data = await api('GET', '/api/history/archive');
  } catch (e) {
    host.innerHTML = `<p class="muted">не удалось получить кеш архивов: ${escapeHtml(e.message)}</p>`;
    return;
  }
  const items = data.items || [];
  const hint = $('btArchiveHint');
  if (hint) {
    hint.textContent = items.length
      ? `архивы с ${data.first_month ? data.first_month.slice(0, 4) + '-' + data.first_month.slice(4, 6) : '2023-01'} · лимит ${fmt(data.max_candles, 0)} свечей`
      : 'кеш пуст — при первом запуске сервер скачает нужные месяцы';
  }
  if (!items.length) {
    host.innerHTML = '<p class="muted">пока ничего не скачано: выберите период и нажмите «Скачать архивы и запустить»</p>';
    return;
  }
  host.innerHTML = items.map((f) => `
    <div class="bt-file">
      <span><b>${escapeHtml(f.file)}</b>
        <span class="muted">${escapeHtml(f.symbol)} · ${escapeHtml(f.market_type)} · ${escapeHtml(f.timeframe)} ·
        ${fmt(f.count, 0)} свечей · ${f.from_utc || '—'} → ${f.to_utc || '—'} UTC · ${f.size_mb} МБ</span></span>
      <span class="bt-file-actions">
        <button class="btn btn-small" data-archive-del="${escapeHtml(f.key)}">Удалить</button>
      </span>
    </div>`).join('');
  host.querySelectorAll('button[data-archive-del]').forEach((el) => {
    el.onclick = async () => {
      if (!confirm(`Удалить скачанную историю ${el.dataset.archiveDel}?`)) return;
      await api('DELETE', '/api/history/archive/' + encodeURIComponent(el.dataset.archiveDel))
        .catch(() => null);
      loadArchiveCache();
    };
  });
}

function runBacktestFromCsv() {
  const params = collectBacktestParams();
  if (!app.btFile) { toast('Сначала загрузите или выберите CSV-файл', 'err'); return; }
  startBacktest({ ...params, source: 'csv', file: app.btFile }, `читаю файл ${app.btFile}…`);
}

function runBacktestSynthetic() {
  const params = collectBacktestParams();
  startBacktest({ ...params, source: 'synthetic', limit_candles: params.limit_candles || 3000 },
                'генерирую синтетические свечи…');
}

async function cancelBacktest() {
  if (!btJobId) return;
  await api('POST', `/api/backtest/job/${btJobId}/cancel`).catch(() => null);
  $('btStatus').textContent = 'останавливаю…';
}

function renderBacktestResult(r) {
  const cards = [
    ['Итоговый баланс', fmt(r.final_equity, 2) + ' $', `старт ${fmt(r.initial_balance, 2)} $`],
    ['Общий профит', money(r.total_pnl, 4), pct(r.return_pct) + ' к балансу'],
    ['Циклов', r.cycles, `прибыльных ${r.wins} · убыточных ${r.losses}`],
    ['Win rate', fmt(r.win_rate, 1) + ' %', `средний цикл ${money(r.avg_cycle_pnl, 4)}`],
    ['Максимум позиций', r.max_positions_used, `возможных ${(app.settings && app.settings.max_positions) || '—'}`],
    ['Макс. просадка', fmt(r.max_drawdown, 4) + ' $', `${pct(r.max_drawdown_pct)} · close-to-close equity`],
    ['Пиковая экспозиция', fmt(r.peak_notional_exposure, 2) + ' $', 'номинал открытых позиций'],
    ['Пик маржи / нагрузка', fmt(r.max_margin_used_estimate, 2) + ' $', `${pct(r.max_deposit_load_pct)} · плечо ${r.leverage || 1}x · ${r.margin_method || ''}`],
    ['Направления', r.direction === 'long' ? 'только LONG' : (r.direction === 'short' ? 'только SHORT' : 'LONG + SHORT'),
      r.allow_opposite_cycles ? 'встречные циклы разрешены' : 'один активный цикл'],
    ['Входы без маржи', fmt(r.margin_rejections || 0, 0), r.busted ? `СЛИВ · ${ts(r.bust_ts)}` : 'слива не было'],
    ['Лучший цикл', money(r.best_cycle, 4), `худший ${money(r.worst_cycle, 4)}`],
    ['Прибыль/убыток', r.profit_factor === null ? '∞' : fmt(r.profit_factor, 2), `комиссии ${fmt(r.fees_total, 4)} $`],
  ];
  $('btStats').innerHTML = cards.map(([l, v, h]) => `<div class="card"><div class="card-label">${l}</div>
    <div class="card-value">${v}</div><div class="card-hint">${h}</div></div>`).join('');

  const d = r.data || {};
  const warnBox = $('btHint');
  if (warnBox) {
    const warnings = [...(d.warnings || [])];
    if (r.busted) warnings.unshift(`СЛИВ: equity опустился до ${fmt(r.final_equity, 2)} $; тест остановлен ${ts(r.bust_ts)}.`);
    if (r.margin_rejections) warnings.push(`Отклонено входов из-за нехватки свободной маржи: ${r.margin_rejections}.`);
    warnBox.className = 'hint-banner ' + (warnings.length ? 'warn' : 'hidden');
    warnBox.innerHTML = warnings.map((w) => '⚠ ' + escapeHtml(w)).join('<br>');
  }
  const info = $('btDataInfo');
  if (info) {
    const sourceLabel = d.source === 'archive'
      ? `<b>Архив Gate.io${d.resampled_from ? ` (${d.resampled_from} → ${d.timeframe})` : ''}</b>`
      : (d.source === 'gate' ? '<b>Реальная история Gate.io</b>'
        : (d.source === 'csv' ? '<b>Ваш файл</b>' : 'Синтетические свечи'));
    info.innerHTML = d.count
      ? `${sourceLabel}: ${fmt(d.count, 0)} свечей, ${d.from_utc} → ${d.to_utc} UTC (${d.span_days} дн.)` +
        (d.months_in_period ? ` · месяцев в периоде: ${d.months_in_period}, скачано сейчас: ${d.months_downloaded}` : '') +
        (d.tail_candles ? ` · последний месяц ещё не в архиве — догружено ${fmt(d.tail_candles, 0)} свечей через API` : '') +
        (d.gaps ? `, пропусков в данных: ${d.gaps}` : '') +
        (d.file ? ` · кеш <code>${escapeHtml(d.file)}</code>` : '')
      : '';
  }
  const tbody = $('btCycles').querySelector('tbody');
  const list = (r.cycles_list || []).slice().reverse();
  tbody.innerHTML = list.length ? list.map((c, i) => `<tr>
    <td>${i + 1}</td><td>${escapeHtml(c.side || '')}</td><td>${ts(c.opened_ts)}</td><td>${ts(c.closed_ts)}</td>
    <td>${duration(c.duration_ms)}</td><td>${fmt(c.positions, 0)}</td><td>${fmt(c.avg_entry, 2)}</td>
    <td>${fmt(c.target, 2)}</td><td>${fmt(c.close_price, 2)}</td><td>${fmt(c.volume_usd, 2)}</td><td>${fmt(c.fees, 4)}</td>
    <td>${money(-Math.abs(Number(c.max_adverse_pnl_usd || 0)), 2)} / ${pct(-Math.abs(Number(c.max_adverse_pnl_pct || 0)), 2)}</td>
    <td>${fmt(c.peak_notional_usd, 2)}</td><td>${fmt(c.peak_margin_usd, 2)}</td>
    <td>${pct(c.max_deposit_load_pct, 2)}</td>
    <td class="${cls(c.pnl)}"><b>${money(c.pnl, 4)}</b></td><td class="${cls(c.pnl_pct)}">${pct(c.pnl_pct)}</td>
    <td class="muted">${escapeHtml(c.reason || '')}</td></tr>`).join('')
    : '<tr class="empty"><td colspan="18">циклов не было — попробуйте уменьшить мин. движение или взять больше свечей</td></tr>';

  const positionsBody = $('btPositions').querySelector('tbody');
  const positions = (r.positions_list || []).slice().reverse();
  positionsBody.innerHTML = positions.length ? positions.map((p) => `<tr>
    <td>${escapeHtml(p.cycle_id || '')}</td><td><b>${p.idx}</b></td><td>${escapeHtml(p.side || '')}</td>
    <td>${ts(p.ts)}</td><td>${ts(p.close_ts)}</td><td>${duration(p.duration_ms)}</td>
    <td>${fmt(p.price, 2)}</td><td>${fmt(p.close_price, 2)}</td><td>${fmt(p.volume_usd, 2)}</td>
    <td>${fmt(p.close_volume_usd, 2)}</td><td>${fmt(p.qty, 8)}</td>
    <td>${p.multiplier > 1 ? `x${p.multiplier}` : 'x1'}</td><td>${fmt(p.fees, 4)}</td>
    <td class="${cls(p.pnl)}">${money(p.pnl, 4)}</td><td class="${cls(p.pnl_pct)}">${pct(p.pnl_pct, 3)}</td></tr>`).join('')
    : '<tr class="empty"><td colspan="15">позиций в бэктесте нет</td></tr>';

  renderEquityCurve(r.equity_curve || []);
  renderDepositLoadCurve(r.equity_curve || []);
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

function renderDepositLoadCurve(curve) {
  const container = $('btLoad');
  if (!container || !window.LightweightCharts) return;
  const LWC = window.LightweightCharts;
  if (!app.btLoad) {
    app.btLoad = LWC.createChart(container, {
      layout: { background: { type: 'solid', color: '#0b1017' }, textColor: '#c6d2df', fontSize: 11 },
      grid: { vertLines: { color: '#131b25' }, horzLines: { color: '#131b25' } },
      rightPriceScale: { borderColor: '#243146' },
      timeScale: { borderColor: '#243146', timeVisible: true, secondsVisible: false },
    });
    app.btLoadSeries = app.btLoad.addLineSeries({ color: '#f0b90b', lineWidth: 2, priceLineVisible: false });
    new ResizeObserver(() => app.btLoad && app.btLoad.applyOptions({
      width: container.clientWidth, height: container.clientHeight })).observe(container);
  }
  const seen = new Set();
  const data = [];
  curve.forEach((point) => {
    const time = Math.floor(point.ts / 1000);
    if (seen.has(time) || point.deposit_load_pct === undefined) return;
    seen.add(time);
    data.push({ time, value: Number(point.deposit_load_pct) || 0 });
  });
  data.sort((a, b) => a.time - b.time);
  app.btLoadSeries.setData(data);
  app.btLoad.timeScale().fitContent();
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
  const box = $('connResult');
  const show = (kind, html) => {
    if (box) { box.className = 'hint-banner ' + kind; box.innerHTML = html; }
  };
  if (box) { box.className = 'hint-banner'; box.textContent = 'проверяю связь с биржей…'; }
  try {
    const res = await api('POST', '/api/settings/test-connection', collectSettings());
    if (res.synthetic) {
      show('warn', '<b>Выбран источник «синтетика»</b> — свечи выдуманные, к бирже бот не обращается. ' +
        'Для работы на реальных данных выберите в поле «Источник данных» вариант Gate.io и сохраните настройки.');
      return;
    }
    if (res.ok) {
      const publicPart = res.public || res;
      const bits = [];
      if (publicPart.symbol) bits.push(`пара <b>${publicPart.symbol}</b> (${publicPart.timeframe})`);
      if (publicPart.price) bits.push(`цена <b>${fmt(publicPart.price, publicPart.price > 100 ? 2 : 5)}</b>`);
      if (publicPart.candle_ts) bits.push(`последняя свеча ${ts(publicPart.candle_ts)}`);
      if (publicPart.latency_ms) bits.push(`ответ биржи ${publicPart.latency_ms} мс`);
      show('ok', '<b>Реальные данные Gate.io идут.</b> ' + bits.join(' · ') +
        (res.keys_present === false ? '<br>API-ключи не заданы — это нормально для виртуальных сделок ' +
          'и для бэктеста; ключи понадобятся только для реальной торговли (режим live).' : '') +
        (res.balance ? `<br>Баланс: ${JSON.stringify(res.balance)}` : ''));
      toast('Связь с биржей есть', 'ok');
    } else {
      show('err', '<b>Не получилось.</b> ' + escapeHtml(res.error || 'неизвестная ошибка') +
        '<br>Проверьте интернет на сервере: <code>curl -s -m 10 https://api.gateio.ws/api/v4/spot/time</code>');
      toast('Ошибка: ' + (res.error || 'неизвестно'), 'err');
    }
  } catch (e) {
    show('err', '<b>Ошибка запроса.</b> ' + escapeHtml(e.message));
    toast('Ошибка: ' + e.message, 'err');
  }
}

async function botAction(path, label) {
  try {
    const res = await api('POST', path);
    if (res && res.ok === false) toast(label + ': ' + (res.error || 'ошибка'), 'err');
    else toast(label + ': выполнено', 'ok');
  } catch (e) { toast(label + ': ' + e.message, 'err'); }
}

async function resetStatistics(scope) {
  const allTime = scope === 'all';
  const label = allTime ? 'Общую статистику' : 'Статистику за сутки';
  const message = allTime
    ? 'Сбросить общие счётчики профита и циклов? Открытый цикл не закроется, история сделок останется в журнале.'
    : 'Начать новый 24-часовой период статистики? История циклов и сделок не удаляется.';
  if (!confirm(message)) return;
  try {
    const path = allTime ? '/api/bot/reset-stats' : '/api/bot/reset-daily-stats';
    const result = await api('POST', path);
    if (!result || result.ok === false) throw new Error((result && result.error) || 'сброс не выполнен');
    app.snapshot = await api('GET', '/api/state');
    renderSnapshot(app.snapshot);
    toast(`${label} сброшена; история сохранена`);
  } catch (e) {
    toast(`${label}: ${e.message}`, 'err');
  }
}

/* --------------------------------------------------------------------- init */
function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('active', p.id === 'tab-' + name));
  app.chartTabActive = name === 'chart';
  if (app.chartTabActive) { setTimeout(() => { updateChart(true); }, 50); }
  if (name === 'history') loadHistory();
  if (name === 'positions') loadPositions();
  if (app.chartTabActive) refreshRecentCycles();
  if (app.chart) app.chart.applyOptions({ width: $('chart').clientWidth, height: $('chart').clientHeight });
  if (app.btEquity) app.btEquity.applyOptions({ width: $('btEquity').clientWidth });
  if (app.btChart) app.btChart.applyOptions({ width: $('btChart').clientWidth });
}

function bootDone() {
  const boot = $('boot');
  if (boot) boot.classList.add('hidden');
}

function bootFail(error) {
  const boot = $('boot');
  const box = $('bootError');
  if (!boot || !box) return;
  const spinner = boot.querySelector('.boot-spinner');
  if (spinner) spinner.style.display = 'none';
  const title = boot.querySelector('h2');
  if (title) title.textContent = 'Панель не смогла подключиться к боту';
  let extra = '';
  if (error && error.status === 401) {
    extra = 'Введите логин и пароль панели (переменные <code>PLANB_AUTH_USER</code> и ' +
      '<code>PLANB_AUTH_PASSWORD</code> в файле <code>.env</code> рядом с ботом). ' +
      'Если окно с паролем не появляется — откройте страницу в приватном окне браузера.';
  } else if (error && (error.network || error.status === undefined)) {
    extra = 'Браузер не получил ответ от бота. Проверьте на сервере:<br>' +
      '<code>systemctl status planb</code> и <code>ss -ltnp | grep 8015</code>, ' +
      'а также что порт открыт в файрволе и Security Group облака.<br>' +
      'Проверка с вашего компьютера: <code>curl -v http://СЕРВЕР:ПОРТ/</code> — ' +
      'если соединение зависает, порт закрыт снаружи.<br>' +
      'На сервере есть готовый диагностический скрипт: ' +
      '<code>bash /root/planb/deploy/check.sh</code>.';
  } else if (error && error.status === 404) {
    extra = 'Сервер ответил 404: проверьте настройку nginx (пример для префикса /planb/ — в README).';
  } else {
    extra = 'Ответ сервера: <code>' + escapeHtml(error ? error.message : 'неизвестная ошибка') + '</code>';
  }
  box.innerHTML = '<b>' + escapeHtml(error ? error.message : 'ошибка') + '</b><br>' + extra;
  box.classList.remove('hidden');
}

async function init() {
  document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => switchTab(t.dataset.tab)));
  $('btnStart').onclick = () => botAction('/api/bot/start', 'Старт');
  $('btnResetAllStats').onclick = () => resetStatistics('all');
  $('btnResetDailyStats').onclick = () => resetStatistics('daily');
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
  $('btnRefreshRecentCycles').onclick = refreshRecentCycles;
  $('btnExportCycles').onclick = exportCyclesCsv;
  $('btnRunBacktest').onclick = runBacktestFromExchange;
  $('btnRunCsv').onclick = runBacktestFromCsv;
  $('btnRunSynthetic').onclick = runBacktestSynthetic;
  $('btnCancelBacktest').onclick = cancelBacktest;
  $('btnUploadCsv').onclick = uploadBtCsv;
  $('btnResetCycle').onclick = () => {
    if (confirm('Закрыть незакрытый цикл только в учёте бота? Позиции на бирже не затрагиваются.\n' +
                'Используйте, если позиция уже закрыта вручную или отсутствует.'))
      botAction('/api/bot/reset-cycle', 'Сброс цикла');
  };
  $('btnFit').onclick = () => app.chart && app.chart.timeScale().fitContent();
  $('chkFib').onchange = () => updateChart(true);
  $('chkMarks').onchange = () => updateChart(true);
  $('btnClearLogs').onclick = () => { $('logs').innerHTML = ''; app.logs = []; };

  try {
    await loadSettings();
    app.snapshot = await api('GET', '/api/state');
    renderSnapshot(app.snapshot);
    bootDone();
  } catch (e) {
    bootFail(e);
    toast('Не удалось загрузить состояние: ' + e.message, 'err');
  }

  const logs = await api('GET', '/api/logs?limit=200').catch(() => ({ logs: [] }));
  (logs.logs || []).forEach(pushLog);
  connectWS();
  loadHistory();
  switchTab('chart');
  setInterval(() => { if (app.chartTabActive) refreshRecentCycles(); }, 30000);
  // страховка: если WebSocket недоступен (прокси/авторизация) — опрашиваем API
  let failures = 0;
  setInterval(async () => {
    if (app.ws && app.ws.readyState === 1) return;
    try {
      app.snapshot = await api('GET', '/api/state');
      renderSnapshot(app.snapshot);
      failures = 0;
    } catch (e) {
      failures += 1;
      if (failures === 3) bootFail(e);      // сервер пропал — покажем причину
    }
  }, 3000);
}

document.addEventListener('DOMContentLoaded', init);
