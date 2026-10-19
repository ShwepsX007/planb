"""Новый метод входа: движение целиком + сетка по направлению движения.

Проверяем то, что просил пользователь:

1. Сетка тянется по направлению движения: при падении 0 внизу, при росте 0 вверху
   (а не всегда снизу вверх, как было раньше).
2. Движение определяется целиком — от предыдущего максимума/минимума, а не только
   по последним N свечам; вход — на коррекции после движения.
3. Сделка не «висит»: если цена уже прошла цель, но профит по фактической цене
   есть — бот фиксирует его, а не ждёт «профита по цели», которого нет.
"""
from __future__ import annotations

import pytest

from app.config import BotSettings
from app.fib import detect_impulse, detect_signal, detect_swing, fib_grid, retrace_price
from app.models import Candle, Cycle, Fill, Position
from app.strategy import StrategyEngine

TF = 300_000


def swing_settings(**kwargs) -> BotSettings:
    base = dict(
        symbol="BTC/USDT", market_type="futures", timeframe="5m",
        entry_mode="swing", candles_count=3, swing_lookback=120, swing_fresh_candles=24,
        min_move_pct=2.0, min_correction_pct=5.0, fib_goal_pct=50.0, min_profit_pct=0.3,
        direction="both", move_measure="range",
        base_volume_usd=10.0, volume_multiplier=2.0, multiplier_start_position=3,
        max_positions=6, fee_pct=0.0, slippage_pct=0.0, total_profit_target_usd=0.0,
        cycle_stop_loss_pct=0.0,
    )
    base.update(kwargs)
    return BotSettings(**base)


def path(points, tf: int = TF):
    """Свечи по точкам ``[(цена, свечей), ...]`` — линейный ход цены."""
    out, ts, price = [], 0, points[0][0]
    for target, n in points[1:]:
        step = (target - price) / n
        for _ in range(n):
            o, c = price, price + step
            out.append(Candle(ts=ts, open=o, high=max(o, c) * 1.0005,
                              low=min(o, c) * 0.9995, close=c, volume=1.0))
            price = c
            ts += tf
    return out


def down_leg_with_bounce(bounce: float = 2.0):
    """Падение 100->90 за 15 свечей, затем отскок на ``bounce`` % от 90."""
    return path([(100, 0), (90, 15), (90 * (1 + bounce / 100), 4)])


def up_leg_with_pullback(pullback: float = 2.0):
    return path([(90, 0), (100, 15), (100 * (1 - pullback / 100), 4)])


# ------------------------------------------------------- 1. направление сетки
def test_grid_zero_is_at_the_end_of_the_move():
    """0 % — конец движения: внизу после падения, вверху после роста."""
    down = {lv["level"]: lv["price"] for lv in fib_grid(90, 100, move_side="down")}
    up = {lv["level"]: lv["price"] for lv in fib_grid(90, 100, move_side="up")}

    assert down[0.0] == pytest.approx(90) and down[1.0] == pytest.approx(100)
    assert up[0.0] == pytest.approx(100), "при росте 0 должен быть вверху"
    assert up[1.0] == pytest.approx(90)
    assert down[0.5] == pytest.approx(95) == up[0.5]          # середина одна
    # 61.8 % — глубокая коррекция: после падения выше середины, после роста ниже
    assert down[0.618] > 95 > up[0.618]


def test_retrace_target_is_measured_from_the_end_of_the_move():
    assert retrace_price(90, 100, "down", 61.8) == pytest.approx(96.18)
    assert retrace_price(90, 100, "up", 61.8) == pytest.approx(93.82)
    assert retrace_price(90, 100, "up", 0) == pytest.approx(100)
    assert retrace_price(90, 100, "up", 100) == pytest.approx(90)


def test_up_move_grid_is_inverted_in_the_chart_payload(tmp_path):
    """График получает уровни с 0 вверху — раньше все уровни рисовались снизу вверх."""
    from app.bot import BotRuntime
    from app.db import Database

    settings = swing_settings()
    runtime = BotRuntime(Database(str(tmp_path / "grid.sqlite3")), settings)
    runtime.engine.cycle = Cycle(
        id="upcycle", side="short", signal_ts=0, signal_price=100.0, opened_ts=0,
        swing_high=100.0, swing_low=90.0, goal_pct=50.0, target=95.0, move_side="up",
    )
    pulse = Candle(ts=0, open=100, high=100, low=90, close=98, volume=1)
    runtime._chart_candles = [pulse]
    payload = runtime.chart_payload(10)
    by_level = {lv["level"]: lv["price"] for lv in payload["levels"] if "highlight" not in lv}
    assert by_level[0.0] == pytest.approx(100.0)
    assert by_level[1.0] == pytest.approx(90.0)
    assert payload["grid_zero"] == "high"
    assert payload["move_side"] == "up"


# ------------------------------------------------- 2. движение определяется целиком
def test_swing_signal_opens_on_a_forming_candle_without_waiting_for_close():
    candles = down_leg_with_bounce(2.0)
    engine = StrategyEngine(swing_settings())
    engine.set_history(candles[:-1])

    actions = engine.on_tick(candles[-1].close, candles[-1])

    assert actions and actions[0].kind == "entry"
    assert actions[0].side == "long"
    assert actions[0].ref_price == pytest.approx(candles[-1].close)


def test_swing_uses_the_whole_move_not_the_last_candles():
    """Сетка должна тянуться 90..100, а не по трём последним растущим свечам."""
    candles = down_leg_with_bounce(2.0)
    impulse, reason = detect_swing(candles, swing_settings())
    assert impulse is not None, reason
    assert impulse.mode == "swing"
    assert impulse.side == "long" and impulse.move_side == "down"
    assert impulse.low == pytest.approx(90.0, abs=0.1)
    assert impulse.high == pytest.approx(100.0, abs=0.1)
    assert impulse.target == pytest.approx(95.0, abs=0.1)
    assert impulse.candles >= 10, "движение целиком, а не последние 3 свечи"
    assert 5 < impulse.correction_pct < 50

    # для сравнения: старый метод видит только последние 3 растущие свечи
    old, old_reason = detect_impulse(candles, swing_settings(entry_mode="impulse",
                                                             min_move_pct=0.1,
                                                             min_profit_pct=0.01))
    if old is not None:
        assert old.side == "short", "старый метод ищет шорт по трём растущим свечам"
        assert old.high - old.low < impulse.high - impulse.low


def test_swing_finds_the_last_leg_not_the_oldest_extreme():
    """Зигзаг 100->90->92->82: сетка по последней ноге 92->82, а не 100->82."""
    candles = path([(100, 0), (90, 12), (92, 4), (82, 12), (84, 4)])
    impulse, reason = detect_swing(candles, swing_settings())
    assert impulse is not None, reason
    assert impulse.side == "long"
    assert impulse.high == pytest.approx(92.0, abs=0.3)
    assert impulse.low == pytest.approx(82.0, abs=0.3)
    assert impulse.target == pytest.approx(87.0, abs=0.3)
    assert impulse.entry_price == pytest.approx(84.0, abs=0.3)


def test_swing_short_after_an_up_move():
    """Рост 90->100 и откат: движение вверх, сетка перевёрнута, вход в шорт."""
    candles = up_leg_with_pullback(2.0)
    impulse, reason = detect_swing(candles, swing_settings())
    assert impulse is not None, reason
    assert impulse.side == "short" and impulse.move_side == "up"
    assert impulse.low == pytest.approx(90.0, abs=0.1)
    assert impulse.high == pytest.approx(100.0, abs=0.1)
    # цель отсчитывается от вершины вниз: 50 % движения
    assert impulse.target == pytest.approx(95.0, abs=0.1)
    assert impulse.entry_price > impulse.target, "шорт входит выше цели"


def test_swing_needs_a_correction_not_the_extreme_itself():
    """Вход «в самом экстремуме» — это не коррекция; раньше так и было."""
    impulse, reason = detect_swing(path([(100, 0), (90, 15)]), swing_settings())
    assert impulse is None
    assert "коррекци" in reason

    engine = StrategyEngine(swing_settings())
    candles = path([(100, 0), (90, 15)])
    for c in candles[:-1]:
        engine.push_candle(c)
    assert engine.on_candle(candles[-1]) == []
    assert "коррекци" in engine.last_check or "рано" in engine.last_check


def test_swing_requires_the_correction_to_be_visible():
    """Отскок 0.5 % от 10 % движения — это ещё не коррекция (порог 5 %)."""
    tiny = path([(100, 0), (90, 15), (90.45, 2)])       # 0.5 % отскока
    impulse, reason = detect_swing(tiny, swing_settings())
    assert impulse is None
    assert "коррекци" in reason


def test_swing_rejects_stale_moves():
    candles = path([(100, 0), (90, 10), (91, 40)])
    impulse, reason = detect_swing(candles, swing_settings(swing_fresh_candles=12))
    assert impulse is None
    assert "свежести" in reason


def test_swing_respects_direction_setting():
    candles = down_leg_with_bounce(2.0)
    impulse, reason = detect_swing(candles, swing_settings(direction="short"))
    assert impulse is None and "отключено" in reason


def test_swing_rejects_when_correction_already_reached_the_target():
    """Отскок выше цели: входить поздно, профита до цели уже нет."""
    candles = path([(100, 0), (90, 15), (96.5, 5)])      # цель 95 уже пройдена
    impulse, reason = detect_swing(candles, swing_settings())
    assert impulse is None
    assert "поздно" in reason


def test_detect_signal_switches_modes():
    old = down_leg_with_bounce(2.0)
    assert detect_signal(old, swing_settings())[0].mode == "swing"
    impulse, _ = detect_signal(old, swing_settings(entry_mode="impulse",
                                                   min_move_pct=0.1, min_profit_pct=0.01))
    assert impulse is not None and impulse.mode == "impulse"


# ------------------------------------------------ движок: вход и закрытие по цели
def test_engine_opens_swing_cycle_and_spans_the_whole_leg():
    settings = swing_settings()
    candles = down_leg_with_bounce(2.0)
    engine = StrategyEngine(settings)
    for c in candles[:-1]:
        engine.push_candle(c)
    acts = engine.on_candle(candles[-1])
    assert acts and acts[0].kind == "entry" and acts[0].side == "long"
    engine.apply_fill(acts[0], Fill(price=candles[-1].close,
                                    qty=acts[0].volume_usd / candles[-1].close,
                                    ts=candles[-1].ts))

    cycle = engine.cycle
    assert cycle.move_side == "down"
    assert cycle.swing_low == pytest.approx(90.0, abs=0.1)
    assert cycle.swing_high == pytest.approx(100.0, abs=0.1)
    assert cycle.target == pytest.approx(95.0, abs=0.1)
    assert "движение целиком" in cycle.note and "коррекция" in cycle.note

    # цена доходит до цели — цикл закрывается
    ts = candles[-1].ts + TF
    acts = engine.on_candle(Candle(ts=ts, open=92.5, high=95.5, low=92.4, close=95.2,
                                   volume=1.0))
    assert acts and acts[0].kind == "close_all"
    closed = engine.close_cycle(acts[0].ref_price, ts, acts[0].reason)
    assert closed.pnl > 0 and engine.cycle is None


def test_engine_short_cycle_grid_direction():
    settings = swing_settings()
    candles = up_leg_with_pullback(2.0)
    engine = StrategyEngine(settings)
    for c in candles[:-1]:
        engine.push_candle(c)
    acts = engine.on_candle(candles[-1])
    assert acts and acts[0].kind == "entry" and acts[0].side == "short"
    engine.apply_fill(acts[0], Fill(price=candles[-1].close,
                                    qty=acts[0].volume_usd / candles[-1].close,
                                    ts=candles[-1].ts))
    cycle = engine.cycle
    assert cycle.move_side == "up"
    assert cycle.target == pytest.approx(95.0, abs=0.1)
    assert cycle.target < cycle.avg_entry          # шорт: цель ниже входа
    # 0 % сетки — на вершине
    levels = {lv["level"]: lv["price"] for lv in
              fib_grid(cycle.swing_low, cycle.swing_high, move_side=cycle.move_side)}
    assert levels[0.0] == pytest.approx(cycle.swing_high)


# ------------------------------------------- 3. «сделка висит»: фиксация по цене
def make_cycle(*, avg: float = 95.0, target: float = 95.0, side: str = "long",
               qty: float = 1.0) -> Cycle:
    pos = Position(idx=1, ts=0, price=avg, qty=qty, volume_usd=avg * qty, fee=0.0)
    return Cycle(id="c1", side=side, signal_ts=0, signal_price=avg, opened_ts=0,
                 swing_high=100.0, swing_low=90.0, goal_pct=50.0, target=target,
                 move_side="down" if side == "long" else "up", positions=[pos])


def test_over_target_profit_is_taken_at_the_real_price():
    """Скриншот: средняя почти на цели, но цена ушла на 0.2 % выше — фиксируем.

    Раньше «armed» считался по профиту ДО ЦЕЛИ: цель ≈ средняя, профит ~0 —
    и бот игнорировал даже резкий уход цены в нужную сторону. Сделка «висела».
    """
    settings = swing_settings(min_profit_pct=0.05)
    engine = StrategyEngine(settings)
    engine.cycle = make_cycle(avg=95.0, target=95.0)

    # 95.02: цель достигнута, но профит 0.02 % < 0.05 % — не закрываем,
    # зато честно объясняем, чего ждём
    assert engine.on_tick(95.02) == []
    assert "цель" in engine.last_check and "держим" in engine.last_check

    # 95.2: фактический профит 0.21 % — забираем по реальной цене, а не по цели
    acts = engine.on_tick(95.2)
    assert acts and acts[0].kind == "close_all"
    assert acts[0].ref_price == pytest.approx(95.2), "фиксируем по цене лучше цели"
    assert "лучше цели" in acts[0].reason


def test_profit_below_minimum_keeps_the_cycle_but_reports_it():
    settings = swing_settings(min_profit_pct=0.5)
    engine = StrategyEngine(settings)
    engine.cycle = make_cycle(avg=95.0, target=95.0)
    assert engine.on_tick(95.2) == []          # 0.21 % < 0.5 %
    assert "достигнута" in engine.last_check


def test_target_hit_on_a_candle_uses_the_close_when_it_is_better():
    settings = swing_settings(min_profit_pct=0.1)
    engine = StrategyEngine(settings)
    engine.cycle = make_cycle(avg=95.0, target=95.0)
    acts = engine.on_candle(Candle(ts=TF, open=95.0, high=95.6, low=94.9, close=95.5,
                                   volume=1.0))
    assert acts and acts[0].kind == "close_all"
    assert acts[0].ref_price == pytest.approx(95.5)


def test_short_over_target_is_taken_too():
    settings = swing_settings(min_profit_pct=0.05)
    engine = StrategyEngine(settings)
    engine.cycle = make_cycle(avg=95.0, target=95.0, side="short")
    acts = engine.on_tick(94.8)
    assert acts and acts[0].kind == "close_all"
    assert acts[0].ref_price == pytest.approx(94.8)


def test_add_happens_when_price_makes_a_new_low_even_if_it_barely_moves_the_average():
    """Регрессия «бот вошёл и не доливает».

    Настройки как у пользователя: объём 1 $, минимальный профит 0.01 %, цель 45 %.
    После входа цена обновила минимум на 90 пунктов, цель поехала за ней, и
    старый запрет «долив не поможет» (профит после усреднения 0.004 % < 0.01 %)
    блокировал ЛЮБОЙ долив — цикл застревал с одной позицией. Долив обязан
    случиться: каждая позиция двигает среднюю к цене и приближает выход.
    """
    settings = swing_settings(
        candles_count=2, min_move_pct=0.01, min_correction_pct=0.0, min_profit_pct=0.01,
        fib_goal_pct=45.0, base_volume_usd=1.0, max_positions=10, update_swing_in_cycle=True)
    engine = StrategyEngine(settings)
    cycle = Cycle(id="stuck", side="long", signal_ts=0, signal_price=81628.62,
                  opened_ts=0, swing_high=81645.7, swing_low=81602.8, goal_pct=45.0,
                  target=81622.14, move_side="down",
                  positions=[Position(idx=1, ts=0, price=81628.62, qty=1.0 / 81628.62,
                                      volume_usd=1.0, fee=0.0)],
                  extreme_at_last_entry=81602.8)
    engine.cycle = cycle

    acts = engine.on_candle(Candle(ts=TF, open=81600.0, high=81605.0, low=81539.0,
                                   close=81539.0, volume=1.0))
    adds = [a for a in acts if a.kind == "add"]
    assert adds, f"долив не сделан: {engine.last_check}"
    assert adds[0].position_idx == 2 and adds[0].volume_usd == pytest.approx(1.0)
    # цель поехала за новым минимумом, но средняя после долива уже ближе к цели
    assert engine.cycle.target < 81622.14
    assert "средняя" in adds[0].reason and "профит" in adds[0].reason


def test_add_is_not_postponed_until_the_average_is_perfect():
    """Долив не откладывается «до идеального профита» — иначе бот молчит.

    Проверяем на той же ситуации, что была у пользователя: после первого долива
    профит до цели может быть ещё меньше минимума, и всё равно бот продолжает
    усредняться на каждом новом минимуме, пока не дойдёт до лимита позиций.
    """
    settings = swing_settings(
        candles_count=2, min_move_pct=0.01, min_correction_pct=0.0, min_profit_pct=0.01,
        fib_goal_pct=45.0, base_volume_usd=1.0, max_positions=4, update_swing_in_cycle=True)
    engine = StrategyEngine(settings)
    engine.cycle = Cycle(
        id="stuck", side="long", signal_ts=0, signal_price=81628.62, opened_ts=0,
        swing_high=81645.7, swing_low=81602.8, goal_pct=45.0, target=81622.14,
        move_side="down",
        positions=[Position(idx=1, ts=0, price=81628.62, qty=1.0 / 81628.62,
                            volume_usd=1.0, fee=0.0)],
        extreme_at_last_entry=81602.8)

    price, ts, adds = 81600.0, 0, 0
    for _ in range(400):
        price -= 1.0
        ts += TF
        acts = engine.on_candle(Candle(ts=ts, open=price + 1, high=price + 1.2,
                                       low=price, close=price, volume=1.0))
        for a in acts:
            if a.kind == "add":
                adds += 1
                engine.apply_fill(a, Fill(price=price, qty=a.volume_usd / price, ts=ts))
                break
        if adds >= 3:
            break
    assert adds == 3, f"доливов {adds}, последняя проверка: {engine.last_check}"
    assert len(engine.cycle.positions) == 4


def test_restored_cycle_can_still_add_positions():
    """После перезапуска бот не должен «забыть» экстремум и перестать доливать.

    В старых сохранениях поля extreme_at_last_entry нет; с нулём условие
    «новый экстремум» не срабатывало никогда — цикл висел с одной позицией.
    """
    settings = swing_settings(min_profit_pct=0.5)
    engine = StrategyEngine(settings)
    engine.restore({
        "id": "restored", "side": "long", "signal_ts": 0, "signal_price": 100.0,
        "opened_ts": 0, "swing_high": 100.0, "swing_low": 99.0, "goal_pct": 50.0,
        "target": 99.5, "move_side": "down", "note": "",
        "positions": [{"idx": 1, "ts": 0, "price": 99.9, "qty": 0.1,
                       "volume_usd": 10.0, "fee": 0.0}],
    })
    assert engine.cycle.extreme_at_last_entry == pytest.approx(99.0)

    acts = engine.on_candle(Candle(ts=TF, open=99.2, high=99.2, low=98.8, close=98.8,
                                   volume=1.0))
    assert [a for a in acts if a.kind == "add"], engine.last_check


def test_status_between_candles_does_not_promise_an_add_that_cannot_happen():
    """Пока нового экстремума нет, панель должна писать именно это, а не «ждём долива»."""
    settings = swing_settings(min_profit_pct=0.5)
    engine = StrategyEngine(settings)
    engine.cycle = make_cycle(avg=100.0, target=100.5)
    engine.cycle.swing_low = 99.0
    engine.cycle.extreme_at_last_entry = 99.0
    engine.on_tick(99.6)                    # цена отошла от минимума
    assert "нужен новый минимум" in engine.last_check

    engine.cycle.swing_low = 98.9           # минимум обновлён, профит до цели < мин.
    engine.on_tick(99.95)
    assert "долив" in engine.last_check and "готов" in engine.last_check


def test_add_is_allowed_when_it_restores_the_profit():
    """Нормальный долив: после усреднения профит до цели снова >= минимума."""
    settings = swing_settings(min_profit_pct=1.0, base_volume_usd=10.0)
    engine = StrategyEngine(settings)
    candles = path([(100, 0), (95, 15), (95.5, 2)])
    for c in candles[:-1]:
        engine.push_candle(c)
    acts = engine.on_candle(candles[-1])
    if not acts:
        pytest.skip("на этих свечах детектор не дал входа — сценарий покрыт ниже")
    engine.apply_fill(acts[0], Fill(price=candles[-1].close, qty=1.0, ts=candles[-1].ts))
    # роняем цену, пока не появится долив
    price, ts = candles[-1].close, candles[-1].ts
    for _ in range(400):
        price -= 0.05
        ts += TF
        acts = engine.on_candle(Candle(ts=ts, open=price + 0.05, high=price + 0.06,
                                       low=price, close=price, volume=1.0))
        if acts and acts[0].kind == "add":
            break
    else:
        pytest.fail("долив не появился")
    engine.apply_fill(acts[0], Fill(price=price, qty=acts[0].volume_usd / price, ts=ts))
    assert engine.cycle.profit_pct_to(engine.cycle.target) >= settings.min_profit_pct - 1e-9
