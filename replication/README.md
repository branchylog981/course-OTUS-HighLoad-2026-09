# ДЗ №3: репликация PostgreSQL

Эта инструкция — как поднять стенд и повторить замеры из [отчёта](../reports/replication/REPORT.md). Весь код лежит в этом же репозитории, рядом с ДЗ №1–2.

## Запуск приложения с двумя репликами

Понадобятся Docker Compose v2 и свободный порт 8082. Все команды выполняются из корня репозитория (каталог `1/`).

Если `.env` ещё нет, создайте его из примера. В существующем файле ничего менять не нужно.

```bash
cp .env.example .env
# Укажите POSTGRES_PASSWORD и JWT_SECRET длиной не меньше 32 байт.
```

Стенд живёт в отдельном Compose-проекте `social-replication` со своими тремя volumes, поэтому обычную БД из `compose.yaml` и результаты ДЗ №2 он не трогает.

```bash
docker compose -f compose.replication.yaml up -d --build --wait app
curl http://localhost:8082/health
curl 'http://localhost:8082/user/search?first_name=Ив&last_name=Пет'
```

При первом запуске всё происходит само: поднимается master, две physical standby создаются через `pg_basebackup -Xs -R`, накатываются схема с индексом, затем стартует API. По умолчанию `READ_HOSTS=["slave1","slave2"]`; если задать `READ_HOSTS='[]'`, получится режим чтения с master — удобно для сравнения. Пароль роли `replicator` задаётся через `REPLICATION_PASSWORD` (по умолчанию учебный `replication-lab-only`). Порты БД наружу не публикуются, API слушает только loopback.

Как это работает: `/user/get/{id}` и `/user/search` распределяются round-robin между двумя репликами — настоящие транзакции READ ONLY, суммарно 10 соединений. Регистрация и логин идут на master. Учтите, что при асинхронной репликации чтение сразу после записи может увидеть старые данные.

## Полное воспроизведение

Нужны Python 3.14 и зависимости генератора:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-benchmark.txt
.venv/bin/python -m replication.run
```

Дальше скрипт работает сам, по шагам:

1. Загружает миллион анкет из того же OTUS CSV, что и ДЗ №2 (при необходимости скачивает его в `data/`), проверяет индекс и достижение WAL на репликах.
2. Нагружает оба GET API смесью 50/50: 1/10/100 клиентов, 3 повтора, 3 с прогрева + 15 с измерений. Сравнивает чтение с master и с двух реплик.
3. Для каждого прогона сверяет успешные HTTP-запросы с дельтами локальных `pg_stat_statements` на всех трёх узлах, ответы — по SHA-256.
4. Поднимает ещё два отдельных кластера: `social-replication-async` (порт 8083) и `social-replication-quorum` (порт 8084), у каждого своя тройка volumes.
5. Пишет строки с отдельными COMMIT и внешним журналом подтверждений, убивает master через SIGKILL, дожидается завершения генератора, сравнивает durable receive LSN обеих реплик, промоутит самую свежую и подключает к ней оставшуюся.
6. Сравнивает множества подтверждённых и сохранившихся seq. В async контролируемый разрыв репликационных сетей перед аварией показывает потерю; в quorum `ANY 1 (slave1, slave2)` + `synchronous_commit=on` сохраняет все подтверждённые строки.
7. Пересоздаёт только API с новым primary и одной читающей репликой, проверяет регистрацию, профиль, поиск и логин; в конце строит отчёт и PNG/SVG-график.

По ресурсам: каждый узел БД и API получает по 2 CPU / 2 GiB. Если ресурсов мало, выполняйте этапы по очереди и останавливайте завершённые проекты.

Данные read-стенда сохраняются и используются повторно, но только если совпали число анкет и SHA-256 исходного CSV. Отчёт и журналы при повторном запуске перезаписываются. Миграции скрипт не меняет и индекс не удаляет.

Полезные частичные запуски:

```bash
# Только замеры чтения; при повторе миллион анкет используется снова.
.venv/bin/python -m replication.run --section read

# Только аварии; --reset удаляет данные ИСКЛЮЧИТЕЛЬНО двух аварийных кластеров.
.venv/bin/python -m replication.run --section failover --reset

# Короткий прогон на НОВОМ read-стенде; это не полные результаты для сдачи.
.venv/bin/python -m replication.run --section read --rows 10000 --duration 3 --warmup 1 --repetitions 1

# Перестроить отчёт из сохранённых JSON, без доступа к Docker/БД.
.venv/bin/python -m replication.report
```

Одно предостережение: короткий прогон с другим `--rows` требует пустой отдельной read-БД — загрузчик откажется менять уже заполненную таблицу с другим количеством анкет.

## Проверки и ручное управление

Основной регрессионный набор запускается через обычный compose:

```bash
docker compose up -d --build --wait
docker compose --profile test run --build --rm tests
```

Тесты маршрутизации требуют готового read-кластера и не любят соседства с benchmark. Они проверяют: фактические адреса БД и recovery/read-only на репликах, запрет INSERT, работу при одной упавшей реплике, 503 при недоступности всех, а также успешные регистрацию и логин на master, когда replay обеих реплик приостановлен.

```bash
docker compose -f compose.replication.yaml --profile test run --build --rm \
  -e RUN_REPLICATION_TESTS=1 tests pytest -q tests/test_replication.py

docker compose -f compose.replication.yaml --profile test run --rm tests \
  ruff check app tests benchmarks replication
```

### После failover

После автоматического failover новая топология записывается в игнорируемый Git файл — `replication/topology-async.env` или `topology-quorum.env`. Дальше все команды управления нужно запускать с **обоими** env-файлами:

```bash
docker compose -p social-replication-quorum --env-file .env \
  --env-file replication/topology-quorum.env -f compose.replication.yaml ps -a

# Перезапуск API после failover: --no-deps не запускает остановленный master.
docker compose -p social-replication-quorum --env-file .env \
  --env-file replication/topology-quorum.env -f compose.replication.yaml \
  up -d --no-deps app
```

Важно: не запускайте `up` всего аварийного кластера после promotion — старый primary должен оставаться остановленным. Чтобы вернуть его в строй, понадобится `pg_rewind` или новый basebackup, это за рамки данного ручного failover. Новый master продолжает работать со второй репликой, в quorum сохраняется `ANY 1 (оставшаяся_реплика)`.

### Состояние репликации и переключение режимов

Посмотреть состояние потока:

```bash
docker compose -f compose.replication.yaml exec master sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT application_name,state,sync_state,flush_lsn,replay_lsn FROM pg_stat_replication"'
```

Вручную включить кворум на исходном master (после подключения обеих реплик):

```bash
docker compose -f compose.replication.yaml exec -T master sh -c \
  'exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
ALTER SYSTEM SET synchronous_standby_names = 'ANY 1 (slave1, slave2)';
ALTER SYSTEM SET synchronous_commit = 'on';
SELECT pg_reload_conf();
SQL
```

Обратно к async: `synchronous_standby_names = ''` и `pg_reload_conf()`. И не забудьте вернуть async перед повтором сравнительных read-замеров — исходный эксперимент измеряет именно асинхронные standby.

### Остановка

С сохранением данных (аналогично для суффиксов `-async`/`-quorum`):

```bash
docker compose -p social-replication -f compose.replication.yaml down
```

`down -v` дополнительно удаляет volumes выбранного проекта.

## Артефакты сдачи

- `compose.replication.yaml` — один master, два standby, API, миграции и тесты.
- `app/routing.py` — маршрутизация и READ ONLY транзакции.
- `replication/` — инициализация, воспроизводимые нагрузки, аварийный сценарий и отчёт.
- `reports/replication/REPORT.md` — отчёт с фактическими результатами.
- `reads.json`, `raw/*.csv.gz` — результаты и журнал каждого HTTP-запроса.
- `failover-*.json` — LSN, роли, потерянные seq, проверки API после переключения.
- `writes-*.jsonl` — внешние подтверждения каждого успешного COMMIT.

## Итог

Кворум обеспечивает сохранность подтверждённых записей при отказе одного master — при описанных в отчёте условиях и выборе самой свежей реплики. Автоматического leader election нет: время ручного переключения и пересоздания API — это простой записи. И ещё раз про главное: standby с неподходящим LSN промоутить нельзя.
