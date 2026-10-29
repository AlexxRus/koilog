"""Гварды живых классов: проверяют среду, а не данные (GU-105).

Живые классы (``test_health_canon.LiveCanonTests``,
``test_stats.RealProgramInventoryTests``) читают боевую базу. Гвард обязан
отличать «базы нет» от «файл есть, но это не база»: такой файл остаётся после
``load`` в пустом дереве, после ``score`` на пустой базе и после прерванного
прогона. Раньше проверялось только ``Path.exists()``, и пустая схема давала
падения с именами, указывающими на данные (пререг ``docs/4-16-live-guards.md``).

Здесь проверяется сам гвард - на временных базах, без ``data/`` и без живой
``sql/training.db``, поэтому файл проходит и в чистом клоне.

Граница (пререг 4-16 §6): гвард проверяет **среду** (файл, таблица, непустая
таблица); **семантику** проверяют сами тесты. Ledger, заполненный только
винтажом, обязан падать - гвард этого не прячет.
"""

from __future__ import annotations

import contextlib
import importlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from koilog import etl
from koilog.health import score


def _module(name: str):
    """Модуль теста под тем именем, под которым его импортировал discover.

    ``unittest discover -s tests`` кладёт модули верхним уровнем, а
    ``python -m unittest tests.<файл>`` - в пакет. Берём уже загруженный
    модуль, чтобы патч попал в тот же объект класса.
    """
    if name in sys.modules:
        return sys.modules[name]
    for candidate in (name, "tests." + name):
        try:
            return importlib.import_module(candidate)
        except ImportError:
            continue
    raise ImportError(f"не найден модуль теста: {name}")


@contextlib.contextmanager
def _base(path):
    """Подменить ``DEFAULT_DB`` на временную базу в обоих модулях.

    ``test_health_canon`` импортировал константу по значению, ``test_stats``
    читает ``etl.DEFAULT_DB`` в момент вызова - патчим оба имени.
    """
    canon = _module("test_health_canon")
    with mock.patch.object(etl, "DEFAULT_DB", path), \
            mock.patch.object(canon, "DEFAULT_DB", path):
        yield


def _zero_byte_base(directory: Path) -> Path:
    """Файл, который существует, но базой не является (E1)."""
    path = directory / "training.db"
    sqlite3.connect(path).close()
    return path


def _empty_base(directory: Path) -> Path:
    """Схема без строк - то, что оставляет ``load`` в пустом дереве (E2)."""
    path = directory / "training.db"
    conn = etl.connect(path)
    try:
        etl.create_schema(conn)
    finally:
        conn.close()
    return path


def _add_set(path: Path, exercise: str = "Приседания со штангой") -> None:
    conn = etl.connect(path)
    try:
        conn.execute(
            "INSERT INTO sets (source, source_row, session_id, date, exercise, "
            "weight, reps) VALUES ('journal', 1, 's1', '2026-10-01', ?, 100.0, 5)",
            (exercise,),
        )
        conn.commit()
    finally:
        conn.close()


def _add_ledger(path: Path, day: str = "2026-10-01",
                spec: str | None = None) -> None:
    conn = etl.connect(path)
    try:
        score._ensure_schema(conn)
        conn.execute(
            "INSERT INTO ml_score (day, as_of, computed_at, score, spec_hash) "
            "VALUES (?, ?, '2026-10-02T00:00:00', 0.5, ?)",
            (day, day, spec or score.spec_hash()),
        )
        conn.commit()
    finally:
        conn.close()


class GuardTestCase(unittest.TestCase):
    """Общая обвязка: временный каталог и вызов гварда класса."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def live_class(self, module_name: str, class_name: str):
        return getattr(_module(module_name), class_name)

    def assert_guard_skips(self, module_name: str, class_name: str,
                           *fragments: str) -> str:
        """``setUpClass`` поднимает ``SkipTest`` с внятной причиной."""
        cls = self.live_class(module_name, class_name)
        with self.assertRaises(unittest.SkipTest) as caught:
            cls.setUpClass()
        reason = str(caught.exception)
        for fragment in fragments:
            self.assertIn(fragment, reason)
        return reason

    def assert_guard_passes(self, module_name: str, class_name: str):
        """``setUpClass`` открывает базу и не скипает."""
        cls = self.live_class(module_name, class_name)
        cls.setUpClass()
        self.addCleanup(cls.tearDownClass)
        return cls


class MissingBaseTests(GuardTestCase):
    """E0: файла базы нет."""

    def test_missing_file_skips_inventory_with_hint(self):
        path = self.dir / "absent.db"
        with _base(path):
            self.assert_guard_skips(
                "test_stats", "RealProgramInventoryTests",
                "нет базы", str(path), "load")
        self.assertFalse(path.exists(), "гвард не должен создавать базу")

    def test_missing_file_skips_canon_with_hint(self):
        path = self.dir / "absent.db"
        with _base(path):
            self.assert_guard_skips(
                "test_health_canon", "LiveCanonTests",
                "нет живой базы", str(path))


class TablelessBaseTests(GuardTestCase):
    """E1: файл существует, таблиц нет (0 байт)."""

    def test_zero_byte_file_skips_inventory(self):
        path = _zero_byte_base(self.dir)
        with _base(path):
            self.assert_guard_skips(
                "test_stats", "RealProgramInventoryTests",
                "нет таблицы sets", "load")
        self.assertEqual(path.stat().st_size, 0, "чтение гварда не пишет в файл")

    def test_zero_byte_file_skips_canon(self):
        path = _zero_byte_base(self.dir)
        with _base(path):
            self.assert_guard_skips(
                "test_health_canon", "LiveCanonTests",
                "нет таблицы ml_score", "refresh --deep")


class EmptyBaseTests(GuardTestCase):
    """E2/E3: схема есть, строк нет."""

    def test_empty_sets_skips_inventory(self):
        path = _empty_base(self.dir)
        with _base(path):
            self.assert_guard_skips(
                "test_stats", "RealProgramInventoryTests",
                "пуста", "выполните load")

    def test_absent_ledger_skips_canon(self):
        path = _empty_base(self.dir)
        with _base(path):
            self.assert_guard_skips(
                "test_health_canon", "LiveCanonTests",
                "нет таблицы ml_score", "score --replay")

    def test_empty_ledger_skips_canon(self):
        path = _empty_base(self.dir)
        conn = etl.connect(path)
        try:
            score._ensure_schema(conn)      # таблица есть, строк нет
        finally:
            conn.close()
        with _base(path):
            self.assert_guard_skips(
                "test_health_canon", "LiveCanonTests",
                "ledger ml_score пуст", "score --replay")


class SetsOnlyBaseTests(GuardTestCase):
    """E4: подходы загружены, health-стадии не гонялись."""

    def test_inventory_passes_guard_with_sets(self):
        path = _empty_base(self.dir)
        _add_set(path)
        with _base(path):
            cls = self.assert_guard_passes(
                "test_stats", "RealProgramInventoryTests")
            self.assertEqual(
                cls.conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0], 1)

    def test_canon_skips_on_sets_only_base(self):
        path = _empty_base(self.dir)
        _add_set(path)
        with _base(path):
            self.assert_guard_skips(
                "test_health_canon", "LiveCanonTests",
                "нет таблицы ml_score")


class ReadyBaseTests(GuardTestCase):
    """Живая форма: ledger непуст - гвард не срабатывает."""

    def test_canon_passes_guard_with_ledger_rows(self):
        path = _empty_base(self.dir)
        _add_ledger(path)
        with _base(path):
            cls = self.assert_guard_passes("test_health_canon", "LiveCanonTests")
            self.assertEqual(cls.current, score.spec_hash())
            self.assertEqual(
                cls.conn.execute("SELECT COUNT(*) FROM ml_score").fetchone()[0], 1)


class ClassLevelTests(GuardTestCase):
    """Механизм: скип класса не стартует ни одного теста."""

    def test_skip_costs_no_started_tests(self):
        path = self.dir / "absent.db"
        for module_name, class_name in (
            ("test_health_canon", "LiveCanonTests"),
            ("test_stats", "RealProgramInventoryTests"),
        ):
            with self.subTest(cls=class_name), _base(path):
                suite = unittest.TestLoader().loadTestsFromTestCase(
                    self.live_class(module_name, class_name))
                result = unittest.TestResult()
                suite.run(result)
                self.assertEqual(result.testsRun, 0)
                self.assertEqual(len(result.skipped), 1)
                self.assertEqual(result.failures, [])
                self.assertEqual(result.errors, [])


class IncidentPinTests(GuardTestCase):
    """Пин инцидента: пустая схема не даёт падений, только два скипа."""

    def test_empty_schema_incident_reports_two_class_skips(self):
        path = _empty_base(self.dir)
        loader = unittest.TestLoader()
        suite = unittest.TestSuite([
            loader.loadTestsFromTestCase(
                self.live_class("test_health_canon", "LiveCanonTests")),
            loader.loadTestsFromTestCase(
                self.live_class("test_stats", "RealProgramInventoryTests")),
        ])
        result = unittest.TestResult()
        with _base(path):
            suite.run(result)
        self.assertEqual(result.testsRun, 0)
        self.assertEqual(len(result.skipped), 2)
        self.assertEqual(result.failures, [])
        self.assertEqual(result.errors, [])


if __name__ == "__main__":
    unittest.main()
