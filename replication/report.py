"""Build the submission report only from recorded measurements."""

import json
import statistics
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from replication.common import NODES, REPORTS


def build_report():
    lines = [
        "# ДЗ №3. Репликация PostgreSQL",
        "",
        "Исходный код: "
        "[репозиторий](https://github.com/branchylog981/course-OTUS-HighLoad-2026-09). "
        "Запуск и воспроизведение: [replication/README.md](../../replication/README.md).",
        "",
        "## Конфигурация и маршрутизация",
        "",
        "PostgreSQL 17: один master и две физические hot standby, отдельные Docker volumes. "
        "`pg_basebackup -Xs -R` создаёт копию и `standby.signal`; WAL передаётся потоком. "
        "У каждой реплики свой `application_name`: `slave1`/`slave2`. "
        "Включены `wal_level=replica`, `max_wal_senders=10`, `hot_standby=on`, "
        "`wal_keep_size=512MB`. Слоты не используются: после отставания за пределы сохранённого "
        "WAL реплику потребуется пересоздать. Учебный стенд не использует Patroni.",
        "",
        "Роль `replicator` имеет LOGIN/REPLICATION; доступ к replication разрешён "
        "через SCRAM в `pg_hba.conf`. Фактические роли узлов, версия PostgreSQL, "
        "индексы и настройки read-кластера: [cluster.json](cluster.json).",
        "",
        "`UserRepository.get_profile()` (`/user/get/{id}` и `/user/me`) и `search()` "
        "(`/user/search`) получают соединение из `ReadRouter`: round-robin между двумя пулами "
        "по 5 соединений. У соединений установлено `read_only=True`, поэтому транзакции "
        "выполняются с `BEGIN READ ONLY`. Ошибка получения соединения переключает выбор на "
        "другую реплику; повтор уже начатой транзакции не выполняется. "
        "Пул записи — 10 соединений: регистрация и проверка пароля при входе используют master. "
        "При пустом `READ_HOSTS` все операции работают с master (исходный режим ДЗ 1–2).",
        "",
        "При асинхронной репликации чтение сразу после записи может вернуть старые данные "
        "или 404. Логин читает master и этой задержке не подвержен. "
        "Если обе реплики недоступны, чтение возвращает 503; "
        "автоматического переноса на master нет.",
        "",
        "```mermaid",
        "flowchart LR",
        "  Client --> API[FastAPI]",
        "  API -->|INSERT, login| M[PostgreSQL master]",
        "  API -->|SELECT READ ONLY| S1[slave1]",
        "  API -->|SELECT READ ONLY| S2[slave2]",
        "  M -->|Streaming WAL| S1",
        "  M -->|Streaming WAL| S2",
        "```",
    ]
    read_path = REPORTS / "reads.json"
    if read_path.exists():
        data = json.loads(read_path.read_text())
        groups = defaultdict(list)
        for run in data["runs"]:
            groups[run["stage"], run["concurrency"]].append(run)
        levels = data["concurrency"]
        lines += [
            "",
            "## План и результаты нагрузки на чтение",
            "",
            f"Дата начала (UTC): `{data['started_at_utc']}`. Данные: {data['rows']:,} анкет "
            "из people.v2.csv, составной B-tree индекс из ДЗ №2 сохранён. "
            "Состав массива и SHA-256 источника: [seed.json](seed.json).",
            "",
            "Смесь 50% `GET /user/get/{id}` и 50% `GET /user/search`. "
            "UUID существующих анкет выбираются детерминированно; для поиска чередуются "
            "частые, редкие и отсутствующие префиксы из ДЗ №2. "
            "Порядок — master, затем две реплики; фоновая запись в этом эксперименте отсутствует. "
            "Состав запросов, SHA-256 ответов, ресурсы и исходные показатели: "
            "[reads.json](reads.json).",
            "",
            f"Одновременность: {', '.join(map(str, levels))} клиентов; "
            f"{data['repetitions']} повтора на точку; перед каждым "
            f"{data['warmup_s']:g} с прогрева, "
            f"окно нагрузки {data['duration_s']:g} с. Closed-loop: клиент ждёт полного ответа. "
            "Keep-alive, timeout 15 с; начатые запросы дожидаются завершения. "
            "Latency включает полное чтение тела; RPS — успешные ответы / "
            "полное время с завершением "
            "очереди. Во время нагрузки каждый ответ проверяется по SHA-256 всего тела. "
            "Оба режима возвращают идентичные ответы. Сырые запросы — [raw/](raw/).",
            "",
            "Каждая БД ограничена 2 CPU / 2 GiB, shared_buffers=256MB; приложение — 2 CPU / 2 GiB. "
            "Все контейнеры на одном Docker-хосте. В обеих фазах запущены три БД, "
            "суммарный пул чтения — 10 соединений. Генератор выполняется на хосте. "
            "Это сравнение распределения SQL-нагрузки на данном стенде, "
            "а не доказательство линейного масштабирования на отдельных серверах.",
            "",
            "Таблица: медианы повторов; для RPS в скобках min–max. p95/p99 — медианы "
            "перцентилей отдельных повторов, не перцентили объединённой выборки.",
            "",
            "| Чтение | Клиенты | Метод | RPS (min–max) | p50, мс | p95, мс | p99, мс | Ошибки |",
            "|---|---:|---|---:|---:|---:|---:|---:|",
        ]
        for stage in ("master", "replicas"):
            for concurrency in levels:
                runs = groups[stage, concurrency]
                if not runs:
                    continue
                for kind in ("get", "search", "total"):
                    values = [r["endpoints"][kind] for r in runs]
                    rps = [v["rps"] for v in values]

                    def median(k, values=values):
                        return statistics.median(v[k] for v in values)

                    lines.append(
                        f"| {stage} | {concurrency} | {kind} | {statistics.median(rps):.1f} "
                        f"({min(rps):.1f}–{max(rps):.1f}) | {median('p50_ms'):.2f} | "
                        f"{median('p95_ms'):.2f} | {median('p99_ms'):.2f} | "
                        f"{sum(v['errors'] for v in values)} |"
                    )
        lines += [
            "",
            "![RPS и задержка](read-comparison.png)",
            "",
            "Сравнение медианного суммарного throughput:",
            "",
        ]
        for concurrency in levels:
            before_runs = groups["master", concurrency]
            after_runs = groups["replicas", concurrency]
            if before_runs and after_runs:
                before_rps = statistics.median(r["endpoints"]["total"]["rps"] for r in before_runs)
                after_rps = statistics.median(r["endpoints"]["total"]["rps"] for r in after_runs)
                change = 100 * (after_rps / before_rps - 1)
                lines.append(
                    f"- {concurrency} клиентов: {before_rps:.1f} → {after_rps:.1f} RPS "
                    f"({change:+.1f}%)."
                )
        lines += [
            "",
            f"В измеренных окнах проверено "
            f"{sum(r['endpoints']['total']['attempts'] for r in data['runs']):,} HTTP-ответов; "
            f"ошибок: {sum(r['endpoints']['total']['errors'] for r in data['runs'])}. "
            "Перенос чтения освобождает master от выбранных SELECT, "
            "но на этом стенде ускорения HTTP не получено. Причины различий производительности "
            "отдельно не профилировались; замер не позволяет приписать их только PostgreSQL.",
            "",
            "## Доказательство переноса чтения",
            "",
            "Для каждого прогона до/после измерены локальные `pg_stat_statements` на всех "
            "трёх узлах. Прогрев исключён. Считаются только SELECT двух API по форме SQL. "
            "Сценарий проверяет, что сумма SQL-вызовов совпадает с количеством успешных "
            "HTTP-ответов отдельно для каждого метода. "
            "`exec_ms` — сумма времени SQL для этих запросов, без времени HTTP и очередей.",
            "",
            "| Фаза | Узел | get SELECT | search SELECT | SQL execution, мс |",
            "|---|---|---:|---:|---:|",
        ]
        for stage in ("master", "replicas"):
            runs = [r for r in data["runs"] if r["stage"] == stage]
            for node in NODES:
                totals = {
                    k: sum(r["sql_delta"][node][k] for r in runs)
                    for k in ("get", "search", "exec_ms")
                }
                lines.append(
                    f"| {stage} | {node} | {totals['get']} | {totals['search']} | "
                    f"{totals['exec_ms']:.2f} |"
                )
        lines += [
            "",
            "В фазе replicas SELECT этих двух методов на master отсутствуют, "
            "обе реплики обслуживают запросы. Следовательно, чтение перенесено фактически. "
            "Изменение HTTP-производительности ограничено также FastAPI, сериализацией ответов "
            "и генератором; рост числа реплик сам по себе не гарантирует роста RPS.",
        ]
        fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
        for stage, label in (("master", "Master reads"), ("replicas", "Replica reads")):
            xs = [c for c in levels if groups[stage, c]]
            for ax, metric in zip(axes, ("rps", "p95_ms"), strict=True):
                ys = [[r["endpoints"]["total"][metric] for r in groups[stage, c]] for c in xs]
                medians = [statistics.median(v) for v in ys]
                ax.errorbar(
                    xs,
                    medians,
                    yerr=[
                        [m - min(v) for m, v in zip(medians, ys, strict=True)],
                        [max(v) - m for m, v in zip(medians, ys, strict=True)],
                    ],
                    marker="o",
                    capsize=4,
                    label=label,
                )
                ax.set_xscale("log")
                ax.set_xticks(levels, labels=list(map(str, levels)))
                ax.set_xlabel("Concurrent clients")
                ax.grid(alpha=0.3)
                ax.legend()
        axes[0].set_ylabel("Successful requests/s")
        axes[1].set_ylabel("p95 latency, ms")
        fig.tight_layout()
        for extension in ("png", "svg"):
            fig.savefig(REPORTS / f"read-comparison.{extension}", dpi=160)
        plt.close(fig)
    failures = []
    for mode in ("async", "quorum"):
        path = REPORTS / f"failover-{mode}.json"
        if path.exists():
            failures.append(json.loads(path.read_text()))
    if failures:
        lines += [
            "",
            "## Аварийная остановка и потери транзакций",
            "",
            "Для каждого режима используется отдельный пустой кластер. Генератор пишет "
            "в `replication_probe(run_id, seq, payload)`, одна строка и отдельный COMMIT "
            "на итерацию. Только после успешного возврата COMMIT клиент добавляет seq "
            "во внешний JSONL-журнал с flush/fsync. Генератор работает 8 с; после первых "
            "подтверждений и ещё 3 с master получает `docker compose kill -s SIGKILL master`. "
            "После аварии генератор завершает своё окно, ошибки подключения учитывает отдельно. "
            "До завершения генератора promotion не выполняется.",
            "",
            "В **async** перед записью принудительно отключены обе репликационные сети: "
            "это контролируемое отставание, при котором асинхронный master продолжает "
            "подтверждать COMMIT. После его аварии сети возвращены. "
            "Это демонстрация возможности потери при сочетании разрыва репликации "
            "и отказа master, а не утверждение о неизбежной потере при каждом отказе.",
            "",
            "В **quorum** сети исправны; `synchronous_standby_names = 'ANY 1 (slave1, slave2)'`, "
            "`synchronous_commit = on`. COMMIT подтверждается после локального flush "
            "и flush WAL как минимум одной из двух реплик: master + одна реплика = 2 из 3 узлов. "
            "`sync_state=quorum` обеих реплик проверен перед нагрузкой. "
            "Если обе реплики недоступны, подтверждения записи блокируются. "
            "`on` гарантирует remote flush, но не немедленную видимость на чтении "
            "(для этого нужен `remote_apply` и учёт того, какая реплика подтвердила запись).",
            "",
            "Перед promotion проверено, что старый master остановлен и WAL receivers отсоединены. "
            "Выбрана реплика с максимальным `pg_last_wal_receive_lsn()` (durable WAL); "
            "также сохранён replay LSN. `pg_promote(true, 60)` завершает recovery. "
            "У второй реплики изменён `primary_conninfo`, проверены streaming и достижение "
            "LSN нового master. Слоты/перезаливка для этой короткой аварии не требуются. "
            "Старый master остаётся выключенным; без `pg_rewind` или нового basebackup "
            "его нельзя возвращать как самостоятельный primary.",
            "",
            "| Режим | Попытки | Подтверждено клиенту | Сохранилось | "
            "Потеряно подтверждённых | Сохранилось без ACK | Новый master |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
        for e in failures:
            lines.append(
                f"| {e['mode']} | {e['attempts']} | {e['acknowledged_count']} | "
                f"{e['persisted_count']} | {len(e['lost_acknowledged'])} | "
                f"{len(e['persisted_without_ack'])} | {e['promoted']} |"
            )
        lines += [
            "",
            "Сравниваются **множества seq**, а не только количество строк. "
            "Потеря = ACK клиента минус строки нового master. "
            "Строки без клиентского ACK выделены отдельно: COMMIT мог состояться, "
            "а ответ потеряться при аварии. Они не считаются потерянными "
            "подтверждёнными транзакциями.",
            "",
            "| Режим | Кандидат | Receive LSN перед promotion | Replay LSN перед promotion |",
            "|---|---|---|---|",
        ]
        for e in failures:
            for node, s in e["candidates"].items():
                lines.append(f"| {e['mode']} | {node} | {s['receive_lsn']} | {s['replay_lsn']} |")
        lines += [
            "",
            "После переключения конфигурация API обновлена и контейнер приложения пересоздан "
            "с `--no-deps`, поэтому старый master не запускается. В обоих экспериментах "
            "регистрация, чтение анкеты, поиск и логин после переключения вернули HTTP 200. "
            "Синхронный режим на новом master сохранён с оставшейся репликой. "
            "Переключение ручное, автоматизировано учебным скриптом; во время отказа и "
            "переконфигурации есть перерыв обслуживания записи, нулевой downtime не заявляется.",
            "До promotion в обоих сценариях `/user/search` также вернул HTTP 200 "
            "при уже остановленном master (после восстановления сетей в async).",
            "",
            "Доказательства: [failover-async.json](failover-async.json), "
            "[failover-quorum.json](failover-quorum.json); клиентские итоги: "
            "[writes-async.json](writes-async.json), [writes-quorum.json](writes-quorum.json); "
            "внешние журналы ACK: [async](writes-async.jsonl), [quorum](writes-quorum.jsonl).",
        ]
    lines += [
        "",
        "## Источники",
        "",
        "- [Спецификация API "
        "OTUS](https://github.com/OtusTeam/highload/blob/master/homework/openapi.json).",
        "- [PostgreSQL 17: streaming и synchronous "
        "replication](https://www.postgresql.org/docs/17/warm-standby.html).",
        "- [Настройки репликации, ANY и "
        "synchronous_standby_names](https://www.postgresql.org/docs/17/runtime-config-replication.html).",
        "- "
        "[synchronous_commit](https://www.postgresql.org/docs/17/runtime-config-wal.html#GUC-SYNCHRONOUS-COMMIT).",
    ]
    (REPORTS / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(f"Report: {REPORTS / 'REPORT.md'}", flush=True)


if __name__ == "__main__":
    build_report()
