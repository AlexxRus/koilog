# R5B — «Лестница прогрессий для vest_only» (пререг, docs-only)

**Статус:** заявка на гейт лида; кода нет и до гейта не будет.  
**Якорь базы:** `sql/training.db`, sha256 `6a0075686cb725b9b05c958534980440375e7cbc0a7a7e7485186802d8014731`,  
58 150 912 B, `PRAGMA integrity_check = ok`, `COUNT(*) FROM sets = 21 040` (не менялась — все замеры `mode=ro`).  
**Якорь git:** `66ba540c3433117f9b089670cc5f8cdcbe93c049` (README п. 3 исправлен, RM-98 текст актуален).  
**Дата замеров:** 2026-11-07, Python 3.13, Windows.

**Спек-источник:** docs/rm-98-prereg.md §2.9/R5b («кандидат отдельного юнита»), код RM-98 (`ce1afaa`),  
реализация `progression_from_db` / `snap_to_ladder` / `available_weights`.

**Единица работы:** три коммита без смешивания скоупов — (1) этот пререг, (2) код+тесты, (3) docs-обвес.

---

## §1 Объём и границы

Юнит **закрывает дефект лестницы для vest_only упражнений после RM-98**:
- **RM-98** (`ce1afaa`) ввёл формулу **E = k·тело + добавка** (k = 0.6 для отжиманий, 1.0 для остальных);
- записанный `sets.weight` для `vest_only` уже содержит E (не сырой жилет);
- проблема: `progression_from_db` читает лестницу из `equipment.available_weights(..., vest=True)`,
  которая возвращает **VEST_LADDER = (0, 2, 4, ..., 20)**, а не **E-ladder = тело + VEST_LADDER**;
- результат: записанный вес 62 кг (например, 70·0.6 + 20 = 62) снимается на лестницу 0–20:
  `snap_to_ladder(62, VEST_LADDER)` → 20 кг, reference_rm ≈ 37.9 вместо ожидаемых ~105.7.

**Объём:** изменение витринной логики чтения (`stats.progression_from_db`, `equipment.available_weights`),
не данных (`etl.py`, `sets.weight`, база не трогаются).

**Фенсы:**
- `etl.py`, `sets.weight`, `rm` не меняются;
- `state.py`, оба ноутбука, `program_patterns.py` — дифф 0 B;
- живая база только `mode=ro`; `reset --hard` запрещён;
- исторический `docs/rm-98-prereg.md` §2.9 не переписывается — закрытие фиксируется строкой AGENTS.md в шаге 3.

---

## §2 Замеры (первым лицом, якорь 66ba540)

### Код
- `koilog/stats.py:1622–1781`: `progression_from_db` вызывает `available_weights(..., vest=vest)` (~:1687),
  затем `snap_to_ladder(weight, weights, hands)` (~:1729);
- `koilog/equipment.py:170–176`: профиль `vest` ссылается на `VEST_LADDER` = `(0, 2, 4, ..., 20)`;
- `koilog/equipment.py:237–246`: `BODYWEIGHT_LADDER` — `on_bodyweight=True`, `weights_of` суммирует тело+лестницу;
- `koilog/equipment.py:481–496`: `weights_of` **для bodyweight_ladder** складывает тело+добавку,
  но для профиля `vest` просто возвращает `profile.weights` (VEST_LADDER);
- `koilog/core.py:213–240`: `snap_to_ladder` — ближайший вес лестницы не тяжелее заданного.

### Воссоздание дефекта на живой базе
```
Упражнение: Отжимания
body_weight = 70.0 кг
Профиль: vest
Записанный вес (last_weight): 62.0 кг (уже E = 0.6·70 + 20 = 62)
```

**Текущее поведение:**
```python
available_weights("Отжимания", vest=True, body_weight=70.0)
  -> VEST_LADDER = (0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0)

snap_to_ladder(62.0, VEST_LADDER, hands=1)
  -> 20.0 кг  # Ближайший на лестнице 0-20, не превышающий 62

reference_rm = one_rm(20.0, reps=18) ≈ 37.9
```
Ожидание: E-ladder = `[70, 72, 74, 76, 78, 80, 82, 84, 86, 88, 90]` (тело + добавка).  
snap_to_ladder(62.0, E-ladder) → `None` (все веса лестницы > 62, значит программа не стартует, что честно).

**Вывод дефекта:**
| Что | Текущее значение | Ожидаемое |
|---|---|---|
| `available_weights("Отжимания", vest=True)` | VEST_LADDER (0-20) | E-ladder = тело + VEST_LADDER (70-90) |
| `snap_to_ladder(62, ladder)` | 20 кг | None (62 < 70) или ближайший ≥ 62 |
| `reference_rm` при старте | ≈ 37.9 | ≈ 105.7 (если начать от 62) |

---

## §3 Карта поверхностей

**Чтение:**
- `stats.progression_from_db(exercise, vest=True, body_weight=...)` — основной вызов;
- `equipment.available_weights(exercise, vest=True, body_weight=...)` — источник лестницы;
- `core.snap_to_ladder(weight, available, hands)` — снятие на лестницу;
- `core.one_rm(weight, reps)` — reference_rm.

**Профили (equipment.py):**
- `EXERCISE_PROFILE["Отжимания"]` = `_V` (="vest");
- `PROFILES["vest"].weights` = `VEST_LADDER` (0-20);
- `BODYWEIGHT_LADDER` = `on_bodyweight=True`, использует `weights_of(body_weight=...)`.

**Исправление (план):**
- Добавить ветку `on_vest_ladder` (или параметр `on_bodyweight` для vest-профилей);
- Либо изменить логику `available_weights` для vest-only упражнений (отжимания, отжимания от пола/скамьи);
- Либо создать новый профиль `vest_bodyweight` (тело + vest).

---

## §5 Пины (должны остаться зелёными после фикса)

**Регресс-пины на non-vest ветку:**
- `pushups_max`, `bodyweight_max` (outlier-правила etl.py) — не тронуты;
- `available_weights(..., vest=False)` для bodyweight-упражнений (пустой список) — регресс-пин;
- `total_bodyweight` упражнения (подтягивания) работают через `bodyweight_ladder` — регресс-пин;
- `absolute` упражнения (гантели, штанга) — регресс-пин.

**Пины поведения:**
- `snap_to_ladder(weight, ladder, hands)` — детерминизм (ближайший не тяжелее);
- `one_rm(weight, reps)` — формула не меняется;
- `next_weight(snapped, ladder)` — переход вверх по лестнице не меняется.

---

## §6 Развилки (решения принимаю до кода)

### Р1 — Источник лестницы для vest_only

| Вариант | Описание | Цена |
|---|---|---|
| **A** (рекомендуется) | `available_weights(..., vest=True, body_weight=X)` для vest-only упражнений = `tuple(X + a for a in VEST_LADDER)` | Требуется передать `body_weight` в профиль `vest` (сейчас игнорируется); тесты на synthetic DB с разными X |
| B | Новый профиль `vest_bodyweight` (как `bodyweight_ladder`, но `on_bodyweight=True`); маппинг упражнений на него | Дублирование логики; нужно менять `EXERCISE_PROFILE` для всех vest-only |
| C | Не фиксить (допустить, что vest_only прогрессии не строятся при весе вне 0-20) | Прогрессия сломана для многих записей |

### Р2 — Пересчёт reference_rm

| Вариант | Описание |
|---|---|
| **A** (рекомендуется) | `reference_rm` считается от snapped веса через `one_rm(snapped, reps)` — формулы `core.one_rm` / `rm_factor` не меняются |
| B | reference_rm от исходного записанного веса (до snap), если snap дал None |

### Р3 — snap-правило для значений между ступенями

| Вариант | Описание |
|---|---|
| **A** (рекомендуется) | Текущее поведение: ближайший **не тяжелее** (`snap_to_ladder` — floor); если вес < min(ladder) → None |
| B | Ближайший **округлённый** (могли бы начать с чуть большего) |

### Р4 — Поведие при E вне диапазона лестницы

| Вариант | Описание |
|---|---|
| **A** (рекомендуется) | Если E < min(E-ladder) → программа не строится (reason: «вес легче минимального снаряда»); если E > max(E-ladder) → последний шаг |
| B | Если E вне диапазона → clamp к границе без ошибки |

---

## §7 План тестов (код — в шаге 2)

**Новые тесты на vest_only лестницу:**
1. `test_vest_only_progression_starts_at_E_ladder`: «Отжимания» с body_weight=70, last_weight=62 → E-ladder=[70..90], snap=None → reason «легче минимального»;
2. `test_vest_only_progression_snaps_correctly`: body_weight=70, last_weight=82 → snap=82 (точное совпадение) или 80 (nearest-floor), reference_rm≈105;
3. `test_vest_vs_bodyweight_ladder_distinction`: vest-only ≠ bodyweight_ladder (последний для total_bodyweight);
4. `test_regression_non_vest_exercises`: pushups, planks остаются пустыми; подтягивания используют bodyweight_ladder.

**Пины на non-vest ветку:**
5. `test_total_bodyweight_uses_bodyweight_ladder`: подтягивания, брусья — тело+добавка;
6. `test_absolute_weights_unchanged`: гантели, штанги — без изменений.

**Ожидание количества тестов:** +6–8 новых, 0 удалённых.

---

## §8 Docs-план (шаг 3)

- Добавление строки в `AGENTS.md:246` («Очередь заказов») — замена «бэклог полировки guard'ов» на «R5B закрыт, дальше TBD»;
- Обновление раздела про RM-98 в README/AGENTS (при необходимости);
- Нормализация U+2011 дефиса в README (при следующем касании).

**§8.3:** меток юнита (R5B) в коде нет; в комментариях — ссылка на этот пререг.

---

## §9 Границы

**Не делаем:**
- Не меняем `etl.resolve_bodyweight` / `resolve_exercise` (запись данных);
- Не меняем `state.py`, `retrain.py` (рампа, гейт);
- Не меняем `ml_features`, `score` (ML-модуль);
- Не нормализуем U+2011 в README в этом коммите (отдельный микро-коммит при следующем docs-касании).

**Мониторинг:**
- После фикса пересчитать `koilog return --exercise "Отжимания"` — убедиться, что progression начинается с корректного веса;
- Проверить `koilog check` — выбросы vest-only не увеличились.

---

**Гейт:** код не начинать, пока Р1–Р4 не приняты.
