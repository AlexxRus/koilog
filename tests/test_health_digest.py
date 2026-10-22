"""Тесты дневного дайджеста: ``koilog.health.digest``.

Правило рекомендации - чистая функция, поэтому проверяется на собранных
руками каналах и состоянии нагрузки, без базы. Чтение ``aw_daily`` /
``load_daily`` проверяется на синтетической базе.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import date as date_cls
from datetime import timedelta

from koilog.health import digest
from koilog.health import state as health_state


class _Gate:
    """Минимальная заглушка :class:`retrain.BaselineGate` для правил."""

    def __init__(self, ready: bool, reasons: tuple[str, ...] = (),
                 window_only: bool = False) -> None:
        self.ready = ready
        self.reasons = reasons
        self.window_only = window_only


def _channel(key: str, label: str, z: float | None, sign: int,
             value: float | None = None) -> digest.Channel:
    bad = z is not None and sign * z > digest.Z_CHANNEL
    return digest.Channel(key, label, value, z, bad)


def _ok_channels() -> list[digest.Channel]:
    return [
        _channel("rhr", "RHR", 0.2, +1, 60.0),
        _channel("hrv_sdnn", "ВСР", 0.3, -1, 70.0),
        _channel("resp_rate", "ЧДД", 0.1, +1, 15.0),
        _channel("wrist_temp", "t запястья", 0.0, +1, 35.5),
        _channel("spo2", "SpO2", 0.0, -1, 96.0),
        _channel("sleep_hours", "сон", 0.1, -1, 7.5),
    ]


def _load(**overrides) -> dict:
    base = {
        "available": True, "date": "2026-10-17", "acwr": 1.1,
        "acwr_warmup": 1.1, "monotony": 1.2, "strain": 9000.0,
        "weekly_tonnage": 5000.0, "rm_regress": False, "gap_warmup": False,
        "since_gap": 30, "last_training": "2026-10-16",
        "days_since_training": 1,
    }
    base.update(overrides)
    return base


class RecommendTests(unittest.TestCase):
    def test_default_is_plan(self):
        rec = digest.recommend(_ok_channels(), _load(), _Gate(True))
        self.assertEqual(rec["level"], "ok")

    def test_red_flag_beats_everything(self):
        channels = _ok_channels()
        channels[1] = _channel("hrv_sdnn", "ВСР", -2.5, -1)   # ВСР вниз
        channels[5] = _channel("sleep_hours", "сон", -2.0, -1)  # сон вниз
        rec = digest.recommend(
            channels, _load(rm_regress=True, monotony=2.7),
            _Gate(False, ("активный эпизод 2026-10-11…2026-10-13",)))
        self.assertEqual(rec["level"], "red")

    def test_hold_when_gate_closed(self):
        load = _load()
        rec = digest.recommend(
            _ok_channels(), load, _Gate(False, ("2026-10-17: RHR z +2.5 выше фона (>1)",)))
        self.assertEqual(rec["level"], "hold")

    def test_rest_with_unfinished_ramp_shows_pause(self):
        """``rest`` при незавершённой рампе видно как «рампа приостановлена».

        Спека §5.3: покой не должен молча терять метку рампы - иначе
        незавершённая рампа выглядит как безымянный покой.
        """
        day = health_state.StateDay(
            day="2026-10-20", state=health_state.STATE_REST,
            reason="гейт ВСР/RHR держит", ramp_day=1,
            ramp_days=health_state.RAMP_DAYS, ramp_paused=True)
        rec = digest._recommend_from_state(day, None)
        self.assertEqual(rec["level"], "hold")
        self.assertIn("рампа приостановлена", rec["text"])
        self.assertIn("рампа приостановлена", digest._state_line(day.to_dict()))

    def test_rest_without_ramp_has_no_pause_mark(self):
        """Покой без начатой рампы - не «пауза»: метки нет (§5.3)."""
        day = health_state.StateDay(
            day="2026-10-20", state=health_state.STATE_REST,
            reason="гейт ВСР/RHR держит", ramp_day=0, ramp_paused=True)
        self.assertNotIn("рампа приостановлена",
                         digest._state_line(day.to_dict()))
        self.assertNotIn("рампа приостановлена",
                         digest._recommend_from_state(day, None)["text"])

    def test_silence_window_after_episode_is_ramp_not_hold(self):
        """Каналы в фоне, но сразу после эпизода - рампа, не покой."""
        rec = digest.recommend(
            _ok_channels(), _load(),
            _Gate(False, ("окно тишины после эпизода 2026-10-11…2026-10-13: "
                          "возврат по рампе до 2026-10-27",), window_only=True))
        self.assertEqual(rec["level"], "ramp")
        self.assertIn("отскок", rec["text"])

    def test_hold_on_active_episode_without_watch(self):
        rec = digest.recommend(
            [], _load(), _Gate(False, ("активный эпизод 2026-10-11…2026-10-13",)))
        self.assertEqual(rec["level"], "hold")

    def test_no_watch_and_no_episode_is_not_hold(self):
        """Пустой aw_daily не должен запирать дайджест в «покой» навсегда."""
        rec = digest.recommend(
            [], _load(), _Gate(False, ("нет данных часов: aw_daily пуст",)))
        self.assertEqual(rec["level"], "ok")

    def test_triple_signal_is_easy_tomorrow(self):
        channels = _ok_channels()
        channels[0] = _channel("rhr", "RHR", 2.2, +1)       # пульс вверх
        channels[1] = _channel("hrv_sdnn", "ВСР", -2.2, -1)  # ВСР вниз
        channels[2] = _channel("resp_rate", "ЧДД", 2.1, +1)  # ЧДД вверх
        rec = digest.recommend(channels, _load(), _Gate(True))
        self.assertEqual(rec["level"], "easy")

    def test_gap_with_inflated_acwr_is_ramp(self):
        rec = digest.recommend(
            _ok_channels(), _load(gap_warmup=True, acwr=2.0), _Gate(True))
        self.assertEqual(rec["level"], "ramp")

    def test_long_break_with_inflated_acwr_is_ramp(self):
        rec = digest.recommend(
            _ok_channels(),
            _load(gap_warmup=False, days_since_training=20, acwr=1.9),
            _Gate(True))
        self.assertEqual(rec["level"], "ramp")

    def test_inflated_acwr_without_gap_is_not_ramp(self):
        rec = digest.recommend(
            _ok_channels(), _load(gap_warmup=False, acwr=2.0), _Gate(True))
        self.assertEqual(rec["level"], "ok")

    def test_every_recommendation_has_title_and_text(self):
        for rec in (
            digest.recommend(_ok_channels(), _load(), _Gate(True)),
            digest.recommend([], _load(), _Gate(False, ("нет данных часов: aw_daily пуст",))),
            digest.recommend(_ok_channels(), _load(gap_warmup=True, acwr=2.0),
                             _Gate(True)),
        ):
            self.assertTrue(rec["title"])
            self.assertTrue(rec["text"])

    # -- скор дня в карточке ------------------------------------------------

    def test_hold_text_explains_channel_gate_difference(self):
        """«В фоне» по каналам и «держит» по гейту - разные пороги, не спор."""
        rec = digest.recommend(
            _ok_channels(), _load(),
            _Gate(False, ("2026-10-17: RHR z +1.2 выше фона (>1)",)))
        self.assertEqual(rec["level"], "hold")
        self.assertIn("±1", rec["text"])
        self.assertIn("±1.5", rec["text"])

    def test_score_note_says_the_model_is_quiet(self):
        score = {"score": 0.42, "threshold": 0.6, "alert": 0}
        rec = digest.recommend(
            _ok_channels(), _load(),
            _Gate(False, ("2026-10-17: RHR z +1.2 выше фона (>1)",)), score)
        # Скор - тихий мониторинг: число не дублируется в тексте решения,
        # но порог и «мониторинг» видны (в строке карточки число уже есть).
        self.assertIn("мониторинг", rec["text"])
        self.assertIn("0.60", rec["text"])
        self.assertNotIn("0.42", rec["text"])

    def test_score_note_mentions_alert(self):
        score = {"score": 0.88, "threshold": 0.7, "alert": 1}
        rec = digest.recommend(
            _ok_channels(), _load(),
            _Gate(False, (), window_only=True), score)
        self.assertEqual(rec["level"], "ramp")
        self.assertIn("выше порога", rec["text"])

    def test_score_note_survives_missing_threshold(self):
        score = {"score": 0.42, "threshold": None, "alert": None}
        rec = digest.recommend(
            _ok_channels(), _load(),
            _Gate(False, ("2026-10-17: RHR z +1.2 выше фона (>1)",)), score)
        self.assertIn("порог", rec["text"])

    def test_recommend_without_score_stays_short(self):
        rec = digest.recommend(_ok_channels(), _load(), _Gate(True))
        self.assertNotIn("Скор дня", rec["text"])
        self.assertNotIn("мониторинг", rec["text"])


class ChannelTests(unittest.TestCase):
    def _db(self, days: int = 30) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE aw_daily (
                date TEXT PRIMARY KEY, rhr REAL, hrv_sdnn REAL,
                resp_rate REAL, wrist_temp REAL, spo2 REAL, sleep_hours REAL
            );
            CREATE TABLE illness_episodes (
                id INTEGER PRIMARY KEY, start TEXT, end TEXT, level TEXT,
                source TEXT, type TEXT DEFAULT 'unknown', notes TEXT
            );
            """
        )
        first = date_cls.fromisoformat("2024-01-01")
        for index in range(days):
            day = (first + timedelta(days=index)).isoformat()
            conn.execute(
                "INSERT INTO aw_daily VALUES (?, ?, ?, ?, ?, ?, ?)",
                (day, 60.0 + (index % 2) * 2.0, 70.0 - (index % 2) * 2.0, 15.0,
                 35.5 + (index % 2) * 0.1, 96.0 - (index % 2) * 1.0,
                 7.5 - (index % 2) * 0.5),
            )
        conn.commit()
        return conn

    def test_no_table_returns_empty(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertEqual(digest.channels(conn), [])

    def test_flat_day_is_in_baseline(self):
        conn = self._db()
        self.addCleanup(conn.close)
        channels = {c.key: c for c in digest.channels(conn, "2024-01-30")}
        self.assertFalse(channels["rhr"].bad)
        self.assertFalse(channels["hrv_sdnn"].bad)
        self.assertEqual(channels["rhr"].value, 62.0)

    def test_rhr_spike_is_flagged(self):
        conn = self._db()
        self.addCleanup(conn.close)
        conn.execute("UPDATE aw_daily SET rhr = 90.0 WHERE date = '2024-01-30'")
        conn.commit()
        channels = {c.key: c for c in digest.channels(conn, "2024-01-30")}
        self.assertTrue(channels["rhr"].bad)
        self.assertGreater(channels["rhr"].z, digest.Z_CHANNEL)

    def test_channel_serializes(self):
        conn = self._db()
        self.addCleanup(conn.close)
        channel = digest.channels(conn, "2024-01-30")[0]
        payload = channel.to_dict()
        self.assertIn("z", payload)
        self.assertIn("bad", payload)


class LoadStateTests(unittest.TestCase):
    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE load_daily (
                date TEXT PRIMARY KEY, acwr REAL, acwr_warmup REAL,
                monotony REAL, strain REAL, weekly_tonnage REAL,
                rm_regress INTEGER DEFAULT 0, gap_warmup INTEGER DEFAULT 0,
                since_gap INTEGER, sets INTEGER DEFAULT 0
            );
            """
        )
        conn.execute(
            "INSERT INTO load_daily VALUES ('2026-10-15', 1.0, 1.0, 1.2, 8000.0,"
            " 4000.0, 0, 0, 40, 20)")
        conn.execute(
            "INSERT INTO load_daily VALUES ('2026-10-16', 1.1, 1.1, 1.3, 9000.0,"
            " 5000.0, 0, 0, 41, 22)")
        conn.execute(
            "INSERT INTO load_daily VALUES ('2026-10-17', 0.0, 1.2, 1.4, 9000.0,"
            " 5000.0, 0, 0, 42, 0)")
        conn.commit()
        return conn

    def test_days_since_training_skips_rest_days(self):
        conn = self._db()
        self.addCleanup(conn.close)
        state = digest.load_state(conn, "2026-10-17")
        self.assertEqual(state["last_training"], "2026-10-16")
        self.assertEqual(state["days_since_training"], 1)
        self.assertEqual(state["date"], "2026-10-17")

    def test_empty_load_daily(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertEqual(digest.load_state(conn), {"available": False})


class DayStateTests(unittest.TestCase):
    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE aw_daily (
                date TEXT PRIMARY KEY, rhr REAL, hrv_sdnn REAL,
                resp_rate REAL, wrist_temp REAL, spo2 REAL, sleep_hours REAL
            );
            CREATE TABLE illness_episodes (
                id INTEGER PRIMARY KEY, start TEXT, end TEXT, level TEXT,
                source TEXT, type TEXT DEFAULT 'unknown', notes TEXT
            );
            """
        )
        first = date_cls.fromisoformat("2024-01-01")
        for index in range(30):
            day = (first + timedelta(days=index)).isoformat()
            conn.execute(
                "INSERT INTO aw_daily VALUES (?, ?, ?, ?, ?, ?, ?)",
                (day, 60.0, 70.0 - (index % 2) * 2.0, 15.0,
                 35.5 + (index % 2) * 0.1, 96.0 - (index % 2) * 1.0,
                 7.5 - (index % 2) * 0.5),
            )
        conn.commit()
        return conn

    def test_state_has_all_blocks(self):
        conn = self._db()
        self.addCleanup(conn.close)
        state = digest.day_state(conn, "2024-01-30")
        self.assertEqual(state["today"], "2024-01-30")
        self.assertEqual(len(state["channels"]), 6)
        self.assertIn("available", state["load"])
        self.assertIn("ready", state["gate"])
        self.assertIn("level", state["recommendation"])

    def test_format_digest_mentions_recommendation(self):
        conn = self._db()
        self.addCleanup(conn.close)
        text = digest.format_digest(digest.day_state(conn, "2024-01-30"))
        self.assertIn("дневной дайджест", text)
        self.assertIn("рекомендация", text)
        self.assertIn("RHR", text)

    def test_dumps_is_valid_json_and_nests_channels(self):
        conn = self._db()
        self.addCleanup(conn.close)
        payload = json.loads(digest.dumps(digest.day_state(conn, "2024-01-30")))
        self.assertEqual(payload["today"], "2024-01-30")
        self.assertIsInstance(payload["channels"][0], dict)
        self.assertIn("label", payload["channels"][0])

    # -- скор из ledger'а ---------------------------------------------------

    def _score_db(self) -> sqlite3.Connection:
        conn = self._db()
        conn.executescript(
            """
            CREATE TABLE ml_score (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL, as_of TEXT, computed_at TEXT NOT NULL,
                score REAL, spec_hash TEXT NOT NULL, n_train INTEGER,
                n_pos INTEGER, last_train_day TEXT, threshold REAL,
                alert INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO ml_score (day, as_of, computed_at, score, spec_hash, "
            "n_train, n_pos, last_train_day, threshold, alert) "
            "VALUES ('2024-01-30', '2024-01-30', '2024-01-30T20:00:00', 0.83, "
            "'spec00000001', 40, 6, '2024-01-29', 0.75, 1)",
        )
        conn.commit()
        return conn

    def test_day_state_reads_canonical_score_row(self):
        conn = self._score_db()
        self.addCleanup(conn.close)
        state = digest.day_state(conn, "2024-01-30")
        self.assertEqual(state["score"]["score"], 0.83)
        self.assertEqual(state["score"]["alert"], 1)
        self.assertEqual(state["score"]["threshold"], 0.75)
        # винт строки виден извне - без него не проверить смену спеки
        self.assertEqual(state["score"]["spec_hash"], "spec00000001")

    def test_format_digest_shows_gate_and_score(self):
        conn = self._score_db()
        self.addCleanup(conn.close)
        text = digest.format_digest(digest.day_state(conn, "2024-01-30"))
        self.assertIn("гейт возврата", text)
        self.assertIn("скор (причинный): 0.83", text)
        self.assertIn("выше порога 0.75", text)
    def test_card_falls_back_to_last_scored_day(self):
        """День эпизода модель не оценивает - показываем последний скор с датой."""
        conn = self._score_db()
        self.addCleanup(conn.close)
        state = digest.day_state(conn, "2024-02-01")   # строки за этот день нет
        self.assertEqual(state["score"]["day"], "2024-01-30")
        self.assertEqual(state["score"]["score"], 0.83)
        text = digest.format_digest(state)
        self.assertIn("скор (причинный за 2024-01-30): 0.83", text)

    def test_day_state_without_score_table_has_no_score(self):
        conn = self._db()
        self.addCleanup(conn.close)
        state = digest.day_state(conn, "2024-01-30")
        self.assertIsNone(state["score"])
        text = digest.format_digest(state)
        self.assertNotIn("скор (причинный", text)
        # гейт при этом всё равно показан: он не зависит от ML
        self.assertIn("гейт возврата", text)
        self.assertIsNone(state["spec_warning"])

    # -- гигиена spec_hash --------------------------------------------------

    def test_spec_warning_absent_when_single_vintage_matches(self):
        conn = self._score_db()
        self.addCleanup(conn.close)
        self.assertIsNone(digest.spec_warning(conn, current="spec00000001"))
        state = digest.day_state(conn, "2024-01-30", spec="spec00000001")
        self.assertIsNone(state["spec_warning"])
        self.assertNotIn("внимание:", digest.format_digest(state))

    def test_spec_warning_when_history_not_reseeded(self):
        """Рецептура сменилась, а история старого винта - карточка скажет.

        Контракт формулировки (N-2, вариант B): сообщение информационное -
        хэши спек, числа дней, причина отсутствия канона; совета
        «нужен полный replay» в нём нет.
        """
        conn = self._score_db()
        self.addCleanup(conn.close)
        state = digest.day_state(conn, "2024-01-30", spec="spec00000009")
        note = state["spec_warning"]
        self.assertIn("spec00000009", note)
        self.assertIn("spec00000001", note)
        self.assertIn("1 дн без канона", note)
        self.assertNotIn("нужен", note)
        self.assertNotIn("не пересеяна", note)
        self.assertIn("внимание:", digest.format_digest(state))
        # без текущей спеки канон - самая свежая запись ledger'а: смешать тут
        # нечего, и сообщать не о чем
        self.assertIsNone(digest.spec_warning(conn))

    def test_spec_warning_mixed_vintages_in_canonical_series(self):
        """Смесь винтов в ledger'е ловится и без текущей спеки."""
        conn = self._score_db()
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO ml_score (day, as_of, computed_at, score, spec_hash, "
            "n_train, n_pos, last_train_day, threshold, alert) "
            "VALUES ('2024-01-25', '2024-01-25', '2024-01-25T20:00:00', 0.4, "
            "'spec00000002', 40, 6, '2024-01-24', 0.75, 0)")
        conn.commit()
        note = digest.spec_warning(conn)
        self.assertIn("spec00000001", note)
        self.assertIn("spec00000002", note)
        self.assertIn("винтаж", note)
        self.assertIn("1 дн без канона", note)

    def test_spec_warning_mixed_vintages_wins_over_current(self):
        """При смешанных винтах названы все рецептуры, а не только текущая."""
        conn = self._score_db()
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO ml_score (day, as_of, computed_at, score, spec_hash, "
            "n_train, n_pos, last_train_day, threshold, alert) "
            "VALUES ('2024-01-25', '2024-01-25', '2024-01-25T20:00:00', 0.4, "
            "'spec00000002', 40, 6, '2024-01-24', 0.75, 0)")
        conn.commit()
        note = digest.spec_warning(conn, current="spec00000009")
        self.assertIn("spec00000001", note)
        self.assertIn("spec00000002", note)
        self.assertIn("spec00000009", note)
        self.assertIn("2 дн без канона", note)


class GateBandTests(unittest.TestCase):
    """Полоса гейта — числовая строка-пояснение, render-only (4.6).

    Полоса читает готовый ``gate.to_dict()``: новой логики гейта нет. Проверяем,
    что в строке только числа (порог гейта ±1 против колонки ±1.5), нет стрелок и
    что она честно различает «готов / держит / окно тишины».
    """

    def test_band_shows_both_thresholds(self):
        gate = {"ready": True, "nights": 2, "reasons": [], "rows": [],
                "silence_until": None}
        text = digest._gate_band(gate)
        self.assertIn("±1", text)
        self.assertIn("±1.5", text)
        self.assertIn("ночей 2", text)
        self.assertIn("готов", text)

    def test_band_says_gate_holds(self):
        gate = {"ready": False, "nights": 2,
                "reasons": ("2026-10-17: RHR z +1.2 выше фона (>1)",),
                "rows": [], "silence_until": None}
        self.assertIn("держит", digest._gate_band(gate))

    def test_band_marks_silence_window_as_ramp(self):
        gate = {"ready": False, "nights": 2,
                "reasons": ("окно тишины: до 2026-10-27",),
                "rows": [], "silence_until": "2026-10-27"}
        text = digest._gate_band(gate)
        self.assertIn("окно тишины", text)
        self.assertIn("рампа", text)

    def test_band_has_no_arrows_or_marks(self):
        """Урок E4: в рендере — числа, без стрелок/цветовых меток."""
        gate = {"ready": False, "nights": 2,
                "reasons": ("2026-10-17: ВСР z -1.4 ниже фона (<-1)",),
                "rows": [], "silence_until": None}
        text = digest._gate_band(gate)
        for mark in ("→", "←", "↑", "↓", "◀", "▶"):
            self.assertNotIn(mark, text)

    def test_band_empty_without_nights(self):
        self.assertEqual(digest._gate_band({}), "")
        self.assertEqual(
            digest._gate_band({"ready": True, "nights": 0, "reasons": [],
                               "rows": [], "silence_until": None}), "")


if __name__ == "__main__":
    unittest.main()
