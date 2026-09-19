# Chess Trainer

Персональный шахматный тренер: парсит партии с Lichess, анализирует через Stockfish,
объясняет ошибки с помощью LLM (OpenRouter/любой OpenAI-совместимый провайдер),
строит дебютный репертуар и генерирует дрели из собственных зевков.

Цель концепции — принцип 80/20: минимум времени, максимум роста (до уровня КМС).

## Статус

- [x] M0.0 Скелет проекта + окружение (git, venv, Stockfish)
- [ ] M0.A Клиент Lichess (выгрузка партий)
- [ ] M0.B Stockfish-анализ + классификация ходов
- [ ] M0.C SQLite-кеш
- [ ] M0.D CLI-отчёт
- [ ] M1 LLM-коуч (OpenRouter)
- [ ] M2 Слабости и дрели
- [ ] M3 Maia (человеческий слой)
- [ ] M4 Дебютный репертуар

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Stockfish: скачать с https://stockfishchess.org/download/windows/ и положить в
`C:\Users\huawei\Stockfish\` или указать `STOCKFISH_PATH`.

## Конфигурация

Все настройки через переменные окружения (см. `app/config.py`). Пример — файл `.env`.

## Использование

```powershell
python -m trainer coach --user YOUR_LICHESS_NICK
```