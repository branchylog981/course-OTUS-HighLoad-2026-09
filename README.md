# Социальная сеть (ДЗ 1–4)

**ДЗ №4:** [лента постов друзей, демоданные и перестройка кэша](#дз-4-лента-постов-друзей).

**ДЗ №3:** [отчёт: репликация, нагрузка, авария master](reports/replication/REPORT.md).
Запуск стенда и повторение экспериментов — [replication/README.md](replication/README.md).

**ДЗ №2:** [отчёт: миллион анкет, поиск и нагрузка до/после индекса](reports/search/REPORT.md).
Воспроизведение замеров — [benchmarks/README.md](benchmarks/README.md).

Монолитное API на FastAPI и PostgreSQL 17 по заданию OTUS (курс Highload).
Реализованы регистрация, вход, получение и поиск анкет по
[спецификации](https://github.com/OtusTeam/highload/blob/master/homework/openapi.json),
плюс `/user/me` для проверки токена и `/health`. ORM нет, SQL пишется напрямую через psycopg 3.

## Запуск

Нужны Docker и Docker Compose v2.

```bash
cp .env.example .env
# вписать в .env свой JWT_SECRET (случайная строка от 32 символов, можно
# сгенерировать: python3 -c 'import secrets; print(secrets.token_hex(32))')
docker compose up -d --build --wait
```

- API: http://localhost:8080
- Swagger: http://localhost:8080/docs

Compose поднимает PostgreSQL и Redis. Сервис `migrate` применяет SQL-миграции перед запуском API. Данные PostgreSQL лежат в volume `postgres_data`, БД и Redis доступны во внутренней сети Compose. `docker compose down` сохраняет данные PostgreSQL, `down -v` удаляет их вместе с volume. После правок кода выполните `docker compose up -d --build --wait`.

## ДЗ 4. Лента постов друзей

`GET /post/feed?offset=0&limit=10` возвращает массив объектов с полями `id`, `text` и `author_user_id`. Нужен `Authorization: Bearer <token>` из `/login`. `offset` и `limit` принимают целые числа от 0 и 1 соответственно. В ленту входят до 1000 последних постов друзей, сначала по времени последнего изменения, затем по UUID в порядке убывания. Редактирование поднимает пост в ленте. При `offset >= 1000` API возвращает `[]`. Изменения между запросами могут сдвигать страницы при такой пагинации.

| Метод | Действие |
| --- | --- |
| `PUT /friend/set/{user_id}` или `PUT /friend/add/{user_id}` | Добавить автора в свой список друзей. Связь направленная |
| `PUT /friend/delete/{user_id}` | Удалить автора из списка |
| `POST /post/create` | Создать пост из `{"text":"…"}`, получить UUID как JSON-строку |
| `PUT /post/update` | Изменить свой пост из `{"id":"…","text":"…"}` |
| `PUT /post/delete/{id}` | Удалить свой пост |
| `GET /post/get/{id}` | Получить опубликованный пост без токена |

Маршруты используют [контракт OTUS](https://github.com/OtusTeam/highload/blob/master/homework/openapi.json).

### Демонстрация

После запуска Compose загрузите демоданные и получите токен наблюдателя. Команда `seed` добавляет четырёх пользователей, две связи и 1051 пост. Повторный запуск сохраняет существующие записи.

```bash
docker compose exec -T app python -m app.feed_admin seed
VIEWER=00000000-0000-0000-0000-000000004001
TOKEN=$(curl -fsS http://localhost:8080/login \
  -H 'Content-Type: application/json' \
  -d "{\"id\":\"$VIEWER\",\"password\":\"Feed-demo-2026\"}" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["token"])')

curl -fsS 'http://localhost:8080/post/feed?limit=3' -H "Authorization: Bearer $TOKEN"
curl -fsS 'http://localhost:8080/post/feed?offset=999&limit=2' -H "Authorization: Bearer $TOKEN"
```

Первая страница содержит посты №1050, №1049 и №1048. На позиции 999 находится пост №51, а пост №1051 принадлежит пользователю за пределами списка друзей.

### Кэш и его перестройка

- PostgreSQL хранит посты, список друзей и версию постов каждого автора. Триггер меняет версию в той же транзакции, что и пост. Ключ готовой ленты в Redis зависит от состава друзей и их версий. Поэтому после успешной записи первый новый запрос использует актуальный ключ. Ключи предыдущих версий истекают через 15 минут.
- Для прогретой ленты PostgreSQL читает только идентификаторы друзей и версии, а Redis отдаёт нужную страницу списка. При промахе приложение берёт до 1000 постов каждого друга из Redis, восстанавливает отсутствующие подборки из PostgreSQL и сливает их в список из максимум 1000 постов. Для холодного чтения версии и посты берутся из одного снимка основной БД, поэтому параллельная запись не может подменить актуальную ленту старой.
- После очистки или перезапуска Redis ленты заполняются при первом чтении. Для принудительной перестройки актуальной ленты и подборок её авторов можно прочитать данные из PostgreSQL командой ниже. При недоступном Redis `/post/feed` отвечает `503` с `Retry-After`, при этом операции с постами и друзьями продолжают записываться в PostgreSQL.

```bash
docker compose exec -T app python -m app.feed_admin rebuild "$VIEWER"
```

## Методы

- `POST /user/register` — регистрация, возвращает `{"user_id": "UUID"}`
- `POST /login` — вход по `id` и паролю, возвращает `{"token": "JWT"}`
- `GET /user/get/{id}` — анкета по id, без авторизации (так в исходной спецификации)
- `GET /user/search?first_name=Ив&last_name=Пет` — поиск по двум префиксам, результат по `id`
- `GET /user/me` — своя анкета, нужен `Authorization: Bearer <token>`
- `GET /health` — `{"status": "ok"}`, если БД отвечает

Регистрация:

```bash
curl -sS http://localhost:8080/user/register \
  -H 'Content-Type: application/json' \
  -d '{
    "first_name": "Иван",
    "second_name": "Петров",
    "birthdate": "1994-05-21",
    "gender": "male",
    "biography": "Книги, музыка, программирование",
    "city": "Москва",
    "password": "Demo-password-2026"
  }'
```

В `/login` передаётся `id` из ответа регистрации:

```bash
curl -sS http://localhost:8080/login \
  -H 'Content-Type: application/json' \
  -d '{"id": "USER_ID", "password": "Demo-password-2026"}'

curl -sS http://localhost:8080/user/get/USER_ID

curl -sS http://localhost:8080/user/me \
  -H 'Authorization: Bearer TOKEN'
```

В анкете нет пароля и его хеша, только поля профиля.

Поиск принимает **оба** обязательных параметра `first_name` и `last_name`
(именно `last_name`, как в спецификации; в таблице фамилия — `second_name`).
Он регистрозависимый и возвращает все совпадения по возрастанию UUID, без пагинации;
если совпадений нет — `200 []`. `%`, `_` и `!` во вводе ищутся буквально.
Пустые префиксы, нулевые байты и строки длиннее 100 символов возвращают `400`.

```bash
curl -sS --get http://localhost:8080/user/search \
  --data-urlencode 'first_name=Ив' --data-urlencode 'last_name=Пет'
```

## Валидация и ошибки

- имя, фамилия, город — непустые, до 100 символов, пробелы по краям срезаются
- дата рождения — строго `YYYY-MM-DD`, от 1900-01-01 до текущей даты
- пол — `male` / `female` / `other` / `unspecified`; в спецификации OTUS этого поля нет, если не передано, сохраняется `unspecified`
- биография — до 5000 символов
- пароль — 8–128 символов при регистрации, 1–128 при входе; лишние поля отклоняются

Ошибки приходят как `{"message": "..."}`. Коды: 400 — невалидные данные, 401 — нет или битый токен у `/user/me`, 404 — нет анкеты, а при входе — неверный id или пароль (ответ одинаковый, у логина в исходном контракте 401 нет), 503 — БД недоступна (`Retry-After: 5`).

## Проверки

Тесты используют PostgreSQL и Redis из Compose.

```bash
docker compose --profile test run --rm tests
```

Покрыты регистрация и вход, валидация, JWT, параллельные чтения, инвалидация ленты после правок постов и списка друзей, лимит кэша, перестройка и запись при сбое Redis. Тесты удаляют только созданных ими пользователей.

Линт там же: `ruff check app tests` и `ruff format --check app tests`.

Postman: импортировать оба файла из `postman/`, окружение **Social Network Local**, запускать шаги по порядку (регистрация и логин сами сохраняют `user_id` и `token`). Или через newman:

```bash
npx --yes newman run postman/social-network.postman_collection.json -e postman/local.postman_environment.json
```

## Запуск без Docker

Нужны Python 3.14, PostgreSQL 17 и Redis 7.4. Создайте пустую БД и заполните `DATABASE_URL`, `REDIS_URL` и `JWT_SECRET` в `.env`. Для подключения к PostgreSQL также можно оставить `DATABASE_URL` пустым и задать стандартные `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m app.migrate
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Тесты в другом терминале: `.venv/bin/python -m pytest -q`.

## Как устроено

- `app/api.py` — ручки FastAPI, зависимости, обработка ошибок
- `app/models.py` — pydantic-модели запросов и анкеты
- `app/repository.py` — SQL-запросы
- `app/posts.py`, `app/feed.py`, `app/feed_admin.py` — операции с постами и друзьями, кэш ленты и команды управления
- `app/security.py` — argon2 и JWT
- `app/config.py` — настройки из переменных окружения
- `app/migrate.py` — применение нумерованных идемпотентных SQL-миграций
- `migrations/001_create_users.sql` — первоначальная схема, таблица `users`
- `migrations/002_users_name_prefix.sql` — составной B-tree для префиксного поиска
- `migrations/003_friends_and_posts.sql` — друзья, посты и версии постов авторов
- `benchmarks/` — загрузчик миллиона анкет, HTTP-нагрузка и генерация отчёта

Пароли — Argon2id с независимой солью; для несуществующих id проверяется фиктивный хеш, чтобы время ответа не выдавало, есть ли пользователь. Токен — JWT HS256 с exp/iss/aud, при проверке алгоритм зафиксирован, TTL задаётся `TOKEN_TTL_SECONDS` (по умолчанию час). Секрет — минимум 32 байта, меняется в `.env`; смена секрета сбрасывает все выданные токены.

Все запросы с пользовательскими данными идут через параметры `%s`, строковой склейки в SQL нет.
Соединения — из пула psycopg_pool. В ДЗ №2 добавлен индекс
`(first_name varchar_pattern_ops, second_name varchar_pattern_ops)` для поиска.
Обычный запуск применяет все миграции. В стенде поиска индекс добавляется во второй фазе.
Для ДЗ №3 добавлен отдельный `compose.replication.yaml`: master и две физические
реплики PostgreSQL, READ ONLY маршрутизация двух GET API. Обычный compose
сохраняет запуск с одной БД.
