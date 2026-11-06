# -*- coding: utf-8 -*-
"""Паттерны тренировочных программ — display-only.

Пререг: ``docs/pg-100-prereg.md``. Модуль отвечает на вопрос «какие типы дня
повторяются и в каком порядке», не оценивая качество программы.

Единица — **тренировочный день**, а не сессия: день с силовой и кардио-сессией
остаётся одним днём. Пул — рабочие подходы без разминки, пропусков, выбросов и
чужих строк. **Кардио** исключается из силовой сигнатуры и показывается
отдельным флагом ``+кардио``.

Уровни сигнатуры (``SIGNATURE_LEVELS``):

* ``name`` (L1) — имена тренировок дня;
* ``group`` (L2) — группы таксономии;
* ``set`` (L3) — набор упражнений (порядок не важен);
* ``ordered`` (L3o) — последовательность упражнений.

Имена тренировок берутся **как хранятся** (вендорский фид — канон); таксономия
применяется только к упражнениям, не к именам тренировок.

Модуль **только читает** базу и не входит в решения — decision-модули его не
импортируют; сам он не импортирует модули нагрузки (``muscle_load``,
``muscle_share``, ``work_arc``). Фенс проверяется тестами.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date as date_cls, timedelta
from typing import Iterable, Iterator, Sequence

from . import taxonomy

__all__ = [
    "CURRENT_WINDOW_DAYS",
    "CYCLE_MAX_GAP",
    "DEFAULT_CAP",
    "DEFAULT_MIN_COVERAGE",
    "LEVEL_LABELS",
    "SIGNATURE_LEVELS",
    "AdherenceReport",
    "EraRow",
    "LagStat",
    "NewDay",
    "Template",
    "TemplateLibrary",
    "Transition",
    "TransitionReport",
    "adherence",
    "current_window",
    "cycle",
    "day_signature",
    "era_table",
    "format_adherence",
    "format_cycle",
    "format_eras",
    "format_templates",
    "has_cardio",
    "templates",
]

#: Уровни сигнатуры в каноническом порядке.
SIGNATURE_LEVELS: tuple[str, ...] = ("name", "group", "set", "ordered")

#: Подписи уровней для рендера.
LEVEL_LABELS: dict[str, str] = {
    "name": "L1 (имена)",
    "group": "L2 (группы)",
    "set": "L3 (набор)",
    "ordered": "L3o (порядок)",
}

#: Доля дней эры, которую обязан покрыть адаптивный K.
DEFAULT_MIN_COVERAGE: float = 0.9

#: Верхняя граница K (сколько шаблонов показывать).
DEFAULT_CAP: int = 10

#: Разрыв в календаре, после которого переход не считается сменой программы.
CYCLE_MAX_GAP: int = 14

#: Длина «текущего окна» в календарных днях от последнего тренировочного дня.
CURRENT_WINDOW_DAYS: int = 90

_CARDIO_GROUP = "КАРДИО"

_LEVEL_ALIASES: dict[str, str] = {
    "name": "name",
    "l1": "name",
    "group": "group",
    "l2": "group",
    "set": "set",
    "l3": "set",
    "ordered": "ordered",
    "l3o": "ordered",
}

_POOL_SQL = """
    SELECT date, session_id, source_row, id, workout_name, exercise
      FROM sets
     WHERE date IS NOT NULL
       AND is_warmup = 0
       AND skipped = 0
       AND is_outlier = 0
       AND is_foreign = 0
"""


def _level_key(level: str) -> str:
    """Нормализовать имя уровня (``L1``/``l3o``/``name``) к ключу."""
    key = str(level).strip().lower()
    if key not in _LEVEL_ALIASES:
        raise ValueError(
            f"неизвестный уровень {level!r}; допустимо: {SIGNATURE_LEVELS}"
        )
    return _LEVEL_ALIASES[key]


@dataclass(slots=True)
class _Day:
    """Один тренировочный день: сырьё для всех четырёх сигнатур."""

    names: set[str] = field(default_factory=set)
    groups: set[str] = field(default_factory=set)
    exercises: set[str] = field(default_factory=set)
    ordered: list[str] = field(default_factory=list)
    cardio: bool = False

    def add_strength(self, workout_name: str, exercise: str) -> None:
        self.names.add(workout_name or "—")
        self.groups.add(taxonomy.group_of(exercise).name)
        if exercise not in self.exercises:
            self.exercises.add(exercise)
            self.ordered.append(exercise)

    def signature(self, level: str) -> tuple[str, ...]:
        if level == "name":
            return tuple(sorted(self.names))
        if level == "group":
            return tuple(sorted(self.groups))
        if level == "set":
            return tuple(sorted(self.exercises))
        return tuple(self.ordered)

    @property
    def first(self) -> str | None:
        return self.ordered[0] if self.ordered else None


def _read_days(
    conn: sqlite3.Connection,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, _Day]:
    """Прочитать пул и свернуть в дни (порядок дат — возрастающий)."""
    sql = _POOL_SQL
    params: list[str] = []
    if since is not None:
        sql += " AND date >= ?"
        params.append(since)
    if until is not None:
        sql += " AND date <= ?"
        params.append(until)
    sql += " ORDER BY date, session_id, source_row, id"

    days: dict[str, _Day] = {}
    for row in conn.execute(sql, params):
        day = row[0]
        workout_name = row[4]
        exercise = row[5]
        record = days.get(day)
        if record is None:
            record = days[day] = _Day()
        if taxonomy.group_of(exercise).name == _CARDIO_GROUP:
            record.cardio = True
            continue
        record.add_strength(workout_name, exercise)
    return days


def has_cardio(conn: sqlite3.Connection, day: str) -> bool:
    """Есть ли в дне кардио-строка (флаг ``+кардио``, вне сигнатуры)."""
    record = _read_days(conn, since=day, until=day).get(day)
    return bool(record and record.cardio)


def day_signature(
    conn: sqlite3.Connection,
    day: str,
    level: str = "name",
) -> tuple[str, ...]:
    """Сигнатура одного тренировочного дня.

    Кардио-строки в сигнатуру не входят; пустой день (нет рабочих подходов)
    даёт пустой кортеж. Порядок элементов детерминирован: для ``name``,
    ``group``, ``set`` — лексикографический, для ``ordered`` — порядок первого
    появления упражнения в дне.
    """
    key = _level_key(level)
    record = _read_days(conn, since=day, until=day).get(day)
    if record is None:
        return ()
    return record.signature(key)


def current_window(
    conn: sqlite3.Connection,
    days: int = CURRENT_WINDOW_DAYS,
) -> tuple[str | None, str | None]:
    """Окно ``days`` календарных дней от ``MAX(date)``: ``(since, until)``."""
    if days < 1:
        raise ValueError("длина окна должна быть >= 1")
    anchor = conn.execute(
        """
        SELECT MAX(date) FROM sets
         WHERE date IS NOT NULL
           AND is_warmup = 0
           AND skipped = 0
           AND is_outlier = 0
           AND is_foreign = 0
        """
    ).fetchone()[0]
    if anchor is None:
        return None, None
    since = (date_cls.fromisoformat(anchor) - timedelta(days=days - 1)).isoformat()
    return since, anchor


@dataclass(frozen=True, slots=True)
class Template:
    """Шаблон эры: сигнатура, её дни и состав."""

    signature: tuple[str, ...]
    days: int
    share: float
    exercises: tuple[tuple[str, int], ...]
    first: str | None
    first_counts: tuple[tuple[str, int], ...]
    cardio_days: int

    @property
    def cardio_share(self) -> float:
        return self.cardio_days / self.days if self.days else 0.0


@dataclass(frozen=True, slots=True)
class TemplateLibrary:
    """Библиотека эры: K шаблонов и полный перечень «прочего»."""

    level: str
    since: str | None
    until: str | None
    total_days: int
    k: int
    min_coverage: float
    templates: tuple[Template, ...]
    others: tuple[Template, ...]

    @property
    def covered_days(self) -> int:
        return sum(template.days for template in self.templates)

    @property
    def coverage(self) -> float:
        return self.covered_days / self.total_days if self.total_days else 0.0

    def __iter__(self) -> Iterator[Template]:
        return iter(self.templates)

    def __len__(self) -> int:
        return len(self.templates)

    def __getitem__(self, index):
        return self.templates[index]


def _select_k(
    counts: Sequence[int],
    total: int,
    min_coverage: float,
    cap: int,
) -> tuple[int, int]:
    """Минимальный K с покрытием >= ``min_coverage``, но не больше ``cap``."""
    if not (0.0 < min_coverage <= 1.0):
        raise ValueError("min_coverage должен быть в (0, 1]")
    if cap < 1:
        raise ValueError("cap должен быть >= 1")
    if total <= 0:
        return 0, 0
    limit = min(len(counts), cap)
    cumulative = 0
    for index in range(limit):
        cumulative += counts[index]
        if cumulative / total >= min_coverage:
            return index + 1, cumulative
    return limit, cumulative


def _make_template(
    signature: tuple[str, ...],
    records: Sequence[_Day],
    total: int,
) -> Template:
    exercise_counts: Counter[str] = Counter()
    first_counts: Counter[str] = Counter()
    cardio_days = 0
    for record in records:
        exercise_counts.update(record.exercises)
        if record.first is not None:
            first_counts[record.first] += 1
        if record.cardio:
            cardio_days += 1
    exercises = tuple(
        sorted(exercise_counts.items(), key=lambda item: (-item[1], item[0]))
    )
    firsts = tuple(
        sorted(first_counts.items(), key=lambda item: (-item[1], item[0]))
    )
    days = len(records)
    return Template(
        signature=signature,
        days=days,
        share=days / total if total else 0.0,
        exercises=exercises,
        first=firsts[0][0] if firsts else None,
        first_counts=firsts,
        cardio_days=cardio_days,
    )


def _build_library(
    days: dict[str, _Day],
    level: str,
    since: str | None,
    until: str | None,
    min_coverage: float,
    cap: int,
) -> TemplateLibrary:
    grouped: dict[tuple[str, ...], list[_Day]] = defaultdict(list)
    for record in days.values():
        grouped[record.signature(level)].append(record)
    entries = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
    total = sum(len(records) for _, records in entries)
    k, _covered = _select_k(
        [len(records) for _, records in entries], total, min_coverage, cap
    )
    built = [
        (signature, _make_template(signature, records, total))
        for signature, records in entries
    ]
    return TemplateLibrary(
        level=level,
        since=since,
        until=until,
        total_days=total,
        k=k,
        min_coverage=min_coverage,
        templates=tuple(template for _, template in built[:k]),
        others=tuple(template for _, template in built[k:]),
    )


def templates(
    conn: sqlite3.Connection,
    since: str | None = None,
    until: str | None = None,
    level: str = "name",
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    cap: int = DEFAULT_CAP,
) -> TemplateLibrary:
    """Библиотека шаблонов окна: адаптивный K и полный перечень «прочего».

    K — минимум сигнатур, покрывающих ``min_coverage`` дней окна, но не больше
    ``cap``. Ничьи разрешаются по убыванию числа дней, затем по имени
    (лексикографически). Дни вне K попадают в ``others`` без потерь.
    """
    key = _level_key(level)
    days = _read_days(conn, since, until)
    return _build_library(days, key, since, until, min_coverage, cap)


@dataclass(frozen=True, slots=True)
class EraRow:
    """Один календарный год: дни, сигнатуры, покрытие, топ-сигнатура."""

    year: str
    days: int
    signatures: int
    k: int
    covered: int
    coverage: float
    top: tuple[str, ...]
    top_days: int


def era_table(
    conn: sqlite3.Connection,
    level: str = "name",
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    cap: int = DEFAULT_CAP,
) -> list[EraRow]:
    """Эры v1 — календарные годы (включая годы без тренировок)."""
    key = _level_key(level)
    days = _read_days(conn)
    if not days:
        return []
    by_year: dict[str, dict[str, _Day]] = defaultdict(dict)
    for day, record in days.items():
        by_year[day[:4]][day] = record
    first_year = int(min(by_year))
    last_year = int(max(by_year))
    rows: list[EraRow] = []
    for year in range(first_year, last_year + 1):
        year_days = by_year.get(str(year), {})
        grouped: dict[tuple[str, ...], int] = defaultdict(int)
        for record in year_days.values():
            grouped[record.signature(key)] += 1
        entries = sorted(grouped.items(), key=lambda item: (-item[1], item[0]))
        total = sum(count for _, count in entries)
        k, covered = _select_k(
            [count for _, count in entries], total, min_coverage, cap
        )
        top, top_days = entries[0] if entries else ((), 0)
        rows.append(EraRow(
            year=str(year),
            days=total,
            signatures=len(entries),
            k=k,
            covered=covered,
            coverage=covered / total if total else 0.0,
            top=top,
            top_days=top_days,
        ))
    return rows


@dataclass(frozen=True, slots=True)
class NewDay:
    """День окна, чья сигнатура не входит в библиотеку."""

    day: str
    signature: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdherenceReport:
    """Приверженность окна библиотеке: доля, знаменатель, «новые» дни."""

    level: str
    since: str | None
    until: str | None
    days: int
    covered: int
    ratio: float
    new_days: tuple[NewDay, ...]
    new_signatures: tuple[tuple[tuple[str, ...], int], ...]


def adherence(
    conn: sqlite3.Connection,
    since: str | None,
    until: str | None,
    templates: TemplateLibrary | Iterable[Template],
    level: str = "name",
) -> AdherenceReport:
    """Доля дней окна, чья сигнатура входит в библиотеку.

    Знаменатель — все тренировочные дни окна. Дни вне библиотеки перечислены
    поимённо (``new_days``), а их сигнатуры агрегированы (``new_signatures``).
    """
    key = _level_key(level)
    known = {
        template.signature
        for template in (templates if isinstance(templates, TemplateLibrary)
                         else templates)
    }
    days = _read_days(conn, since, until)
    new_days: list[NewDay] = []
    counts: Counter[tuple[str, ...]] = Counter()
    for day in sorted(days):
        signature = days[day].signature(key)
        if signature not in known:
            new_days.append(NewDay(day=day, signature=signature))
            counts[signature] += 1
    total = len(days)
    covered = total - len(new_days)
    return AdherenceReport(
        level=key,
        since=since,
        until=until,
        days=total,
        covered=covered,
        ratio=covered / total if total else 0.0,
        new_days=tuple(new_days),
        new_signatures=tuple(
            sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ),
    )


@dataclass(frozen=True, slots=True)
class Transition:
    """Переход между соседними тренировочными днями."""

    frm: tuple[str, ...]
    to: tuple[str, ...]
    count: int


@dataclass(frozen=True, slots=True)
class LagStat:
    """Повтор сигнатуры через ``lag`` календарных дней."""

    lag: int
    same: int
    total: int

    @property
    def share(self) -> float:
        return self.same / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class TransitionReport:
    """Матрица переходов, кольцо и лаг-повторы окна."""

    level: str
    since: str | None
    until: str | None
    days: int
    transitions: int
    gaps: int
    max_gap: int
    matrix: tuple[Transition, ...]
    ring: tuple[tuple[str, ...], ...]
    ring_hits: int
    ring_share: float | None
    lag_repeat: tuple[LagStat, ...]


def _as_signature(value) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _dominant_ring(
    counter: Counter[tuple[tuple[str, ...], tuple[str, ...]]],
) -> tuple[tuple[str, ...], ...]:
    """Кольцо как цепочка самых частых переходов (детерминированно)."""
    if not counter:
        return ()
    outgoing: dict[tuple[str, ...], list[tuple[int, tuple[str, ...]]]] = \
        defaultdict(list)
    for (frm, to), count in counter.items():
        outgoing[frm].append((count, to))
    start = min(
        outgoing,
        key=lambda node: (-sum(count for count, _ in outgoing[node]), node),
    )
    ring: list[tuple[str, ...]] = [start]
    seen = {start}
    current = start
    while True:
        candidates = sorted(
            outgoing.get(current, []), key=lambda item: (-item[0], item[1])
        )
        if not candidates:
            break
        nxt = candidates[0][1]
        if nxt == start or nxt in seen:
            break
        ring.append(nxt)
        seen.add(nxt)
        current = nxt
    return tuple(ring) if len(ring) >= 2 else ()


def cycle(
    conn: sqlite3.Connection,
    since: str | None,
    until: str | None,
    max_gap: int = CYCLE_MAX_GAP,
    level: str = "name",
    ring: Iterable | None = None,
) -> TransitionReport:
    """Переходы между соседними днями окна, кольцо и повтор через 7 дней.

    Переход считается только между соседними тренировочными днями; разрыв в
    календаре больше ``max_gap`` переходом не считается (простой/болезнь — не
    смена программы). ``ring`` можно задать явно; иначе кольцо выводится как
    цепочка самых частых переходов.
    """
    if max_gap < 0:
        raise ValueError("max_gap должен быть >= 0")
    key = _level_key(level)
    days = _read_days(conn, since, until)
    ordered = sorted(days)
    signatures = {day: days[day].signature(key) for day in ordered}

    counter: Counter[tuple[tuple[str, ...], tuple[str, ...]]] = Counter()
    gaps = 0
    for previous, current in zip(ordered, ordered[1:]):
        delta = (date_cls.fromisoformat(current)
                 - date_cls.fromisoformat(previous)).days
        if delta > max_gap:
            gaps += 1
            continue
        counter[(signatures[previous], signatures[current])] += 1

    transitions = len(ordered) - 1 - gaps if ordered else 0
    if ring is not None:
        ring_signatures = tuple(_as_signature(item) for item in ring)
        if len(ring_signatures) < 2:
            ring_signatures = ()
    else:
        ring_signatures = _dominant_ring(counter)
    if len(ring_signatures) >= 2:
        pairs = {
            (ring_signatures[index], ring_signatures[(index + 1) % len(ring_signatures)])
            for index in range(len(ring_signatures))
        }
        ring_hits = sum(counter[pair] for pair in pairs)
        ring_share = ring_hits / transitions if transitions else None
    else:
        ring_hits = 0
        ring_share = None

    day_set = set(ordered)
    lag_repeat: list[LagStat] = []
    for lag in range(1, 8):
        same = 0
        total = 0
        for day in ordered:
            target = (date_cls.fromisoformat(day) + timedelta(days=lag)).isoformat()
            if target not in day_set:
                continue
            total += 1
            if signatures[target] == signatures[day]:
                same += 1
        lag_repeat.append(LagStat(lag=lag, same=same, total=total))

    matrix = tuple(
        Transition(frm=frm, to=to, count=count)
        for (frm, to), count in sorted(
            counter.items(), key=lambda item: (-item[1], item[0])
        )
    )
    return TransitionReport(
        level=key,
        since=since,
        until=until,
        days=len(ordered),
        transitions=transitions,
        gaps=gaps,
        max_gap=max_gap,
        matrix=matrix,
        ring=ring_signatures,
        ring_hits=ring_hits,
        ring_share=ring_share,
        lag_repeat=tuple(lag_repeat),
    )


# --------------------------------------------------------------------------
# Рендер
# --------------------------------------------------------------------------


def _signature_label(signature: Sequence[str]) -> str:
    if not signature:
        return "—"
    return " / ".join(signature)


def _window_label(since: str | None, until: str | None) -> str:
    left = since or "начало"
    right = until or "конец"
    return f"{left}…{right}"


def _ratio(value: int, total: int) -> str:
    share = value / total * 100 if total else 0.0
    return f"{value}/{total} = {share:.1f} %"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(
        header.ljust(widths[index]) for index, header in enumerate(headers)
    ).rstrip()]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(
            cell.ljust(widths[index]) for index, cell in enumerate(row)
        ).rstrip())
    return lines


def _first_label(template: Template) -> str:
    if template.first is None:
        return "—"
    if len(template.first_counts) > 1:
        return f"{template.first} ({template.first_counts[0][1]}/{template.days})"
    return template.first


def _composition(template: Template) -> str:
    if not template.exercises:
        return "—"
    return ", ".join(f"{name}×{count}" for name, count in template.exercises)


_TEMPLATE_HEADERS = (
    "#", "сигнатура", "дней", "доля", "первое", "+кардио", "состав",
)


def _template_row(index: int, template: Template, total: int) -> list[str]:
    return [
        str(index),
        _signature_label(template.signature),
        str(template.days),
        _ratio(template.days, total),
        _first_label(template),
        _ratio(template.cardio_days, template.days),
        _composition(template),
    ]


def format_templates(library: TemplateLibrary) -> str:
    """Таблица библиотеки: K шаблонов + полный перечень «прочего»."""
    if library.total_days == 0:
        return "нет тренировочных дней в окне\n"
    lines = [
        f"шаблоны {LEVEL_LABELS[library.level]}: "
        f"{_window_label(library.since, library.until)}, "
        f"дней {library.total_days}, "
        f"сигнатур {len(library.templates) + len(library.others)}, "
        f"K={library.k}, "
        f"покрытие {_ratio(library.covered_days, library.total_days)}"
    ]
    rows = [
        _template_row(index, template, library.total_days)
        for index, template in enumerate(library.templates, start=1)
    ]
    lines.extend(_table(_TEMPLATE_HEADERS, rows))
    lines.append("")
    if library.others:
        lines.append(
            f"прочее: {len(library.others)} сигнатур, "
            f"{sum(template.days for template in library.others)} дн"
        )
        other_rows = [
            _template_row(index, template, library.total_days)
            for index, template in enumerate(library.others, start=1)
        ]
        lines.extend(_table(_TEMPLATE_HEADERS, other_rows))
    else:
        lines.append("прочее: 0 сигнатур (0 дн) — нет")
    return "\n".join(lines) + "\n"


def format_eras(rows: list[EraRow]) -> str:
    """Таблица эр: год, дни, сигнатуры, K, покрытие, топ-сигнатура."""
    if not rows:
        return "нет тренировочных дней\n"
    lines = [
        f"эры L1: {rows[0].year}…{rows[-1].year} "
        f"({len(rows)} календарных лет)"
    ]
    table_rows = [
        [
            row.year,
            str(row.days),
            str(row.signatures),
            str(row.k),
            _ratio(row.covered, row.days),
            ("—" if not row.top
             else f"{_signature_label(row.top)} ({_ratio(row.top_days, row.days)})"),
        ]
        for row in rows
    ]
    lines.extend(_table(
        ("год", "дней", "сигнатур", "K", "покрытие", "топ"),
        table_rows,
    ))
    return "\n".join(lines) + "\n"


def format_adherence(report: AdherenceReport) -> str:
    """Строка приверженности + поимённый перечень «новых» дней."""
    lines = [
        f"приверженность {LEVEL_LABELS[report.level]}: "
        f"{_window_label(report.since, report.until)}, "
        f"{_ratio(report.covered, report.days)}"
    ]
    if report.new_days:
        lines.append(f"новые дни: {len(report.new_days)}")
        for item in report.new_days:
            lines.append(f"{item.day}  {_signature_label(item.signature)}")
    else:
        lines.append("новые дни: нет")
    return "\n".join(lines) + "\n"


def format_cycle(report: TransitionReport) -> str:
    """Таблица переходов, кольцо и лаг-повторы окна."""
    if report.days == 0:
        return "нет тренировочных дней в окне\n"
    lines = [
        f"цикл {LEVEL_LABELS[report.level]}: "
        f"{_window_label(report.since, report.until)}, "
        f"дней {report.days}, переходов {report.transitions}, "
        f"разрывов > {report.max_gap} дн: {report.gaps}"
    ]
    if report.matrix:
        rows = [
            [
                _signature_label(item.frm),
                _signature_label(item.to),
                str(item.count),
            ]
            for item in report.matrix
        ]
        lines.extend(_table(("от", "к", "раз"), rows))
    else:
        lines.append("переходов нет")
    lines.append("")
    if len(report.ring) >= 2:
        chain = " → ".join(_signature_label(signature)
                           for signature in report.ring)
        chain += f" → {_signature_label(report.ring[0])}"
        hits = _ratio(report.ring_hits, report.transitions)
        lines.append(f"кольцо: {chain}: {hits}")
    else:
        lines.append("кольцо: не определено")
    lag_rows = [
        [str(item.lag), str(item.same), str(item.total),
         _ratio(item.same, item.total)]
        for item in report.lag_repeat
    ]
    lines.extend(_table(("лаг", "совпало", "пар", "доля"), lag_rows))
    return "\n".join(lines) + "\n"
