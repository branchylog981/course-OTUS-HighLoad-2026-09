# ДЗ №2. Поиск анкет и индексирование

Проведён реальный HTTP-тест `/user/search` на **1,000,000 анкетах**.
Одинаковый код приложения, данные, ресурсы и набор запросов использованы до и после индекса.
Изменение между фазами — индекс `idx_users_name_prefix` и обновление статистики `ANALYZE`.

## Стенд

- Начало: `2026-11-02T08:40:28.213592+00:00`, окончание: `2026-11-02T08:50:07.088108+00:00` (UTC).
- Хост: `Linux-6.18.33.2-microsoft-standard-WSL2-x86_64-with-glibc2.43`; Docker `29.9.2`,
  12 vCPU, 23.5 GiB памяти.
- PostgreSQL: `PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2) on x86_64-pc-linux-gnu, compiled by gcc (Debian 12.2.0-14+deb12u1) 12.2.0, 64-bit`.
- БД ограничена 2 CPU / 2 GiB, приложение — 2 CPU / 2 GiB; генератор нагрузки работает на хосте.
- БД: `shared_buffers=512MB`, `max_parallel_workers_per_gather=0`,
  `lc_collate=en_US.utf8`,
  `work_mem=4096 kB`.
  Параллелизм SQL отключён, чтобы сравнивать именно клиентскую одновременность.
- Один процесс Uvicorn, штатный пул psycopg от 1 до 10 соединений, ожидание пула 5 с,
  SQL statement timeout 5 с. Код/конфигурация одинаковы в обеих фазах.
- Клиент: Python 3.14.4, aiohttp 3.14.5,
  HTTP/1.1 keep-alive.
  Ресурсы, образы и SHA-256 исходных файлов записаны в [results.json](results.json).
- Размер таблицы: 238.04 MiB;
  размер добавленного индекса: **9.58 MiB**.

## Данные

Источник — [people.v2.csv OTUS](https://raw.githubusercontent.com/OtusTeam/highload/master/homework/people.v2.csv). Сохранены реальные имя, фамилия,
дата рождения и город; пол — `unspecified`, биография техническая.
SHA-256 исходного файла: `7e67312e0a42c3377bd60248329ab5025b1bcfbd13848ffdff57770413bb1ec1`.

В первом проходе прочитано 999,930 корректных строк,
пропущено 1 повреждённых;
70 анкет взяты повторно с начала файла. Для каждой строки создан отдельный
детерминированный UUIDv5, поэтому результат — ровно 1,000,000 отдельных записей.
Различных имён: 138, фамилий: 1006.
Частотность исходных имён и фамилий сохраняется;
добавленные строки составляют 0.007% массива.
Сохранён порядок исходного CSV, сгруппированного по фамилии;
он влияет на локальность обращений к страницам таблицы. `CLUSTER` не выполнялся.
Учётные записи загрузчика предназначены для поиска: вход для них отключён,
общего известного пароля нет. Обычная регистрация продолжает использовать Argon2id.

Загрузка выполнена через параметризованный `COPY`, после неё — `VACUUM (ANALYZE) users`.
Исходный CSV не включён в Git; контрольная сумма, частоты имён и параметры генерации —
в [seed.json](seed.json). Таблица находится в отдельном volume проекта `social-benchmark`.

## Запрос и нагрузка

```sql
SELECT id, first_name, second_name, birthdate, gender, biography, city
FROM users
WHERE first_name LIKE $1 ESCAPE '!' AND second_name LIKE $2 ESCAPE '!'
ORDER BY id;
```

Параметры для первой пары: `$1 = 'Алек%'`, `$2 = 'Иван%'`.
HTTP-параметр `last_name` соответствует столбцу `second_name`.
Поиск регистрозависимый, оба префикса обязательны. `%`, `_` и `!` во вводе экранируются
как буквальные символы; сами значения передаются отдельно от SQL.
`prepare=False` для этого запроса сохраняет планирование с известными префиксами:
generic plan не может вывести индексный диапазон из неизвестного параметра LIKE.

Во всех тестах по кругу чередуются шесть пар, без ограничения выдачи (`LIMIT` отсутствует).
Одинаковое содержимое ответов в обеих фазах проверено SHA-256; порядок ID и соблюдение
обоих префиксов проверены до нагрузки. Во время теста проверяется статус и хеш полного тела.

| Префикс имени | Префикс фамилии | Анкет в ответе | Доля от таблицы |
| --- | --- | ---: | ---: |
| `Алек` | `Иван` | 1388 | 0.13880% |
| `Мар` | `Смир` | 859 | 0.08590% |
| `Иль` | `Петр` | 139 | 0.01390% |
| `Роб` | `Абрам` | 7 | 0.00070% |
| `Злат` | `Яшин` | 2 | 0.00020% |
| `Несуществующее` | `Имя` | 0 | 0.00000% |

Для каждой фазы и одновременности 1, 10, 100, 1000 проведено
**3 повтора**. Перед каждым — минимум один проход по всем запросам
и 3 с прогрева с одним клиентом; он не входит в статистику.
Измеряемое окно запуска запросов — 15 с.
Каждый виртуальный пользователь отправляет следующий запрос только после полного предыдущего ответа
(closed-loop). В конце окна новые запросы не запускаются, уже начатые полностью дожидаются ответа;
клиентский timeout — 120 с. Нагрузка записывается в порядке `before`, затем `after`,
внутри фазы — повторы и одновременность по возрастанию. Между прогонами пауза 1 с.

**Throughput = число корректных HTTP 200 / фактическое время прогона с завершением
всех начатых запросов**. Ошибки не считаются успешным throughput.
Latency измеряется до чтения всего тела и включает сеть, очередь приложения, ожидание
соединения, SQL и сериализацию. Среднее/p95/p99 на графиках включают также ошибочные запросы;
отдельный p95 только успешных ответов сохранён в JSON. Таблицы показывают медиану
показателей отдельных повторов, а число ошибок — сумму повторов.
Заливка на графиках latency и интервалы throughput — min–max между повторами,
не доверительный интервал.
Ни один незавершённый запрос не отброшен из статистики.

## До индекса

До теста проверено, что у `users` есть только `users_pkey`.

| Одновременность | Успешных req/s | Среднее, мс | p95, мс | p99, мс | Ошибки / запросы |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 11.46 | 87.27 | 97.52 | 103.59 | 0 / 581 |
| 10 | 20.74 | 480.43 | 605.93 | 697.90 | 0 / 942 |
| 100 | 20.98 | 4335.73 | 6140.47 | 6273.42 | 0 / 1174 |
| 1000 | 21.37 | 45568.26 | 46642.22 | 46708.79 | 0 / 3003 |

![Latency до индекса](latency-before.png)

![Throughput до индекса](throughput-before.png)

## Добавление индекса

```sql
CREATE INDEX IF NOT EXISTS idx_users_name_prefix
    ON users (first_name varchar_pattern_ops, second_name varchar_pattern_ops);
ANALYZE users;
```

DDL сохранён в [миграции 002](../../migrations/002_users_name_prefix.sql).
Обычный запуск приложения применяет обе миграции; benchmark запускает только 001,
а индекс создаёт после завершения всех замеров первой фазы.

## После индекса

| Одновременность | Успешных req/s | Среднее, мс | p95, мс | p99, мс | Ошибки / запросы |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 231.76 | 4.31 | 10.13 | 12.15 | 0 / 10380 |
| 10 | 235.45 | 42.43 | 68.02 | 82.85 | 0 / 10521 |
| 100 | 195.30 | 506.23 | 586.04 | 619.86 | 0 / 9134 |
| 1000 | 177.39 | 5116.53 | 5814.58 | 5941.19 | 0 / 9382 |

![Latency после индекса](latency-after.png)

![Throughput после индекса](throughput-after.png)

| Одновременность | Рост успешного throughput | Снижение p95 |
| ---: | ---: | ---: |
| 1 | 20.23× | 9.63× |
| 10 | 11.35× | 8.91× |
| 100 | 9.31× | 10.48× |
| 1000 | 8.30× | 8.02× |

Всего измерено **45,117 HTTP-запросов**, из них **0 ошибок**;
максимум одновременно выполнявшихся клиентских запросов — **1000**.
При 1 клиенте преимущество индекса заметно без длинной очереди.
При 1000 клиентах latency включает очередь, а throughput ограничивается ресурсами всего API,
где остаются ожидание пула БД и обработка полных JSON-ответов. После индекса насыщение
достигается уже при 1–10 клиентах; дальнейший рост одновременности увеличивает задержку.
Отдельное профилирование CPU, очередей и сериализации не проводилось, поэтому точное
узкое место после индекса этим тестом не установлено. Ошибки и tail latency сохранены.

## Почему этот индекс

Оба условия — поиск с фиксированного начала строки. B-tree с `varchar_pattern_ops`
позволяет построить диапазоны для `LIKE 'префикс%'` при локали, отличной от C;
тип столбцов в схеме — `VARCHAR`, поэтому выбран именно `varchar_pattern_ops`.
Обычный индекс с локализованным порядком сравнения не гарантирует такой доступ для LIKE.
[Документация PostgreSQL об operator classes](https://www.postgresql.org/docs/17/indexes-opclass.html).

Оба столбца находятся в одном индексе, поскольку фильтр требует их одновременно.
Порядок `(first_name, second_name)` соответствует обязательному фильтру по имени.
В PostgreSQL 17 диапазон по первому столбцу ограничивает сканируемый участок индекса;
условие по второму проверяется внутри него и сокращает обращения к таблице, но при диапазоне
по первому столбцу не гарантирует дополнительного сужения сканируемого участка.
Это не утверждение, что порядок оптимален для любого распределения запросов: при очень
коротком префиксе имени и редком префиксе фамилии обратный порядок может быть выгоднее.
[Многоколоночные индексы PostgreSQL 17](https://www.postgresql.org/docs/17/indexes-multicolumn.html).

`id` не добавлен в индекс: после двух префиксных диапазонов он всё равно не устраняет
`ORDER BY id` для общего случая. Сортируется только найденная выборка; Sort виден в EXPLAIN.
Покрывающий индекс на все поля анкеты увеличил бы размер и стоимость записей, а запрос
возвращает в том числе `biography`; для этого ДЗ он не требуется. Триграммный индекс нужен
для более общего поиска по подстроке; здесь проверяется именно префиксный поиск.
Для очень широких префиксов планировщик может выбрать Seq Scan — индекс не обязан
быть быстрее при возврате значительной части таблицы. Индекс ускоряет чтение ценой
дополнительного места и работы при INSERT/UPDATE.

## EXPLAIN ANALYZE после индекса

Использован **тот же SQL и те же значения параметров**, что в HTTP-методе,
с `EXPLAIN (ANALYZE, BUFFERS, SETTINGS)`. Планировщик не принуждался к индексу,
`enable_seqscan` не менялся. Перед съёмкой планов запросы прогреты.
Ниже — отдельные SQL-замеры без HTTP-очереди; их время нельзя напрямую сравнивать с HTTP p95.
В таблице использованы JSON-планы; текстовый EXPLAIN снят следующим вызовом,
поэтому времена в таблице и текстовых планах немного различаются.
JSON-планы и полные текстовые планы обеих фаз сохранены в [explain/](explain/).

| Пара префиксов | Execution Time до, мс | После, мс |
| --- | ---: | ---: |
| `Алек` / `Иван` | 52.828 | 0.723 |
| `Мар` / `Смир` | 47.583 | 0.435 |
| `Иль` / `Петр` | 46.412 | 0.192 |
| `Роб` / `Абрам` | 51.651 | 0.032 |
| `Злат` / `Яшин` | 45.576 | 0.030 |
| `Несуществующее` / `Имя` | 49.440 | 0.012 |

### Запрос 1: `Алек` / `Иван`

```text
{"first_name": "Алек", "last_name": "Иван"}

Sort  (cost=5740.43..5743.87 rows=1375 width=176) (actual time=0.875..0.988 rows=1388 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 318kB
  Buffers: shared hit=659
  ->  Bitmap Heap Scan on users  (cost=1131.25..5668.76 rows=1375 width=176) (actual time=0.462..0.710 rows=1388 loops=1)
        Filter: (((first_name)::text ~~ 'Алек%'::text) AND ((second_name)::text ~~ 'Иван%'::text))
        Heap Blocks: exact=598
        Buffers: shared hit=659
        ->  Bitmap Index Scan on idx_users_name_prefix  (cost=0.00..1130.91 rows=1370 width=0) (actual time=0.430..0.431 rows=1388 loops=1)
              Index Cond: (((first_name)::text ~>=~ 'Алек'::text) AND ((first_name)::text ~<~ 'Алел'::text) AND ((second_name)::text ~>=~ 'Иван'::text) AND ((second_name)::text ~<~ 'Ивао'::text))
              Buffers: shared hit=61
Settings: max_parallel_workers_per_gather = '0'
Planning Time: 0.075 ms
Execution Time: 1.023 ms
```

### Запрос 2: `Мар` / `Смир`

```text
{"first_name": "Мар", "last_name": "Смир"}

Sort  (cost=4393.15..4395.55 rows=960 width=176) (actual time=0.375..0.398 rows=859 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 188kB
  Buffers: shared hit=454
  ->  Bitmap Heap Scan on users  (cost=1060.16..4345.60 rows=960 width=176) (actual time=0.118..0.272 rows=859 loops=1)
        Filter: (((first_name)::text ~~ 'Мар%'::text) AND ((second_name)::text ~~ 'Смир%'::text))
        Heap Blocks: exact=396
        Buffers: shared hit=454
        ->  Bitmap Index Scan on idx_users_name_prefix  (cost=0.00..1059.92 rows=956 width=0) (actual time=0.099..0.099 rows=859 loops=1)
              Index Cond: (((first_name)::text ~>=~ 'Мар'::text) AND ((first_name)::text ~<~ 'Мас'::text) AND ((second_name)::text ~>=~ 'Смир'::text) AND ((second_name)::text ~<~ 'Смис'::text))
              Buffers: shared hit=58
Settings: max_parallel_workers_per_gather = '0'
Planning Time: 0.069 ms
Execution Time: 0.422 ms
```

### Запрос 3: `Иль` / `Петр`

```text
{"first_name": "Иль", "last_name": "Петр"}

Sort  (cost=670.02..670.32 rows=118 width=176) (actual time=0.084..0.088 rows=139 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 51kB
  Buffers: shared hit=106
  ->  Bitmap Heap Scan on users  (cost=217.96..665.96 rows=118 width=176) (actual time=0.031..0.064 rows=139 loops=1)
        Filter: (((first_name)::text ~~ 'Иль%'::text) AND ((second_name)::text ~~ 'Петр%'::text))
        Heap Blocks: exact=90
        Buffers: shared hit=106
        ->  Bitmap Index Scan on idx_users_name_prefix  (cost=0.00..217.93 rows=117 width=0) (actual time=0.024..0.024 rows=139 loops=1)
              Index Cond: (((first_name)::text ~>=~ 'Иль'::text) AND ((first_name)::text ~<~ 'Илэ'::text) AND ((second_name)::text ~>=~ 'Петр'::text) AND ((second_name)::text ~<~ 'Петс'::text))
              Buffers: shared hit=16
Settings: max_parallel_workers_per_gather = '0'
Planning Time: 0.075 ms
Execution Time: 0.097 ms
```

### Запрос 4: `Роб` / `Абрам`

```text
{"first_name": "Роб", "last_name": "Абрам"}

Sort  (cost=109.16..109.16 rows=1 width=176) (actual time=0.027..0.027 rows=7 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 26kB
  Buffers: shared hit=14
  ->  Index Scan using idx_users_name_prefix on users  (cost=0.42..109.14 rows=1 width=176) (actual time=0.009..0.024 rows=7 loops=1)
        Index Cond: (((first_name)::text ~>=~ 'Роб'::text) AND ((first_name)::text ~<~ 'Ров'::text) AND ((second_name)::text ~>=~ 'Абрам'::text) AND ((second_name)::text ~<~ 'Абран'::text))
        Filter: (((first_name)::text ~~ 'Роб%'::text) AND ((second_name)::text ~~ 'Абрам%'::text))
        Buffers: shared hit=14
Settings: max_parallel_workers_per_gather = '0'
Planning Time: 0.070 ms
Execution Time: 0.034 ms
```

### Запрос 5: `Злат` / `Яшин`

```text
{"first_name": "Злат", "last_name": "Яшин"}

Sort  (cost=149.19..149.20 rows=1 width=176) (actual time=0.023..0.023 rows=2 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=11
  ->  Bitmap Heap Scan on users  (cost=70.42..149.18 rows=1 width=176) (actual time=0.020..0.021 rows=2 loops=1)
        Filter: (((first_name)::text ~~ 'Злат%'::text) AND ((second_name)::text ~~ 'Яшин%'::text))
        Heap Blocks: exact=2
        Buffers: shared hit=11
        ->  Bitmap Index Scan on idx_users_name_prefix  (cost=0.00..70.42 rows=20 width=0) (actual time=0.017..0.017 rows=2 loops=1)
              Index Cond: (((first_name)::text ~>=~ 'Злат'::text) AND ((first_name)::text ~<~ 'Злау'::text) AND ((second_name)::text ~>=~ 'Яшин'::text) AND ((second_name)::text ~<~ 'Яшио'::text))
              Buffers: shared hit=9
Settings: max_parallel_workers_per_gather = '0'
Planning Time: 0.069 ms
Execution Time: 0.030 ms
```

### Запрос 6: `Несуществующее` / `Имя`

```text
{"first_name": "Несуществующее", "last_name": "Имя"}

Sort  (cost=8.47..8.47 rows=1 width=176) (actual time=0.006..0.006 rows=0 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=3
  ->  Index Scan using idx_users_name_prefix on users  (cost=0.42..8.46 rows=1 width=176) (actual time=0.005..0.005 rows=0 loops=1)
        Index Cond: (((first_name)::text ~>=~ 'Несуществующее'::text) AND ((first_name)::text ~<~ 'Несуществующеж'::text) AND ((second_name)::text ~>=~ 'Имя'::text) AND ((second_name)::text ~<~ 'Имѐ'::text))
        Filter: (((first_name)::text ~~ 'Несуществующее%'::text) AND ((second_name)::text ~~ 'Имя%'::text))
        Buffers: shared hit=3
Settings: max_parallel_workers_per_gather = '0'
Planning Time: 0.069 ms
Execution Time: 0.012 ms
```

## Ограничения измерения

Это локальный сравнительный эксперимент на общем WSL2/Docker-хосте, а не обещание
производительности production. Кэш PostgreSQL и ОС прогревается одинаковым сценарием;
холодный дисковый запуск не измеряется. Планирование и статистика PostgreSQL могут немного
различаться между повторами. До индекса всегда измерялось раньше, порядок фаз не рандомизирован.

Closed-loop отражает фиксированное число одновременных клиентов: при замедлении API
генератор автоматически снижает интенсивность. Он не моделирует фиксированный внешний RPS
и не корректирует coordinated omission. При высокой одновременности завершение очереди
увеличивает фактическое время теста; `elapsed_seconds` и `drain_seconds` сохранены для каждого
повтора. Ошибочные ответы не повышают показанный успешный throughput.
Максимальная загрузка процесса генератора: 8.4% одного ядра;
это диагностический показатель, сам по себе он не доказывает отсутствие всех ограничений клиента.
Выдача без пагинации соответствует спецификации, поэтому доля затрат на передачу и обработку
больших ответов может быть существенной после ускорения SQL.

## Воспроизведение и исходные результаты

См. [инструкцию benchmark](../../benchmarks/README.md). Полный запуск:

```bash
.venv/bin/python -m pip install -r requirements-benchmark.txt
.venv/bin/python -m benchmarks.run
```

Повторный запуск на уже загруженной выделенной БД: `python -m benchmarks.run --reuse-data`.
Скрипт загрузчика отказывается писать в непустую БД без проверки метаданных, не использует TRUNCATE
и не обращается к обычному Compose-проекту. Фаза `before` удаляет только тестируемый индекс
в выделенной БД; после полного эксперимента индекс остаётся созданным.

- [results.json](results.json): каждый из 24 прогонов, ошибки, времена, ресурсы и хеши.
- [summary.csv](summary.csv): компактная таблица для повторной обработки.
- [raw/](raw/): gzip CSV каждого HTTP-запроса, включая ошибки.
- [seed.json](seed.json): происхождение и состав данных.
- [explain/](explain/): планы до и после, размеры таблицы и индексов.
- Четыре графика сохранены в PNG и SVG; построены matplotlib из реальных результатов.

Перестроить отчёт и графики без нагрузки: `python -m benchmarks.report`.
