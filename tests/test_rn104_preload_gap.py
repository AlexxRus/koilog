"""RN-104: нагрузка перед пропуском больше двух недель.

Пререгистрация - ``docs/rn-104-prereg.md`` (§4 Р1–Р5, §6 план тестов).
Проверок десять, по одной на пункт §6:

1. границы прогона включительные, возврат - первый день с нагрузкой;
2. простой ровно 14 дней **не** пропуск, 20 дней - пропуск (Р2-b=A);
3. нагрузка за 7 и 14 дней до простоя + дни с нагрузкой (Р3=A);
4. ряд - полная история ``_session_rows``, а не окно ``load_daily`` (Р2-a=B);
5. без пропусков - честная строка, не молчание (Р5=A);
6. перцентиль только при ``MIN_GAPS`` завершённых пропусков (Р4=C);
7. идущий простой не выдаётся за завершённый (Р5=A);
8. поверхность ``return`` не подключена к новой функции (пин §5.5);
9. семантика E после RM-98 не пересчитывается (пин §5.4);
10. прогон ничего не пишет в базу (display-only, Р1=A).
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import unittest
from datetime import date as date_cls
from datetime import timedelta
from pathlib import Path

from koilog import cli
from koilog.health import digest
from koilog.health import load as health_load
from koilog.health import retrain as health_retrain


_ROOT = Path(__file__).resolve().parent.parent
_LIVE = _ROOT / "sql" / "training.db"


def _live() -> sqlite3.Connection | None:
    """Живая база только на чтение; ``None``, если её нет или она пуста."""
    if not _LIVE.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{_LIVE}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sets'"
        ).fetchone()
        if not found or not conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]:
            conn.close()
            return None
    except sqlite3.Error:
        return None
    return conn


def _db() -> sqlite3.Connection:
    """Пустая in-memory база со схемой ``sets`` под ``_session_rows``."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sets (
            exercise      TEXT,
            date          TEXT,
            weight        REAL,
            reps          INTEGER,
            koi           REAL,
            rm_session    REAL,
            time_seconds  INTEGER,
            is_warmup     INTEGER NOT NULL DEFAULT 0,
            skipped       INTEGER NOT NULL DEFAULT 0,
            is_outlier    INTEGER NOT NULL DEFAULT 0,
            is_gravitron  INTEGER NOT NULL DEFAULT 0,
            is_foreign    INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    return conn


def _add(conn: sqlite3.Connection, day: str, weight: float = 100.0,
         reps: int = 10, exercise: str = "Жим штанги лежа") -> None:
    """Один подход: тоннаж = ``weight * reps``; e1RM = ``weight`` (одна сессия)."""
    conn.execute(
        "INSERT INTO sets (exercise, date, weight, reps, koi, rm_session, "
        "time_seconds, is_warmup, skipped, is_outlier, is_gravitron, is_foreign) "
        "VALUES (?, ?, ?, ?, 0, ?, NULL, 0, 0, 0, 0, 0)",
        (exercise, day, weight, reps, weight),
    )


def _block(conn: sqlite3.Connection, start: str, days: int,
           weight: float = 100.0, reps: int = 10) -> None:
    """Непрерывный блок тренировок с ``start`` длиной ``days`` дней."""
    first = date_cls.fromisoformat(start)
    for offset in range(days):
        _add(conn, (first + timedelta(days=offset)).isoformat(), weight, reps)


def _fixture() -> sqlite3.Connection:
    """14 дней подряд, простой 20 дней, тренировка, простой ровно 14 дней.

    Первый прогон - 2024-01-03…2024-01-22 (20 дней, пропуск), второй -
    2024-01-24…2024-02-06 (ровно 14 дней, **не** пропуск при пороге > 14).
    """
    conn = _db()
    _block(conn, "2023-12-20", 14)
    _add(conn, "2024-01-23")
    _add(conn, "2024-02-07")
    conn.commit()
    return conn


def _multi(gaps: int, block: int = 10, gap: int = 20) -> sqlite3.Connection:
    """``gaps`` завершённых пропусков по 20 дней между блоками тренировок."""
    conn = _db()
    cursor = date_cls.fromisoformat("2024-01-01")
    for _ in range(gaps + 1):
        for offset in range(block):
            _add(conn, (cursor + timedelta(days=offset)).isoformat())
        cursor += timedelta(days=block + gap)
    conn.commit()
    return conn


class GapLoadTests(unittest.TestCase):
    """Десять пунктов §6 на синтетике (фикстура - in-memory ``sets``)."""

    def setUp(self):
        self.conn = _db()
        self.addCleanup(self.conn.close)

    # 1 -------------------------------------------------------------------
    def test_gap_boundaries_inclusive(self):
        """Прогон - (первый, последний) нулевой день; возврат - день с нагрузкой."""
        conn = _fixture()
        self.addCleanup(conn.close)
        data = health_load.gap_load(conn, "2024-01-23")
        self.assertEqual(data["status"], "completed")
        gap = data["gap"]
        self.assertEqual(gap["start"], "2024-01-03")
        self.assertEqual(gap["end"], "2024-01-22")
        self.assertEqual(gap["days"], 20)
        # возврат - первый день с нагрузкой после прогона, он же `date`
        self.assertEqual(data["date"], "2024-01-23")

    # 2 -------------------------------------------------------------------
    def test_gap_strictly_longer_than_two_weeks(self):
        """20-дневный прогон найден, ровно 14-дневный - нет (Р2-b=A)."""
        conn = _fixture()
        self.addCleanup(conn.close)
        data = health_load.gap_load(conn, "2024-02-07")
        self.assertEqual(data["n_completed"], 1)
        self.assertEqual(data["gap"]["start"], "2024-01-03")
        self.assertEqual(data["gap"]["days"], 20)
        self.assertIsNone(data["ongoing"])
        # 2024-02-06 - последний день 14-дневного простоя: порога он не достиг
        edge = health_load.gap_load(conn, "2024-02-06")
        self.assertEqual(edge["n_completed"], 1)
        self.assertIsNone(edge["ongoing"])
        self.assertEqual(edge["gap"]["start"], "2024-01-03")
        # живое свойство (если база есть): все найденные пропуски > 14 дней
        live = _live()
        if live is not None:
            self.addCleanup(live.close)
            live_data = health_load.gap_load(live)
            self.assertEqual(live_data["status"], "completed")
            self.assertGreater(live_data["gap"]["days"],
                               health_load.PRELOAD_GAP_DAYS - 1)
            self.assertGreaterEqual(live_data["n_completed"], 20)

    # 3 -------------------------------------------------------------------
    def test_load_before_gap_7_and_14(self):
        """T7/T14 и дни с нагрузкой считаются на бумаге (Р3=A)."""
        conn = _fixture()
        self.addCleanup(conn.close)
        gap = health_load.gap_load(conn, "2024-01-23")["gap"]
        # перед простоем 14 дней по 100 кг x 10
        self.assertEqual(gap["t7"], 7000.0)
        self.assertEqual(gap["t14"], 14000.0)
        self.assertEqual(gap["d7"], 7)
        self.assertEqual(gap["d14"], 14)

    # 4 -------------------------------------------------------------------
    def test_load_uses_full_history_not_window(self):
        """Ряд - полная история ``_session_rows``, а не окно ``load_daily``."""
        conn = self.conn
        # только 2016 год; таблицы load_daily нет вовсе
        _block(conn, "2016-01-01", 10)
        _add(conn, "2016-01-31")
        conn.commit()
        self.assertIsNone(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'load_daily'"
            ).fetchone(), "фикстура не должна иметь load_daily")
        data = health_load.gap_load(conn, "2016-01-31")
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["gap"]["start"], "2016-01-11")
        self.assertEqual(data["gap"]["end"], "2016-01-30")
        self.assertEqual(data["gap"]["days"], 20)
        # живое свойство: первый пропуск 2015 года виден в выдаче (пин §5.3)
        live = _live()
        if live is not None:
            self.addCleanup(live.close)
            first = health_load.gap_load(live, "2015-09-01")
            self.assertEqual(first["gap"]["start"], "2015-08-13")
            self.assertEqual(first["gap"]["end"], "2015-08-28")

    # 5 -------------------------------------------------------------------
    def test_no_gap_is_honest(self):
        """Без пропусков строка есть и честна, а не молчит (Р5=A)."""
        conn = self.conn
        _block(conn, "2024-03-01", 20)
        conn.commit()
        data = health_load.gap_load(conn, "2024-03-20")
        self.assertEqual(data["status"], "no_gaps")
        self.assertIsNone(data["gap"])
        self.assertIsNone(data["ongoing"])
        text = digest.format_digest(digest.day_state(conn, "2024-03-20"))
        self.assertIn("пропусков больше двух недель не было", text)

    # 6 -------------------------------------------------------------------
    def test_thin_history_no_percentile(self):
        """Меньше ``MIN_GAPS`` завершённых пропусков - перцентиля нет (Р4=C)."""
        thin = _multi(2)
        self.addCleanup(thin.close)
        data = health_load.gap_load(thin)
        self.assertEqual(data["n_completed"], 2)
        self.assertIsNone(data["gap"]["percentile"])
        fat = _multi(3)
        self.addCleanup(fat.close)
        data = health_load.gap_load(fat)
        self.assertEqual(data["n_completed"], 3)
        self.assertIsNotNone(data["gap"]["percentile"])
        self.assertGreaterEqual(data["gap"]["percentile"], 0)
        self.assertLessEqual(data["gap"]["percentile"], 100)

    # 7 -------------------------------------------------------------------
    def test_ongoing_gap_not_reported_as_finished(self):
        """Идущий простой показывается как идущий (Р5=A)."""
        conn = self.conn
        _block(conn, "2024-01-01", 10)
        conn.commit()
        data = health_load.gap_load(conn, "2024-02-10")
        self.assertEqual(data["status"], "ongoing")
        self.assertIsNone(data["gap"])
        self.assertEqual(data["n_completed"], 0)
        self.assertEqual(data["ongoing"]["start"], "2024-01-11")
        self.assertEqual(data["ongoing"]["days"], 31)
        line = digest._preload_gap_line(data)
        self.assertIn("идёт", line)
        self.assertIn("не завершён", line)
        self.assertNotIn("перед пропуском", line)
        # 10 дней простоя порога ещё не достигли - это не «идёт пропуск»
        short = health_load.gap_load(conn, "2024-01-20")
        self.assertEqual(short["status"], "no_gaps")
        self.assertIsNone(short["ongoing"])

    # 8 -------------------------------------------------------------------
    def test_return_output_unchanged(self):
        """``return`` не подключён к новой функции - его вывод не изменился."""
        self.assertNotIn("gap_load", inspect.getsource(health_retrain))
        source = inspect.getsource(cli)
        start = source.index("def cmd_return")
        end = source.index("def cmd_digest")
        self.assertNotIn("gap_load", source[start:end])

    # 9 -------------------------------------------------------------------
    def test_e_semantics_untouched(self):
        """``vest_only`` вес остаётся E по RM-98: ``gap_load`` его не считает."""
        conn = self.conn
        _block(conn, "2024-01-01", 10, weight=42.0, reps=10)  # «Отжимания», E=42
        conn.commit()
        _add(conn, "2024-01-31", weight=42.0, reps=10,
             exercise="Отжимания")
        conn.commit()
        gap = health_load.gap_load(conn, "2024-01-31")["gap"]
        # тоннаж = 42*10, а не пересчёт веса тела
        self.assertEqual(gap["t7"], 7 * 42.0 * 10)
        self.assertEqual(gap["t14"], 10 * 42.0 * 10)
        source = inspect.getsource(health_load.gap_load)
        self.assertNotIn("resolve_bodyweight", source)
        self.assertNotIn("bodyweight_share", source)

    # 10 ------------------------------------------------------------------
    def test_no_writes(self):
        """Прогон не меняет ``sets`` и не создаёт ``load_daily``."""
        conn = _fixture()
        self.addCleanup(conn.close)
        before = [tuple(row) for row in
                  conn.execute("SELECT * FROM sets ORDER BY rowid")]
        health_load.gap_load(conn, "2024-02-07")
        digest.format_digest(digest.day_state(conn, "2024-02-07"))
        after = [tuple(row) for row in
                 conn.execute("SELECT * FROM sets ORDER BY rowid")]
        self.assertEqual(before, after)
        self.assertIsNone(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'load_daily'"
            ).fetchone())
        # JSON-карточка тоже читается без записи
        payload = json.loads(digest.dumps(digest.day_state(conn, "2024-02-07")))
        self.assertEqual(payload["gap_load"]["status"], "completed")
        self.assertEqual(
            [tuple(row) for row in conn.execute(
                "SELECT * FROM sets ORDER BY rowid")], before)


if __name__ == "__main__":
    unittest.main()
