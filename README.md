# Chess Trainer

Персональный шахматный тренер для игроков **1600–2400**: забирает партии с Lichess,
считает Stockfish, объясняет ошибки без «воды», собирает дебютный репертуар,
готовит к сопернику и тренирует на твоих же зевках.

Веб-приложение и CLI работают с одним кешем: разбор в браузере сразу доступен
из терминала.

> Текущая версия: **0.9.21**. История изменений — [CHANGELOG.md](CHANGELOG.md).

## Что это

- **FastAPI + vanilla JS** — без сборки фронтенда и тяжёлых фреймворков.
- **SQLite** по умолчанию, **PostgreSQL** через `DATABASE_URL`.
- **Stockfish** — оценки, классификация ходов, варианты.
- **Maia-2 ONNX** — человеко-подобный соперник и оценка «человечности» ошибок.
- **LLM опционально** — только для тренерских текстов и идей, не для конкретных
  ошибок: причина хода строится из FEN/SAN/линии движка.

## Что умеет

### Веб

- вход/регистрация, несколько профилей, админ-когорта;
- дашборд, план дня, прогресс, задача дня;
- список партий и разбор с доской, графиком и комментариями;
- ошибки в разборе: **значок → класс хода → короткое «почему»**, цифры и PV —
  под «Детали движка»;
- отчёт по дебюту: лучший ход, линия, потеря win%, объяснение и переход к позиции;
- план недели с evidence: «в N% партий ошибки в фазе X» + конкретная партия/ход/потеря
  с переходом к позиции;
- реальный дебютный репертуар из твоих партий: первый ход, частые ответы собеседника, твой ответ и главная линия;
- тренер эндшпиля: эталонные позиции (Лусена, Филидор, оппозиция, треугольник,
  активный король, гонка пешек и др.) и эндшпильные ошибки из твоих партий;
  прогресс по источникам, решения, ссылка на партию и ход;
- тренажёр миттельшпиля;
- темы ходов в отчёте: тактические, позиционные и атакующие возможности с
  отдельными счётчиками возможностей/исполнения. Метки строятся
  детерминированно из Stockfish/PV и признаков позиции, LLM их не определяет;
- репертуар, подготовка к сопернику, человечность, FIDE, турнир;
- светлая/тёмная тема.

### Анализ

- Stockfish depth 14 / multipv 3 по умолчанию; профили `quick` (10/1),
  `standard` (12/2), `deep` (14/3).
- Классы ходов: `best/good/inaccuracy/mistake/blunder`, упущенные выигрыши.
- `avg_win_loss` — средняя потеря win% на ход; `avg_win_before` — средний win%
  перед ходом. Это не ACPL и не «точность»; старые ключи кеша читаются.
- Разбор дебюта считается **одним детерминированным проходом** Stockfish:
  depth 18, `threads=1`, без ограничения времени.

### Тренировка

- Дрели из своих ошибок → PGN/JSON для Lichess.
- Дрели под конкретного соперника: его ошибки + твои провалы в матчах с ним.
- Задача дня с проверкой ответа и объяснением после ответа.
- План и недельный фокус по слабым фазам/дебютам.
- Человечность ошибок: `natural / borderline / unnatural`; фильтр дрелей.

### Дебюты и соперники

- Личный репертуар по твоим партиям, слабые ветви, экспорт PGN/JSON.
- Maia-2 как соперник выбранной силы.
- Подготовка к сопернику: его репертуар, слабости, точки встречи.
- FIDE-профиль и тренд рейтинга через публичный Lichess-прокси, без токена.

### CLI

```bash
python -m trainer web
python -m trainer coach --user NICK
python -m trainer review --user NICK
python -m trainer drills --user NICK
python -m trainer prepare --user NICK --opponent RIVAL
python -m trainer humanize --user NICK
python -m trainer repertoire --user NICK
python -m trainer plan --user NICK
python -m trainer overview --user NICK
python -m trainer progress --user NICK
python -m trainer fide --id FIDE_ID
python -m trainer tournament --games "Ivan:white:win:2100"
python -m trainer user list
```

Полный список флагов: `python -m trainer <команда> --help`.

## Стек

| Слой | Что используется |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn |
| Frontend | HTML/CSS/JS без сборки |
| БД | SQLite или PostgreSQL (`DATABASE_URL`) |
| Движки | Stockfish, Maia-2 ONNX |
| LLM | любой OpenAI-совместимый API (OpenRouter по умолчанию) |
| Тесты | pytest |

## Быстрый старт

### Linux / macOS

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python tools/fetch_maia_onnx.py
python -m trainer web
```

Открой `http://127.0.0.1:8000`.

### Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python tools\fetch_maia_onnx.py
python -m trainer web
```

Если русский текст в консоли ломается — `chcp 65001` или `PYTHONUTF8=1`.

## Stockfish

Нужен бинарь Stockfish. Варианты:

- Linux: `sudo apt install stockfish`, затем `STOCKFISH_PATH=/usr/games/stockfish`;
- Windows/macOS: скачать с <https://stockfishchess.org/download/> и указать
  `STOCKFISH_PATH` в `.env`;
- если `STOCKFISH_PATH` не задан, приложение ищет `stockfish` в `PATH`.

Проверка:

```bash
stockfish --version
```

## Maia-2

Модель и словари скачиваются в `data/maia/`:

```bash
python tools/fetch_maia_onnx.py
```

Путь можно переопределить через `MAIA_ONNX_PATH` / `MAIA_DIR`. Модель нужна
для тренажёра дебютов и человечности; без неё остальной анализ работает.

## Конфигурация

Все настройки — через переменные окружения (см. `app/config.py`). Создай `.env`
в корне репозитория; **не коммить его**.

Минимально:

```dotenv
LLM_API_KEY=
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=openai/gpt-4o-mini

LICHESS_TOKEN=
STOCKFISH_PATH=/usr/games/stockfish
APP_TZ=Asia/Almaty
```

Основные переменные:

| Переменная | По умолчанию | Зачем |
|---|---|---|
| `LLM_API_KEY` | пусто | тренерские тексты; без ключа часть LLM-функций отключена |
| `LLM_MODEL` | `openai/gpt-4o-mini` | модель OpenAI-совместимого API |
| `LLM_BASE_URL` | OpenRouter | база API |
| `LICHESS_TOKEN` | пусто | приватные/лимитированные запросы Lichess |
| `STOCKFISH_PATH` | поиск в `PATH` | путь к бинарю |
| `STOCKFISH_DEPTH` | `14` | глубина анализа партии |
| `STOCKFISH_MULTIPV` | `3` | число линий |
| `STOCKFISH_THREADS` | `4` | потоки Stockfish |
| `STOCKFISH_HASH_MB` | `512` | hash Stockfish |
| `STOCKFISH_TIME_MS` | `3000` | лимит на позицию, мс |
| `APP_TZ` | `Asia/Almaty` | даты задачи/плана/прогресса |
| `DATABASE_URL` | пусто | PostgreSQL вместо SQLite |
| `ADMIN_NICKS` | пусто | seed-список админов |
| `ADMIN_BOOTSTRAP_PASSWORD` | пусто | первый админ на пустой БД |
| `ALLOW_LEGACY_CLAIM` | `0` | временный claim старых профилей без пароля |
| `LOGIN_RATE_MAX_ATTEMPTS` | `5` | неудачных попыток входа до блокировки |
| `LOGIN_RATE_WINDOW_SEC` | `900` | окно rate-limit логина, сек |
| `MAIA_ONNX_PATH` | `data/maia/maia2-rapid.onnx` | модель Maia |

### Админы

`ADMIN_NICKS=nick1,nick2` повышает **только уже существующие** профили при
старте. Для первого запуска на пустой базе:

```dotenv
ADMIN_NICKS=boss
ADMIN_BOOTSTRAP_PASSWORD=change-me-after-login
```

После входа смени пароль. `ALLOW_LEGACY_CLAIM=1` включай только на короткое
окно миграции.

## Запуск

```bash
# CLI + браузер
python -m trainer web

# без автозапуска браузера
python -m trainer web --port 9000 --no-browser

# напрямую Uvicorn (для сервера)
uvicorn app.api:create_api --factory --host 0.0.0.0 --port 8000
```

## Приватность и безопасность

Кратко (полное уведомление: `frontend/privacy.html`, открывается из футера приложения и со страницы «Профили»):

- **Пароли**: минимум 8 символов, PBKDF2-HMAC-SHA256 (соль, 200 000 итераций); сервер отклоняет короткие пароли при регистрации и смене.
- **Rate-limit входа**: не больше `LOGIN_RATE_MAX_ATTEMPTS` (по умолчанию 5) неудачных попыток за `LOGIN_RATE_WINDOW_SEC` (по умолчанию 900) секунд — отдельно по IP и по логину. Счётчики хранятся в meta-таблице той же БД (SQLite/PostgreSQL), а не в памяти процесса, поэтому лимит общий для всех процессов с одной базой и переживает рестарт. Успешный вход сбрасывает счётчики; при исчерпании лимита вход отвечает generic `429`. За reverse proxy IP соединения — это IP прокси, учитывайте при выборе порогов. Если процессы работают с разными SQLite-файлами, лимит действует в пределах каждого файла.
- **Скрытого трекинга нет**: приложение не отправляет pings о времени на сайте. «Время сегодня» — только часы Lichess из разобранных партий и минуты, введённые вручную.
- **Удаление аккаунта**: «Профили → Удалить аккаунт и данные» — необратимо, с подтверждением паролем (для профилей с паролем) и вводом логина. Админом чужой профиль удаляется без пароля. Через поддержку: tleukhanov0409@mail.ru.
- **LLM**: если задан `LLM_API_KEY`, тренерские тексты формирует OpenAI-совместимый API (по умолчанию OpenRouter, `LLM_MODEL`); в запросы попадают позиции (FEN), ходы, ники и фрагменты разбора — но не пароль и не его хеш. Per-user opt-out нет и не имитируется; чтобы полностью отключить LLM, не задавайте `LLM_API_KEY` (или оставьте пустым) — движковые разборы продолжат работать локально.
- **Контакт**: tleukhanov0409@mail.ru.

### Ручная приемка фронтенда (нет JS-тестов)

1. Регистрация с паролем короче 8 символов отклоняется сервером и подсвечивается в форме.
2. Пять неверных паролей подряд → ответ 429 с generic-текстом; верный пароль после этого тоже 429 до истечения окна; успешный вход сбрасывает счётчик.
3. «Профили → Удалить аккаунт и данные»: кнопка видна, требует ввод логина и (для профиля с паролем) верный пароль; после удаления сессия гаснет, данные из БД исчезли.
4. Футер и экран входа содержат ссылку «Приватность и условия», она открывается и содержит контакт tleukhanov0409@mail.ru.

## API

- Swagger/OpenAPI: `GET /api/docs`.
- Авторизация — cookie-сессия; приватные эндпоинты без неё возвращают 401/404.
- Партии: `GET /api/games`, `GET /api/games/{id}`, `GET /api/games/{id}/review`.
- Дебюты: `POST /api/opening/start`, `POST /api/opening/move`, `GET /api/opening`.
- Миттельшпиль: `POST /api/middlegame/session`, `GET /api/middlegame/session/{id}`.
- Долгие операции: `POST /api/{coach,review,mentor,prepare}/run` → `GET /api/jobs/{id}`.
- Админ: `GET /api/admin/cohort`, `GET /api/admin/cohort.csv`.

## Тесты

```bash
pip install pytest
pytest -q
```

На версии 0.9.17 — 1032 теста. Отдельные группы:

```bash
pytest tests/test_api.py -q
pytest tests/test_analyzer_lines.py -q
```

## Деплой

Требования к серверу для веба: **2 vCPU / 4 ГБ RAM / 40+ ГБ диска**,
постоянный диск под `data/` (SQLite и Maia). Stockfish + Maia вместе
чувствительны к RAM.

- **VPS**: Uvicorn/systemd или Docker + Caddy/Nginx для TLS; `data/` на
  постоянном диске; бэкап `data/trainer.db`.
- **PostgreSQL**: задай `DATABASE_URL`; SQLite тогда не используется.
- **PaaS с эфемерным диском** (Heroku eco и т.п.) без volume/Postgres не
  подходит: база и модель будут теряться.
- **Домен**: свой или временный `sslip.io`/DuckDNS/Cloudflare Tunnel.
- Секреты — только в env/секретах хостинга, не в git.

`Procfile`/`Aptfile`/`app.json` в корне — старые Heroku-заготовки; для VPS
удобнее обычный systemd/Docker-запуск.

## Метрики

- `avg_win_loss` — средняя потеря win% на ход игрока.
- `avg_win_before` — средний win% перед ходом игрока.
- `drop` — потеря win% на конкретном ходу.
- `best_line` — продолжение после лучшего хода, без самого лучшего хода.

Это не ACPL и не «точность»: ACPL считается в центпойнах, а win% — вероятность.
Старые записи кеша читаются без пересчёта.

## Структура

```text
app/            FastAPI, анализ, БД, дебюты, миттельшпиль, LLM
frontend/       SPA без сборки
data/           SQLite, Maia, пользовательские артефакты (не коммитить)
tests/          pytest
tools/          fetch_maia_onnx.py
__main__.py     CLI `python -m trainer`
```
