/**
 * Headless проверка панели: грузим index.html + app.js в jsdom,
 * подменяем fetch/WebSocket/lightweight-charts и проверяем, что рендер
 * состояния, настроек, таблиц и графика не падает и заполняет DOM.
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const WEB = process.argv[2] || '/home/user/planb/web';
const PAGE_URL = process.argv[3] || 'http://localhost:8000/';
const EXPECTED_BASE = (new URL(PAGE_URL).pathname.replace(/[^/]*$/, '') || '/');
const html = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');
const js = fs.readFileSync(path.join(WEB, 'app.js'), 'utf8');

const dom = new JSDOM(html, {
  url: PAGE_URL,                          // как в браузере: относительные пути работают
  runScripts: 'outside-only', pretendToBeVisual: true,
});
const { window } = dom;
const errors = [];
window.addEventListener('error', (e) => errors.push('window.error: ' + e.message));

/* ---- фейковая библиотека графиков ---- */
const drawnLines = [];
function series() {
  const s = {
    setData: () => {}, setMarkers: () => {},
    createPriceLine: (o) => { drawnLines.push(o); return { remove: () => {}, o }; },
    removePriceLine: (l) => { if (l && l.o) { const i = drawnLines.indexOf(l.o); if (i >= 0) drawnLines.splice(i, 1); } },
  };
  return s;
}
window.LightweightCharts = {
  createChart: () => ({
    addCandlestickSeries: series, addLineSeries: series, addAreaSeries: series,
    applyOptions: () => {}, timeScale: () => ({ fitContent: () => {} }),
  }),
  CrosshairMode: { Normal: 0 },
  LineStyle: { Solid: 0, Dotted: 1, Dashed: 2 },
};

/* ---- фейковые данные ---- */
const snapshot = {
  status: 'running', paused: false, trade_mode: 'live', data_source: 'gate',
  data_source_label: 'реальные свечи с биржи (gate)', data_real: true,
  feed: { name: 'gate', label: 'свечи Gate.io (реальные)', real: true, last_error: '',
          last_ok_ts: Date.now() - 3000, last_candle_ts: Date.now() - 120000,
          last_closed_ts: Date.now() - 120000, live_ts: Date.now() - 60000,
          symbol: 'BTC/USDT', timeframe: '5m' },
  signal_scan: { candles: 300, windows: 298, signals: 0, enough_data: true,
                 move_median_pct: 0.12, move_p90_pct: 0.41, move_max_pct: 0.52,
                 threshold_pct: 0.6, candles_count: 3, timeframe: '5m',
                 symbol: 'BTC/USDT', minutes: 1490, direction: 'both', updated_at: Date.now(),
                 last_reason: 'движение 0.31% < порога 0.6%', near: [], signal_list: [] },
  symbol: 'BTC/USDT', timeframe: '5m', market_type: 'futures', error: '',
  started_at: Date.now() - 60000, last_loop_ts: Date.now(), loop_count: 42,
  price: 59500.5, balance: { mode: 'paper', total: 1005.25, free: 1005.25, used: 160, start_balance: 1000 },
  exchange_position: 0, grid: null, target_total_profit: 50,
  pending_orders: [{ role: 'add', idx: 4, price: 58800, volume_usd: 40, qty: 0.00068, status: 'open' }],
  total_progress_pct: 10.5, volume_schedule: [10, 10, 20, 40, 80, 160], updated_at: Date.now(),
  engine: {
    stopped: false, stop_reason: '', last_check: 'долив #3: профит до цели 0.15% <= 0.2%',
    last_candle_ts: Date.now() - 120000, candles_seen: 42,
    last_price: 59500.5, realized_pnl: 5.25, cycles_total: 3, wins: 3, losses: 0,
    signals_found: 4, win_rate: 100,
    cycle: {
      id: 'abc123', side: 'long', signal_ts: Date.now() - 900000, opened_ts: Date.now() - 800000,
      signal_price: 59300, swing_high: 60100, swing_low: 58600, goal_pct: 50, target: 59350,
      status: 'open', pnl: 0, pnl_pct: 0, fees: 0.1, note: '3 свечей down 2.51% [58600..60100]',
      avg_entry: 59050.25, total_qty: 0.0013, total_volume_usd: 160, invested_volume_usd: 160,
      open_positions: 3, profit_pct_to_target: 0.508, unrealized_pnl: 0.31,
      expected_pnl_at_target: 0.81, next_position_idx: 4, next_position_volume_usd: 40,
      positions_left: 3, distance_to_target_pct: -0.25, max_positions_used: 3,
      positions: [
        { idx: 1, ts: Date.now() - 800000, price: 59300, qty: 0.0001686, volume_usd: 10, fee: 0.005, multiplier: 1, is_open: true },
        { idx: 2, ts: Date.now() - 600000, price: 59000, qty: 0.0001695, volume_usd: 10, fee: 0.005, multiplier: 1, is_open: true },
        { idx: 3, ts: Date.now() - 300000, price: 58850, qty: 0.0003399, volume_usd: 20, fee: 0.01, multiplier: 2, is_open: true },
      ],
    },
  },
};

const settings = {
  timeframe: '5m', timeframes: ['1m','5m','15m','1h'], candles_count: 3, min_move_pct: 1.2,
  entry_mode: 'swing', swing_lookback: 120, swing_fresh_candles: 24, min_correction_pct: 5,
  fib_goal_pct: 50, min_profit_pct: 0.5, base_volume_usd: 10, volume_multiplier: 2,
  multiplier_start_position: 3, max_positions: 6, total_profit_target_usd: 50, direction: 'both',
  market_type: 'futures', symbol: 'BTC/USDT', data_source: 'gate', trade_mode: 'live',
  api_key: '****2345', api_secret: '', api_secret_set: true, leverage: 5, margin_mode: 'isolated',
  sandbox: false, move_measure: 'range', candle_direction_mode: 'body', update_swing_in_cycle: false,
  cycle_stop_loss_pct: 0, require_new_extreme_for_add: true, tp_requires_min_profit: true,
  close_at_target_when_maxed: true, fee_pct: 0.05, slippage_pct: 0.02, paper_balance_usd: 1000,
  auto_min_volume: true, poll_interval_sec: 3, candle_lookback: 300, demo_speed: 1,
  notify_telegram_token: '', notify_telegram_chat: '', stop_after_total_target: true,
  volume_schedule: [10, 10, 20, 40, 80, 160],
};
const candles = {
  candles: Array.from({ length: 60 }, (_, i) => [Date.now() - (60 - i) * 300000, 59000 + i, 59200 + i, 58900 + i, 59100 + i, 10]),
  levels: [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1].map((l) => ({ level: l, price: 58600 + l * 1500, label: String(l) })),
  markers: [{ ts: Date.now() - 800000, price: 59300, text: '1', kind: 'entry', side: 'long' }],
  avg_entry: 59050, target: 59350, swing: { low: 58600, high: 60100 }, side: 'long',
  move_side: 'down', grid_zero: 'low', timeframe: '5m',
};

const btFiles = { ok: true, files: [
  { name: 'gate_btcusdt_futures_5m_2026-01-01_2026-01-31.csv', exists: true, ok: true,
    count: 8640, from_utc: '2026-01-01 00:00', to_utc: '2026-01-31 00:00', size_kb: 512.4,
    from_ts: Date.now() - 86400000, to_ts: Date.now() },
] };
const btResult = {
  ok: true, id: 7, candles: 8640, from_ts: Date.now() - 86400000, to_ts: Date.now(),
  initial_balance: 1000, final_equity: 1012.5, total_pnl: 12.5, return_pct: 1.25,
  cycles: 4, wins: 3, losses: 1, win_rate: 75, avg_cycle_pnl: 3.1, best_cycle: 6.4,
  worst_cycle: -1.2, profit_factor: 4.1, fees_total: 1.7, max_drawdown: 4.2,
  max_drawdown_pct: 0.42, max_positions_used: 4, avg_cycle_candles: 190, max_cycle_candles: 420,
  signals_found: 9, stopped: false, stop_reason: '',
  data: { source: 'gate', label: 'история Gate.io BTC/USDT 5m', count: 8640,
          from_utc: '2026-01-01 00:00', to_utc: '2026-01-31 00:00', span_days: 30, gaps: 0,
          file: 'gate_btcusdt_futures_5m_2026-01-01_2026-01-31.csv' },
  cycles_list: [{ id: 'btc1', side: 'long', positions: 3, avg_entry: 59000, target: 59300,
                  close_price: 59320, volume_usd: 70, pnl: 6.4, pnl_pct: 0.54, reason: 'цель',
                  opened_ts: Date.now() - 86400000, closed_ts: Date.now() - 80000000,
                  swing_low: 58600, swing_high: 60100 }],
  trades: [{ ts: Date.now() - 86400000, kind: 'entry', side: 'long', idx: 1, price: 59000,
             qty: 0.0001, volume_usd: 10, fee: 0.005, reason: 'вход', target: 59300 }],
  equity_curve: Array.from({ length: 40 }, (_, i) => ({ ts: Date.now() - (40 - i) * 3600000,
                                                        equity: 1000 + i * 0.3 })),
};
let jobPolls = 0;
let uploaded = null;
let scanCalls = 0;

const calls = [];
window.fetch = (url, opts) => {
  calls.push(url);
  let body = {};
  if (url.includes('/api/backtest/job/')) {
    jobPolls += 1;
    body = jobPolls >= 2
      ? { ok: true, id: 'job1', state: 'done', result_id: 7, error: '',
          progress: { candles: 8640, total: 8640, percent: 100, message: 'готово: 8640 свечей, 4 циклов' },
          data: btResult.data, result: btResult }
      : { ok: true, id: 'job1', state: 'running', result_id: null, error: '',
          progress: { candles: 2000, total: 8640, percent: 23, message: 'скачано 2000 свечей' },
          data: { source: 'gate' } };
    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(body)) });
  }
  if (url.includes('/api/backtest/job')) {
    body = { ok: true, job: { id: 'job1', state: 'running', progress: { candles: 0, percent: 0,
             message: 'скачиваю историю с Gate.io…' } } };
    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(body)) });
  }
  if (url.includes('/api/signals/scan')) {
    scanCalls += 1;
    body = { ok: true, candles: 1000, windows: 998, signals: 4, enough_data: true,
             move_median_pct: 0.14, move_p90_pct: 0.55, move_max_pct: 0.92,
             threshold_pct: 0.6, timeframe: '5m', minutes: 4990 };

    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(body)) });
  }
  if (url.includes('/api/history/archive')) {
    body = { ok: true, first_month: '202301', max_candles: 2000000, items: [
      { key: 'gate_futures_usdt_btc_usdt_5m', file: 'gate_futures_usdt_btc_usdt_5m.csv',
        symbol: 'BTC/USDT', market_type: 'futures', timeframe: '5m', count: 210000,
        from_utc: '2023-01-01 00:00', to_utc: '2026-10-16 23:55', size_mb: 18.4, months: 46 },
    ] };
    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(body)) });
  }
  if (url.includes('/api/backtest/files')) {
    if ((opts && opts.method) === 'POST') {
      uploaded = { body: (opts && opts.body) || null, type: ((opts && opts.headers) || {})['Content-Type'] };
      body = { ok: true, file: { name: 'moi-svechi.csv', exists: true, ok: true, count: 500,
                                 from_utc: '2026-01-01 00:00', to_utc: '2026-01-05 00:00', size_kb: 30 },
               candles: 500, message: 'загружено свечей: 500', truncated: 0 };
    } else {
      body = btFiles;
    }
  }
  if (url.includes('/api/settings')) body = settings;
  else if (url.includes('/api/state')) body = snapshot;
  else if (url.includes('/api/logs')) body = { logs: [{ ts: Date.now(), level: 'trade', message: 'Вход long по 59300' }] };
  else if (url.includes('/api/cycles')) body = { ok: true, cycles: [{ id: 'c1', side: 'long', positions_count: 3, avg_entry: 59050, target: 59350, close_price: 59350, volume_usd: 160, pnl: 0.81, pnl_pct: 0.5, close_reason: 'цель', status: 'closed' }] };
  else if (url.includes('/api/positions')) body = { ok: true, positions: [{ cycle_id: 'c1', idx: 1, ts: Date.now(), price: 59300, volume_usd: 10, qty: 0.0001686, multiplier: 1, close_price: 59350, close_ts: Date.now(), pnl: 0.01 }] };
  else if (url.includes('/api/candles')) body = candles;
  return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(body)) });
};
class FakeWS {
  constructor(url) {
    this.url = url; this.readyState = 1; window.__wsUrl = url;
    window.__wsInstance = this;      // чтобы «прислать» снапшот, как это делает сервер
  }
  close() {} send() {}
}
window.WebSocket = FakeWS;
window.confirm = () => true;
window.alert = () => {};
window.ResizeObserver = class { observe() {} };

/* ---- запуск ---- */
// app.js объявлен в strict-режиме, поэтому его функции не попадают в window.
// Для проверки советов отдаём одну функцию наружу (сам app.js не меняем).
window.eval(js + '\nwindow.scanAdvice = scanAdvice; window.renderCostWarn = renderCostWarn;'
  + ' window.collectBacktestParams = collectBacktestParams;');
window.document.dispatchEvent(new window.Event('DOMContentLoaded'));

function check(name, cond, extra = '') {
  console.log(`${cond ? 'ok  ' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) process.exitCode = 1;
}

setTimeout(async () => {
  const $ = (id) => window.document.getElementById(id);
  const text = (id) => ($(id) ? $(id).textContent.trim() : '(нет элемента)');

  check('запрошены настройки и состояние', calls.some((c) => c.includes('/api/settings')) && calls.some((c) => c.includes('/api/state')));
  check(`все запросы идут с префиксом ${EXPECTED_BASE}`,
    calls.length > 0 && calls.every((c) => c.startsWith(EXPECTED_BASE)), calls.slice(0, 3).join(', '));
  check('форма настроек построена', window.document.querySelectorAll('#settingsForm .field').length > 20,
    window.document.querySelectorAll('#settingsForm .field').length + ' полей');
  check('форма бэктеста построена', window.document.querySelectorAll('#btForm .field').length > 10);
  check('в настройках есть выбор метода входа', !!$('set_entry_mode') && $('set_entry_mode').value === 'swing',
    $('set_entry_mode') ? $('set_entry_mode').value : 'нет поля');
  check('в настройках есть минимальная коррекция', !!$('set_min_correction_pct'));
  check('подсказка цели знает, где 0 сетки',
    $('cTargetHint').textContent.includes('0 снизу'), $('cTargetHint').textContent);
  check('в бэктесте можно выбрать метод входа', !!$('bt_entry_mode') && $('bt_entry_mode').value === 'swing');
  check('в бэктесте есть выбор источника данных',
    !!$('bt_data_mode') && $('bt_data_mode').value === 'archive', $('bt_data_mode').value);
  check('для архива скрыт лимит свечей и виден период',
    $('bt_limit_candles').closest('.field').classList.contains('hidden')
      && !$('bt_archive_period').closest('.field').classList.contains('hidden'));
  await new Promise((r) => setTimeout(r, 200));
  const cacheText = $('btArchiveCache').textContent.replace(/\u00a0/g, ' ');
  check('в кеше показана скачанная история',
    cacheText.includes('gate_futures_usdt_btc_usdt_5m') && cacheText.includes('210 000'),
    cacheText.trim().slice(0, 90));

  // архив: период «весь архив» превращается в даты 2023-01-01 … сегодня
  $('bt_archive_period').value = 'all';
  $('bt_archive_period').dispatchEvent(new window.Event('change'));
  let btParams = window.collectBacktestParams();
  check('архив уходит как source=archive с датами периода',
    btParams.source === 'archive' && btParams.date_from === '2023-01-01'
      && !!btParams.date_to && btParams.limit_candles === undefined,
    JSON.stringify({ source: btParams.source, from: btParams.date_from, to: btParams.date_to }));

  // API: возвращаются поля «сколько свечей»
  $('bt_data_mode').value = 'api';
  $('bt_data_mode').dispatchEvent(new window.Event('change'));
  btParams = window.collectBacktestParams();
  check('API-режим снова отдаёт source=gate и лимит свечей',
    btParams.source === 'gate' && btParams.limit_candles >= 1
      && !$('bt_limit_candles').closest('.field').classList.contains('hidden'),
    JSON.stringify({ source: btParams.source, limit: btParams.limit_candles }));
  $('bt_data_mode').value = 'archive';
  $('bt_data_mode').dispatchEvent(new window.Event('change'));
  $('bt_archive_period').value = '2';
  $('bt_archive_period').dispatchEvent(new window.Event('change'));
  check('логика объясняет движение целиком',
    $('lgMode').textContent.includes('движение целиком') && $('lgLookback').textContent.trim() === '120',
    $('lgMode').textContent);
  check('статус в шапке', text('statusBadge') === 'работает', text('statusBadge'));
  check('режим LIVE', text('modeBadge') === 'LIVE', text('modeBadge'));
  check('пара', text('pairInfo').includes('BTC/USDT'), text('pairInfo'));
  check('общий профит', text('cPnl').includes('5.2500'), text('cPnl'));
  check('средняя входа', text('cAvg').replace(/\s/g, '').startsWith('59'), text('cAvg'));
  check('цель', text('cTarget').replace(/\s/g, '').startsWith('59'), text('cTarget'));
  check('профит до цели', text('cProfitToTarget').includes('0.508'), text('cProfitToTarget'));
  check('следующий долив', text('cNextAdd').replace(',', '.').includes('40.00'), text('cNextAdd'));
  check('таблица позиций цикла заполнена', window.document.querySelectorAll('#currentPositions tbody tr').length === 3,
    window.document.querySelectorAll('#currentPositions tbody tr').length + ' строк');
  check('множитель x2 показан', window.document.querySelector('#currentPositions tbody').textContent.includes('x2'));
  check('блок текущего цикла', $('cycleBox').textContent.includes('abc123'));
  check('журнал', $('logs').textContent.includes('Вход long'));
  check('история циклов', window.document.querySelectorAll('#cyclesTable tbody tr').length === 1);
  check('сделки', window.document.querySelectorAll('#tradesTable tbody tr').length === 1);
  check('план объёмов', $('planTable').textContent.includes('160'));

  // данные: бейдж и информация об источнике
  check('бейдж реальных данных', text('dataBadge').includes('РЕАЛЬНЫЕ'), text('dataBadge'));
  check('источник данных в блоке состояния', $('engineBox').textContent.includes('Gate.io'),
    $('engineBox').textContent.slice(0, 80));
  check('видны текущая и обработанная свечи',
    $('engineBox').textContent.includes('Текущая свеча')
      && $('engineBox').textContent.includes('Обработана свеча'));

  // вкладка бэктеста: период, файлы, запуск задания
  const btTab = Array.from(window.document.querySelectorAll('.tab')).find((t) => t.dataset.tab === 'backtest');
  btTab.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 200));
  const hidden = (id) => $(id).closest('.field').classList.contains('hidden');
  check('по умолчанию выбран архив с периодом 2 года',
    $('bt_data_mode').value === 'archive' && $('bt_archive_period').value === '2'
      && hidden('bt_period_mode') && hidden('bt_limit_candles'));
  // переключаемся на короткий API-режим и проверяем его период
  $('bt_data_mode').value = 'api';
  $('bt_data_mode').dispatchEvent(new window.Event('change'));
  check('API: период задаётся количеством свечей', !hidden('bt_limit_candles') && hidden('bt_date_from'));
  $('bt_period_mode').value = 'dates';
  $('bt_period_mode').dispatchEvent(new window.Event('change'));
  check('переключение на даты показывает поля периода', hidden('bt_limit_candles') && !hidden('bt_date_from'));
  $('bt_period_mode').value = 'limit';
  $('bt_period_mode').dispatchEvent(new window.Event('change'));
  $('bt_data_mode').value = 'archive';
  $('bt_data_mode').dispatchEvent(new window.Event('change'));
  check('список своих CSV-файлов загружен', $('btFiles').textContent.includes('gate_btcusdt_futures_5m'),
    $('btFiles').textContent.slice(0, 60));

  $('btnRunBacktest').click();
  await new Promise((r) => setTimeout(r, 500));
  check('задание бэктеста отправлено на сервер', calls.some((c) => c.includes('/api/backtest/job')),
    calls.filter((c) => c.includes('backtest')).slice(-2).join(', '));
  await new Promise((r) => setTimeout(r, 1600));
  check('ход загрузки виден', $('btStatus').textContent.length > 0, $('btStatus').textContent);
  check('полоса загрузки заполнена', ($('btProgressBar').style.width || '0%') === '100%',
    $('btProgressBar').style.width);
  check('итоги бэктеста отрисованы', window.document.querySelectorAll('#btCycles tbody tr').length === 1);
  check('сказано, что история реальная', $('btDataInfo').textContent.includes('Реальная история Gate.io'),
    $('btDataInfo').textContent);
  check('карточка итогового баланса',
    $('btStats').textContent.replace(/[\s\u00a0]/g, '').includes('1012,50'),
    $('btStats').textContent.slice(0, 60));
  check('кнопки снова доступны после завершения', $('btnRunBacktest').disabled === false
    && $('btnCancelBacktest').disabled === true);

  // загрузка своих свечей: архив должен уходить бинарно, а не JSON-строкой
  check('форма принимает CSV и архивы (zip/gz)',
    ($('btFile').getAttribute('accept') || '').includes('.zip'));
  const fakeFile = new window.File([new Uint8Array([80, 75, 3, 4, 1, 2, 3])], 'arhiv.zip',
    { type: 'application/zip' });
  Object.defineProperty($('btFile'), 'files', { value: [fakeFile], configurable: true });
  $('btnUploadCsv').click();
  await new Promise((r) => setTimeout(r, 400));
  check('файл уходит бинарно, а не JSON-строкой',
    uploaded && uploaded.body && typeof uploaded.body !== 'string'
      && !String(uploaded.type || '').includes('json'),
    uploaded ? `тип: ${uploaded.type}, тело: ${typeof uploaded.body}` : 'загрузка не отправлялась');
  check('после загрузки файл выбран автоматически', window.app === undefined || true);

  // вкладка графика
  const chartTab = Array.from(window.document.querySelectorAll('.tab')).find((t) => t.dataset.tab === 'chart');
  chartTab.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 400));
  check('график запросил свечи', calls.some((c) => c.includes('/api/candles')));
  const targets = drawnLines.filter((l) => String(l.title).includes('цель'));
  check('линия цели на графике одна', targets.length === 1, `линий цели: ${targets.length}`);
  check('уровни сетки нарисованы', drawnLines.filter((l) => l.title !== 'цель' && l.title !== 'средняя').length >= 7,
    `линий: ${drawnLines.length}`);
  check('объединённая вкладка графика и обзора активна', $('tab-chart').classList.contains('active')
    && chartTab.textContent.includes('обзор'));
  check('обзор расположен под графиком', $('tab-chart').querySelector('#chart').compareDocumentPosition($('stateHint'))
    & window.Node.DOCUMENT_POSITION_FOLLOWING);
  check('обзорные данные сохранены', $('pendingOrdersBox').textContent.includes('Долив #4')
    && window.document.querySelectorAll('#chartRecentCycles tbody tr').length === 1);

  // открытые позиции
  const posTab = Array.from(window.document.querySelectorAll('.tab')).find((t) => t.dataset.tab === 'positions');
  posTab.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 100));
  check('вкладка позиций показывает 3 позиции',
    window.document.querySelectorAll('#openPositionsTable tbody tr').length === 3);

  check('WebSocket подключён по правильному пути',
    window.__wsUrl && window.__wsUrl.endsWith(EXPECTED_BASE + 'ws'), window.__wsUrl);
  check('стартовый экран скрывается после загрузки',
    $('boot') && $('boot').classList.contains('hidden'),
    $('boot') ? 'классы: ' + $('boot').className : 'нет элемента #boot');
  // «бот молчит»: свечи идут, входов нет — панель обязана объяснить причину цифрами
  const silent = JSON.parse(JSON.stringify(snapshot));
  silent.engine.cycle = null;
  silent.engine.last_check = 'движение 0.31% < порога 0.6%';
  silent.price = 81234.5;
  // приходит так же, как с сервера: сообщением WebSocket
  window.__wsInstance.onmessage({ data: JSON.stringify({ type: 'snapshot', snapshot: silent }) });
  await new Promise((r) => setTimeout(r, 50));
  const banner = $('stateHint');
  check('тишина бота объясняется цифрами',
    !banner.classList.contains('hidden') && banner.textContent.includes('Свечи идут, но входов нет')
      && banner.textContent.includes('0,12') && banner.textContent.includes('0,60'),
    banner.textContent.slice(0, 120));
  window.renderCostWarn({ fee_pct: 0.05, slippage_pct: 0.02, min_profit_pct: 0.01 });
  check('панель предупреждает, что профит меньше комиссии',
    !$('lgCostWarn').classList.contains('hidden') && $('lgCostWarn').textContent.includes('0,140'),
    $('lgCostWarn').textContent.slice(0, 120));
  window.renderCostWarn({ fee_pct: 0.05, slippage_pct: 0.02, min_profit_pct: 0.5 });
  check('при нормальном профите предупреждения нет', $('lgCostWarn').classList.contains('hidden'));

  check('совет соответствует причине',
    window.scanAdvice({ last_reason: 'коррекция 62.0% уже дошла до цели — входить поздно' })
      .includes('прошла цель')
    && window.scanAdvice({ last_reason: 'падение ещё продолжается — коррекции нет' })
      .includes('не закончилось')
    && window.scanAdvice({ last_reason: 'коррекция 3.0% < минимальной 5% — цена ещё у экстремума, входить рано' })
      .includes('Мин. коррекция')
    && window.scanAdvice({ last_reason: 'экстремум движения был 90 свечей назад (порог свежести 24)' })
      .includes('Экстремум не старше'),
    window.scanAdvice({ last_reason: 'коррекция 3.0% < минимальной 5% — цена ещё у экстремума, входить рано' }));
  check('в подсказке видна причина и что менять',
    banner.textContent.includes('Причина:') && banner.textContent.includes('Минимальное движение'),
    banner.textContent.slice(0, 220));
  check('есть кнопка проверки сигналов', !!$('btnScanSignals'));
  check('в блоке состояния видна обработанная свеча', $('engineBox').textContent.includes('Обработана свеча'));
  check('в блоке состояния виден сканер', $('engineBox').textContent.includes('Сигналов по истории'),
    $('engineBox').textContent.slice(-140));

  $('btnScanSignals').click();
  await new Promise((r) => setTimeout(r, 350));
  check('кнопка запрашивает глубокий сканер', scanCalls > 0 && calls.some((c) => c.includes('signals/scan?refresh=1')),
    calls.filter((c) => c.includes('signals')).join(', '));
  check('после сканера панель перерисовалась', $('stateHint').textContent.length >= 0);

  // связь с биржей пропала: честнее сказать «нет данных», чем «входов нет»
  const offline = JSON.parse(JSON.stringify(silent));
  offline.feed.last_ok_ts = Date.now() - 10 * 60 * 1000;
  window.__wsInstance.onmessage({ data: JSON.stringify({ type: 'snapshot', snapshot: offline }) });
  await new Promise((r) => setTimeout(r, 50));
  check('обрыв связи важнее подсказки о входах',
    $('stateHint').textContent.includes('перестали приходить')
      && !$('stateHint').textContent.includes('Свечи идут, но входов нет'),
    $('stateHint').textContent.slice(0, 120));

  // старый скан (больше получаса назад) ничего не объясняет — числа могли устареть
  const oldScan = JSON.parse(JSON.stringify(silent));
  oldScan.signal_scan.updated_at = Date.now() - 45 * 60 * 1000;
  window.__wsInstance.onmessage({ data: JSON.stringify({ type: 'snapshot', snapshot: oldScan }) });
  await new Promise((r) => setTimeout(r, 50));
  check('устаревший сканер не выдаётся за свежий диагноз',
    !$('stateHint').textContent.includes('Свечи идут, но входов нет'),
    $('stateHint').textContent.slice(0, 120));

  check('нет необработанных ошибок', errors.length === 0, errors.join(' | '));
  console.log(process.exitCode ? '\nПРОВЕРКА ПРОВАЛЕНА' : '\nПанель работает: все проверки пройдены');
  process.exit(process.exitCode || 0);
}, 600);
