"""Признаки и модели: можно ли увидеть болезнь заранее по нагрузке и часам.

Постановка задачи честная и узкая. Эпизодов болезни в окне **единицы**
(2-6 в год), поэтому «классификатор на каждый день» здесь был бы обманом:
при таком ``n`` любая модель запоминает даты. Вместо этого строится **система
алерта на эпизод** - «за 14 дней до болезни признаки выглядели так» - и
описательная статистика. ML тут для проверки гипотез, а не для «предскажи
болезнь» (см. §6.4 плана).

Что считается:

- **Признаки** (:func:`build_features`) - на каждый день непрерывного ряда:
  личные z-скоры часовых метрик, долг сна, разброс времени отбоя, внешняя
  нагрузка (ACWR, EWMA-ACWR, монотонность, стрейн, недельный тоннаж),
  флаг просадки e1RM, взаимодействие «недосып × тоннаж». С v2 добавлены
  **тренды и разрывы серии**: ``hrv_slope3/5``, ``rhr_slope3/5`` (наклон
  z-ряда, не уровень), ``cum_tonnage_10/21``, ``days_since_training``,
  ``gap_14``, «долг × тоннаж за 21 день».
- **Две метки** (:func:`build_labels`) - старая day-классификация
  (``y``, окно 14 дней) и **v2** (``y_onset``): «эпизод начнётся в
  ``(t, t + 7]``», плюс флаг ``at_risk`` - дни внутри эпизода из обучения
  исключены.
- **Протокол v1** (:func:`leave_one_episode_out`) - каждый эпизод по очереди
  уходит в тест целиком; случайные фолды запрещены, иначе утечка времени.
  Метрика - recall эпизодов и ложные срабатывания в месяц, плюс
  обязательный бейзлайн «только пульс и сон».
- **Протокол v2** (:func:`onset_cv`) - то же поэпизодное разделение, но
  постановка «риск на горизонте» (landmark survival): P(onset в
  ``(t, t+H]`` | жив на t). Метрики событийные - ``lead_time`` первого
  алерта, поимка эпизодов с ``lead >= H_MIN = 3``, ложные алерты в месяц.
  Буквальный h(t) = P(onset в день t) здесь не берётся: признаки самого
  дня t (ВСР уже подняты) учат модель предсказывать «сегодня болен», а не
  «через 3-5 дней».
- **Честная оценка** (:func:`walk_forward`, :func:`matched_auc`) - главная
  оценка v2: день за днём, трейн только по прошлому, и вместо порога 0.5
  (для ``class_weight=balanced`` он ничего не значит) - operating point:
  порог = квантиль прошлых вероятностей под бюджет ``b`` алерт-дней в месяц.
  ``recall@бюджет`` сравнивается с двумя нулями - случайным алертом того же
  размера и сезонным календарём; AUC против далёкого контроля (15-28 дней
  до начала) даётся с **bootstrap-CI по эпизодам**, потому что дни одного
  эпизода не независимы.
- **Лаг-анализ** (:func:`lag_analysis`) - за сколько дней до эпизода нагрузка
  и физиология отклоняются сильнее всего.
- **Красный флаг** (:func:`red_flag_report`) - операционализация Meeusen et al.
  (2013): регресс e1RM + падение ВСР + долг сна + высокая монотонность.

``numpy`` и ``scikit-learn`` - **необязательная** зависимость (``koilog[ml]``).
Модуль импортируется и без них: тогда функции обучения поднимут понятную
ошибку, а построение признаков (чистая стандартная библиотека) продолжит
работать. Остальные модули ``health`` их не тянут.
"""

from __future__ import annotations

import json
import random
import sqlite3
import statistics
import warnings
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date as _date, timedelta as _timedelta

from . import illness as health_illness
from . import load as health_load
from .text import table

try:  # pragma: no cover - зависит от окружения
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None

try:  # pragma: no cover
    from sklearn.linear_model import LogisticRegression as _LogReg
except ImportError:  # pragma: no cover
    _LogReg = None

try:  # pragma: no cover
    from sklearn.ensemble import GradientBoostingClassifier as _GBM
except ImportError:  # pragma: no cover
    _GBM = None

try:  # pragma: no cover
    from sklearn.preprocessing import StandardScaler as _Scaler
except ImportError:  # pragma: no cover
    _Scaler = None

#: Есть ли необязательная зависимость ``koilog[ml]``.
HAS_ML = _np is not None and _LogReg is not None

__all__ = [
    "BASELINE_FEATURES",
    "CAUSAL_LABEL_RULE",
    "HAS_ML",
    "HORIZON",
    "H_MIN",
    "H_ONSET",
    "MIN_HISTORY",
    "MODEL_FEATURES",
    "V2_BASELINE_FEATURES",
    "V2_FEATURES",
    "build_features",
    "build_labels",
    "causal_score",
    "causal_scores",
    "lag_analysis",
    "leave_one_episode_out",
    "matched_auc",
    "onset_cv",
    "red_flag_report",
    "report",
    "walk_forward",
    "walk_probs",
]

#: Горизонт алерта: болезнь предсказывается за столько дней до начала.
HORIZON = 14

#: Горизонт ML v2: день ``t`` положителен, если эпизод начнётся в
#: ``(t, t + H_ONSET]`` («риск на горизонте», landmark survival - см.
#: докстринг :func:`leave_one_episode_out_onset`).
H_ONSET = 7

#: Минимальный lead time v2: срабатывание ближе чем за ``H_MIN`` дней до
#: начала эпизода опоздало и поимкой не считается.
H_MIN = 3

#: Окно личного базлайна для z-скоров.
BASELINE_DAYS = 60

#: Признаки основной модели.
MODEL_FEATURES: tuple[str, ...] = (
    "z_rhr", "z_hrv", "z_resp", "z_wrist", "z_spo2",
    "sleep_debt7", "sleep_start_sd7",
    "acwr", "acwr_ewma", "monotony", "strain", "weekly_tonnage",
    "rm_regress", "interaction",
)

#: Признаки ML v2: к основной модели добавлены тренды и разрывы серии -
#: то, что отстаёт от «уровня» и потому раньше предупреждает (§9.1 плана).
V2_FEATURES: tuple[str, ...] = MODEL_FEATURES + (
    "cum_tonnage_10", "cum_tonnage_21",
    "hrv_slope3", "hrv_slope5", "rhr_slope3", "rhr_slope5",
    "days_since_training", "gap_14", "interaction21",
)

#: Бейзлайн без нагрузки: только физиология.
BASELINE_FEATURES: tuple[str, ...] = (
    "z_rhr", "z_hrv", "z_resp", "z_wrist", "sleep_debt7",
)

#: Бейзлайн v2 без нагрузки: физиология плюс её же тренды.
V2_BASELINE_FEATURES: tuple[str, ...] = (
    "z_rhr", "z_hrv", "z_resp", "z_wrist", "sleep_debt7",
    "hrv_slope3", "rhr_slope3",
)

_FEATURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ml_features (
  date            TEXT PRIMARY KEY,
  y               INTEGER NOT NULL DEFAULT 0,
  y_onset         INTEGER NOT NULL DEFAULT 0,
  at_risk         INTEGER NOT NULL DEFAULT 1,
  episode_id      INTEGER,
  z_rhr           REAL, z_hrv REAL, z_resp REAL, z_wrist REAL, z_spo2 REAL,
  sleep_debt7     REAL,
  sleep_start_sd7 REAL,
  acwr            REAL, acwr_ewma REAL, monotony REAL, strain REAL,
  weekly_tonnage  REAL,
  rm_regress      INTEGER NOT NULL DEFAULT 0,
  interaction     REAL,
  cum_tonnage_10  REAL,
  cum_tonnage_21  REAL,
  hrv_slope3      REAL, hrv_slope5 REAL,
  rhr_slope3      REAL, rhr_slope5 REAL,
  days_since_training INTEGER NOT NULL DEFAULT 0,
  gap_14          INTEGER NOT NULL DEFAULT 0,
  interaction21   REAL,
  trained         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ml_labels (
  date       TEXT PRIMARY KEY,
  y          INTEGER NOT NULL DEFAULT 0,
  y_onset    INTEGER NOT NULL DEFAULT 0,
  episode_id INTEGER,
  onset      TEXT
);
"""

#: Колонки, добавляемые в старые базы (миграция без пересоздания).
_MIGRATION: tuple[tuple[str, str, str], ...] = (
    ("ml_features", "y_onset", "INTEGER NOT NULL DEFAULT 0"),
    ("ml_features", "at_risk", "INTEGER NOT NULL DEFAULT 1"),
    ("ml_features", "cum_tonnage_10", "REAL"),
    ("ml_features", "cum_tonnage_21", "REAL"),
    ("ml_features", "hrv_slope3", "REAL"),
    ("ml_features", "hrv_slope5", "REAL"),
    ("ml_features", "rhr_slope3", "REAL"),
    ("ml_features", "rhr_slope5", "REAL"),
    ("ml_features", "days_since_training", "INTEGER NOT NULL DEFAULT 0"),
    ("ml_features", "gap_14", "INTEGER NOT NULL DEFAULT 0"),
    ("ml_features", "interaction21", "REAL"),
    ("ml_labels", "y_onset", "INTEGER NOT NULL DEFAULT 0"),
)

_FEATURE_COLUMNS: tuple[str, ...] = (
    # метки (y, y_onset, at_risk, episode_id) сюда не входят: их пишет
    # build_labels, здесь - только признаки
    "date",
    "z_rhr", "z_hrv", "z_resp", "z_wrist", "z_spo2",
    "sleep_debt7", "sleep_start_sd7", "acwr", "acwr_ewma", "monotony",
    "strain", "weekly_tonnage", "rm_regress", "interaction",
    "cum_tonnage_10", "cum_tonnage_21",
    "hrv_slope3", "hrv_slope5", "rhr_slope3", "rhr_slope5",
    "days_since_training", "gap_14", "interaction21", "trained",
)


# --------------------------------------------------------------------------
# Признаки
# --------------------------------------------------------------------------


def _require_ml() -> None:
    if not HAS_ML:
        raise RuntimeError(
            "нужны numpy и scikit-learn; поставь:  pip install koilog[ml]"
        )


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _load_rows(conn: sqlite3.Connection, window: str | None) -> list[dict]:
    rows = conn.execute(
        "SELECT hd.date, hd.trained, hd.rhr, hd.hrv_sdnn, hd.sleep_hours, "
        "ad.resp_rate, ad.wrist_temp, ad.spo2 "
        "FROM health_days hd LEFT JOIN aw_daily ad ON ad.date = hd.date "
        + ("WHERE hd.date >= ? " if window else "")
        + "ORDER BY hd.date",
        (window,) if window else (),
    ).fetchall()
    external = {
        row["date"]: dict(row)
        for row in conn.execute(
            "SELECT date, tonnage, acwr, acwr_ewma, monotony, strain, "
            "weekly_tonnage, rm_regress FROM load_daily "
            + ("WHERE date >= ? " if window else "")
            + "ORDER BY date",
            (window,) if window else (),
        )
    }
    out = []
    for row in rows:
        item = dict(row)
        item.update(external.get(row["date"], {}))
        out.append(item)
    return out


def _sleep_debt(sleeps: list[float | None]) -> list[float]:
    """Долг сна: сумма недобора против базлайна за 7 дней.

    Базлайн - личная медиана сна по прошлым 30 наблюдениям. Недобор считается
    по каждому дню (отрицательный вклад = 0): пересып не «отменяет» недосып
    задним числом, а именно накопление недобора связывают с падением
    иммунитета.
    """
    out: list[float] = []
    history: list[float] = []
    for value in sleeps:
        baseline = statistics.median(history[-30:]) if len(history) >= 7 else None
        debt = 0.0
        if baseline is not None and value is not None:
            debt = max(0.0, baseline - value)
        out.append(debt)
        if value is not None:
            history.append(value)
    # накопление за 7 дней
    return [sum(out[max(0, i - 6):i + 1]) for i in range(len(out))]


def _sleep_start_sd(rows: list[dict]) -> list[float | None]:
    conn_starts = [row.get("sleep_start") for row in rows]
    out: list[float | None] = []
    for index in range(len(conn_starts)):
        window = [s for s in conn_starts[max(0, index - 6):index + 1] if s]
        if len(window) < 3:
            out.append(None)
            continue
        minutes = [_minutes(s) for s in window]
        out.append(statistics.pstdev(minutes))
    return out


def _minutes(clock: str) -> float:
    hour, _, minute = clock.partition(":")
    value = int(hour) * 60 + int(minute)
    # время отбоя после полуночи (00:00-12:00) относится к прошлой ночи
    return value if value >= 12 * 60 else value + 24 * 60


def _rolling_sum(values: list[float], window: int) -> list[float | None]:
    """Сумма за ``window`` дней, отстающая (включает текущий день).

    Пока в окне не набралось ``window`` наблюдений - ``None``: честнее
    начинать серию с полного окна, чем учить модель на неполных суммах.
    """
    out: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < window:
            out.append(None)
        else:
            out.append(sum(values[index - window + 1:index + 1]))
    return out


def _slope(series: list[float | None], window: int) -> list[float | None]:
    """Наклон линейного тренда за последние ``window`` дней (в единицах/день).

    Это **тренд, а не уровень**: z-скор держится высоким и во время, и
    после болезни, а наклон показывает, куда ряд идёт. Нужны все ``window``
    значений подряд без пропусков, иначе ``None``.
    """
    out: list[float | None] = []
    for index in range(len(series)):
        chunk = series[index - window + 1:index + 1] if index + 1 >= window else []
        if len(chunk) < window or any(value is None for value in chunk):
            out.append(None)
            continue
        # наклон МНК по x = 0..window-1: Cov(x, y) / Var(x)
        mean_x = (window - 1) / 2
        mean_y = sum(chunk) / window
        var_x = sum((x - mean_x) ** 2 for x in range(window))
        cov = sum((x - mean_x) * (y - mean_y)
                  for x, y in enumerate(chunk))
        out.append(cov / var_x)
    return out


def _gap_lengths(trained: list[int]) -> list[int]:
    """Длина текущего «разрыва серии»: дней без тренировки подряд, включая сегодня."""
    out: list[int] = []
    streak = 0
    for flag in trained:
        streak = 0 if flag else streak + 1
        out.append(streak)
    return out


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Миграция старых ``ml_features``/``ml_labels``: добавить новые колонки.

    Таблицы материализуются заново при каждом запуске, но пересоздавать их
    ради колонок жалко - вдруг лежат в долгой личной базе. ``ALTER TABLE``
    добавляет недостающее и не трогает остальное.
    """
    for table, column, decl in _MIGRATION:
        existing = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if _has_table(conn, table) and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def build_features(
    conn: sqlite3.Connection,
    window: str | None = health_load.DEFAULT_WINDOW,
) -> int:
    """Материализовать ``ml_features``; вернуть число дней."""
    conn.executescript(_FEATURE_SCHEMA)
    _ensure_columns(conn)
    if not (_has_table(conn, "aw_daily") and _has_table(conn, "load_daily")):
        raise RuntimeError(
            "нет aw_daily/load_daily - сначала:  "
            "python -m koilog apple-load && python -m koilog health-load"
        )
    rows = _load_rows(conn, window)
    if not rows:
        conn.execute("DELETE FROM ml_features")
        conn.commit()
        return 0

    # время отбоя нужно для консистентности сна - подтягиваем из aw_daily
    starts = {row["date"]: row["sleep_start"]
              for row in conn.execute("SELECT date, sleep_start FROM aw_daily")}
    for row in rows:
        row["sleep_start"] = starts.get(row["date"])

    z_rhr = health_illness.zscore([r["rhr"] for r in rows], BASELINE_DAYS)
    z_hrv = health_illness.zscore([r["hrv_sdnn"] for r in rows], BASELINE_DAYS)
    z_resp = health_illness.zscore([r["resp_rate"] for r in rows], BASELINE_DAYS)
    z_wrist = health_illness.zscore([r["wrist_temp"] for r in rows], BASELINE_DAYS)
    z_spo2 = health_illness.zscore([r["spo2"] for r in rows], BASELINE_DAYS)
    debt7 = _sleep_debt([r["sleep_hours"] for r in rows])
    start_sd = _sleep_start_sd(rows)

    tonnage = [row.get("tonnage") or 0.0 for row in rows]
    cum10 = _rolling_sum(tonnage, 10)
    cum21 = _rolling_sum(tonnage, 21)
    hrv3, hrv5 = _slope(z_hrv, 3), _slope(z_hrv, 5)
    rhr3, rhr5 = _slope(z_rhr, 3), _slope(z_rhr, 5)
    gaps = _gap_lengths([row["trained"] or 0 for row in rows])

    payload = []
    for index, row in enumerate(rows):
        tonnage_week = row.get("weekly_tonnage") or 0.0
        debt = debt7[index]
        cum21_value = cum21[index]
        payload.append((
            row["date"], z_rhr[index], z_hrv[index], z_resp[index],
            z_wrist[index], z_spo2[index], debt, start_sd[index],
            row.get("acwr"), row.get("acwr_ewma"), row.get("monotony"),
            row.get("strain"), row.get("weekly_tonnage"),
            row.get("rm_regress") or 0, debt * tonnage_week,
            cum10[index], cum21_value,
            hrv3[index], hrv5[index], rhr3[index], rhr5[index],
            gaps[index], 1 if gaps[index] >= 14 else 0,
            debt * cum21_value if cum21_value is not None else None,
            row["trained"] or 0,
        ))
    conn.execute("DELETE FROM ml_features")
    placeholders = ", ".join("?" * len(_FEATURE_COLUMNS))
    conn.executemany(
        f"INSERT INTO ml_features ({', '.join(_FEATURE_COLUMNS)}) "
        f"VALUES ({placeholders})",
        payload,
    )
    conn.commit()
    return len(payload)


# --------------------------------------------------------------------------
# Метки
# --------------------------------------------------------------------------


def build_labels(
    conn: sqlite3.Connection,
    window: str | None = health_load.DEFAULT_WINDOW,
    horizon: int = HORIZON,
    onset_horizon: int = H_ONSET,
) -> int:
    """Разметить дни.

    Две метки:

    - ``y=1`` за ``horizon`` дней до начала эпизода - **старая**
      day-классификация, остаётся как бейзлайн (§9.1);
    - ``y_onset=1``, если эпизод начнётся в ``(t, t + onset_horizon]``
      («риск на горизонте», H_ONSET = 7) - метка ML v2;
    - ``at_risk=0`` внутри эпизода: эти дни «не жив» и в обучение
      hazard-модели не идут (P(onset | не было до t)).

    ``gap_only`` в обучение не берётся (§5): смысла в «перерыве без болезни»
    как в положительном классе нет, а как в отрицательном он бы врал, потому
    что внутри перерыва тренировок и не было.

    **Привязка к ближайшему будущему эпизоду.** Окна плотных кластеров
    (onset'ы ближе горизонта) перекрываются, и день принадлежал сразу двум
    эпизодам: ``episode_id`` затирал тот, что обработан позже, а ``onset``
    жил при этом от первого. В CV это давало ``lead`` до 14 при окне 7 и
    «вымышленные» onset'ы. Теперь каждый день привязан к **первому
    будущему** onset'у — к тому, что этот день и должен предсказывать;
    окно второго эпизода кластера начинается после первого.
    """
    conn.executescript(_FEATURE_SCHEMA)
    _ensure_columns(conn)
    episodes = [e for e in health_illness.episodes(conn) if e.level != "gap_only"]
    dates = [row["date"] for row in conn.execute(
        "SELECT date FROM ml_features ORDER BY date")]
    index_of = {day: i for i, day in enumerate(dates)}

    # дни самого эпизода — «не жив»: в hazard не идут
    for episode in episodes:
        if episode.start not in index_of:
            continue
        for position in range(index_of[episode.start], len(dates)):
            day = dates[position]
            if day > episode.end:
                break
            conn.execute(
                "UPDATE ml_features SET at_risk = 0 WHERE date = ?", (day,))

    onsets = sorted((e.start, e.id) for e in episodes if e.start in index_of)
    starts = [start for start, _ in onsets]
    labels: dict[str, tuple] = {}
    for day in dates:
        position = bisect_right(starts, day)  # первый onset строго после дня
        y = onset_label = 0
        eid = stored_onset = None
        if position < len(onsets):
            first_start, first_id = onsets[position]
            lead_days = (_date.fromisoformat(first_start)
                         - _date.fromisoformat(day)).days
            if lead_days <= horizon:
                y = 1
                eid = first_id
                stored_onset = first_start
                if lead_days <= onset_horizon:
                    onset_label = 1
        labels[day] = (y, onset_label, eid, stored_onset)

    conn.execute("DELETE FROM ml_labels")
    conn.executemany(
        "INSERT INTO ml_labels (date, y, y_onset, episode_id, onset) "
        "VALUES (?, ?, ?, ?, ?)",
        [(day, y, onset_label, eid, stored_onset)
         for day, (y, onset_label, eid, stored_onset) in labels.items()],
    )
    conn.execute(
        "UPDATE ml_features SET y = COALESCE("
        "(SELECT y FROM ml_labels WHERE ml_labels.date = ml_features.date), 0), "
        "y_onset = COALESCE("
        "(SELECT y_onset FROM ml_labels "
        " WHERE ml_labels.date = ml_features.date), 0), "
        "episode_id = (SELECT episode_id FROM ml_labels "
        "WHERE ml_labels.date = ml_features.date)"
    )
    conn.commit()
    return sum(1 for y, _, _, _ in labels.values() if y)


# --------------------------------------------------------------------------
# Матрица и протокол
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Dataset:
    """День × признаки плюс метки, «свой» эпизод и флаг риска."""

    dates: list[str]
    X: "object"  # numpy.ndarray
    y: "object"   # numpy.ndarray (старая метка)
    y_onset: "object"  # numpy.ndarray (метка v2)
    at_risk: "object"  # numpy.ndarray (день вне эпизода)
    episode: list[int | None]


def _dataset(conn: sqlite3.Connection, features: tuple[str, ...]) -> Dataset:
    _require_ml()
    columns = ", ".join(features)
    rows = conn.execute(
        f"SELECT date, y, y_onset, at_risk, episode_id, {columns} "
        "FROM ml_features ORDER BY date"
    ).fetchall()
    dates = [row["date"] for row in rows]
    y = _np.array([row["y"] for row in rows], dtype=float)
    y_onset = _np.array([row["y_onset"] for row in rows], dtype=float)
    at_risk = _np.array([row["at_risk"] for row in rows], dtype=bool)
    episode = [row["episode_id"] for row in rows]
    matrix = _np.array(
        [[_num(row[name]) for name in features] for row in rows], dtype=float
    )
    return Dataset(dates, matrix, y, y_onset, at_risk, episode)


def _num(value) -> float:
    return float("nan") if value is None else float(value)


def _impute(train: "object", other: "object") -> tuple["object", "object"]:
    with warnings.catch_warnings():
        # столбец может быть целиком NaN (метрики часов есть не каждый день)
        warnings.simplefilter("ignore", RuntimeWarning)
        medians = _np.nanmedian(train, axis=0)
    medians = _np.where(_np.isnan(medians), 0.0, medians)
    def fill(matrix):
        return _np.where(_np.isnan(matrix), medians, matrix)
    return fill(train), fill(other)


def leave_one_episode_out(
    conn: sqlite3.Connection,
    features: tuple[str, ...] = MODEL_FEATURES,
    horizon: int = HORIZON,
    model: str = "logreg",
) -> dict:
    """Оценить алерт, оставляя по одному эпизоду в тесте.

    Случайные фолды здесь нельзя: дни одного эпизода почти одинаковы, и
    попав частью в обучение, частью в тест, модель «узнаёт» эпизод по дате.
    Поэтому тест - это все дни-предвестники одного эпизода плюс столько же
    отрицательных дней рядом с ним (по календарю, без утечки из других
    эпизодов).

    Возвращает словарь с recall эпизодов, точностью/полнотой по дням и
    числом ложных срабатываний в месяц.
    """
    _require_ml()
    data = _dataset(conn, features)
    episodes = sorted({ep for ep in data.episode if ep is not None})
    if not episodes:
        return {"episodes": 0, "note": "нет подтверждённых/вероятных эпизодов"}

    y = data.y
    total_tp = total_fp = total_fn = 0
    caught = 0
    negatives = int((y == 0).sum())
    months = max(1.0, len(data.dates) / 30.44)
    for episode in episodes:
        test_pos = [i for i, ep in enumerate(data.episode) if ep == episode]
        train = [i for i in range(len(data.dates)) if data.episode[i] != episode]
        # если в обучении не осталось ни одного положительного дня (например,
        # эпизод всего один), учить не на чем: считаем дни пропущенными, а не
        # роняем протокол
        if len(set(y[train].tolist())) < 2:
            total_fn += len(test_pos)
            continue
        # контроль: отрицательные дни, ближайшие по календарю к эпизоду
        center = sum(test_pos) // len(test_pos)
        neg_idx = [i for i, value in enumerate(y) if value == 0]
        neg_idx.sort(key=lambda i: abs(i - center))
        test_neg = neg_idx[:len(test_pos)]
        test = test_pos + test_neg

        x_train, x_test = _impute(data.X[train], data.X[test])
        scaler = _Scaler()
        x_train = scaler.fit_transform(x_train)
        x_test = scaler.transform(x_test)
        estimator = _estimator(model)
        estimator.fit(x_train, y[train])
        predicted = estimator.predict(x_test)
        for position, index in enumerate(test):
            if index in test_pos:
                if predicted[position] == 1:
                    total_tp += 1
                else:
                    total_fn += 1
            elif predicted[position] == 1:
                total_fp += 1
        if any(predicted[test.index(i)] == 1 for i in test_pos):
            caught += 1

    precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 0.0
    recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 0.0
    return {
        "episodes": len(episodes),
        "episodes_caught": caught,
        "episode_recall": caught / len(episodes),
        "day_precision": precision,
        "day_recall": recall,
        "false_positive_days": total_fp,
        "false_positives_per_month": total_fp / months,
        "negatives": negatives,
        "model": model,
    }


def onset_cv(
    conn: sqlite3.Connection,
    features: tuple[str, ...] = V2_FEATURES,
    horizon: int = H_ONSET,
    min_lead: int = H_MIN,
    model: str = "logreg",
) -> dict:
    """Оценить **v2**: риск начала эпизода на горизонте ``horizon`` дней.

    Постановка - дискретная выживаемость в landmark-форме (по плану §9.1,
    «риск на горизонте»): метка дня ``t`` - «эпизод начнётся в
    ``(t, t + horizon]``», то есть P(onset | жив на t). Буквальный
    h(t) = P(onset в день t) здесь хуже: признаки самого дня t (ВСР и пульс
    уже подняты) учат модель предсказывать «сегодня болен», а не
    «через 3-5 дней».

    Протокол тот же, что в :func:`leave_one_episode_out` - по одному
    эпизоду в тест, случайных фолдов нет. Дни **внутри** эпизодов
    (``at_risk=0``) из обучения исключены целиком: они «не жив», и
    hazard без них не имеет смысла.

    Метрики событийные: поимка эпизода с ``lead_time >= min_lead``,
    медианный lead первого алерта и ложные алерты в месяц.
    """
    _require_ml()
    data = _dataset(conn, features)
    y = data.y_onset
    episodes = sorted({ep for ep in data.episode if ep is not None})
    if not episodes:
        return {"episodes": 0, "note": "нет подтверждённых/вероятных эпизодов"}

    at_risk = data.at_risk
    caught = late = total_fp = 0
    leads: list[int] = []
    per_episode: list[dict] = []
    fp_days: list[str] = []
    months = max(1.0, len(data.dates) / 30.44)
    starts = {e.id: e.start for e in health_illness.episodes(conn)
              if e.level != "gap_only"}
    date_index = {day: i for i, day in enumerate(data.dates)}
    for episode in episodes:
        # в тест идут только дни **метки v2** (y_onset=1) и «живые»
        # (at_risk=1): дни за 8-14 дней до эпизода метки не имеют, и алерт
        # там - это ложный, а не «очень ранний»; дни внутри прошлого
        # эпизода - не «жив», и hazard их не рассматривает
        test_pos = [
            i for i, ep in enumerate(data.episode)
            if ep == episode and y[i] == 1 and at_risk[i]
        ]
        # onset берём из таблицы эпизодов, а не «последний день окна + 1»:
        # в плотном кластере окно обрезано, и такая реконструкция врала
        onset_date = starts.get(episode)
        if onset_date is None and test_pos:
            tail = test_pos[-1] + 1
            onset_date = data.dates[onset_position] if (
                (onset_position := test_pos[-1] + 1) < len(data.dates)) else None
        onset_position = date_index.get(onset_date) if onset_date else None
        if not test_pos or onset_position is None:
            per_episode.append({"episode": episode, "onset": onset_date,
                                "status": "no_window", "lead": None,
                                "alarms": 0, "alarm_day": None})
            continue
        train = [
            i for i in range(len(data.dates))
            if at_risk[i] and data.episode[i] != episode
        ]
        # если в обучении не осталось ни одного положительного дня (например,
        # эпизод всего один), учить не на чем: считаем пропущенным, а не
        # роняем протокол
        if len(set(y[train].tolist())) < 2:
            per_episode.append({"episode": episode, "onset": onset_date,
                                "status": "no_train", "lead": None,
                                "alarms": 0, "alarm_day": None})
            continue
        # контроль: «живые» дни без метки, ближайшие по календарю к эпизоду
        center = sum(test_pos) // len(test_pos)
        neg_idx = [i for i, value in enumerate(y) if value == 0 and at_risk[i]]
        neg_idx.sort(key=lambda i: abs(i - center))
        test_neg = neg_idx[:len(test_pos)]
        test = test_pos + test_neg

        x_train, x_test = _impute(data.X[train], data.X[test])
        scaler = _Scaler()
        x_train = scaler.fit_transform(x_train)
        x_test = scaler.transform(x_test)
        estimator = _estimator(model)
        estimator.fit(x_train, y[train])
        predicted = estimator.predict(x_test)

        # первый алерт = ближайший к эпизоду день, где модель сработала
        alarms = [i for pos, i in enumerate(test)
                  if i in test_pos and predicted[pos] == 1]
        alarms.sort()
        lead = None
        status = "missed"
        if alarms:
            lead = onset_position - alarms[0]
            if lead >= min_lead:
                status = "caught"
                caught += 1
                leads.append(lead)
            else:
                status = "late"
                late += 1
        per_episode.append({
            "episode": episode, "onset": onset_date, "status": status,
            "lead": lead, "alarms": len(alarms),
            "alarm_day": data.dates[alarms[0]] if alarms else None,
        })
        here_fp = [i for pos, i in enumerate(test)
                   if i in test_neg and predicted[pos] == 1]
        total_fp += len(here_fp)
        fp_days.extend(data.dates[i] for i in here_fp)

    median_lead = int(statistics.median(leads)) if leads else None
    return {
        "episodes": len(episodes),
        "episodes_caught": caught,
        "episode_recall": caught / len(episodes),
        "episodes_late": late,
        "median_lead": median_lead,
        "false_alarms": total_fp,
        "false_alarms_per_month": total_fp / months,
        "horizon": horizon,
        "min_lead": min_lead,
        "model": model,
        # поэпизодные детали - для аудита сравнения моделей и распределения
        # lead (медиана ровно на капе горизонта выглядит подозрительно);
        # fp_days - даты ложных дней, чтобы считать их стриками
        "per_episode": per_episode,
        "fp_days": fp_days,
    }


# --------------------------------------------------------------------------
# Честная оценка: различение и recall@бюджет
# --------------------------------------------------------------------------


def _roc_auc(scores: list[float], labels: list[int]) -> float | None:
    """ROC-AUC без sklearn: ранги Манна-Уитни, связи - средний ранг."""
    if not scores or len(scores) != len(labels):
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(order)
    position = 0
    while position < len(order):
        end = position
        while (end + 1 < len(order)
               and scores[order[end + 1]] == scores[order[position]]):
            end += 1
        average = (position + end) / 2 + 1
        for step in range(position, end + 1):
            ranks[order[step]] = average
        position = end + 1
    positives = [i for i, value in enumerate(labels) if value == 1]
    negatives = len(labels) - len(positives)
    if not positives or not negatives:
        return None
    rank_sum = sum(ranks[i] for i in positives)
    return (rank_sum - len(positives) * (len(positives) + 1) / 2) / (
        len(positives) * negatives)


def _average_precision(scores: list[float], labels: list[int]) -> float | None:
    """PR-AUC (average precision); у случайной модели = доля положительных."""
    total = sum(labels)
    if not total or len(scores) != len(labels):
        return None
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    found = 0
    precision_sum = 0.0
    for step, index in enumerate(order, start=1):
        if labels[index] == 1:
            found += 1
            precision_sum += found / step
    return precision_sum / total


def _quantile(values: list[float], fraction: float) -> float:
    """Квантиль ряда (ближайший индекс) - порог под бюджет алертов."""
    if not values:
        raise ValueError("пустой ряд")
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, index))]


def _day_before(day: str, other: str) -> bool:
    """``other`` - ровно следующий календарный день после ``day``."""
    return (_date.fromisoformat(other) - _date.fromisoformat(day)).days == 1


def _episode_bootstrap_auc(
    groups: list[list[float]],
    negatives: list[float],
    bootstrap: int,
    seed: int = 17,
) -> dict:
    """AUC «дни эпизодов против прочих дней» + CI, ресемплим эпизоды.

    Дни одного эпизода почти не независимы, поэтому интервал строится по
    эпизодам: берутся группы дней одного эпизода целиком. Отрицательные дни
    фиксированы - их много, и изменчивость интервала от них мала.
    """
    if not groups or not negatives:
        return {"auc": None, "low": None, "high": None, "bootstrap": 0}
    truth = [1] * sum(len(group) for group in groups) + [0] * len(negatives)
    point = _roc_auc(
        [value for group in groups for value in group] + negatives, truth)
    chooser = random.Random(seed)
    sample: list[float] = []
    for _ in range(max(1, bootstrap)):
        chosen = [chooser.choice(groups) for _ in groups]
        values = [value for group in chosen for value in group] + negatives
        marks = ([1] * sum(len(group) for group in chosen)
                 + [0] * len(negatives))
        value = _roc_auc(values, marks)
        if value is not None:
            sample.append(value)
    return {
        "auc": point,
        "low": _quantile(sample, 0.025) if sample else None,
        "high": _quantile(sample, 0.975) if sample else None,
        "bootstrap": len(sample),
    }


def _episode_bootstrap_delta(
    model_hits: list[float],
    random_hits: list[float],
    bootstrap: int,
    seed: int = 17,
) -> dict:
    """Разница поимки «модель минус случайный» + CI, ресемплим эпизоды.

    ``random_hits[i]`` - доля ресэмплов случайного алерта, поймавших эпизод
    ``i`` (усреднение по повторам, а не 0/1). Парный ресемплинг: эпизод
    берётся целиком со своими двумя значениями, поэтому интервал говорит
    именно о разнице на одном и том же наборе эпизодов, а не о двух
    раздельных интервалах.
    """
    if not model_hits or len(model_hits) != len(random_hits):
        return {"delta": None, "low": None, "high": None, "bootstrap": 0}
    deltas = [model - baseline
              for model, baseline in zip(model_hits, random_hits)]
    chooser = random.Random(seed)
    sample: list[float] = []
    for _ in range(max(1, bootstrap)):
        chosen = [chooser.choice(deltas) for _ in deltas]
        sample.append(sum(chosen) / len(chosen))
    return {
        "delta": sum(deltas) / len(deltas),
        "low": _quantile(sample, 0.025) if sample else None,
        "high": _quantile(sample, 0.975) if sample else None,
        "bootstrap": len(sample),
    }


#: Минимум прошлых ``at_risk``-дней, чтобы вообще оценивать день. До него
#: модель училась бы на слишком короткой истории, а положительных примеров
#: в ней ещё нет.
MIN_HISTORY = 30

#: Версия **причинного** правила меток (см. :func:`_causal_train`). Меняется,
#: если изменится правило метки, горизонт или окно трейна: старая история
#: скоров не переписывается, а помечается другим ``spec_hash`` в ledger'е
#: (:mod:`koilog.health.score`), и анализ сегментирует по нему.
CAUSAL_LABEL_RULE = "causal-v1"


def _predict(train_x, day_x, y, model: str) -> float:
    """Обучить модель на ``train_x``/``y`` и вернуть вероятность для ``day_x``.

    Общий шаг для причинного walk-forward (:func:`_walk_probs_data`) и
    дневного скора (:func:`causal_score`): если развести их по разным
    копиям, «скор дня» и оценка разойдутся, а это ровно то, чего быть не
    должно.
    """
    x_train, x_day = _impute(train_x, day_x)
    scaler = _Scaler()
    x_train = scaler.fit_transform(x_train)
    x_day = scaler.transform(x_day)
    estimator = _estimator(model)
    estimator.fit(x_train, y)
    return float(estimator.predict_proba(x_day)[0][1])


def _walk_probs_data(data: "Dataset", model: str,
                     min_history: int = MIN_HISTORY) -> list[float | None]:
    """Цикл walk-forward: вероятность на каждый день, трейн строго по прошлому.

    Метки берутся из ``ml_features.y_onset`` (полные данные): это
    **оракульная** ретро-оценка :func:`walk_forward`. Живой скор считается
    причинно (:func:`causal_score`, общий шаг обучения - :func:`_predict`),
    потому что ретро-метка дня ``t`` подглядывает исход после ``t``.
    """
    at_risk = data.at_risk
    probs: list[float | None] = [None] * len(data.dates)
    for t in range(len(data.dates)):
        # обучение строго на прошлом; до min_history дней истории и до
        # первого эпизода положительных нет - день не оценивается
        if not at_risk[t]:
            continue
        history = [i for i in range(t) if at_risk[i]]
        if len(history) < min_history:
            continue
        y = data.y_onset[history]
        if len(set(y.tolist())) < 2:
            continue
        probs[t] = _predict(data.X[history], data.X[[t]], y, model)
    return probs


def _plus_days(day: str, days: int) -> str:
    """ISO-дата ``day + days`` (сравнения ISO-строк лексикографичны)."""
    return (_date.fromisoformat(day) + _timedelta(days=days)).isoformat()


def _causal_train(
    dates: list[str],
    at_risk,
    starts: list[str],
    t: int,
    horizon: int = H_ONSET,
) -> tuple[list[int], list[float]]:
    """Индексы и метки трейна для eval-дня ``t`` - только известное к ``t``.

    Причинное правило (``CAUSAL_LABEL_RULE``) на день ``T = dates[t]`` для
    дня ``i`` с окном ``(dates[i], dates[i]+horizon]``:

    - метка **1**, если в окне есть onset, уже случившийся к ``T`` (``o <= T``);
    - метка **0**, если окно к ``T`` закрылось (``dates[i]+horizon <= T``),
      а onset'а в нём так и нет;
    - день **исключается**, если окно ещё открыто и исход неизвестен.

    Последнее - суть правки: «исход неизвестен → 0» записало бы ложные
    негативы в каждом дне перед эпизодом (ровно там, где скор и важен).
    Дни внутри эпизода (``at_risk=0``) не берутся, как и раньше.
    """
    index: list[int] = []
    labels: list[float] = []
    for i in range(t):
        if not at_risk[i]:
            continue
        day_i = dates[i]
        position = bisect_right(starts, day_i)   # первый onset строго после i
        onset = starts[position] if position < len(starts) else None
        edge = _plus_days(day_i, horizon)
        if onset is not None and onset <= edge and onset <= dates[t]:
            index.append(i)
            labels.append(1.0)
        elif edge <= dates[t]:
            index.append(i)
            labels.append(0.0)
    return index, labels


def _causal_score_at(
    data: "Dataset", starts: list[str], t: int, model: str,
    min_history: int = MIN_HISTORY,
) -> dict | None:
    """Скор дня ``t``, посчитанный только по известному к этому дню."""
    if not data.at_risk[t]:
        return {"scored": False, "reason": "day_inside_episode", "score": None,
                "n_train": None, "n_pos": None, "last_train_day": None}
    index, labels = _causal_train(data.dates, data.at_risk, starts, t)
    if len(index) < min_history or len(set(labels)) < 2:
        return {"scored": False, "reason": "not_enough_history", "score": None,
                "n_train": len(index), "n_pos": int(sum(labels)),
                "last_train_day": None}
    y = _np.array(labels, dtype=float)
    prob = _predict(data.X[index], data.X[[t]], y, model)
    return {
        "scored": True,
        "reason": None,
        "score": prob,
        "n_train": len(index),
        "n_pos": int(sum(labels)),
        "last_train_day": data.dates[index[-1]],
    }


def causal_score(
    conn: sqlite3.Connection,
    day: str | None = None,
    features: tuple[str, ...] = V2_FEATURES,
    model: str = "logreg",
    min_history: int = MIN_HISTORY,
) -> dict | None:
    """Скор одного дня ``day`` (по умолчанию - последнего) по причинному правилу.

    В отличие от :func:`walk_probs` (ретро-оценка на полных метках) здесь
    обучающая история обрезана по ``day``: метка дня, у которого окно ещё
    открыто, неизвестна и день исключается, а не обнуляется. Именно это
    число должна была бы выдать развёрнутая система, поэтому оно и идёт в
    ledger (:mod:`koilog.health.score`).

    Возвращает ``None``, если данных нет; иначе словарь со ``scored``,
    ``score`` и диагностикой ``n_train``/``n_pos``/``last_train_day``.
    """
    _require_ml()
    data = _dataset(conn, features)
    if not data.dates:
        return None
    t = None
    for i, value in enumerate(data.dates):
        if day is None or value <= day:
            t = i
    if t is None:
        return None
    starts = sorted(e.start for e in health_illness.episodes(conn)
                    if e.level != "gap_only")
    info = _causal_score_at(data, starts, t, model, min_history)
    return {"day": data.dates[t], **(info or {})}


def causal_scores(
    conn: sqlite3.Connection,
    features: tuple[str, ...] = V2_FEATURES,
    model: str = "logreg",
    min_history: int = MIN_HISTORY,
) -> tuple[list[str], list[dict | None]]:
    """Причинные скоры на все дни за один проход (холодный старт ledger'а).

    Тот же :func:`_causal_score_at`, что и у :func:`causal_score`, поэтому
    воспроизведение истории и живая запись - одно и то же число, а не две
    похожие формулы.
    """
    _require_ml()
    data = _dataset(conn, features)
    starts = sorted(e.start for e in health_illness.episodes(conn)
                    if e.level != "gap_only")
    out = [_causal_score_at(data, starts, t, model, min_history)
           for t in range(len(data.dates))]
    return data.dates, out


def walk_probs(
    conn: sqlite3.Connection,
    features: tuple[str, ...] = V2_FEATURES,
    model: str = "logreg",
    min_history: int = MIN_HISTORY,
) -> tuple[list[str], list[float | None]]:
    """``(даты, вероятности)``: скор на каждый день, трейн строго по прошлому.

    ``probs[t]`` равен ``None``, если день не оценивался: это дни внутри
    эпизода (``at_risk=0``, там «не жив»), первые ``min_history`` дней и дни
    до появления в прошлом обоих классов.

    Это **оракульная** ретро-оценка: метки ``y_onset`` построены по полным
    данным, поэтому день ``t`` учится на днях, чей исход стал известен
    только после ``t``. Оптимизм тут максимален за 1-2 дня до onset'а -
    ровно на тех lead, на которых стоит nowcast, - поэтому каузальная
    оценка (:func:`causal_score`) ниже, и именно она идёт в ledger
    (:mod:`koilog.health.score`), а эти числа остаются верхней границей.
    Разницу формализует причинное воспроизведение истории (см. README).
    """
    _require_ml()
    data = _dataset(conn, features)
    probs = _walk_probs_data(data, model, min_history)
    return data.dates, probs


def walk_forward(
    conn: sqlite3.Connection,
    features: tuple[str, ...] = V2_FEATURES,
    model: str = "logreg",
    budgets: tuple[float, ...] = (2.0, 4.0, 8.0),
    min_lead: int = H_MIN,
    nowcast_lead: int = 2,
    horizon: int = H_ONSET,
    season_months: tuple[int, ...] = (12, 1, 2),
    seed: int = 17,
    random_repeats: int = 50,
    auc_bootstrap: int = 500,
) -> dict:
    """День за днём, прогноз только по прошлому; порог - под бюджет.

    Дефолтный порог ``0.5`` на несбалансированных классах ничего не
    значит. Здесь на каждый день берётся квантиль **прошлых** вероятностей
    так, чтобы выходило ``b`` алерт-дней в месяц (``b`` из ``budgets``), -
    это и есть operating point. Главное в отчёте - recall эпизодов при
    фиксированном бюджете против двух нулей:

    - **случайный** алерт с тем же числом дней (берётся **реализованный**
      бюджет модели, а не номинальный: из-за плато вероятностей квантиль
      даёт меньше алертов, и сравнивать с номиналом было бы нечестно);
    - **сезонный календарь** - все дни ``season_months`` (зима, где эпизоды
      действительно скучены, см. кластеризацию стартов).

    Поимка - та же, что в :func:`onset_cv` (``lead >= min_lead``), чтобы
    цифры стояли в одной таблице, а не в двух разных определениях.

    Два способа не соврать про знаменатель:

    - **Уловимость эпизода** (``episodes_catchable``, ``episodes_unreachable``):
      часть эпизодов не поймана вовсе - их ``lead >= min_lead``-окно целиком
      лежит внутри болезни соседа («геометрия»), либо история ещё не накоплена
      («нет истории»). Делить recall на все эпизоды и приписывать это модели -
      самообман; в отчёте знаменатель - уловимые, а число неуловимых
      показывается отдельно.
    - **Nowcast** (``episode_recall_nowcast``): алерт попал в последние
      ``nowcast_lead`` дней до начала - триггер «отменить тяжёлую сессию,
      измерить температуру», а не предсказание. Свой знаменатель
      (``episodes_nowcastable``): узкие окна есть даже у эпизодов, чьё
      ``lead >= min_lead``-окно целиком в болезни соседа.
    """
    _require_ml()
    data = _dataset(conn, features)
    at_risk = data.at_risk
    dates = data.dates
    n = len(dates)
    probs = _walk_probs_data(data, model)

    scored = [t for t in range(n) if probs[t] is not None]
    scores = [probs[t] for t in scored]
    labels = [int(bool(data.y_onset[t])) for t in scored]
    months = max(1.0, n / 30.44)

    starts = {e.id: e.start for e in health_illness.episodes(conn)
              if e.level != "gap_only"}
    position_of = {day: i for i, day in enumerate(dates)}
    windows: dict[int, tuple[list[int], int]] = {}
    for episode in sorted({ep for ep in data.episode if ep is not None}):
        test_pos = [i for i, ep in enumerate(data.episode)
                    if ep == episode and data.y_onset[i] == 1 and at_risk[i]]
        onset = position_of.get(starts.get(episode, ""))
        if test_pos and onset is not None:
            windows[episode] = (test_pos, onset)

    # Уловимость эпизода - иначе recall делится на 33 и провоцирует
    # самообман «ловим только половину». Две причины, по которым эпизод не
    # поймают вовсе, - геометрия кластера (всё lead>=min_lead-окно лежит
    # внутри болезни соседнего эпизода: там нет at_risk-дней с таким lead,
    # даже «алертить всегда» не поможет) и отсутствие истории (первые дни
    # ряда: ни 30 дней прошлого, ни одного положительного дня - модель не
    # оценивает их, значит не может и алертнуть).
    catchable: dict[int, list[int]] = {}
    nowcastable: dict[int, list[int]] = {}
    unreachable = {"geometry": 0, "no_history": 0}
    for episode, (test_pos, onset) in windows.items():
        far = [i for i in test_pos if onset - i >= min_lead]
        near = [i for i in test_pos if onset - i <= nowcast_lead]
        if not far:
            unreachable["geometry"] += 1
        elif not any(probs[i] is not None for i in far):
            unreachable["no_history"] += 1
        else:
            catchable[episode] = far
        if near and any(probs[i] is not None for i in near):
            nowcastable[episode] = near

    # зона дня до ближайшего будущего onset'а: если ложные алерты вне
    # 7-дневного окна распределены как календарь, у них нет структуры -
    # алерт «за 15-28 дней» ничего не знает про будущий эпизод
    upcoming = sorted(_date.fromisoformat(day) for day in starts.values())
    zone_of: list[str] = []
    for day in dates:
        moment = _date.fromisoformat(day)
        lead = next(((d - moment).days for d in upcoming if d >= moment), None)
        zone_of.append("none" if lead is None
                       else "1-7" if lead <= 7
                       else "8-14" if lead <= 14
                       else "15-28" if lead <= 28 else "29+")
    zone_base: dict[str, int] = {}
    for t in scored:
        if not data.y_onset[t]:
            zone_base[zone_of[t]] = zone_base.get(zone_of[t], 0) + 1

    def fp_zones(flags: list[bool]) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in range(n):
            if flags[t] and data.y_onset[t] == 0:
                out[zone_of[t]] = out.get(zone_of[t], 0) + 1
        return out

    episode_order = list(windows)

    def nowcast_hits(flags: list[bool]) -> list[float]:
        """Поимка nowcast по каждому эпизоду в порядке ``windows``.

        1.0 - алерт попал в последние ``nowcast_lead`` дней до начала.
        Свой знаменатель (``nowcastable``): узкие окна есть даже у эпизодов,
        у которых ``lead >= min_lead``-окна нет вовсе - та самая геометрия
        кластеров.
        """
        hits = []
        for episode in episode_order:
            test_pos, onset = windows[episode]
            alarms = [i for i in test_pos if flags[i]]
            hits.append(1.0 if (episode in nowcastable and any(
                onset - i <= nowcast_lead for i in alarms)) else 0.0)
        return hits

    def episode_stats(flags: list[bool]) -> dict:
        caught = late = missed = caught_catchable = 0
        leads: list[int] = []
        for episode, (test_pos, onset) in windows.items():
            alarms = [i for i in test_pos if flags[i]]
            if not alarms:
                missed += 1
            else:
                lead = onset - min(alarms)
                if lead >= min_lead:
                    caught += 1
                    leads.append(lead)
                    if episode in catchable:
                        caught_catchable += 1
                else:
                    late += 1
        caught_nowcast = int(sum(nowcast_hits(flags)))
        return {
            "episodes_caught": caught,
            "episodes_late": late,
            "episodes_missed": missed,
            "episodes_caught_catchable": caught_catchable,
            "episodes_caught_nowcast": caught_nowcast,
            "episode_recall": caught / len(windows) if windows else None,
            "episode_recall_catchable": (
                caught_catchable / len(catchable)) if catchable else None,
            "episode_recall_nowcast": (
                caught_nowcast / len(nowcastable)) if nowcastable else None,
            "median_lead": int(statistics.median(leads)) if leads else None,
        }

    def streak_stats(flags: list[bool]) -> tuple[int, int]:
        count = longest = current = 0
        last = None
        for t in range(n):
            if flags[t]:
                if last is not None and _day_before(dates[last], dates[t]):
                    current += 1
                else:
                    current = 1
                    count += 1
                last = t
                longest = max(longest, current)
            else:
                last = None
                current = 0
        return count, longest

    def describe(flags: list[bool]) -> dict:
        alerts = sum(1 for value in flags if value)
        false_days = sum(1 for t in range(n)
                         if flags[t] and data.y_onset[t] == 0)
        count, longest = streak_stats(flags)
        out = {
            "alert_days": alerts,
            "alerts_per_month": alerts / months,
            "false_alarm_days": false_days,
            "false_alarms_per_month": false_days / months,
            "streaks": count,
            "longest_streak": longest,
            "fp_zones": fp_zones(flags),
        }
        out.update(episode_stats(flags))
        return out

    def budget_flags(budget: float) -> list[bool]:
        fraction = min(0.5, budget / 30.44)
        flags = [False] * n
        history: list[float] = []
        for t in range(n):
            if probs[t] is None:
                continue
            if len(history) >= 30:
                flags[t] = probs[t] >= _quantile(history, 1.0 - fraction)
            history.append(probs[t])
        return flags

    def average_metrics(rows: list[dict]) -> dict:
        keys = ("alert_days", "alerts_per_month", "false_alarm_days",
                "false_alarms_per_month", "streaks", "longest_streak",
                "episodes_caught", "episodes_late", "episodes_missed",
                "episodes_caught_nowcast", "episodes_caught_catchable",
                "episode_recall", "episode_recall_catchable",
                "episode_recall_nowcast")
        out = {}
        for key in keys:
            values = [row[key] for row in rows if row[key] is not None]
            out[key] = sum(values) / len(values) if values else None
        leads = [row["median_lead"] for row in rows
                 if row["median_lead"] is not None]
        out["median_lead"] = int(statistics.median(leads)) if leads else None
        return out

    generator = random.Random(seed)
    by_budget = []
    for budget in budgets:
        model_flags = budget_flags(budget)
        model_row = describe(model_flags)
        fraction = min(0.5, model_row["alert_days"] / len(scored)) if scored else 0.0
        random_rows = []
        random_hits: list[list[float]] = []
        for _ in range(max(1, random_repeats)):
            flags = [probs[t] is not None and generator.random() < fraction
                     for t in range(n)]
            random_rows.append(describe(flags))
            random_hits.append(nowcast_hits(flags))
        # ноль усредняется на уровне эпизода (доля повторов, поймавших
        # эпизод), чтобы интервал разницы был парным и честным
        baseline_hits = [
            sum(hits[index] for hits in random_hits) / len(random_hits)
            for index in range(len(episode_order))
        ]
        by_budget.append({
            "budget": budget,
            "model": model_row,
            "random": average_metrics(random_rows),
            "nowcast_delta": _episode_bootstrap_delta(
                nowcast_hits(model_flags), baseline_hits,
                auc_bootstrap, seed + budget),
        })

    def collect(predicate) -> list[list[float]]:
        groups = []
        for test_pos, onset in windows.values():
            part = [probs[i] for i in test_pos
                    if probs[i] is not None and predicate(onset - i)]
            if part:
                groups.append(part)
        return groups

    negatives = [probs[t] for t in scored if not data.y_onset[t]]
    auc_ci = _episode_bootstrap_auc(
        collect(lambda lead: True), negatives, auc_bootstrap, seed)
    # расклад по lead: физиология и продром живут ближе к onset'у, чем
    # горизонт алерта (гипотеза §9.1 - «продром срывает тренировки»)
    auc_by_lead = {
        "near": _episode_bootstrap_auc(
            collect(lambda lead: lead <= 2), negatives, auc_bootstrap, seed),
        "far": _episode_bootstrap_auc(
            collect(lambda lead: lead >= min_lead),
            negatives, auc_bootstrap, seed),
    }

    seasonal = [bool(at_risk[t]) and int(dates[t][5:7]) in season_months
                for t in range(n)]
    return {
        "days": n,
        "evaluated": len(scored),
        "positive_days": int(sum(labels)),
        "positive_rate": (sum(labels) / len(labels)) if labels else None,
        "auc": auc_ci["auc"],
        "auc_ci": auc_ci,
        "auc_by_lead": auc_by_lead,
        "pr_auc": _average_precision(scores, labels),
        "episodes": len(windows),
        "episodes_catchable": len(catchable),
        "episodes_nowcastable": len(nowcastable),
        "episodes_unreachable": unreachable,
        "budgets": by_budget,
        "fp_zone_base": zone_base,
        "season_months": list(season_months),
        "seasonal": describe(seasonal),
        "always": describe([bool(value) for value in at_risk]),
        "horizon": horizon,
        "min_lead": min_lead,
        "nowcast_lead": nowcast_lead,
        "model": model,
    }


def matched_auc(
    conn: sqlite3.Connection,
    features: tuple[str, ...] = V2_FEATURES,
    model: str = "logreg",
    control: tuple[int, int] = (15, 28),
    bootstrap: int = 2000,
    seed: int = 17,
) -> dict:
    """AUC «окно эпизода vs дальний контроль» + CI по эпизодам.

    Различение окна против **далёких** дней (``control`` дней до onset) -
    то, что отличает модель от «алертить каждый день»: соседние дни слишком
    похожи на предвестники (продром сам срывает тренировки), и AUC против
    ближайшего контроля ничего не доказывает.

    Интервал - не по дням, а **по эпизодам**: дни одного эпизода почти не
    независимы, наивный по дням был бы ложно узким. Эпизоды ресемплируются
    с возвращением, внутри фолда сохраняется LOO-схема (эпизод целиком вне
    обучения). Это и есть поправка на то, что конфигурацию контроля выбрали,
    посмотрев на данные.
    """
    _require_ml()
    data = _dataset(conn, features)
    at_risk = data.at_risk
    dates = data.dates
    y = data.y_onset
    starts = {e.id: e.start for e in health_illness.episodes(conn)
              if e.level != "gap_only"}
    position_of = {day: i for i, day in enumerate(dates)}
    folds: list[tuple[list[float], list[int]]] = []
    for episode in sorted({ep for ep in data.episode if ep is not None}):
        test_pos = [i for i, ep in enumerate(data.episode)
                    if ep == episode and data.y_onset[i] == 1 and at_risk[i]]
        start = starts.get(episode)
        onset = position_of.get(start) if start else None
        if not test_pos or onset is None:
            continue
        test_neg = []
        for shift in range(control[0], control[1] + 1):
            day = (_date.fromisoformat(start) - _timedelta(days=shift)).isoformat()
            index = position_of.get(day)
            if index is not None and at_risk[index]:
                test_neg.append(index)
        if not test_neg:
            continue
        train = [i for i in range(len(dates))
                 if at_risk[i] and data.episode[i] != episode]
        if len(set(data.y_onset[train].tolist())) < 2:
            continue
        test = test_pos + test_neg
        fold_labels = [1] * len(test_pos) + [0] * len(test_neg)
        x_train, x_test = _impute(data.X[train], data.X[test])
        scaler = _Scaler()
        x_train = scaler.fit_transform(x_train)
        x_test = scaler.transform(x_test)
        estimator = _estimator(model)
        estimator.fit(x_train, data.y_onset[train])
        predicted = estimator.predict_proba(x_test)[:, 1]
        folds.append(([float(value) for value in predicted], fold_labels))

    if not folds:
        return {"auc": None, "low": None, "high": None, "episodes": 0}

    point = _roc_auc(
        [score for part, _ in folds for score in part],
        [label for _, part in folds for label in part],
    )
    generator = random.Random(seed)
    sample: list[float] = []
    for _ in range(max(1, bootstrap)):
        chosen = [generator.choice(folds) for _ in folds]
        value = _roc_auc(
            [score for part, _ in chosen for score in part],
            [label for _, part in chosen for label in part],
        )
        if value is not None:
            sample.append(value)
    return {
        "auc": point,
        "low": _quantile(sample, 0.025) if sample else None,
        "high": _quantile(sample, 0.975) if sample else None,
        "episodes": len(folds),
        "bootstrap": len(sample),
        "control": list(control),
    }


def _estimator(model: str):
    if model == "logreg":
        return _LogReg(max_iter=1000, class_weight="balanced", C=1.0)
    if model == "gbm":
        return _GBM(max_depth=3, min_samples_leaf=20, n_estimators=100,
                    learning_rate=0.05)
    raise ValueError(f"неизвестная модель: {model}")


# --------------------------------------------------------------------------
# Лаг-анализ и красный флаг
# --------------------------------------------------------------------------


def lag_analysis(
    conn: sqlite3.Connection,
    feature: str = "weekly_tonnage",
    max_lag: int = 21,
) -> list[dict]:
    """Насколько признак отклоняется за ``lag`` дней до эпизода.

    Для каждого лага сравниваются значения признака в дни «за 0…lag» до
    начала эпизодов против всех остальных дней. Возвращается стандартизованная
    разница средних (эффект) - описательная величина, без p-value: при
    единицах эпизодов p-value был бы самообманом.
    """
    rows = conn.execute(
        f"SELECT date, {feature} FROM ml_features ORDER BY date"
    ).fetchall()
    values = {row["date"]: row[feature] for row in rows}
    dates = [row["date"] for row in rows]
    index_of = {day: i for i, day in enumerate(dates)}
    onsets = [e.start for e in health_illness.episodes(conn)
              if e.level != "gap_only" and e.start in index_of]
    if not onsets or not dates:
        return []

    all_values = [values[d] for d in dates if values[d] is not None]
    if len(all_values) < 2:
        return []
    baseline_mean = statistics.fmean(all_values)
    baseline_sd = statistics.pstdev(all_values) or 1.0

    out = []
    for lag in range(max_lag + 1):
        picked = []
        for onset in onsets:
            position = index_of[onset] - lag
            if 0 <= position < len(dates):
                value = values[dates[position]]
                if value is not None:
                    picked.append(value)
        if picked:
            effect = (statistics.fmean(picked) - baseline_mean) / baseline_sd
        else:
            effect = None
        out.append({"lag": lag, "n": len(picked), "effect": effect})
    return out


def red_flag_report(conn: sqlite3.Connection) -> dict:
    """Красный флаг Meeusen et al.: сколько признаков сошлось за 14 дней до эпизода.

    Флаг дня складывается из четырёх пунктов: просадка e1RM, ВСР ниже своего
    обычного (z < -1), долг сна (z-скор > 1 по своему ряду), монотонность
    выше 2. Считается, сколько эпизодов имели хотя бы один день с флагом ≥ 2.
    """
    rows = conn.execute(
        "SELECT date, y, episode_id, z_hrv, sleep_debt7, monotony, rm_regress "
        "FROM ml_features ORDER BY date"
    ).fetchall()
    debt = [row["sleep_debt7"] for row in rows]
    debt_z = health_illness.zscore(debt)
    flags: dict[int, int] = {}
    for index, row in enumerate(rows):
        points = 0
        if row["rm_regress"]:
            points += 1
        if row["z_hrv"] is not None and row["z_hrv"] < -1:
            points += 1
        if debt_z[index] is not None and debt_z[index] > 1:
            points += 1
        if row["monotony"] is not None and row["monotony"] > health_load.MONOTONY_ALERT:
            points += 1
        if points >= 2 and row["episode_id"] is not None:
            flags[row["episode_id"]] = max(flags.get(row["episode_id"], 0), points)
    episodes = [e.id for e in health_illness.episodes(conn) if e.level != "gap_only"]
    return {
        "episodes": len(episodes),
        "episodes_with_flag": sum(1 for e in episodes if e in flags),
        "max_points": max(flags.values()) if flags else 0,
    }


# --------------------------------------------------------------------------
# Отчёт
# --------------------------------------------------------------------------


def report(
    conn: sqlite3.Connection,
    window: str | None = health_load.DEFAULT_WINDOW,
    as_json: bool = False,
) -> dict:
    """Полный отчёт: признаки, лаги, CV-протоколы, красный флаг, бюджеты.

    Помимо v1/v2-CV здесь считаются блоки честной оценки: ``walk_v2`` и
    ``walk_baseline`` (recall@бюджет против случайного и сезонного нуля,
    AUC с bootstrap-CI по эпизодам) и ``matched_v2``/``matched_baseline``
    (AUC против далёкого контроля с тем же интервалом).
    """
    build_features(conn, window)
    positives = build_labels(conn, window)

    data = {
        "window": window,
        "has_ml": HAS_ML,
        "positive_days": positives,
        "red_flag": red_flag_report(conn),
        "lag_tonnage": lag_analysis(conn, "weekly_tonnage"),
        "lag_hrv": lag_analysis(conn, "z_hrv"),
    }
    if HAS_ML:
        with warnings.catch_warnings():
            # при n=1 и куче пропусков sklearn/numpy сыплют ConvergenceWarning
            # и «All-NaN slice»; это ожидаемо и в отчёте не помогает
            warnings.simplefilter("ignore")
            data["cv_logreg"] = leave_one_episode_out(conn, MODEL_FEATURES, model="logreg")
            data["cv_baseline"] = leave_one_episode_out(conn, BASELINE_FEATURES, model="logreg")
            data["cv_gbm"] = leave_one_episode_out(conn, MODEL_FEATURES, model="gbm")
            # v2 - риск на горизонте H: основная строка отчёта
            data["onset_v2"] = onset_cv(conn, V2_FEATURES, model="logreg")
            data["onset_baseline"] = onset_cv(
                conn, V2_BASELINE_FEATURES, model="logreg")
            # честная оценка: порог под бюджет и AUC с CI по эпизодам
            data["walk_v2"] = walk_forward(conn, V2_FEATURES, model="logreg")
            data["walk_baseline"] = walk_forward(
                conn, V2_BASELINE_FEATURES, model="logreg")
            data["matched_v2"] = matched_auc(conn, V2_FEATURES, model="logreg")
            data["matched_baseline"] = matched_auc(
                conn, V2_BASELINE_FEATURES, model="logreg")
    if as_json:
        return data
    return data


def dumps(data: dict) -> str:
    """JSON-представление отчёта для ``--json``."""
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def format_lag(rows: list[dict], feature: str) -> str:
    """Таблица лаг-анализа: на сколько SD признак отклонён за ``lag`` дней."""
    body = table(
        (f"лаг, дн", f"{feature}, эффект (SD)", "n"),
        [(row["lag"], "—" if row["effect"] is None else f"{row['effect']:+.2f}",
          row["n"]) for row in rows],
    )
    return f"лаг-анализ: {feature}\n\n{body}" if body else f"лаг-анализ: {feature} — нет данных"


def _rate(value) -> str:
    """Доля для отчёта - «—», когда считать не на чем (нет окон)."""
    return "—" if value is None else f"{value:.2f}"


def format_report(data: dict) -> str:
    """Готовый текстовый отчёт для CLI и ноутбука."""
    lines = [f"окно с {data['window']}, положительных дней: "
             f"{data['positive_days']}"]
    if not data.get("has_ml"):
        lines.append("numpy/scikit-learn не установлены - CV-протокол пропущен")
        lines.append("поставь:  pip install koilog[ml]")
        return "\n".join(lines)

    flag = data["red_flag"]
    lines.append(f"красный флаг (>=2 признака): эпизодов с флагом "
                 f"{flag['episodes_with_flag']} из {flag['episodes']}")

    # v2 - риск на горизонте H (7 дней), event-метрики
    onset_rows = []
    for key, title in (("onset_v2", "v2 все признаки"),
                       ("onset_baseline", "v2 только физиология")):
        block = data.get(key)
        if not block or not block.get("episodes"):
            continue
        onset_rows.append((
            title, block["episodes"], block["episodes_caught"],
            block.get("episodes_late", 0),
            "—" if block.get("median_lead") is None else block["median_lead"],
        ))
    if onset_rows:
        lines.append("")
        lines.append(f"v2: риск эпизода на горизонте "
                     f"{data.get('onset_v2', {}).get('horizon', H_ONSET)} дн "
                     f"(поимка при lead >= "
                     f"{data.get('onset_v2', {}).get('min_lead', H_MIN)})")
        lines.append(table(
            ("модель", "эпизодов", "поймано", "опоздало",
             "медианный lead"),
            onset_rows,
        ))
        lines.append("ложные срабатывания считаются только бюджетно "
                     "(см. recall@бюджет): порог 0.5 здесь не operating "
                     "point и цифру сравнивать не с чем")

    # честная оценка: recall@бюджет против нулей + AUC с CI по эпизодам
    for walk_key, walk_title in (("walk_v2", "все признаки"),
                                 ("walk_baseline", "только физиология")):
        walk = data.get(walk_key)
        if not walk or not walk.get("budgets"):
            continue
        lines.append("")
        lines.append(
            f"recall@бюджет ({walk_title}, день за днём, порог по прошлому, "
            f"поимка при lead >= {walk.get('min_lead', H_MIN)})")
        unreachable = walk.get("episodes_unreachable") or {}
        geometry = unreachable.get("geometry", 0)
        history = unreachable.get("no_history", 0)
        lines.append(
            f"эпизодов {walk['episodes']}, уловимых "
            f"{walk.get('episodes_catchable', walk['episodes'])}"
            + (f"; {geometry} неуловимы вовсе (lead>="
               f"{walk.get('min_lead', H_MIN)}-окно целиком в болезни "
               f"соседнего эпизода)" if geometry else "")
            + (f"; {history} без истории (первые дни ряда)"
               if history else ""))
        budget_rows = []
        for budget in walk["budgets"]:
            model_row = budget["model"]
            random_row = budget["random"]
            budget_rows.append((
                f"{budget['budget']:.0f}",
                f"{model_row['alerts_per_month']:.1f}",
                _rate(model_row["episode_recall_catchable"]),
                f"{model_row['episodes_caught_catchable']}",
                _rate(random_row["episode_recall_catchable"]),
            ))
        lines.append(table(
            ("цель/мес", "факт/мес", "recall", "поймано", "случайный"),
            budget_rows,
        ))
        # nowcast: алерт в последние nowcast_lead дней до начала - триггер
        # «отменить тяжёлую сессию, измерить температуру», а не предсказание.
        # Узкие окна есть даже у эпизодов, чьё lead >= min_lead-окно целиком
        # лежит в болезни соседа, поэтому знаменатель тут свой.
        near_lead = walk.get("nowcast_lead", 2)
        lines.append(
            f"nowcast (алерт за {near_lead} дн до начала, уловимых "
            f"{walk.get('episodes_nowcastable', walk['episodes'])}): "
            + " / ".join(
                _rate(item["model"].get("episode_recall_nowcast"))
                for item in walk["budgets"])
            + "; случайный "
            + " / ".join(
                _rate(item["random"].get("episode_recall_nowcast"))
                for item in walk["budgets"]))
        deltas = [item.get("nowcast_delta") or {} for item in walk["budgets"]]
        if any(item.get("delta") is not None for item in deltas):
            parts = []
            for item in deltas:
                if item.get("delta") is None:
                    parts.append("—")
                    continue
                parts.append(f"{item['delta']:+.2f}" + (
                    f" [{item['low']:+.2f}, {item['high']:+.2f}]"
                    if item.get("low") is not None else ""))
            lines.append("  nowcast-разница против случайного "
                         "(эпизодный bootstrap): " + " / ".join(parts))
        # есть ли у ложных алертов структура вне окна 7 дней?
        far = max(walk["budgets"], key=lambda item: item["budget"])
        zones = far["model"].get("fp_zones") or {}
        base = walk.get("fp_zone_base") or {}
        total = sum(zones.values())
        if total:
            parts = []
            for key in ("8-14", "15-28", "29+"):
                if key not in zones and key not in base:
                    continue
                parts.append(
                    f"{key} дн {100 * zones.get(key, 0) / total:.0f}% "
                    f"(календарь {100 * base.get(key, 0) / max(1, sum(base.values())):.0f}%)")
            if parts:
                lines.append(
                    "ложные алерты (макс. бюджет), зона до следующего "
                    "onset'а: " + "; ".join(parts))
        seasonal = walk.get("seasonal") or {}
        if seasonal.get("episode_recall_catchable") is not None:
            lines.append(
                f"сезонный календарь ({'-'.join(str(m) for m in walk['season_months'])}): "
                f"recall {_rate(seasonal['episode_recall_catchable'])} по уловимым "
                f"при {seasonal['alerts_per_month']:.1f} алертов/мес")
        always = walk.get("always") or {}
        if always.get("episode_recall") is not None:
            lines.append(
                f"всегда алерт (потолок): "
                f"{_rate(always['episode_recall'])} по всем "
                f"{walk['episodes']} эпизодам, "
                f"{_rate(always.get('episode_recall_catchable'))} по уловимым, "
                f"{always['alerts_per_month']:.1f} алертов/мес")
        if walk.get("auc") is not None:
            interval = walk.get("auc_ci") or {}
            bounds = ""
            if interval.get("low") is not None:
                bounds = f" [{interval['low']:.3f}, {interval['high']:.3f}]"
            lines.append(
                f"различение всех дней: AUC {walk['auc']:.3f}{bounds}, "
                f"PR-AUC {walk['pr_auc']:.3f}, доля положительных "
                f"{walk['positive_rate']:.3f}")
        for key, lead_title in (("near", "1-2 дня до начала"),
                                ("far", ">= 3 дней до начала")):
            lead_block = (walk.get("auc_by_lead") or {}).get(key) or {}
            if lead_block.get("auc") is None or lead_block.get("low") is None:
                continue
            lines.append(
                f"  AUC по lead ({lead_title}): {lead_block['auc']:.3f} "
                f"[{lead_block['low']:.3f}, {lead_block['high']:.3f}]")

    for key, title in (("matched_v2", "все признаки"),
                       ("matched_baseline", "только физиология")):
        block = data.get(key)
        if not block or block.get("auc") is None:
            continue
        lines.append(
            f"AUC окно vs {block['control'][0]}-{block['control'][1]} дн "
            f"({title}): {block['auc']:.3f} "
            f"[{block['low']:.3f}, {block['high']:.3f}] "
            f"по {block['episodes']} эпизодам")

    # старая day-классификация - бейзлайн
    lines.append("")
    rows = []
    for key, title in (("cv_logreg", "L2-логистика"),
                       ("cv_baseline", "бейзлайн"),
                       ("cv_gbm", "GBM (depth 3)")):
        block = data.get(key)
        if not block or not block.get("episodes"):
            continue
        rows.append((
            title, block["episodes"], block["episodes_caught"],
            f"{block['episode_recall']:.2f}", f"{block['day_precision']:.2f}",
            f"{block['day_recall']:.2f}",
        ))
    body = table(
        ("модель", "эпизодов", "поймано", "recall эп.", "точность",
         "полнота"),
        rows,
    )
    if body:
        lines.append("day-классификация (бейзлайн v1, окно 14 дн):")
        lines.append(body)
    return "\n".join(lines)
