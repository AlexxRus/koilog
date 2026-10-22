# AGENT NOTES — карта кода (обновляется при каждом раунде)

Короткие пометки: где искать логику, какие функции использовать (не дублировать).

## 1. Тренировки, прогрессия, веса, 1RM (модулей `training.py` **нет**)

Использовать **существующую** логику (особенно для рампы 5a), не изобретать новую.
Точные места (проверены по коду):

- `koilog/core.py`:
  - `one_rm(weight, reps)` — оценка 1RM (Epley-подобная); `rm_factor(reps)` — множитель.
  - `next_weight(weight, available, hands=1)` — шаг к **ближайшему** весу стека
    (обычно вверх, если ступень выше). Это **штатная прогрессия** и
    **продолжение после рампы** (от последнего выполненного дня), **не** рампа.
  - `progression(...)` — прогрессия по прошлому.
- `koilog/stats.py`: `available_weights(...)` — стек веса под упражнение;
  `progression_from_db(...)` / `progression_from_available(...)` — прогрессия на
  реальных данных; по упражнению/периоду — `best_rm`, `best_day`, `prev_rm`,
  `rm_delta` (готовые агрегации, не писать заново).
- `koilog/equipment.py`: `weights_of(...)`, `available_weights(...)`,
  `in_stock(...)` — стек оборудования (гантели/штанга, шаги грифа).
- `koilog/health/load.py`: дневной лучший `load_daily.rm_best` (`MAX(rm_session)`
  по дню, строка ~445); `rm_regress(rm_best)` — динамика 1RM по дневным лучшим.

**Правило веса рампы (решение O2, A/A/A — см. `docs/state-machine-spec.md` §6/§10, реализовано в `health/state.py:ramp_target` + `retrain.return_plan`):**
- целевой вес рампы = **floor** от «положенного» по 1RM до болезни: примитив
  **`core.snap_to_ladder(weight, ladder)`** — ближайший доступный **не тяжелее**
  (лестница из `available_weights`/`weights_of`). **Не** `next_weight` — тот даёт
  вверх/ближайший, это отклонённый вариант B.
- Пометка — **структурное поле плана** (`rule: "ramp_floor"` в `retrain.RampPlan`,
  оно же в `RampTarget`), дайджест рендерит механически; тест прибивает
  `snap_to_ladder(42, …) == 40` на синтетике.
- Floor применяется к производному весу **каждого** дня рампы; после рампы
  `normal` продолжает **от последнего выполненного дня рампы** (там уже
  `next_weight`). Ниже минимума стека → минимальный доступный.
- `next_weight*`/`suggest*` — **только** штатная прогрессия и пост-рампа.

> **Правило:** перед реализацией рампы — взять точные сигнатуры найденных функций (grep) и вызывать их напрямую.

## 2. Конвейер здоровья (порядок звеньев)

### `koilog/health/apple.py` — выгрузка Apple Health
- `resolve_source(path=None)` — приоритет явного пути. Иначе ищет **самый свежий** `*.zip` в `data/`, `data/aw/`, иначе крупнейший `*.xml` (без CDA), иначе `APPLE_DIR/export.xml`. **Важно:** при отсутствии файла/архива бросает `FileNotFoundError` (нужен guard в refresh).
- `parse_apple(source)` — парсит XML/ZIP, возвращает данные.
- `write_apple(conn, data)` — пишет в `aw_*` (`aw_daily`, `aw_workouts`, `aw_sleep`, `aw_bodytemp`, `aw_ecg`, `aw_hr`, `aw_meta`).
- `_SCHEMA` — схема `aw_*` + `aw_meta`.

### `koilog/health/load.py` — нагрузка
- `load_daily(conn, window=None)` — пересобирает `load_daily` и `health_days`. Ряд **непрерывный** (нули вместо пропусков), чтобы ACWR не «сшивался» через простои. Читает `sets` (ETL), `aw_workouts`, `aw_daily`, `test_days`.
- `_session_rows`, `_workout_rows`, `_test_days`, `_aw_dates`, `_calendar`, `_apply_windows` (acute/chronic/ewma по SRPE/тоннажу), `_write`, `health_days`.
- Требует наличия таблиц `aw_*` (особенно `aw_daily`, `aw_workouts`).

### `koilog/health/illness.py` — эпизоды
- `build(conn, window=None)` — пересобирает `illness_episodes`, `illness_day`. Требует `aw_daily` (иначе RuntimeError). Читает `sets` (тренированные дни), находит gaps, мерджит эпизоды.
- `episodes(conn)`, `summarize(conn)`, `format_episodes(conn)`.
- `_SCHEMA` для `illness_episodes`, `illness_day`.

### `koilog/health/analysis.py` — срезы для ноутбука (stdlib-only)
- `signature(conn, window, level="confirmed")` — среднее каналов по эпизодам + строка `"обычные"` (`start == "—"`, подпись «обычные дни») = фон по дням **вне** эпизодов; по умолчанию только `confirmed`.
- `episode_channels(conn, episode_id=None, window=None)` (4.4, `e72fd7f`) — по каждому эпизоду (`illness.episodes`, все уровни) min/**mean**/max каждого канала и **день пика**. Список каналов фиксирован = `_CHANNELS` (закреплён в пререге до расчёта — пост-хок лов запрещён). Фон для ВСР-флагов — строка «обычные» из `signature` (**то же окно**), `bg = bg_row["ВСР, мс"]`; `hrv_high/low = mean vs bg`. Числа для пререга 4.1 (post-hoc): выше фона у **2/19** confirmed (у **10/33** по всем уровням — вероятные/gap чаще у фона; сверка «10/56»→«10/33» см. §8.5-поправку 20.10: `episode_channels` читает персистентную базу = 33 эпизода, «56» — сырой merge без окна).
- `format_episode_channels(rows)` — таблица «канал | min | mean | max | пик (дата)», пометки «выше/ниже фона» текстом (без стрелок, урок E4). CLI: `illness --detail ID`.
- `format_episode_channels([])` → «нет эпизодов (или эпизод с таким id не найден)».

> **Детрейн-бакеты (4.7, `b01bc09`):** `retrain._BINS` — 5 бакетов, длинный хвост разбит `(28, 60, "28–60 дн")` + `(60, 10**6, ">60 дн")`, полуоткрытые `[низ, верх)`; бакет `<7 дн` остаётся пустым (`min_off = DETRAIN_GAP_MIN = 7`). `detraining_bins` даёт `mean`/`median`/`iqr` (q75−q25, `_quantile`) по совокупности точек бина. **Display-only:** `detraining_bins` ← `format_curve` ← `cli.cmd_return`; в `state`/`digest`/`score`/`ml` бакетов нет.

### `koilog/health/refresh.py` — единый шаг (ноутбуки + CLI)
- `run(conn, deep=False, source=None, journal=_DEFAULT_JOURNAL)` — конвейер: **журнал → выгрузка → нагрузка → календарь → эпизоды → (ML при deep)**.
- **Журнал (`sets`)** — новая первая стадия: `journal.csv` грузится отдельным файлом от выгрузки часов, и без этой стадии свежая тренировка **молча не появлялась** в `sets` (ноутбук зовёт только `refresh`). Слепок `JOURNAL_KEY = "refresh_journal"` (`mtime:size`) в `aw_meta`; `_need_load_journal` скипает неизменённый файл. **Слепок пишется ПОСЛЕ часовой стадии** — `apple.write_apple` чистит `aw_meta`, слепок, поставленный первым, был бы стёрт. `journal=None`/`False` — журнала нет (чистый клон, тесты); sentinel `_DEFAULT_JOURNAL` → `etl.DEFAULT_JOURNAL`. В CLI: `refresh --journal PATH` / `refresh --no-journal`.
- Парсинг XML (~45с) **скипается** по слепку: `_need_parse` сравнивает `mtime:size` источника с `aw_meta.refresh_source` (`REFRESH_KEY`). **Слепок пишется ТОЛЬКО после успешной записи** `aw_*` (если разбор упал — следующий прогон попробует снова).
- **Guard (реализован в текущем раунде):** при `FileNotFoundError` от `resolve_source` — тихо работаем от БД без парсинга, стадия `выгрузка (нет файла — работаю от базы)`. Не ломает чистый клон.
- `deep=True` — вызывает `ml.build_features`, `ml.build_labels` (стандартная библиотека), затем — если есть sklearn — стадию **`скор`**: `score.sync` — сев при пустом ledger'е (`score.replay`), иначе запись последнего дня (`score.record`). Стадия нужна именно в daily-шаге: запись в тот же день, когда исход ещё не известен (см. `score.py`).
- `_freshness(conn)` — MAX(date/start) по (`aw_daily`, `load_daily`, `health_days`, `ml_features`, **`ml_score`**, `illness_episodes`).
- `format_freshness(step)` — итог: звенья строкой + свежесть **таблицей** «таблица | дата» (через `health.text.table`), чтобы отставание виделось построчно.

### `koilog/health/ml.py` — ML
- `build_features(conn)`, `build_labels(conn)` — формируют `ml_features`, `ml_labels`.
- Метки: `ml_labels` (date,y,y_onset,episode_id,onset). `lead_days = onset_date - day`. `y_onset` в горизонте H_ONSET (~7). Эпизод дня назначается по **первому onset строго после дня** (bisect_right) → каждый день имеет ровно один `episode_id`. Окна разных эпизодов **не пересекаются** (нет двойного счёта поимок).
- Дни эпизода (`at_risk=0`) **исключаются вовсе**: не идут ни в обучающую историю (`ml.py:964` `history = [i for i in range(t) if at_risk[i]]`), ни в скоринг (`ml.py:962` `if not at_risk[t]: continue`). Это **не** «больные дни как негативы» (проверено C12: 98 дней `at_risk=0`, из них 39 с `y_onset=1`). Не менять осознанно, с перегонкой.
- `report(conn, ...)` — строит walk-forward **в четырёх вариантах**: `walk_v2`/`walk_baseline` (причинные — **главная строка**), `walk_v2_oracle`/`walk_baseline_oracle` (оракульные — **верхняя граница**); все отличаются только правилом меток. Событийные метрики (catchable/nowcastable, lead), bootstrap, `recall_delta` (парный bootstrap по уловимым эпизодам), `precision` (день-уровневая, точечная), страты. Две бухгалтерии: потолок (включая burn-in) vs catchable (без burn-in) — держать явным в README (C8).
- `walk_forward(conn, features, model, causal=False)` — единый цикл; `causal=True` берёт причинные метки, `causal=False` — оракульные. **Оба отличаются только правилом меток** (общий `_predict`, история, бюджеты, seed). `ORACLE_LABEL_RULE = "oracle"`; `catchable_order`/`catchable_hits`/`per_catchable` — событийная бухгалтерия в бюджетных строках.
- `relapse_strata(conn)` / `stratified_recall(walk, strata)` / `_strata_difference` — стратификация `relapse` (onset в пределах `RELAPSE_GAP_DAYS = 14` от предыдущего = граница кластера D10) vs `de novo`; `MIN_STRATUM = 3`. Страта **оценочная**, в скоринге не участвует. Вывод прогона: **не различимо** («ML ловит de novo» не подтвердилось).
- `walk_probs(conn, features, model, min_history=MIN_HISTORY)` → `(dates, probs)` и внутренний `_walk_probs_data` — **оракульный** цикл walk-forward (`walk_forward`). `MIN_HISTORY = 30`. Общий шаг обучения — `_predict` (им же пользуется причинный скор). Refactor проверяется числовыми тестами `walk_forward`.
- **Причинный скор (D12):** `_causal_train(dates, at_risk, starts, t, horizon)` — метки, известные к eval-дню: 1, если onset в окне `(i, i+H_ONSET]` уже случился к `T`; 0, если окно закрылось; день **исключается**, если окно открыто (не обнуляется!). `_causal_score_at`, `causal_score(conn, day, ...)` (один день), `causal_scores(conn, ...)` (все дни за проход, для сева ledger'а). `CAUSAL_LABEL_RULE = "causal-v1"`. Это то, что идёт в ledger; `walk_probs` остаётся верхней границей.
- **Пересчёт скора дня НЕ идемпотентен** (оракульно): новый эпизод меняет обучающую историю последних `H_ONSET` дней (замер 30.06: оракул 0.3385 vs причинный 0.2397). Причинный скор от будущего не зависит: обрезка базы по `as_of` даёт то же число (регресс-тест). Поэтому `ml_score` — append-only ledger (`score.py`).

### `koilog/health/retrain.py` — возврат к весам, гейт (A1/D10)
- `baseline_gate(conn, today=None, nights=GATE_NIGHTS, silence_days=GATE_SILENCE_DAYS)` — «можно ли возвращаться». Проверка **односторонняя**: держат `rhr_z > +GATE_Z` и `hrv_z < −GATE_Z` две ночи подряд (`nights=2`), t запястья `z > GATE_Z_TEMP`, активный эпизод. Низкий RHR и высокая ВСР **не** держат (раньше `abs()` держал «отдохнул» наравне с «заболевает» — это и был артефакт FP).
- **Окно тишины** `GATE_SILENCE_DAYS = 14`: `end < day <= end+14` после эпизода — гейт не готов с причиной «окно тишины» (высокая ВСР после болезни = отскок, не допуск). `BaselineGate.window_only` = блокирует **только** окно (каналы в фоне) → `digest.recommend` отдаёт «рампа», не «покой». Логика уровня гейта; `ml_features`/`ml_labels`/AUC не трогаются.
- Числа D10 (правило записано до прогона; полная пререгистрация — `docs/d10-retro-test.md`): односторонний без окна освобождает 292 дня, из них **28 pre-relapse** (lead 1–7 до внутрикластерного рецидива) → отклонён; окно 7/10 → 2 pre-relapse; окно **14** → 0 при цене 47 дней (16 % от 292); N = 14 = граница кластера (onset'ы ≤ 14 дней = один кластер). Живой кейс 17–19.10.2026: 18.10 каналы в фоне (z RHR −0.7, ВСР −0.3), а 19.10 начался рецидив (RHR z +4.4, ВСР z +4.5). Перепроверка «294» из прежней прикидки → теперь 292.
- `format_gate(gate)` печатает таблицу ночей + причины; `return_plan` собирает рампу **независимо** от `gate.ready` (гейт — только информация, поэтому «рампа» в окне не противоречит).

### `koilog/health/digest.py` — дайджест и рекомендация

- Порядок рекомендации: красный флаг → **окно тишины (ramp)** → покой (активный эпизод или гейт не пройден) → «завтра легко» → рампа (простой) → «можно по плану». Ветка окна читает `getattr(gate, "window_only", False)` (заглушки в тестах могут не иметь поля).
- `recommend(channels, load, gate, score=None)` — канонический скор дня (`{"score", "threshold", "alert"}`) не переопределяет решение, а **объясняет** его (добавляет «Скор дня … — модель в фоне / выше порога бюджета»). В тексте «покой» прямо сказано, что гейт ±1 строже колонки «откл.» ±1.5 — это снимает мнимое противоречие «все каналы в фоне / покой» (правка №3).
- `digest` **не импортирует** `ml`/`score`: канонический скор читает `_score_row(conn, day)` из `ml_score` по SQL (иначе кольцо: `score` зовёт `recommend`). `_query(conn, sql, params=())`. `day_state` кладёт `score` (или `None`) и `spec_warning`, `format_digest` печатает строки «гейт возврата» и «скор (причинный[ за дату])».
- **Строка скора — тихий мониторинг** (после causal replay): `скор (причинный): 0.70 — мониторинг (порог 0.90)`. Без решающей интонации, порог в строке — только непрерывность ряда. Алертная ветка: `выше порога 0.90 (алерт)`; холодный старт: `порог ещё не накоплен`.
- `spec_warning(conn, current=None)` — **чистый SQL** (без импорта `ml`/`score`, слои не протекают): предупреждает, если канонический `spec_hash` дня не совпадает с `current` (вызывающий подставляет, напр. `score.spec_hash()`). `day_state(..., spec=)` кладёт в `state["spec_warning"]`, `format_digest` печатает строку `внимание: …`.

### `koilog/health/state.py` — state-машина «перерыв → рампа → штатный» (раунд 5+5a + 6, **сделано**)

- Пререгистрация — `docs/state-machine-spec.md` (решения O1–O3 = **A/A/A**, §10;
  sick-tail — §5-бис).
- `state_day(conn, day) -> StateDay` — **единственный источник правды**: состояние
  (`sick`/`rest`/`ramp`/`normal`), причина, `k` (выполненные дни рампы из
  `RAMP_DAYS = 5`), цель рампы. `digest.recommend` **рендерит** его, не собирает
  решение само (правила — в `state.decide`, куда идёт и прямой вызов
  `recommend(channels, load, gate)` для совместимости).
- `RAMP_DAYS = 5` (выполненных дней), `RAMP_GAP_DAYS = 7` (простой → рампа),
  `SICK_TAIL_MAX = 7` (предохранитель обратного скана) — константы в `state.py`.
- `k` = число **тренировочных** дней из `sets` в `(anchor, day]`, где `anchor` =
  последний `sick`-день (разметка **или** хвост, `_last_sick_day`), иначе конец
  последнего завершённого эпизода (не `gap_only`), иначе последняя тренировка.
  `sick` = сброс (anchor сдвигается на новый эпизод); `rest` = пауза
  (`ramp_paused`, счётчик сохраняется). Приоритет `sick > rest > ramp > normal`;
  выход `ramp → normal` — `k >= RAMP_DAYS` **и** `gate.ready`.
- **Sick-tail (раунд 6):** `_gate_status(gate)` → `"holds"`/`"unknown"`/`"clean"`
  по причинам гейта (канальные `RHR z`/`ВСР z`/`t запястья z` = holds; `нет фона`/
  `не хватает ночей`/`нет данных` = unknown; только окно тишины = clean).
  `_sick_tail(conn, day, gate)` — обратный скан (stateless) до `SICK_TAIL_MAX`;
  стоп на первом чистом дне; если в интервале есть `sick`-якорь
  (`_is_sick_anchor` = активный эпизод ∨ красный флаг) — день `sick`. Хвост
  только **продлевает** `sick`; незаякоренный дериваж → `rest`. Дополнение к
  спеке §5-бис (20.10.2026): живой кейс эп-34 ревизован выгрузкой, держится
  синтетическим тестом.
- Целевой вес — floor от 1RM до болезни: `retrain.RampPlan.rule = "ramp_floor"`
  (структурное поле O2), `core.snap_to_ladder` (ближайший не тяжелее, 42 ⇒ 40);
  `state.ramp_target` оборачивает `retrain.return_plan`. `next_weight` — пост-рампа.
- Пустая база (нет `aw_daily`/`load_daily`) → `normal`, причина «нет данных»
  (рампа из пустоты не выдумывается).
- Рендер паузы (§5.3): `digest._state_line`/`_ramp_progress` показывают
  «(рампа приостановлена)» и для `rest`, если рампа уже начата (`ramp_day > 0`).

### `koilog/health/score.py` — ledger дневного скора (D12 + A3)
- Ledger `ml_score(id PK AUTOINCREMENT, day, as_of, computed_at, score, spec_hash, n_train, n_pos, last_train_day, threshold, alert)`. **Append-only**: только INSERT, каноническое значение дня — последняя строка (`MAX(id)`). `_ensure_schema` сносит дорелизную схему без `spec_hash`.
- `spec_hash(features, model, min_history, horizon, label_rule)` — версия рецепта (sha1, 12 hex). Смена рецепта = новый hash; порог считается только внутри одного рецепта.
- `spec_warning(conn, current=None)` — обёртка: считает текущий `spec_hash()` и предупреждает о примеси винтажей в ledger'е (в `__all__`; CLI зовёт в `cmd_score`/`cmd_digest`).
- `record(conn, ...)` — единственный путь записи дня: **только последний день** (`ml.causal_score`), порог из `threshold`, INSERT. Если день внутри эпизода / мало истории — строки нет (`reason`).
- `replay(conn, since=None, ...)` — холодный старт: `ml.causal_scores` по всей истории, добавляет недостающие дни (повторный replay ничего не дублирует). `sync(conn, ...)` — шаг refresh: сев если ledger пуст по этому `spec_hash`, иначе `record`.
- `threshold(conn, spec, before_day, budget=DEFAULT_BUDGET(2.0))` — **верхний** квантиль канонических скоров `< before_day`: уровень `q = 1 − min(0.5, budget/30.44)` (доля алертных дней ≈ бюджет/мес, кап на долю 0.5), порог `= quantile(history, q)`. При бюджете 2 → `q ≈ 0.934`, на живой истории порог ≈ 0.90 (алертят только самые высокие скоры). `< MIN_HISTORY` → `None`. Порог считается **только внутри одного `spec_hash`** и только по дням строго раньше целевого.
- `audit(conn, day=None, ...)` — пересчёт дня сейчас, без записи (гейт + рекомендация через `digest.recommend`); `format_day` — строка дня.
- **Проспективность**: `computed_at < min(onset, day+H_ONSET)` (onset строго после дня); сев пишет историю позже исходов → 0 проспективных. Помечается `*` в `format_scores`.
- `_ready(conn)` — понятная ошибка «нет ml_features/ml_labels — сначала `refresh --deep`» вместо `sqlite3.OperationalError` (её ловит `cmd_score`, печатает в stderr, код 1).
- **Воспроизводимость — регресс-тест**: скор дня `T` не зависит от данных после `T`; обрезка базы по `as_of` даёт то же число (`tests/test_health_score.py`).
- Стоимость на живых данных: сев всей истории (`replay`) ~9.5 с разово; живая запись дня — одна тренировка, ~0.1 с.

## 3. ETL/Схемы

- `koilog/etl.py` — базовые таблицы (`sets`, представления WEEKLY/SESSION), `connect()`, миграции колонок.
- `koilog/health/*` — свои `_SCHEMA` (aw_*, load_daily/health_days/test_days, illness_*, ml_*).

## 4. CLI
- `koilog/cli.py` — команды: `load`, `apple-load`, `health-load`, `illness`, `ml`, `report`, `refresh` (флаги `--deep`, `--xml`, `--journal`, `--no-journal`), `digest` (`--date`, `--json`), `score` (`--date`, `--replay`, `--since`, `--last`, `--prospective`, `--model`, `--budget`, `--json`). `cmd_refresh` вызывает `health_refresh.run`. `--xml` задаёт источник часов, `--journal` — путь к журналу (`--no-journal` — не грузить); слепки (`refresh_source`, `refresh_journal`) действуют и для них (загрузка/разбор только при изменении файла).
- `cmd_score`: без флагов — `sync` (сев при пустом ledger'е, иначе запись дня), печатает последние дни; `--replay` — сев истории; `--date` — `audit` (пересчёт без записи). `RuntimeError`/`sqlite3.OperationalError` (нет ML-таблиц / нет extra ml) ловятся → сообщение в stderr, код 1 (без трассировки).
- `illness --detail ID` — печатает таблицу «канал | min | mean | max | пик (дата)» для одного эпизода (`analysis.format_episode_channels`), пометки «выше/ниже фона» текстом. Без `--detail` — сводка эпизодов.

## 5. Ноутбуки
- `koilog.ipynb`, `koilog-health.ipynb` — ячейка 2 вызывает `refresh.run`, печатает свежесть до работы. Health-ноутбук пересобирается внутренним драйвером (nbclient не требуется).
- Драйвер `tools/nb_run.py` (**в репо**): пишет JSON как Jupyter (`text` — **список строк**, `image/png` — строкой), `execution_count` = порядковый номер code-ячейки (а не номер в текущем прогоне), фигуры — через `savefig` в dpi фигуры. Третий аргумент — ячейки-**прелюдии** (напр. 12), которые выполняются ради `rcParams`, но их сохранённый вывод не переписывается. Глушит warning Agg (`non-interactive`). Корень репозитория — от расположения файла (override: env `KOILOG_ROOT`), абсолютных путей нет.

## 6. Приоритет порядка (актуальный)

Порядок: **7+8 → C10+C11 → A1 с D10 внутри → D12 + урезанный A3 → causal replay → 5(+5a) → 6 → D11 → E → 2/4 по мере сил**. D10 не откладывать (уточняет A1). D12 + урезанный A3 вытянуты вперёд (маленькая правка + ценность проспективных данных). 5 раньше E (чинит известные артефакты FP в перерывах, gap). **D11 перед E** (определения раньше аналитики; закрывает висячую ссылку §9 спеки; защищает физиология-first от утечки тоннажа в решения). E2: веса участия — справочные константы, держать устойчивыми (±20% допустимы).

**Гигиена статусов:** статус «Готово» снабжать **хэшем коммита-доказательства** (прецедент: пометка «D11 готов» была ошибочной — третий случай расхождения карты с кодом). Статус без хэша — не подтверждён.

Сделано: 7+8, C10+C11, **A1+D10** (`46d9465`), **D12 + урезанный A3** (`cf59c55`), **causal replay** (`c513fc7`), **спека state-машины 5(+5a)** (`d1829d8`), **карта + драйвер** (`408e1e8`), **реализация 5+5a** (`25d075f`), **конвейер** (`63da8c0`), **раунд 6 — sick-tail** (`180222b`), **арифметика константы** (`aea7f39`), **D11-пререгистрация** (`0a38df8`), **решение D11 + E-пререгистрация** (`d44164f`).

- **D12 + урезанный A3** — append-only ledger `ml_score` (перезаписи нет как операции, канонический скор = `MAX(id)`), причинное правило меток (`CAUSAL_LABEL_RULE = "causal-v1"`, открытое окно — **исключение**, не 0), строка скора/гейта в карточке; `docs/d12-score-snapshots.md`. Порог берётся из канонической истории ledger'а. Первый сев `replay` — 880 дней, 0 проспективных, один `spec_hash`. Пререгистрация честной оценки и правило главной строки — `docs/causal-replay-spec.md`.
- **causal replay** — `walk_forward(causal=True)`, отчёт в 4 вариантах (V2/baseline × causal/oracle), `relapse_strata`/`MIN_STRATUM`, `recall_delta`/`precision`, `spec_warning` в `digest`/`score`. Прогон на боевой базе (880 дней): AUC causal 0.529 [0.433, 0.641] vs oracle 0.663 [0.584, 0.752]; **при бюджете 2/мес recall(catchable) 0.000 и nowcast 0.000**. Мёртв **весь** ML, включая физиологический бейзлайн (causal AUC 0.5004) — прежний сигнал был в основном взглядом в будущее. Страты relapse/de novo **не различимы**. Вывод: **продукт = гейт возврата + окно тишины + рампа + дайджест, скор — тихий мониторинг**. Это третий честный минус проекта.
- **Спека state-машины** (`docs/state-machine-spec.md`) — переходы «перерыв → рампа → штатный», решения O1–O3 = **A/A/A** (рампа 5 **выполненных** дней; вес — floor структурным полем `rule="ramp_floor"`; «день k из N» с паузой без сброса).
- **Реализация 5+5a** — `koilog/health/state.py` (`state_day` — единственный источник правды; `decide`; `ramp_target`; `RAMP_DAYS`/`RAMP_GAP_DAYS`), `digest.recommend` рендерит состояние (строка «состояние», скор по состояниям), `retrain.RampPlan.rule = "ramp_floor"`, тесты переходов `tests/test_health_state.py`. Скор — тихий мониторинг, состояние не переопределяет.
- **Раунд 6 (п.1, `b01bc09`)** — **4.7** детрейн-бакеты (`retrain._BINS`, 5 бакетов, `iqr`, display-only) + **4.6** полоса гейта (`digest._gate_band`, render-only по `gate.rows`/`reasons`). 733 OK. **П.2 (`e72fd7f`)** — **4.4** каналы эпизода (`analysis.episode_channels`/`format_episode_channels`) + `illness --detail`. 742 OK. Фон = `signature` «обычные» (≈54.6 мс), `mean vs bg`; выше фона у **2/19** confirmed (у **10/56** по всем уровням — post-hoc, статмотив для 4.1). Урок E4 (без стрелок/порогов в рендере) — исполняемый тест.
- **Раунд 6 — sick-tail** — `state.SICK_TAIL_MAX = 7`, `_gate_status`/`_sick_tail`/`_is_sick_anchor`/`_last_sick_day`; `sick` снимается только чистым гейтом, хвост продлевает, незаякоренный дериваж → `rest`, якорь рампы после `sick` = последний `sick`-день. Рендер паузы (§5.3) в `digest._state_line`/`_ramp_progress` для `rest`. Тесты: `tests/test_health_state.py` (+7 `SickTailTests`), `tests/test_health_digest.py` (+2). **Ревизия данных:** выгрузка 20.10 уточнила ночь 19.10 (RHR 73/ВСР 65), эп-34 снят разметкой — кейс держится синтетической репликой; дополнение к спеке §5-бис от 20.10.2026 (append-only). README: nowcast-раздел переписан под причинный итог (nowcast 0.000), добавлен sick-tail.

Дальше — **E1–E4** (пререгистрация `docs/e-groups-spec.md`; до кода) + обязательный тест-инвариант D11-b в коммит E, затем 2–4. **D11 закрыт** (решение **a = E, b = A**, спека `docs/d11-load-input.md` §9): нагрузка = фичи/мониторинг, не вход решений; ключ режима — колонка таксономии (`taxonomy.group_of`), не эвристика по названию; `ml_features` не трогается; ACWR не начинается без источника sRPE (тоннажный «прокси» запрещён впрок). Открытые развилки E (требуют решения до кода): E2-a «ноль vs отсутствие» на неделе (реком. A: `None`+`n_days`), E2-b окно (реком. rolling 7), E3-a порог истории для ratio (реком. N=3), E4-a модуль (реком. `health/groups.py`). Ветка E не начинается, пока развилки не закрыты. `63da8c0` (конвейер журнала) **не менял исторические значения**: 10 дней «журнал 31 vs `sets` 30» — пустые строки-заметки (`;;;;;;;;"🙂"`), штатно отбрасываемые ETL и до фикса; 454 дня «только в `sets`» — 2015–2021 из старой версии файла; max `sets` = max журнала = 2026-10-19. Версия ряда не менялась, changelog не нужен. Мониторинг ledger'а продолжается (окно тишины эп-33 до 2026-10-27); рампа 27.10 — по текущему протоколу лестницы.

- **Поправка отчётности (2026-10-22, G-3) — счётчики тестов и реатрибуция восьмёрки фейлов.** Датированный append-only: записи выше **не переписываются**; здесь фиксируются их верная речь и новые измерения.
  - **Исправляются три записи** (журнал отчётности раунда 4.1 и статусные строки §6; в коде и в git их нет):
    1. **«759 OK»** — неверно: боевой прогон после 4.1 был `FAILED (failures=8)`, а не `OK`. Число 759 верное (столько тестов собирается и сейчас), слово «OK» — нет.
    2. **«742 OK (skipped=1)»** (статус 4.4 в этой же строке §6 и сообщение коммита `e72fd7f`: «Арифметика: 733 → 742 OK (skipped=1)») — неверно по конвенции unittest: футер состоит из двух строк — «Ran 742 tests in …s» и «OK (skipped=1)», — то есть **741 тест прошёл, 1 пропущен**; «742 OK» читается как «742 прошло и ещё 1 пропущен» = 743.
    3. **Классификация «environmental — правдоподобен»** — снята: это не «environmental» (пересборка живой базы через `illness.build`), а прямой отпечаток инцидента с усечённым файлом журнала.
  - **Новая атрибуция восьмёрки фейлов — по фикстурам и механизму.** `RealDataTests.setUpClass` грузит `data/journal.csv` в **временную** БД (`tempfile.TemporaryDirectory`); живая БД и `illness.build` на эти числа не влияют. Фикстуры фейлов прямо показывают состояние файла в момент прогона: `test_eleven_overlapping_dates_skipped` → `0 != 11` (пересечение журнала с отчётом пусто ⇒ в журнале не было ни одной даты 2015–2021); `test_2019_2020_gap_kept_as_is` → «0 not greater than 0» (за 2019 ноль подходов в обоих источниках); `test_row_counts` → ≠ 15428; `test_history_is_contiguous` → ≠ 2026-10-19; `test_taxonomy.CoverageTests.test_no_unused_entries` → справочник не покрыт урезанным набором упражнений. Полный файл даёт 11 пропущенных дат и 15451 подход — то есть фейлы снялись сменой файла, а не кодом. **Класс «environmental» опровергнут**, парк-пункт «environmental — докажи прогоном» снят: доказывать нечего.
  - **Счётчики: что на самом деле (измерено 2026-10-22 в worktree, `suite.countTestCases()`).** `3fecfc4` → 725, `b01bc09` → 733, `e72fd7f` → 742, `68888cb` → 742 (docs-only), `2759a03` → 742 (docs-only), `ac02233` → **742 (тестов не добавил)**, `16ed0ea` → 759 (+17), `ae5f48e` (HEAD) → 759. Значит запись «Ran 737 base + 22 additions = 759» — арифметика неверная: верно **742 + 17 = 759**, и **737 не воспроизводится ни в одном окружении** на `68888cb` (там «Ran N» = 739 или 742, см. ниже).
  - **«Ran N» зависит от окружения, а не от коммита.** В worktree без `data/` и `sql/` класс `RealProgramInventoryTests` (`tests/test_stats.py`) снимается в `setUpClass` через `raise unittest.SkipTest`, и его 3 метода в «Ran N» не попадают: 742 → 739. Worktree-формулировка: **31 real-data skip + 1 numpy** (19 «нет data/journal.csv и/или data/report.csv» + 9 «нет data/journal.csv» + 2 «нет data/report.csv» + 1 «нет базы …\sql\training.db: выполните load» = 31; плюс 1 «sklearn есть: подсказка про extra ml не проверится»).
  - **Оба футера дословно.** Worktree @`68888cb` (нет `data/`, нет `sql/`):
    ```
    Ran 739 tests in 11.206s
    OK (skipped=32)
    ```
    Рабочий каталог, HEAD `ae5f48e` (с `data/` и `sql/`):
    ```
    Ran 759 tests in 142.002s
    OK (skipped=1)
    ```
    Контроль — рабочий каталог @`68888cb` с текущими файлами данных: `Ran 742 tests in 148.764s` / `FAILED (failures=4, skipped=1)` — те же 4 устаревших ожидания, что были на HEAD до `ae5f48e`.
  - **grep-охват.** Записи, которые эта поправка закрывает, ищутся по: «742», «759 OK», «environmental», «illness.build».
  - **Связи.** `ae5f48e` — коммит R-1a (освежение ожиданий по журналу; правки только в `tests/test_etl.py`, датированный комментарий «as-of 2026-10-22, заменяется live-data тиром (W7)»). Пост-мортем инцидента с журналом — `C:\Users\Admin\.opencode\plan\BB-round6.md` §10 (вне git; в нём оба хэша, таймлайн и реестр улик, хранение до закрытия W7).
  - **N-3 (новое, кода нет).** Сообщение коммита `ac02233` заявляет `tests/test_health_score.py: VintageTests` (мутация тега → другой `spec_hash`, равенство `c0c625dc1d16` старому тегу, чтение строк под винтажами, oracle не в ledger) — **в коммите их нет**: `ac02233` трогает только `koilog/health/ml.py` и `koilog/health/score.py`, а литерала `c0c625dc1d16` в `tests/` нет ни в одном коммите истории. Это же объясняет «+22» вместо «+17». Зарегистрировано, решение — отдельной правкой после отдельной пререга; здесь не решается.

## 7. Окружение разработки (не поведение репозитория)

*Заметки выше — про локальный инструментарий/машину, а не про поведение кода репозитория.*

- **Скрипты `python script.py` виснут** на чтении файлов BB при импорте (faulthandler показывает: `importlib get_data`), а `python -c "..."` и `python -m ...` работают мгновенно. Обход: запускать через
  `python -c "exec(open(r'...\\script.py', encoding='utf-8').read())"`,
  а для драйвера ноутбука — `python tools/nb_run.py <nb> <cells>` (или через `exec(open(...))`, если `python script.py` виснет).
- После таймаута команды остаются **зависшие python-процессы** (висят на чтении/записи, держат файлы). Убивать по командной строке: `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` → `Stop-Process -Id`. Заблокированный файл удалять через `python -c "os.remove(...)"`.
- PowerShell: нет `head`/`tail`/`&&`; heredoc не работает. Вывод фильтровать `Select-Object -Last N` / `Select-String`.
- Не читать в скрипте `koilog-health.ipynb` из BB напрямую: копировать в temp и читать оттуда (то же — через `python -c`).
- Ноутбуки: `nb_run.py` (в temp) прогоняет выбранные code-ячейки и пишет JSON как Jupyter (`indent=1, ensure_ascii=False`, хвостовой `\n`; `text` — список строк). `execution_count` = порядковый номер code-ячейки, поэтому перезапуск одной ячейки 2 сохраняет `execution_count=1`. Есть прелюдия-ячейки (3-й аргумент) для `rcParams`.
