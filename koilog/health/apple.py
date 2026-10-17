"""Разбор выгрузки Apple Health в таблицы ``aw_*``.

Выгрузка - это один XML на гигабайт с двумя с половиной миллионами записей,
поэтому файл разбирается **одним проходом** ``xml.etree.ElementTree.iterparse``
и в память целиком не поднимается. Записи, которые нужны поштучно (сырой пульс,
сегменты сна, тренировки), собираются в списки, а всё суммируемое (шаги, калории,
часы стояния) сводится в дневные агрегаты сразу.

Что важно знать про формат:

- **Записи дублируются.** По документации Apple любая ``Record``, вложенная в
  ``Workout``, встречается ещё и на верхнем уровне. Поэтому вложенные записи
  пропускаются (счётчик ``inside_workout``), иначе пульс тренировки считался бы
  дважды.
- **Дети ``Workout`` нельзя очищать раньше родителя.** ``WorkoutStatistics``
  (средний и максимальный пульс, энергия) и ``MetadataEntry`` (MET) читаются,
  когда закрывается сам ``Workout``. Если чистить их на выходе, к моменту чтения
  родителя они уже перестанут существовать.
- **Сутки для ночных метрик привязаны к пробуждению.** ВСР, дыхание, SpO2 и
  температура запястья получают ключ ``_night_key``: замер до полудня
  (включая подъём в 07:00) относится к той же дате окончания, а «переход»
  через полдень сдвигается на −12 ч.
- **Источник фильтруется.** В выгрузке перемешаны Apple Watch, сторонние
  приложения (``Connect``, ``Athlytic``) и ручной ввод. Пульс покоя, ВСР,
  дыхание, SpO2 и температуру запястья пишет только Apple Watch. Сверка идёт
  после нормализации пробелов: сама выгрузка пишет имя как
  ``Apple\xa0Watch\xa0— Алексей`` с неразрывным пробелом.

Один и тот же экспорт можно грузить повторно: таблицы ``aw_*`` пересобираются
целиком (это снимок, а не журнал), таблица ``sets`` не затрагивается.
"""

from __future__ import annotations

import glob
import os
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from .. import etl
from .text import table

__all__ = [
    "APPLE_DIR",
    "DEFAULT_WINDOW",
    "AppleData",
    "AppleLoadReport",
    "COVERAGE_COLUMNS",
    "coverage",
    "coverage_by_year",
    "daily",
    "default_xml",
    "format_coverage",
    "format_coverage_by_year",
    "is_watch_source",
    "load",
    "parse_apple",
    "write_apple",
]

# --------------------------------------------------------------------------
# Пути и константы
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent.parent
APPLE_DIR = ROOT / "data" / "aw" / "apple_health_export"

#: Первый день, с которого в выгрузке есть полный набор часовых метрик
#: (ВСР, дыхание, температура запястья, SpO2). Раньше 2024-01-16 есть только
#: пульс покоя и сон, и то не сплошь.
DEFAULT_WINDOW = "2024-01-16"

#: Идентификаторы типов записей.
RHR = "HKQuantityTypeIdentifierRestingHeartRate"
HRV = "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
RESP = "HKQuantityTypeIdentifierRespiratoryRate"
WRIST_TEMP = "HKQuantityTypeIdentifierAppleSleepingWristTemperature"
SPO2 = "HKQuantityTypeIdentifierOxygenSaturation"
SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
HEART_RATE = "HKQuantityTypeIdentifierHeartRate"
STEPS = "HKQuantityTypeIdentifierStepCount"
ACTIVE_KCAL = "HKQuantityTypeIdentifierActiveEnergyBurned"
BASAL_KCAL = "HKQuantityTypeIdentifierBasalEnergyBurned"
EXERCISE_MIN = "HKQuantityTypeIdentifierAppleExerciseTime"
STAND_HOUR = "HKCategoryTypeIdentifierAppleStandHour"
WALKING_HR = "HKQuantityTypeIdentifierWalkingHeartRateAverage"
VO2MAX = "HKQuantityTypeIdentifierVO2Max"
BODY_TEMP = "HKQuantityTypeIdentifierBodyTemperature"
EFFORT = "HKQuantityTypeIdentifierWorkoutEffortScore"
EFFORT_EST = "HKQuantityTypeIdentifierEstimatedWorkoutEffortScore"
PHYSICAL_EFFORT = "HKQuantityTypeIdentifierPhysicalEffort"
DISTANCE = "HKQuantityTypeIdentifierDistanceWalkingRunning"

_STAND_STOOD = "HKCategoryValueAppleStandHourStood"
_ASLEEP = {
    "HKCategoryValueSleepAnalysisAsleep",
    "HKCategoryValueSleepAnalysisAsleepCore",
    "HKCategoryValueSleepAnalysisAsleepDeep",
    "HKCategoryValueSleepAnalysisAsleepREM",
    "HKCategoryValueSleepAnalysisAsleepUnspecified",
}
_AWAKE = {"HKCategoryValueSleepAnalysisAwake"}
_INBED = {"HKCategoryValueSleepAnalysisInBed"}

#: Приложение-источник, которому верим для внутренней нагрузки. Сторонние
#: источники (``Connect``, ``MapMyFitness``, ``Athlytic``) пишут пульс, но не
#: ВСР и не ночную температуру, а ручной ввод вообще не про физиологию.
WATCH_SOURCE_MARK = "Apple Watch"

#: Разделители, которые Apple расставляет как неразрывные пробелы. В самой
#: выгрузке источник записан как ``Apple\xa0Watch\xa0— Алексей``: обычный
#: поиск подстроки ``"Apple Watch"`` его не видел и **молча отбрасывал все
#: часовые метрики** (RHR/ВСР/дыхание/температура/SpO2), а вместе с ними и
#: всю сигнатуру болезни. Поэтому имя источника нормализуется перед сверкой.
_SPACE_TRANSLATION = str.maketrans({
    "\xa0": " ",   # NBSP
    "\u202f": " ", # narrow no-break space
    "\u2009": " ", # thin space
    "\u3000": " ", # ideographic space
})


def is_watch_source(source: str) -> bool:
    """Запись сделана Apple Watch, а не сторонним приложением."""
    return WATCH_SOURCE_MARK in source.translate(_SPACE_TRANSLATION)

#: Дети ``Workout``: их нельзя очищать, пока не прочитан родитель.
_WORKOUT_CHILDREN = {
    "WorkoutStatistics", "MetadataEntry", "WorkoutZoneGroup",
    "WorkoutEvent", "WorkoutActivity", "WorkoutRoute",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS aw_meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS aw_daily (
  date            TEXT PRIMARY KEY,
  rhr             REAL,
  hrv_sdnn        REAL,
  hrv_n           INTEGER,
  resp_rate       REAL,
  wrist_temp      REAL,
  spo2            REAL,
  sleep_hours     REAL,
  sleep_inbed_h   REAL,
  sleep_start     TEXT,
  sleep_end       TEXT,
  sleep_deep_h    REAL,
  sleep_rem_h     REAL,
  sleep_core_h    REAL,
  sleep_awake_h   REAL,
  steps           INTEGER,
  active_kcal     REAL,
  basal_kcal      REAL,
  exercise_min    REAL,
  stand_hours     INTEGER,
  walking_hr      REAL,
  vo2max          REAL,
  physical_effort REAL
);

CREATE TABLE IF NOT EXISTS aw_sleep (
  start_ts TEXT,
  end_ts   TEXT,
  stage    TEXT,
  hours    REAL
);

CREATE TABLE IF NOT EXISTS aw_workouts (
  start_ts          TEXT,
  end_ts            TEXT,
  date              TEXT,
  activity          TEXT,
  duration_min      REAL,
  energy_kcal       REAL,
  energy_basal_kcal REAL,
  distance_km       REAL,
  avg_hr            REAL,
  max_hr            REAL,
  mets              REAL,
  effort            REAL,
  effort_estimated  REAL,
  source            TEXT,
  UNIQUE (start_ts, activity, source)
);

CREATE TABLE IF NOT EXISTS aw_bodytemp (
  ts     TEXT,
  date   TEXT,
  value  REAL,
  source TEXT
);

CREATE TABLE IF NOT EXISTS aw_hr (
  ts     TEXT,
  date   TEXT,
  bpm    REAL,
  source TEXT
);

CREATE TABLE IF NOT EXISTS aw_ecg (
  date TEXT,
  file TEXT
);

CREATE INDEX IF NOT EXISTS ix_aw_hr_date       ON aw_hr(date);
CREATE INDEX IF NOT EXISTS ix_aw_sleep_start   ON aw_sleep(start_ts);
CREATE INDEX IF NOT EXISTS ix_aw_workouts_date ON aw_workouts(date);
CREATE INDEX IF NOT EXISTS ix_aw_bodytemp_date ON aw_bodytemp(date);
"""

#: Порядок колонок ``aw_daily``: и схема, и строки-значения идут по нему.
DAILY_COLUMNS: tuple[str, ...] = (
    "date", "rhr", "hrv_sdnn", "hrv_n", "resp_rate", "wrist_temp", "spo2",
    "sleep_hours", "sleep_inbed_h", "sleep_start", "sleep_end",
    "sleep_deep_h", "sleep_rem_h", "sleep_core_h", "sleep_awake_h",
    "steps", "active_kcal", "basal_kcal", "exercise_min", "stand_hours",
    "walking_hr", "vo2max", "physical_effort",
)


# --------------------------------------------------------------------------
# Разбор значений
# --------------------------------------------------------------------------


def _dt(value: str | None) -> datetime | None:
    """``2026-01-12 18:24:39 +0300`` -> наивный локальный ``datetime``."""
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _f(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _night_key(moment: datetime) -> str:
    """Ключ ночи: привязываем к дате пробуждения.

    Сон, закончившийся до полудня, относится к той же дате окончания.
    Сон, длившийся через полдень (редко), по-прежнему смещается на -12ч.
    """
    if moment.hour < 12:
        return moment.date().isoformat()
    return (moment - timedelta(hours=12)).date().isoformat()


# --------------------------------------------------------------------------
# Накопители
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Day:
    """Одна строка ``aw_daily``: ночные метрики и дневные суммы."""

    rhr: float | None = None
    hrv: float | None = None
    hrv_n: int | None = None
    resp: float | None = None
    wrist: float | None = None
    spo2: float | None = None
    sleep_hours: float | None = None
    sleep_inbed: float | None = None
    sleep_start: str | None = None
    sleep_end: str | None = None
    sleep_deep: float | None = None
    sleep_rem: float | None = None
    sleep_core: float | None = None
    sleep_awake: float | None = None
    steps: float = 0.0
    active_kcal: float = 0.0
    basal_kcal: float = 0.0
    exercise_min: float = 0.0
    stand_hours: int = 0
    walking_sum: float = 0.0
    walking_n: int = 0
    vo2max: float | None = None
    effort_sum: float = 0.0
    effort_n: int = 0


@dataclass(slots=True)
class _Night:
    """Ночные сэмплы одной ночи до усреднения."""

    hrv: list[float] = field(default_factory=list)
    resp: list[float] = field(default_factory=list)
    wrist: list[float] = field(default_factory=list)
    spo2: list[float] = field(default_factory=list)


@dataclass(slots=True)
class AppleData:
    """Всё, что удалось вычитать из выгрузки, до записи в базу."""

    daily: dict[str, _Day] = field(default_factory=dict)
    nights: dict[str, _Night] = field(default_factory=dict)
    sleep: list[tuple[str, str, str, float]] = field(default_factory=list)
    hr: list[tuple[str, str, float, str]] = field(default_factory=list)
    workouts: list[dict] = field(default_factory=list)
    bodytemp: list[tuple[str, str, float, str]] = field(default_factory=list)
    effort: list[tuple[datetime, datetime, float]] = field(default_factory=list)
    effort_est: list[tuple[datetime, datetime, float]] = field(default_factory=list)
    ecg: list[tuple[str, str]] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    _segs: dict[str, list[tuple[datetime, datetime, str]]] = field(default_factory=dict)

    def day(self, key: str) -> _Day:
        store = self.daily.get(key)
        if store is None:
            store = _Day()
            self.daily[key] = store
        return store

    def night(self, key: str) -> _Night:
        store = self.nights.get(key)
        if store is None:
            store = _Night()
            self.nights[key] = store
        return store


# --------------------------------------------------------------------------
# Разбор XML
# --------------------------------------------------------------------------


def _iter_nodes(path: str | os.PathLike) -> Iterator[tuple[str, ET.Element]]:
    """Один проход по XML: отдаёт ``Record`` и ``Workout`` по мере закрытия.

    Вложенные в ``Workout`` записи пропускаются: они дублируют верхнеуровневые.
    Дети ``Workout`` (``WorkoutStatistics``, ``MetadataEntry``) не очищаются,
    пока не прочитан родитель.
    """
    inside_workout = 0
    for event, elem in ET.iterparse(str(path), events=("start", "end")):
        if event == "start":
            if elem.tag == "Workout":
                inside_workout += 1
            continue
        tag = elem.tag
        if tag == "Workout":
            inside_workout -= 1
            yield "workout", elem
            elem.clear()
        elif tag == "Record":
            if not inside_workout:
                yield "record", elem
            elem.clear()
        elif tag in _WORKOUT_CHILDREN:
            continue
        else:
            # Correlation, ActivitySummary, ExportDate, Me, ClinicalRecord и пр.
            elem.clear()


def parse_apple(
    path: str | os.PathLike,
    window: str | None = DEFAULT_WINDOW,
) -> AppleData:
    """Прочитать выгрузку и собрать :class:`AppleData`.

    ``window`` - первая дата включительно; всё раньше пропускается. ``None``
    означает «весь файл».
    """
    start = date_cls.fromisoformat(window) if window else None
    data = AppleData()

    def keep(day: str) -> bool:
        return start is None or day >= start.isoformat()

    for kind, elem in _iter_nodes(path):
        if kind == "workout":
            _read_workout(elem, data, keep)
        else:
            _read_record(elem, data, keep)

    _read_meta(path, data)
    _finalize(data)
    return data


def _read_meta(path: str | os.PathLike, data: AppleData) -> None:
    """Заголовок и электрокардиограммы (отдельные CSV рядом с XML)."""
    try:
        for event, elem in ET.iterparse(str(path), events=("start",)):
            if elem.tag == "ExportDate":
                data.meta["export_date"] = elem.get("value") or ""
                break
    except ET.ParseError:
        pass
    folder = Path(path).parent / "electrocardiograms"
    if folder.is_dir():
        for ecg_path in sorted(glob.glob(str(folder / "ecg_*.csv"))):
            name = Path(ecg_path).name
            day = name[4:14]
            try:
                date_cls.fromisoformat(day)
            except ValueError:
                continue
            data.ecg.append((day, name))


def _read_record(elem: ET.Element, data: AppleData, keep) -> None:
    rtype = elem.get("type") or ""
    data.counts[rtype] = data.counts.get(rtype, 0) + 1
    raw = elem.get("value")
    source = elem.get("sourceName") or ""
    value = _f(raw)
    moment = _dt(elem.get("startDate"))
    if moment is None:
        return

    if rtype == HEART_RATE:
        if value is not None and keep(moment.date().isoformat()):
            data.hr.append((moment.isoformat(sep=" "), moment.date().isoformat(),
                            value, source))
        return

    if rtype == SLEEP:
        _read_sleep(elem, data, keep, moment, raw)
        return

    if rtype == STAND_HOUR:
        # категориальная запись: "стою" / "не стою", число из неё не вычитать
        if raw == _STAND_STOOD and keep(moment.date().isoformat()):
            data.day(moment.date().isoformat()).stand_hours += 1
        return

    if value is None:
        return

    if rtype in (RHR, HRV, RESP, WRIST_TEMP, SPO2):
        if not is_watch_source(source):
            return
        if rtype == RHR:
            if keep(moment.date().isoformat()):
                data.day(moment.date().isoformat()).rhr = value
        else:
            key = _night_key(moment)
            if keep(key):
                night = data.night(key)
                if rtype == HRV:
                    night.hrv.append(value)
                elif rtype == RESP:
                    night.resp.append(value)
                elif rtype == WRIST_TEMP:
                    night.wrist.append(value)
                else:
                    night.spo2.append(value * 100.0)
        return

    if rtype in (EFFORT, EFFORT_EST):
        end = _dt(elem.get("endDate")) or moment
        (data.effort if rtype == EFFORT else data.effort_est).append(
            (moment, end, value))
        return

    if rtype == BODY_TEMP:
        day = moment.date().isoformat()
        if keep(day):
            data.bodytemp.append((moment.isoformat(sep=" "), day, value, source))
        return

    day = moment.date().isoformat()
    if not keep(day):
        return
    store = data.day(day)
    if rtype == STEPS:
        store.steps += value
    elif rtype == ACTIVE_KCAL:
        store.active_kcal += value
    elif rtype == BASAL_KCAL:
        store.basal_kcal += value
    elif rtype == EXERCISE_MIN:
        store.exercise_min += value
    elif rtype == WALKING_HR:
        store.walking_sum += value
        store.walking_n += 1
    elif rtype == VO2MAX:
        store.vo2max = value
    elif rtype == PHYSICAL_EFFORT:
        store.effort_sum += value
        store.effort_n += 1


def _read_sleep(elem, data: AppleData, keep, start: datetime, stage: str | None) -> None:
    end = _dt(elem.get("endDate"))
    if end is None or end <= start:
        return
    key = _night_key(end)
    if not keep(key):
        return
    data.sleep.append((start.isoformat(sep=" "), end.isoformat(sep=" "),
                       stage or "", (end - start).total_seconds() / 3600.0))
    store = data.day(key)
    if store.sleep_start is None or start.strftime("%H:%M") < store.sleep_start:
        store.sleep_start = start.strftime("%H:%M")
    if store.sleep_end is None or end.strftime("%H:%M") > store.sleep_end:
        store.sleep_end = end.strftime("%H:%M")
    # сегменты копим отдельно: итоговые часы сна считаем по ним в _finalize
    data._segs.setdefault(key, []).append((start, end, stage or ""))


def _read_workout(elem: ET.Element, data: AppleData, keep) -> None:
    start = _dt(elem.get("startDate"))
    end = _dt(elem.get("endDate"))
    if start is None or not keep(start.date().isoformat()):
        return
    stats: dict[str, ET.Element] = {}
    mets = None
    for child in list(elem):
        if child.tag == "WorkoutStatistics":
            stats[child.get("type") or ""] = child
        elif child.tag == "MetadataEntry" and child.get("key") == "HKAverageMETs":
            mets = _f((child.get("value") or "").split(" ")[0])

    def stat(key: str, attr: str) -> float | None:
        node = stats.get(key)
        return _f(node.get(attr)) if node is not None else None

    data.workouts.append({
        "start_ts": start.isoformat(sep=" "),
        "end_ts": (end or start).isoformat(sep=" "),
        "date": start.date().isoformat(),
        "activity": (elem.get("workoutActivityType") or "").replace(
            "HKWorkoutActivityType", ""),
        "duration_min": ((end - start).total_seconds() / 60.0) if end else None,
        "energy_kcal": stat(ACTIVE_KCAL, "sum"),
        "energy_basal_kcal": stat(BASAL_KCAL, "sum"),
        "distance_km": stat(DISTANCE, "sum"),
        "avg_hr": stat(HEART_RATE, "average"),
        "max_hr": stat(HEART_RATE, "maximum"),
        "mets": mets,
        "source": elem.get("sourceName") or "",
    })


def _finalize(data: AppleData) -> None:
    """Усреднить ночные сэмплы, посчитать часы сна и подписать усилие."""
    # оценка усилия тренировок - по пересечению интервалов
    for workout in data.workouts:
        start = _dt(workout["start_ts"])
        end = _dt(workout["end_ts"])
        workout["effort"] = _match_effort(data.effort, start, end)
        workout["effort_estimated"] = _match_effort(data.effort_est, start, end)

    # ночные сэмплы
    for key, night in data.nights.items():
        store = data.day(key)
        if night.hrv:
            store.hrv = sum(night.hrv) / len(night.hrv)
            store.hrv_n = len(night.hrv)
        if night.resp:
            store.resp = sum(night.resp) / len(night.resp)
        if night.wrist:
            store.wrist = sum(night.wrist) / len(night.wrist)
        if night.spo2:
            store.spo2 = sum(night.spo2) / len(night.spo2)

    # пересекающиеся сегменты сна -> суммарные часы
    for key, segs in data._segs.items():
        store = data.day(key)
        if not segs:
            continue
        asleep = 0.0
        awake = 0.0
        inbed = 0.0
        deep = 0.0
        rem = 0.0
        core = 0.0
        # упрощённо: суммируем по типам с перекрытиями (Apple даёт непересекающиеся в пределах стадии)
        for start, end, s in segs:
            h = (end - start).total_seconds() / 3600.0
            if s in _ASLEEP:
                asleep += h
                if s == "HKCategoryValueSleepAnalysisAsleepDeep":
                    deep += h
                elif s == "HKCategoryValueSleepAnalysisAsleepREM":
                    rem += h
                elif s == "HKCategoryValueSleepAnalysisAsleepCore":
                    core += h
            elif s in _AWAKE:
                awake += h
            elif s in _INBED:
                inbed += h
        if store.sleep_hours is None or asleep > store.sleep_hours:
            store.sleep_hours = asleep
        if store.sleep_inbed is None or inbed > store.sleep_inbed:
            store.sleep_inbed = inbed or asleep + awake
        store.sleep_awake = awake or store.sleep_awake
        store.sleep_deep = deep or store.sleep_deep
        store.sleep_rem = rem or store.sleep_rem
        store.sleep_core = core or store.sleep_core


# --------------------------------------------------------------------------
# Строки, отчёт
# --------------------------------------------------------------------------


def _int(value: float | None) -> int | None:
    return None if value is None else int(round(value))


def _daily_rows(data: AppleData) -> list[tuple]:
    """Строки ``aw_daily`` в порядке :data:`DAILY_COLUMNS`."""
    rows: list[tuple] = []
    for day in sorted(data.daily):
        s = data.daily[day]
        rows.append((
            day,
            s.rhr, s.hrv, s.hrv_n, s.resp, s.wrist, s.spo2,
            s.sleep_hours, s.sleep_inbed, s.sleep_start, s.sleep_end,
            s.sleep_deep, s.sleep_rem, s.sleep_core, s.sleep_awake,
            _int(s.steps), s.active_kcal or None, s.basal_kcal or None,
            s.exercise_min or None, s.stand_hours or None,
            (s.walking_sum / s.walking_n) if s.walking_n else None,
            s.vo2max,
            (s.effort_sum / s.effort_n) if s.effort_n else None,
        ))
    return rows


def _workout_rows(data: AppleData) -> list[dict]:
    """Строки тренировок; усилие уже подписано в :func:`_finalize`."""
    return list(data.workouts)


def _match_effort(
    scores: list[tuple[datetime, datetime, float]],
    start: datetime | None,
    end: datetime | None,
) -> float | None:
    """Значение усилия, чей интервал заметнее всего пересекается с тренировкой."""
    if start is None or end is None:
        return None
    best = None
    best_overlap = 0.0
    for score_start, score_end, value in scores:
        overlap = (min(end, score_end) - max(start, score_start)).total_seconds()
        if overlap > best_overlap:
            best_overlap = overlap
            best = value
    return best


@dataclass(slots=True)
class AppleLoadReport:
    """Итог разбора и (при записи) загрузки выгрузки."""

    window: str
    records: int
    workouts: int
    days: int
    hr_samples: int
    sleep_segments: int
    bodytemp: int
    first: str | None = None
    last: str | None = None
    written: bool = False

    def lines(self) -> list[str]:
        head = "записано" if self.written else "проверка (без записи)"
        out = [f"Apple Health: {head}, окно с {self.window}"]
        out.append(
            f"  записей: {self.records}, тренировок: {self.workouts}, "
            f"дней: {self.days}, пульса: {self.hr_samples}"
        )
        out.append(
            f"  сон: {self.sleep_segments} сегментов, "
            f"температура (ручная): {self.bodytemp}"
        )
        if self.first:
            out.append(f"  период: {self.first} … {self.last}")
        return out


def summarize(data: AppleData, window: str, written: bool = False) -> AppleLoadReport:
    rows = _daily_rows(data)
    days = [row[0] for row in rows]
    return AppleLoadReport(
        window=window,
        records=sum(data.counts.values()),
        workouts=len(data.workouts),
        days=len(rows),
        hr_samples=len(data.hr),
        sleep_segments=len(data.sleep),
        bodytemp=len(data.bodytemp),
        first=min(days) if days else None,
        last=max(days) if days else None,
        written=written,
    )


# --------------------------------------------------------------------------
# Запись и точка входа
# --------------------------------------------------------------------------


def write_apple(conn: sqlite3.Connection, data: AppleData) -> None:
    """Пересобрать таблицы ``aw_*`` из разобранной выгрузки.

    Таблицы чистятся целиком: выгрузка - это снимок, а не инкремент, поэтому
    перезапись даёт ровно то, что в файле, и не оставляет хвостов от прошлых
    прогонов. ``sets`` не затрагивается.
    """
    conn.executescript(_SCHEMA)
    for table in ("aw_daily", "aw_sleep", "aw_workouts", "aw_bodytemp",
                  "aw_hr", "aw_ecg", "aw_meta"):
        conn.execute(f"DELETE FROM {table}")

    placeholders = ", ".join("?" * len(DAILY_COLUMNS))
    conn.executemany(
        f"INSERT INTO aw_daily ({', '.join(DAILY_COLUMNS)}) VALUES ({placeholders})",
        _daily_rows(data),
    )
    conn.executemany("INSERT INTO aw_sleep VALUES (?, ?, ?, ?)", data.sleep)
    conn.executemany("INSERT INTO aw_hr VALUES (?, ?, ?, ?)", data.hr)
    conn.executemany("INSERT INTO aw_bodytemp VALUES (?, ?, ?, ?)", data.bodytemp)
    conn.executemany("INSERT INTO aw_ecg VALUES (?, ?)", data.ecg)
    conn.executemany(
        "INSERT INTO aw_workouts (start_ts, end_ts, date, activity, duration_min, "
        "energy_kcal, energy_basal_kcal, distance_km, avg_hr, max_hr, mets, "
        "effort, effort_estimated, source) VALUES (:start_ts, :end_ts, :date, "
        ":activity, :duration_min, :energy_kcal, :energy_basal_kcal, :distance_km, "
        ":avg_hr, :max_hr, :mets, :effort, :effort_estimated, :source)",
        _workout_rows(data),
    )
    conn.executemany("INSERT OR REPLACE INTO aw_meta VALUES (?, ?)", list(data.meta.items()))
    conn.commit()


def default_xml() -> Path:
    """Найти XML выгрузки: самый большой ``*.xml``, кроме CDA-дубля."""
    candidates = [Path(p) for p in glob.glob(str(APPLE_DIR / "*.xml"))]
    if not candidates:
        return APPLE_DIR / "export.xml"
    real = [p for p in candidates if "cda" not in p.name.lower()]
    pool = real or candidates
    return max(pool, key=lambda p: p.stat().st_size)


# --------------------------------------------------------------------------
# Чтение из базы: ноутбук и отчёт
# --------------------------------------------------------------------------


def coverage(conn: sqlite3.Connection, window: str | None = DEFAULT_WINDOW) -> dict:
    """Насколько заполнены часовые метрики в базе, по колонкам.

    Заполняемость здесь - не мелочь: именно по ней видно, что метрика
    физически есть в окне, а не что её колонка существует.
    """
    where = "WHERE date >= ?" if window else ""
    params = (window,) if window else ()
    row = conn.execute(
        f"""SELECT COUNT(*) AS days,
                   COUNT(rhr) AS rhr, COUNT(hrv_sdnn) AS hrv,
                   COUNT(resp_rate) AS resp, COUNT(wrist_temp) AS wrist,
                   COUNT(spo2) AS spo2, COUNT(sleep_hours) AS sleep,
                   MIN(date) AS first, MAX(date) AS last
            FROM aw_daily {where}""",
        params,
    ).fetchone()
    hr = conn.execute(
        "SELECT COUNT(*) FROM aw_hr "
        + ("WHERE date >= ? " if window else ""), params
    ).fetchone()
    workouts = conn.execute(
        "SELECT COUNT(*) FROM aw_workouts "
        + ("WHERE date >= ? " if window else ""), params
    ).fetchone()
    return {
        "window": window,
        "days": row["days"],
        "first": row["first"],
        "last": row["last"],
        "rhr": row["rhr"], "hrv": row["hrv"], "resp": row["resp"],
        "wrist": row["wrist"], "spo2": row["spo2"], "sleep": row["sleep"],
        "hr_samples": hr[0],
        "workouts": workouts[0],
    }


def format_coverage(info: dict) -> str:
    """Строка покрытия для CLI и ноутбука."""
    line = (
        f"дней {info['days']}  ({info['first']} … {info['last']}), "
        f"тренировок {info['workouts']}, пульса {info['hr_samples']}"
    )
    filled = ", ".join(
        f"{name} {info[key]} ({info[key] * 100 // max(1, info['days'])}%)"
        for key, name in (("rhr", "RHR"), ("hrv", "ВСР"), ("resp", "ЧДД"),
                          ("wrist", "t запястья"), ("spo2", "SpO2"),
                          ("sleep", "сон"))
    )
    return f"{line}\n{filled}"


#: Колонки ``aw_daily``, заполняемость которых показывается по годам.
COVERAGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("rhr", "RHR"),
    ("hrv_sdnn", "ВСР"),
    ("resp_rate", "ЧДД"),
    ("wrist_temp", "t запястья"),
    ("spo2", "SpO2"),
    ("sleep_hours", "сон"),
)


def coverage_by_year(
    conn: sqlite3.Connection, window: str | None = DEFAULT_WINDOW
) -> list[dict]:
    """Заполненность часовых метрик по годам - строка на год.

    Это та же проверка «метрика физически есть в окне», что и в
    :func:`coverage`, но разрезанная по календарным годам: по ней видно, что
    до ``DEFAULT_WINDOW`` (2024-01-16) часов нет вовсе или они обрывками, и
    что полезное окно начинается именно там.
    """
    where = "WHERE date >= ?" if window else ""
    params = (window,) if window else ()
    counts = ", ".join(
        f"COUNT({column}) AS {column}" for column, _ in COVERAGE_COLUMNS
    )
    rows = conn.execute(
        f"""SELECT substr(date, 1, 4) AS year, COUNT(*) AS days, {counts}
            FROM aw_daily {where}
            GROUP BY substr(date, 1, 4) ORDER BY year""",
        params,
    ).fetchall()
    workouts = {
        row["year"]: row["n"]
        for row in conn.execute(
            "SELECT substr(date, 1, 4) AS year, COUNT(*) AS n "
            "FROM aw_workouts GROUP BY year"
        )
    }
    out = []
    for row in rows:
        item = {"year": row["year"], "days": row["days"],
                "workouts": workouts.get(row["year"], 0)}
        item.update({name: row[column] for column, name in COVERAGE_COLUMNS})
        out.append(item)
    return out


def format_coverage_by_year(rows: list[dict]) -> str:
    """Таблица покрытия по годам для CLI и ноутбука."""
    headers = ["год", "дней"] + [name for _, name in COVERAGE_COLUMNS] + ["тренир."]
    body = [
        [r["year"], r["days"]] + [r[name] for _, name in COVERAGE_COLUMNS]
        + [r["workouts"]]
        for r in rows
    ]
    return table(headers, body)


def daily(
    conn: sqlite3.Connection, window: str | None = DEFAULT_WINDOW
) -> list[dict]:
    """Ряд физиологии по дням для графиков в ноутбуке."""
    where = "WHERE date >= ?" if window else ""
    params = (window,) if window else ()
    rows = conn.execute(
        f"""SELECT date, rhr, hrv_sdnn, resp_rate, wrist_temp, spo2,
                   sleep_hours, steps
            FROM aw_daily {where} ORDER BY date""",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def load(
    xml: str | os.PathLike | None = None,
    db: str | os.PathLike = etl.DEFAULT_DB,
    window: str | None = DEFAULT_WINDOW,
    check: bool = False,
) -> AppleLoadReport:
    """Разобрать выгрузку; при ``check=False`` записать в базу ``aw_*``."""
    path = Path(xml) if xml else default_xml()
    if not path.exists():
        raise FileNotFoundError(f"нет выгрузки Apple Health: {path}")
    data = parse_apple(path, window)
    report = summarize(data, window or "вся история", written=not check)
    if check:
        return report
    conn = etl.connect(db)
    try:
        write_apple(conn, data)
    finally:
        conn.close()
    return report
