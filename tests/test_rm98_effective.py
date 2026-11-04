"""RM-98: эффективная нагрузка BW-1RM (``docs/rm-98-prereg.md``, §5–§7).

Модель одна: у упражнения «на своём теле» записана только **добавка**, а в
нагрузку идёт ``E = k x тело + добавка``. Семейство отжиманий - ``k = 0.6``,
всё остальное - ``1.0`` (версионная таблица §5 пререга). ``TOTAL_BODYWEIGHT``
и ``ABSOLUTE`` не переписываются, группы без 1RM (``NO_RM``) остаются
бит-в-бит.

Здесь закрывается тест-минимум §7: (а) добавка, (б) пустая добавка, (в)
суммарный вес, (г) снаряд, no-RM vest, гравитрон вне среза, идемпотентность
повторного прогона, винтаж N-2 при ``SPEC_VERSION = 2`` и гард ``return_plan``.
Плюс пин display-чистки ``INTERIM_BW_EXERCISES`` (R5a): это подпись, а не
расчёт, поэтому проверяется только текст.
"""

from __future__ import annotations

import sqlite3
import unittest

from koilog import etl, stats, taxonomy
from koilog.core import one_rm
from koilog.health import retrain, score
from koilog.taxonomy import WeightMode

#: Вес тела в юнит-тестах: константа ``core.BODY_WEIGHT`` по умолчанию.
BODY = 70.0

#: Калибровочный якорь §2.7/§5: медиана отношения при 0.6 равна 0.96.
#: Число продублировано здесь намеренно - это пиксель приёмки, а не расчёт.
_SHARE = 0.6


class ShareTableTests(unittest.TestCase):
    """§5: версионная таблица ``k`` - 0.6 у семейства отжиманий, 1.0 у прочих."""

    def test_pinned_values(self):
        self.assertEqual(etl.EFFECTIVE_BODYWEIGHT_SHARE, _SHARE)
        self.assertEqual(len(etl.PUSHUPS_FAMILY), 4)

    def test_pushup_family_uses_share(self):
        for name in (
            "Отжимания",
            "Отжимания от скамьи",
            "Отжимания от пола",
            "Отжимания трицепсами спиной к скамье",
        ):
            self.assertEqual(etl.bodyweight_share(name), _SHARE, name)

    def test_other_bodyweight_exercises_use_full_body(self):
        self.assertEqual(etl.bodyweight_share("Приседания"), 1.0)
        self.assertEqual(
            etl.bodyweight_share("Подтягивания на перекладине"), 1.0)

    def test_spaces_do_not_change_the_share(self):
        self.assertEqual(etl.bodyweight_share("Отжимания  от пола"), _SHARE)
        self.assertEqual(etl.bodyweight_share(None), 1.0)


class EffectiveLoadTests(unittest.TestCase):
    """(а)–(г), no-RM и провенанс на уровне :func:`etl.resolve_bodyweight`."""

    def test_a_added_weight_gives_share_of_body_plus_added(self):
        r = etl.resolve_bodyweight("Отжимания", 20.0, BODY)
        self.assertEqual(r.mode, WeightMode.VEST_ONLY)
        self.assertEqual(r.weight, 62.0)             # 0.6 x 70 + 20
        self.assertEqual(r.added_weight, 20.0)
        self.assertIsNotNone(one_rm(r.weight, 10))

    def test_a_non_pushup_bodyweight_uses_full_body(self):
        r = etl.resolve_bodyweight("Приседания", 5.0, BODY)
        self.assertEqual(r.mode, WeightMode.VEST_ONLY)
        self.assertEqual(r.weight, 75.0)             # 1.0 x 70 + 5
        self.assertEqual(r.added_weight, 5.0)

    def test_b_empty_added_weight_still_carries_the_body(self):
        """NULL и 0 - это «своего груза нет», а не «вес неизвестен»."""
        for empty in (None, 0.0):
            r = etl.resolve_bodyweight("Отжимания", empty, BODY)
            self.assertEqual(r.weight, 42.0, empty)   # 0.6 x 70
            self.assertEqual(r.added_weight, 0.0)
            self.assertIsNotNone(one_rm(r.weight, 10))

    def test_v_total_bodyweight_is_not_rewritten(self):
        r = etl.resolve_bodyweight("Подтягивания на перекладине", 78.0, BODY)
        self.assertEqual(r.mode, WeightMode.TOTAL_BODYWEIGHT)
        self.assertEqual(r.weight, 78.0)
        self.assertEqual(r.added_weight, 8.0)        # 78 - 70

    def test_g_absolute_is_not_rewritten(self):
        r = etl.resolve_bodyweight("Жим штанги лежа", 100.0, BODY)
        self.assertEqual(r.mode, WeightMode.ABSOLUTE)
        self.assertEqual(r.weight, 100.0)
        self.assertEqual(r.added_weight, 0.0)
        empty = etl.resolve_bodyweight("Жим штанги лежа", None, BODY)
        self.assertIsNone(empty.weight)
        self.assertIsNone(empty.added_weight)

    def test_no_rm_vest_is_not_rewritten(self):
        """Гард стоит до арифметики тела: пресс и планка остаются как записаны."""
        for name, weight in (
            ("Скручивания", 0.0),
            ("Скручивания", 30.0),
            ("Скручивания лежа", None),
            ("Подъем ног в висе", 45.0),
            ("Планка", None),
        ):
            r = etl.resolve_bodyweight(name, weight, BODY)
            self.assertEqual(r.weight, weight, name)
            self.assertIsNone(r.added_weight, name)

    def test_no_rm_absolute_is_not_rewritten(self):
        """No-RM встречается и в ``absolute``: ветка снаряда идёт первой."""
        r = etl.resolve_bodyweight(
            "Скручивания книзу на верхнем блоке", 25.0, BODY)
        self.assertEqual(r.mode, WeightMode.ABSOLUTE)
        self.assertEqual(r.weight, 25.0)
        self.assertEqual(r.added_weight, 0.0)

    def test_provenance_survives_the_rewrite(self):
        """R4: ``raw_weight``/``added_weight`` помнят, что было записано."""
        row = etl._finalize(
            etl.SetRow(
                source=etl.SOURCE_JOURNAL, source_row=1, session_id="s",
                date="2024-03-04", exercise="Отжимания", weight=20.0, reps=10,
            ),
            body_weight=BODY,
        )
        self.assertEqual(row.raw_weight, 20.0)
        self.assertEqual(row.weight, 62.0)
        self.assertEqual(row.added_weight, 20.0)
        self.assertEqual(row.rm, one_rm(62.0, 10))


#: Строки сквозного прогона. Год 2024 без ``total_bodyweight`` - вес тела
#: берётся фолбэком, поэтому ожидания в тестах ниже буквальные.
_ROWS = (
    ("2024-03-04", "Отжимания", 20.0, 10),
    ("2024-03-04", "Отжимания", None, 10),
    ("2024-03-04", "Приседания", 5.0, 20),
    ("2024-03-04", "Скручивания", 0.0, 30),
    ("2024-03-04", "Жим штанги лежа", 100.0, 5),
    ("2019-05-06", "Подтягивания на перекладине", 62.0, 8),
    ("2019-05-07", "Подтягивания на перекладине", 78.0, 8),
)


def _loaded() -> tuple[sqlite3.Connection, dict[int, float]]:
    """Разобрать строки как загрузка и прогнать пересчёт тела + метрики."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    etl.create_schema(conn)
    parsed = [
        etl._finalize(
            etl.SetRow(
                source=etl.SOURCE_JOURNAL, source_row=index, session_id=f"s{index}",
                date=day, exercise=exercise, weight=weight, reps=reps,
            ),
            body_weight=BODY,
        )
        for index, (day, exercise, weight, reps) in enumerate(_ROWS, 1)
    ]
    etl.upsert(conn, parsed)
    applied = etl._refill_bodyweight(conn, None, BODY)
    etl.recompute_metrics(conn)
    return conn, applied


def _by_exercise(conn: sqlite3.Connection, exercise: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sets WHERE exercise = ? ORDER BY id", (exercise,)
    ).fetchall()


class RefillTests(unittest.TestCase):
    """Сквозной прогон «разбор → пересчёт тела → метрики» на синтетике."""

    def setUp(self):
        self.conn, self.applied = _loaded()
        self.addCleanup(self.conn.close)

    def test_pushups_get_effective_load_and_rm(self):
        added, empty = _by_exercise(self.conn, "Отжимания")
        self.assertEqual(added["weight"], 62.0)
        self.assertEqual(added["added_weight"], 20.0)
        self.assertEqual(added["raw_weight"], 20.0)
        self.assertEqual(added["rm"], one_rm(62.0, 10))
        self.assertEqual(empty["weight"], 42.0)
        self.assertEqual(empty["added_weight"], 0.0)
        self.assertIsNone(empty["raw_weight"])
        self.assertEqual(empty["rm"], one_rm(42.0, 10))

    def test_squats_get_rm_from_full_body(self):
        (row,) = _by_exercise(self.conn, "Приседания")
        self.assertEqual(row["weight"], 75.0)
        self.assertEqual(row["rm"], one_rm(75.0, 20))

    def test_no_rm_stays_without_rm(self):
        (row,) = _by_exercise(self.conn, "Скручивания")
        self.assertEqual(row["weight"], 0.0)
        self.assertIsNone(row["added_weight"])
        self.assertIsNone(row["rm"])

    def test_absolute_row_is_untouched(self):
        (row,) = _by_exercise(self.conn, "Жим штанги лежа")
        self.assertEqual(row["weight"], 100.0)
        self.assertEqual(row["added_weight"], 0.0)
        self.assertEqual(row["rm"], one_rm(100.0, 5))

    def test_total_bodyweight_keeps_recorded_weight(self):
        rows = _by_exercise(self.conn, "Подтягивания на перекладине")
        self.assertEqual([r["weight"] for r in rows], [62.0, 78.0])
        self.assertTrue(all(r["weight_mode"] == "total_bodyweight" for r in rows))

    def test_gravitron_is_flagged_and_outside_the_rm_slice(self):
        """4.13: вес помощи не становится нагрузкой и срез 1RM не трогает."""
        rows = _by_exercise(self.conn, "Подтягивания на перекладине")
        gravitron = rows[0]
        self.assertEqual(gravitron["is_gravitron"], 1)
        self.assertEqual(gravitron["weight"], 62.0)   # не 0.6 x тело
        self.assertEqual(gravitron["added_weight"], -16.0)

    def test_second_run_makes_zero_shifts(self):
        """Идемпотентность: повторный пересчёт не двигает ни одной колонки."""
        snapshot_sql = (
            "SELECT id, exercise, weight, added_weight, rm, rm_session, koi,"
            " rm_delta, weight_mode, is_outlier, is_gravitron"
            "  FROM sets ORDER BY id"
        )
        before = [tuple(row) for row in self.conn.execute(snapshot_sql)]
        etl._refill_bodyweight(self.conn, None, BODY)
        etl.recompute_metrics(self.conn)
        after = [tuple(row) for row in self.conn.execute(snapshot_sql)]
        self.assertEqual(before, after)


class SpecVintageTests(unittest.TestCase):
    """R7: ``SPEC_VERSION`` 1 → 2 поднимает новый винтаж через механику N-2."""

    def _ledger(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        score._ensure_schema(conn)
        return conn

    def _insert(self, conn, day, spec):
        conn.execute(
            "INSERT INTO ml_score (day, as_of, computed_at, score, spec_hash,"
            " n_train, n_pos, last_train_day, threshold, alert)"
            " VALUES (?, ?, '2026-01-01T00:00:00+00:00', 0.5, ?, 100, 5,"
            " ?, 0.9, 0)",
            (day, day, spec, day),
        )
        conn.commit()

    def test_version_is_two_and_enters_the_hash(self):
        self.assertEqual(score.SPEC_VERSION, 2)
        current = score.spec_hash()
        saved = score.SPEC_VERSION
        try:
            score.SPEC_VERSION = 1
            previous = score.spec_hash()
        finally:
            score.SPEC_VERSION = saved
        self.assertNotEqual(previous, current)
        self.assertEqual(current, score.spec_hash())

    def test_old_rows_are_marked_vintage_and_not_canon(self):
        conn = self._ledger()
        self.addCleanup(conn.close)
        current = score.spec_hash()
        saved = score.SPEC_VERSION
        try:
            score.SPEC_VERSION = 1
            previous = score.spec_hash()
        finally:
            score.SPEC_VERSION = saved
        self._insert(conn, "2024-01-01", previous)
        self._insert(conn, "2024-01-02", current)

        items = {item["day"]: item for item in score.rows(conn)}
        self.assertTrue(items["2024-01-01"]["vintage"])
        self.assertFalse(items["2024-01-02"]["vintage"])

        canon = score._canonical(conn, spec=current)
        self.assertEqual([row["day"] for row in canon], ["2024-01-02"])

        text = score.format_scores(score.rows(conn))
        self.assertIn("винтаж", text)


class ReturnPlanGuardTests(unittest.TestCase):
    """R7/§2.8: рампа - это рампа **внешнего** веса, не «своего тела»."""

    def _db(self) -> sqlite3.Connection:
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
                wrist_temp REAL
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

    def _add(self, conn, exercise, weight, reps):
        conn.execute(
            "INSERT INTO sets (exercise, date, weight, reps, rm, rm_session,"
            " set_index) VALUES (?, '2024-01-01', ?, ?, ?, ?, 1)",
            (exercise, weight, reps, one_rm(weight, reps), one_rm(weight, reps)),
        )

    def test_bodyweight_exercise_gets_no_barbell_ramp(self):
        conn = self._db()
        self.addCleanup(conn.close)
        self.assertIs(taxonomy.weight_mode_of("Приседания"), WeightMode.VEST_ONLY)
        self._add(conn, "Приседания", 69.0, 20)

        plan = retrain.return_plan(conn, "Приседания", today="2024-01-25")

        self.assertIsNone(plan.program)
        self.assertIsNone(plan.start_weight)
        self.assertIn("внешнего веса", plan.reason)

    def test_total_bodyweight_exercise_gets_no_barbell_ramp(self):
        conn = self._db()
        self.addCleanup(conn.close)
        self._add(conn, "Подтягивания на перекладине", 78.0, 8)

        plan = retrain.return_plan(
            conn, "Подтягивания на перекладине", today="2024-01-25")

        self.assertIsNone(plan.program)
        self.assertIn("внешнего веса", plan.reason)

    def test_absolute_exercise_still_gets_the_ramp(self):
        conn = self._db()
        self.addCleanup(conn.close)
        self._add(conn, "Жим гантелей лежа", 40.0, 8)

        plan = retrain.return_plan(conn, "Жим гантелей лежа", today="2024-01-25")

        self.assertIsNotNone(plan.program)
        self.assertGreater(len(plan.program or ()), 0)


class DisplayCleanupTests(unittest.TestCase):
    """R5a: интерим-пометка убрана - это текст, расчёт не меняется."""

    def test_interim_constant_is_gone(self):
        self.assertFalse(hasattr(stats, "INTERIM_BW_EXERCISES"))

    def test_report_has_no_interim_footnote(self):
        row = stats.ExerciseStats(
            exercise="Отжимания", exercise_group="ОТЖИМАНИЯ", equipment="",
            sessions=1, sets=3, reps_total=30, volume=1000.0,
            first_day="2024-01-01", last_day="2024-01-01", sources="journal",
            timed_sets=0, koi_total=0.0, weight_modes=("vest_only",),
        )
        text = stats.format_report([row])
        self.assertIn("только груз", text)
        self.assertNotIn("интерим", text.casefold())


if __name__ == "__main__":
    unittest.main()
