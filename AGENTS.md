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
- `deep=True` — вызывает `ml.build_features`, `ml.build_labels` (требуют numpy/sklearn, extra ml).
- `_freshness(conn)` — MAX(date/start) по (`aw_daily`, `load_daily`, `health_days`, `ml_features`, `illness_episodes`).
- `format_freshness(step)` — итог: звенья строкой + свежесть **таблицей** «таблица | дата» (через `health.text.table`), чтобы отставание виделось построчно.

### `koilog/health/ml.py` — ML
- `build_features(conn)`, `build_labels(conn)` — формируют `ml_features`, `ml_labels`.
- Метки: `ml_labels` (date,y,y_onset,episode_id,onset). `lead_days = onset_date - day`. `y_onset` в горизонте H_ONSET (~7). Эпизод дня назначается по **первому onset строго после дня** (bisect_right) → каждый день имеет ровно один `episode_id`. Окна разных эпизодов **не пересекаются** (нет двойного счёта поимок).
- Дни эпизода (`at_risk=0`) исключаются из окон поимки. Больные дни эпизода = негативы (дизайн hazard/labeling). Не менять осознанно, с перегонкой.
- `report(conn, ...)` — строит walk-forward v2 (`walk_v2`), событийные метрики (catchable/nowcastable, lead), bootstrap. Две бухгалтерии: потолок (включая burn-in) vs catchable (без burn-in) — держать явным в README (C8).

## 3. ETL/Схемы
- `koilog/etl.py` — базовые таблицы (`sets`, представления WEEKLY/SESSION), `connect()`, миграции колонок.
- `koilog/health/*` — свои `_SCHEMA` (aw_*, load_daily/health_days/test_days, illness_*, ml_*).

## 4. CLI
- `koilog/cli.py` — команды: `load`, `apple-load`, `health-load`, `illness`, `ml`, `report`, `refresh` (флаги `--deep`, `--xml`). `cmd_refresh` вызывает `health_refresh.run`. `--xml` задаёт источник; слепок (`refresh_source`) действует и для него (разбор только при изменении файла).

## 5. Ноутбуки
- `koilog.ipynb`, `koilog-health.ipynb` — ячейка 2 вызывает `refresh.run`, печатает свежесть до работы. Health-ноутбук пересобирается внутренним драйвером (nbclient не требуется).

## 6. Приоритет порядка (актуальный)
Согласованный: **7+8 → C10+C11 → A1 с D10 внутри → D12 + урезанный A3 → 5(+5a) → 6 → E → 2/4 по мере сил**. D10 не откладывать (уточняет A1). D12 + урезанный A3 вытянуты вперёд (маленькая правка + ценность проспективных данных). 5 раньше E (чинит известные артефакты FP в перерывах, gap). E2: веса участия — справочные константы, держать устойчивыми (±20% допустимы).

## 7. Окружение разработки (не поведение репозитория)

*Заметки выше — про локальный инструментарий/машину, а не про поведение кода репозитория.*

- **Скрипты `python script.py` виснут** на чтении файлов BB при импорте (faulthandler показывает: `importlib get_data`), а `python -c "..."` и `python -m ...` работают мгновенно. Обход: запускать через
  `python -c "exec(open(r'...\\script.py', encoding='utf-8').read())"`,
  а для драйвера ноутбука — `python -c "import sys; sys.argv=['nb_run','<nb>','<cells>']; exec(open(r'...\\nb_run.py',encoding='utf-8').read())"`.
- После таймаута команды остаются **зависшие python-процессы** (висят на чтении/записи, держат файлы). Убивать по командной строке: `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` → `Stop-Process -Id`. Заблокированный файл удалять через `python -c "os.remove(...)"`.
- PowerShell: нет `head`/`tail`/`&&`; heredoc не работает. Вывод фильтровать `Select-Object -Last N` / `Select-String`.
- Не читать в скрипте `koilog-health.ipynb` из BB напрямую: копировать в temp и читать оттуда (то же — через `python -c`).
- Ноутбуки: `nb_run.py` (в temp) прогоняет выбранные code-ячейки и пишет JSON байт-в-байт (`indent=1, ensure_ascii=False`, хвостовой `\n`). Перезапуск одной ячейки 2 сохраняет `execution_count=1` (первая code-ячейка).
