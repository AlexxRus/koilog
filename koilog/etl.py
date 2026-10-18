"""Загрузка двух выгрузок в одну SQLite: схема, парсеры, метрики, отчёты.

Два источника устроены принципиально по-разному и оба описывают один и тот же
тренировочный дневник:

``data/journal.csv``
    Плоский CSV, разделитель ``;``, 12 колонок, одна строка - один подход.
    Даты ``25.09.26``, есть ``ВРЕМЯ С`` и ``ДИСТАНЦИЯ КМ`` - по ним
    однозначно видно, где упражнение идёт на время.

``data/report.csv``
    Блочный отчёт: сначала заголовки тренировки
    (``Дата`` / ``Программа`` / ``День`` / ``Продолжительность``), затем
    пронумерованные упражнения и подходы под ними. Одна строка - один подход,
    но связи с упражнением нет, она держится на порядке.
    Дополнительно даёт программу и день - в журнале этого нет.

Обе выгрузки грузятся в одну таблицу ``sets`` и различаются колонкой
``source``. Ключ ``(source, source_row)`` делает загрузку идемпотентной:
повторный прогон того же файла обновляет существующие строки, а не плодит
дубликаты, поэтому новую выгрузку можно добавить к истории, не перезаписывая
её целиком.
"""

from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date as date_cls
from pathlib import Path

from . import rules, taxonomy
from .core import BODY_WEIGHT, RM_MAX_REPS, one_rm
from .taxonomy import Group, WeightMode as Mode, normalize

__all__ = [
    "JOURNAL_COLUMNS",
    "LoadReport",
    "OutlierGroup",
    "SetRow",
    "TimedDecision",
    "WeightDecision",
    "QualityReport",
    "bodyweight_for",
    "bodyweight_map",
    "connect",
    "create_schema",
    "detect_timed",
    "estimate_bodyweight",
    "exercise_audit",
    "ExerciseAudit",
    "export_csv",
    "implied_bodyweight",
    "load_all",
    "load_journal",
    "load_workoutreport",
    "outlier_detail",
    "outlier_summary",
    "outlier_totals",
    "parse_journal_date",
    "parse_notes",
    "parse_report_date",
    "parse_report_minutes",
    "quality_report",
    "recompute_metrics",
    "reinterpreted_as_seconds",
    "report_dates",
    "resolve_bodyweight",
    "summary",
    "timed_suspects",
    "unmapped_exercises",
    "upsert",
    "weight_anomalies",
]

# --------------------------------------------------------------------------
# Пути по умолчанию
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "sql" / "training.db"
DEFAULT_EXPORT = ROOT / "export" / "exported_sets.csv"

#: Имена файлов без суффикса выгрузки: два источника в одной папке - это
#: журнал и отчёт, а не «кто-то отгрузил». Старые имена с ``-export``
#: остаются запасными: чужая копия проекта может быть ещё не переименована.
_LEGACY_JOURNAL = ROOT / "data" / "journal-export.csv"
_LEGACY_REPORT = ROOT / "data" / "report-export.csv"


def _first_existing(*paths: Path) -> Path:
    """Первый существующий путь, иначе первый вообще."""
    for path in paths:
        if path.exists():
            return path
    return paths[0]


DEFAULT_JOURNAL = _first_existing(ROOT / "data" / "journal.csv", _LEGACY_JOURNAL)
DEFAULT_REPORT = _first_existing(ROOT / "data" / "report.csv", _LEGACY_REPORT)

#: Значения колонки ``sets.source``. Раньше здесь стояли имена сторонних
#: приложений, которыми велась выгрузка; в базе они остались, поэтому
#: переименование - это ещё и миграция (см. ``_migrate_sources``).
SOURCE_JOURNAL = "journal"
SOURCE_REPORT = "report"

#: Старые имена источников -> новые. ``UNIQUE (source, source_row)`` не
#: даёт переименовать колонку обычным ``UPDATE`` по одной строке базы и
#: получить дубликаты ключа, поэтому вся миграция выполняется одной
#: транзакцией с отложенной проверкой уникальности: сначала все строки
#: временно уходят во временное имя, затем возвращаются под новым.
LEGACY_SOURCES: tuple[tuple[str, str], ...] = (
    ("fitsession", SOURCE_JOURNAL),
    ("workoutreport", SOURCE_REPORT),
)


# --------------------------------------------------------------------------
# Схема
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS sets (
  id             INTEGER PRIMARY KEY,
  source         TEXT    NOT NULL,
  source_row     INTEGER NOT NULL,
  session_id     TEXT    NOT NULL,
  date           TEXT    NOT NULL,
  time           TEXT,
  workout_name   TEXT,
  program        TEXT,
  day            TEXT,
  duration_min   REAL,
  exercise       TEXT    NOT NULL,
  exercise_group TEXT,
  equipment      TEXT,
  weight_mode    TEXT,
  set_index      INTEGER,
  is_warmup      INTEGER NOT NULL DEFAULT 0,
  weight         REAL,
  raw_weight     REAL,
  added_weight   REAL,
  reps           INTEGER,
  time_seconds   INTEGER,
  distance       REAL,
  rest_seconds   REAL,
  is_timed       INTEGER NOT NULL DEFAULT 0,
  timed_reason   TEXT,
  skipped        INTEGER NOT NULL DEFAULT 0,
  note           TEXT,
  is_outlier     INTEGER NOT NULL DEFAULT 0,
  outlier_rule   TEXT,
  rm             REAL,
  rm_session     REAL,
  rm_delta       REAL,
  rm_ma_30d      REAL,
  koi            REAL,
  created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (source, source_row)
);

CREATE INDEX IF NOT EXISTS ix_sets_date     ON sets(date);
CREATE INDEX IF NOT EXISTS ix_sets_exercise ON sets(exercise);
CREATE INDEX IF NOT EXISTS ix_sets_group    ON sets(exercise_group);
CREATE INDEX IF NOT EXISTS ix_sets_session  ON sets(session_id);
CREATE INDEX IF NOT EXISTS ix_sets_source   ON sets(source);
"""

#: Колонки, добавленные к уже существующей базе. ``CREATE TABLE IF NOT EXISTS``
#: их не создаёт, поэтому старая база без этих колонок получала бы ошибку
#: ``no such column`` на первом же INSERT.
#: ``raw_weight`` хранит вес ровно как он записан в выгрузке. Без него второй
#: проход загрузки невозможен: ``weight`` к этому моменту уже переписан в
#: суммарный, и прибавление веса тела второй раз дало бы двойной жилет.
ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("is_outlier", "INTEGER NOT NULL DEFAULT 0"),
    ("outlier_rule", "TEXT"),
    ("raw_weight", "REAL"),
)

#: Недельные суммы КОИ - **по каждому упражнению отдельно**, как в исходной
#: логике КОИ: КОИ считается для упражнения, а не для группы. Складывать в
#: одну сумму жим и тягу только потому, что они в одной группе, нельзя -
#: у каждого упражнения своя база 1RM, а значит и свой масштаб.
#:
#: Полосы недельного КОИ: < 2 - лёгкая неделя, 2…3 - тяжёлая, 3…4 - очень
#: тяжёлая, больше 4 - «Вы сошли с ума??». Пороги те же, что в
#: ``core.KOI_BANDS_WEEK``.
#:
#: Неделя берётся по **ISO 8601** (``%G-W%V``: год и номер недели,
#: начинающейся с понедельника), а не по ``%Y-W%W``. Прежний ``%W`` давал
#: метку на единицу меньше календарной и разрывал одну реальную неделю на
#: стыке года: ``2025-12-29`` попадало в ``2025-W52``, а ``2026-01-01`` - в
#: несуществующую ``2026-W00``, хотя это одна неделя ``2026-W01``. Теперь
#: метка совпадает с календарями и приложениями, и недели не рвутся.
WEEKLY_KOI_VIEW = """
DROP VIEW IF EXISTS v_weekly_koi;
CREATE VIEW v_weekly_koi AS
SELECT strftime('%G-W%V', date)              AS week,
       exercise,
       MIN(exercise_group)                    AS exercise_group,
       COUNT(*)                               AS sets_done,
       SUM(koi)                               AS koi_week,
       CASE WHEN SUM(koi) <  2 THEN 'Лёгкий'
            WHEN SUM(koi) <  3 THEN 'Тяжёлый'
            WHEN SUM(koi) <  4 THEN 'Очень тяжёлый'
            ELSE 'Вы сошли с ума??' END       AS band
FROM sets
WHERE koi IS NOT NULL AND exercise_group IS NOT NULL
GROUP BY week, exercise;
"""

#: КОИ внутри одной тренировки - тоже **по упражнению**, а не по группе.
#: Полосы здесь в разы ниже недельных, потому что это сумма одного упражнения
#: за один вечер; пороги - из ``core.KOI_BANDS``: < 0.4 - мало, 0.4…1 -
#: оптимум, 1…2 - тяжело, больше 2 - перебор.
SESSION_KOI_VIEW = """
DROP VIEW IF EXISTS v_session_koi;
CREATE VIEW v_session_koi AS
SELECT date,
       session_id,
       exercise,
       MIN(exercise_group)                    AS exercise_group,
       COUNT(*)                               AS sets_done,
       SUM(koi)                               AS koi_session,
       CASE WHEN SUM(koi) < 0.4 THEN 'Minimal'
            WHEN SUM(koi) < 1.0 THEN 'Optimal'
            WHEN SUM(koi) < 2.0 THEN 'Load'
            ELSE 'Crazy' END                  AS band
FROM sets
WHERE koi IS NOT NULL AND exercise_group IS NOT NULL
GROUP BY date, session_id, exercise;
"""


# --------------------------------------------------------------------------
# Строка набора
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadReport:
    """Итог загрузки: сколько подходов откуда и что пропущено."""

    journal: int
    report: int
    skipped_dates: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.journal + self.report


@dataclass(slots=True)
class SetRow:
    """Один подход в том виде, в каком он попадёт в БД."""

    source: str
    source_row: int
    session_id: str
    date: str
    exercise: str
    group: Group = field(default_factory=lambda: taxonomy.UNKNOWN)
    weight_mode: Mode = Mode.ABSOLUTE
    time: str | None = None
    workout_name: str | None = None
    program: str | None = None
    day: str | None = None
    duration_min: float | None = None
    set_index: int | None = None
    is_warmup: int = 0
    weight: float | None = None
    raw_weight: float | None = None
    added_weight: float | None = None
    reps: int | None = None
    time_seconds: int | None = None
    distance: float | None = None
    rest_seconds: float | None = None
    is_timed: int = 0
    timed_reason: str | None = None
    skipped: int = 0
    note: str | None = None
    is_outlier: int = 0
    outlier_rule: str | None = None
    rm: float | None = None

    @property
    def exercise_group(self) -> str:
        return self.group.name

    @property
    def equipment(self) -> str:
        return self.group.equipment

    @property
    def counts_rm(self) -> bool:
        """Считается ли для этой группы 1RM (см. ``taxonomy.Group``)."""
        return self.group.counts_rm

    def as_tuple(self) -> tuple:
        return (
            self.source, self.source_row, self.session_id, self.date, self.time,
            self.workout_name, self.program, self.day, self.duration_min,
            self.exercise, self.exercise_group, self.equipment,
            str(self.weight_mode), self.set_index, self.is_warmup,
            self.weight, self.raw_weight, self.added_weight, self.reps, self.time_seconds,
            self.distance, self.rest_seconds, self.is_timed, self.timed_reason,
            self.skipped, self.note, self.is_outlier, self.outlier_rule, self.rm,
        )


_COLUMNS = (
    "source, source_row, session_id, date, time, workout_name, program, day, "
    "duration_min, exercise, exercise_group, equipment, weight_mode, "
    "set_index, is_warmup, weight, raw_weight, added_weight, reps, time_seconds, "
    "distance, rest_seconds, is_timed, timed_reason, skipped, note, "
    "is_outlier, outlier_rule, rm"
)

_INSERT = (
    f"INSERT INTO sets ({_COLUMNS}) VALUES "
    f"({', '.join('?' * 29)}) "
    f"ON CONFLICT(source, source_row) DO UPDATE SET "
    + ", ".join(
        f"{name} = excluded.{name}"
        for name in _COLUMNS.replace(" ", "").split(",")
        if name not in ("source", "source_row")
    )
)


# --------------------------------------------------------------------------
# Подключение и схема
# --------------------------------------------------------------------------


def connect(db_path: str | os.PathLike = DEFAULT_DB) -> sqlite3.Connection:
    """Открыть БД с включёнными внешними ключами и разумными настройками."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """Создать таблицы и представления, если их ещё нет.

    ``DROP TABLE`` здесь нет намеренно: прежняя версия начинала загрузку с
    ``DROP TABLE workouts``, из-за чего каждый прогон ``main()`` уничтожал
    историю вместе с автоинкрементом и ``created_at``, а дозагрузить новую
    выгрузку было невозможно.
    """
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    _migrate_sources(conn)
    conn.executescript(WEEKLY_KOI_VIEW)
    conn.executescript(SESSION_KOI_VIEW)
    conn.commit()


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Добрать в существующую таблицу колонки из ``ADDED_COLUMNS``."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(sets)")}
    for name, spec in ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE sets ADD COLUMN {name} {spec}")


def _migrate_sources(conn: sqlite3.Connection) -> None:
    """Переименовать в ``sets.source`` имена сторонних приложений.

    Идемпотентно: если старого имени в базе нет, запрос не делается вовсе.

    Если под новым именем уже есть строки с теми же номерами, миграция
    поднимает ошибку, а не выбирает что удалить. Такой случай означает, что
    база правилась руками, и автоматическое разрешение здесь - это тихое
    уничтожение одной из двух копий одних и тех же подходов.
    """
    for old, new in LEGACY_SOURCES:
        count = conn.execute(
            "SELECT COUNT(*) FROM sets WHERE source = ?", (old,)
        ).fetchone()[0]
        if not count:
            continue
        clash = conn.execute(
            """
            SELECT COUNT(*)
              FROM sets AS old_rows
              JOIN sets AS new_rows ON new_rows.source_row = old_rows.source_row
             WHERE old_rows.source = ? AND new_rows.source = ?
            """,
            (old, new),
        ).fetchone()[0]
        if clash:
            raise RuntimeError(
                f"в базе одновременно есть источники {old!r} и {new!r} "
                f"с {clash} одинаковыми source_row: переименование "
                f"невозможно без потери строк, разберитесь вручную"
            )
        conn.execute("UPDATE sets SET source = ? WHERE source = ?", (new, old))
        conn.commit()


# --------------------------------------------------------------------------
# Преобразования значений
# --------------------------------------------------------------------------


def _to_float(value: str | None) -> float | None:
    """Число с запятой или точкой. Пустое - ``None``, а не 0."""
    if value is None:
        return None
    text = value.strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: str | None) -> int | None:
    """Целое из строки вида ``10``, ``10.0``. Пустое - ``None``."""
    number = _to_float(value)
    return None if number is None else int(number)


def _to_bool(value: str | None) -> int:
    """``True`` из ``ДА``/``YES``/``1``/``+`` и чисел.

    Прежняя версия писала ``NULL`` вместо ``0`` в 14 694 строках: гард
    ``if row[10].strip()`` обрывал вызов конвертера на пустой строке.
    """
    if value is None:
        return 0
    text = value.strip().upper()
    if not text:
        return 0
    if text in ("ДА", "YES", "Y", "TRUE", "1", "+"):
        return 1
    if text in ("НЕТ", "NO", "N", "FALSE", "0", "-"):
        return 0
    return 1 if _to_float(text) else 0


def _to_time(value: str | None) -> str | None:
    """``ЧЧ:ММ`` -> ``ЧЧ:ММ:СС``, чтобы время в БД сравнивалось как текст."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text.split(":")) == 2:
        return f"{text}:00"
    return text


#: Двузначный год из выгрузки журнала. Порог выбран так, чтобы
#: ``25`` читалось как 2025, а не 1925: ``strptime('%y')`` относит 69...99
#: к предыдущему столетию.
_YEAR_PIVOT = 70


def parse_journal_date(value: str) -> str:
    """``25.09.26`` -> ``2026-09-25``.

    Прежняя версия при ошибке молча возвращала исходную строку, из-за чего в
    текстовой колонке даты оказывались вперемешку два формата и сортировка
    ломалась. Здесь неверная дата - исключение, а не тихий сюрприз.
    """
    text = value.strip()
    try:
        day, month, year = (int(part) for part in text.split("."))
    except ValueError as exc:
        raise ValueError(f"не разобрана дата журнала: {value!r}") from exc

    if year < 100:
        year += 2000 if year < _YEAR_PIVOT else 1900
    try:
        return date_cls(year, month, day).isoformat()
    except ValueError as exc:
        raise ValueError(f"несуществующая дата журнала: {value!r}") from exc


#: Русские месяцы в родительном падеже, как в отчёте: ``23 сент. 2021 г.``.
#: Ключ - первые три буквы, поэтому ``сент.``/``сен.`` и ``июня``/``июл.``
#: попадают в одну ветку.
_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5,
    "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
_REPORT_DATE = re.compile(r"^(\d{1,2})\s+(\S+?)\.?\s+(\d{4})$")
_REPORT_MINUTES = re.compile(r"(\d+(?:[.,]\d+)?)\s*(мин\.|мин|час|ч|сек)", re.I)


def parse_report_date(value: str) -> str:
    """``23 сент. 2021 г.`` -> ``2021-09-23``.

    В исходнике между годом и ``г.`` стоит узкий неразрывный пробел
    (U+202F), поэтому перед разбором текст нормализуется.
    """
    text = normalize(value).rstrip(".")
    # отрезаем хвост вида "2021 г"
    text = re.sub(r"\s*г\.?$", "", text).strip()

    match = _REPORT_DATE.match(text)
    if not match:
        raise ValueError(f"не разобрана дата отчёта: {value!r}")
    day, month_name, year = match.groups()
    prefix = month_name[:3].casefold()
    if prefix not in _MONTHS:
        raise ValueError(f"неизвестный месяц в дате отчёта: {value!r}")
    try:
        return date_cls(int(year), _MONTHS[prefix], int(day)).isoformat()
    except ValueError as exc:
        raise ValueError(f"несуществующая дата отчёта: {value!r}") from exc


def parse_report_minutes(value: str | None) -> float | None:
    """``21 мин.`` -> ``21.0``, ``1 ч 5 мин.`` -> ``65.0``.

    Складываются все найденные компоненты, а не берётся первый: иначе
    ``1 ч 5 мин.`` молча превращалось в 60. В реальных данных формат всегда
    ``N мин.``, но полчаса/час встретятся при следующей выгрузке.
    """
    if value is None:
        return None
    total = 0.0
    found = False
    for amount, unit in _REPORT_MINUTES.findall(value):
        number = float(amount.replace(",", "."))
        unit = unit.casefold()
        if unit in ("ч", "час"):
            total += number * 60.0
        elif unit in ("сек",):
            total += number / 60.0
        else:
            total += number
        found = True
    return total if found else None


# --------------------------------------------------------------------------
# Правила: время вместо повторов и вес тела
# --------------------------------------------------------------------------

#: Порог эвристики «вероятно это секунды, а не повторы». Выше 20 повторов
#: Brzycki всё равно не работает, так что такие значения всё равно не дадут
#: 1RM - но в колонке ``reps`` им самое место.
TIMED_HEURISTIC_REPS = RM_MAX_REPS


@dataclass(frozen=True, slots=True)
class TimedDecision:
    is_timed: bool
    reps: int | None
    time_seconds: int | None
    reason: str | None


def detect_timed(
    exercise: str | None,
    weight: float | None,
    reps: int | None,
    time_seconds: int | None = None,
    distance: float | None = None,
    source: str = SOURCE_JOURNAL,
) -> TimedDecision:
    """Решить, что стоит в колонке повторов: повторы или секунды.

    Решение принимается строго до разбора веса, иначе аномальная строка
    ``Планка 45.0 x 60`` превратится в 70 + 45 = 115 кг вместо честных
    60 секунд.

    Порядок признаков:

    1. Журнал отдаёт время и дистанцию отдельными колонками - это
       однозначный признак, 2 289 строк.
    2. Явный список ``taxonomy.TIMED_EXERCISES`` - для отчёта отчёта, где
       таких колонок нет.
    3. Эвристика ``weight == 0 and reps > 20`` для отчёта: без веса и с
       аномально большим числом это скорее секунды. 87 подходов, из них
       ``Гиперэкстензия`` и ``Обратные скручивания`` попадают в
       ``timed_suspects`` - там это может быть и настоящее число повторов.
    """
    if time_seconds is not None:
        return TimedDecision(True, None, time_seconds, "колонка «время»")
    if distance is not None:
        return TimedDecision(True, None, None, "колонка «дистанция»")
    if taxonomy.is_timed_exercise(exercise):
        return TimedDecision(True, None, reps, "упражнение из списка на время")
    if reps is None:
        return TimedDecision(False, None, None, None)
    if source == SOURCE_REPORT and not weight and reps > TIMED_HEURISTIC_REPS:
        return TimedDecision(True, None, reps, "эвристика: нет веса и >20 «повторов»")
    return TimedDecision(False, reps, time_seconds, None)


#: Причина, по которой записанный «вес» был прочитан как секунды. Такие
#: подходы не выбросы: запись разобрана и попала в ``time_seconds``. Константа
#: нужна, чтобы ``koilog outliers`` мог их пересчитать и сказать «197 подходов
#: разобраны», а не «197 подходов выброшены».
SECONDS_TIMED_REASON = "вес больше 20 в упражнении на своём теле"


@dataclass(frozen=True, slots=True)
class WeightDecision:
    weight: float | None
    added_weight: float | None
    mode: Mode
    reason: str


def resolve_bodyweight(
    exercise: str | None,
    weight: float | None,
    body_weight: float = BODY_WEIGHT,
) -> WeightDecision:
    """Привести вес к суммарному и выделить добавку.

    Одного порога «вес меньше 70 - значит это жилет» здесь недостаточно, и
    данные это показывают:

    * ``Отжимания`` держатся плотным кластером 74...90: это суммарный вес,
      хотя режим объявлен ``VEST_ONLY``. Такие строки помечаются выбросом
      (``rules.PUSHUPS_MAX``) - прибавлять к ним вес тела было бы вторым
      враньём поверх первого;
    * ``Подтягивания на перекладине`` держатся плотным кластером 67...82:
      это вес тела с жилетом, а 68 кг жилета не бывает;
    * ``Гиперэкстензия`` идёт 22.7, 27.2, ... 49.8 с шагом 4.5 - это блины
      тренажёра, вообще не про собственное тело.

    Поэтому режим записи объявлен явно на каждое упражнение
    (``taxonomy.WEIGHT_MODE_OVERRIDE``), а режимы эти противоположны: у
    отжиманий записана только добавка, у подтягиваний - суммарный вес. Именно
    поэтому одного порога не хватает.
    """
    mode = taxonomy.weight_mode_of(exercise)
    if weight is None:
        return WeightDecision(None, None, mode, "вес не указан")

    if mode is Mode.ABSOLUTE:
        return WeightDecision(weight, 0.0, mode, "вес как есть")

    if not taxonomy.counts_rm_of(exercise):
        # Для группы без 1RM вес не переписывается: прибавлять вес тела
        # значит выдумать суммарный вес, которого никто не записывал, а
        # потом ещё и снять с него метрики, которых для пресса и планки нет.
        return WeightDecision(
            weight, None, mode, "группа без 1RM, вес оставлен как записан"
        )

    if weight == 0:
        return WeightDecision(
            None, None, mode, "своё тело без добавки - 1RM не считается"
        )

    if mode is Mode.TOTAL_BODYWEIGHT:
        return WeightDecision(
            weight, weight - body_weight, mode,
            f"записан суммарный вес, добавка = {weight:g} - {body_weight:g}",
        )

    # VEST_ONLY: записана только добавка, своё тело прибавляем.
    return WeightDecision(
        weight + body_weight, weight, mode,
        f"записан жилет {weight:g}, суммарно {weight + body_weight:g}",
    )


# --------------------------------------------------------------------------
# Сборка строки
# --------------------------------------------------------------------------


def _classify(exercise: str | None) -> tuple[Group, Mode]:
    return taxonomy.group_of(exercise), taxonomy.weight_mode_of(exercise)


def _finalize(row: SetRow, body_weight: float = BODY_WEIGHT) -> SetRow:
    """Дополнить разобранную строку: группа, тайминг, вес, 1RM."""
    group, mode = _classify(row.exercise)
    row.group = group
    row.weight_mode = mode

    timed = detect_timed(
        row.exercise, row.weight, row.reps, row.time_seconds, row.distance,
        row.source,
    )
    row.is_timed = 1 if timed.is_timed else 0
    row.timed_reason = timed.reason
    row.reps = timed.reps
    if timed.is_timed:
        row.time_seconds = timed.time_seconds

    # Записанное в выгрузке значение сохраняется до всех переписываний.
    # ``raw_weight`` - это ответ на вопрос «что человек вписал в журнал»,
    # и он не должен меняться от того, что дальше сделала арифметика.
    row.raw_weight = row.weight

    # 1. Правила, которые меняют саму запись, а не отбрасывают её: вес,
    #    прочитанный как секунды, уходит в time_seconds и очищает вес.
    if rules.seconds_in_weight(row.group.name, row.weight):
        row.time_seconds = int(row.weight or 0)
        row.weight = None
        row.reps = None
        row.is_timed = 1
        row.timed_reason = SECONDS_TIMED_REASON

    resolved = resolve_bodyweight(row.exercise, row.weight, body_weight)
    row.weight = resolved.weight
    row.added_weight = resolved.added_weight
    row.weight_mode = resolved.mode

    # 2. Правила, которые помечают строку выбросом. Проверяются после
    #    разбора веса - им нужен режим записи, - но смотрят на значение
    #    как записанное, а не на переписанное.
    #
    #    Подход на времени не проверяется: он и так не попадает ни в 1RM,
    #    ни в скользящее среднее, ни в КОИ. В частности 197 подходов
    #    «подъём ног в висе» и «планка», у которых записанный вес 30…115
    #    прочитан как секунды, - это не выбросы, а разобранные записи.
    #    Помечать их было бы двойным счётом: и в is_timed, и в is_outlier.
    hit = (
        None
        if row.is_timed
        else rules.check_set(
            group=row.group.name,
            weight_mode=str(resolved.mode),
            raw_weight=row.raw_weight,
            reps=row.reps,
        )
    )
    row.is_outlier = 0 if hit is None else 1
    row.outlier_rule = str(hit) if hit is not None else None

    # 3. Метрики. Пропущенный, тайминговый и помеченный выбросом подход
    #    ничего не получает: из них нечего считать.
    row.rm = None
    if (
        row.counts_rm
        and not row.is_timed
        and not row.skipped
        and not row.is_outlier
    ):
        row.rm = one_rm(row.weight, row.reps)
    return row


# --------------------------------------------------------------------------
# Парсер: журнала
# --------------------------------------------------------------------------

JOURNAL_COLUMNS = (
    "date", "time", "workout", "exercise", "distance", "weight", "multiplier",
    "time_s", "reps", "rest_s", "skipped", "note",
)


def _journal_session_id(day: str, clock: str | None, index: int) -> str:
    """Ключ тренировки: дата и время, а при его отсутствии - порядковый номер.

    В выгрузке встречаются тренировки без времени, и иногда в один день их
    две. Без составного ключа «лучший подход за день» смешал бы разные
    тренировки между собой.
    """
    if clock:
        return f"{day}T{clock}"
    return f"{day}#{index}"


def load_journal(
    path: str | os.PathLike = DEFAULT_JOURNAL,
    body_weight: float = BODY_WEIGHT,
) -> Iterator[SetRow]:
    """Разобрать плоскую выгрузку журнала.

    Читается через модуль ``csv`` с ``newline=''`` - в файле 1 531 строка с
    переносом внутри кавычек, построчная обработка на такой выгрузке
    разваливается.
    """
    with io.open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        header = next(reader, None)
        if header is None:
            return
        if [c.strip().upper() for c in header[:4]] != [
            "ДАТА", "ВРЕМЯ", "ТРЕНИРОВКА", "УПРАЖНЕНИЕ"
        ]:
            raise ValueError(
                f"неожиданный заголовок журнала: {header!r}"
            )

        per_session: dict[str, dict[str, int]] = {}
        session_index: dict[str, int] = {}
        for number, fields in enumerate(reader, start=1):
            if not fields or not any(f.strip() for f in fields):
                continue

            def cell(i: int) -> str | None:
                return fields[i].strip() if i < len(fields) and fields[i].strip() else None

            raw_date = cell(0)
            if raw_date is None:
                continue
            day = parse_journal_date(raw_date)
            clock = _to_time(cell(1))
            exercise = normalize(cell(3))
            if not exercise:
                continue

            session_id = _journal_session_id(day, clock, len(session_index))
            if session_id not in session_index:
                session_index[session_id] = len(session_index) + 1

            counter = per_session.setdefault(session_id, {})
            counter[exercise] = counter.get(exercise, 0) + 1

            row = SetRow(
                source=SOURCE_JOURNAL,
                source_row=number,
                session_id=session_id,
                date=day,
                time=clock,
                workout_name=normalize(cell(2)) or None,
                exercise=exercise,
                set_index=counter[exercise],
                is_warmup=1 if normalize(cell(2)) == "Разминка" else 0,
                weight=_to_float(cell(5)),
                reps=_to_int(cell(8)),
                time_seconds=_to_int(cell(7)),
                distance=_to_float(cell(4)),
                rest_seconds=_to_float(cell(9)),
                skipped=_to_bool(cell(10)),
                note=cell(11),
            )
            yield _finalize(row, body_weight)


# --------------------------------------------------------------------------
# Парсер: блочный отчёт
# --------------------------------------------------------------------------

_REPORT_EXERCISE = re.compile(r"^;\s*(?P<number>\d+)\.\s*(?P<name>.+?)\s*$")
_REPORT_KEY = re.compile(r"^(?P<key>[^;]+?);(?P<value>.*)$")

#: Строка подхода - это пять полей: ``;;Вес;x;Пвт``. Разбирается разбиением
#: по ``;``, а не регуляркой: регулярка на три разделителя здесь молча не
#: совпадала, потому что разделителей четыре.
#: (``;;Вес;;Пвт`` из шапки отчёта разбирается так же - с пустым маркером.)
_REPORT_SET_FIELDS = 5
_REPORT_SET_MARKERS = ("x", "х", "х", "X")


def _parse_report_set(line: str) -> tuple[float | None, int | None] | None:
    """Вернуть ``(вес, повторы)`` для строки подхода, иначе ``None``."""
    parts = line.split(";")
    if len(parts) != _REPORT_SET_FIELDS:
        return None
    if parts[0].strip() or parts[1].strip():
        return None
    if parts[3].strip() not in ("x", "х", "X", "Х"):
        return None
    return _to_float(parts[2]), _to_int(parts[4])


def load_workoutreport(
    path: str | os.PathLike = DEFAULT_REPORT,
    body_weight: float = BODY_WEIGHT,
    skip_dates: Iterable[str] = (),
) -> Iterator[SetRow]:
    """Разобрать блочный отчёт отчёта.

    Блок устроен так::

        Дата;23 сент. 2021 г.
        Программа;Диана
        День;день 1
        Продолжительность;21 мин.

        ;1. Жим штанги стоя (армейский жим)
        ;;18.0;x;10

    Связь подхода с упражнением держится только на порядке строк, поэтому
    текущее упражнение хранится в переменной цикла, а не ищется заново.

    Номер тренировки в сутках увеличивается на каждой строке ``Дата``:
    502 блока приходятся на 465 дат, то есть в 37 дней тренировок было по
    две, и без этого они слились бы в одну сессию.

    ``skip_dates`` пропускает даты, уже загруженные из журнала - решение
    владельца: при совпадении дат побеждает журнал, потому что в нём
    больше полей и он продолжается до 2026 года.
    """
    skip = {str(d) for d in skip_dates}
    per_session: dict[str, dict[str, int]] = {}
    session_index: dict[str, int] = {}

    day: str | None = None
    program: str | None = None
    part: str | None = None
    duration: float | None = None
    exercise: str | None = None
    source_row = 0

    with io.open(path, encoding="utf-8-sig") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            if not line.strip():
                # Пустая строка заканчивает упражнение, но не тренировку:
                # между упражнениями их бывает по нескольку.
                continue

            if ";" in line and not line.startswith(";"):
                key_match = _REPORT_KEY.match(line)
                if key_match:
                    key = key_match.group("key").strip()
                    value = key_match.group("value").strip()
                    if key == "Дата":
                        day = parse_report_date(value)
                        program = None
                        part = None
                        duration = None
                        exercise = None
                        session_index[day] = session_index.get(day, 0) + 1
                    elif key == "Программа":
                        program = value or None
                    elif key == "День":
                        part = value or None
                    elif key == "Продолжительность":
                        duration = parse_report_minutes(value)
                    continue

            set_values = _parse_report_set(line)
            if set_values is not None:
                weight, reps = set_values
                if day is None or exercise is None:
                    continue
                if day in skip:
                    continue

                source_row += 1
                session_id = f"{day}#{session_index.get(day, 1)}"

                counter = per_session.setdefault(session_id, {})
                counter[exercise] = counter.get(exercise, 0) + 1

                row = SetRow(
                    source=SOURCE_REPORT,
                    source_row=source_row,
                    session_id=session_id,
                    date=day,
                    workout_name=_report_workout_name(program, part),
                    program=program,
                    day=part,
                    duration_min=duration,
                    exercise=exercise,
                    set_index=counter[exercise],
                    weight=weight,
                    reps=reps,
                )
                yield _finalize(row, body_weight)
                continue

            ex_match = _REPORT_EXERCISE.match(line)
            if ex_match:
                exercise = normalize(ex_match.group("name"))
                continue


def report_dates(path: str | os.PathLike = DEFAULT_REPORT) -> set[str]:
    """Все даты, встречающиеся в отчёте, в виде ``YYYY-MM-DD``."""
    found: set[str] = set()
    with io.open(path, encoding="utf-8-sig") as handle:
        for raw in handle:
            if raw.startswith("Дата;"):
                found.add(parse_report_date(raw.split(";", 1)[1]))
    return found


def _report_workout_name(program: str | None, part: str | None) -> str | None:
    """Собрать название тренировки из программы и дня.

    Отчёт не отдаёт готового названия, а ``Программа``/``День`` меняются от
    тренировки к тренировке. Склеиваем их так же, как журнал склеивает
    ``День N. Группа мышц``, чтобы группировка по тренировкам работала на
    обоих источниках.
    """
    parts = [p for p in (program, part) if p]
    return " / ".join(parts) if parts else None


# --------------------------------------------------------------------------
# Запись
# --------------------------------------------------------------------------


def upsert(
    conn: sqlite3.Connection,
    rows: Iterable[SetRow],
    batch: int = 5000,
) -> int:
    """Записать строки, обновляя уже существующие по ``(source, source_row)``."""
    written = 0
    chunk: list[tuple] = []
    for row in rows:
        chunk.append(row.as_tuple())
        if len(chunk) >= batch:
            conn.executemany(_INSERT, chunk)
            written += len(chunk)
            chunk.clear()
    if chunk:
        conn.executemany(_INSERT, chunk)
        written += len(chunk)
    conn.commit()
    return written


def _require_file(path: str | os.PathLike | None, what: str) -> None:
    """Убедиться, что указанный источник существует.

    ``None`` означает «источника нет» и пропускается. Непустой путь,
    которого нет на диске, - почти всегда опечатка, и молча проглотить её
    опаснее, чем упасть: ``load`` отчитался бы «загружено 0 подходов»,
    и владелец решил бы, что выгрузка пустая.
    """
    if path is None:
        return
    resolved = Path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"файл выгрузки {what} не найден: {resolved}")


def load_all(
    conn: sqlite3.Connection,
    journal: str | os.PathLike | None = DEFAULT_JOURNAL,
    report: str | os.PathLike | None = DEFAULT_REPORT,
    body_weight: float = BODY_WEIGHT,
    journal_wins: bool = True,
    bodyweight_by_year: dict[int, float] | None = None,
) -> LoadReport:
    """Загрузить обе выгрузки в ``sets`` и пересчитать метрики.

    При ``journal_wins`` даты, уже встречающиеся в выгрузке журнала,
    из отчёта пропускаются - так решено дублирование 11 дат на стыке двух
    источников. Список пропущенных дат возвращается в отчёте, а не теряется.

    Источник, равный ``None``, пропускается - так вызывающий говорит «его
    у меня нет». А вот указанный путь, которого нет на диске, - это ошибка,
    и она поднимается: молча пропустить его значило бы пересобрать базу
    в пустую и отчитаться об успехе.

    **Два прохода по весу тела.** Первый проход кладёт строки с константой
    ``body_weight``, чтобы получить в базе вообще всё, включая записи
    ``total_bodyweight``. Второй проход берёт вес тела по годам из этих же
    записей (``estimate_bodyweight``) и пересобирает ``weight``,
    ``added_weight`` и ``rm`` уже с ним.

    Одного прохода было бы достаточно, если бы вес тела был известен заранее.
    Он не известен - его нет ни в одной выгрузке, - а без него суммарный вес
    собственного тела отличается от настоящего на те самые 1...2 кг, которые
    видно в каждой строке. Поэтому два.

    ``bodyweight_by_year`` перекрывает оценку для отдельных лет, а ``None`` в
    значении года означает «взять из данных».
    """
    create_schema(conn)

    _require_file(journal, "журнала")
    _require_file(report, "отчёт")

    fit_rows: list[SetRow] = []
    if journal is not None:
        # Полная перезагрузка источника, а не дозапись. Ключ строки -
        # ``(source, source_row)``: если выгрузку переэкспортировали и
        # нумерация строк сдвинулась, прежние строки этого источника остались
        # бы в базе висеть рядом с новыми, и статистика считалась бы по
        # задвоенным подходам. Пока файл передан, он - единственная правда об
        # источнике, поэтому старые строки удаляются. Источник, равный
        # ``None``, не трогается: его данных у нас нет.
        conn.execute("DELETE FROM sets WHERE source = ?", (SOURCE_JOURNAL,))
        fit_rows = list(load_journal(journal, body_weight))
        upsert(conn, fit_rows)

    report_rows: list[SetRow] = []
    skipped: list[str] = []
    if report is not None:
        conn.execute("DELETE FROM sets WHERE source = ?", (SOURCE_REPORT,))
        known = {r.date for r in fit_rows}
        if not journal_wins:
            known = set()
        overlap = sorted(known & report_dates(report)) if known else []
        skipped = overlap
        report_rows = list(load_workoutreport(report, body_weight, skip_dates=overlap))
        upsert(conn, report_rows)

    _refill_bodyweight(conn, bodyweight_by_year, fallback=body_weight)
    recompute_metrics(conn)
    return LoadReport(
        journal=len(fit_rows),
        report=len(report_rows),
        skipped_dates=skipped,
    )


def _refill_bodyweight(
    conn: sqlite3.Connection,
    overrides: dict[int, float] | None,
    fallback: float,
) -> dict[int, float]:
    """Пересобрать вес, добавку и 1RM с весом тела, взятым из данных.

    Правила (``rules.check_set``) применяются заново, но по записанному весу
    (``raw_weight``), поэтому от нового веса тела они не зависят: 25 кг жилета
    останутся 25 кг жилета, а не превратятся в 95 и не переедут порог.
    Пересчитываются зато ``weight``, ``added_weight`` и ``rm``.

    Возвращает применённую карту ``год -> вес`` - её печатает ``load``.
    """
    years = bodyweight_by_year_overrides(overrides)
    estimates = estimate_bodyweight(conn)
    applied: dict[int, float] = {}
    for year, value in estimates.items():
        applied[year] = years.get(year, value)

    rows = conn.execute(
        """
        SELECT id, date, exercise, raw_weight, reps, weight_mode
          FROM sets
         WHERE weight_mode <> 'absolute'
           AND skipped = 0
           AND is_timed = 0
        """
    ).fetchall()

    updates: list[tuple] = []
    for row in rows:
        try:
            year = int(row["date"][:4])
        except (TypeError, ValueError):  # pragma: no cover - дата всегда есть
            continue
        body = applied.get(year, fallback)
        decision = resolve_bodyweight(row["exercise"], row["raw_weight"], body)
        hit = rules.check_set(
            group=taxonomy.group_of(row["exercise"]).name,
            weight_mode=str(decision.mode),
            raw_weight=row["raw_weight"],
            reps=row["reps"],
        )
        rm = (
            one_rm(decision.weight, row["reps"])
            if hit is None
            and taxonomy.counts_rm_of(row["exercise"])
            and decision.weight is not None
            and decision.weight > 0
            else None
        )
        updates.append((
            decision.weight,
            decision.added_weight,
            rm,
            0 if hit is None else 1,
            str(hit) if hit is not None else None,
            row["id"],
        ))

    conn.executemany(
        "UPDATE sets SET weight = ?, added_weight = ?, rm = ?, "
        "is_outlier = ?, outlier_rule = ? WHERE id = ?",
        updates,
    )
    conn.commit()
    return applied


def bodyweight_by_year_overrides(
    overrides: dict[int, float] | None,
) -> dict[int, float]:
    """Отбросить из переопределений годы со значением ``None``.

    ``--bodyweight-year 2021=none`` означает «взять из данных», а не
    «записать NULL».
    """
    if not overrides:
        return {}
    return {
        int(year): float(value)
        for year, value in overrides.items()
        if value is not None
    }


# --------------------------------------------------------------------------
# Производные метрики
# --------------------------------------------------------------------------

#: Подходы, по которым вообще считаются метрики. Общая часть всех четырёх
#: окон: пропущенный подход (``ПРОПУЩЕН`` в выгрузке), подход на времени и
#: подход, помеченный правилом как выброс, не должны попадать ни в 1RM за
#: тренировку, ни в дельту, ни в скользящее среднее, ни в КОИ. Раньше 268
#: пропущенных строк несли ``koi``, 331 - ``rm_ma_30d`` и 59 - ``rm_delta``,
#: и всё это складывалось в недельные суммы как настоящая нагрузка.
_COUNTS_SQL = """
    FROM sets
   WHERE skipped = 0
     AND is_timed = 0
     AND is_outlier = 0
"""

#: Лучший 1RM упражнения внутри одной тренировки - тот, по которому
#: считается дельта к прошлой тренировке.
_SESSION_WINDOW = f"""
    SELECT id,
           MAX(rm) OVER (
               PARTITION BY session_id, exercise
           ) AS rm_session
    {_COUNTS_SQL}
"""

_DELTA_WINDOW = f"""
    SELECT id,
           rm_session - LAG(rm_session) OVER (
               PARTITION BY exercise
               ORDER BY date, session_id
           ) AS rm_delta
    {_COUNTS_SQL}
"""

#: Скользящее среднее по упражнению за 30 дней. RANGE по julianday, а не
#: ROWS: нужно именно 30 календарных дней, а не 30 строк. Партиция - по
#: **упражнению**, а не по группе: эта величина служит базой 1RM в формуле
#: КОИ, а КОИ считается для упражнения (у жима и тяги в одной группе разные
#: базы, и общая база искажала бы интенсивность обоих).
_MA_WINDOW = f"""
    SELECT id,
           AVG(rm_session) OVER (
               PARTITION BY exercise
               ORDER BY julianday(date)
               RANGE BETWEEN 30 PRECEDING AND CURRENT ROW
           ) AS rm_ma_30d
    {_COUNTS_SQL}
"""

#: КОИ подхода: сколько повторов «стоят» отклонения интенсивности от 100 %.
#:
#: Делить на 100 обязательно: интенсивность - доля (``weight / rm``), а
#: формула ``reps / (1 - интенсивность)`` выведена в процентах, поэтому
#: без ``/ 100`` результат завышался в сто раз - 6 повторов при 60 %
#: давали 15.00 вместо 0.15, и недельная сводка целиком состояла из
#: «Вы сошли с ума??».
#:
#: Порог 98 % вместо 100 %: при интенсивности, стремящейся к 1, знаменатель
#: уходит в ноль и КОИ взлетает до тысяч. Таких строк 180 из 16 849, и это
#: не недобросовестность, а сама природа формулы - её нужно ограничивать.
#: NULL, а не ``inf``: иначе сумма по упражнению обрывалась на первом же
#: подходе к отказу.
_KOI_UPDATE = """
UPDATE sets
   SET koi = reps / ((1.0 - weight / rm_ma_30d) * 100.0)
 WHERE rm_ma_30d IS NOT NULL
   AND reps IS NOT NULL
   AND weight IS NOT NULL
   AND weight > 0
   AND weight <= rm_ma_30d * 0.98
   AND skipped = 0
   AND is_timed = 0
   AND is_outlier = 0
"""


def _run_window_update(conn: sqlite3.Connection, sql: str, column: str) -> None:
    conn.execute(
        f"UPDATE sets SET {column} = NULL WHERE {column} IS NOT NULL"
    )
    conn.execute(
        f"WITH w AS ({sql}) "
        f"UPDATE sets SET {column} = (SELECT {column} FROM w WHERE w.id = sets.id) "
        f"WHERE EXISTS (SELECT 1 FROM w WHERE w.id = sets.id)"
    )
    conn.commit()


def recompute_metrics(conn: sqlite3.Connection) -> None:
    """Пересчитать ``rm_session``, ``rm_delta``, ``rm_ma_30d`` и ``koi``.

    Всё считается четырьмя UPDATE'ами поверх уже загруженных строк, без
    перечитывания файлов. Поэтому добавление новой выгрузки не требует
    перезагрузки истории.
    """
    _run_window_update(conn, _SESSION_WINDOW, "rm_session")
    _run_window_update(conn, _DELTA_WINDOW, "rm_delta")
    _run_window_update(conn, _MA_WINDOW, "rm_ma_30d")

    conn.execute("UPDATE sets SET koi = NULL WHERE koi IS NOT NULL")
    conn.execute(_KOI_UPDATE)
    conn.commit()


# --------------------------------------------------------------------------
# Вес тела по годам
# --------------------------------------------------------------------------


#: Упражнения, записанный вес в которых - это вес собственного тела целиком.
#:
#: Берутся только они: любое другое упражнение с режимом ``total_bodyweight``
#: записывает не тело, а что-то своё. ``Подтягивания широким хватом за голову``
#: в 2021 году записаны как 60 кг шесть раз - это не 60-килограммовый человек,
#: а рычаг, который сам снимает часть веса. Такие строки в оценку не идут.
BODYWEIGHT_SOURCES: tuple[str, ...] = (
    "Подтягивания на перекладине",
    "Подтягивания обратным хватом",
    "Подтягивания узким обратным хватом",
    "Отжимания на брусьях",
    "Трицепсовые отжимания на брусьях",
    "Брусья",
)

#: На сколько оценки разных упражнений могут разойтись, не вызывая вопросов.
BODYWEIGHT_TOLERANCE = 3.0

#: Сколько подходов нужно упражнению в году, чтобы его оценка считалась.
BODYWEIGHT_MIN_SETS = 5

#: «Чистый пол»: нижняя граница упражнения-года берётся вместо моды, только
#: если она плотная - не меньше ``BODYWEIGHT_MIN_SETS`` подходов и не меньше
#: этой доли всех подходов упражнения за год. Разреженный минимум (25.0 в
#: 2018 - один подход весом дополнительного снаряда) моде не конкурент.
BODYWEIGHT_FLOOR_SHARE = 0.20


def estimate_bodyweight(conn: sqlite3.Connection) -> dict[int, float]:
    """Оценка веса собственного тела по годам, из данных а не константы.

    Заменяет ``core.BODY_WEIGHT = 70``, из которой росли 1RM своего тела,
    ``added_weight`` и тексты аномалий. Считать приходится потому, что вес
    тела нигде не выгружается, а записи ``total_bodyweight`` (подтягивания,
    брусья) содержат его вместе с добавкой.

    Считается в два шага.

    **Шаг 1: «чистый пол», иначе мода - по каждому упражнению внутри года.**
    Не минимум и не среднее: сырые минимумы в 2018 равны 25.0, в 2021 - 60.0,
    потому что минимальные строки записаны весом дополнительного снаряда, а
    среднее тянет вниз тот же хвост. Но и одна мода бывает неправа: в 2026
    мода подтягиваний 78 при 112 подходах - это почти всегда жилет, а вот
    нижняя граница в 70 кг стоит 28 подходов из 112. Поэтому берётся самое
    низкое значение упражнения-года, только если оно **плотное**: не меньше
    ``BODYWEIGHT_MIN_SETS`` подходов и не меньше ``BODYWEIGHT_FLOOR_SHARE``
    доли подходов этого упражнения за год («чистый пол»: тело не бывает
    ниже реально записанного плотного кластера, верх - это добавка).
    Разреженный минимум (25.0 - один подход) моде не конкурент, и действует
    прежнее правило: мода. Округление до 0.1 кг: значения вида 68.5 и 68.7
    встречаются по единице раз, и точное совпадение разбило бы группу на
    пустые классы.

    **Шаг 2: год оценивается по упражнению с наибольшим числом подходов, а
    расхождение с остальными разрешается вниз.** Это нужно из-за жилета.
    ``Подтягивания`` и ``Брусья`` в один и тот же год дают разные моды, и
    разница всегда объяснима: в 2021 подтягивания дают 68, а брусья - 78,
    82, 79, 77, 73, 72, 80, 90, 95, 100, и всё это ровно 68 плюс жилет от 4
    до 32 кг. Жилет добавляет, значит верный вес тела - **нижний** кластер,
    а не самый многолюдный верхний. То же в 2020: подтягивания 69, брусья
    69 / 80 / 100.

    Поэтому: если оценки упражнений года разошлись больше чем на
    ``BODYWEIGHT_TOLERANCE``, берётся меньшая; если разошлись меньше - та,
    у которой больше подходов. Оценка не может быть и выше, чем собственное
    тело, и ниже его, так что «вниз» - единственное безопасное направление
    разрешения: лишняя добавка в ``added_weight`` видна как аномалия, а
    отрицательная - как абсурд.
    """
    placeholders = ",".join("?" * len(BODYWEIGHT_SOURCES))
    rows = conn.execute(
        f"""
        SELECT CAST(substr(date, 1, 4) AS INTEGER) AS year,
               exercise,
               ROUND(weight, 1)                   AS bucket,
               COUNT(*)                            AS hits
          FROM sets
         WHERE weight_mode = 'total_bodyweight'
           AND weight IS NOT NULL
           AND weight <= ?                       -- правило BODYWEIGHT_MAX
           AND is_outlier = 0
           AND skipped = 0
           AND exercise IN ({placeholders})
         GROUP BY year, exercise, bucket
         ORDER BY year, exercise, hits DESC, bucket ASC
        """,
        (rules.BODYWEIGHT_MAX, *BODYWEIGHT_SOURCES),
    ).fetchall()

    # year -> {упражнение: {вес: подходов}}
    per_year: dict[int, dict[str, dict[float, int]]] = {}
    for row in rows:
        buckets = per_year.setdefault(row["year"], {}).setdefault(
            row["exercise"], {}
        )
        bucket = row["bucket"]
        buckets[bucket] = buckets.get(bucket, 0) + row["hits"]

    def exercise_estimate(buckets: dict[float, int]) -> tuple[int, float]:
        """Оценка одного упражнения-года: плотный пол, иначе мода.

        Возвращает (подходов выбранного кластера, оценка) - ту же структуру,
        что и раньше хранила ``(hits моды, мода)``, чтобы шаг 2 не менялся.
        """
        lowest = min(buckets)
        if (buckets[lowest] >= BODYWEIGHT_MIN_SETS
                and buckets[lowest] >= BODYWEIGHT_FLOOR_SHARE * sum(buckets.values())):
            # «Чистый пол»: вес тела не бывает ниже реально записанного
            # плотного кластера, а верхние значения - это добавленный жилет.
            return buckets[lowest], lowest
        # При равенстве голосов берётся большее значение: добавка завышает
        # запись, поэтому из двух равновероятных кандидатов верным с большей
        # вероятностью окажется верхний.
        top = max(buckets.items(), key=lambda pair: (pair[1], pair[0]))
        return top[1], top[0]

    result: dict[int, float] = {}
    for year, votes in sorted(per_year.items()):
        estimates = {
            exercise: exercise_estimate(buckets)
            for exercise, buckets in votes.items()
        }
        supported = [
            estimate
            for estimate in estimates.values()
            if estimate[0] >= BODYWEIGHT_MIN_SETS
        ] or list(estimates.values())
        strongest = max(supported, key=lambda pair: pair[0])
        lowest = min(value for _, value in supported)
        if strongest[1] - lowest > BODYWEIGHT_TOLERANCE:
            # упражнения разошлись - верим нижнему кластеру
            result[year] = lowest
        else:
            result[year] = strongest[1]
    return result


def bodyweight_map(
    conn: sqlite3.Connection,
    overrides: dict[int, float] | None = None,
    fallback: float = BODY_WEIGHT,
) -> dict[int, float]:
    """Применённая карта ``год -> вес тела`` целиком.

    То же, что считает ``_refill_bodyweight``, но по уже загруженной базе -
    нужно для печати и для плана, где год берётся последний.
    """
    years = bodyweight_by_year_overrides(overrides)
    estimates = estimate_bodyweight(conn)
    return {year: years.get(year, value) for year, value in estimates.items()}


def bodyweight_for(
    conn: sqlite3.Connection,
    date: str,
    overrides: dict[int, float] | None = None,
    fallback: float = BODY_WEIGHT,
) -> float:
    """Вес тела для даты: переопределение года -> оценка -> константа."""
    if overrides:
        try:
            year = int(date[:4])
        except (TypeError, ValueError):
            year = None
        if year is not None and year in overrides:
            return float(overrides[year])
    try:
        return estimate_bodyweight(conn).get(int(date[:4]), fallback)
    except (TypeError, ValueError):
        return fallback


# --------------------------------------------------------------------------
# Отчёты о качестве данных
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Блок анализа выбросов
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutlierGroup:
    """Подходы, помеченные одним правилом, сжатые до пересказа."""

    rule: str
    sets: int
    exercises: int
    weight_low: float | None
    weight_high: float | None
    first_day: str
    last_day: str

    def format(self) -> str:
        weight = (
            "без веса" if self.weight_low is None
            else (f"{self.weight_low:g} кг" if self.weight_low == self.weight_high
                  else f"{self.weight_low:g}…{self.weight_high:g} кг")
        )
        return (
            f"  {self.rule:20} {self.sets:>5}  упражнений {self.exercises:<3} "
            f"{weight:>16}  {self.first_day} … {self.last_day}"
        )


def outlier_summary(conn: sqlite3.Connection) -> list[OutlierGroup]:
    """Сколько подходов помечено каждым правилом, с диапазонами и датами.

    Обход идёт по ``rules.RULES``, а не по данным, поэтому правило без
    срабатываний попадает в отчёт с нулём: в блоке анализа должно быть видно
    и то, что проверено и оказалось чистым.
    """
    found = {
        row["outlier_rule"]: row
        for row in conn.execute(
            """
            SELECT outlier_rule,
                   COUNT(*)                AS sets,
                   COUNT(DISTINCT exercise) AS exercises,
                   MIN(weight)             AS low,
                   MAX(weight)             AS high,
                   MIN(date)               AS first_day,
                   MAX(date)               AS last_day
              FROM sets
             WHERE is_outlier = 1
               AND outlier_rule IS NOT NULL
             GROUP BY outlier_rule
            """
        )
    }
    out: list[OutlierGroup] = []
    for rule in rules.RULES:
        row = found.get(str(rule.name))
        if row is None:
            out.append(OutlierGroup(str(rule.name), 0, 0, None, None, "", ""))
            continue
        out.append(OutlierGroup(
            rule=str(rule.name),
            sets=row["sets"],
            exercises=row["exercises"],
            weight_low=row["low"],
            weight_high=row["high"],
            first_day=row["first_day"] or "",
            last_day=row["last_day"] or "",
        ))
    # Правила, добавленные в базу мимо rules.RULES (старая база, ручная
    # правка), не должны пропадать из отчёта молча.
    for name, row in found.items():
        if all(g.rule != name for g in out):
            out.append(OutlierGroup(
                rule=name,
                sets=row["sets"],
                exercises=row["exercises"],
                weight_low=row["low"],
                weight_high=row["high"],
                first_day=row["first_day"] or "",
                last_day=row["last_day"] or "",
            ))
    return out


def outlier_detail(
    conn: sqlite3.Connection,
    rule: str | None = None,
    limit: int = 50,
) -> list[tuple]:
    """Отдельные подходы-выбросы по правилам.

    Возвращает строки в том же порядке, в каком их печатает ``koilog outliers``:
    ``(правило, дата, упражнение, группа, вес, повторы, секунды)``. Порядок
    задан здесь, а не в ``cli``, потому что CSV-выгрузка берёт те же строки
    и подставляет их в свой заголовок - если колонки разъедутся, заголовок
    будет врать, а это худший вид поломки в отчёте.

    ``limit <= 0`` означает «без ограничения»: ``LIMIT 0`` в SQL вернул бы
    ноль строк, и выгрузка в CSV оказалась бы пустой именно там, где её
    просят целиком.
    """
    sql = """
        SELECT outlier_rule, date, exercise, exercise_group, weight, reps,
               time_seconds
          FROM sets
         WHERE is_outlier = 1
    """
    params: list[object] = []
    if rule is not None:
        sql += " AND outlier_rule = ?"
        params.append(rule)
    sql += " ORDER BY outlier_rule, exercise, date"
    if limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [tuple(row) for row in conn.execute(sql, params)]


def outlier_totals(conn: sqlite3.Connection) -> tuple[int, int]:
    """(помечено выбросами, всего подходов) - для заголовка блока."""
    marked = conn.execute(
        "SELECT COUNT(*) FROM sets WHERE is_outlier = 1"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    return int(marked), int(total)


def reinterpreted_as_seconds(conn: sqlite3.Connection) -> list[tuple]:
    """Подходы, у которых записанный «вес» прочитан как секунды.

    Это не выбросы: записи разобраны, вес убран, время записано в
    ``time_seconds``. Но их всё равно надо показывать - иначе 197 строк,
    где вместо 45 кг стоит 45 секунд, исчезнут из отчёта молча.

    Возвращает строки ``(упражнение, подходов, от, до)``.
    """
    return [
        tuple(row)
        for row in conn.execute(
            """
            SELECT exercise,
                   COUNT(*)     AS sets,
                   MIN(date)    AS first_day,
                   MAX(date)    AS last_day
              FROM sets
             WHERE timed_reason = ?
             GROUP BY exercise
             ORDER BY sets DESC, exercise
            """,
            (SECONDS_TIMED_REASON,),
        )
    ]


def unmapped_exercises(conn: sqlite3.Connection) -> list[str]:
    """Упражнения в БД, которым не назначена группа."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT exercise FROM sets "
            "WHERE exercise_group IS NULL OR exercise_group = ? "
            "ORDER BY exercise",
            (taxonomy.UNKNOWN.name,),
        )
    ]


def timed_suspects(conn: sqlite3.Connection, limit: int = 200) -> list[tuple]:
    """Подходы, где «повторы» на самом деле могут быть секундами.

    Это эвристика ``weight == 0 and reps > 20`` плюс случаи, где сработал
    явный список на время, но вес при этом ненулевой. Такие строки стоит
    просмотреть руками: ``Гиперэкстензия`` с 35 может означать и 35 секунд
    удержания, и 35 настоящих повторов.
    """
    return [
        tuple(row)
        for row in conn.execute(
            """
            SELECT date, exercise, weight, reps, time_seconds, timed_reason
              FROM sets
             WHERE is_timed = 1
               AND weight IS NOT NULL
               AND weight > 0
             ORDER BY exercise, date
             LIMIT ?
            """,
            (limit,),
        )
    ]


#: Порог для ``weight_anomalies`` при ``total_bodyweight``. Константа
#: ``BODY_WEIGHT`` равна 70, а в 2017...2023 вес тела был около 68, поэтому
#: ``added_weight`` systematically уходит в минус на 1...2 кг - это не
#: ошибка, а дрейф. Порог -5 отсекает 1 000 строк шума и оставляет единичные
#: реальные сбои: 39.0 и 47.3 у подтягиваний (по одному подходу), 25.0 в 2018.
IMPLAUSIBLE_ADDED_WEIGHT = -5.0

#: Максимальный суммарный вес, при котором собственное тело плюс жилет
#: выглядит неправдоподобно. 140 кг при исходном Body Weight 70 - это жилет
#: в 70 кг, чего не бывает.
IMPLAUSIBLE_TOTAL_WEIGHT = BODY_WEIGHT * 2

#: Сколько строк забирать в ``quality_report``. Нужен, потому что SQL требует
#: LIMIT, а считать мы хотим все замечания, а не только первые. С запасом:
#: на полной базе подозрительных строк десятки, не тысячи.
QUALITY_FETCH_LIMIT = 10_000


def weight_anomalies(conn: sqlite3.Connection, limit: int = 200) -> list[tuple]:
    """Строки, где разбор веса выглядит подозрительно.

    Три случая:

    * ``added_weight`` сильно отрицателен при ``TOTAL_BODYWEIGHT`` - записанный
      вес заметно меньше константы собственного тела. У подтягиваний это
      39.0, 47.3 и 59.2 по одному подходу: при константе 70 они читаются как
      «вес тела был 31 кг»;
    * собственное тело плюс жилет даёт неправдоподобно большой вес;
    * есть вес, но нет ни повторов, ни времени - подход не считается ничем.
    """
    return [
        tuple(row)
        for row in conn.execute(
            """
            SELECT date, source, exercise, weight, added_weight, reps,
                   time_seconds, weight_mode
              FROM sets
             WHERE is_outlier = 0
               AND (
                    (weight_mode = 'total_bodyweight'
                     AND added_weight IS NOT NULL
                     AND added_weight < ?)
                 OR (weight_mode = 'vest_only' AND weight > ?)
                 OR (weight IS NOT NULL
                     AND reps IS NULL
                     AND time_seconds IS NULL)
               )
             ORDER BY exercise, date
             LIMIT ?
            """,
            (IMPLAUSIBLE_ADDED_WEIGHT, IMPLAUSIBLE_TOTAL_WEIGHT, limit),
        )
    ]


def implied_bodyweight(conn: sqlite3.Connection) -> list[tuple]:
    """Какой вес тела на самом деле стоит в записях ``total_bodyweight``.

    Диагностика к оценке: показывает разброс внутри года, из которого видно,
    откуда взялась выбранная цифра. Если тело было 68, то константа 70
    завышала добавку на 2 кг на каждом подходе.

    Возвращает ``(год, минимум, оценка, максимум, подходов)``. Средняя
    колонка - это ровно та оценка, которая применена при загрузке, см.
    ``estimate_bodyweight``; минимум и максимум остаются в отчёте не для
    красоты: именно по их разбросу видно, что внутри года в записях есть
    чужой вес снаряда (2018: от 25.0 до 69.0) и жилет (2021: от 68.0 до
    100.0 при теле 68).
    """
    modes = estimate_bodyweight(conn)
    out: list[tuple] = []
    for row in conn.execute(
        """
        SELECT CAST(substr(date, 1, 4) AS INTEGER) AS year,
               MIN(weight)                         AS lowest,
               MAX(weight)                         AS highest,
               COUNT(*)                            AS sets
          FROM sets
         WHERE weight_mode = 'total_bodyweight'
           AND is_outlier = 0
         GROUP BY year
         ORDER BY year
        """
    ):
        out.append((
            row["year"], row["lowest"], modes.get(row["year"]),
            row["highest"], row["sets"],
        ))
    return out


def _describe_anomaly(row: tuple) -> str:
    """Человеческое объяснение строки из ``weight_anomalies``."""
    date, source, name, weight, added, reps, seconds, mode = row
    if mode == "total_bodyweight" and added is not None and added < IMPLAUSIBLE_ADDED_WEIGHT:
        # Вес тела теперь известен по годам (estimate_bodyweight), поэтому
        # «настоящий» вес выводится обратным сложением, а не декларируется
        # невозможным: запись меньше оценки года - похоже на запись жилета.
        return (f"записан суммарный {weight:g}, добавка {added:g} - "
                f"похоже на запись жилета, а не суммарного веса")
    if mode == "vest_only":
        return f"жилет даёт суммарный {weight:g} - неправдоподобно много"
    if reps is None and seconds is None:
        return f"есть вес {weight:g}, но нет ни повторов, ни времени"
    return f"вес {weight:g}, добавка {added}, повторов {reps}"


@dataclass(frozen=True, slots=True)
class ExerciseAudit:
    """Как упражнение легло в базу: источники, режимы веса, выбросы.

    Это «журнал разбора» для ``koilog explain``: сколько подходов пришло из
    каждого файла, какие сработали правила и какие режимы веса встретились.
    """

    exercise: str
    total: int
    with_rm: int
    timed: int
    skipped: int
    warmup: int
    outlier: int
    first_day: str
    last_day: str
    sources: tuple[str, ...]
    outlier_rules: tuple[tuple[str, int], ...]
    weight_modes: tuple[tuple[str, int], ...]
    raw_low: float | None
    raw_high: float | None


def _display_path(path: str | Path) -> str:
    """Путь для отчёта: относительно корня проекта, иначе - только имя файла.

    Абсолютный путь ``C:\\Users\\<имя>\\...`` в выводе ``explain`` бесполезен
    читателю и выдаёт локальную машину, поэтому печатаем путь от корня проекта
    (``data\\journal.csv``). Если файл лежит вне проекта, показываем имя.
    """
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except (ValueError, OSError):
        return Path(path).name


def parse_notes() -> list[str]:
    """Текстовый разбор того, как читаются выгрузки.

    Строки описывают именно парсинг (кодировку, разделитель, приоритет дат,
    правила) - чтобы ``koilog explain`` мог показать их, не заставляя лезть в
    код. Пути печатаются относительно корня проекта, а не как есть: отчёт не
    должен выдавать локальную машину.
    """
    return [
        f"журнал:  {_display_path(DEFAULT_JOURNAL)}",
        f"отчёт:   {_display_path(DEFAULT_REPORT)}",
        "кодировка: utf-8-sig (BOM снимается), разделитель ячеек ';'",
        "даты: журнал - уже ISO (ГГГГ-ММ-ДД); отчёт - «25 сент. 2021 г.» "
        "переводится в ISO",
        "даты, встречающиеся в обоих файлах: берётся журнал, строки отчёта за "
        "этот день пропускаются (журнал подробнее)",
        "«ПРОПУЩЕН» и пустые повторы: подход помечается skipped и в метрики не "
        "входит",
        "разминка: помечается is_warmup и в 1RM/КОИ не входит",
        "вес больше 20 кг в упражнении «на своём теле без веса»: читается как "
        "секунды (rules.seconds_in_weight), переносится в time_seconds",
        "выбросы (rules.check_set): помечаются is_outlier и outlier_rule, "
        "остаются в export, но исключаются из 1RM, дельты, МА, КОИ и объёма",
        "вес собственного тела оценивается по годам из записей "
        "total_bodyweight (см. estimate_bodyweight), константы 70 больше нет",
    ]


def exercise_audit(
    conn: sqlite3.Connection, exercise: str
) -> ExerciseAudit | None:
    """Собрать разбор одного упражнения; ``None``, если его нет в базе."""
    row = conn.execute(
        """
        SELECT COUNT(*)                        AS total,
               COALESCE(SUM(rm IS NOT NULL),0)  AS with_rm,
               COALESCE(SUM(is_timed), 0)       AS timed,
               COALESCE(SUM(skipped), 0)        AS skipped,
               COALESCE(SUM(is_warmup), 0)      AS warmup,
               COALESCE(SUM(is_outlier), 0)     AS outlier,
               MIN(date)                        AS first_day,
               MAX(date)                        AS last_day,
               MIN(raw_weight)                  AS raw_low,
               MAX(raw_weight)                  AS raw_high
          FROM sets
         WHERE exercise = ?
        """,
        (exercise,),
    ).fetchone()
    if row is None or not row["total"]:
        return None

    sources = tuple(
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT source FROM sets WHERE exercise = ? ORDER BY source",
            (exercise,),
        )
    )
    rules = tuple(
        (r[0], r[1])
        for r in conn.execute(
            "SELECT outlier_rule, COUNT(*) FROM sets "
            "WHERE exercise = ? AND outlier_rule IS NOT NULL "
            "GROUP BY outlier_rule ORDER BY 2 DESC",
            (exercise,),
        )
    )
    modes = tuple(
        (r[0] or "—", r[1])
        for r in conn.execute(
            "SELECT weight_mode, COUNT(*) FROM sets WHERE exercise = ? "
            "GROUP BY weight_mode ORDER BY 2 DESC",
            (exercise,),
        )
    )
    return ExerciseAudit(
        exercise=exercise,
        total=row["total"],
        with_rm=row["with_rm"],
        timed=row["timed"],
        skipped=row["skipped"],
        warmup=row["warmup"],
        outlier=row["outlier"],
        first_day=row["first_day"] or "",
        last_day=row["last_day"] or "",
        sources=sources,
        outlier_rules=rules,
        weight_modes=modes,
        raw_low=row["raw_low"],
        raw_high=row["raw_high"],
    )


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Итог проверки данных: что требует ручного решения."""
    unmapped: list[str]
    timed_suspects: list[tuple]
    weight_anomalies: list[tuple]
    implied_bodyweight: list[tuple]
    outliers: list[OutlierGroup] = field(default_factory=list)
    outlier_counts: tuple[int, int] = (0, 0)
    seconds_reinterpreted: list[tuple] = field(default_factory=list)

    @property
    def outlier_sets(self) -> int:
        """Сколько подходов помечено выбросами."""
        return self.outlier_counts[0]

    @property
    def problems(self) -> int:
        """Сколько всего замечаний, а не сколько напечатано."""
        return len(self.unmapped) + len(self.timed_suspects) + len(self.weight_anomalies)

    def format(self, limit: int = 20) -> str:
        """Отчёт для чтения глазами.

        Общая функция, а не одна на CLI: её же зовёт ноутбук, чтобы
        форматирование проблемных данных не расходилось в двух местах.
        """
        lines: list[str] = []
        lines.append(f"нераспознанные упражнения: {len(self.unmapped)}")
        lines += [f"    {name}" for name in self.unmapped[:limit]]

        lines.append(f"подозрительные «повторы» (похоже на секунды): "
                     f"{len(self.timed_suspects)}")
        for date, name, weight, reps, seconds, reason in self.timed_suspects[:limit]:
            lines.append(f"    {date}  {name:36} вес={weight} повт={reps} "
                         f"сек={seconds}  {reason}")

        lines.append(f"неправдоподобная добавка к весу: "
                     f"{len(self.weight_anomalies)}")
        for row in self.weight_anomalies[:limit]:
            lines.append(f"    {row[0]}  {row[2]:36} {row[1]:12}  "
                         f"{_describe_anomaly(row)}")

        lines.append("вес тела по годам (упражнения с суммарным весом):")
        for year, lowest, most, highest, count in self.implied_bodyweight:
            lines.append(
                f"    {year}  минимум {lowest:6.1f}  оценка {most:6.1f}  "
                f"максимум {highest:6.1f}  подходов {count}"
                if most is not None
                else f"    {year}  подходов {count}: оценить нечего"
            )

        if self.outliers:
            marked, total = self.outlier_counts
            share = (marked / total * 100) if total else 0.0
            lines.append("")
            lines.append(
                f"ВЫБРОСЫ: {marked} подходов из {total} ({share:.1f} %)"
                if marked else "ВЫБРОСЫ: нет"
            )
            for group in self.outliers:
                lines.append(group.format())

        if self.seconds_reinterpreted:
            total = sum(row[1] for row in self.seconds_reinterpreted)
            lines.append("")
            lines.append(
                f"ВЕС, ПРОЧИТАННЫЙ КАК СЕКУНДЫ: {total} (не выбросы, "
                f"записи разобраны)"
            )
            for exercise, sets, first_day, last_day in self.seconds_reinterpreted:
                lines.append(f"    {exercise[:44]:<44} {sets:>5}   "
                             f"{first_day} … {last_day}")

        lines.append("")
        lines.append(f"всего замечаний: {self.problems}")
        return "\n".join(lines) + "\n"


def quality_report(conn: sqlite3.Connection) -> QualityReport:
    """Собрать отчёт о качестве данных по загруженной базе.

    Списки забираются целиком, а урезаются уже при печати (``format(limit)``).
    Иначе ``QualityReport.problems`` показывал бы число напечатанных строк, а
    не число проблем, и две разные цифры назывались бы одним словом.
    """
    return QualityReport(
        unmapped=unmapped_exercises(conn),
        timed_suspects=timed_suspects(conn, limit=QUALITY_FETCH_LIMIT),
        weight_anomalies=weight_anomalies(conn, limit=QUALITY_FETCH_LIMIT),
        implied_bodyweight=implied_bodyweight(conn),
        outliers=outlier_summary(conn),
        outlier_counts=outlier_totals(conn),
        seconds_reinterpreted=reinterpreted_as_seconds(conn),
    )


def summary(conn: sqlite3.Connection) -> dict[str, object]:
    """Сводка по загруженным данным: объём, покрытие, пропуски."""
    query = conn.execute(
        """
        SELECT source,
               COUNT(*)                     AS sets,
               COUNT(DISTINCT session_id)   AS sessions,
               COUNT(DISTINCT date)         AS days,
               MIN(date)                    AS first_day,
               MAX(date)                    AS last_day,
               SUM(rm IS NOT NULL)          AS with_rm,
               SUM(rm IS NULL)              AS without_rm,
               SUM(is_timed = 1)            AS timed,
               SUM(skipped = 1)             AS skipped
          FROM sets
         GROUP BY source
         ORDER BY source
        """
    )
    by_source = {
        row["source"]: {k: row[k] for k in row.keys() if k != "source"}
        for row in query
    }
    totals = conn.execute(
        """
        SELECT COUNT(*)                    AS sets,
               COUNT(DISTINCT session_id)   AS sessions,
               COUNT(DISTINCT date)         AS days,
               MIN(date)                    AS first_day,
               MAX(date)                    AS last_day,
               SUM(rm IS NOT NULL)          AS with_rm,
               SUM(rm IS NULL)              AS without_rm,
               SUM(is_timed = 1)            AS timed,
               SUM(skipped = 1)             AS skipped,
               COUNT(DISTINCT exercise)     AS exercises,
               COUNT(DISTINCT exercise_group) AS groups
          FROM sets
        """
    ).fetchone()
    return {
        "by_source": by_source,
        "total": {k: totals[k] for k in totals.keys()},
    }


# --------------------------------------------------------------------------
# Выгрузка в CSV
# --------------------------------------------------------------------------

EXPORT_COLUMNS = (
    "date", "source", "session_id", "workout_name", "program", "day",
    "exercise", "exercise_group", "equipment", "set_index", "is_warmup",
    "weight", "raw_weight", "added_weight", "reps", "time_seconds", "distance",
    "rest_seconds", "is_timed", "skipped", "is_outlier", "outlier_rule",
    "note", "rm", "rm_session", "rm_delta", "rm_ma_30d", "koi",
)


def export_csv(
    conn: sqlite3.Connection,
    path: str | os.PathLike = DEFAULT_EXPORT,
) -> int:
    """Выгрузить обработанные подходы в CSV. Возвращает число строк."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    query = (
        f"SELECT {', '.join(EXPORT_COLUMNS)} FROM sets ORDER BY date, session_id, id"
    )
    written = 0
    # newline='' обязателен, иначе на Windows в файл попадут пустые строки
    # между записями из-за \r\n -> \r\r\n.
    with io.open(target, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(EXPORT_COLUMNS)
        for row in conn.execute(query):
            writer.writerow([row[c] for c in EXPORT_COLUMNS])
            written += 1
    return written
