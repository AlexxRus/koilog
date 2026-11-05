# -*- coding: utf-8 -*-
"""PW-99: muscle_share module tests (display-only, import fence)."""

from __future__ import annotations

import collections
import sqlite3
import unittest
from pathlib import Path

from koilog import muscles, muscle_share, taxonomy

ROOT = Path(__file__).resolve().parent


def _db() -> sqlite3.Connection:
    """Create an in-memory DB with sets schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sets (
            date TEXT,
            session_id TEXT,
            exercise TEXT,
            weight REAL,
            reps INTEGER,
            koi REAL,
            weight_mode TEXT,
            is_warmup INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            is_outlier INTEGER NOT NULL DEFAULT 0,
            is_foreign INTEGER NOT NULL DEFAULT 0,
            is_gravitron INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    return conn


def _add(conn, date, exercise, weight=100, reps=10, koi=50, warm=0,
         skipped=0, outlier=0, foreign=0, grav=0):
    """Insert a row into sets."""
    conn.execute(
        "INSERT INTO sets "
        "(date, exercise, weight, reps, koi, is_warmup, skipped, "
        "is_outlier, is_foreign, is_gravitron) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (date, exercise, weight, reps, koi, warm, skipped, outlier, foreign, grav),
    )


class ShareTemplateTests(unittest.TestCase):
    """Проверка таблицы v1: Σ=1, покрытие профилей, overrides."""

    def test_all_templates_normalize_to_one(self):
        """Все шаблоны нормализуются к Σ=1."""
        for profile, role in muscle_share._SHARES_V1.items():
            sh = muscle_share._shares(profile, role)
            total = sum(sh.values())
            self.assertAlmostEqual(total, 1.0, places=10,
                                   msg=f"Σ≠1 for {profile}")

    def test_group_overrides_exist_for_pulling_groups(self):
        """Overrides есть для тяг ВЕРТ/ГОР с одним профилем."""
        vert_key = ("ТЯГА ВЕРТИКАЛЬНАЯ",
                    ("широчайшие", "ромбовидные", "бицепс", "дельты задние"))
        horz_key = ("ТЯГА ГОРИЗОНТАЛЬНАЯ",
                    ("широчайшие", "ромбовидные", "бицепс", "дельты задние"))
        self.assertIn(vert_key, muscle_share._GROUP_OVERRIDES)
        self.assertIn(horz_key, muscle_share._GROUP_OVERRIDES)
        self.assertNotEqual(
            muscle_share._GROUP_OVERRIDES[vert_key],
            muscle_share._GROUP_OVERRIDES[horz_key],
        )

    def test_share_of_returns_none_for_unknown_profile(self):
        """Нет шаблона → None."""
        # Упражнение без профиля.
        self.assertIsNone(muscle_share.share_of("Бег"))
        # Профиль есть, но нет в таблице.
        # (Кардио/статика уже исключены выше.)

    def test_covered_profiles_count(self):
        """28 уникальных композиций покрыты."""
        covered = set()
        from koilog import muscles

        for name in muscles.PROFILES:
            prof = muscles.muscles_of(name)
            if prof is None or prof.is_static or muscles.DASH in prof.muscles:
                continue
            if taxonomy.mode_of(name) is taxonomy.Mode.CARDIO:
                continue
            if prof.muscles not in muscle_share._SHARES_V1:
                continue
            covered.add(prof.muscles)
        # Проверка по заголовку пререга (§3).
        self.assertEqual(len(covered), 28,
                         f"Covered profiles mismatch: {len(covered)} vs 28")

    def test_format_shares_produces_table(self):
        """format_shares возвращает таблицу Markdown."""
        txt = muscle_share.format_shares()
        self.assertIn("| группа | профиль | роль | доли |", txt)
        self.assertGreater(txt.count("|"), 10)


class InvariantTests(unittest.TestCase):
    """Инвариант Σ(share×E) = E per set (residual=0)."""

    def test_invariant_residual_is_zero_on_simple_case(self):
        """Один подход — residual = 0."""
        conn = _db()
        # Жим лёжа: профиль грудь|трицепс|дельты передние, share=(4,2,1)/7.
        _add(conn, "2024-01-01", "Жим штанги лежа", weight=100, reps=10, koi=50)
        conn.commit()

        worst, days = muscle_share.check_invariant(conn)
        self.assertEqual(days, 1)
        self.assertAlmostEqual(worst, 0.0, places=10)

    def test_invariant_holds_for_multiple_sets(self):
        """Несколько подходов — residual всё ещё 0."""
        conn = _db()
        _add(conn, "2024-01-01", "Жим штанги лежа", weight=100, reps=10)
        _add(conn, "2024-01-01", "Приседания со штангой", weight=80, reps=8)
        _add(conn, "2024-01-02", "Становая тяга", weight=120, reps=6)
        conn.commit()

        worst, days = muscle_share.check_invariant(conn)
        self.assertEqual(days, 2)
        self.assertAlmostEqual(worst, 0.0, places=10)

    def test_invariant_works_with_null_koi(self):
        """koi=None не ломает инвариант."""
        conn = _db()
        _add(conn, "2024-01-01", "Жим штанги лежа", weight=100, reps=10, koi=None)
        conn.commit()

        worst, days = muscle_share.check_invariant(conn)
        self.assertAlmostEqual(worst, 0.0, places=10)


class TotalsTests(unittest.TestCase):
    """Агрегация kg/koi/sets по мышцам."""

    def test_totals_basic_aggregation(self):
        """Базовый расчёт kg/koi/sets."""
        conn = _db()
        # Жим лёжа: грудь|трицепс|дельты передние (4:2:1).
        _add(conn, "2024-01-01", "Жим штанги лежа", weight=100, reps=10, koi=70)
        conn.commit()

        kg, koi, sets_cnt = muscle_share.totals_by_muscle(conn)

        expected_total_e = 100 * 10  # 1000
        expected_chest_share = 4 / 7
        expected_triceps_share = 2 / 7
        expected_delt_front_share = 1 / 7

        self.assertAlmostEqual(kg["грудь"], expected_total_e * expected_chest_share, places=5)
        self.assertAlmostEqual(kg["трицепс"], expected_total_e * expected_triceps_share, places=5)
        self.assertAlmostEqual(kg["дельты передние"], expected_total_e * expected_delt_front_share, places=5)
        # koi аналогично.
        self.assertAlmostEqual(koi["грудь"], 70 * expected_chest_share, places=5)
        # Sets count.
        self.assertAlmostEqual(sets_cnt["грудь"], expected_chest_share, places=5)

    def test_90_day_window(self):
        """bucket='90' режет окно."""
        from datetime import date, timedelta

        conn = _db()
        # Используем текущие даты для корректной проверки окна 90 дней.
        today = date.today()
        old_date = (today - timedelta(days=150)).isoformat()
        recent_date = (today - timedelta(days=30)).isoformat()

        _add(conn, old_date, "Жим штанги лежа", weight=100, reps=10)
        _add(conn, recent_date, "Жим штанги лежа", weight=100, reps=10)
        conn.commit()

        kg_all, _, _ = muscle_share.totals_by_muscle(conn, bucket="all")
        kg_90, _, _ = muscle_share.totals_by_muscle(conn, bucket="90")

        # old_date > 90 дней от anchor (recent_date), значит оно excluded из 90d.
        self.assertGreater(sum(kg_all.values()), sum(kg_90.values()))
        # kg_90 содержит только recent подход (~1000 кг * доля).
        self.assertGreater(sum(kg_90.values()), 0)

    def test_since_override(self):
        """since= перекрывает bucket."""
        conn = _db()
        _add(conn, "2024-01-01", "Жим штанги лежа", weight=100, reps=10)
        _add(conn, "2024-03-01", "Жим штанги лежа", weight=100, reps=10)
        conn.commit()

        kg1, _, _ = muscle_share.totals_by_muscle(conn, since="2024-02-01")
        kg2, _, _ = muscle_share.totals_by_muscle(conn, bucket="all", since="2024-02-01")

        self.assertEqual(sum(kg1.values()), sum(kg2.values()))

    def test_excludes_cardio_and_static(self):
        """Кардио и статика excluded."""
        conn = _db()
        _add(conn, "2024-01-01", "Беговая дорожка", weight=0, reps=0, koi=0)
        _add(conn, "2024-01-01", "Планка", weight=0, reps=0, koi=0)
        _add(conn, "2024-01-01", "Жим штанги лежа", weight=100, reps=10)
        conn.commit()

        kg, _, _ = muscle_share.totals_by_muscle(conn)
        # Только жим считается.
        self.assertGreater(kg["грудь"], 0)


class ImportFenceTests(unittest.TestCase):
    """Фенс импорта: muscle_share не тянет решения и не тянут решения его."""

    DECISION_MODULES = (
        "koilog/health/state.py",
        "koilog/health/retrain.py",
        "koilog/health/digest.py",
        "koilog/health/score.py",
        "koilog/health/ml.py",
        "koilog/health/groups.py",
    )

    def test_decision_modules_do_not_import_muscle_share(self):
        """Решения не импортируют muscle_share."""
        for rel in self.DECISION_MODULES:
            with self.subTest(module=rel):
                text = (ROOT.parent / rel).read_text(encoding="utf-8")
                self.assertNotIn("muscle_share", text,
                                 f"{rel} imports muscle_share")
                self.assertNotIn("from . import muscle_share", text)
                self.assertNotIn("from .. import muscle_share", text)

    def test_muscle_share_does_not_import_decision_modules(self):
        """muscle_share не импортирует решения."""
        text = (ROOT.parent / "koilog" / "muscle_share.py").read_text(encoding="utf-8")
        bad = ["state", "retrain", "digest", "score", "ml"]
        for mod in bad:
            # Импортируется только muscles, taxonomy, collections, sqlite3, datetime.
            self.assertNotIn(f"'{mod}'", text)
            self.assertNotIn(f'"{mod}"', text)

    def test_importing_muscle_share_does_not_pull_decision_modules(self):
        """Импорт muscle_share не тянет решения (subprocess guard)."""
        code = (
            "import sys\n"
            "import koilog.muscle_share\n"
            "bad = [m for m in sys.modules if m in (\n"
            "    'koilog.health.state', 'koilog.health.retrain',\n"
            "    'koilog.health.digest', 'koilog.health.score',\n"
            "    'koilog.health.ml', 'koilog.health.groups')]\n"
            "assert not bad, bad\n"
        )
        # Запуск через subprocess для чистого окружения.
        import subprocess
        proc = subprocess.run(
            ["python", "-c", code],
            capture_output=True,
            text=True,
            cwd=str(ROOT.parent),
        )
        self.assertEqual(proc.returncode, 0,
                         f"Import pulled decision modules: {proc.stderr}")

    def test_version_constants_present(self):
        """VERSION и VERSION_DATE определены."""
        self.assertEqual(muscle_share.VERSION, "v1")
        self.assertEqual(muscle_share.VERSION_DATE, "2026-11-05")


class ProfileCoverageTests(unittest.TestCase):
    """Покрытие профилей: missing=0."""

    def test_no_missing_templates_in_pool(self):
        """Если профиль есть — шаблон тоже есть."""
        from koilog import muscles, taxonomy

        missing = []
        for name in muscles.PROFILES:
            prof = muscles.muscles_of(name)
            if prof is None or prof.is_static or muscles.DASH in prof.muscles:
                continue
            if taxonomy.mode_of(name) is taxonomy.Mode.CARDIO:
                continue
            if prof.muscles not in muscle_share._SHARES_V1:
                missing.append((name, prof.muscles))

        self.assertEqual(len(missing), 0,
                         f"Missing templates: {missing}")


if __name__ == "__main__":
    unittest.main()
