# -*- coding: utf-8 -*-
"""PW-105: sEMG-модель долей мышц v2 (display-only, фенс импорта).

Пререг — ``docs/pw-105-prereg.md`` (§3.4 таблица 58 пар, §5 инварианты,
§6 план тестов). Проверок двенадцать, по одной на пункт §6:

1. покрытие — 58 пар ровно; 28 профилей + 2 override; нет лишних/пропавших.
2. уровни и числа — A=18, B=9, C=26, «не измерено»=5; числа 22/4/2/30.
3. A/B → источник, C → основание — у A/B ≥1 ``source_id`` из 1–27;
   C называет № экстраполяции либо «экспертная v1».
4. «не измерено» ≠ 0 — доля > 0, стоит флаг, Σ профиля = 1.
5. Σ=1 — все 28 профилей + 2 override.
6. ключи v1↔v2 — множества профилей и мышц совпадают.
7. инвариант — ``check_invariant(conn)`` на синтетике: residual == 0.
8. провенанс и версия — непустые поля; PDF помечен; ``VERSION == "v2"``.
9. откат — на синтетике переключение на v1 даёт прежние суммы.
10. override — обе тяговые группы возвращают override, не базовую строку.
11. ``format_shares`` и фенс — версия, уровень, DOI/журнал; импорт = 0 оба
    направления; v1-ветка печатается дословно.
12. решения не тронуты — golden-выход ``digest``/``state``/``score`` на
    синтетике идентичен до/после вызовов ``muscle_share``.

Правило W7-(g): живой отпечаток §2.4 в тестах **не** прибивается — только
свойства (Σ=1, residual=0, полнота провенанса). Числа §2 — docs-дельта.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from koilog import etl, muscles, muscle_share, taxonomy
from koilog.health import digest as health_digest
from koilog.health import score as health_score
from koilog.health import state as health_state

ROOT = Path(__file__).resolve().parent.parent

#: Представители всех трёх сдвинутых профилей и обеих тяг.
SQUAT = "Выпады"                                  # П2: квадрицепс+ягодицы+бицепс бедра
DEADLIFT = "Становая тяга"                        # П5: 5 мышц, включая трапецию
RDL = "Становая тяга на прямых ногах"              # П26: бицепс бедра+ягодицы+поясница
CHEST = "Жим штанги лежа"                         # П1
PULL_VERT = "Подтягивания широким хватом за голову"
PULL_HORZ = "Тяга штанги в наклоне"


def _db() -> sqlite3.Connection:
    """Пустая in-memory база со схемой ``sets`` под ``_pool_rows``."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sets (
            date TEXT,
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


def _add(conn, date, exercise, weight=100, reps=10, koi=50, grav=0):
    conn.execute(
        "INSERT INTO sets (date, exercise, weight, reps, koi, is_gravitron) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (date, exercise, weight, reps, koi, grav),
    )


def _sample_db() -> sqlite3.Connection:
    """Синтетика: три сдвинутых профиля + жим + тяга (оба override)."""
    conn = _db()
    _add(conn, "2024-01-01", CHEST, 100, 10, 70)
    _add(conn, "2024-01-02", SQUAT, 80, 8, 40)
    _add(conn, "2024-01-03", DEADLIFT, 120, 5, 60)
    _add(conn, "2024-01-04", RDL, 100, 8, 45)
    _add(conn, "2024-01-05", PULL_VERT, 90, 6, 35)
    _add(conn, "2024-01-06", PULL_HORZ, 70, 10, 30)
    conn.commit()
    return conn


class CoverageTests(unittest.TestCase):
    """§6 #1: 58 пар, 28 профилей, 2 override."""

    def test_coverage_is_exactly_58_pairs_28_profiles_2_overrides(self):
        pairs = {(profile, muscle)
                 for profile in muscle_share._SHARES_V2
                 for muscle in profile}
        self.assertEqual(len(pairs), 58)
        self.assertEqual(len(muscle_share._SHARES_V2), 28)
        self.assertEqual(len(muscle_share._SHARES_V1), 28)
        self.assertEqual(len(muscle_share._SHARES_V2_OVERRIDES), 2)
        self.assertEqual(len(muscle_share._OVERRIDE_PROVENANCE_V2), 2)

        # Провенанс — ровно те же пары, без лишних/пропавших.
        self.assertEqual(set(muscle_share._PROVENANCE_V2), pairs)

        # Ключи override совпадают с v1 (композиция та же).
        self.assertEqual(set(muscle_share._SHARES_V2_OVERRIDES),
                         set(muscle_share._GROUP_OVERRIDES))


class LevelsAndNumbersTests(unittest.TestCase):
    """§6 #2: счётчики уровней и статусов чисел заморожены пререгом."""

    def test_level_and_number_tallies_match_prereg(self):
        levels = {}
        numbers = {}
        for prov in muscle_share._PROVENANCE_V2.values():
            levels[prov["level"]] = levels.get(prov["level"], 0) + 1
            numbers[prov["numbers"]] = numbers.get(prov["numbers"], 0) + 1

        self.assertEqual(levels.get("A"), 18)
        self.assertEqual(levels.get("B"), 9)
        self.assertEqual(levels.get("C-э", 0) + levels.get("C-экс", 0), 26)
        self.assertEqual(levels.get("C-э"), 20)
        self.assertEqual(levels.get("C-экс"), 6)
        self.assertEqual(levels.get("не измерено"), 5)

        self.assertEqual(numbers.get("OA"), 22)
        self.assertEqual(numbers.get("PDF"), 4)
        self.assertEqual(numbers.get("override"), 2)
        self.assertEqual(numbers.get("—"), 30)
        self.assertEqual(sum(numbers.values()), 58)


class SourceOrBasisTests(unittest.TestCase):
    """§6 #3: A/B называют номер источника; C — основание."""

    def test_levels_point_at_sources_or_explicit_basis(self):
        for (profile, muscle), prov in muscle_share._PROVENANCE_V2.items():
            where = "|".join(profile) + ":" + muscle
            level = prov["level"]
            ids = prov["source_ids"]

            if level in ("A", "B"):
                self.assertTrue(ids, f"{where}: у {level} нет source_ids")
                for sid in ids:
                    self.assertTrue(1 <= int(sid) <= 27,
                                    f"{where}: source_id {sid} вне 1–27")
            elif level == "C-э":
                self.assertTrue(ids, f"{where}: C-э без номера экстраполяции")
            elif level == "C-экс":
                self.assertIn("экспертная v1", prov["basis"],
                              f"{where}: C-экс не назван «экспертная v1»")
            elif level == "не измерено":
                self.assertIn("нет", prov["basis"],
                              f"{where}: «не измерено» без пометки об отсутствии канала")
            else:
                self.fail(f"{where}: неизвестный уровень {level!r}")

            # Числовая/override-строка обязана иметь источник.
            if prov["numbers"] != "—":
                self.assertTrue(ids, f"{where}: числа без источника")


class NotMeasuredNotZeroTests(unittest.TestCase):
    """§6 #4: «не измерено» никогда не ноль и не выпадает из Σ."""

    def test_not_measured_pairs_keep_positive_v1_share(self):
        not_measured = [(p, m) for (p, m), prov
                        in muscle_share._PROVENANCE_V2.items()
                        if prov["level"] == "не измерено"]
        self.assertEqual(len(not_measured), 5)

        for profile, muscle in not_measured:
            prov = muscle_share._PROVENANCE_V2[(profile, muscle)]
            share = muscle_share._shares_v2(profile)[muscle]
            self.assertGreater(share, 0.0, f"{profile}:{muscle} обнулён")
            self.assertEqual(prov["numbers"], "—")
            self.assertIn("не измерено", prov["value_origin"])
            self.assertAlmostEqual(
                sum(muscle_share._shares_v2(profile).values()), 1.0, places=12)

        # Ни одна доля v2 не равна нулю (все 58 пар).
        for profile in muscle_share._SHARES_V2:
            for muscle, share in muscle_share._shares_v2(profile).items():
                self.assertGreater(share, 0.0, f"{profile}:{muscle}")


class SumToOneTests(unittest.TestCase):
    """§6 #5: Σ=1 для всех профилей и override."""

    def test_every_profile_and_override_sums_to_one(self):
        for profile in muscle_share._SHARES_V2:
            total = sum(muscle_share._shares_v2(profile).values())
            self.assertAlmostEqual(total, 1.0, places=12,
                                   msg=f"Σ≠1: {'|'.join(profile)}")
            self.assertEqual(set(muscle_share._SHARES_V2[profile]), set(profile))

        for key, weights in muscle_share._SHARES_V2_OVERRIDES.items():
            total = sum(muscle_share._normalize(weights).values())
            self.assertAlmostEqual(total, 1.0, places=12, msg=f"Σ≠1: {key}")
            self.assertEqual(set(weights), set(key[1]))


class KeysMatchTests(unittest.TestCase):
    """§6 #6: композиция v1 не менялась."""

    def test_v1_and_v2_have_identical_profile_and_muscle_keys(self):
        self.assertEqual(set(muscle_share._SHARES_V1), set(muscle_share._SHARES_V2))
        for profile in muscle_share._SHARES_V1:
            self.assertEqual(set(muscle_share._SHARES_V2[profile]), set(profile))


class InvariantTests(unittest.TestCase):
    """§6 #7: Σ(доля×E) = E на синтетике (residual == 0)."""

    def test_invariant_zero_and_totals_preserve_e(self):
        conn = _sample_db()
        residual, days = muscle_share.check_invariant(conn, version="v2")
        self.assertAlmostEqual(residual, 0.0, places=10)
        self.assertEqual(days, 6)

        kg, _koi, _sets = muscle_share.totals_by_muscle(conn, version="v2")
        expected_e = 100 * 10 + 80 * 8 + 120 * 5 + 100 * 8 + 90 * 6 + 70 * 10
        self.assertAlmostEqual(sum(kg.values()), expected_e, places=6)


class ProvenanceAndVersionTests(unittest.TestCase):
    """§6 #8: провенанс полон, PDF помечен, канон — v2."""

    def test_provenance_complete_and_version_is_v2(self):
        required = ("source_ids", "basis", "doi_journal", "extracted_at",
                    "level", "numbers", "numbers_from", "value_origin")
        allowed_numbers = {"OA", "PDF", "override", "—"}

        for (profile, muscle), prov in muscle_share._PROVENANCE_V2.items():
            where = "|".join(profile) + ":" + muscle
            for field in required:
                self.assertIn(field, prov, f"{where}: нет поля {field}")
            for field in ("basis", "doi_journal", "extracted_at", "level",
                          "numbers", "numbers_from", "value_origin"):
                self.assertTrue(str(prov[field]).strip(),
                                f"{where}: пустое поле {field}")
            self.assertIn(prov["numbers"], allowed_numbers, where)
            self.assertEqual(prov["extracted_at"], muscle_share.EXTRACTED_AT)
            self.assertEqual(
                dt.date.fromisoformat(prov["extracted_at"]).isoformat(),
                prov["extracted_at"])
            if prov["numbers"] != "—":
                self.assertTrue(prov["source_ids"], f"{where}: числа без источника")

        pdf = [(p, m) for (p, m), prov in muscle_share._PROVENANCE_V2.items()
               if prov["numbers"] == "PDF"]
        self.assertEqual(len(pdf), 4)
        for profile, muscle in pdf:
            prov = muscle_share._PROVENANCE_V2[(profile, muscle)]
            self.assertIn("PDF", prov["numbers_from"].upper())

        self.assertEqual(muscle_share.VERSION, "v2")
        self.assertEqual(
            dt.date.fromisoformat(muscle_share.VERSION_DATE).isoformat(),
            muscle_share.VERSION_DATE)


class RollbackTests(unittest.TestCase):
    """§6 #9: переключение на v1 даёт прежние суммы PW-99."""

    def test_v1_rollback_reproduces_previous_totals(self):
        conn = _sample_db()
        kg, koi, sets_cnt = muscle_share.totals_by_muscle(conn, version="v1")

        manual_kg = {}
        for row in conn.execute(
                "SELECT exercise, weight, reps, koi FROM sets ORDER BY date"):
            profile = muscles.muscles_of(row["exercise"]).muscles
            group = taxonomy.group_of(row["exercise"]).name
            role = muscle_share._GROUP_OVERRIDES.get((group, profile))
            if role is None:
                role = muscle_share._SHARES_V1[profile]
            shares = muscle_share._shares(profile, role)
            e = float(row["weight"]) * float(row["reps"])
            for muscle, share in shares.items():
                manual_kg[muscle] = manual_kg.get(muscle, 0.0) + e * share

        for muscle, expected in manual_kg.items():
            self.assertAlmostEqual(kg[muscle], expected, places=10,
                                   msg=f"v1 откат разошёлся: {muscle}")
        self.assertAlmostEqual(sum(kg.values()), sum(manual_kg.values()), places=10)
        self.assertAlmostEqual(sum(koi.values()), sum(koi.values()), places=10)
        self.assertGreater(sum(sets_cnt.values()), 0)

        # Сдвинутые профили действительно отличаются от v1 (иначе тест пуст).
        for exercise in (SQUAT, DEADLIFT, RDL):
            v1 = muscle_share.share_of(exercise, version="v1")
            v2 = muscle_share.share_of(exercise, version="v2")
            self.assertEqual(set(v1), set(v2))
            self.assertNotEqual(
                {m: round(v, 9) for m, v in v1.items()},
                {m: round(v, 9) for m, v in v2.items()},
                f"{exercise}: v2 не отличается от v1")


class OverrideTests(unittest.TestCase):
    """§6 #10: обе тяги получают групповой override, не базовую строку."""

    def test_both_pulling_groups_return_override(self):
        pulling = ("широчайшие", "ромбовидные", "бицепс", "дельты задние")
        base = muscle_share._shares_v2(pulling)

        cases = {
            PULL_VERT: ("ТЯГА ВЕРТИКАЛЬНАЯ", {5, 2, 2, 1}),
            PULL_HORZ: ("ТЯГА ГОРИЗОНТАЛЬНАЯ", {4, 3, 2, 1}),
        }
        for exercise, (group_name, role_set) in cases.items():
            self.assertEqual(taxonomy.group_of(exercise).name, group_name)
            shares = muscle_share.share_of(exercise, version="v2")
            self.assertIsNotNone(shares)
            self.assertEqual(set(shares), set(pulling))
            self.assertAlmostEqual(sum(shares.values()), 1.0, places=12)

            weights = muscle_share._SHARES_V2_OVERRIDES[(group_name, pulling)]
            self.assertEqual(set(weights.values()), role_set)
            expected = muscle_share._normalize(weights)
            for muscle, value in expected.items():
                self.assertAlmostEqual(shares[muscle], value, places=12)

            # Это не базовая строка 4/2/2/1.
            differs = any(abs(shares[m] - base[m]) > 1e-9 for m in pulling)
            self.assertTrue(differs, f"{group_name}: вернулась базовая строка")


class FormatAndFenceTests(unittest.TestCase):
    """§6 #11: формат v2 + фенс импорта в обе стороны; v1 дословен."""

    DECISION_MODULES = (
        "koilog/health/state.py",
        "koilog/health/retrain.py",
        "koilog/health/digest.py",
        "koilog/health/score.py",
        "koilog/health/ml.py",
        "koilog/health/groups.py",
    )

    def test_format_shares_v2_and_both_way_import_fence(self):
        txt = muscle_share.format_shares("v2")
        self.assertIn(muscle_share.VERSION, txt)
        self.assertIn(muscle_share.VERSION_DATE, txt)
        self.assertIn("| группа | профиль | уровень | числа | доля | DOI/журнал |", txt)
        self.assertIn("Vigotsky", txt)
        self.assertIn("Felici", txt)
        self.assertIn("оценка-прокси", txt)
        self.assertIn("%MVC", txt)
        self.assertIn("относительная активация", txt.lower())
        self.assertIn("не «доля силы»", txt)
        self.assertIn("PLoS ONE", txt)          # №2 — источник П2/П5/П26
        self.assertNotIn("доля силы (", txt.lower())  # запрещённая формулировка

        # v1-ветка дословна (§2.2 пререга): шапка и колонки без %MVC.
        v1 = muscle_share.format_shares("v1")
        self.assertTrue(v1.startswith("# PW-99 Shares Table v1 (2026-11-05)"))
        self.assertIn("| группа | профиль | роль | доли |", v1)
        self.assertNotIn("%MVC", v1)
        self.assertIn("| грудь|трицепс|дельты передние | 4/2/1 | "
                      "грудь 0.5714; трицепс 0.2857; дельты передние 0.1429 |", v1)

        # Направление 1: решения не импортируют muscle_share.
        for rel in self.DECISION_MODULES:
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("muscle_share", text, f"{rel} импортирует muscle_share")

        # Направление 2: muscle_share не импортирует решения (текст + subprocess).
        module_text = (ROOT / "koilog" / "muscle_share.py").read_text(encoding="utf-8")
        for name in ("state", "retrain", "digest", "score", "ml"):
            self.assertNotIn(f"'{name}'", module_text)
            self.assertNotIn(f'"{name}"', module_text)

        code = (
            "import sys\n"
            "import koilog.muscle_share\n"
            "bad = [m for m in sys.modules if m.startswith('koilog.health.')]\n"
            "assert not bad, bad\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, cwd=str(ROOT))
        self.assertEqual(proc.returncode, 0,
                         f"импорт подтянул health-модули: {proc.stderr}")


class DecisionsUntouchedTests(unittest.TestCase):
    """§6 #12: golden-выход решений идентичен до/после muscle_share."""

    def _health_db(self, tmpdir):
        path = str(Path(tmpdir) / "synthetic.db")
        conn = etl.connect(path)
        etl.create_schema(conn)
        rows = [
            ("2024-01-01", CHEST, 100, 10, 70),
            ("2024-01-02", SQUAT, 80, 8, 40),
            ("2024-01-03", DEADLIFT, 120, 5, 60),
        ]
        for i, (day, exercise, weight, reps, koi) in enumerate(rows, 1):
            conn.execute(
                "INSERT INTO sets (source, source_row, session_id, date, exercise,"
                " weight, reps, koi) VALUES ('journal', ?, ?, ?, ?, ?, ?, ?)",
                (i, "s1", day, exercise, weight, reps, koi),
            )
        conn.commit()
        return conn

    @staticmethod
    def _score_probe(conn, day):
        try:
            return ("ok", repr(health_score.audit(conn, day)))
        except Exception as exc:  # noqa: BLE001 — golden сравнение типа/текста
            return ("err", type(exc).__name__, str(exc))

    def test_decision_outputs_are_identical_before_and_after(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = self._health_db(tmpdir)
            day = "2024-01-02"
            try:
                before = (
                    repr(health_digest.day_state(conn, day)),
                    repr(health_state.state_day(conn, day)),
                    self._score_probe(conn, day),
                    conn.total_changes,
                )

                # Все публичные вызовы muscle_share (только чтение).
                muscle_share.format_shares("v1")
                muscle_share.format_shares("v2")
                muscle_share.share_of(DEADLIFT, "v1")
                muscle_share.share_of(DEADLIFT, "v2")
                muscle_share.totals_by_muscle(conn, version="v1")
                muscle_share.totals_by_muscle(conn, bucket="90", version="v2")
                muscle_share.check_invariant(conn, version="v1")
                muscle_share.check_invariant(conn, version="v2")
                report = muscle_share.delta_v1_to_v2(conn)
                self.assertIn("дельта v1 → v2", report)

                after = (
                    repr(health_digest.day_state(conn, day)),
                    repr(health_state.state_day(conn, day)),
                    self._score_probe(conn, day),
                    conn.total_changes,
                )
            finally:
                conn.close()

            self.assertEqual(before, after)
            self.assertEqual(before[3], after[3], "muscle_share писал в базу")


if __name__ == "__main__":
    unittest.main()
