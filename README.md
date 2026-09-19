# Chess Trainer

Персональный шахматный тренер: парсит партии с Lichess, анализирует через Stockfish,
объясняет ошибки с помощью LLM (OpenRouter/любой OpenAI-совместимый провайдер),
строит дебютный репертуар и генерирует дрели из собственных зевков.

Цель концепции — принцип 80/20: минимум времени, максимум роста (до уровня КМС).

## Статус

- [x] M0.0 Скелет проекта + окружение (git, venv, Stockfish)
- [x] M0.A Клиент Lichess (выгрузка партий)
- [x] M0.B Stockfish-анализ + классификация ходов
- [x] M0.C SQLite-кеш
- [x] M0.D CLI-отчёт (`coach`)
- [x] M1 LLM-коуч (`review`: OpenRouter или любой OpenAI-compat, grounding по Stockfish)
- [x] M2 Слабости и дрели (`coach` — узоры/фазы, `drills` — PGN/JSON)
- [x] M3 Человеческий слой (`humanize`: Maia/MaiaLite, фильтр дрелей по вердикту)
- [ ] M4 Дебютный репертуар

## Возможности M0

- Выгрузка рейтинговых партий с Lichess (`rapid/blitz/classical`).
- Анализ Stockfish: ACPL (потеря win% на ход), классификация ходов
  (best/good/inaccuracy/mistake/blunder по порогам Lichess), упущенные выигрыши.
- Локальная классификация дебютов по ходам партии
  (датасет [lichess-org/chess-openings](https://github.com/lichess-org/chess-openings), CC0).
- Кеш партий и анализа в SQLite: повторные отчёты мгновенны.
- Русскоязычный текстовый отчёт + JSON.

## Возможности M2

- «Слабости по фазам»: распределение зевков/ошибок по дебюту, миттельшпилю и
  эндшпилю со средними потерями win% — добавляется в отчёт `coach`.
- «Узоры ошибок»: классификация каждой ошибки по мотиву
  (вилка, связка, висячая фигура, пропущенный мат) со статистıкой по встречаемости.
- Генерация дрелей из собственных ошибок: `python -m trainer drills --user NICK`
  выбирает позиции, где ты зевнул/ошибся при ясном перевесе, и собирает их в
  `data/drills.pgn` (можно импортировать в Lichess) или `.json` — каждая дрель
  с FEN, лучшим ходом движка и линией.

## Возможности M3

- Оценка «человечности» каждой ошибки: насколько её сделал бы человек твоего уровня.
  Вердикты: **неестественная** (явно исправимая тактика — тренировать в первую
  очередь) / **пограничная** / **естественная** (сам движок-человек её допустил).
- Два движка-политики:
  - `MaiaLite` (по умолчанию) — мягкое «человекоподобное» распределение Stockfish
    (температура `--temperature`, по умолчанию 15), работает прямо сейчас;
- реальная **Maia** (`--engine maia`) — подключается если задать `MAIA_PATH`
  на бинарь с maia-chess.net (иначе переключение на MaiaLite с предупреждением).
- `python -m trainer humanize` считает человечность всех твоих зевков/ошибок из
  кеша и пишет `data/humanity.json`, а `drills --verdict unnatural` собирает
  дрели только из самых «неестественных» промахов.

## Возможности M1

- LLM-коуч на любом OpenAI-совместимом API (по умолчанию OpenRouter; можно указать
  свою базу/модель — поддерживаются и локальные модели вроде Ollama).
- Отбор ключевых моментов партии (зевки/ошибки/упущенные выигрыши) и объяснение
  каждого на русском в 2–4 предложениях + итог по партии.
- Grounding: модель получает только факты движка (ход, оценки до/после, лучший ход
  и вариант, продолжение партии) и ей запрещено выдумывать оценки.
- `review --dry-run` печатает готовые промпты без обращения к API.

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Stockfish: скачать с <https://stockfishchess.org/download/windows/> и положить в
`C:\Users\huawei\Stockfish\` (путь по умолчанию указан в `app/config.py`) или
задать переменную `STOCKFISH_PATH`.

Если русский текст в консоли выводится кракозябрами — выполни `chcp 65001`
или запусти `python` с `PYTHONUTF8=1`.

## Конфигурация

Все настройки через переменные окружения (см. `app/config.py`). Пример — файл `.env`.

## Использование

```powershell
# Анализ Stockfish + текстовый отчёт (слабости по фазам, узоры ошибок)
python -m trainer coach --user YOUR_LICHESS_NICK

# Дрели из своих ошибок (PGN — импортируй на lichess.org/analysis)
python -m trainer drills --user YOUR_LICHESS_NICK --out data/drills.pgn
python -m trainer drills --user YOUR_LICHESS_NICK --out data/drills.json --min-drop 10

# Человеческий слой: человечность ошибок + дрели только «неестественных»
python -m trainer humanize --user YOUR_LICHESS_NICK
python -m trainer drills --user YOUR_LICHESS_NICK --verdict unnatural

# LLM-разбор ключевых моментов (из кеша; без ключа — только --dry-run)
python -m trainer review --user YOUR_LICHESS_NICK
python -m trainer review --user YOUR_LICHESS_NICK --dry-run   # промпты без вызова API
python -m trainer review --game GAME_ID                       # одна партия по id

# Тесты
python -m unittest discover -s tests -v
```

Для дрелей с полными линиями движка переанализируй кеш:
`python -m trainer coach --user NICK --refresh` (линии `best_line` появляются
только в анализах, сделанных после M1).

LLM-ключ: положи `LLM_API_KEY` в `.env` (корень репо). Опционально
`LLM_BASE_URL=https://openrouter.ai/api/v1` и `LLM_MODEL`.