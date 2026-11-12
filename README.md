# course-services

Учебный репозиторий backend-сервисов. `course-service` реализует API курсов,
заданий, сдач, ревью и артефактов на FastAPI. Сдачи проверяет отдельный
воркер через HTTP-сервис.

## Контракт API (лабораторная № 2)

[OpenAPI YAML](course-service/openapi.yaml) описывает целевой API курсов,
заданий, сдач, ревью и артефактов.

## Состав проекта

- `course-service/src/` содержит маршруты, бизнес-логику, модели БД и воркер.
- `course-service/alembic/` содержит миграции PostgreSQL.
- `course-service/mock_checker.py` имитирует внешний сервис проверки.
- `course-service/scripts/` содержит загрузку демонстрационных данных и эксперимент.
- `course-service/tests/` содержит HTTP-тесты и проверки БД.
- `bff/`, `code-coordinator/` и `plagiarism-service/` пока содержат заготовки.

## Локальная установка

Требуются Python 3.14 или новее и uv.

Если uv ещё не установлен:

```bash
python3 -m pip install uv==0.12.17
uv -V
```

Склонируйте репозиторий и перейдите в каталог приложения:

```bash
git clone git@github.com:yyeart/course-services.git
cd course-services/course-service
uv sync --locked --group dev
```

Все следующие команды выполняйте из `course-service/`

## Настройка окружения и БД

Создайте локальный файл настроек:

```bash
cp .env.example .env
```
Заполните `.env`. Значения пользователя, пароля и имени БД должны совпадать
в `POSTGRES_*` и `DATABASE_URL`. Пример для локального запуска:

```dotenv
APP__TITLE=Course service title
APP__DESCRIPTION=Course service description
APP__SUMMARY=Course service summary
APP__VERSION=0.1.0
APP__DEBUG=false
POSTGRES_USER=course
POSTGRES_PASSWORD=local-password
POSTGRES_DB=course
DATABASE_URL=postgresql+asyncpg://course:local-password@127.0.0.1:5432/course
JWT_SECRET=replace-with-a-long-random-secret-for-local-development
CHECKER_URL=http://127.0.0.1:8889/check
CHECKER_TIMEOUT_SECONDS=5
CHECKER_MAX_CONCURRENCY=5
CHECKER_MAX_ATTEMPTS=3
WORKER_POLL_INTERVAL_SECONDS=1
WORKER_LEASE_SECONDS=30
```

Запустите PostgreSQL, примените миграцию и загрузите демонстрационные записи:

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run python -m scripts.seed_demo
```

Seed-команда выводит UUID администратора, преподавателя, студента и курса.
Она добавляет записи только в локальную БД.

## Запуск приложения и проверки

Запустите три процесса в отдельных терминалах из `course-service/`.
Сначала внешний mock:

```bash
uv run uvicorn mock_checker:app --host 127.0.0.1 --port 8889
```

Затем API:

```bash
uv run uvicorn src.main:app \
  --host 127.0.0.1 \
  --port 8888 \
  --env-file .env \
  --workers 1 \
  --log-level info \
  --no-access-log \
  --log-config log-config.yaml
```

И воркер, который забирает задания из PostgreSQL и вызывает mock:

```bash
uv run python -m src.checker.worker
```

Проверить доступность API можно через `http://127.0.0.1:8888/docs`.
Защищённым маршрутам нужен подписанный Bearer JWT с `sub` в формате UUID,
ролью `admin`, `professor` или `student` и сроком действия `exp`.
Секрет подписи берётся из `JWT_SECRET`.

## Тесты

Сначала подготовьте `.env`, как описано выше.

Запустить тесты без интеграционных проверок БД:

```bash
uv run pytest
```

Для интеграционных тестов БД сначала запустите отдельный контейнер из
`course-service/`:

```bash
docker compose --profile test up -d postgres-test
RUN_DB_TESTS=1 uv run pytest
```

Тесты используют порт `5433` и применяют миграцию к тестовой БД. Остальные
проверки можно запускать без контейнера `postgres-test`. Настройка
`DATABASE_URL` должна указывать на локальную основную БД на порту `5432`.

## Проверка качества кода

Конфигурации Pylint, mypy, Ruff, Black, isort и Radon находятся в
[`pyproject.toml`](course-service/pyproject.toml).

Проверки без автоматического исправления исходников:

```bash
uv run pylint ./src --rcfile pyproject.toml
uv run mypy ./src --config-file pyproject.toml
uv run ruff check ./src --config pyproject.toml
uv run black --check --verbose ./src
uv run isort ./src --check
```

Применить сортировку импортов и форматирование:

```bash
uv run isort src tests
uv run black src tests
```

Посмотреть метрики кода:

```bash
uv run radon hal src
uv run radon cc src -s
uv run radon mi src -s
```

Посмотреть дерево зависимостей:

```bash
uv tree --locked
```
