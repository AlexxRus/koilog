"""Возврат к рабочим весам после болезни (раунд 5).

Идея простая: болезнь - это вынужденный простой, а простой по-разному бьёт по
качествам. Из литературы (Mujika & Padilla 2000; Nishiumi 2025; Lepers 2024)
следует, что **максимальная сила - самое живучее качество**, а взрывная
мощность - самое хрупкое: поэтому после болезни сначала возвращают рабочие
веса и лишь в конце - скоростные элементы. Но начинать «с того же веса»
нельзя: первые недели после вирусной инфекции - это риск миокардита и
обострения, поэтому возврат идёт ступенями и **только после гейта** по пульсу
покой и ВСР.

Что считает модуль:

- :func:`detraining_curve` - личная кривая «дней простоя → просадка e1RM» по
  истории тренировок, с биннингом и сверкой с литературными ориентирами.
- :func:`baseline_gate` - можно ли вообще начинать: RHR выше фона и ВСР ниже
  фона две ночи подряд держат возврат (проверка односторонняя - Kiviniemi 2007;
  Vesterinen 2016; Buchheit/Plews), а сразу после эпизода действует окно тишины
  (возврат по рампе, потому что высокая ВСР после болезни - отскок, а не допуск).
- :func:`return_plan` - рампа возврата от **предболезненного** рабочего веса:
  верх 1-й недели и объём по длине простоя (таблица откалибрована по
  личному журналу: возврат сразу на >=100 % ассоциирован с рецидивом
  болезни), шаги через :func:`ramp_program` - «лесенка с перекрытием»
  недель, полный вес возвращается внутри полосы).
- :func:`returns` - разметка прошлых возвратов «жёсткий/мягкий» и связь
  скорости реампера с рецидивами (Bruusgaard 2010; Nuzzo 2024; Raysmith &
  Drew 2016).

Модуль опирается только на стандартную библиотеку и ядро ``koilog`` (база
``sets`` + инвентарь), поэтому работает и без extra ``ml``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import timedelta

from .. import core, equipment
from . import illness as health_illness
from .text import table

__all__ = [
    "DETRAIN_GAP_MIN",
    "GATE_NIGHTS",
    "GATE_SILENCE_DAYS",
    "GATE_Z",
    "RAMP_TABLE",
    "RETURN_REBOUND_DAYS",
    "BaselineGate",
    "DetrainPoint",
    "Ramp",
    "RampPlan",
    "ReturnRow",
    "baseline_gate",
    "default_exercise",
    "detraining_bins",
    "detraining_curve",
    "format_curve",
    "format_gate",
    "format_return",
    "format_returns",
    "ramp_for",
    "ramp_program",
    "return_plan",
    "returns",
]

#: Минимальный простой по упражнению, который считается «детрейном».
DETRAIN_GAP_MIN = 7

#: Порог z-скора для гейта. Проверка односторонняя: блокируют только
#: отклонения в «плохую» сторону - RHR выше фона и ВСР ниже фона (гриппоподобная
#: реакция). Низкий RHR и высокая ВСР - не повод держать: симметричный ``abs``
#: путал «отдохнул» с «заболевает» и держал ~треть дней без причины.
GATE_Z = 1.0

#: Сколько ночей подряд должны быть в фоне, чтобы разрешить возврат.
GATE_NIGHTS = 2

#: Окно тишины после эпизода: столько дней возврат идёт только по рампе. Нужно,
#: потому что после болезни высокая ВСР - это отскок (суперкомпенсация), а не
#: «готов к нагрузке»; на живом кейсе 18.10.2026 каналы были в фоне, а через
#: день начался рецидив внутри кластера. 14 дн выбрано ретро-тестом (D10) по
#: заранее записанному правилу: A освобождает 292 дня, из них 28 pre-relapse
#: (вред), поэтому A отклонён; окно 7 и 10 дн оставляют 2 pre-relapse дня
#: (рецидивы с разрывом 17 дн), окно 14 дн - ноль при цене 47 дней (16% от 292).
GATE_SILENCE_DAYS = 14

#: Окно личного базлайна для гейта (как в :mod:`koilog.health.illness`).
GATE_BASELINE_DAYS = health_illness.BASELINE_DAYS

#: Порог температуры запястья (z), выше которого возврат запрещён.
GATE_Z_TEMP = health_illness.Z_TEMP

#: Горизонт, за который смотрим рецидив после возврата.
RETURN_REBOUND_DAYS = 21

#: Окно, за которое измеряется скорость реампера.
RETURN_HORIZON_DAYS = 28

#: Доля предболезненного веса, с которой первый заход считается «жёстким»
#: (выше) или «мягким» (ниже :data:`SOFT_RATIO`).
HARD_RATIO = 0.75
SOFT_RATIO = 0.60


@dataclass(frozen=True, slots=True)
class Ramp:
    """Параметры рампы для одной полосы простоя."""

    label: str
    start_pct: float      # верх 1-й недели, доля от предболезненного веса
    volume_cut: float     # на сколько срезан объём (для справки)
    sets: int             # число рабочих подходов на старте
    weekly_gain: float    # целевой шаг роста в неделю (для справки)
    weeks: int            # ориентир полного возврата, недель


#: Полосы калиброваны **по личным данным** (152 паузы >= 3 недель, 2016-2026),
#: а не только по GRTP-литературе: медианный верх первой сессии после пауз
#: 14-20 дн ~100 %, 21-30 дн ~93 %, 31-45 дн ~88 %, 46+ дн ~83 % базы.
#: Возврат «сразу на >=100 %» ассоциирован с рецидивом (45 % против 10 %
#: при старте 85-99 %), поэтому верх первой недели всегда ниже базы.
#: Объём при возврате в данных почти не резался (4 подхода -> 3-4),
#: поэтому срез объёма - всего один подход (~25 %), а не половина.
RAMP_TABLE: tuple[Ramp, ...] = (
    Ramp("до недели", 0.95, 0.25, 3, 0.050, 1),
    Ramp("1–2 недели", 0.90, 0.25, 3, 0.050, 2),
    Ramp("2–4 недели", 0.85, 0.25, 3, 0.050, 3),
    Ramp("больше месяца", 0.70, 0.25, 3, 0.075, 4),
)

#: Литературные ориентиры детрейна для сверки с личной кривой.
DETRAIN_ANCHORS: tuple[tuple[str, str, str], ...] = (
    ("до ~1 нед", "1ПМ практически без изменений", "Mujika & Padilla 2000"),
    ("~2 нед", "эксцентрика −5,5 %, RFD −13 %", "Nishiumi 2025; Yamashita 2023"),
    ("~3 нед", "сила держится, объём падает", "Mujika 2000; Campbell 2013*"),
    ("~6–7 нед", "мощность −9…10 %, сила ~−14 %", "Kraemer 2002"),
    ("~12 нед", "сила −8 %, мощность −13 %", "Lepers 2024"),
)


# --------------------------------------------------------------------------
# Чистые функции
# --------------------------------------------------------------------------


def ramp_for(days_off: int) -> Ramp:
    """Полоса рампы по длине простоя."""
    if days_off < 7:
        return RAMP_TABLE[0]
    if days_off < 14:
        return RAMP_TABLE[1]
    if days_off < 28:
        return RAMP_TABLE[2]
    return RAMP_TABLE[3]


def _step_rungs(
    ladder: Sequence[float], index: int, sets: int,
) -> tuple[float, ...]:
    """Веса подходов одного шага: лесенка, которой дорастают до цели недели.

    Верхний подход шага - сам целевой вес, предыдущие идут по ступени ниже
    по инвентарю (у нижнего края лестницы - дублируются). Шаг с целью 50 кг
    при трёх подходах даёт 40/45/50: лёгкая разминка внутри сессии и один
    рабочий подход на верху. Перекрытие между неделями получается само:
    следующая неделя стартует со ступени, на которой заканчивалась
    предыдущая (50 и в 36/44/50, и в 50/54/58 - ровно личный стиль
    «разминка + возрастающая лесенка» из журнала).
    """
    span = len(ladder) - 1
    top_offset = sets - 1
    return tuple(
        ladder[min(max(index - top_offset + j, 0), span)] for j in range(sets)
    )


def _session_reps(
    rungs: Sequence[float],
    reference_rm: float | None,
    max_reps: int,
) -> int:
    """Повторы тренировки: максимум, пока суммарный КОИ остаётся в Optimal.

    На возврате нагрузкой управляет повторы, а не вес: вес растёт по
    лестнице инвентаря сам, а число повторов подбирается так, чтобы КОИ
    тренировки не вылез за верх «Optimal» (1.0). Верхняя граница
    перебора - предболезненное число повторов (``max_reps``): на возврате
    мы не растим повторы выше того, что уже было.

    Если и минимальные повторы (``core.WAVE_MIN_REPS``) не влезают
    в полосу, берём минимальные - и режим «Load» в таблице показывает это
    честно, а не молча занижает вес.
    """
    cap = max(min(max_reps, core.RM_MAX_REPS), 1)
    floor = min(core.WAVE_MIN_REPS, cap)
    best = floor
    for reps in range(floor, cap + 1):
        specs = tuple(core.SetSpec(w, reps) for w in rungs)
        if core.koi_sum(specs, reference_rm).total < core.KOI_BANDS[1][0]:
            best = reps
        else:
            break
    return best


def ramp_program(
    weights: Sequence[float],
    start_weight: float,
    base_weight: float | None,
    start_reps: int,
    *,
    sets: int = 3,
    weeks: int = 2,
    steps: int | None = None,
    reference_rm: float | None = None,
) -> core.Progression:
    """Рампа возврата, которая **доходит** до предболезненного веса.

    Обычная волна :func:`koilog.core.progression` растёт «сначала повторы,
    потом вес» и от лёгкого старта идёт десятками шагов. На полосе
    «1–2 недели» это значило, что за обещанные две недели полный вес не
    возвращался вообще: шапка обещала одно, а таблица обрывалась на той же
    лёгкой нагрузке. Поэтому рампа собирается отдельно, в стиле личного
    журнала - «лесенка с перекрытием»:

    * ``start_weight`` - это **верх** первой недели (доля базы из
      :data:`RAMP_TABLE`), а лесенка сессии уходит вниз от него на
      ``sets - 1`` ступени: разминка легче рабочего подхода, как всегда;
    * шаг = **неделя восстановления** (``weeks``), минимум два шага и не
      больше ступеней лестницы от ``start_weight`` до ``base_weight``;
    * верх недели растёт по ступеням инвентаря к базе, и нижние ступени
      следующей недели перекрываются с верхом предыдущей - между неделями
      нет прыжка через ступени;
    * повторы подбираются так, чтобы суммарный КОИ тренировки остался в
      полосе «Optimal» и не выше предболезненного числа повторов
      (см. :func:`_session_reps`).

    Если ступеней больше, чем недель, промежуточные веса пропускаются, но
    полный вес всё равно возвращается **внутри** полосы; если ступеней
    меньше недель, вес добирается раньше конца полосы - остаток недель
    идёт на повторы и мощность.

    ``steps`` переопределяет количество шагов, если задан явно - в основном
    для тестов.
    """
    target = core.snap_to_ladder(base_weight, weights) if base_weight else None
    if target is None or target < start_weight:
        # база не на лестнице или легче старта: целиться в саму базу
        target = base_weight if base_weight and base_weight >= start_weight \
            else start_weight

    heavy = [w for w in weights if start_weight - 1e-9 <= w <= target + 1e-9]
    if not heavy:
        heavy = [start_weight]
    # Ступени ниже старта: разминка первой недели берётся из полного
    # списка инвентаря, поэтому лесенка не упирается в верхнюю точку.
    below = [w for w in weights if w < start_weight - 1e-9]
    keep = max(max(sets, 1) - 1, 0)
    ladder = (below[-keep:] if keep else []) + heavy
    start_index = len(ladder) - len(heavy)
    span = len(heavy) - 1

    if steps is not None:
        sessions = max(steps, 1)
    else:
        # Один шаг на неделю восстановления - столько недель, сколько длился
        # простой. Шагов не бывает меньше двух (иначе облегчённый старт
        # некуда поставить) и больше, чем ступеней лестницы: длинная полоса
        # добирает вес быстрее, остаток недель уходит на повторы и мощность.
        sessions = min(max(weeks, 2), span + 1)

    out: list[core.ProgramStep] = []
    for i in range(sessions):
        top = start_index if sessions == 1 else \
            start_index + (i * span) // (sessions - 1)
        rungs = _step_rungs(ladder, top, max(sets, 1))
        reps = _session_reps(rungs, reference_rm, start_reps)
        specs = tuple(core.SetSpec(w, reps) for w in rungs)
        result = core.koi_sum(specs, reference_rm)
        out.append(core.ProgramStep(
            index=i + 1,
            sets=specs,
            warmup=None,
            reference_rm=reference_rm,
            koi=result.total,
            band=core.koi_band(result.total),
            rejected=result.rejected,
        ))
    return core.Progression(tuple(out), tuple(weights), 1, None)


@dataclass(slots=True)
class DetrainPoint:
    """Один прошлый простой: сколько дней и на сколько просел e1RM."""

    exercise: str
    days_off: int
    pre_rm: float
    post_rm: float

    @property
    def drop_kg(self) -> float:
        return self.pre_rm - self.post_rm

    @property
    def drop_pct(self) -> float:
        return (self.pre_rm - self.post_rm) / self.pre_rm if self.pre_rm else 0.0


#: Границы бинов личной детрейн-кривой: ``(низ, верх, подпись)``.
#: Интервалы полуоткрытые (``низ <= days_off < верх``). Длинный хвост разбит
#: на ``28–60`` и ``>60``: у ``>28 дн`` слишком разный состав (28 дн и
#: 106-дневный перерыв тянут среднее в разные стороны), а решений бин не
#: принимает - это чисто описательная витрина (см. ``format_curve``).
_BINS: tuple[tuple[int, int, str], ...] = (
    (0, 7, "<7 дн"),
    (7, 14, "7–14 дн"),
    (14, 28, "14–28 дн"),
    (28, 60, "28–60 дн"),
    (60, 10 ** 6, ">60 дн"),
)


def _quantile(values: list[float], q: float) -> float:
    """Линейный квантиль (метод ``linear``) по отсортированному ряду.

    Для размаха бина (``iqr``) достаточно линейной интерполяции между
    соседними порядковыми значениями - не тянем ``statistics.quantiles``
    (он требует n >= 2 и падает на коротких рядах).
    """
    if not values:
        raise ValueError("пустой ряд")
    pos = q * (len(values) - 1)
    low = int(pos)
    high = min(low + 1, len(values) - 1)
    frac = pos - low
    return values[low] * (1 - frac) + values[high] * frac


def detraining_bins(points: list[DetrainPoint]) -> list[dict]:
    """Свести точки в бины: n, средняя, медианная просадка и размах (в %).

    ``mean``/``median``/``iqr`` берутся по **совокупности точек бина** (не
    скользящим окном). ``iqr`` - интерквартильный размах (``q75 - q25``): он
    честно показывает разброс там, где малые ``n`` и 106-дневные перерывы
    делают среднее обманчивым. При ``n == 0`` все три - ``None``.
    """
    out = []
    for low, high, label in _BINS:
        bucket = [p for p in points if low <= p.days_off < high]
        drops = sorted(p.drop_pct for p in bucket)
        if not bucket:
            out.append({"label": label, "n": 0, "mean": None, "median": None,
                        "iqr": None})
            continue
        middle = drops[len(drops) // 2] if len(drops) % 2 else \
            (drops[len(drops) // 2 - 1] + drops[len(drops) // 2]) / 2
        spread = (_quantile(drops, 0.75) - _quantile(drops, 0.25)
                  if len(drops) >= 2 else 0.0)
        out.append({
            "label": label,
            "n": len(bucket),
            "mean": sum(drops) / len(drops),
            "median": middle,
            "iqr": spread,
        })
    return out


# --------------------------------------------------------------------------
# Чтение из базы
# --------------------------------------------------------------------------


def _today(conn: sqlite3.Connection) -> str | None:
    for sql in ("SELECT MAX(date) FROM aw_daily", "SELECT MAX(date) FROM sets"):
        try:
            row = conn.execute(sql).fetchone()
        except sqlite3.OperationalError:
            continue
        if row and row[0]:
            return row[0]
    return None


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def default_exercise(conn: sqlite3.Connection) -> str | None:
    """Упражнение с наибольшим числом тренировочных дней (для умолчания)."""
    try:
        row = conn.execute(
            """SELECT exercise, COUNT(DISTINCT date) AS n FROM sets
               WHERE rm_session IS NOT NULL AND is_warmup = 0 AND skipped = 0
               GROUP BY exercise ORDER BY n DESC, exercise LIMIT 1"""
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _sessions(conn: sqlite3.Connection, exercise: str) -> list[tuple[str, float]]:
    """Дни с тренировкой упражнения и лучшим e1RM этого дня."""
    rows = conn.execute(
        """SELECT date, MAX(rm_session) AS rm FROM sets
           WHERE exercise = ? AND rm_session IS NOT NULL
                 AND is_warmup = 0 AND skipped = 0
           GROUP BY date ORDER BY date""",
        (exercise,),
    ).fetchall()
    return [(row[0], row[1]) for row in rows if row[1] is not None]


def detraining_curve(
    conn: sqlite3.Connection,
    exercise: str | None = None,
    min_off: int = DETRAIN_GAP_MIN,
) -> list[DetrainPoint]:
    """Личная кривая: просадка e1RM после простоят по каждому упражнению.

    Точка берётся на паре «последняя тренировка до простоя → первая после»:
    ``days_off`` - разрыв в календаре, ``pre_rm``/``post_rm`` - лучший e1RM
    этих двух дней. Простой здесь - это перерыв в **данном упражнении**, а не
    в журнале: пропуск жима может быть сменой программы, а не болезнью, и это
    стоит держать в голове при чтении кривой.
    """
    if exercise is None:
        names = [row[0] for row in conn.execute(
            """SELECT DISTINCT exercise FROM sets
               WHERE rm_session IS NOT NULL AND is_warmup = 0 AND skipped = 0"""
        ).fetchall()]
    else:
        names = [exercise]

    points: list[DetrainPoint] = []
    for name in names:
        sessions = _sessions(conn, name)
        for (day0, rm0), (day1, rm1) in zip(sessions, sessions[1:]):
            gap = (date_cls.fromisoformat(day1) - date_cls.fromisoformat(day0)).days
            if gap >= min_off and rm0 > 0:
                points.append(DetrainPoint(name, gap, rm0, rm1))
    points.sort(key=lambda p: (p.days_off, p.exercise))
    return points


@dataclass(slots=True)
class BaselineGate:
    """Результат гейта по ВСР/RHR/температуре."""

    ready: bool
    today: str | None
    nights: int
    reasons: tuple[str, ...] = ()
    rows: tuple[dict, ...] = ()
    #: Дата, до которой (включительно) действует окно тишины после эпизода.
    #: Заполнено, только если окно реально сработало.
    silence_until: str | None = None

    @property
    def window_only(self) -> bool:
        """Блокирует только окно тишины (каналы в фоне).

        Тогда возврат идёт по рампе, но это не «покой» и не «рано по
        самочувствию»: высокая ВСР после болезни - отскок, а не допуск.
        """
        return self.silence_until is not None and all(
            reason.startswith("окно тишины") for reason in self.reasons)

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "today": self.today,
            "nights": self.nights,
            "reasons": list(self.reasons),
            "rows": list(self.rows),
            "silence_until": self.silence_until,
        }


def baseline_gate(
    conn: sqlite3.Connection,
    today: str | None = None,
    nights: int = GATE_NIGHTS,
    silence_days: int = GATE_SILENCE_DAYS,
) -> BaselineGate:
    """Можно ли возвращаться к нагрузке: ВСР и RHR в фоне ``nights`` ночей.

    Берётся тот же личный 60-дневный базлайн, что и в :mod:`illness`, и
    смотрятся **две ночи подряд**: реагировать на одиночный провал - это
    фоновый шум, а не сигнал (Kiviniemi 2007; Buchheit/Plews). Проверка
    **односторонняя**: держат высокий RHR и низкая ВСР (гриппоподобная
    реакция), а низкий RHR и высокая ВСР - нет. Симметричный ``abs`` держал
    «отдохнул» наравне с «заболевает»: ретро-тест D10 показал, что это
    ~треть дней без причины, а после болезни высокая ВСР вообще ожидаема
    (отскок) и не должна читаться как «готов». Дополнительно запрещает
    возврат активный эпизод, окно тишины ``silence_days`` после эпизода
    (возврат по рампе, не покой) и высокая температура запястья: с
    температурой тренироваться нельзя из-за риска вирусного миокардита.
    """
    try:
        rows = conn.execute(
            """SELECT date, rhr, hrv_sdnn, wrist_temp FROM aw_daily
               ORDER BY date"""
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        return BaselineGate(False, today, 0, ("нет данных часов: aw_daily пуст",))

    dates = [row[0] for row in rows]
    rhr = [row[1] for row in rows]
    hrv = [row[2] for row in rows]
    wrist = [row[3] for row in rows]
    rhr_z = health_illness.zscore(rhr)
    hrv_z = health_illness.zscore(hrv)
    wrist_z = health_illness.zscore(wrist)

    if today is None:
        today = dates[-1]
    index = None
    for i, day in enumerate(dates):
        if day <= today:
            index = i
    if index is None:
        return BaselineGate(False, today, 0, (f"нет данных на {today}",))

    reasons: list[str] = []
    detail: list[dict] = []
    checked = 0
    for offset in range(nights):
        i = index - offset
        if i < 0:
            reasons.append(f"не хватает ночей: есть только {checked}")
            break
        checked += 1
        night = {
            "date": dates[i],
            "rhr": rhr[i], "rhr_z": rhr_z[i],
            "hrv": hrv[i], "hrv_z": hrv_z[i],
            "wrist": wrist[i], "wrist_z": wrist_z[i],
        }
        detail.append(night)
        # Односторонне: плохо, когда RHR выше фона или ВСР ниже фона.
        if rhr_z[i] is None:
            reasons.append(f"{dates[i]}: нет фона для RHR")
        elif rhr_z[i] > GATE_Z:
            reasons.append(f"{dates[i]}: RHR z {rhr_z[i]:+.1f} выше фона "
                           f"(>{GATE_Z:g})")
        if hrv_z[i] is None:
            reasons.append(f"{dates[i]}: нет фона для ВСР")
        elif hrv_z[i] < -GATE_Z:
            reasons.append(f"{dates[i]}: ВСР z {hrv_z[i]:+.1f} ниже фона "
                           f"(<{-GATE_Z:g})")
        if wrist_z[i] is not None and wrist_z[i] > GATE_Z_TEMP:
            reasons.append(f"{dates[i]}: t запястья z {wrist_z[i]:+.1f}")

    silence_until: str | None = None
    if _has_table(conn, "illness_episodes"):
        episodes = health_illness.episodes(conn)
        for episode in episodes:
            if episode.start <= today <= episode.end:
                reasons.append(
                    f"активный эпизод {episode.start}…{episode.end} "
                    f"({episode.level})")
                break
        else:
            # Окно тишины: сразу после болезни возврат только по рампе. Высокая
            # ВСР здесь - ожидаемый отскок, а не допуск к нагрузке. Если
            # эпизоды идут близко, берём самое позднее окончание окна.
            if silence_days > 0:
                day = date_cls.fromisoformat(today)
                best = None          # (start, end, until) самого позднего окна
                for episode in episodes:
                    if episode.level == "gap_only":
                        continue
                    end = date_cls.fromisoformat(episode.end)
                    if end < day <= end + timedelta(days=silence_days):
                        until = end + timedelta(days=silence_days)
                        if best is None or until > best[2]:
                            best = (episode.start, episode.end, until)
                if best is not None:
                    reasons.append(
                        f"окно тишины после эпизода {best[0]}…{best[1]}: "
                        f"возврат по рампе до {best[2].isoformat()}")
                    silence_until = best[2].isoformat()
    return BaselineGate(
        ready=not reasons and checked >= nights,
        today=today,
        nights=checked,
        reasons=tuple(reasons),
        rows=tuple(detail),
        silence_until=silence_until,
    )


@dataclass(slots=True)
class RampPlan:
    """План возврата по одному упражнению."""

    exercise: str
    today: str
    days_off: int
    ramp: Ramp
    base_date: str | None
    base_weight: float | None
    base_reps: int | None
    base_rm: float | None
    start_weight: float | None
    start_reps: int | None
    sets: int
    program: core.Progression | None
    full_return_step: int | None
    gate: BaselineGate
    #: Правило округления стартового веса рампы (решение O2 = A): floor -
    #: ближайший доступный вес **не тяжелее** целевого (``core.snap_to_ladder``),
    #: а не ``next_weight`` (тот даёт вверх). Структурное поле, не текст:
    #: дайджест рендерит его механически, тест прибивает (1RM => 42 => 40).
    rule: str = "ramp_floor"
    warnings: tuple[str, ...] = ()
    reason: str | None = None
    #: КОИ КОИ упражнения за предболезненный день и за его неделю. Нужны, чтобы
    #: в строке «предболезненный вес» рядом с 1RM было видно, насколько тяжёлой
    #: была последняя тренировка до болезни (и не завышена ли база разминкой).
    base_koi: float | None = None
    base_week: str | None = None
    base_week_koi: float | None = None

    def to_dict(self) -> dict:
        steps = []
        if self.program is not None:
            for step in self.program:
                steps.append({
                    "step": step.index,
                    "sets": " ".join(f"{s.weight:g}x{s.reps}" for s in step.sets),
                    "weight": step.weight,
                    "reps": step.reps,
                    "load": step.load,
                    "koi": step.koi,
                    "band": step.band,
                })
        return {
            "exercise": self.exercise,
            "today": self.today,
            "days_off": self.days_off,
            "ramp": self.ramp.label,
            "start_pct": self.ramp.start_pct,
            "base_date": self.base_date,
            "base_weight": self.base_weight,
            "base_reps": self.base_reps,
            "base_rm": self.base_rm,
            "start_weight": self.start_weight,
            "start_reps": self.start_reps,
            "sets": self.sets,
            "full_return_step": self.full_return_step,
            "rule": self.rule,
            "base_koi": self.base_koi,
            "base_week": self.base_week,
            "base_week_koi": self.base_week_koi,
            "gate": self.gate.to_dict(),
            "warnings": list(self.warnings),
            "reason": self.reason,
            "steps": steps,
        }


def _best_set(conn: sqlite3.Connection, exercise: str, day: str):
    """Лучший рабочий подход дня - по **подходному** e1RM, а не по сессионному.

    ``rm_session`` одинаков на всех подходах одной сессии (это ``MAX(rm)`` по
    окну ``session_id``), поэтому сортировка по нему не выбирает лучший подход,
    а отдаёт произвольную строку - обычно первый (самый лёгкий) подход. Именно
    из-за этого предболезненный вес брался как разминочный.
    """
    return conn.execute(
        """SELECT weight, reps, rm, rm_session FROM sets
           WHERE exercise = ? AND date = ? AND rm_session IS NOT NULL
                 AND is_warmup = 0 AND skipped = 0
           ORDER BY rm DESC, weight DESC LIMIT 1""",
        (exercise, day),
    ).fetchone()


def _koi_on_day(conn: sqlite3.Connection, exercise: str, day: str) -> float | None:
    """Суммарный КОИ упражнения за день (``None``, если КОИ не посчитан).

    В синтетических базах тестов колонки ``koi`` может не быть вовсе - тогда
    честнее вернуть ``None``, чем падать.
    """
    try:
        row = conn.execute(
            "SELECT SUM(koi) FROM sets "
            "WHERE exercise = ? AND date = ? AND koi IS NOT NULL",
            (exercise, day),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row and row[0] is not None else None


def _iso_week_label(day: str) -> str:
    """Метка ISO-недели (``ГГГГ-Www``) - календарём Python, без SQLite.

    ``strftime('%G-W%V', ...)`` умеют только SQLite 3.46+ (май 2024), а в
    сборках Python на CI он старше: такая метка там возвращала NULL. У
    ``datetime`` ISO-неделя есть всегда - через ``isocalendar()``.
    """
    year, week, _ = date_cls.fromisoformat(day).isocalendar()
    return f"{year}-W{week:02d}"


def _iso_week_bounds(day: str) -> tuple[str, str]:
    """Понедельник и воскресенье той ISO-недели, в которую попадает ``day``."""
    current = date_cls.fromisoformat(day)
    monday = current - timedelta(days=current.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def _koi_week(conn: sqlite3.Connection, exercise: str, day: str):
    """Метка ISO-недели и суммарный КОИ упражнения за неё.

    Неделя в SQL отбирается границами (Пн-Вс), а не сравнением метки:
    так не нужны устаревающие ``strftime``-форматы. Метка же считается в
    Python (:func:`_iso_week_label`) и на старом SQLite остаётся валидной.
    """
    try:
        label = _iso_week_label(day)
        monday, sunday = _iso_week_bounds(day)
        total = conn.execute(
            "SELECT SUM(koi) FROM sets "
            "WHERE exercise = ? AND koi IS NOT NULL "
            "  AND date BETWEEN ? AND ?",
            (exercise, monday, sunday),
        ).fetchone()[0]
    except (sqlite3.OperationalError, ValueError):
        return None, None
    return label, total



def return_plan(
    conn: sqlite3.Connection,
    exercise: str | None = None,
    *,
    episode: health_illness.Episode | None = None,
    today: str | None = None,
    steps: int | None = None,
) -> RampPlan:
    """Построить рампу возврата от предболезненного рабочего веса.

    База - **последняя тренировка этого упражнения до начала эпизода**, а не
    последняя вообще и не план: важно вернуться к своему весу, а не к чужому.
    Старт (верх первой недели) и объём берутся из :data:`RAMP_TABLE` по длине
    простоя; шаги строит
    :func:`ramp_program` так, чтобы полный вес возвращался **внутри** полосы
    (``Ramp.weeks`` недель), а не когда-то потом. База КОИ - предболезненный
    1RM (чтобы в колонке «нагрузка» было видно, что первые недели лёгкие), а
    КОИ самого предболезненного дня и его недели попадают в шапку рядом с 1RM.
    """
    today = today or _today(conn)
    exercise = exercise or default_exercise(conn)
    if not today or not exercise:
        return RampPlan(
            exercise=exercise or "?", today=today or "?", days_off=0,
            ramp=RAMP_TABLE[0], base_date=None, base_weight=None,
            base_reps=None, base_rm=None, start_weight=None, start_reps=None,
            sets=0, program=None, full_return_step=None,
            gate=BaselineGate(False, today, 0, ("нет данных",)),
            reason="нет упражнения или даты: сначала разбери журнал и часы",
        )

    if episode is None:
        if _has_table(conn, "illness_episodes"):
            candidates = [
                e for e in health_illness.episodes(conn)
                if e.end <= today and e.level in ("confirmed", "probable")
            ]
            episode = candidates[-1] if candidates else None

    # Последняя тренировка упражнения до начала эпизода (или до сегодня).
    anchor = episode.start if episode else today
    row = conn.execute(
        """SELECT date FROM sets
           WHERE exercise = ? AND date < ? AND rm_session IS NOT NULL
                 AND is_warmup = 0 AND skipped = 0
           ORDER BY date DESC LIMIT 1""",
        (exercise, anchor),
    ).fetchone()
    if row is None:
        return RampPlan(
            exercise=exercise, today=today, days_off=0, ramp=RAMP_TABLE[0],
            base_date=None, base_weight=None, base_reps=None, base_rm=None,
            start_weight=None, start_reps=None, sets=0, program=None,
            full_return_step=None, gate=baseline_gate(conn, today),
            reason=f"нет тренировок «{exercise}» до {anchor} - не от чего "
                   f"строить рампу",
        )
    base_date = row[0]
    best = _best_set(conn, exercise, base_date)
    base_weight = best["weight"] if best else None
    base_reps = best["reps"] if best else None
    base_rm = (best["rm"] if best and best["rm"] is not None
               else (best["rm_session"] if best else None))
    base_koi = _koi_on_day(conn, exercise, base_date)
    base_week, base_week_koi = _koi_week(conn, exercise, base_date)

    days_off = (date_cls.fromisoformat(today) - date_cls.fromisoformat(base_date)).days
    ramp = ramp_for(days_off)

    weights = equipment.available_weights(exercise)
    warnings: list[str] = []

    if not weights or base_weight is None:
        return RampPlan(
            exercise=exercise, today=today, days_off=days_off, ramp=ramp,
            base_date=base_date, base_weight=base_weight, base_reps=base_reps,
            base_rm=base_rm, start_weight=None, start_reps=None, sets=0,
            program=None, full_return_step=None, gate=baseline_gate(conn, today),
            reason=f"для «{exercise}» нет внешнего веса - рампа не строится",
            base_koi=base_koi, base_week=base_week,
            base_week_koi=base_week_koi,
        )

    target = base_weight * ramp.start_pct
    start_weight = core.snap_to_ladder(target, weights)
    if start_weight is None:
        start_weight = weights[0]
        warnings.append(
            f"самый лёгкий снаряд {weights[0]:g} кг тяжелее целевого старта "
            f"{target:.1f} кг - начинаем с него")
    elif abs(start_weight - target) > 1e-9:
        warnings.append(
            f"в инвентаре нет {target:.1f} кг: верх первой недели с "
            f"ближайшего не тяжелее - {start_weight:g} кг")

    start_reps = base_reps or core.REPS_ON_INCREASE
    program = ramp_program(
        weights, start_weight, base_weight, start_reps, sets=ramp.sets,
        weeks=ramp.weeks, steps=steps, reference_rm=base_rm,
    )
    full_step = None
    if base_weight:
        target_weight = core.snap_to_ladder(base_weight, weights) or base_weight
        if target_weight < base_weight - 1e-9:
            warnings.append(
                f"предболезненного веса {base_weight:g} кг нет в инвентаре - "
                f"добраемся до {target_weight:g} кг")
        for step in program:
            # Верхний подход недели - рабочий: полный возврат наступает,
            # когда до базы дорастает именно он.
            if step.sets[-1].weight >= target_weight - 1e-9:
                full_step = step.index
                break

    return RampPlan(
        exercise=exercise, today=today, days_off=days_off, ramp=ramp,
        base_date=base_date, base_weight=base_weight, base_reps=base_reps,
        base_rm=base_rm, start_weight=start_weight, start_reps=start_reps,
        sets=ramp.sets, program=program, full_return_step=full_step,
        gate=baseline_gate(conn, today), warnings=tuple(warnings),
        base_koi=base_koi, base_week=base_week, base_week_koi=base_week_koi,
    )


@dataclass(slots=True)
class ReturnRow:
    """Прошлый возврат после эпизода: скорость реампера и рецидив."""

    episode_start: str
    episode_end: str
    level: str
    base_date: str
    first_date: str
    days_off: int
    pre_rm: float
    first_rm: float
    horizon_rm: float
    speed_pct_week: float
    rebound_in: int | None

    @property
    def first_ratio(self) -> float:
        return self.first_rm / self.pre_rm if self.pre_rm else 0.0

    @property
    def kind(self) -> str:
        if self.first_ratio >= HARD_RATIO:
            return "жёсткий"
        if self.first_ratio <= SOFT_RATIO:
            return "мягкий"
        return "средний"

    def to_dict(self) -> dict:
        return {
            "episode_start": self.episode_start,
            "episode_end": self.episode_end,
            "level": self.level,
            "base_date": self.base_date,
            "first_date": self.first_date,
            "days_off": self.days_off,
            "pre_rm": self.pre_rm,
            "first_rm": self.first_rm,
            "horizon_rm": self.horizon_rm,
            "speed_pct_week": self.speed_pct_week,
            "rebound_in": self.rebound_in,
            "first_ratio": self.first_ratio,
            "kind": self.kind,
        }


def returns(
    conn: sqlite3.Connection,
    exercise: str | None = None,
    *,
    rebound_days: int = RETURN_REBOUND_DAYS,
    horizon_days: int = RETURN_HORIZON_DAYS,
) -> list[ReturnRow]:
    """Разметить прошлые возвраты: как быстро вернулся и был ли рецидив.

    Скорость реампера считается как прирост e1RM за ``horizon_days`` после
    первого занятия, нормированный на предболезненный вес и переведённый в
    проценты в неделю. Рецидив - новый эпизод, начавшийся в течение
    ``rebound_days`` после возврата. Выборка крошечная (эпизодов единицы),
    поэтому это описательная статистика, а не доказательство.
    """
    exercise = exercise or default_exercise(conn)
    if not exercise:
        return []
    sessions = _sessions(conn, exercise)
    if not sessions:
        return []
    if not _has_table(conn, "illness_episodes"):
        return []
    episodes = health_illness.episodes(conn)
    rows: list[ReturnRow] = []
    for episode in episodes:
        if episode.level not in ("confirmed", "probable"):
            continue
        prior = [s for s in sessions if s[0] < episode.start]
        after = [s for s in sessions if s[0] > episode.end]
        if not prior or not after:
            continue
        base_date, pre_rm = prior[-1]
        first_date, first_rm = after[0]
        horizon_limit = (date_cls.fromisoformat(first_date)
                         + timedelta(days=horizon_days)).isoformat()
        window = [rm for day, rm in sessions
                  if first_date < day <= horizon_limit]
        horizon_rm = max(window) if window else first_rm
        weeks = horizon_days / 7.0
        speed = (horizon_rm - first_rm) / pre_rm / weeks * 100.0 if pre_rm else 0.0
        rebound_in = None
        for other in episodes:
            if other is episode or other.start <= episode.end:
                continue
            delta = (date_cls.fromisoformat(other.start)
                     - date_cls.fromisoformat(first_date)).days
            if 0 <= delta <= rebound_days:
                rebound_in = delta
                break
        rows.append(ReturnRow(
            episode_start=episode.start, episode_end=episode.end,
            level=episode.level, base_date=base_date, first_date=first_date,
            days_off=(date_cls.fromisoformat(first_date)
                      - date_cls.fromisoformat(base_date)).days,
            pre_rm=pre_rm, first_rm=first_rm, horizon_rm=horizon_rm,
            speed_pct_week=speed, rebound_in=rebound_in,
        ))
    return rows


# --------------------------------------------------------------------------
# Вывод
# --------------------------------------------------------------------------


def format_curve(
    points: list[DetrainPoint], exercise: str | None = None
) -> str:
    """Личная детрейн-кривая + литературные ориентиры."""
    title = f"детрейн по «{exercise}»" if exercise else "детрейн по всем упражнениям"
    if not points:
        return (f"{title}: нет простоев ≥ {DETRAIN_GAP_MIN} дн "
                f"(нужны две тренировки упражнения вокруг перерыва)")
    lines = [title]
    bins = detraining_bins(points)
    body = table(
        ["простой", "n", "средняя просадка", "медиана", "размах (IQR)"],
        [[b["label"], b["n"],
          "—" if b["mean"] is None else f"{b['mean'] * 100:+.1f} %",
          "—" if b["median"] is None else f"{b['median'] * 100:+.1f} %",
          "—" if b["iqr"] is None else f"{b['iqr'] * 100:.1f} %"]
         for b in bins],
    )
    lines.append(body)
    lines.append("")
    lines.append("литературные ориентиры (сила - живучее качество, "
                 "мощность - хрупкое):")
    lines.append(table(
        ["простой", "что происходит", "источник"],
        [[a, b, c] for a, b, c in DETRAIN_ANCHORS],
    ))
    return "\n".join(lines)


def format_gate(gate: BaselineGate) -> str:
    """Гейт возврата: готов / держать, с причинами."""
    head = "гейт ВСР/RHR: " + ("можно возвращаться" if gate.ready
                               else "держать, рано")
    lines = [f"{head}  (ночей проверено: {gate.nights}, сегодня "
             f"{gate.today or '?'})"]
    if gate.rows:
        body = table(
            ["ночь", "RHR", "z RHR", "ВСР", "z ВСР", "t зап.", "z t"],
            [[r["date"],
              "" if r["rhr"] is None else f"{r['rhr']:.0f}",
              "" if r["rhr_z"] is None else f"{r['rhr_z']:+.1f}",
              "" if r["hrv"] is None else f"{r['hrv']:.0f}",
              "" if r["hrv_z"] is None else f"{r['hrv_z']:+.1f}",
              "" if r["wrist"] is None else f"{r['wrist']:.2f}",
              "" if r["wrist_z"] is None else f"{r['wrist_z']:+.1f}"]
             for r in gate.rows],
        )
        lines.append(body)
    for reason in gate.reasons:
        lines.append(f"  - {reason}")
    return "\n".join(lines)


def format_return(plan: RampPlan) -> str:
    """Готовая рампа возврата для CLI и ноутбука."""
    lines = [f"=== возврат к весам: {plan.exercise} ==="]
    if plan.reason and plan.program is None:
        lines.append(f"! {plan.reason}")
        return "\n".join(lines)
    if plan.base_weight is not None:
        line = (
            f"предболезненный вес: {plan.base_weight:g} кг"
            + (f" x {plan.base_reps}" if plan.base_reps else "")
            + (f" ({plan.base_date})" if plan.base_date else "")
            + (f", 1RM {plan.base_rm:.1f}" if plan.base_rm else ""))
        if plan.base_koi is not None:
            line += f", КОИ упражнения {plan.base_koi:.2f}"
        if plan.base_week_koi is not None and plan.base_week:
            line += f", КОИ за неделю {plan.base_week}: {plan.base_week_koi:.2f}"
        lines.append(line)
    weeks_full = plan.full_return_step
    if weeks_full is None:
        weeks_full = plan.ramp.weeks
    lines.append(
        f"простой: {plan.days_off} дн  →  полоса «{plan.ramp.label}», "
        f"верх 1-й недели {plan.ramp.start_pct:.0%} от базы, "
        f"объём −{plan.ramp.volume_cut:.0%}, полный возврат ~{weeks_full:g} нед "
        f"(один шаг в неделю)")
    if plan.start_weight is not None:
        target = (plan.base_weight or 0) * plan.ramp.start_pct
        week = ""
        if plan.program is not None and len(plan.program):
            week = "первая неделя: " + "/".join(
                f"{s.weight:g}" for s in plan.program[0].sets) + "; "
        lines.append(
            f"{week}верх {plan.start_weight:g} кг (цель {target:.1f} кг), "
            f"подходов {plan.sets}; повторы подобраны так, чтобы КОИ "
            f"тренировки оставался в Optimal")
    if plan.full_return_step is not None:
        lines.append(
            f"полный вес достигается на шаге {plan.full_return_step} "
            f"(неделя {plan.full_return_step}) - дальше растим повторы "
            f"и добавляем мощность последней")
    elif plan.program is not None:
        lines.append("в пределах рампы полный вес не достигается - "
                     "продолжай волной после него")

    if plan.program is not None and len(plan.program):
        base = plan.base_weight or plan.program[0].weight
        rows = []
        for step in plan.program:
            share = step.weight / base if base else None
            rows.append([
                step.index,
                " ".join(f"{s.weight:g}x{s.reps}" for s in step.sets),
                "" if share is None else f"{share:.0%}",
                "" if step.load is None else f"{step.load:.0%}",
                f"{step.koi:.2f}" if step.koi is not None else "",
                step.band or "",
            ])
        lines.append("")
        lines.append("рампа (подходы - лесенка с перекрытием недель, доля и "
                     "нагрузка - по верхнему):")
        lines.append(table(["шаг", "подходы", "доля", "нагрузка", "КОИ", "режим"],
                           rows))

    lines.append("")
    lines.append(format_gate(plan.gate))
    lines.append("")
    lines.append("правила: симптомы вернулись - ступень назад и 24 ч без "
                 "симптомов; мощность/взрывное включаем последними; "
                 "с температурой и миалгией не тренируемся (риск миокардита).")
    for warning in plan.warnings:
        lines.append(f"~ {warning}")
    return "\n".join(lines)


def format_returns(rows: list[ReturnRow], exercise: str | None = None) -> str:
    """Таблица прошлых возвратов + сводка по рецидивам."""
    title = f"возвраты по «{exercise}»" if exercise else "возвраты"
    if not rows:
        return f"{title}: нет эпизодов с тренировками до и после"
    body = table(
        ["эпизод", "возврат", "простой", "1-й вес", "скорость", "рецидив", "тип"],
        [[r.episode_start + "…" + r.episode_end, r.first_date, r.days_off,
          f"{r.first_ratio:.0%}", f"{r.speed_pct_week:+.1f} %/нед",
          "—" if r.rebound_in is None else f"через {r.rebound_in} дн",
          r.kind]
         for r in rows],
    )
    out = [title, body, ""]
    for kind in ("жёсткий", "средний", "мягкий"):
        group = [r for r in rows if r.kind == kind]
        if not group:
            continue
        rebound = sum(1 for r in group if r.rebound_in is not None)
        out.append(f"{kind}: {len(group)}, рецидив у {rebound} "
                   f"({rebound / len(group):.0%})")
    out.append("")
    out.append("выборка крошечная - это описание, а не доказательство "
               "(Bruusgaard 2010; Nuzzo 2024; Raysmith & Drew 2016).")
    return "\n".join(out)


def dumps(value) -> str:
    """JSON-представление для ``--json``."""
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    elif isinstance(value, list):
        value = [v.to_dict() if hasattr(v, "to_dict") else v for v in value]
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
