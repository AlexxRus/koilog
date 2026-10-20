"""Тесты state-машины возврата: ``koilog.health.state`` (раунд 5+5a).

Пререгистрация — ``docs/state-machine-spec.md`` (решения O1–O3 = A/A/A).
Проверяется прежде всего то, что можно сломать незаметно (спека §8):

- переходы ``sick → ramp → normal``;
- ``ramp → rest → ramp`` - счётчик ``k`` **сохраняется** (пауза, не сброс);
- ``sick`` посреди рампы **сбрасывает** ``k`` (anchor сдвигается на конец
  нового эпизода);
- гейт-блокированный день (``rest``) ``k`` не двигает;
- floor целевого веса рампы (O2 = A): 1RM ⇒ 42 ⇒ 40, поле ``rule``;
- скор **не** влияет на состояние (§8.3);
- состояние детерминировано: повторный расчёт даёт тот же результат (§8.1).

Синтетическая база: ``sets`` (тренировочные дни = выполненные дни рампы),
``aw_daily`` (фон для гейта), ``illness_episodes`` (эпизоды).
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import date as date_cls
from datetime import timedelta

from koilog import core
from koilog.health import state


def _calendar(start: str, count: int) -> list[str]:
    first = date_cls.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sets (
            exercise   TEXT,
            date       TEXT,
            weight     REAL,
            reps       INTEGER,
            rm         REAL,
            rm_session REAL,
            set_index  INTEGER,
            koi        REAL,
            is_warmup  INTEGER NOT NULL DEFAULT 0,
            skipped    INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE aw_daily (
            date       TEXT PRIMARY KEY,
            rhr        REAL,
            hrv_sdnn   REAL,
            resp_rate  REAL,
            wrist_temp REAL,
            spo2       REAL,
            sleep_hours REAL
        );
        CREATE TABLE illness_episodes (
            id     INTEGER PRIMARY KEY,
            start  TEXT NOT NULL,
            end    TEXT NOT NULL,
            level  TEXT NOT NULL,
            source TEXT,
            type   TEXT NOT NULL DEFAULT 'unknown',
            notes  TEXT
        );
        """
    )
    return conn


#: Фон, к которому гейт/каналы считают z (значения «нейтральной» ночи).
_RHR, _HRV, _RESP, _TEMP, _SPO2, _SLEEP = 60.0, 70.0, 15.0, 35.5, 96.0, 7.5


def _flat_physiology(conn, start: str, count: int) -> list[str]:
    """Ровный фон с запасом вариации (SD не ноль, значит z вычислим).

    Осторожно: базлайн считается по **всей** серии ``aw_daily`` (включая
    будущие дни) - так устроен гейт. Поэтому «обычная» ночь ставится ровно на
    фон (z = 0), а разброс создают редкие экскурсии каждую шестую ночь: SD
    становится заметно больше нуля, не сдвигая фон.
    """
    days = _calendar(start, count)
    for index, day in enumerate(days):
        excursion = 1.5 if (index % 7 == 6) else 0.0
        tilt = excursion * (1.0 if (index // 7) % 2 else -1.0)
        conn.execute(
            "INSERT INTO aw_daily (date, rhr, hrv_sdnn, resp_rate, "
            "wrist_temp, spo2, sleep_hours) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (day, _RHR + tilt, _HRV - tilt, _RESP, _TEMP + tilt * 0.1,
             _SPO2, _SLEEP),
        )
    return days


def _night(conn, day: str, *, rhr: float | None = None,
           hrv: float | None = None) -> None:
    """Задать конкретную ночь (вернуть RHR к фону или поднять его)."""
    sets, params = [], []
    if rhr is not None:
        sets.append("rhr = ?")
        params.append(rhr)
    if hrv is not None:
        sets.append("hrv_sdnn = ?")
        params.append(hrv)
    params.append(day)
    conn.execute(f"UPDATE aw_daily SET {', '.join(sets)} WHERE date = ?",
                 params)


def _ready_nights(conn, days: list[str]) -> None:
    """Сделать указанные ночи «готовыми»: RHR и ВСР ровно на фоне (z ~ 0)."""
    for day in days:
        _night(conn, day, rhr=_RHR, hrv=_HRV)


def _spike(conn, days: list[str]) -> None:
    """Высокий RHR на указанные дни (гейт держит - rest, не окно тишины)."""
    for day in days:
        _night(conn, day, rhr=80.0)


def _train(conn, day: str, exercise: str = "Приседания со штангой",
           weight: float = 45.0, reps: int = 5) -> None:
    """Отметить тренировочный день (одна рабочая сессия)."""
    conn.execute(
        "INSERT INTO sets (exercise, date, weight, reps, rm, rm_session, "
        "is_warmup, set_index) VALUES (?, ?, ?, ?, ?, ?, 0, 1)",
        (exercise, day, weight, reps, weight * reps * 0.0333 + weight,
         weight * reps * 0.0333 + weight),
    )


def _episode(conn, start: str, end: str, level: str = "confirmed") -> None:
    conn.execute(
        "INSERT INTO illness_episodes (start, end, level, source, type) "
        "VALUES (?, ?, ?, 'test', 'unknown')", (start, end, level),
    )


class TransitionTests(unittest.TestCase):
    """Переходы состояний (спека §3, §5)."""

    def _setup(self) -> sqlite3.Connection:
        conn = _db()
        # Длинный ровный фон **с запасом**: нужен личный базлайн
        # (BASELINE_MIN = 14 ночей) уже к началу эпизода.
        _flat_physiology(conn, "2023-12-01", 120)
        conn.commit()
        return conn

    def test_sick_while_episode_active(self):
        conn = self._setup()
        self.addCleanup(conn.close)
        _episode(conn, "2024-01-10", "2024-01-12")
        conn.commit()
        day = state.state_day(conn, "2024-01-11")
        self.assertEqual(day.state, state.STATE_SICK)

    def test_after_episode_is_ramp_within_silence_window(self):
        conn = self._setup()
        self.addCleanup(conn.close)
        _episode(conn, "2024-01-10", "2024-01-12")
        conn.commit()
        # Окно тишины 14 дн: 13..26.01.
        day = state.state_day(conn, "2024-01-15")
        self.assertEqual(day.state, state.STATE_RAMP)
        self.assertIn("окно тишины", day.reason)

    def test_ramp_to_normal_after_ramp_days_and_gate(self):
        conn = self._setup()
        self.addCleanup(conn.close)
        _episode(conn, "2024-01-10", "2024-01-12")
        # Пять выполненных дней рампы после эпизода (13..17.01).
        for day in _calendar("2024-01-13", 5):
            _train(conn, day)
        _ready_nights(conn, ["2024-01-26", "2024-01-27"])
        conn.commit()
        # Пока идёт окно тишины - рампа, даже с выполненными днями.
        mid = state.state_day(conn, "2024-01-17")
        self.assertEqual(mid.state, state.STATE_RAMP)
        self.assertEqual(mid.ramp_day, 5)
        # Окно прошло (27.01) и гейт готов - выходим в штатный план.
        done = state.state_day(conn, "2024-01-27")
        self.assertEqual(done.state, state.STATE_NORMAL)

    def test_ramp_to_rest_keeps_counter(self):
        """Пауза в rest не сбрасывает k (спека §3, §5.3)."""
        conn = self._setup()
        self.addCleanup(conn.close)
        _episode(conn, "2024-01-10", "2024-01-12")
        _train(conn, "2024-01-13")
        _train(conn, "2024-01-14")
        # Две ночи подряд с высоким RHR - гейт держит (это rest, не окно).
        _spike(conn, ["2024-01-15", "2024-01-16"])
        conn.commit()
        rest = state.state_day(conn, "2024-01-16")
        self.assertEqual(rest.state, state.STATE_REST)
        self.assertTrue(rest.ramp_paused)
        # Каналы вернулись в фон - снова рампа, и счётчик сохранён (k = 2).
        _ready_nights(conn, ["2024-01-16", "2024-01-17"])
        conn.commit()
        ramp = state.state_day(conn, "2024-01-17")
        self.assertEqual(ramp.state, state.STATE_RAMP)
        self.assertEqual(ramp.ramp_day, 2)

    def test_sick_in_the_middle_of_ramp_resets_counter(self):
        """Новый эпизод сбрасывает k: рампа начинается заново (§5.1)."""
        conn = self._setup()
        self.addCleanup(conn.close)
        _episode(conn, "2024-01-10", "2024-01-12")
        _train(conn, "2024-01-13")
        _train(conn, "2024-01-14")
        _episode(conn, "2024-01-15", "2024-01-16")   # рецидив посреди рампы
        conn.commit()
        sick = state.state_day(conn, "2024-01-15")
        self.assertEqual(sick.state, state.STATE_SICK)
        # После второго эпизода anchor сдвинулся: выполненных дней рампы нет.
        after = state.state_day(conn, "2024-01-17")
        self.assertEqual(after.state, state.STATE_RAMP)
        self.assertEqual(after.ramp_day, 0)

    def test_rest_day_without_session_does_not_move_counter(self):
        """Гейт-блокированный день без тренировки k не двигает (§5.1)."""
        conn = self._setup()
        self.addCleanup(conn.close)
        _episode(conn, "2024-01-10", "2024-01-12")
        _train(conn, "2024-01-13")
        _spike(conn, ["2024-01-14", "2024-01-15"])
        conn.commit()
        rest = state.state_day(conn, "2024-01-15")
        self.assertEqual(rest.state, state.STATE_REST)
        # Каналы вернулись в фон - рампа; тренировки в rest-дни не было, k = 1.
        _ready_nights(conn, ["2024-01-15", "2024-01-16"])
        conn.commit()
        ramp = state.state_day(conn, "2024-01-16")
        self.assertEqual(ramp.state, state.STATE_RAMP)
        self.assertEqual(ramp.ramp_day, 1)   # только 13.01

    def test_no_data_is_normal_without_ramp(self):
        conn = _db()
        self.addCleanup(conn.close)
        day = state.state_day(conn, "2024-01-01")
        self.assertEqual(day.state, state.STATE_NORMAL)
        self.assertEqual(day.reason, "нет данных")

    def test_state_is_deterministic(self):
        conn = self._setup()
        self.addCleanup(conn.close)
        _episode(conn, "2024-01-10", "2024-01-12")
        for day in _calendar("2024-01-13", 3):
            _train(conn, day)
        conn.commit()
        first = state.state_day(conn, "2024-01-18")
        second = state.state_day(conn, "2024-01-18")
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_score_does_not_change_state(self):
        """Скор - мониторинг: он не влияет на состояние (§8.3)."""
        conn = self._setup()
        self.addCleanup(conn.close)
        _episode(conn, "2024-01-10", "2024-01-12")
        conn.commit()
        day = state.state_day(conn, "2024-01-15")
        self.assertEqual(day.state, state.STATE_RAMP)
        # В state нет вообще понятия скора - это и есть гарантия.
        self.assertNotIn("score", day.to_dict())

    def test_long_gap_restarts_ramp(self):
        """Простой после рампы - не «прожитые 5 дней без нагрузки» (§5.2).

        Рампа завершена (5 выполненных дней), человек снова пропал на
        >= RAMP_GAP_DAYS: состояние снова ``ramp``, а счётчик отсчитывается
        заново (k = 0) от последней тренировки.
        """
        conn = self._setup()
        self.addCleanup(conn.close)
        conn.executescript(
            "CREATE TABLE load_daily (date TEXT, acwr REAL, acwr_warmup REAL, "
            "monotony REAL, strain REAL, weekly_tonnage REAL, rm_regress INT, "
            "gap_warmup INT, since_gap INT, sets INT)"
        )
        _episode(conn, "2024-01-10", "2024-01-12")
        for day in _calendar("2024-01-13", 5):
            _train(conn, day)
        _ready_nights(conn, ["2024-01-26", "2024-01-27"])
        # load-строка дня с длинным простоем: gap_warmup = 1.
        conn.execute(
            "INSERT INTO load_daily VALUES "
            "('2024-01-27', 0.0, 0.0, NULL, NULL, 0.0, 0, 1, 10, 0)")
        conn.commit()
        day = state.state_day(conn, "2024-01-27")
        self.assertEqual(day.state, state.STATE_RAMP)
        self.assertEqual(day.ramp_day, 0)


class RampTargetTests(unittest.TestCase):
    """5a: целевой вес рампы - floor от 1RM (O2 = A)."""

    def _setup(self) -> sqlite3.Connection:
        conn = _db()
        _flat_physiology(conn, "2023-12-01", 120)
        return conn

    def test_snap_to_ladder_floors_at_42(self):
        """1RM ⇒ 42 ⇒ 40: ближайший доступный вес **не тяжелее**."""
        ladder = (20.0, 30.0, 40.0, 45.0, 50.0)
        self.assertEqual(core.snap_to_ladder(42.0, ladder), 40.0)

    def test_ramp_target_rule_is_ramp_floor(self):
        conn = self._setup()
        self.addCleanup(conn.close)
        _train(conn, "2024-01-08", weight=45.0, reps=5)
        _episode(conn, "2024-01-10", "2024-01-12")
        conn.commit()
        target = state.ramp_target(conn, "2024-01-15")
        self.assertIsNotNone(target)
        self.assertEqual(target.rule, "ramp_floor")
        if target.weight is not None:
            # Целевой вес лежит на лестнице инвентаря и не тяжелее 45*0.95.
            ladder = __import__("koilog.equipment", fromlist=["x"]) \
                .available_weights("Приседания со штангой")
            intended = 45.0 * 0.95
            self.assertIn(target.weight, ladder)
            self.assertLessEqual(target.weight, intended + 1e-9)

    def test_ramp_target_without_weight_has_reason(self):
        conn = self._setup()
        self.addCleanup(conn.close)
        _train(conn, "2024-01-08", exercise="Подтягивания на перекладине",
               weight=82.0, reps=5)
        _episode(conn, "2024-01-10", "2024-01-12")
        conn.commit()
        target = state.ramp_target(
            conn, "2024-01-15", "Подтягивания на перекладине")
        # Веса нет - но правило всё равно структурное, и есть причина.
        self.assertEqual(target.rule, "ramp_floor")
        self.assertIsNone(target.weight)
        self.assertTrue(target.reason)


class PlanRuleTests(unittest.TestCase):
    """Структурное поле плана возврата (O2)."""

    def test_ramp_plan_carries_rule_field(self):
        from koilog.health import retrain

        conn = _db()
        self.addCleanup(conn.close)
        _flat_physiology(conn, "2023-12-01", 120)
        _train(conn, "2024-01-08", weight=45.0, reps=5)
        _episode(conn, "2024-01-10", "2024-01-12")
        conn.commit()
        plan = retrain.return_plan(conn, "Приседания со штангой",
                                   today="2024-01-15")
        self.assertEqual(plan.rule, "ramp_floor")
        self.assertEqual(plan.to_dict()["rule"], "ramp_floor")


if __name__ == "__main__":
    unittest.main()
