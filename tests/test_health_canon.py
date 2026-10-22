"""Канон скора = текущая спека; винтаж читаем, но в канон и порог не входит.

Решение N-2, вариант B (владелец, 2026-10-22). Что здесь прибито:

- **канон** - строки ledger'а под *текущей* рецептурой. Строка, записанная по
  другой спецификации (винт), невоспроизводима (счёт по другому рецепту) и в
  канон не входит: даже если записана позже канонической;
- **карточка** дня показывает число канона (с его датой), а не число винтажа.
  Отсюда главное: шесть дней со скором около 1.00 и ``alert = 1``, жившие в
  винте, из канона исчезают - это тот самый FP-артефакт, который вариант B
  снимает;
- **порог** считается по канону: винтажные строки в него не входят;
- **предупреждение о составе** информационное: хэши спек, числа дней и
  причина отсутствия канона. Совета «нужен полный replay» в нём нет - для
  хвостов эпизодов он невыполним (их не оценивает ни одна рецептура);
- **вывод** ledger'а помечает винтажные строки, чтобы они не читались как
  канонические.

Синтетика - чистый SQL (без numpy); свойства по живой базе - как в W7-(g):
свойства, а не константы.
"""

from __future__ import annotations

import re
import sqlite3
import unittest

from koilog.etl import DEFAULT_DB
from koilog.health import digest, score

#: Текущая рецептура и «винт» (другая спецификация) в синтетике.
CURRENT = "spec0000000a"
VINTAGE = "spec0000000b"

_SCHEMA = """
CREATE TABLE ml_score (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    day            TEXT NOT NULL,
    as_of          TEXT,
    computed_at    TEXT NOT NULL,
    score          REAL,
    spec_hash      TEXT NOT NULL,
    n_train        INTEGER,
    n_pos          INTEGER,
    last_train_day TEXT,
    threshold      REAL,
    alert          INTEGER
);
CREATE TABLE ml_features (
    date    TEXT PRIMARY KEY,
    at_risk INTEGER NOT NULL DEFAULT 1
);
"""


def _ledger(rows, features=()) -> sqlite3.Connection:
    """Маленький ledger: (день, спека, скор, порог, alert) на строку."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    for day, spec, value, thr, alert in rows:
        conn.execute(
            "INSERT INTO ml_score (day, as_of, computed_at, score, spec_hash, "
            "n_train, n_pos, last_train_day, threshold, alert) "
            "VALUES (?, ?, '2024-06-01T10:00:00', ?, ?, 100, 5, '2024-05-31', "
            "?, ?)",
            (day, day, value, spec, thr, alert),
        )
    for day, at_risk in features:
        conn.execute("INSERT INTO ml_features (date, at_risk) VALUES (?, ?)",
                     (day, at_risk))
    conn.commit()
    return conn


class CanonTests(unittest.TestCase):
    """Канон = текущая спека; винтаж в канон и порог не входит."""

    def test_card_reads_current_spec_row_even_when_vintage_written_later(self):
        """Винт, записанный позже канона, канон не вытесняет."""
        conn = _ledger([
            ("2024-05-10", CURRENT, 0.20, 0.80, 0),
            ("2024-05-10", VINTAGE, 0.99, 0.90, 1),
        ])
        self.addCleanup(conn.close)
        state = digest.day_state(conn, "2024-05-10", spec=CURRENT)
        self.assertEqual(state["score"]["score"], 0.20)
        self.assertEqual(state["score"]["alert"], 0)
        self.assertEqual(state["score"]["spec_hash"], CURRENT)
        text = digest.format_digest(state)
        self.assertIn("скор (причинный): 0.20", text)
        self.assertNotIn("0.99", text)

    def test_day_without_canonical_row_falls_back_to_nearest_canonical_day(self):
        """День только с винтом показывает ближайший каноничный день с датой."""
        conn = _ledger([
            ("2024-05-09", CURRENT, 0.30, 0.80, 0),
            ("2024-05-10", VINTAGE, 0.99, 0.90, 1),
        ])
        self.addCleanup(conn.close)
        state = digest.day_state(conn, "2024-05-10", spec=CURRENT)
        self.assertEqual(state["score"]["day"], "2024-05-09")
        self.assertEqual(state["score"]["score"], 0.30)
        self.assertEqual(state["score"]["alert"], 0)
        text = digest.format_digest(state)
        self.assertIn("скор (причинный за 2024-05-09): 0.30", text)
        self.assertNotIn("0.99", text)

    def test_vintage_alert_never_reaches_canonical_context(self):
        """Тест-пин владельца: алерт винта не появляется в каноническом виде.

        Единственная строка с ``alert = 1`` - винт. Ни карточка дня, ни канон
        ledger'а, ни порог не должны её увидеть.
        """
        conn = _ledger([
            ("2024-05-09", CURRENT, 0.30, 0.80, 0),
            ("2024-05-10", VINTAGE, 0.999, 0.90, 1),
        ])
        self.addCleanup(conn.close)
        state = digest.day_state(conn, "2024-05-10", spec=CURRENT)
        self.assertNotEqual(state["score"]["score"], 0.999)
        self.assertNotEqual(state["score"]["alert"], 1)
        self.assertEqual(state["score"]["spec_hash"], CURRENT)
        canon = score.rows(conn, spec=CURRENT, current=CURRENT)
        self.assertTrue(canon)
        for item in canon:
            self.assertEqual(item["spec_hash"], CURRENT)
            self.assertFalse(item["vintage"])
        self.assertNotIn("алерт", digest.format_digest(state))

    def test_canonical_row_is_current_spec_for_every_day(self):
        """Канон дня - строка текущей спеки; чужая спека в канон не попадает."""
        conn = _ledger([
            ("2024-05-08", CURRENT, 0.10, 0.80, 0),
            ("2024-05-08", VINTAGE, 0.90, 0.80, 0),
            ("2024-05-09", CURRENT, 0.20, 0.80, 0),
            ("2024-05-10", VINTAGE, 0.95, 0.80, 0),
        ])
        self.addCleanup(conn.close)
        for day in ("2024-05-08", "2024-05-09", "2024-05-10"):
            row = digest._score_row(conn, day, CURRENT)
            if row is not None:
                self.assertEqual(row["spec_hash"], CURRENT)

    def test_threshold_ignores_vintage_rows(self):
        """Порог под бюджет считается по канону; винтаж его не двигает."""
        v2_scores = [0.10 + 0.01 * index for index in range(40)]
        rows = []
        for index, value in enumerate(v2_scores):
            rows.append((f"2024-06-{index + 1:02d}", CURRENT, value, 0.80, 0))
        for index in range(40):
            rows.append((f"2024-06-{index + 1:02d}", VINTAGE, 0.99, 0.80, 1))
        conn = _ledger(rows)
        self.addCleanup(conn.close)
        thr = score.threshold(conn, CURRENT, "2024-12-31")
        expected = score._threshold_value(v2_scores, score.DEFAULT_BUDGET)
        self.assertIsNotNone(thr)
        self.assertAlmostEqual(thr, expected)
        self.assertLess(thr, 0.5)      # винтажные 0.99 в порог не попали


class WarningTests(unittest.TestCase):
    """Предупреждение о составе ledger'а: факты, причина, без советов."""

    def _mixed(self, features=()) -> sqlite3.Connection:
        return _ledger([
            ("2024-05-09", CURRENT, 0.30, 0.80, 0),
            ("2024-05-10", VINTAGE, 0.99, 0.90, 1),
        ], features=features)

    def test_warning_states_facts_and_forbids_replay_advice(self):
        conn = self._mixed()
        self.addCleanup(conn.close)
        note = digest.spec_warning(conn, CURRENT)
        self.assertIsNotNone(note)
        self.assertIn(CURRENT, note)
        self.assertIn(VINTAGE, note)
        self.assertIn("1 дн без канона", note)
        self.assertNotIn("нужен", note)
        self.assertNotIn("полный replay", note)
        self.assertNotIn("не пересеяна", note)

    def test_warning_reason_names_episode_tails_when_all_orphans_are_at_risk_zero(self):
        conn = self._mixed(features=[("2024-05-10", 0)])
        self.addCleanup(conn.close)
        note = digest.spec_warning(conn, CURRENT)
        self.assertIn("at_risk", note)
        self.assertIn("replay неприменим", note)

    def test_warning_reason_is_generic_when_orphan_is_not_episode_tail(self):
        conn = self._mixed(features=[("2024-05-10", 1)])
        self.addCleanup(conn.close)
        note = digest.spec_warning(conn, CURRENT)
        self.assertIn("дни вне текущего рецепта", note)
        self.assertNotIn("replay неприменим", note)

    def test_warning_is_static_and_carries_no_dates(self):
        """Один и тот же состав - один и тот же текст (без «сейчас» и дат)."""
        conn = self._mixed(features=[("2024-05-10", 0)])
        self.addCleanup(conn.close)
        first = digest.spec_warning(conn, CURRENT)
        second = digest.spec_warning(conn, CURRENT)
        self.assertEqual(first, second)
        self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}", first))

    def test_warning_absent_when_single_spec_matches_current(self):
        conn = _ledger([("2024-05-10", CURRENT, 0.30, 0.80, 0)])
        self.addCleanup(conn.close)
        self.assertIsNone(digest.spec_warning(conn, CURRENT))

    def test_warning_names_current_spec_even_when_history_older(self):
        """История по другой рецептуре: канон пуст, винтаж назван прямо."""
        conn = _ledger([("2024-05-10", VINTAGE, 0.30, 0.80, 0)])
        self.addCleanup(conn.close)
        note = digest.spec_warning(conn, CURRENT)
        self.assertIn(CURRENT, note)
        self.assertIn(VINTAGE, note)
        self.assertNotIn("нужен", note)


class RowsOutputTests(unittest.TestCase):
    """Вывод ledger'а: винтаж помечен, но виден."""

    def _mixed(self) -> sqlite3.Connection:
        return _ledger([
            ("2024-05-09", CURRENT, 0.30, 0.80, 0),
            ("2024-05-10", VINTAGE, 0.99, 0.90, 1),
        ])

    def test_rows_mark_vintage_by_current_spec(self):
        conn = self._mixed()
        self.addCleanup(conn.close)
        items = score.rows(conn, current=CURRENT)
        marks = {(item["day"], item["vintage"]) for item in items}
        self.assertIn(("2024-05-09", False), marks)
        self.assertIn(("2024-05-10", True), marks)

    def test_format_scores_shows_vintage_column_and_footnote(self):
        conn = self._mixed()
        self.addCleanup(conn.close)
        items = score.rows(conn, current=CURRENT)
        text = score.format_scores(items)
        self.assertIn("винтаж", text)
        self.assertIn("в канон и порог не входит", text)

    def test_format_scores_without_vintage_has_no_footnote(self):
        conn = _ledger([("2024-05-09", CURRENT, 0.30, 0.80, 0)])
        self.addCleanup(conn.close)
        items = score.rows(conn, current=CURRENT)
        text = score.format_scores(items)
        self.assertIn("винтаж", text)          # заголовок колонки
        self.assertNotIn("в канон и порог не входит", text)


class LiveCanonTests(unittest.TestCase):
    """Свойства живой базы (как в W7-(g): свойства, а не константы)."""

    @classmethod
    def setUpClass(cls):
        path = DEFAULT_DB
        if not path.exists():
            raise unittest.SkipTest(f"нет живой базы: {path}")
        cls.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        cls.conn.row_factory = sqlite3.Row
        cls.current = score.spec_hash()

    @classmethod
    def tearDownClass(cls):
        conn = getattr(cls, "conn", None)
        if conn is not None:
            conn.close()

    def _orphan_days(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT day FROM ml_score "
            "EXCEPT SELECT DISTINCT day FROM ml_score WHERE spec_hash = ?",
            (self.current,)).fetchall()
        return [row["day"] for row in rows]

    def test_canonical_series_has_single_spec(self):
        """Канон ledger'а - строки одной (текущей) спеки."""
        items = score.rows(self.conn, spec=self.current)
        self.assertTrue(items)
        for item in items:
            self.assertEqual(item["spec_hash"], self.current)
            self.assertFalse(item["vintage"])

    def test_ledger_days_split_into_canonical_and_orphans(self):
        """Дни ledger'а = канон + дни без канона (без дыр и пересечений)."""
        total = self.conn.execute(
            "SELECT COUNT(DISTINCT day) FROM ml_score").fetchone()[0]
        canon = self.conn.execute(
            "SELECT COUNT(DISTINCT day) FROM ml_score WHERE spec_hash = ?",
            (self.current,)).fetchone()[0]
        orphans = self._orphan_days()
        self.assertEqual(total, canon + len(orphans))

    def test_orphan_days_are_episode_tails(self):
        """Дни без канона - дни вне оценки (``at_risk = 0``)."""
        orphans = self._orphan_days()
        if not orphans:
            self.skipTest("в живой базе нет дней без канона")
        for day in orphans:
            row = self.conn.execute(
                "SELECT at_risk FROM ml_features WHERE date = ?", (day,)).fetchone()
            self.assertIsNotNone(row, f"{day} нет в ml_features")
            self.assertEqual(row["at_risk"], 0, day)

    def test_card_never_shows_vintage_number(self):
        """Ни в один день карточка не показывает число винтажа."""
        for day in self._orphan_days():
            row = digest._score_row(self.conn, day, self.current)
            if row is None:
                continue
            self.assertEqual(row["spec_hash"], self.current, day)

    def test_vintage_alerts_do_not_reach_canonical_series(self):
        """Алерты винта не попадают в канонический ряд."""
        for item in score.rows(self.conn, spec=self.current):
            self.assertEqual(item["spec_hash"], self.current)


if __name__ == "__main__":
    unittest.main()
