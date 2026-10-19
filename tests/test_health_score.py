"""Тесты ledger'а дневного скора: ``koilog.health.score``.

Главное здесь - не арифметика порога, а **семантика записи**, и она теперь
другая, чем в первой версии модуля:

- ledger **append-only**: ``record`` всегда добавляет строку, каноническое
  значение дня - последняя; «обновления» как операции нет;
- скорер пишет **только последний день** данных, историю засевает ``replay``;
- скор **причинный**: метка дня, чей исход к eval-дню ещё неизвестен,
  исключается, а не обнуляется, - поэтому повторный прогон не меняет число
  (в отличие от оракульного ``walk_probs``);
- порог берётся из канонической истории ledger'а, а не из пересчёта.

Как и остальные ML-тесты, работают на маленькой синтетической базе в памяти и
пропускаются без ``numpy``/``scikit-learn``.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import date as date_cls
from datetime import timedelta

from koilog.health import apple, illness, load, ml, score

#: Два эпизода: до и в середине ряда. В ``illness.zscore`` фон копится с
#: нуля, поэтому первый onset ставится не в первый же день.
_EPISODES = (
    (1, "2024-01-21"),
    (2, "2024-02-26"),
)


def _calendar(start: str, count: int) -> list[str]:
    first = date_cls.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


def _synthetic_db(days: int = 80) -> sqlite3.Connection:
    """Та же синтетика, что в тестах ``ml``: фон + «болезнь» на 55-60 дне."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(apple._SCHEMA)
    conn.executescript(load._SCHEMA)
    conn.executescript(illness._SCHEMA)
    conn.executescript(ml._FEATURE_SCHEMA)

    for index, day in enumerate(_calendar("2024-01-01", days)):
        sick = 55 <= index <= 60
        rhr = 60 + (index % 4) * 0.4 + (15 if sick else 0)
        hrv = 70 + (index % 3) * 0.5 - (35 if sick else 0)
        resp = 15 + (index % 3) * 0.1 + (5 if sick else 0)
        wrist = 35.5 + (index % 2) * 0.1 + (1.0 if sick else 0)
        sleep = 7.5 - (index % 3) * 0.1 - (3 if sick else 0)
        conn.execute(
            "INSERT INTO aw_daily (date, rhr, hrv_sdnn, resp_rate, wrist_temp, "
            "spo2, sleep_hours, sleep_start) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (day, rhr, hrv, resp, wrist, 96.0, sleep, "23:30"),
        )
        trained = index % 2 == 0
        conn.execute(
            "INSERT INTO health_days (date, trained, aw_coverage, rhr, hrv_sdnn, "
            "sleep_hours, in_window) VALUES (?, ?, 1, ?, ?, ?, 1)",
            (day, 1 if trained else 0, rhr, hrv, sleep),
        )
        conn.execute(
            "INSERT INTO load_daily (date, sets, tonnage, weekly_tonnage, srpe, "
            "acwr, acwr_ewma, monotony, strain, rm_regress) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (day, 20 if trained else 0, 900.0 if trained else 0.0,
             6000.0, 300.0, 1.2, 1.1, 1.5, 9000.0, 0),
        )
    for episode_id, start in _EPISODES:
        conn.execute(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (?, ?, ?, 'confirmed', 'watch', 'unknown')",
            (episode_id, start, start),
        )
    conn.commit()
    return conn


def _prepared(days: int = 80) -> sqlite3.Connection:
    """База с признаками и метками: то, что нужно ledger'у."""
    conn = _synthetic_db(days)
    ml.build_features(conn, window=None)
    ml.build_labels(conn, window=None)
    return conn


def _truncate(conn: sqlite3.Connection, day: str) -> None:
    """Обрезать базу по ``day`` и пересобрать ML (симуляция «сегодня = day»)."""
    for table in ("aw_daily", "health_days", "load_daily"):
        conn.execute(f"DELETE FROM {table} WHERE date > ?", (day,))
    conn.execute("DELETE FROM illness_episodes WHERE start > ?", (day,))
    conn.commit()
    ml.build_features(conn, window=None)
    ml.build_labels(conn, window=None)


def _add_episode(conn: sqlite3.Connection, episode_id: int, start: str) -> None:
    conn.execute(
        "INSERT INTO illness_episodes (id, start, end, level, source, type) "
        "VALUES (?, ?, ?, 'confirmed', 'watch', 'unknown')",
        (episode_id, start, start))
    conn.commit()


@unittest.skipUnless(ml.HAS_ML, "нет numpy/scikit-learn (koilog[ml])")
class WalkProbsTests(unittest.TestCase):
    def test_walk_probs_gives_date_for_every_day(self):
        conn = _prepared()
        dates, probs = ml.walk_probs(conn, ml.V2_FEATURES)
        self.assertEqual(len(dates), len(probs))
        self.assertEqual(dates, sorted(dates))
        # первые дни не оцениваются: истории ещё нет
        self.assertIsNone(probs[0])

    def test_walk_probs_agrees_with_walk_forward(self):
        """Скор дня и честная оценка считаются одним циклом, а не двумя."""
        conn = _prepared()
        _, probs = ml.walk_probs(conn, ml.V2_FEATURES)
        result = ml.walk_forward(conn, ml.V2_FEATURES, model="logreg",
                                 budgets=(2.0,), random_repeats=3,
                                 auc_bootstrap=10)
        self.assertEqual(sum(1 for prob in probs if prob is not None),
                         result["evaluated"])


class CausalTrainTests(unittest.TestCase):
    """Правило меток - чистая функция, проверяется без numpy."""

    def test_open_window_is_excluded_not_zeroed(self):
        """День перед будущим onset'ом не должен стать ложным негативом."""
        dates = _calendar("2024-01-01", 12)
        at_risk = [True] * 12
        starts = ["2024-01-20"]                     # onset сильно позже
        index, labels = ml._causal_train(dates, at_risk, starts, t=9)
        # дни 0..2: окно закрылось к eval-дню (01-10) → 0
        # дни 3..8: окно открыто, исход неизвестен → исключены (а не 0)
        self.assertEqual(index, [0, 1, 2])
        self.assertEqual(labels, [0.0, 0.0, 0.0])

    def test_known_onset_in_window_is_positive(self):
        dates = _calendar("2024-01-01", 12)
        at_risk = [True] * 12
        starts = ["2024-01-05"]
        index, labels = ml._causal_train(dates, at_risk, starts, t=8)
        # окно дней 0..3 содержит уже случившийся 01-05 → 1
        self.assertEqual(index, [0, 1, 2, 3])
        self.assertEqual(labels, [1.0, 1.0, 1.0, 1.0])

    def test_days_inside_episode_are_not_train(self):
        dates = _calendar("2024-01-01", 12)
        at_risk = [True] * 12
        at_risk[4] = False
        index, _ = ml._causal_train(dates, at_risk, ["2024-01-20"], t=9)
        self.assertNotIn(4, index)


@unittest.skipUnless(ml.HAS_ML, "нет numpy/scikit-learn (koilog[ml])")
class CausalScoreTests(unittest.TestCase):
    def test_causal_score_reproducible_after_truncation(self):
        """Обрезка базы по дню не меняет причинный скор этого дня."""
        conn = _prepared()
        full = ml.causal_score(conn, "2024-03-05")
        self.assertTrue(full["scored"])
        _truncate(conn, "2024-03-05")
        cut = ml.causal_score(conn, "2024-03-05")
        self.assertEqual(full["score"], cut["score"])
        self.assertEqual(full["n_train"], cut["n_train"])
        self.assertEqual(full["n_pos"], cut["n_pos"])

    def test_future_onset_changes_oracle_but_not_causal(self):
        """Новый эпизод переписывает ретро-метки, но не живой скор."""
        conn = _prepared()
        day = "2024-03-10"
        dates, probs_before = ml.walk_probs(conn, ml.V2_FEATURES)
        position = dates.index(day)
        causal_before = ml.causal_score(conn, day)
        y_before = conn.execute(
            "SELECT y_onset FROM ml_labels WHERE date = ?", (day,)).fetchone()[0]

        _add_episode(conn, 3, "2024-03-12")     # на 2 дня позже eval-дня
        ml.build_labels(conn, window=None)

        y_after = conn.execute(
            "SELECT y_onset FROM ml_labels WHERE date = ?", (day,)).fetchone()[0]
        self.assertNotEqual(y_before, y_after, "метка не перевернулась - тест слеп")
        _, probs_after = ml.walk_probs(conn, ml.V2_FEATURES)
        self.assertTrue(
            any(probs_before[j] != probs_after[j]
                for j in range(position - 6, position + 1)),
            "оракульный скор обязан поехать")
        self.assertEqual(causal_before["score"], ml.causal_score(conn, day)["score"])

    def test_causal_scores_covers_all_days(self):
        conn = _prepared()
        dates, infos = ml.causal_scores(conn, ml.V2_FEATURES)
        self.assertEqual(len(dates), len(infos))
        self.assertTrue(any(info and info.get("scored") for info in infos))


@unittest.skipUnless(ml.HAS_ML, "нет numpy/scikit-learn (koilog[ml])")
class AuditTests(unittest.TestCase):
    def test_audit_carries_gate_and_recommendation(self):
        conn = _prepared()
        day = score.audit(conn, "2024-03-10")
        self.assertTrue(day["available"])
        self.assertEqual(day["day"], "2024-03-10")
        self.assertIn("score", day)
        self.assertIn("spec_hash", day)
        self.assertIn("ready", day["gate"])
        self.assertEqual(set(day["recommendation"]),
                         {"level", "title", "text"})

    def test_audit_day_inside_episode_is_not_scored(self):
        """В день эпизода модель дня не оценивает - и это видно, а не «0»."""
        conn = _prepared()
        day = score.audit(conn, _EPISODES[1][1])
        self.assertTrue(day["available"])
        self.assertFalse(day["scored"])
        self.assertIsNone(day["score"])
        self.assertEqual(day["reason"], "day_inside_episode")
        # в карточке - человеческая причина, а не код
        self.assertIn("день внутри эпизода", score.format_day(day))
        self.assertEqual(score.reason_text("day_inside_episode"),
                         "день внутри эпизода")
        self.assertEqual(score.reason_text(None), "день не оценён")

    def test_format_day_is_one_line(self):
        conn = _prepared()
        text = score.format_day(score.audit(conn, "2024-03-10"))
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("гейт", text)

    def test_format_day_survives_empty_base(self):
        conn = _synthetic_db(days=3)
        text = score.format_day(score.audit(conn, None))
        self.assertIn("скор", text)


@unittest.skipUnless(ml.HAS_ML, "нет numpy/scikit-learn (koilog[ml])")
class LedgerTests(unittest.TestCase):
    def test_record_appends_and_writes_only_last_day(self):
        conn = _prepared()
        first = score.record(conn)
        self.assertTrue(first["written"])
        day = first["day"]
        self.assertEqual(conn.execute(
            "SELECT COUNT(DISTINCT day) FROM ml_score").fetchone()[0], 1)

        second = score.record(conn)          # append-only: ещё одна строка
        self.assertTrue(second["written"])
        self.assertEqual(second["day"], day)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM ml_score WHERE day = ?", (day,)).fetchone()[0],
            2)
        items = score.rows(conn)             # каноническая - одна на день
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["score"], second["score"])

    def test_record_skips_day_inside_episode(self):
        conn = _prepared()
        last = conn.execute("SELECT MAX(date) FROM ml_features").fetchone()[0]
        _add_episode(conn, 3, last)
        ml.build_labels(conn, window=None)
        result = score.record(conn)
        self.assertFalse(result["written"])
        self.assertEqual(result["reason"], "day_inside_episode")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ml_score")
                         .fetchone()[0], 0)

    def test_replay_seeds_full_history_and_is_idempotent(self):
        conn = _prepared()
        first = score.replay(conn)
        self.assertGreater(first["written"], ml.MIN_HISTORY)
        self.assertEqual(first["skipped"], 0)
        days = conn.execute(
            "SELECT COUNT(DISTINCT day), COUNT(*) FROM ml_score").fetchone()
        self.assertEqual(days[0], days[1])   # ровно одна строка на день

        again = score.replay(conn)
        self.assertEqual(again["written"], 0)
        self.assertGreater(again["skipped"], ml.MIN_HISTORY)

    def test_sync_seeds_then_records(self):
        conn = _prepared()
        first = score.sync(conn)
        self.assertTrue(first["seeded"])
        self.assertGreater(first["written"], ml.MIN_HISTORY)
        second = score.sync(conn)
        self.assertFalse(second["seeded"])

    def test_threshold_is_quantile_of_canonical_history(self):
        conn = _prepared()
        score.replay(conn)
        spec = score.spec_hash()
        day = "2024-03-10"
        history = [row["score"] for row in score.rows(conn)
                   if row["day"] < day and row["score"] is not None]
        expected = ml._quantile(history, 1.0 - min(0.5, 2.0 / 30.44))
        self.assertAlmostEqual(score.threshold(conn, spec, day), expected)

    def test_threshold_needs_history_and_matching_spec(self):
        conn = _prepared()
        spec = score.spec_hash()
        self.assertIsNone(score.threshold(conn, spec, "2024-03-10"))
        score.replay(conn)
        self.assertIsNotNone(score.threshold(conn, spec, "2024-03-10"))
        self.assertIsNone(score.threshold(conn, "deadbeef", "2024-03-10"))

    def test_prospective_uses_computed_at(self):
        conn = _prepared()
        score.replay(conn)
        # сев записан «сейчас», позже всех исходов - проспективных нет
        self.assertEqual(score.prospective(conn), [])
        conn.execute("UPDATE ml_score SET computed_at = '2024-02-01T09:00:00' "
                     "WHERE day = '2024-02-10'")
        conn.commit()
        self.assertIn("2024-02-10", score.prospective(conn))

    def test_rows_mark_prospective_and_format_lists_days(self):
        conn = _prepared()
        score.replay(conn)
        items = score.rows(conn, limit=5)
        self.assertTrue(items)
        self.assertLessEqual(len(items), 5)
        self.assertIn("prospective", items[0])
        text = score.format_scores(items)
        self.assertIn(items[-1]["day"], text)
        self.assertIn("скор", text)

    def test_rows_and_prospective_survive_missing_table(self):
        conn = _synthetic_db(days=3)
        self.assertEqual(score.rows(conn), [])
        self.assertEqual(score.prospective(conn), [])
        self.assertIn("ledger пуст", score.format_scores([]))
        self.assertEqual(score.format_scores([], empty="нет дней"), "нет дней")

    def test_legacy_schema_is_replaced(self):
        """Дорелизная схема (``date`` PK, без ``spec_hash``) сносится."""
        conn = _prepared()
        conn.execute("CREATE TABLE ml_score (date TEXT PRIMARY KEY, prob REAL)")
        conn.commit()
        score.record(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ml_score)")}
        self.assertIn("spec_hash", columns)
        self.assertNotIn("date", columns)
        self.assertIn("day", columns)

    def test_record_without_numpy_raises_with_hint(self):
        if ml.HAS_ML:
            self.skipTest("sklearn есть: подсказка про extra ml не проверится")
        conn = _prepared()
        with self.assertRaises(RuntimeError):
            score.record(conn)


if __name__ == "__main__":
    unittest.main()
