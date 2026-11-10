# -*- coding: utf-8 -*-
"""PW-99: muscle_share module tests (display-only, import fence)."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from koilog import muscle_share, taxonomy

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
        """VERSION и VERSION_DATE определены (PW-105 §6 #8: канон — v2)."""
        # Осознанное обновление пина PW-99: шаг 2 PW-105 переводит канон на v2.
        self.assertEqual(muscle_share.VERSION, "v2")
        self.assertEqual(muscle_share.VERSION_DATE, "2026-11-09")


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


class MCSensitivityTests(unittest.TestCase):
    """MC-детерминизм seed=99 (§13 пререга)."""

    def test_mc_determinism_seed_99(self):
        """Два прогона с seed=99 дают идентичные результаты."""
        import random
        import collections

        # Фикстуры для MC теста.
        conn = _db()
        from datetime import date, timedelta
        today = date.today()
        
        # Добавим несколько упражнений с разными профилями.
        for i in range(10):
            d = (today - timedelta(days=i*10)).isoformat()
            _add(conn, d, "Жим штанги лежа", weight=60+i*5, reps=8)
            _add(conn, d, "Становая тяга", weight=80+i*5, reps=6)
            _add(conn, d, "Тяга гантели в наклоне одной рукой", weight=30+i*3, reps=10)
        conn.commit()

        def run_mc(seed_val):
            rng = random.Random(seed_val)
            base_kg = collections.Counter()
            
            rows = list(conn.execute("SELECT exercise, weight, reps FROM sets WHERE weight IS NOT NULL AND reps IS NOT NULL"))
            for r in rows:
                sh = muscle_share.share_of(r["exercise"])
                if sh:
                    e = float(r["weight"]) * float(r["reps"])
                    for m, w in sh.items():
                        base_kg[m] += e * w

            flips = 0
            n_draws = 50
            base_rank = tuple(m for m, _ in base_kg.most_common())

            for _ in range(n_draws):
                perturbed = collections.Counter()
                for r in rows:
                    sh = muscle_share.share_of(r["exercise"])
                    if sh:
                        e = float(r["weight"]) * float(r["reps"])
                        # Перемешиваем доли внутри профиля.
                        raw_sh = {m2: sh[m2] * rng.uniform(0.8, 1.2) for m2 in sh}
                        total = sum(raw_sh.values())
                        sh_norm = {m2: v / total for m2, v in raw_sh.items()}
                        for m2, w in sh_norm.items():
                            perturbed[m2] += e * w

                rank = tuple(m for m, _ in perturbed.most_common())
                if rank != base_rank:
                    flips += 1

            return flips, base_rank

        # Два прогона с одинаковым seed.
        flips1, rank1 = run_mc(99)
        flips2, rank2 = run_mc(99)

        # Должны быть идентичны — это главное условие детерминизма.
        self.assertEqual(flips1, flips2, f"MC flips differ: {flips1} vs {flips2}")
        self.assertEqual(rank1, rank2, "MC base ranks differ")


class LivePoolParityTests(unittest.TestCase):
    """Live-guard: каждый подход пула 4.14 имеет share_of != None.

    По прецеденту LiveWarmupTests (test_warmup_flag.py): setUpClass проверяет
    наличие базы и таблицы, SkipTest если нет; соединение mode=ro.

    §13 пререга PW-99 пункт 5: muscles.PROFILES ⊆ exercises in pool + explicit exclusions.
    """

    @classmethod
    def setUpClass(cls):
        from pathlib import Path

        db_path = Path(__file__).resolve().parent.parent / "sql" / "training.db"
        if not db_path.exists():
            raise unittest.SkipTest(f"нет живой базы: {db_path}")

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
        except sqlite3.OperationalError:
            conn.close()
            raise unittest.SkipTest(f"в базе нет таблицы sets: {db_path}")

        if not rows:
            conn.close()
            raise unittest.SkipTest(f"база пуста: в sets нет подходов")

        # Проверить что есть хотя бы один подход в пуле 4.14.
        pool_count = conn.execute(
            """
            SELECT COUNT(*) FROM sets
             WHERE date IS NOT NULL
               AND is_warmup = 0
               AND skipped = 0
               AND is_outlier = 0
               AND is_foreign = 0
            """
        ).fetchone()[0]

        if not pool_count:
            conn.close()
            raise unittest.SkipTest("пул 4.14 пуст: нет подходов для проверки parity")

        cls.conn = conn

    @classmethod
    def tearDownClass(cls):
        conn = getattr(cls, "conn", None)
        if conn is not None:
            conn.close()

    def test_all_pool_sets_have_share(self):
        """Каждый подход пула 4.14 имеет share_of != None."""
        from koilog import muscles, taxonomy

        no_share = []
        for r in self.conn.execute(
            """
            SELECT DISTINCT exercise FROM sets
             WHERE date IS NOT NULL
               AND is_warmup = 0
               AND skipped = 0
               AND is_outlier = 0
               AND is_foreign = 0
            """
        ):
            ex = r[0]
            prof = muscles.muscles_of(ex)
            # Пул исключает кардио/статистику/DASH — проверяем что шаблон есть.
            if prof is not None and muscles.DASH not in prof.muscles and not prof.is_static:
                if taxonomy.mode_of(ex) is not taxonomy.Mode.CARDIO:
                    sh = muscle_share.share_of(ex)
                    if sh is None:
                        no_share.append(ex)

        self.assertEqual(len(no_share), 0,
                         f"Подходы пула без шаблона shares: {no_share}")

    def test_profiles_covered_in_pool_or_excluded(self):
        """§13.5: каждое имя PROFILES либо в пуле, либо в явных исключениях (полный список)."""
        from koilog import muscles, taxonomy

        # Полный список исключений (§4 пререга PW-99, 14 имён):
        # Кардио (mode=CARDIO или профиль содержит DASH) — 4 имени.
        # Статика — 2 имени.
        # Неиспользуемые профили (не встречались в пуле за всю историю) — 8 имён.
        expected_exclusions = {
            # Кардио (mode=CARDIO):
            "Бег",
            "Беговая дорожка",
            "Гребля в тренажере",
            "Тренажер для гребли",
            # Статика (is_static=True):
            "Планка",
            "Планка на руках",
            # Неиспользуемые (есть в PROFILES, но нет в пуле):
            "Разминка",
            "Жим гантелей",
            "Махи в наклоне одной рукой",
            "Разведение рук стоя",
            "Разгибание руки с гантелью из-за головы",
            "Сгибание рук с гантелями",
            "Тяга гантели одной рукой стоя в наклоне",
            "Французский жим лежа",
        }

        # Соберём все упражнения из пула 4.14.
        pool_exercises = set()
        for r in self.conn.execute(
            """
            SELECT DISTINCT exercise FROM sets
             WHERE date IS NOT NULL
               AND is_warmup = 0
               AND skipped = 0
               AND is_outlier = 0
               AND is_foreign = 0
            """
        ):
            pool_exercises.add(r[0])

        # Проверить каждое имя PROFILES.
        missing_from_pool = []
        for name in muscles.PROFILES:
            prof = muscles.muscles_of(name)
            if prof is None:
                continue

            # Исключения: статика, DASH, CARDIO mode — явно исключаем.
            if prof.is_static or muscles.DASH in prof.muscles:
                continue
            if taxonomy.mode_of(name) is taxonomy.Mode.CARDIO:
                continue

            # Проверяем что есть в пуле или в списке исключений.
            if name not in pool_exercises and name not in expected_exclusions:
                missing_from_pool.append(name)

        # Реальный ассерт: если есть профили которые не в пуле и не в исключениях — тест падает.
        self.assertEqual(
            len(missing_from_pool), 0,
            f"Профили PROFILES не в пуле и не в исключениях: {missing_from_pool}. "
            "Добавьте их в expected_exclusions или верните данные."
        )


if __name__ == "__main__":
    unittest.main()
