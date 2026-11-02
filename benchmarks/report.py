"""Build the Markdown report and four figures from recorded measurements."""

import argparse
import csv
import json
from pathlib import Path
from statistics import median

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def grouped(results: dict, stage: str, concurrency: int) -> list[dict]:
    return [
        run
        for run in results["runs"]
        if run["stage"] == stage and run["concurrency"] == concurrency
    ]


def metric(results: dict, stage: str, concurrency: int, key: str) -> float:
    return median(run[key] for run in grouped(results, stage, concurrency))


def save_figure(fig, stem: Path):
    for suffix in ("png", "svg"):
        path = stem.with_suffix("." + suffix)
        fig.savefig(path, dpi=160)
        if suffix == "svg":
            # Matplotlib emits trailing spaces in SVG paths; keep generated Git diffs clean.
            path.write_text(
                "\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n"
            )


def figures(results: dict, directory: Path):
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11})
    for stage, label in (("before", "до индекса"), ("after", "после индекса")):
        x = results["concurrency_levels"]
        fig, ax = plt.subplots(figsize=(9, 5.5), layout="constrained")
        for key, title, color in (
            ("latency_mean_ms", "Среднее", "#2563eb"),
            ("latency_p95_ms", "p95", "#d97706"),
            ("latency_p99_ms", "p99", "#dc2626"),
        ):
            y = [metric(results, stage, c, key) for c in x]
            low = [min(run[key] for run in grouped(results, stage, c)) for c in x]
            high = [max(run[key] for run in grouped(results, stage, c)) for c in x]
            ax.plot(x, y, "o-", label=title, color=color, linewidth=2)
            ax.fill_between(x, low, high, alpha=0.12, color=color)
        ax.set(
            xscale="log",
            yscale="log",
            xlabel="Одновременные пользователи",
            ylabel="Latency полного HTTP-ответа, мс",
            title=f"Latency {label}",
        )
        ax.set_xticks(x, [str(c) for c in x])
        ax.grid(True, which="major", alpha=0.25)
        ax.legend()
        save_figure(fig, directory / f"latency-{stage}")
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(9, 5.5), layout="constrained")
        y = [metric(results, stage, c, "throughput_rps") for c in x]
        low = [min(run["throughput_rps"] for run in grouped(results, stage, c)) for c in x]
        high = [max(run["throughput_rps"] for run in grouped(results, stage, c)) for c in x]
        ax.errorbar(
            x,
            y,
            yerr=[
                [v - lo for v, lo in zip(y, low, strict=True)],
                [hi - v for v, hi in zip(y, high, strict=True)],
            ],
            fmt="o-",
            color="#0f766e",
            linewidth=2,
            capsize=5,
            label="Медиана; диапазон min–max повторов",
        )
        ax.set(
            xscale="log",
            xlabel="Одновременные пользователи",
            ylabel="Успешные запросы в секунду",
            title=f"Throughput {label}",
        )
        ax.set_ylim(bottom=0)
        ax.set_xticks(x, [str(c) for c in x])
        ax.grid(True, alpha=0.25)
        ax.legend()
        save_figure(fig, directory / f"throughput-{stage}")
        plt.close(fig)


def table(results: dict, stage: str) -> str:
    lines = [
        "| Одновременность | Успешных req/s | Среднее, мс | p95, мс | p99, мс | Ошибки / запросы |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for c in results["concurrency_levels"]:
        runs = grouped(results, stage, c)
        values = [
            metric(results, stage, c, key)
            for key in ("throughput_rps", "latency_mean_ms", "latency_p95_ms", "latency_p99_ms")
        ]
        lines.append(
            f"| {c} | "
            + " | ".join(f"{value:.2f}" for value in values)
            + f" | {sum(r['errors'] for r in runs)} / {sum(r['attempts'] for r in runs)} |"
        )
    return "\n".join(lines)


def build_report(directory: Path):
    results = json.loads((directory / "results.json").read_text())
    seed = json.loads((directory / "seed.json").read_text())
    before = json.loads((directory / "explain" / "before.json").read_text())
    after = json.loads((directory / "explain" / "after.json").read_text())
    expected = len(results["concurrency_levels"]) * results["repetitions"] * 2
    if len(results["runs"]) != expected:
        raise ValueError(f"Incomplete experiment: expected {expected} runs")
    if [q["matches"] for q in before["queries"]] != [q["matches"] for q in after["queries"]]:
        raise ValueError("Search results changed after index creation")
    figures(results, directory)
    with (directory / "summary.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "stage",
                "concurrency",
                "repetition",
                "throughput_rps",
                "latency_mean_ms",
                "latency_p50_ms",
                "latency_p95_ms",
                "latency_p99_ms",
                "attempts",
                "errors",
                "elapsed_seconds",
                "drain_seconds",
                "peak_inflight",
                "mean_inflight",
                "client_cpu_percent_one_core",
            ],
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(results["runs"])

    query_table = "\n".join(
        f"| `{q['first_name']}` | `{q['last_name']}` | {q['matches']} | "
        f"{q['selectivity_percent']:.5f}% |"
        for q in before["queries"]
    )
    gain_lines = []
    for c in results["concurrency_levels"]:
        throughput_gain = metric(results, "after", c, "throughput_rps") / metric(
            results, "before", c, "throughput_rps"
        )
        p95_gain = metric(results, "before", c, "latency_p95_ms") / metric(
            results, "after", c, "latency_p95_ms"
        )
        gain_lines.append(f"| {c} | {throughput_gain:.2f}× | {p95_gain:.2f}× |")
    gains = "\n".join(gain_lines)
    index = next(i for i in after["indexes"] if i["name"] == "idx_users_name_prefix")
    plan_summary = "\n".join(
        f"| `{b['first_name']}` / `{b['last_name']}` | "
        f"{b['plan']['Execution Time']:.3f} | {a['plan']['Execution Time']:.3f} |"
        for b, a in zip(before["queries"], after["queries"], strict=True)
    )
    explain_text = "\n\n".join(
        f"### Запрос {i + 1}: `{q['first_name']}` / `{q['last_name']}`\n\n```text\n"
        + (directory / "explain" / f"after-{i + 1:02d}.txt").read_text().strip()
        + "\n```"
        for i, q in enumerate(after["queries"])
    )
    min_c, max_c = min(results["concurrency_levels"]), max(results["concurrency_levels"])
    max_client_cpu = max(run["client_cpu_percent_one_core"] for run in results["runs"])
    peak = max(run["peak_inflight"] for run in results["runs"])
    total_errors = sum(run["errors"] for run in results["runs"])
    total_attempts = sum(run["attempts"] for run in results["runs"])
    report = f"""# ДЗ №2. Поиск анкет и индексирование

Проведён реальный HTTP-тест `/user/search` на **{seed["rows"]:,} анкетах**.
Одинаковый код приложения, данные, ресурсы и набор запросов использованы до и после индекса.
Изменение между фазами — индекс `idx_users_name_prefix` и обновление статистики `ANALYZE`.

## Стенд

- Начало: `{results["started_at_utc"]}`, окончание: `{results["finished_at_utc"]}` (UTC).
- Хост: `{results["platform"]}`; Docker `{results["docker"]["ServerVersion"]}`,
  {results["docker"]["NCPU"]} vCPU, {results["docker"]["MemTotal"] / 1024**3:.1f} GiB памяти.
- PostgreSQL: `{after["version"]}`.
- БД ограничена 2 CPU / 2 GiB, приложение — 2 CPU / 2 GiB; генератор нагрузки работает на хосте.
- БД: `shared_buffers=512MB`, `max_parallel_workers_per_gather=0`,
  `lc_collate={after["settings"].get("lc_collate")}`,
  `work_mem={after["settings"].get("work_mem")} kB`.
  Параллелизм SQL отключён, чтобы сравнивать именно клиентскую одновременность.
- Один процесс Uvicorn, штатный пул psycopg от 1 до 10 соединений, ожидание пула 5 с,
  SQL statement timeout 5 с. Код/конфигурация одинаковы в обеих фазах.
- Клиент: Python {results["python"]}, aiohttp {results["client_packages"]["aiohttp"]},
  HTTP/1.1 keep-alive.
  Ресурсы, образы и SHA-256 исходных файлов записаны в [results.json](results.json).
- Размер таблицы: {after["table_bytes"] / 1024**2:.2f} MiB;
  размер добавленного индекса: **{index["bytes"] / 1024**2:.2f} MiB**.

## Данные

Источник — [people.v2.csv OTUS]({seed["source_url"]}). Сохранены реальные имя, фамилия,
дата рождения и город; пол — `unspecified`, биография техническая.
SHA-256 исходного файла: `{seed["source_sha256"]}`.

В первом проходе прочитано {seed["valid_source_rows_first_pass"]:,} корректных строк,
пропущено {seed["skipped_source_rows"]} повреждённых;
{seed["repeated_rows"]} анкет взяты повторно с начала файла. Для каждой строки создан отдельный
детерминированный UUIDv5, поэтому результат — ровно {seed["rows"]:,} отдельных записей.
Различных имён: {seed["unique_first_names"]}, фамилий: {seed["unique_last_names"]}.
Частотность исходных имён и фамилий сохраняется;
добавленные строки составляют {100 * seed["repeated_rows"] / seed["rows"]:.3f}% массива.
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
{query_table}

Для каждой фазы и одновременности {", ".join(map(str, results["concurrency_levels"]))} проведено
**{results["repetitions"]} повтора**. Перед каждым — минимум один проход по всем запросам
и {results["warmup_seconds"]:g} с прогрева с одним клиентом; он не входит в статистику.
Измеряемое окно запуска запросов — {results["duration_seconds"]:g} с.
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

{table(results, "before")}

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

{table(results, "after")}

![Latency после индекса](latency-after.png)

![Throughput после индекса](throughput-after.png)

| Одновременность | Рост успешного throughput | Снижение p95 |
| ---: | ---: | ---: |
{gains}

Всего измерено **{total_attempts:,} HTTP-запросов**, из них **{total_errors:,} ошибок**;
максимум одновременно выполнявшихся клиентских запросов — **{peak}**.
При {min_c} клиенте преимущество индекса заметно без длинной очереди.
При {max_c} клиентах latency включает очередь, а throughput ограничивается ресурсами всего API,
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
{plan_summary}

{explain_text}

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
Максимальная загрузка процесса генератора: {max_client_cpu:.1f}% одного ядра;
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

- [results.json](results.json): каждый из {expected} прогонов, ошибки, времена, ресурсы и хеши.
- [summary.csv](summary.csv): компактная таблица для повторной обработки.
- [raw/](raw/): gzip CSV каждого HTTP-запроса, включая ошибки.
- [seed.json](seed.json): происхождение и состав данных.
- [explain/](explain/): планы до и после, размеры таблицы и индексов.
- Четыре графика сохранены в PNG и SVG; построены matplotlib из реальных результатов.

Перестроить отчёт и графики без нагрузки: `python -m benchmarks.report`.
"""
    (directory / "REPORT.md").write_text(report)
    print(f"Report and figures saved to {directory / 'REPORT.md'}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("reports/search"))
    build_report(parser.parse_args().directory)
