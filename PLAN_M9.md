# M9 — Мульти-пользователи: привязка профиля, FSM входа, персональные данные

План подготовлен по коду на `97a23fd` (v0.3.0, 309 тестов зелёные).
Репозиторий: `D:\все для разработки\проекты\chess_trainer`.

## Задача

Чтобы пользовались не только я. На старте программа спрашивает «Кто ты?»,
привязывает ник Lichess (+ FIDE ID), помнит выбор в SQL. Если пришлённый
`--user` отличается от закреплённого — спрашивает подтверждение. Вход
описан явной FSM. Все персональные файлы уходят в свои папки.

## Решения пользователя

1. **FSM** — только на стартовую идентификацию, без интерактивного меню команд.
2. **Конфликт `--user` vs закреплённый** — спросить в TTY («переключиться?»),
   в неинтерактивном режиме работать от `--user` и писать уведомление в stderr.
3. **Артефакты** — в `data/<ник>/` (+ фолбэк на старые общие файлы при чтении).
4. **id профиля** — ник Lichess + FIDE ID (поле в профиле, `fide` подставляет своё).

## Что ломает текущий код (найдено при изучении)

- `app/db.py:22` — `games.id TEXT PRIMARY KEY`, вставка через
  `ON CONFLICT(id) DO NOTHING` (`db.py:170`). Если два зарег. человека сознали
  друг друга на Lichess, партия второго молча теряется. Нужен PK `(id, user)`.
- `app/db.py:43` — у `analyses` FK `REFERENCES games(id) ON DELETE CASCADE`.
  С новым составным PK ссылка на `id` одна становится битой (SQLite даст
  «foreign key mismatch» при любой вставке в analyses) → FK снимаем.
  Общий `analyses` оставляем намеренно: анализ партии один, второй человек
  не пересчитывает — это выигрыш, а не компромисс.
- `app/db.py:188` `get_game(game_id)` после миграции может вернуть строку
  не того человека → нужен параметр `user`.
- Все артефакты — в общем `data/`:
  `__main__.py:661,742,829,908,1045,1171,1196,1223,1225,1253,1254,1333`,
  `app/fide.py:217-218` (`fide.json` перезаписывается при разных FIDE ID
  из-за проверки `_same_fide_id`, `fide.py:165`).
- `input()` в проекте нет ни одного (`tournament` — только флаги), то есть
  первая интерактивная фича. Консоль cp1251 → в промптах только символы
  без `→` (использовать `-`/`->`), иначе `UnicodeEncodeError` при вводе.

## Дизайн

### 1. Схема БД и миграция (`app/db.py`)

Новые таблицы (добавляются в `_SCHEMA`, идемпотентно):

```sql
CREATE TABLE IF NOT EXISTS users(
    nick_lower TEXT PRIMARY KEY,   -- как ввели, в нижнем регистре (ключ поиска)
    nick TEXT NOT NULL,            -- каноническое написание из games.user / Lichess
    fide_id TEXT,                  -- может быть NULL
    bound_at INTEGER,              -- когда привязан
    last_seen_at INTEGER           -- последний запуск
);
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT
);
```

`meta["current_user"]` = канонический ник.

Миграция `_migrate_v3(conn)` — вызывается из `init_db()` после `executescript`,
идемпотентная, определяется по тексту DDL в `sqlite_master`:

1. `PRAGMA foreign_keys=OFF` (снять ВНУТРИ миграции на своём соединении).
2. Если в `sql` таблицы `games` нет `(id, user)`:
   - `CREATE TABLE games_new(... тот же список колонок ..., PRIMARY KEY(id,user))`
   - `INSERT INTO games_new SELECT * FROM games`
   - `DROP TABLE games; CREATE TABLE games(<старый DDL с новым PK>); INSERT ...; DROP games_new`
3. Если в `sql` таблицы `analyses` есть `REFERENCES games`:
   - пересобрать `analyses` тем же способом БЕЗ FK (PK `game_id` остаётся).
4. Бэкфилл юзеров:
   `INSERT OR IGNORE INTO users(nick_lower, nick, bound_at)
    SELECT lower(user), user, strftime('%s','now') FROM games GROUP BY user`
   → Tleukhanov зарегистрируется сам, никуда не вводить руками.
5. `meta["current_user"]` = самый активный из бэкфилла (максимум `max(created_at)`),
   если ещё не задан.
6. Перенос старых артефактов: если ровно один бэкфилленный юзер и
   `data/{humanity,repertoire,progress,mentor}.json` + `repertoire_*.pgn`
   лежат в корне — **переместить** их в `data/<ник>/` (копий не остаётся,
   чтение падать не может). Если бэкфилл дал >1 юзера — файлы не трогаем,
   работает фолбэк на чтение.

`Database` получает методы:
`upsert_user`, `list_users`, `get_user(nick)`, `get_current_user`,
`set_current_user(nick)`, `set_user_fide(nick, fide_id)`,
`touch_user(nick)` (last_seen_at).

`get_game(game_id, user=None)`: с `user` — фильтр по человеку; без — первая
строка (совместимость для `coach --game` без `--user`, `__main__.py:480,487`).

### 2. FSM входа (`app/identity.py`, новый)

Маленький явный автомат + инжектируемые `prompt`/`echo` (тесты подставляют
скриптованные ответы, никакого `input()` в логике).

Состояния: `START`, `ENTER_NICK`, `CONFIRM_NICK`, `CHOOSE_USER`,
`CONFIRM_SWITCH`, `DONE`, `ABORT`.
Переходы — таблица `TRANSITIONS: dict[tuple[state, event], state]`;
`handle(state, event, payload) -> (new_state, action)`; `run()` крутит цикл
и переводит `action` в вывод/запись в БД. Валидация ника: `^[A-Za-z0-9_-]{2,30}$`
(правила Lichess), иначе событие `invalid` → остаёмся в текущем состоянии.

Поведение:
- юзеров нет → `ENTER_NICK` → `CONFIRM_NICK` («Привязать tleukhanov как
  Tleukhanov? [д/Н]») → `DONE` (+ запись в `users`, `current_user`).
- юзеры есть → `CHOOSE_USER` (нумерованный список, Enter = текущий)
  или `CONFIRM_SWITCH` при конфликте.
- ввод пустой Enter → брать текущего, не спрашивать повторно.

### 3. Разрешение идентичности в CLI (`__main__.py`)

В `main()` после `parse_args`, до dispatch, для команд, которым нужен юзер
(`coach, review, drills, humanize, repertoire, overview, plan, mentor,
progress, prepare`; `fide` — опционально, `tournament` — не нужен):

| ситуация | TTY | не-TTY (скрипт/тест) |
|---|---|---|
| есть `--user`, совпал с закреплённым | без вопросов | то же |
| есть `--user`, отличается | спросить «переключиться?» | уведомление в stderr + работать от `--user` |
| нет `--user`, закреплён есть | промпт «Кто ты? [текущий]:» (Enter = да) | тихо взять закреплённого |
| нет `--user`, закреплённого нет | FSM | ошибка «укажи --user или user add» |

`args.user` выставляется каноническим ником до вызова команд, вся
дальнейшая логика не меняется. Новые флаги в корень: `--user` уже есть на
подкомандах (оставляем), добавляем `--yes` (неинтерактивно соглашаться на
переключение) и `user`-подкоманду.

### 4. Подкоманда `user`

```
python -m trainer user list          # ник, FIDE, партий, анализов, привязка, *
python -m trainer user whoami        # текущий закреплённый
python -m trainer user add NICK [--fide ID]
python -m trainer user switch NICK   # закрепить (через тот же FSM-подтверждение)
```
`*` — метка `current_user`. Без NICK — интерактивный ввод (FSM).

### 5. Персональные пути (`app/paths.py`, новый)

```python
def artifact(name, user) -> Path          # data/<user_lower>/<name>, mkdir
def artifact_read(name, user) -> Path|None# своё, иначе data/<name> (фолбэк), иначе None
def drills_dir(user) -> Path              # data/<user_lower>/
```
Заменить все `settings.data_dir / "..."` в `__main__.py` (12 мест) и
`app/fide.py:217`. Явные флаги (`--out/--json/--pgn/--humanity/--drills-dir`)
сохраняют приоритет — не трогаем. `humanity`, `repertoire`, `progress`,
`mentor`, `drills`, `fide.json` читаем через `artifact_read`, пишем через
`artifact`.

`fide`: `args.id` → профиль `users.fide_id` → `settings.fide_id`; если `--id`
дан и юзер известен — сохранить в профиль (`set_user_fide`).

### 6. Каскад по `__main__.py`

- `main()`: блок `resolve_identity(args)` + dispatch + `touch_user`.
- Гонки/порядок: `drills --opponent` (`__main__.py:682`) — свои партии берутся
  от разрешённого юзера, партии соперника по-прежнему `games.user = соперник`
  (это не профиль, бэкфиллить не надо).
- `_cmd_coach` фолбэк `args.user = "?"` (`__main__.py:477`) остаётся, но
  идентичность разрешается раньше и обычно `?` уже не нужен.
- Вывод приветствия в начале консоли: `Кто ты: Tleukhanov (FIDE -)` — коротко,
  только в интерактивном режиме, чтобы не мусорить в логах тестов.

## Файлы

| файл | что делаем |
|---|---|
| `app/db.py` | `_migrate_v3`, таблицы `users`/`meta`, методы профиля, `get_game(user=)` |
| `app/identity.py` | новый: FSM + валидация ника + `resolve_identity` |
| `app/paths.py` | новый: `artifact` / `artifact_read` / `drills_dir` |
| `app/fide.py` | `_cache_path(user)`; дефолт id из профиля |
| `__main__.py` | `--yes`, подкоманда `user`, блок идентичности в `main()`, 12 путей артефактов |
| `README.md` | раздел «Мульти-пользователи», `v0.4.0` |
| `tests/test_db_users.py` | новый |
| `tests/test_identity.py` | новый (FSM) |
| `tests/test_identity_cli.py` | новый (CLI + изоляция двух людей) |
| `tests/test_paths.py` | новый (фолбэк чтения) |

## Тесты

1. **Миграция**: в фикстуре собрать старую схему (`id PK`, FK), скормить
   `_migrate_v3` → PK `(id,user)`, все строки на месте, FK ушёл, `users`
   заполнены, повторный запуск ничего не ломает. Отдельно: два человека с
   одной `game id` обе сохраняются.
2. **FSM**: пусто→ввод→подтверждение; юзеры→список; отказ («нет») не
   привязывает; невалидный ник не уходит в состояние `DONE`; `--yes` без
   ввода; не-TTY не вызывает `prompt`.
3. **CLI**: `drills` без `--user` при закреплённом берёт его;
   `--user другой` в не-TTY → stderr-уведомление и работа от него;
   `user list/add/switch/whoami`; `fide` подставляет свой ID.
4. **Пути**: чтение фолбэка на общий файл; запись всегда в свою папку;
   два юзера не затирают `humanity.json`.
5. **Регресс**: все текущие 309 тестов проходят без изменений
   (они передают `--user` явно, значит FSM не сработает).

## Порядок работ

1. `app/db.py`: схема + миграция + тесты миграции (самый рискованный шаг,
   гоняем на копии реального `trainer.db` в tmp, оригинал не трогаем).
2. `app/paths.py` + перенос 13 точек артефактов + `app/fide.py`.
3. `app/identity.py` (FSM) + тесты.
4. `__main__.py`: `user`, `--yes`, `resolve_identity` в `main()`.
5. Полный suite, README, `v0.4.0`, smoke на копии реальной базы,
   коммит и пуш.

## Ограничения (вне M9)

- Нет ролями/разграничением прав: каждый видит кеш как есть — «только свои
  папки» это про удобство, не про безопасность. Все работают на одной машине.
- `--game` без `--user` по-прежнему берёт первую найденную партию по id.
- Логин/пароль, локализация интерфейса, перенос данных между машинами.
