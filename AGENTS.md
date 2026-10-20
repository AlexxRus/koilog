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

## 5. Ноутбуки
- `koilog.ipynb`, `koilog-health.ipynb` — ячейка 2 вызывает `refresh.run`, печатает свежесть до работы. Health-ноутбук пересобирается внутренним драйвером (nbclient не требуется).
- Драйвер `tools/nb_run.py` (**в репо**): пишет JSON как Jupyter (`text` — **список строк**, `image/png` — строкой), `execution_count` = порядковый номер code-ячейки (а не номер в текущем прогоне), фигуры — через `savefig` в dpi фигуры. Третий аргумент — ячейки-**прелюдии** (напр. 12), которые выполняются ради `rcParams`, но их сохранённый вывод не переписывается. Глушит warning Agg (`non-interactive`). Корень репозитория — от расположения файла (override: env `KOILOG_ROOT`), абсолютных путей нет.

## 6. Приоритет порядка (актуальный)
Согласованный: **7+8 → C10+C11 → A1 с D10 внутри → D12 + урезанный A3 → causal replay → 5(+5a) → 6 → E → 2/4 по мере сил**. D10 не откладывать (уточняет A1). D12 + урезанный A3 вытянуты вперёд (маленькая правка + ценность проспективных данных). 5 раньше E (чинит известные артефакты FP в перерывах, gap). E2: веса участия — справочные константы, держать устойчивыми (±20% допустимы).

Сделано: 7+8, C10+C11, **A1+D10** (`46d9465`), **D12 + урезанный A3** (`cf59c55`), **causal replay** (`c513fc7`), **спека state-машины 5(+5a)** (`d1829d8`), **реализация 5+5a** (`state.py` + floor-поле), **конвейер** (`63da8c0`), **раунд 6 — sick-tail** (`state.py` + рендер паузы + README/спека, этот раунд).

- **D12 + урезанный A3** — append-only ledger `ml_score` (перезаписи нет как операции, канонический скор = `MAX(id)`), причинное правило меток (`CAUSAL_LABEL_RULE = "causal-v1"`, открытое окно — **исключение**, не 0), строка скора/гейта в карточке; `docs/d12-score-snapshots.md`. Порог берётся из канонической истории ledger'а. Первый сев `replay` — 880 дней, 0 проспективных, один `spec_hash`. Пререгистрация честной оценки и правило главной строки — `docs/causal-replay-spec.md`.
- **causal replay** — `walk_forward(causal=True)`, отчёт в 4 вариантах (V2/baseline × causal/oracle), `relapse_strata`/`MIN_STRATUM`, `recall_delta`/`precision`, `spec_warning` в `digest`/`score`. Прогон на боевой базе (880 дней): AUC causal 0.529 [0.433, 0.641] vs oracle 0.663 [0.584, 0.752]; **при бюджете 2/мес recall(catchable) 0.000 и nowcast 0.000**. Мёртв **весь** ML, включая физиологический бейзлайн (causal AUC 0.5004) — прежний сигнал был в основном взглядом в будущее. Страты relapse/de novo **не различимы**. Вывод: **продукт = гейт возврата + окно тишины + рампа + дайджест, скор — тихий мониторинг**. Это третий честный минус проекта.
- **Спека state-машины** (`docs/state-machine-spec.md`) — переходы «перерыв → рампа → штатный», решения O1–O3 = **A/A/A** (рампа 5 **выполненных** дней; вес — floor структурным полем `rule="ramp_floor"`; «день k из N» с паузой без сброса).
- **Реализация 5+5a** — `koilog/health/state.py` (`state_day` — единственный источник правды; `decide`; `ramp_target`; `RAMP_DAYS`/`RAMP_GAP_DAYS`), `digest.recommend` рендерит состояние (строка «состояние», скор по состояниям), `retrain.RampPlan.rule = "ramp_floor"`, тесты переходов `tests/test_health_state.py`. Скор — тихий мониторинг, состояние не переопределяет.
- **Раунд 6 — sick-tail** — `state.SICK_TAIL_MAX = 7`, `_gate_status`/`_sick_tail`/`_is_sick_anchor`/`_last_sick_day`; `sick` снимается только чистым гейтом, хвост продлевает, незаякоренный дериваж → `rest`, якорь рампы после `sick` = последний `sick`-день. Рендер паузы (§5.3) в `digest._state_line`/`_ramp_progress` для `rest`. Тесты: `tests/test_health_state.py` (+7 `SickTailTests`), `tests/test_health_digest.py` (+2). **Ревизия данных:** выгрузка 20.10 уточнила ночь 19.10 (RHR 73/ВСР 65), эп-34 снят разметкой — кейс держится синтетической репликой; дополнение к спеке §5-бис от 20.10.2026 (append-only). README: nowcast-раздел переписан под причинный итог (nowcast 0.000), добавлен sick-tail.

Дальше — ветка **E**, затем 2–4. **D11** (тоннаж для атрибуции/Round 4) — **не начат**: в репозитории нет ни спеки, ни кода, ни черновика (есть только тоннаж как *вход* нагрузки: `load_daily.tonnage`/`weekly_tonnage`, и ссылка в спеке §8 «другие ветки»). Пометка «готов к просмотру» в прежней редакции этой строки была **ошибочной**. Варианты: либо перевести D11 в пререгистрацию (спека до кода, образец D10/D12), либо идти в ветку E, как планировалось. Мониторинг ledger'а продолжается (окно тишины эп-33 до 2026-10-27).

## 7. Окружение разработки (не поведение репозитория)

*Заметки выше — про локальный инструментарий/машину, а не про поведение кода репозитория.*

- **Скрипты `python script.py` виснут** на чтении файлов BB при импорте (faulthandler показывает: `importlib get_data`), а `python -c "..."` и `python -m ...` работают мгновенно. Обход: запускать через
  `python -c "exec(open(r'...\\script.py', encoding='utf-8').read())"`,
  а для драйвера ноутбука — `python tools/nb_run.py <nb> <cells>` (или через `exec(open(...))`, если `python script.py` виснет).
- После таймаута команды остаются **зависшие python-процессы** (висят на чтении/записи, держат файлы). Убивать по командной строке: `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` → `Stop-Process -Id`. Заблокированный файл удалять через `python -c "os.remove(...)"`.
- PowerShell: нет `head`/`tail`/`&&`; heredoc не работает. Вывод фильтровать `Select-Object -Last N` / `Select-String`.
- Не читать в скрипте `koilog-health.ipynb` из BB напрямую: копировать в temp и читать оттуда (то же — через `python -c`).
- Ноутбуки: `nb_run.py` (в temp) прогоняет выбранные code-ячейки и пишет JSON как Jupyter (`indent=1, ensure_ascii=False`, хвостовой `\n`; `text` — список строк). `execution_count` = порядковый номер code-ячейки, поэтому перезапуск одной ячейки 2 сохраняет `execution_count=1`. Есть прелюдия-ячейки (3-й аргумент) для `rcParams`.
