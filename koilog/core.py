"""Расчёты 1RM, КОИ и программа роста нагрузки.

Модуль чистый: не знает про sqlite3, csv и файловый ввод-вывод.
Всё, что здесь считается, покрывается тестами без БД.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

# --------------------------------------------------------------------------
# Константы
# --------------------------------------------------------------------------

#: Вес собственного тела, кг. Из него выводится «суммарный» вес записей,
#: где указан только жилет (см. etl.resolve_bodyweight).
BODY_WEIGHT = 70.0

#: Brzycki валидна примерно до 10-12 повторов. 20 - верхняя граница,
#: выше которой экстраполяция становится бессмысленной (коэффициент 2.118).
#: Менять здесь, если понадобится ужесточить.
RM_MAX_REPS = 20

#: Пороги КОИ за **одну тренировку по одному упражнению**: меньше 0.4 -
#: слишком мало, 0.4…1 - оптимум, 1…2 - тяжело, больше 2 - перебор. Полоса
#: считается по сумме подходов упражнения за вечер (см. ``etl.SESSION_KOI_VIEW``).
KOI_BANDS: tuple[tuple[float, str], ...] = (
    (0.4, "Minimal"),
    (1.0, "Optimal"),
    (2.0, "Load"),
)
KOI_ABOVE = "Crazy"

#: Пороги **недельного** КОИ по одному упражнению: меньше 2 - лёгкая неделя,
#: 2…3 - тяжёлая, 3…4 - очень тяжёлая, больше 4 - «Вы сошли с ума??».
KOI_BANDS_WEEK: tuple[tuple[float, str], ...] = (
    (2.0, "Лёгкий"),
    (3.0, "Тяжёлый"),
    (4.0, "Очень тяжёлый"),
)
KOI_ABOVE_WEEK = "Вы сошли с ума??"

#: Сколько повторов предлагать на новом весе.
REPS_ON_INCREASE = 6

#: Сколько шагов показывать в сводке переходов по весам.
TRANSITIONS = 13

#: Границы поиска повторов на новом весе. Минимум 4: брать новую гантель
#: ради пары повторов смысла нет.
WAVE_MIN_REPS = 4
WAVE_MAX_REPS = 12


# --------------------------------------------------------------------------
# 1RM
# --------------------------------------------------------------------------


def rm_factor(reps: int) -> float | None:
    """Коэффициент Brzycki ``36 / (37 - reps)``."""
    if reps is None or reps < 1 or reps > RM_MAX_REPS:
        return None
    return 36.0 / (37 - reps)


def one_rm(weight: float | None, reps: int | None) -> float | None:
    """1RM по формуле Brzycki.

    Возвращает ``None``, если посчитать нельзя: нет веса, нет повторов,
    вес нулевой или повторов больше ``RM_MAX_REPS``. Раньше здесь возникали
    отрицательные и абсурдные значения (80 кг x 36 повторов -> 2880 кг).
    """
    factor = rm_factor(reps)
    if factor is None or weight is None or weight <= 0:
        return None
    return weight * factor


# --------------------------------------------------------------------------
# Интенсивность и КОИ
# --------------------------------------------------------------------------


def intensity(weight: float | None, rm: float | None) -> float | None:
    """Доля от 1RM: 0.85 = 85 %."""
    if weight is None or rm is None or rm <= 0:
        return None
    return weight / rm


def koi(reps: int | None, load: float | None) -> float | None:
    """КОИ (коэффициент относительной интенсивности): ``reps / (100 - инт.%)``.

    Интенсивность здесь — доля (``weight / rm``), а формула выведена в
    процентах, поэтому ``1 - load`` надо домножить на 100 (``10 повторов
    при 75 % -> 10 / 25 = 0.4``). Без этого домножения суммарный КОИ
    тренировки выходил в 100 раз больше и любая программа попадала в полосу
    «Crazy»; SQL в etl считает в той же шкале.

    ``None``, если интенсивность >= 100 %: знаменатель уходил в ноль или
    в минус, и разминка с удвоенным весом *вычиталась* из нагрузки
    (``6 / (100 - 169) = -0.087``).
    """
    if reps is None or load is None or load < 0:
        return None
    if load >= 1.0:
        return None
    return reps / ((1.0 - load) * 100.0)


def koi_band(
    value: float | None,
    bands: Sequence[tuple[float, str]] = KOI_BANDS,
    above: str = KOI_ABOVE,
) -> str | None:
    """Название режима по порогам ``KOI_BANDS`` (или других ``bands``)."""
    if value is None:
        return None
    for limit, name in bands:
        if value < limit:
            return name
    return above


# --------------------------------------------------------------------------
# Структуры предметной области
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SetSpec:
    """Один подход: вес и повторы."""

    weight: float
    reps: int

    @property
    def rm(self) -> float | None:
        return one_rm(self.weight, self.reps)

    @property
    def factor(self) -> float | None:
        return rm_factor(self.reps)


@dataclass(frozen=True, slots=True)
class KoIResult:
    """Сумма КОИ по набору подходов + подходы, в сумму не вошедшие."""

    total: float
    rejected: tuple[SetSpec, ...] = ()

    def __float__(self) -> float:
        return self.total


def koi_sum(sets: Iterable[SetSpec], rm: float | None) -> KoIResult:
    """Суммарный КОИ набора подходов относительно ``rm``.

    Подходы с интенсивностью >= 100 % не обрывают сумму, а отбрасываются
    в ``rejected``: раньше ``koi_all`` возвращал ``inf`` на первом таком
    подходе и терял все остальные.
    """
    total = 0.0
    rejected: list[SetSpec] = []
    for spec in sets:
        value = koi(spec.reps, intensity(spec.weight, rm))
        if value is None:
            rejected.append(spec)
        else:
            total += value
    return KoIResult(total, tuple(rejected))


# --------------------------------------------------------------------------
# Переходы по весам
# --------------------------------------------------------------------------


def next_weight(weight: float, available: Sequence[float], hands: int = 1) -> float | None:
    """Следующий вес из списка доступных снарядов.

    ``hands`` - сколько рук делят вес (1 или 2). Вес в килограммах делится
    на него перед поиском, а найденное значение умножается обратно, поэтому
    ``available`` и ``weight`` должны быть в одних единицах: для гантелей в
    две руки это суммарные килограммы (40 кг), а список может быть как
    суммарным (``10, 14, … 80``), так и на одну гантель - но тогда делить
    вес на 2 нельзя.

    ``None``, если вес вне списка или список уже исчерпан.
    """
    if hands < 1 or weight is None:
        return None
    unit = weight / hands
    index = _index_of(available, unit)
    if index is None or index >= len(available) - 1:
        return None
    return available[index + 1] * hands


def _index_of(available: Sequence[float], value: float) -> int | None:
    """Индекс значения с допуском на погрешность float-деления."""
    if value is None:
        return None
    for i, candidate in enumerate(available):
        if abs(candidate - value) < 1e-9:
            return i
    return None


def snap_to_ladder(
    weight: float,
    available: Sequence[float],
    hands: int = 1,
) -> float | None:
    """Ближайший вес из лестницы, не превышающий ``weight``.

    Нужен потому, что записанный вес не обязан лежать на лестнице инвентаря:
    ``Отжимания`` записаны как 82 кг, а ``Тяга штанги в наклоне`` - как 44 кг
    при лестнице с шагом 2.5. Без этого приведения программа не стартует
    вообще, а с ним стартует с честного веса, который точно есть.

    Возвращает ``None``, только если весь инвентарь **тяжелее** записанного
    веса (ни один снаряд не легче) - тогда подставлять нечего, и это уже
    сообщение об ошибке данных. Обратный случай (всё легче) не ``None``:
    берётся ближайший не тяжелее, то есть самый тяжёлый из лестницы.
    """
    if hands < 1 or not available:
        return None
    unit = weight / hands
    best: float | None = None
    for candidate in available:
        if candidate <= unit + 1e-9:
            best = candidate
    return None if best is None else best * hands


def ladder_index(
    weight: float,
    available: Sequence[float],
    hands: int = 1,
) -> int | None:
    """Индекс веса в лестнице; ``-1``, если он ниже первого снаряда."""
    if hands < 1 or not available:
        return None
    index = _index_of(available, weight / hands)
    return index


def find_optimal_reps(
    weight: float,
    target_rm: float | None,
    min_reps: int = 1,
    max_reps: int = RM_MAX_REPS,
) -> int | None:
    """Максимальное число повторов, при котором 1RM не превышает ``target_rm``.

    1RM монотонно растёт с повторами, поэтому после первого превышения
    перебор прекращается. ``None``, если ни один вариант не подошёл.
    """
    if target_rm is None or target_rm <= 0:
        return None
    best: int | None = None
    for reps in range(max(min_reps, 1), min(max_reps, RM_MAX_REPS) + 1):
        rm = one_rm(weight, reps)
        if rm is None:
            continue
        if rm <= target_rm:
            best = reps
        else:
            break
    return best


# --------------------------------------------------------------------------
# Программа роста нагрузки
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProgramStep:
    """Один шаг программы: набор подходов плюс диагностика."""

    index: int
    sets: tuple[SetSpec, ...]
    warmup: SetSpec | None
    reference_rm: float | None
    koi: float | None
    band: str | None
    rejected: tuple[SetSpec, ...] = field(default=())

    @property
    def weight(self) -> float:
        return self.sets[-1].weight

    @property
    def reps(self) -> int:
        return self.sets[-1].reps

    @property
    def set_rm(self) -> float | None:
        return self.sets[-1].rm

    @property
    def load(self) -> float | None:
        """Интенсивность последнего подхода относительно базы 1RM."""
        return intensity(self.weight, self.reference_rm)


@dataclass(frozen=True, slots=True)
class Progression:
    """Результат расчёта программы."""

    steps: tuple[ProgramStep, ...]
    available: tuple[float, ...]
    hands: int
    stopped: str | None = None

    def __iter__(self):
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    def __getitem__(self, index):
        return self.steps[index]


def progression(
    available: Sequence[float],
    start_weight: float,
    start_reps: int,
    sets: int = 3,
    hands: int = 1,
    warmup_weight: float | None = None,
    warmup_reps: int = 0,
    steps: int = 18,
    reference_rm: float | None = None,
) -> Progression:
    """Программа роста нагрузки: волна повторов, затем переход на новый вес.

    Что учтено, чтобы программа не врала:

    * база 1RM для КОИ пересчитывается по ходу программы как текущий максимум
      достигнутого, а не берётся один раз из стартового подхода - из-за этого
      сумма КОИ была всегда ``inf`` и все строки выводили «Crazy»;
    * разминка в КОИ не входит и не может дать отрицательный вклад;
    * один подход с интенсивностью >= 100 % не обрывает сумму, а попадает
      в ``rejected``;
    * волна ограничена ``RM_MAX_REPS``, иначе на исчерпании списка весов
      повторы росли до 37 и формула падала с ZeroDivisionError;
    * при исчерпании списка весов расчёт останавливается с пояснением в
      ``stopped``, а не зацикливается;
    * проверка «повысить вес» и добавление повтора выполняются **только
      после выравнивания повторов по всем рабочим подходам**: пока подходы
      не равны, происходит лишь сдвиг «волны» (``state[1:] + [last]``).
      Иначе повтор добавлялся в последний подход раньше, чем волна дошла
      до него, и подходы расходились ещё сильнее.

    ``start_weight`` - суммарный вес подхода в килограммах (для двух гантелей
    это удвоенный вес одной гантели). ``hands`` показывает, на сколько делить,
    чтобы найти значение в ``available``.

    ``reference_rm`` - база 1RM, если она известна заранее (например, посчитана
    по базе). По умолчанию берётся максимум 1RM стартовых подходов.
    """
    if sets < 1 or hands < 1:
        raise ValueError("sets и hands должны быть >= 1")

    warmup: SetSpec | None = None
    if warmup_weight and warmup_weight > 0 and warmup_reps > 0:
        warmup = SetSpec(warmup_weight, warmup_reps)

    state = [SetSpec(start_weight, start_reps)] * sets
    out: list[ProgramStep] = []
    stopped: str | None = None

    reference = reference_rm
    if reference is None:
        achieved = [spec.rm for spec in state if spec.rm is not None]
        reference = max(achieved) if achieved else None

    for index in range(steps):
        last = state[-1]
        result = koi_sum(state, reference)

        out.append(
            ProgramStep(
                index=index + 1,
                sets=tuple(state),
                warmup=warmup,
                reference_rm=reference,
                koi=result.total,
                band=koi_band(result.total),
                rejected=result.rejected,
            )
        )
        if index == steps - 1:
            break

        nxt = next_weight(last.weight, available, hands)
        all_equal = all(spec == last for spec in state)

        if not all_equal:
            # Проверки веса и повторов - только после выравнивания: волна
            # повторов должна дойти до последнего подхода целиком.
            state = state[1:] + [last]
        elif nxt is not None and (last.rm or 0) >= (one_rm(nxt, REPS_ON_INCREASE) or 0):
            state = state[1:] + [SetSpec(nxt, REPS_ON_INCREASE)]
        else:
            if last.reps >= RM_MAX_REPS:
                stopped = (
                    f"{last.weight:g} кг x {last.reps}: список весов исчерпан "
                    f"и предел {RM_MAX_REPS} повторов достигнут"
                )
                break
            state = state[1:] + [SetSpec(last.weight, last.reps + 1)]

        top = [spec.rm for spec in state if spec.rm is not None]
        if top and (reference is None or max(top) > reference):
            reference = max(top)

    return Progression(tuple(out), tuple(available), hands, stopped)


@dataclass(frozen=True, slots=True)
class TransitionStep:
    """Переход с одного веса на следующий.

    ``reference_rm`` - 1RM состояния, из которого сделан переход (текущий
    вес на его текущем числе повторов), ``delta`` - насколько изменился 1RM
    при переходе.
    """

    from_weight: float
    from_reps: int
    weight: float
    reps: int
    rm: float
    reference_rm: float

    @property
    def delta(self) -> float:
        return self.rm - self.reference_rm

    @property
    def load(self) -> float:
        return self.weight / self.reference_rm


@dataclass(frozen=True, slots=True)
class TransitionPlan:
    """Результат прогона по списку весов."""

    steps: tuple[TransitionStep, ...]
    available: tuple[float, ...]
    hands: int
    stopped: str | None = None

    def __iter__(self):
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    def __getitem__(self, index):
        return self.steps[index]


def transitions(
    available: Sequence[float],
    start_weight: float,
    start_reps: int,
    hands: int = 1,
    count: int = TRANSITIONS,
    decision_reps: int = REPS_ON_INCREASE,
    min_reps: int = WAVE_MIN_REPS,
    max_reps: int = WAVE_MAX_REPS,
    optimal_reps: bool = False,
) -> TransitionPlan:
    """Сводка переходов на новый вес по списку доступных снарядов.

    Один подход ползёт по списку: пока его 1RM не хватает, чтобы оправдать
    следующую гантель, растут повторы («волна»). Как только 1RM текущего
    состояния перекрывает 1RM следующего веса на ``decision_reps``
    повторов, происходит переход.

    Число повторов на новом весе выбирается одним из двух способов, и это
    единственный параметр, который стоит переключать:

    ``optimal_reps=False`` (по умолчанию)
        Ровно ``decision_reps`` повторов - то есть 6. Это «волна со сбросом
        на 6»: каждый новый вес начинается с предсказуемых 6 повторов, и
        шаг программы читается прямо в таблице. Раньше подбор повторов был
        обязательным, и на первом же переходе получалось 4 повтора, на
        втором 3, а дальше расчёт упирался в нижнюю границу поиска и
        останавливался - то есть половина переходов в сводке была
        вырожденной.
    ``optimal_reps=True``
        Максимум повторов, при котором 1RM не превышает базу
        (``find_optimal_reps``). Требует перебора и поэтому заметно медленнее.

    База берётся из 1RM текущего состояния, а не из константы; критерий выбора
    повторов - «максимум, не превышающий базу»; молчаливого дефолта в 6
    повторов нет, а прогон останавливается с пояснением, когда список весов
    исчерпан.
    """
    if not available or _index_of(available, start_weight / hands) is None:
        return TransitionPlan((), tuple(available), hands,
                             "стартовый вес не найден в списке снарядов")

    steps: list[TransitionStep] = []
    weight, reps = start_weight, start_reps
    stopped: str | None = None
    # «Волна» на одном весе не может идти дольше RM_MAX_REPS шагов, иначе
    # при зацикленном списке весов расчёт не завершится.
    budget = count * (RM_MAX_REPS + 2)

    for _ in range(budget):
        if len(steps) >= count:
            break

        current = one_rm(weight, reps)
        if current is None:
            stopped = f"{weight:g} кг x {reps}: 1RM не считается"
            break

        nxt = next_weight(weight, available, hands)
        if nxt is None:
            stopped = (
                f"{weight:g} кг - верх списка снарядов: дальше рост только "
                f"повторами на этом весе"
            )
            break

        if current < (one_rm(nxt, decision_reps) or 0.0):
            # Гантель пока не оправдана - продолжаем волну на текущем весе.
            if reps >= RM_MAX_REPS:
                stopped = (
                    f"{weight:g} кг x {reps}: следующая гантель не берётся, "
                    f"а предел {RM_MAX_REPS} повторов уже достигнут"
                )
                break
            weight, reps = weight, reps + 1
            continue

        new_reps = (
            find_optimal_reps(nxt, current, min_reps, max_reps)
            if optimal_reps else decision_reps
        )
        achieved = one_rm(nxt, new_reps) if new_reps is not None else None
        if achieved is None:
            stopped = (
                f"{nxt:g} кг: подобрать повторы без превышения базы не удалось"
                if optimal_reps
                else f"{nxt:g} кг x {new_reps}: 1RM не считается"
            )
            break

        steps.append(
            TransitionStep(
                from_weight=weight,
                from_reps=reps,
                weight=nxt,
                reps=new_reps,
                rm=achieved,
                reference_rm=current,
            )
        )
        weight, reps = nxt, new_reps

    if stopped is None and len(steps) < count:
        stopped = "прогон не сошёлся за отведённый бюджет шагов"

    return TransitionPlan(tuple(steps), tuple(available), hands, stopped)
