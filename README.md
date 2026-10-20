# Социальная сеть — ДЗ №1

Монолитное API на **Python 3.14 / FastAPI / PostgreSQL 17**: регистрация, вход и просмотр анкет. SQL выполняется напрямую через psycopg 3, ORM нет. Фронтенд не требуется для выполнения ДЗ; для ручной проверки доступен Swagger UI.

Контракт основан на [спецификации OTUS](https://github.com/OtusTeam/highload/blob/master/homework/openapi.json). Реализованы три метода задания. Дополнительно доступны авторизованный `/user/me` и проверка готовности `/health`.

## Запуск через Docker Compose

Понадобятся Docker Engine/Desktop с работающим daemon и Docker Compose v2+. В WSL включите интеграцию Docker Desktop с используемым дистрибутивом.

```bash
cp .env.example .env
python3 - <<'PY'
from pathlib import Path
import secrets
p = Path('.env')
p.write_text(p.read_text().replace(
    'replace-this-with-a-random-secret-at-least-32-characters',
    secrets.token_hex(32),
))
p.chmod(0o600)
PY
docker compose up -d --build --wait
```

Приложение: http://localhost:8080. Swagger UI: http://localhost:8080/docs. OpenAPI: http://localhost:8080/openapi.json. Готовность приложения и БД: http://localhost:8080/health.

Compose сначала ожидает PostgreSQL, затем выполняет SQL-миграцию отдельной одноразовой командой и запускает приложение. Это один монолит и одна БД. Данные сохраняются в Docker volume `postgres_data`. PostgreSQL не публикует порт на хост; API доступно на loopback-интерфейсе. Если порт занят, измените `APP_PORT` в `.env` и `base_url` в Postman.

```bash
docker compose ps -a
docker compose logs app migrate
docker compose down
```

`down` сохраняет данные. **`docker compose down -v` удаляет данные БД.** После изменения кода используйте `docker compose up -d --build --wait`. Миграция `001` идемпотентна (`CREATE TABLE IF NOT EXISTS`); она создает первоначальную схему, но не меняет структуру уже существующей таблицы. Последующие изменения схемы нужно оформлять отдельными миграциями.

## API

| Метод | Путь | Результат |
| --- | --- | --- |
| POST | `/user/register` | `200 {"user_id": "UUID"}` |
| POST | `/login` | `200 {"token": "JWT"}` |
| GET | `/user/get/{id}` | `200` и анкета, просмотр публичный |
| GET | `/user/me` | `200` и собственная анкета; нужен `Authorization: Bearer <token>` |
| GET | `/health` | `200 {"status": "ok"}`, если БД отвечает |

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

Логин — UUID, возвращенный при регистрации. Подставьте его вместо `USER_ID`:

```bash
curl -sS http://localhost:8080/login \
  -H 'Content-Type: application/json' \
  -d '{"id": "USER_ID", "password": "Demo-password-2026"}'

curl -sS http://localhost:8080/user/get/USER_ID

curl -sS http://localhost:8080/user/me \
  -H 'Authorization: Bearer TOKEN_FROM_LOGIN'
```

Анкета содержит `id`, `first_name`, `second_name`, `birthdate`, `gender`, `biography`, `city`. Пароль и его хеш никогда не возвращаются.

Правила ввода:

- Имя, фамилия и город — непустые строки до 100 символов; пробелы по краям удаляются.
- Дата рождения — строго `YYYY-MM-DD`, от `1900-01-01` до текущей даты UTC.
- `gender` — `male`, `female`, `other`, `unspecified`. Это расширение спецификации OTUS, в которой пол отсутствует. Если поле не передано, сохраняется `unspecified` для совместимости с исходными примерами.
- Интересы передаются в `biography`: до 5000 символов, пустая строка допустима.
- Пароль при регистрации — 8–128 символов, без обрезки пробелов или иных преобразований. Для входа допустимы 1–128 символов.
- Все поля регистрации, кроме `gender`, обязательны. Неизвестные поля отклоняются. Нулевые байты в полях анкеты запрещены.

Ошибки имеют вид `{"message": "Описание"}`:

| Код | Причина |
| --- | --- |
| 400 | Невалидный JSON, поля, дата или UUID |
| 401 | Отсутствующий, поддельный или просроченный токен для `/user/me` |
| 404 | Нет анкеты; при входе — неверный ID **или** пароль (одинаковый ответ) |
| 500 | Внутренняя ошибка без технических подробностей |
| 503 | БД недоступна, исчерпан пул соединений или истек тайм-аут; `Retry-After: 5` |

Код `404` для неверных учетных данных выбран по исходному контракту OTUS, где у `/login` нет ответа `401`. Публичный просмотр по ID также соответствует исходной спецификации. `/user/me` демонстрирует использование выданного токена.

## Postman

Импортируйте оба файла:

- [`postman/social-network.postman_collection.json`](postman/social-network.postman_collection.json)
- [`postman/local.postman_environment.json`](postman/local.postman_environment.json)

Выберите окружение **Social Network Local** и выполните коллекцию целиком через Collection Runner, в исходном порядке. Регистрация автоматически сохраняет `user_id`, вход — `token`. Коллекция проверяет успешный сценарий и ошибки, включая SQL-инъекции. Каждый запуск создает двух новых пользователей; данные предыдущего запуска не мешают проверкам.

Для запуска из терминала с Node.js/npm:

```bash
npx --yes newman run postman/social-network.postman_collection.json \
  -e postman/local.postman_environment.json
```

## Автоматические проверки

Интеграционные тесты используют **настоящий PostgreSQL** из Compose:

```bash
docker compose --profile test run --rm tests
```

Проверяются полный сценарий, отдельные соли Argon2id, сохранение SQL-инъекций как текста, неверные/просроченные JWT, валидация, параллельные чтения и ответ `503`. Тесты удаляют только созданных ими пользователей, без `TRUNCATE`. Postman-коллекция также проверяется на согласованность автоматически.

Для проверки стиля:

```bash
docker compose --profile test run --rm tests ruff check app tests
docker compose --profile test run --rm tests ruff format --check app tests
```

## Запуск Python без контейнера приложения

Нужны Python 3.14 и доступная PostgreSQL 17. Создайте пустую БД и пользователя, заполните `DATABASE_URL` и `JWT_SECRET` в `.env`. Пароль в URL должен быть percent-encoded, если содержит специальные символы. Альтернатива — пустой `DATABASE_URL` и стандартные переменные `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m app.migrate
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
```

В другом терминале: `.venv/bin/python -m pytest -q`. При таком запуске настройки читаются из `.env`. В Compose DSN формируется libpq из отдельных переменных PostgreSQL; поэтому пароль БД со специальными символами не требует URL-кодирования.

## Устройство и безопасность

```text
app/api.py           HTTP-методы, зависимости и обработка ошибок
app/models.py        Входные данные и публичная анкета
app/repository.py    SQL-запросы с параметрами %s
app/security.py      Argon2id, выпуск и проверка JWT
app/config.py        Переменные окружения
app/migrate.py       Применение первоначальной SQL-миграции
migrations/          Схема БД
tests/               Интеграционные проверки
postman/             Коллекция и локальное окружение
```

Пароли хешируются Argon2id с независимой случайной солью (`64 MiB`, 3 прохода, параллелизм 4). Исходные пароли не сохраняются. При несуществующем пользователе выполняется проверка фиктивного хеша, чтобы не пропускать дорогую операцию проверки пароля.

JWT подписывается HS256, содержит срок действия, ID пользователя, издателя и аудиторию; при проверке алгоритм зафиксирован. По умолчанию срок действия — 3600 секунд (`TOKEN_TTL_SECONDS`, допустимо 60–86400). JWT_SECRET должен содержать не менее 32 байт; используйте случайное значение. Токены не сохраняются в БД, выхода и отзыва отдельных токенов в этом ДЗ нет; смена секрета аннулирует все выданные токены.

Все SQL-запросы с данными используют связывание параметров psycopg; строковой подстановки в SQL нет. Статические DDL и `SELECT 1` не содержат пользовательского ввода. Соединения с БД берутся из ограниченного пула; это не кэш данных. Репликация, шардирование и кэширование не используются. Создается только индекс первичного ключа, автоматически необходимый PostgreSQL для уникальности ID; дополнительных индексов нет.

Пароли и токены не выводятся приложением в сообщения ошибок. `.env` исключен из Git и Docker build context. Для публичного размещения потребуется HTTPS и ограничение частоты попыток входа; текущая конфигурация предназначена для локальной сдачи ДЗ.

## Публикация на GitHub

После создания пустого репозитория на GitHub выполните из каталога проекта:

```bash
git init -b main
git add .
git commit -m "Implement social network homework 1"
git remote add origin https://github.com/YOUR_ACCOUNT/YOUR_REPOSITORY.git
git push -u origin main
```

Для сдачи приложите ссылку на репозиторий и файл Postman-коллекции из `postman/`. Локальный `.env` и виртуальное окружение в репозиторий не попадут.
