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
- [ ] M1 LLM-коуч (OpenRouter)
- [ ] M2 Слабости и дрели
- [ ] M3 Maia (человеческий слой)
- [ ] M4 Дебютный репертуар

## Возможности M0

- Выгрузка рейтинговых партий с Lichess (`rapid/blitz/classical`).
- Анализ Stockfish: ACPL (потеря win% на ход), классификация ходов
  (best/good/inaccuracy/mistake/blunder по порогам Lichess), упущенные выигрыши.
- Локальная классификация дебютов по ходам партии
  (датасет [lichess-org/chess-openings](https://github.com/lichess-org/chess-openings), CC0).
- Кеш партий и анализа в SQLite: повторные отчёты мгновенны.
- Русскоязычный текстовый отчёт + JSON.

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
python -m trainer coach --user YOUR_LICHESS_NICK
```