# Социальная сеть (ДЗ 1)

Монолитное API на FastAPI и PostgreSQL 17 по заданию OTUS (курс Highload). Реализованы три метода из [спецификации](https://github.com/OtusTeam/highload/blob/master/homework/openapi.json), плюс `/user/me` для проверки токена и `/health`. ORM нет, SQL пишется напрямую через psycopg 3.

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

Compose поднимает PostgreSQL, одноразовым сервисом `migrate` накатывает миграцию из `migrations/`, потом запускает приложение. Данные лежат в volume `postgres_data`, порт БД наружу не публикуется. `docker compose down` данные сохраняет, `down -v` — удаляет вместе с БД. После правок кода — снова `docker compose up -d --build --wait`.

## Методы

- `POST /user/register` — регистрация, возвращает `{"user_id": "UUID"}`
- `POST /login` — вход по `id` и паролю, возвращает `{"token": "JWT"}`
- `GET /user/get/{id}` — анкета по id, без авторизации (так в исходной спецификации)
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

## Валидация и ошибки

- имя, фамилия, город — непустые, до 100 символов, пробелы по краям срезаются
- дата рождения — строго `YYYY-MM-DD`, от 1900-01-01 до текущей даты
- пол — `male` / `female` / `other` / `unspecified`; в спецификации OTUS этого поля нет, если не передано, сохраняется `unspecified`
- биография — до 5000 символов
- пароль — 8–128 символов при регистрации, 1–128 при входе; лишние поля отклоняются

Ошибки приходят как `{"message": "..."}`. Коды: 400 — невалидные данные, 401 — нет или битый токен у `/user/me`, 404 — нет анкеты, а при входе — неверный id или пароль (ответ одинаковый, у логина в исходном контракте 401 нет), 503 — БД недоступна (`Retry-After: 5`).

## Проверки

Тесты идут против настоящего PostgreSQL из compose:

```bash
docker compose --profile test run --rm tests
```

Покрыты основной сценарий, ошибки валидации, JWT (просроченный, подписанный другим секретом, другим алгоритмом), проверка, что SQL-инъекции сохраняются как обычный текст, и параллельные чтения. Тесты удаляют только созданных ими пользователей.

Линт там же: `ruff check app tests` и `ruff format --check app tests`.

Postman: импортировать оба файла из `postman/`, окружение **Social Network Local**, запускать шаги по порядку (регистрация и логин сами сохраняют `user_id` и `token`). Или через newman:

```bash
npx --yes newman run postman/social-network.postman_collection.json -e postman/local.postman_environment.json
```

## Запуск без Docker

Нужны Python 3.14 и PostgreSQL 17. Создать пустую БД, заполнить `DATABASE_URL` и `JWT_SECRET` в `.env` (либо оставить `DATABASE_URL` пустым и задать стандартные `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`):

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
- `app/security.py` — argon2 и JWT
- `app/config.py` — настройки из переменных окружения
- `app/migrate.py` — применение миграции
- `migrations/001_create_users.sql` — схема, одна таблица `users`

Пароли — Argon2id с независимой солью; для несуществующих id проверяется фиктивный хеш, чтобы время ответа не выдавало, есть ли пользователь. Токен — JWT HS256 с exp/iss/aud, при проверке алгоритм зафиксирован, TTL задаётся `TOKEN_TTL_SECONDS` (по умолчанию час). Секрет — минимум 32 байта, меняется в `.env`; смена секрета сбрасывает все выданные токены.

Все запросы с пользовательскими данными идут через параметры `%s`, строковой склейки в SQL нет. Соединения — из пула psycopg_pool. Индекс только под первичный ключ, дополнительных нет. Репликации, шардирования и кэшей нет — по заданию это пока монолит.
