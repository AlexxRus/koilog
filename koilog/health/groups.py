"""Погрупповая нагрузка (E2) и сравнение балансов (E3) — D11-a=E.

Разграничение D11-b=A: нагрузка здесь — **фичи/мониторинг**, не вход решений.
Модуль **чистый**: знает таксономию и ``sets``; о состоянии (``state``) не знает
намеренно (файрвол импортов, см. ``docs/e-groups-spec.md`` §5). Решение о
перерыве/рампе живёт в :mod:`koilog.health.state` и читает гейт, не эти числа.

Одна метрика на режим (E2):

* **железо** (``Mode.IRON``) — тоннаж (``weight * reps``), дни без тренировки 0;
* **своё тело** (``Mode.BODYWEIGHT``) — **подходы** (тоннаж там систематически
  обнулён: вес пуст или телесный);
* **кардио** (``Mode.CARDIO``) — **время** (секунды), тоннаж 0 по построению.

Ветка режима берётся **только** из :func:`koilog.taxonomy.mode_of` [У1]: никаких
``if "жим" in name``. Неопознанное имя роняет агрегат со списком имён — забытое
ответвление должно быть невозможным, а не маловероятным.

Окно (E2-b, ``ROLLING_WINDOW = 7``) — **то же**, что у
:func:`koilog.health.load.weekly_tonnage`, дословно: один определение — два
потребителя. Серия ведётся по **непрерывному** календарю (как ``load_daily``),
чтобы окно не «сшивалось» через простой.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date as date_cls
from datetime import timedelta

from .. import taxonomy
from .load import weekly_tonnage

__all__ = [
    "BASELINE_LAGS",
    "ROLLING_WINDOW",
    "GroupDay",
    "GroupMetric",
    "GroupSeries",
    "TonnageRatio",
    "SetsRatio",
    "TimeRatio",
    "aggregate",
    "baseline",
    "balance",
    "group_of_metric",
    "modes_in_group",
    "series",
    "series_by_mode",
    "unknown_groups",
]

#: Окно агрегата. Строго равно окну ``weekly_tonnage``: смена — одна правка
#: плюс тест «E-rolling(железо) == weekly_tonnage» (E2-b).
ROLLING_WINDOW = 7

#: Сетка базлайна для сравнения балансов: значения rolling-7, отстоящие от
#: текущего на 7/14/21 дней (окна той же ширины, взаимно не перекрываются и с
#: текущим не пересекаются). Все три обязательны (E3-a).
#: Цена зафиксирована в спеке: после перерыва, выбивающего сетку, ratio
#: молчит ~3 недели — это фича, сравнения после паузы бессмысленны.
BASELINE_LAGS = (7, 14, 21)

_METRIC_OF_MODE = {
    taxonomy.Mode.IRON: "tonnage",
    taxonomy.Mode.BODYWEIGHT: "sets",
    taxonomy.Mode.CARDIO: "seconds",
}

_METRIC_TO_MODE = {metric: mode for mode, metric in _METRIC_OF_MODE.items()}


def group_of_metric(metric: str) -> taxonomy.Mode:
    """Режим, которому принадлежит метрика (обратный к ``_METRIC_OF_MODE``).

    Нужен для структурного запрета кросс-метрики: сравнение типизировано
    веткой, поэтому смешение должно быть **ошибкой типов**, а не дисциплиной.
    """
    return _METRIC_TO_MODE[metric]


class GroupDay:
    """Одна точка погруппового дневного ряда."""

    __slots__ = ("date", "metric", "value", "n_sets")

    def __init__(self, date: str, metric: str, value: float, n_sets: int) -> None:
        self.date = date
        self.metric = metric
        self.value = value
        self.n_sets = n_sets


class GroupSeries:
    """Непрерывный погрупповой ряд: дни без данных = 0 (окно не «сшивается»)."""

    __slots__ = ("group", "mode", "metric", "days", "_index")

    def __init__(
        self,
        group: str,
        mode: taxonomy.Mode,
        metric: str,
        days: list[GroupDay],
    ) -> None:
        self.group = group
        self.mode = mode
        self.metric = metric
        self.days = days
        self._index = {day.date: i for i, day in enumerate(days)}

    @property
    def dates(self) -> list[str]:
        return [day.date for day in self.days]

    def values(self) -> list[float]:
        return [day.value for day in self.days]

    def rolling(self, window: int = ROLLING_WINDOW) -> list[float]:
        """Скользящая сумма метрики; для железа — ровно ``weekly_tonnage``.

        Кардио/подходы идут через ту же функцию: окно одно для всех метрик
        (E2-b), меняется только входной ряд.
        """
        return weekly_tonnage(self.values(), window)

    def window_at(
        self, date: str, window: int = ROLLING_WINDOW
    ) -> tuple[float | None, int]:
        """Значение и ``n_days`` за ``window``-окно, кончающееся на ``date``.

        ``n_days`` — число **дней с данными** по группе в окне (погрупповой и
        всегда рядом со значением, E2-a). Пустое окно → ``(None, 0)``:
        «нет данных» ≠ 0 (иначе простой читался бы как лёгкая неделя).
        """
        index = self._index.get(date)
        if index is None:
            return None, 0
        lo = max(0, index - window + 1)
        slice_ = self.days[lo:index + 1]
        n = sum(1 for day in slice_ if day.n_sets > 0)
        if n == 0:
            return None, 0
        return sum(day.value for day in slice_), n


def _calendar(first: str, last: str) -> list[str]:
    current, stop = date_cls.fromisoformat(first), date_cls.fromisoformat(last)
    out: list[str] = []
    while current <= stop:
        out.append(current.isoformat())
        current += timedelta(days=1)
    return out


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _daily_by_group(conn: sqlite3.Connection) -> dict[str, dict[str, list[float]]]:
    """Свести ``sets`` в дневные суммы по **конкретному имени** упражнения.

    Возвращает ``{имя: {дата: [тоннаж, подходы, секунды]}}``. Разбиение по
    имени, а не по группе, — чтобы смешанные группы («Приседания» на своём
    теле vs со штангой) разрешались через :func:`taxonomy.mode_of`.
    """
    if not _has_table(conn, "sets"):
        return {}
    rows = conn.execute(
        """
        SELECT exercise,
               date,
               SUM(CASE WHEN is_warmup = 0 AND skipped = 0
                        THEN weight * reps ELSE 0 END)  AS tonnage,
               SUM(CASE WHEN is_warmup = 0 AND skipped = 0
                        THEN 1 ELSE 0 END)              AS sets,
               SUM(CASE WHEN is_warmup = 0 AND skipped = 0
                        THEN COALESCE(time_seconds, 0) ELSE 0 END) AS seconds
        FROM sets
        WHERE date IS NOT NULL
        GROUP BY exercise, date
        """
    ).fetchall()
    out: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        name = row["exercise"]
        out.setdefault(name, {})[row["date"]] = [
            row["tonnage"] or 0.0,
            float(row["sets"] or 0),
            row["seconds"] or 0.0,
        ]
    return out


def unknown_groups(conn: sqlite3.Connection) -> list[str]:
    """Имена из ``sets``, которым таксономия не назначила режим.

    Печатается/падает вызывающий. Неопознанное имя не относится к ветке молча:
    это отдельное решение о расширении таксономии, а не побочный эффект
    агрегата (E1).
    """
    if not _has_table(conn, "sets"):
        return []
    seen = {row["exercise"] for row in conn.execute("SELECT DISTINCT exercise FROM sets")}
    return sorted(name for name in seen if taxonomy.mode_of(name) is None)


def modes_in_group(conn: sqlite3.Connection, group: str) -> list[taxonomy.Mode]:
    """Режимы, реально встречающиеся среди имён группы (стабильный порядок).

    Порядок — по объявлению ``Mode`` (iron, bodyweight, cardio), чтобы E-строка
    была детерминированной.
    """
    daily = _daily_by_group(conn)
    present = {
        mode
        for name in daily
        if (mode := taxonomy.mode_of(name)) is not None
        and taxonomy.group_of(name).name == group
    }
    return [mode for mode in taxonomy.Mode if mode in present]


def series(
    conn: sqlite3.Connection,
    group: str,
    mode: taxonomy.Mode,
    until: str | None = None,
) -> GroupSeries:
    """Непрерывный дневной ряд **(группа × режим)** (E2, §2.5 — решение A).

    ``mode`` — **обязательный** параметр: «нагрузка ТРИЦЕПСа» не существует как
    одно число, поэтому нет и молчаливого выбора ветки. Обе подветки смешанной
    группы берутся двумя явными вызовами (или :func:`series_by_mode`).

    ``until`` — день карточки: ряд дотягивается до него пустыми днями, чтобы
    окно, кончающееся сегодня, видело вчерашнюю нагрузку.

    ``RuntimeError`` поднимается только для **баг-класса**: имя резолвится в
    режим, отличный от запрошенного, при том что его группа — эта (т.е.
    нарушен инвариант «режим по имени согласован с группой»).
    """
    daily = _daily_by_group(conn)
    members: list[str] = []
    for name in daily:
        name_mode = taxonomy.mode_of(name)
        if taxonomy.group_of(name).name != group:
            continue
        if name_mode is None:
            raise RuntimeError(
                f"имя {name!r} не резолвится в режим: таксономия не назначила "
                f"группу/режим (E1: невозможное, а не маловероятное)"
            )
        if name_mode is mode:
            members.append(name)
    metric = _METRIC_OF_MODE[mode]

    dates = sorted({date for name in members for date in daily[name]})
    if not dates:
        return GroupSeries(group, mode, metric, [])
    last = dates[-1]
    if until and until > last:
        # Дотянем пустые дни до дня карточки: окно, кончающееся на ``until``,
        # должно уметь увидеть недавнюю тренировку (иначе день без тренировки
        # сегодня показывал бы «—», хотя вчера нагрузка была). Пустые дни = 0.
        last = until
    calendar = _calendar(dates[0], last)
    index = {"tonnage": 0, "sets": 1, "seconds": 2}[metric]

    days: list[GroupDay] = []
    for day in calendar:
        value = 0.0
        n_sets = 0
        for name in members:
            cell = daily[name].get(day)
            if cell is None:
                continue
            value += cell[index]
            n_sets += int(cell[1])
        days.append(GroupDay(day, metric, value, n_sets))
    return GroupSeries(group, mode, metric, days)


def series_by_mode(
    conn: sqlite3.Connection, group: str
) -> dict[taxonomy.Mode, GroupSeries]:
    """Все режимные подветки группы, включая **мёртвые** (пустые) — §2.5.

    Мёртвая подветка возвращается с пустым рядом, чтобы E-строка показала её
    явно («—»), а не спрятала.
    """
    found = modes_in_group(conn, group)
    if not found:
        # Группа без своих имён в текущей истории: ряд по режиму группы (пустой).
        found = [taxonomy.GROUPS[group].mode]
    return {mode: series(conn, group, mode) for mode in found}


def aggregate(
    conn: sqlite3.Connection, group: str, mode: taxonomy.Mode,
    day: str | None = None,
) -> dict:
    """Текущий rolling-7 подветки (группа × режим): значение, ``n_days``, метрика.

    Главный выход E2. ``value is None`` = «нет данных в окне» (E2-a), не 0.
    Якорь — ``day`` (день карточки), а не последняя тренировка подветки: иначе
    мёртвая подветка («ТРИЦЕПС» к 2021) показывала бы вечно живое число (§2.5).
    """
    row = series(conn, group, mode, day)
    if not row.days:
        return {"group": group, "mode": row.mode, "metric": row.metric,
                "value": None, "n_days": 0, "date": None}
    current = day or row.days[-1].date
    value, n_days = row.window_at(current)
    return {"group": group, "mode": row.mode, "metric": row.metric,
            "value": value, "n_days": n_days, "date": current}


def baseline(
    conn: sqlite3.Connection,
    group: str,
    mode: taxonomy.Mode,
    date: str | None = None,
    lags: tuple[int, ...] = BASELINE_LAGS,
) -> dict:
    """Сетка базлайна подветки (группа × режим): rolling-7 на ``lags`` до ``date``.

    Все значения должны быть **валидны** (не ``None``), иначе ``ready=False``:
    частичный базлайн не экстраполируем (E3-a). У каждой подветки — своя сетка,
    своя медиана, свой «мало истории» (§2.5, проверка №17).
    """
    row = series(conn, group, mode, date)
    if not row.days:
        return {"group": group, "mode": mode, "metric": row.metric,
                "ready": False, "values": [], "median": None}
    date = date or row.days[-1].date
    values: list[float] = []
    for lag in lags:
        value, _ = row.window_at(_shift(date, -lag))
        values.append(value)  # type: ignore[arg-type]
    ready = all(value is not None for value in values)
    median = statistics.median(values) if ready else None
    return {"group": group, "mode": mode, "metric": row.metric, "ready": ready,
            "values": values, "median": median}


def _shift(date: str, days: int) -> str:
    return (date_cls.fromisoformat(date) + timedelta(days=days)).isoformat()


class GroupMetric:
    """Типизированный результат: ветка/метрика, ``ratio`` и ``n_days``.

    Структурный запрет кросс-метрики [У2]: :func:`balance` возвращает **тот
    тип**, которому принадлежат обе группы, и смешение (``TonnageRatio`` vs
    ``SetsRatio``) роняет :func:`balance` ``TypeError``-ом — ошибка типов, а не
    дисциплина.
    """

    __slots__ = ("numerator", "denominator", "mode", "ratio", "n_days")

    #: Имя метрики ветки; перекрывается подклассами (см. ``TonnageRatio``).
    metric = ""

    def __init__(
        self,
        numerator: str,
        denominator: str,
        ratio: float,
        n_days: int = 0,
    ) -> None:
        self.numerator = numerator
        self.denominator = denominator
        self.mode = _METRIC_TO_MODE[type(self).metric]
        self.ratio = ratio
        self.n_days = n_days


class TonnageRatio(GroupMetric):
    """Кратность по тоннажу (железо). «1.3× к медиане 3 нед»."""

    metric = "tonnage"


class SetsRatio(GroupMetric):
    """Кратность по подходам (своё тело)."""

    metric = "sets"


class TimeRatio(GroupMetric):
    """Кратность по времени (кардио)."""

    metric = "seconds"


_RATIO_OF_MODE = {
    taxonomy.Mode.IRON: TonnageRatio,
    taxonomy.Mode.BODYWEIGHT: SetsRatio,
    taxonomy.Mode.CARDIO: TimeRatio,
}


def balance(
    conn: sqlite3.Connection,
    numerator: str,
    denominator: str,
    mode: taxonomy.Mode,
    date: str | None = None,
) -> GroupMetric | None:
    """Отношение текущего rolling-7 двух групп одной ветки/метрики (E3).

    Обе группы берутся в **одном** ``mode`` (§2.5): сравнение — только внутри
    одной ветки и одной метрики [У2]. Разные режимы/метрики → ``TypeError``
    (структурный запрет, не дисциплина). Baseline — **медиана** трёх валидных
    значений сетки (ФЛАГ 1: среднее тащится больной неделей). Иначе ``None``
    (нулевой знаменатель, мало истории). Округление — до 0.1 (кратность).
    """
    row_n = series(conn, numerator, mode, date)
    row_d = series(conn, denominator, mode, date)
    date = date or (row_n.days[-1].date if row_n.days else None)
    if date is None:
        return None
    current, n_days = row_n.window_at(date)
    base = baseline(conn, denominator, mode, date)
    if current is None or not base["ready"] or not base["median"]:
        return None
    ratio = current / base["median"]
    return _RATIO_OF_MODE[mode](numerator, denominator, round(ratio, 1), n_days)


def _refuse_cross_mode_sum(group: str, *modes: taxonomy.Mode) -> None:
    """Запрет кросс-режимной суммы внутри группы (проверка №17, §2.5).

    «Весь трицепс за неделю» не существует как число: режимы измеряются
    разными метриками (тоннаж / подходы / время). Складывать их — ошибка
    типов, а не дисциплины.
    """
    unique = {mode for mode in modes if mode is not None}
    if len(unique) > 1:
        raise TypeError(
            f"кросс-режимная сумма внутри группы {group!r} запрещена: "
            f"{', '.join(sorted(m.value for m in unique))} измеряются разными "
            f"метриками (§2.5, D11-a [У2])"
        )
