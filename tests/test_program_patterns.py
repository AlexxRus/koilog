# -*- coding: utf-8 -*-
"""PG-100: паттерны тренировочных программ — display-only.

Пререг: ``docs/pg-100-prereg.md``. Тестирует контракт §8/§13:
детерминизм байт-в-байт; уровни сигнатуры (L1/L2/L3/L3o); кардио исключено;
правило адаптивного K (≥90 %, кап 10, ничьи count desc → имя asc);
эры (пустой год честно); цикл (переходы = дни − 1 − разрывы > 14 дн);
приверженность 0..1 со знаменателем; import/AST фенсы."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

from koilog import program_patterns as pp

ROOT = Path(__file__).resolve().parent.parent


def _db() -> sqlite3.Connection:
    """In-memory база с полными схемами sets / load_daily / v_weekly_koi."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sets (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            date           TEXT,
            session_id     TEXT,
            exercise       TEXT,
            weight         REAL,
            reps           INTEGER,
            weight_mode    TEXT,
            exercise_group TEXT,
            koi            REAL,
            is_warmup      INTEGER NOT NULL DEFAULT 0,
            skipped        INTEGER NOT NULL DEFAULT 0,
            is_outlier     INTEGER NOT NULL DEFAULT 0,
            is_foreign     INTEGER NOT NULL DEFAULT 0,
            workout_name   TEXT,
            source_row     INTEGER
        );
        CREATE VIEW v_weekly_koi AS
            SELECT week, exercise, exercise_group,
                   SUM(koi) OVER (PARTITION BY week, exercise) AS koi_sum,
                   '—' AS band
            FROM (SELECT * FROM sets WHERE 0);
        CREATE TABLE load_daily (
            date         TEXT PRIMARY KEY,
            acwr_warmup  REAL,
            gap_warmup   INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    return conn


def _add(conn, day, session, exercise, workout_name=None, cardio=False):
    """Вставить один силовой или кардио-подход."""
    group = "КАРДИО" if cardio else (
        "ЖИМ ЛЁЖА" if "жим" in exercise.lower() else
        "ПРИСЕДАНИЯ" if "присед" in exercise.lower() else
        "ТЯГА ВЕРТИКАЛЬНАЯ" if "подтяг" in exercise.lower() else
        "ТЯГА ГОРИЗОНТАЛЬНАЯ" if "тяга" in exercise.lower() else
        "ПРЕСС" if "скручив" in exercise.lower() else
        "—"
    )
    conn.execute(
        """
        INSERT INTO sets
            (date, session_id, exercise, workout_name, exercise_group,
             weight, reps, is_warmup, skipped, is_outlier, is_foreign)
        VALUES (?, ?, ?, ?, ?, 10.0, 10, 0, 0, 0, 0)
        """,
        (day, session, exercise, workout_name or exercise, group),
    )


def _add_cardio(conn, day, session, exercise="Бег"):
    _add(conn, day, session, exercise, workout_name="+кардио", cardio=True)


class SignatureLevelTests(unittest.TestCase):
    """§13.2: уровни L1/L2/L3/L3o на синтетике."""

    def test_l1_signature_is_workout_names_sorted(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-08", "s1", "Жим штанги лежа", "A")
        _add(conn, "2024-01-08", "s1", "Приседания", "B")
        _add(conn, "2024-01-08", "s2", "Становая тяга", "C")
        conn.commit()
        sig = pp.day_signature(conn, "2024-01-08", level="name")
        self.assertEqual(sig, ("A", "B", "C"))

    def test_l2_signature_is_groups_sorted(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-08", "s1", "Жим штанги лежа", workout_name="A")
        _add(conn, "2024-01-08", "s1", "Приседания", workout_name="B")
        conn.commit()
        sig = pp.day_signature(conn, "2024-01-08", level="group")
        # Жим лёжа + приседания = 2 группы
        self.assertEqual(len(sig), 2)
        self.assertIn("ЖИМ ЛЁЖА", sig)
        self.assertIn("ПРИСЕДАНИЯ", sig)

    def test_l3_signature_is_exercise_set_order_independent(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-08", "s1", "Жим штанги лежа", workout_name="A")
        _add(conn, "2024-01-08", "s1", "Приседания", workout_name="B")
        conn.commit()
        sig_set = pp.day_signature(conn, "2024-01-08", level="set")
        self.assertEqual(sig_set, ("Жим штанги лежа", "Приседания"))

    def test_ordered_signature_keeps_first_appearance_order(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-08", "s1", "Приседания", workout_name="A")
        _add(conn, "2024-01-08", "s1", "Жим штанги лежа", workout_name="A")
        conn.commit()
        sig_ord = pp.day_signature(conn, "2024-01-08", level="ordered")
        self.assertEqual(sig_ord, ("Приседания", "Жим штанги лежа"))

    def test_cardio_excluded_from_strength_signature(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-08", "s1", "Жим штанги лежа", workout_name="A")
        _add_cardio(conn, "2024-01-08", "s2", "Бег")
        conn.commit()
        sig = pp.day_signature(conn, "2024-01-08", level="name")
        self.assertEqual(sig, ("A",))  # только силовая A

    def test_empty_day_gives_empty_tuple_no_exception(self):
        conn = _db()
        self.addCleanup(conn.close)
        sig = pp.day_signature(conn, "2024-01-08", level="name")
        self.assertEqual(sig, ())

    def test_single_set_day_no_exception(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-08", "s1", "Жим штанги лежа", workout_name="Solo")
        conn.commit()
        sig = pp.day_signature(conn, "2024-01-08", level="name")
        self.assertEqual(sig, ("Solo",))


class LibraryKLogicTests(unittest.TestCase):
    """§13.3: правило K, ничьи, «прочее»."""

    def test_k_covers_min_coverage_but_not_more_than_cap(self):
        conn = _db()
        self.addCleanup(conn.close)
        # 10 дней: 5 × A, 3 × B, 2 × C → покрытие ≥0.9 требует 9 дней, т.е. K=3 (5+3+2=10≥9)
        for i in range(5):
            _add(conn, f"2024-01-{i+1:02d}", "s1", "Жим", workout_name="A")
        for i in range(3):
            _add(conn, f"2024-01-{6+i:02d}", "s1", "Жим", workout_name="B")
        for i in range(2):
            _add(conn, f"2024-01-{10+i:02d}", "s1", "Жим", workout_name="C")
        conn.commit()
        lib = pp.templates(conn, "2024-01-01", "2024-01-15", min_coverage=0.9, cap=10)
        self.assertEqual(lib.k, 3)  # A + B + C = 10/10 = 100 %
        self.assertEqual(len(lib.templates), 3)
        self.assertEqual(len(lib.others), 0)
        self.assertEqual(lib.coverage, 1.0)

    def test_ties_broken_by_count_desc_then_name_asc(self):
        conn = _db()
        self.addCleanup(conn.close)
        # A и B по 3 дня → побеждает A (лексикографически раньше)
        for i in range(3):
            _add(conn, f"2024-01-{i+1:02d}", "s1", "Жим", workout_name="B")
        for i in range(3):
            _add(conn, f"2024-01-{4+i:02d}", "s1", "Жим", workout_name="A")
        for i in range(2):
            _add(conn, f"2024-01-{8+i:02d}", "s1", "Жим", workout_name="C")
        conn.commit()
        lib = pp.templates(conn, "2024-01-01", "2024-01-15", min_coverage=0.5, cap=3)
        self.assertEqual(lib.k, 2)
        self.assertEqual(lib.templates[0].signature, ("A",))
        self.assertEqual(lib.templates[1].signature, ("B",))

    def test_others_contains_all_remaining_signatures_without_loss(self):
        conn = _db()
        self.addCleanup(conn.close)
        # 10 шаблонов: K=5, остальные 5 в others
        names = list("ABCDEFGHIJ")
        for index, name in enumerate(names):
            _add(conn, f"2024-01-{index+1:02d}", "s1", "Жим", workout_name=name)
        conn.commit()
        lib = pp.templates(conn, "2024-01-01", "2024-01-15", min_coverage=0.5, cap=5)
        self.assertEqual(len(lib.templates) + len(lib.others), 10)
        all_sigs = {t.signature for t in lib.templates} | {t.signature for t in lib.others}
        self.assertEqual(all_sigs, {(n,) for n in names})


class EraTableTests(unittest.TestCase):
    """§13.4: эры, пустой год честно."""

    def test_empty_year_shown_with_zero_days(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-08", "s1", "Жим", workout_name="A")
        conn.commit()
        eras = pp.era_table(conn, level="name")
        years = [e.year for e in eras]
        self.assertIn("2024", years)
        era_2024 = next(e for e in eras if e.year == "2024")
        self.assertEqual(era_2024.days, 1)
        self.assertEqual(era_2024.signatures, 1)

    def test_window_anchor_equals_max_date(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-03-15", "s1", "Жим", workout_name="A")
        _add(conn, "2024-03-20", "s1", "Жим", workout_name="A")
        conn.commit()
        since, until = pp.current_window(conn, days=5)
        self.assertEqual(until, "2024-03-20")
        self.assertEqual(since, "2024-03-16")


class CycleTransitionTests(unittest.TestCase):
    """§13.5: цикл, переходы = дни − 1 − разрывы > max_gap."""

    def test_transitions_equal_days_minus_one_minus_gaps(self):
        conn = _db()
        self.addCleanup(conn.close)
        # Дни: 1, 2, 3, 15 (разрыв 12 дней между 3 и 15 > 14? нет, значит 3 перехода)
        for day in ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-15"):
            _add(conn, day, "s1", "Жим", workout_name="A")
        conn.commit()
        rep = pp.cycle(conn, "2024-01-01", "2024-01-20", max_gap=14)
        self.assertEqual(rep.days, 4)
        self.assertEqual(rep.transitions, 3)  # 4-1-0
        self.assertEqual(rep.gaps, 0)

    def test_gap_greater_than_max_gap_excluded_from_transitions(self):
        conn = _db()
        self.addCleanup(conn.close)
        # Дни: 1, 2, 20 (разрыв 18 > 14 → 1 пропущенный переход)
        for day in ("2024-01-01", "2024-01-02", "2024-01-20"):
            _add(conn, day, "s1", "Жим", workout_name="A")
        conn.commit()
        rep = pp.cycle(conn, "2024-01-01", "2024-01-25", max_gap=14)
        self.assertEqual(rep.days, 3)
        self.assertEqual(rep.transitions, 1)  # 3-1-1
        self.assertEqual(rep.gaps, 1)

    def test_ring_hits_count_consecutive_transitions(self):
        conn = _db()
        self.addCleanup(conn.close)
        # Кольцо A→B→C→A: 3 перехода по кольцу, всего 3 перехода
        for day, wn in (("2024-01-01", "A"), ("2024-01-02", "B"), ("2024-01-03", "C")):
            _add(conn, day, "s1", "Жим", workout_name=wn)
        conn.commit()
        rep = pp.cycle(
            conn, "2024-01-01", "2024-01-10",
            level="name", ring=["A", "B", "C"]
        )
        self.assertEqual(rep.ring_hits, 2)  # A→B, B→C (нет возврата C→A)
        self.assertEqual(rep.ring_share, 1.0)


class AdherenceReportTests(unittest.TestCase):
    """§13.6: приверженность 0..1, знаменатель, новые дни."""

    def test_adherence_ratio_between_0_and_1(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-01", "s1", "Жим", workout_name="Known")
        _add(conn, "2024-01-02", "s1", "Жим", workout_name="Unknown")
        conn.commit()
        lib = pp.templates(conn, "2024-01-01", "2024-01-10", min_coverage=0.5, cap=3)
        adh = pp.adherence(conn, "2024-01-01", "2024-01-10", lib)
        self.assertGreaterEqual(adh.ratio, 0.0)
        self.assertLessEqual(adh.ratio, 1.0)

    def test_adherence_denominator_equals_days_in_window(self):
        conn = _db()
        self.addCleanup(conn.close)
        for day in ("2024-01-01", "2024-01-02", "2024-01-03"):
            _add(conn, day, "s1", "Жим", workout_name="Known")
        conn.commit()
        lib = pp.templates(conn, "2024-01-01", "2024-01-10")
        adh = pp.adherence(conn, "2024-01-01", "2024-01-10", lib)
        self.assertEqual(adh.days, 3)

    def test_new_days_listed_with_their_signatures(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-01", "s1", "Жим", workout_name="Known")
        _add(conn, "2024-01-02", "s1", "Жим", workout_name="NewSig")
        conn.commit()
        lib = pp.templates(conn, "2024-01-01", "2024-01-10", min_coverage=0.5, cap=1)
        adh = pp.adherence(conn, "2024-01-01", "2024-01-10", lib)
        self.assertEqual(len(adh.new_days), 1)
        self.assertEqual(adh.new_days[0].day, "2024-01-02")
        self.assertEqual(adh.new_days[0].signature, ("NewSig",))


class DeterminismTests(unittest.TestCase):
    """§13.1: детерминизм байт-в-байт."""

    def test_two_runs_are_bit_identical(self):
        conn = _db()
        self.addCleanup(conn.close)
        for day, wn in (("2024-01-01", "A"), ("2024-01-02", "B"), ("2024-01-03", "A")):
            _add(conn, day, "s1", "Жим", workout_name=wn)
        conn.commit()
        first_lib = pp.templates(conn, "2024-01-01", "2024-01-10")
        second_lib = pp.templates(conn, "2024-01-01", "2024-01-10")
        self.assertEqual(first_lib.k, second_lib.k)
        self.assertEqual(len(first_lib.templates), len(second_lib.templates))
        fmt1 = pp.format_templates(first_lib)
        fmt2 = pp.format_templates(second_lib)
        self.assertEqual(fmt1, fmt2)


class FenceTests(unittest.TestCase):
    """§13.7: фенсы импорта и отсутствия записи в БД."""

    def test_decision_modules_do_not_import_program_patterns(self):
        """state/retrain/digest/score/ml/groups не импортируют модуль."""
        files = [
            ROOT / "koilog" / "health" / name
            for name in ("state.py", "retrain.py", "digest.py", "score.py",
                         "ml.py", "groups.py")
        ]
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("program_patterns", text, f"{path.name}: импорт запрещён")

    def test_program_patterns_does_not_import_load_modules(self):
        """Модуль не импортирует muscle_load/muscle_share/work_arc."""
        import ast
        source = (ROOT / "koilog" / "program_patterns.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        bad_names = ("muscle_load", "muscle_share", "work_arc")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(
                        alias.name, bad_names, f"импорт {alias.name} запрещён"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None:
                    self.assertNotIn(
                        node.module, bad_names, f"from {node.module} импорт запрещён"
                    )

    def test_importing_program_patterns_does_not_pull_decision_or_load_modules(
            self):
        """Импорт модуля не тянет decision/нагрузочные модули."""
        code = (
            "import sys\n"
            "import koilog.program_patterns\n"
            "bad = [m for m in sys.modules if m in (\n"
            "    'koilog.health.state', 'koilog.health.score',\n"
            "    'koilog.health.ml', 'koilog.muscle_load',\n"
            "    'koilog.muscle_share', 'koilog.work_arc')]\n"
            "assert not bad, bad\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_module_has_no_db_write_ast(self):
        """AST-проверка: модуль не вызывает execut/write."""
        import ast
        source = (ROOT / "koilog" / "program_patterns.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        writes = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("executescript", "commit"):
                    writes.append(node.func.attr)
        self.assertEqual(writes, [], f"Запись в БД через {writes} запрещена")


if __name__ == "__main__":
    unittest.main()
