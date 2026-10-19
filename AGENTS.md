# AGENT NOTES — карта кода (обновляется при каждом раунде)

Короткие пометки: где искать логику, какие функции использовать (не дублировать).

## 1. Тренировки, прогрессия, веса, 1RM — `koilog/training.py`

Использовать **существующую** логику (особенно для рампы 5a), не изобретать новую.

- `rm_estimate(weight, reps, formula="epley")` — оценка 1RM. Основа для «дошёл до перехода по 1RM».
- `rm_best_from_sets(sets)` — лучший 1RM по набору сетов (используется в расчётах RM).
- Весовой стек и округление до доступных весов (гантели/штанга): функции, которые приводят вес к ближайшему в стеке оборудования (учёт шагов грифа/гантелей). Искать по именам: `round_to_available*`, `available_weights*`, `get_available_weights*`, `nearest*weight`.
- Критерий перехода на следующий вес в стеке (когда результат позволяет повысить вес): `suggest_next*`, `next_weight*`, `progress*weight*`, `can_progress*`. Именно их вызывать при определении целевого веса в фазе «рампа» (п.5а бэклога).

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
- `run(conn, deep=False, source=None)` — конвейер: **выгрузка → нагрузка → календарь → эпизоды → (ML при deep)**.
- Парсинг XML (~45с) **скипается** по слепку: `_need_parse` сравнивает `mtime:size` источника с `aw_meta.refresh_source` (`REFRESH_KEY`). **Слепок пишется ТОЛЬКО после успешной записи** `aw_*` (если разбор упал — следующий прогон попробует снова).
- **Guard (реализован в текущем раунде):** при `FileNotFoundError` от `resolve_source` — тихо работаем от БД без парсинга, стадия `выгрузка (нет файла — работаю от базы)`. Не ломает чистый клон.
- `deep=True` — вызывает `ml.build_features`, `ml.build_labels` (стандартная библиотека), затем — если есть sklearn — стадию **`скор`**: `score.sync` — сев при пустом ledger'е (`score.replay`), иначе запись последнего дня (`score.record`). Стадия нужна именно в daily-шаге: запись в тот же день, когда исход ещё не известен (см. `score.py`).
- `_freshness(conn)` — MAX(date/start) по (`aw_daily`, `load_daily`, `health_days`, `ml_features`, **`ml_score`**, `illness_episodes`).
- `format_freshness(step)` — итог: звенья строкой + свежесть **таблицей** «таблица | дата» (через `health.text.table`), чтобы отставание виделось построчно.

### `koilog/health/ml.py` — ML
- `build_features(conn)`, `build_labels(conn)` — формируют `ml_features`, `ml_labels`.
- Метки: `ml_labels` (date,y,y_onset,episode_id,onset). `lead_days = onset_date - day`. `y_onset` в горизонте H_ONSET (~7). Эпизод дня назначается по **первому onset строго после дня** (bisect_right) → каждый день имеет ровно один `episode_id`. Окна разных эпизодов **не пересекаются** (нет двойного счёта поимок).
- Дни эпизода (`at_risk=0`) **исключаются вовсе**: не идут ни в обучающую историю (`ml.py:964` `history = [i for i in range(t) if at_risk[i]]`), ни в скоринг (`ml.py:962` `if not at_risk[t]: continue`). Это **не** «больные дни как негативы» (проверено C12: 98 дней `at_risk=0`, из них 39 с `y_onset=1`). Не менять осознанно, с перегонкой.
- `report(conn, ...)` — строит walk-forward v2 (`walk_v2`), событийные метрики (catchable/nowcastable, lead), bootstrap. Две бухгалтерии: потолок (включая burn-in) vs catchable (без burn-in) — держать явным в README (C8).
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
- `digest` **не импортирует** `ml`/`score`: канонический скор читает `_score_row(conn, day)` из `ml_score` по SQL (иначе кольцо: `score` зовёт `recommend`). `_query(conn, sql, params=())`. `day_state` кладёт `score` (или `None`), `format_digest` печатает строки «гейт возврата» и «скор (причинный[ за дату])».

### `koilog/health/score.py` — ledger дневного скора (D12 + A3)
- Ledger `ml_score(id PK AUTOINCREMENT, day, as_of, computed_at, score, spec_hash, n_train, n_pos, last_train_day, threshold, alert)`. **Append-only**: только INSERT, каноническое значение дня — последняя строка (`MAX(id)`). `_ensure_schema` сносит дорелизную схему без `spec_hash`.
- `spec_hash(features, model, min_history, horizon, label_rule)` — версия рецепта (sha1, 12 hex). Смена рецепта = новый hash; порог считается только внутри одного рецепта.
- `record(conn, ...)` — единственный путь записи дня: **только последний день** (`ml.causal_score`), порог из `threshold`, INSERT. Если день внутри эпизода / мало истории — строки нет (`reason`).
- `replay(conn, since=None, ...)` — холодный старт: `ml.causal_scores` по всей истории, добавляет недостающие дни (повторный replay ничего не дублирует). `sync(conn, ...)` — шаг refresh: сев если ledger пуст по этому `spec_hash`, иначе `record`.
- `threshold(conn, spec, before_day, budget=DEFAULT_BUDGET(2.0))` — квантиль канонических скоров `< before_day`; `min(0.5, budget/30.44)`; `< MIN_HISTORY` → `None`.
- `audit(conn, day=None, ...)` — пересчёт дня сейчас, без записи (гейт + рекомендация через `digest.recommend`); `format_day` — строка дня.
- **Проспективность**: `computed_at < min(onset, day+H_ONSET)` (onset строго после дня); сев пишет историю позже исходов → 0 проспективных. Помечается `*` в `format_scores`.
- `_ready(conn)` — понятная ошибка «нет ml_features/ml_labels — сначала `refresh --deep`» вместо `sqlite3.OperationalError` (её ловит `cmd_score`, печатает в stderr, код 1).
- **Воспроизводимость — регресс-тест**: скор дня `T` не зависит от данных после `T`; обрезка базы по `as_of` даёт то же число (`tests/test_health_score.py`).
- Стоимость на живых данных: сев всей истории (`replay`) ~9.5 с разово; живая запись дня — одна тренировка, ~0.1 с.

## 3. ETL/Схемы

- `koilog/etl.py` — базовые таблицы (`sets`, представления WEEKLY/SESSION), `connect()`, миграции колонок.
- `koilog/health/*` — свои `_SCHEMA` (aw_*, load_daily/health_days/test_days, illness_*, ml_*).

## 4. CLI
- `koilog/cli.py` — команды: `load`, `apple-load`, `health-load`, `illness`, `ml`, `report`, `refresh` (флаги `--deep`, `--xml`), `digest` (`--date`, `--json`), `score` (`--date`, `--replay`, `--since`, `--last`, `--prospective`, `--model`, `--budget`, `--json`). `cmd_refresh` вызывает `health_refresh.run`. `--xml` задаёт источник; слепок (`refresh_source`) действует и для него (разбор только при изменении файла).
- `cmd_score`: без флагов — `sync` (сев при пустом ledger'е, иначе запись дня), печатает последние дни; `--replay` — сев истории; `--date` — `audit` (пересчёт без записи). `RuntimeError`/`sqlite3.OperationalError` (нет ML-таблиц / нет extra ml) ловятся → сообщение в stderr, код 1 (без трассировки).

## 5. Ноутбуки
- `koilog.ipynb`, `koilog-health.ipynb` — ячейка 2 вызывает `refresh.run`, печатает свежесть до работы. Health-ноутбук пересобирается внутренним драйвером (nbclient не требуется).
- Драйвер `nb_run.py` (в temp): пишет JSON как Jupyter (`text` — **список строк**, `image/png` — строкой), `execution_count` = порядковый номер code-ячейки (а не номер в текущем прогоне), фигуры — через `savefig` в dpi фигуры. Третий аргумент — ячейки-**прелюдии** (напр. 12), которые выполняются ради `rcParams`, но их сохранённый вывод не переписывается. Глушит warning Agg (`non-interactive`).

## 6. Приоритет порядка (актуальный)
Согласованный: **7+8 → C10+C11 → A1 с D10 внутри → D12 + урезанный A3 → causal replay → 5(+5a) → 6 → E → 2/4 по мере сил**. D10 не откладывать (уточняет A1). D12 + урезанный A3 вытянуты вперёд (маленькая правка + ценность проспективных данных). 5 раньше E (чинит известные артефакты FP в перерывах, gap). E2: веса участия — справочные константы, держать устойчивыми (±20% допустимы).

Сделано: 7+8, C10+C11, **A1+D10** (односторонний гейт + окно тишины 14 дн, коммит `46d9465`), **D12 + урезанный A3** (append-only ledger `ml_score` + причинный скор + строка скора/гейта в карточке; `docs/d12-score-snapshots.md`; коммит ждёт одобрения). D12 в текущем раунде переписан: «заморозка + пересчёт рядом» заменена на **append-only ledger** (перезаписи нет как операции, канонический скор = `MAX(id)`), а пересчёт переведён на **причинное правило меток** (`CAUSAL_LABEL_RULE = "causal-v1"`, открытое окно — исключение, не 0). Причина: и перезапись, и «неизвестное → 0» дают утечку через обучающую историю; замер 30.06 — оракул 0.3385 vs причинный 0.2397 (воспроизводимость подтверждена на боевой базе). Порог берётся из канонической истории ledger'а, а не из свежепересчитанных прошлых. `ml_score` в боевой БД пересоздан (старая схема без `spec_hash` сносится), первый сев `replay` — 880 дней, 0 проспективных, один `spec_hash`. Пререгистрация честной оценки и правило главной строки — `docs/causal-replay-spec.md`.

Дальше — **causal replay** (перенести оценку `walk_forward` на причинные метки: каузальные числа главной строкой, оракульные рядом как верхняя граница; проверить, выживает ли nowcast, — спека в `docs/causal-replay-spec.md`), затем **5 (state-машина «перерыв → рампа → штатный»)**, затем 5a (целевой вес рампы через `koilog/training.py`), затем **6 + остаток правки №3** (nowcast-скор в карточке — частично закрыто A3; остаток — согласовать формулировки), затем ветка E, затем 2–4; D11 (тоннаж для атрибуции/Round 4) — готов к просмотру.

## 7. Окружение разработки (не поведение репозитория)

*Заметки выше — про локальный инструментарий/машину, а не про поведение кода репозитория.*

- **Скрипты `python script.py` виснут** на чтении файлов BB при импорте (faulthandler показывает: `importlib get_data`), а `python -c "..."` и `python -m ...` работают мгновенно. Обход: запускать через
  `python -c "exec(open(r'...\\script.py', encoding='utf-8').read())"`,
  а для драйвера ноутбука — `python -c "import sys; sys.argv=['nb_run','<nb>','<cells>']; exec(open(r'...\\nb_run.py',encoding='utf-8').read())"`.
- После таймаута команды остаются **зависшие python-процессы** (висят на чтении/записи, держат файлы). Убивать по командной строке: `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` → `Stop-Process -Id`. Заблокированный файл удалять через `python -c "os.remove(...)"`.
- PowerShell: нет `head`/`tail`/`&&`; heredoc не работает. Вывод фильтровать `Select-Object -Last N` / `Select-String`.
- Не читать в скрипте `koilog-health.ipynb` из BB напрямую: копировать в temp и читать оттуда (то же — через `python -c`).
- Ноутбуки: `nb_run.py` (в temp) прогоняет выбранные code-ячейки и пишет JSON как Jupyter (`indent=1, ensure_ascii=False`, хвостовой `\n`; `text` — список строк). `execution_count` = порядковый номер code-ячейки, поэтому перезапуск одной ячейки 2 сохраняет `execution_count=1`. Есть прелюдия-ячейки (3-й аргумент) для `rcParams`.
