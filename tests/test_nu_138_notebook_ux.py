"""NU-138: пины ноутбук-UX пакета и display-слоя витрин.

Две группы проверок:

* **структура ноутбуков** - порядок ячеек, единственное поле ``УПРАЖНЕНИЕ``,
  отсутствие заглушки ``names[0]``, перенос витрин 4.14 в ``koilog`` и
  перестановка «Возврат» → ML. Читаются только исходники ячеек, исполнение
  ноутбуков здесь не запускается (у него свой драйвер и render-коммит).
* **display-слой** - резолвер подписи группы, е/ё-фоллбэк истории, компоненты
  верхнего подхода и то, что подпись не меняет ни одного числа. База
  синтетическая или поддельная по минимуму колонок.

Граница решений (``state``/``retrain``/``gate``/``digest``/``score``/``ml``)
проверяется фенсом импортов: ни один из этих модулей не тянет ``muscles`` /
``muscle_load`` / ``stats``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unittest
from pathlib import Path

from koilog import etl, muscle_load, stats, taxonomy

ROOT = Path(__file__).resolve().parents[1]

DELTA_LABELS = {"дельты передние", "дельты средние", "дельты задние"}


# --------------------------------------------------------------------------
# Чтение ноутбуков
# --------------------------------------------------------------------------

def _cells(name: str) -> list[tuple[str, str]]:
    nb = json.loads((ROOT / name).read_text(encoding="utf-8"))
    return [(c["cell_type"], "".join(c["source"])) for c in nb["cells"]]


def _find(cells, needle: str, kind: str | None = None) -> list[int]:
    return [
        i for i, (k, src) in enumerate(cells)
        if (kind is None or k == kind) and needle in src
    ]


def _one(cells, needle: str, kind: str | None = None) -> int:
    hits = _find(cells, needle, kind)
    if len(hits) != 1:
        raise AssertionError(f"{needle!r}: ожидалась одна ячейка, нашли {hits}")
    return hits[0]


# --------------------------------------------------------------------------
# Синтетическая база
# --------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(etl.SCHEMA)
    conn.executescript(etl.WEEKLY_KOI_VIEW)
    return conn


def _add(
    conn: sqlite3.Connection,
    *,
    row: int,
    date: str,
    exercise: str,
    group: str,
    session: str = "s1",
    weight: float | None = 30.0,
    reps: int = 8,
    koi: float | None = 0.5,
    rm: float | None = 30.0,
    rm_session: float | None = 30.0,
    weight_mode: str = "absolute",
    added: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO sets (source, source_row, session_id, date, exercise,
                          exercise_group, equipment, weight_mode, set_index,
                          weight, added_weight, reps, rm, rm_session, koi,
                          skipped, is_warmup, is_timed, is_outlier)
             VALUES ('journal', ?, ?, ?, ?, ?, 'гантели', ?, 1, ?, ?, ?, ?, ?,
                     ?, 0, 0, 0, 0)
        """,
        (row, session, date, exercise, group, weight_mode, weight, added,
         reps, rm, rm_session, koi),
    )
    conn.commit()


# --------------------------------------------------------------------------
# 1. Структура koilog.ipynb (п.1, 3, 4, 5, 7, 8)
# --------------------------------------------------------------------------

class KoiLogNotebookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cells = _cells("koilog.ipynb")

    def test_recent_list_and_field_come_before_growth_program(self):
        recent = _one(self.cells, "format_recent_exercises")
        field = _one(self.cells, 'УПРАЖНЕНИЕ = "')
        growth = _one(self.cells, "stats.format_plan(")
        self.assertLess(recent, field)
        self.assertLess(field, growth)

    def test_field_is_declared_once_and_explicitly(self):
        decls = [
            i for i, (kind, src) in enumerate(self.cells)
            if kind == "code" and re.search(r'^УПРАЖНЕНИЕ = "', src, re.M)
        ]
        self.assertEqual(len(decls), 1)
        self.assertIn('УПРАЖНЕНИЕ = "Жим гантелей лёжа"',
                      self.cells[decls[0]][1])

    def test_four_readers_take_the_shared_field(self):
        # §6 п.1: поле объявлено ровно один раз, а рост/переходы/история/график
        # его только читают. «Разбор» тоже читает (он и раньше брал
        # УПРАЖНЕНИЕ), поэтому читателей не четыре, а пять, - пин не на число,
        # а на отсутствие собственного объявления в любом читателе.
        for needle in (
            "stats.format_plan(",           # программа роста
            "stats.format_transitions(",    # переходы
            "RESOLVED = stats.resolve_exercise(",   # история
            "def draw_rm(",                 # график 1RM
            "etl.exercise_audit(",          # разбор
        ):
            cell = _one(self.cells, needle)
            self.assertIn("УПРАЖНЕНИЕ", self.cells[cell][1], needle)
            self.assertNotRegex(
                self.cells[cell][1], r'^УПРАЖНЕНИЕ\s*=', msg=needle)

    def test_no_first_name_fallback_in_chart(self):
        chart = _one(self.cells, "def draw_rm(")
        src = self.cells[chart][1]
        self.assertNotIn("names[0]", src)
        self.assertNotIn("NAMES or", src)
        self.assertIn("not in NAMES", src)       # честная подсказка вместо подмены

    def test_weekly_groups_after_exercise_weekly(self):
        weekly = _one(self.cells, "stats.weekly_koi(")
        groups = _one(self.cells, "stats.weekly_koi_by_group(")
        self.assertLess(weekly, groups)
        self.assertIn("group_label=muscle_load.group_label",
                      self.cells[groups][1])

    def test_muscle_showcase_moved_after_weekly_groups(self):
        groups = _one(self.cells, "stats.weekly_koi_by_group(")
        header = _one(self.cells, "## Нагрузка по мышцам", kind="markdown")
        ring = min(_find(self.cells, "muscle_load.ring("))
        fresh_table = _one(self.cells, 'print(f"свежесть на')
        self.assertLess(groups, header)
        self.assertLess(header, ring)
        self.assertLess(ring, fresh_table)
        # витрина живёт в koilog целиком: обе половины 4.14 после недельных КОИ
        # (подпись группы в Сводке/неделях - не витрина, она стоит раньше)
        for needle in ("muscle_load.ring(", "muscle_load.week_ring(",
                       "muscle_load.tonnage_by_muscle("):
            for i in _find(self.cells, needle):
                self.assertLess(groups, i, needle)

    def test_muscle_cells_have_their_dependencies(self):
        imports = _one(self.cells, "from koilog.health import")
        src = self.cells[imports][1]
        self.assertIn("refresh", src)
        self.assertIn("text", src)
        ring = _one(self.cells, "muscle_load.ring(")
        self.assertIn("%matplotlib inline", self.cells[ring][1])

    def test_open_questions_cell_is_cleaned(self):
        text = "\n".join(
            src for kind, src in self.cells
            if kind == "markdown" and "О формулах" in src)
        self.assertNotIn("76", text)
        self.assertIn("47", text)
        self.assertIn("2018–2021", text)
        self.assertIn("26", text)
        self.assertIn("Что уже решено", text)
        self.assertIn("Что остаётся открытым", text)

    def test_weight_mode_words_are_in_the_notebook(self):
        text = "\n".join(src for _, src in self.cells)
        self.assertIn("тело+груз", text)
        self.assertIn("только груз", text)


# --------------------------------------------------------------------------
# 2. Структура koilog-health.ipynb (п.6, 7)
# --------------------------------------------------------------------------

class HealthNotebookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cells = _cells("koilog-health.ipynb")

    def test_return_section_goes_before_ml(self):
        ret_header = _one(self.cells, "## Возврат к весам", kind="markdown")
        ret_code = _one(self.cells, "retrain.return_plan(")
        ml_header = _one(self.cells, "## ML:", kind="markdown")
        ml_code = _one(self.cells, "ml.report(conn)")
        self.assertLess(ret_header, ml_header)
        self.assertLess(ret_code, ml_header)
        self.assertLess(ret_header, ml_code)

    def test_muscle_showcase_is_only_a_pointer(self):
        text = "\n".join(src for _, src in self.cells)
        self.assertNotIn("muscle_load", text)
        self.assertIn("переехала в", text)
        self.assertIn("koilog.ipynb", text)

    def test_weight_mode_words_are_in_the_notebook(self):
        text = "\n".join(
            src for kind, src in self.cells if kind == "markdown")
        self.assertIn("тело+груз", text)
        self.assertIn("только груз", text)


class MapTests(unittest.TestCase):
    def test_map_names_the_new_showcase_place(self):
        text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("переехали в `koilog`", text)


# --------------------------------------------------------------------------
# 3. Резолвер группы (п.4)
# --------------------------------------------------------------------------

class GroupLabelTests(unittest.TestCase):
    def test_delta_members_get_subgroup_labels(self):
        members = [
            name for name in taxonomy.EXERCISE_GROUP
            if taxonomy.group_of(name).name == muscle_load.DELTA_GROUP
        ]
        self.assertTrue(members)
        for name in members:
            label = muscle_load.group_label(name)
            self.assertIn(label, DELTA_LABELS, name)

    def test_other_group_with_delta_muscle_keeps_its_own_group(self):
        # фенс пререга §2.2-1: у жима лёжа в профиле «дельты передние»,
        # но группа таксономии другая - подпись меняться не должна
        self.assertEqual(muscle_load.group_label("Жим гантелей лёжа"),
                         "ЖИМ ЛЁЖА")
        self.assertIn("дельты передние",
                      __import__("koilog.muscles", fromlist=["x"])
                      .muscles_of("Жим гантелей лёжа").muscles)

    def test_unknown_name_returns_none(self):
        self.assertIsNone(muscle_load.group_label("Нет такого упражнения"))


class ReportDeltasTests(unittest.TestCase):
    ROWS = [
        # (дата, упражнение, группа, koi одного подхода)
        # все три дельтовидных упражнения — в одной неделе (2024-W01),
        # иначе разбивка по подгруппам сравнивает разные недели
        ("2024-01-03", "Подъёмы рук в стороны", "ДЕЛЬТЫ", 2.0),
        ("2024-01-03", "Обратные разведения одной рукой", "ДЕЛЬТЫ", 1.0),
        ("2024-01-03", "Жим гантелей лёжа", "ЖИМ ЛЁЖА", 1.5),
        ("2024-01-03", "Разведение рук стоя", "ДЕЛЬТЫ", 1.5),
    ]

    def setUp(self):
        self.conn = _db()
        self.addCleanup(self.conn.close)
        for i, (day, exercise, group, koi) in enumerate(self.ROWS, start=1):
            _add(self.conn, row=i, date=day, exercise=exercise, group=group,
                 koi=koi, session=f"s{i}")

    def _report(self, label):
        rows = stats.exercise_report(self.conn, order="exercise")
        return stats.format_report(rows, group_label=label)

    def test_report_shows_delta_labels(self):
        text = self._report(muscle_load.group_label)
        self.assertIn("дельты средние", text)
        self.assertIn("дельты задние", text)
        self.assertIn("ЖИМ ЛЁЖА", text)
        # строка жима лёжа не должна получить дельтовидную подпись
        line = next(ln for ln in text.splitlines()
                    if ln.startswith("Жим гантелей лёжа"))
        self.assertNotIn("дельты", line)

    def test_numbers_do_not_depend_on_the_label(self):
        plain = self._report(None)
        tagged = self._report(muscle_load.group_label)

        def tail(text, name):
            # имя в таблице усечено по ширине колонки - сверяем по префиксу
            line = next(ln for ln in text.splitlines()
                        if ln.startswith(name[:20]))
            return line.split()[-7:]        # подходы…объём - числа

        for _, exercise, _, _ in self.ROWS:
            self.assertEqual(tail(tagged, exercise), tail(plain, exercise),
                             exercise)

    def test_weekly_groups_take_the_same_koi_weeks(self):
        plain = stats.weekly_koi_by_group(self.conn)
        tagged = stats.weekly_koi_by_group(
            self.conn, group_label=muscle_load.group_label)

        # без подписи: "ДЕЛЬТЫ" - простое среднее трёх упражнений недели
        delta = next(r for r in plain
                     if r.exercise_group == "ДЕЛЬТЫ" and r.week == "2024-W01")
        self.assertAlmostEqual(delta.koi_mean, (2.0 + 1.0 + 1.5) / 3)

        # с подписью: средние - среднее 2.0 и 1.5 из тех же koi_week
        middle = next(r for r in tagged
                      if r.exercise_group == "дельты средние"
                      and r.week == "2024-W01")
        self.assertAlmostEqual(middle.koi_mean, (2.0 + 1.5) / 2)
        self.assertAlmostEqual(middle.koi_max, 2.0)
        self.assertEqual(middle.exercises, 2)

        back = next(r for r in tagged
                    if r.exercise_group == "дельты задние"
                    and r.week == "2024-W01")
        self.assertAlmostEqual(back.koi_mean, 1.0)


# --------------------------------------------------------------------------
# 4. История: е/ё-резолвер и компоненты верхнего подхода (п.2, 5)
# --------------------------------------------------------------------------

class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.conn = _db()
        self.addCleanup(self.conn.close)
        rows = [
            ("Подъёмы рук в стороны", "ДЕЛЬТЫ", 2),
            ("Жим гантелей лёжа", "ЖИМ ЛЁЖА", 1),
            ("Жим гантелей лежа", "ЖИМ ЛЁЖА", 3),
        ]
        i = 0
        for exercise, group, count in rows:
            for _ in range(count):
                i += 1
                _add(self.conn, row=i, date="2024-01-03",
                     exercise=exercise, group=group, session=f"s{i}")

    def test_missing_yo_variant_is_found(self):
        self.assertEqual(stats.resolve_exercise(self.conn, "Подъемы рук в стороны"),
                         "Подъёмы рук в стороны")

    def test_exact_match_always_wins(self):
        self.assertEqual(stats.resolve_exercise(self.conn, "Жим гантелей лежа"),
                         "Жим гантелей лежа")
        self.assertEqual(stats.resolve_exercise(self.conn, "Жим гантелей лёжа"),
                         "Жим гантелей лёжа")

    def test_two_stored_variants_are_not_merged(self):
        rows = stats.history(self.conn, "Жим гантелей лежа")
        self.assertEqual(len(rows), 3)
        rows = stats.history(self.conn, "Жим гантелей лёжа")
        self.assertEqual(len(rows), 1)

    def test_unknown_name_comes_back_as_is(self):
        self.assertEqual(stats.resolve_exercise(self.conn, "Нет такого"),
                         "Нет такого")
        self.assertEqual(stats.history(self.conn, "Нет такого"), [])

    def test_history_header_prints_the_resolved_name(self):
        rows = stats.history(self.conn, "Подъемы рук в стороны")
        text = stats.format_history(rows, "Подъёмы рук в стороны")
        self.assertIn("Подъёмы рук в стороны", text.splitlines()[0])

    def test_exercise_audit_stays_exact(self):
        # п.2 не трогает etl.exercise_audit: фоллбэк живёт только в истории
        self.assertIsNone(
            etl.exercise_audit(self.conn, "Подъемы рук в стороны"))


class HistoryDisplayTests(unittest.TestCase):
    def setUp(self):
        self.conn = _db()
        self.addCleanup(self.conn.close)
        _add(self.conn, row=1, date="2024-01-03", exercise="Отжимания",
             group="ОТЖИМАНИЯ", weight=90.0, reps=5, added=20.0,
             weight_mode="total_bodyweight", rm=100.0, rm_session=100.0)
        _add(self.conn, row=2, date="2024-01-10", exercise="Отжимания",
             group="ОТЖИМАНИЯ", weight=20.0, reps=8, added=None,
             weight_mode="vest_only", rm=25.0, rm_session=25.0)
        _add(self.conn, row=3, date="2024-01-17", exercise="Отжимания",
             group="ОТЖИМАНИЯ", weight=None, reps=10, added=None,
             weight_mode="vest_only", rm=None, rm_session=None)
        _add(self.conn, row=4, date="2024-01-24", exercise="Отжимания",
             group="ОТЖИМАНИЯ", weight=90.0, reps=5, added=None,
             weight_mode="total_bodyweight", rm=100.0, rm_session=100.0)
        self.rows = stats.history(self.conn, "Отжимания")

    def test_total_bodyweight_shows_components(self):
        text = stats.format_history(self.rows, "Отжимания")
        self.assertIn("90 (70+20)", text)

    def test_vest_only_shows_the_added_weight_and_its_label(self):
        text = stats.format_history(self.rows, "Отжимания")
        self.assertIn("20 кг x 8", text)
        self.assertIn("только груз", text)

    def test_unknown_component_is_an_honest_dash(self):
        row = next(r for r in self.rows if r.date == "2024-01-24")
        self.assertEqual(row.top_set, "—")
        row = next(r for r in self.rows if r.date == "2024-01-17")
        self.assertEqual(row.top_set, "—")

    def test_top_row_labels(self):
        row = next(r for r in self.rows if r.date == "2024-01-03")
        self.assertEqual(row.top_set, "90 (70+20)")
        self.assertEqual(row.top_semantics, "тело+груз")
        row = next(r for r in self.rows if r.date == "2024-01-10")
        self.assertEqual(row.top_semantics, "только груз")


# --------------------------------------------------------------------------
# 5. Фенсы: справочник мышц не протекает в решения
# --------------------------------------------------------------------------

class ImportFenceTests(unittest.TestCase):
    DECISION_MODULES = (
        "koilog/health/state.py",
        "koilog/health/retrain.py",
        "koilog/health/digest.py",
        "koilog/health/score.py",
        "koilog/health/ml.py",
        "koilog/health/groups.py",
    )

    def test_decision_paths_do_not_import_showcase_modules(self):
        for rel in self.DECISION_MODULES:
            with self.subTest(module=rel):
                text = (ROOT / rel).read_text(encoding="utf-8")
                self.assertNotIn("muscle_load", text)
                self.assertNotIn("from ..muscles", text)
                self.assertNotIn("from koilog import stats", text)

    def test_stats_does_not_import_muscles(self):
        # справочник мышц остаётся за фенсом: stats получает резолвер
        # вызывающим (callable), а не импортом. Слово в докстринге - не импорт.
        text = (ROOT / "koilog" / "stats.py").read_text(encoding="utf-8")
        self.assertNotRegex(
            text, r"(?m)^\s*(?:from|import)\s+\S*\b(?:muscles|muscle_load)\b")


if __name__ == "__main__":
    unittest.main()
