# course-services

Учебный репозиторий backend-сервисов. Сейчас реализована основа `course-service`
на FastAPI (настройки окружения, фабрика приложения, роут состояния сервиса,
логирование и HTTP-тесты)

## Контракт API (лабораторная № 2)

[OpenAPI YAML](course-service/openapi.yaml) описывает целевой API курсов,
заданий, сдач, ревью и артефактов.

## Состав проекта

### course-service/
Приложение FastAPI, зависимости, настройки инструментов и тесты

### bff/
Заготовка сервиса .gitkeep

### code-coordinator/
Заготовка сервиса .gitkeep

### plagiarism-service/
Заготовка сервиса .gitkeep


```text
course-services/
├── README.md
├── .gitignore
├── bff/
├── code-coordinator/
├── plagiarism-service/
└── course-service/
    ├── .env.example
    ├── log-config.yaml
    ├── pyproject.toml
    ├── uv.lock
    ├── src/
    │   ├── __init__.py
    │   ├── main.py
    │   ├── core/
    │   │   ├── __init__.py
    │   │   ├── config.py
    │   │   └── services.py
    │   └── web/
    │       ├── __init__.py
    │       ├── app.py
    │       ├── lifespans.py
    │       ├── routers.py
    │       └── schemas.py
    └── tests/
        ├── __init__.py
        ├── conftest.py
        └── test_app.py
```

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

## Настройка окружения

Создайте локальный файл настроек:

```bash
cp .env.example .env
```
Перед запуском заполните `.env`, например:

```dotenv
APP__TITLE=Course service title
APP__DESCRIPTION=Course service description
APP__SUMMARY=Course service summary
APP__VERSION=0.1.0
APP__DEBUG=false
```

## Запуск приложения

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
