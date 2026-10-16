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
function series() {
  const s = {
    setData: () => {}, setMarkers: () => {},
    createPriceLine: (o) => ({ remove: () => {} , o}),
    removePriceLine: () => {},
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
          symbol: 'BTC/USDT', timeframe: '5m' },
  symbol: 'BTC/USDT', timeframe: '5m', market_type: 'futures', error: '',
  started_at: Date.now() - 60000, last_loop_ts: Date.now(), loop_count: 42,
  price: 59500.5, balance: { mode: 'paper', total: 1005.25, free: 1005.25, used: 160, start_balance: 1000 },
  exchange_position: 0, grid: null, target_total_profit: 50,
  total_progress_pct: 10.5, volume_schedule: [10, 10, 20, 40, 80, 160], updated_at: Date.now(),
  engine: {
    stopped: false, stop_reason: '', last_check: 'долив #3: профит до цели 0.15% <= 0.2%',
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
  levels: [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1].map((l) => ({ level: l, price: 58600 + l * 1500, label: String(l) }))
    .concat([{ level: 0.5, price: 59350, label: 'цель', highlight: true }]),
  markers: [{ ts: Date.now() - 800000, price: 59300, text: '1', kind: 'entry', side: 'long' }],
  avg_entry: 59050, target: 59350, swing: { low: 58600, high: 60100 }, side: 'long', timeframe: '5m',
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
  if (url.includes('/api/backtest/files')) body = btFiles;
  if (url.includes('/api/settings')) body = settings;
  else if (url.includes('/api/state')) body = snapshot;
  else if (url.includes('/api/logs')) body = { logs: [{ ts: Date.now(), level: 'trade', message: 'Вход long по 59300' }] };
  else if (url.includes('/api/cycles')) body = { ok: true, cycles: [{ id: 'c1', side: 'long', positions_count: 3, avg_entry: 59050, target: 59350, close_price: 59350, volume_usd: 160, pnl: 0.81, pnl_pct: 0.5, close_reason: 'цель', status: 'closed' }] };
  else if (url.includes('/api/positions')) body = { ok: true, positions: [{ cycle_id: 'c1', idx: 1, ts: Date.now(), price: 59300, volume_usd: 10, qty: 0.0001686, multiplier: 1, close_price: 59350, close_ts: Date.now(), pnl: 0.01 }] };
  else if (url.includes('/api/candles')) body = candles;
  return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(body)) });
};
class FakeWS {
  constructor(url) { this.url = url; this.readyState = 1; window.__wsUrl = url; }
  close() {} send() {}
}
window.WebSocket = FakeWS;
window.confirm = () => true;
window.alert = () => {};
window.ResizeObserver = class { observe() {} };

/* ---- запуск ---- */
window.eval(js);
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
  check('время последней свечи показано', $('engineBox').textContent.includes('Последняя свеча'));

  // вкладка бэктеста: период, файлы, запуск задания
  const btTab = Array.from(window.document.querySelectorAll('.tab')).find((t) => t.dataset.tab === 'backtest');
  btTab.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 200));
  const hidden = (id) => $(id).closest('.field').classList.contains('hidden');
  check('по умолчанию период задаётся количеством свечей', !hidden('bt_limit_candles') && hidden('bt_date_from'));
  $('bt_period_mode').value = 'dates';
  $('bt_period_mode').dispatchEvent(new window.Event('change'));
  check('переключение на даты показывает поля периода', hidden('bt_limit_candles') && !hidden('bt_date_from'));
  $('bt_period_mode').value = 'limit';
  $('bt_period_mode').dispatchEvent(new window.Event('change'));
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

  // вкладка графика
  const chartTab = Array.from(window.document.querySelectorAll('.tab')).find((t) => t.dataset.tab === 'chart');
  chartTab.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 400));
  check('график запросил свечи', calls.some((c) => c.includes('/api/candles')));
  check('вкладка графика активна', $('tab-chart').classList.contains('active'));
  check('вкладка панели скрыта', !$('tab-dashboard').classList.contains('active'));

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
  check('нет необработанных ошибок', errors.length === 0, errors.join(' | '));
  console.log(process.exitCode ? '\nПРОВЕРКА ПРОВАЛЕНА' : '\nПанель работает: все проверки пройдены');
  process.exit(process.exitCode || 0);
}, 600);
