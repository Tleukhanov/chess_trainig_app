"""M9: стартовая идентификация — FSM, валидация ника, resolve_identity.

Маленький явный автомат: состояния, таблица переходов и оркестратор ``run``.
Ввод/вывод инжектируются (``ask``/``notify``), поэтому в логике нет голого
``input()`` и тесты подставляют скриптованные ответы.

``resolve_identity`` связывает FSM с CLI: по ситуации «--user против
закреплённого» решает, спрашивать ли в TTY или молча работать от ``--user``
(не-TTY), и возвращает канонический ник для ``args.user``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from app.db import Database

NICK_RE = re.compile(r"^[A-Za-z0-9_-]{2,30}$")

# --- состояния автомата ---
START = "start"
ENTER_NICK = "enter_nick"
CONFIRM_NICK = "confirm_nick"
CHOOSE_USER = "choose_user"
CONFIRM_SWITCH = "confirm_switch"
DONE = "done"
ABORT = "abort"

# --- события ---
E_ENTER = "enter"
E_NICK_OK = "nick_ok"
E_INVALID = "invalid"
E_YES = "yes"
E_NO = "no"
E_SKIP = "skip"

# --- действия (side-effects, которые оркестратор превращает в вывод/БД) ---
A_NONE = None
A_BIND = "bind"  # записать users + current_user
A_SWITCH = "switch"  # сменить current_user
A_STOP = "stop"  # завершить без записи

TRANSITIONS: dict[tuple[str, str], tuple[str, str]] = {
    (START, E_ENTER): (ENTER_NICK, A_NONE),
    (ENTER_NICK, E_NICK_OK): (CONFIRM_NICK, A_NONE),
    (ENTER_NICK, E_INVALID): (ENTER_NICK, A_NONE),
    (CONFIRM_NICK, E_YES): (DONE, A_BIND),
    (CONFIRM_NICK, E_NO): (ABORT, A_STOP),
    (CONFIRM_NICK, E_INVALID): (CONFIRM_NICK, A_NONE),
    (CHOOSE_USER, E_SKIP): (DONE, A_NONE),
    (CHOOSE_USER, E_INVALID): (CHOOSE_USER, A_NONE),
    (CONFIRM_SWITCH, E_YES): (DONE, A_SWITCH),
    (CONFIRM_SWITCH, E_NO): (ABORT, A_STOP),
    (CONFIRM_SWITCH, E_INVALID): (CONFIRM_SWITCH, A_NONE),
}

_YES = {"д", "да", "y", "yes"}


def validate_nick(nick: str | None) -> str | None:
    """Канонический ник (с сохранением регистра) или None, если невалиден."""
    raw = (nick or "").strip()
    if not raw:
        return None
    return raw if NICK_RE.match(raw) else None


def handle(state: str, event: str) -> tuple[str, str]:
    """Шаг автомата: (новое состояние, действие)."""
    return TRANSITIONS.get((state, event), (state, A_NONE))


def _confirm(answer: str) -> str:
    return E_YES if answer.strip().lower() in _YES else E_NO


@dataclass
class _Picked:
    """Выбор, которым завершился FSM."""

    nick_lower: str
    nick: str
    action: str


def run(
    db: Database,
    *,
    ask: Callable[[str], str],
    requested: str | None = None,
    yes: bool = False,
    current: dict | None = None,
    users: list[dict] | None = None,
) -> _Picked | None:
    """Интерактивная завязка: кого использовать и кого закрепить.

    Возвращает выбор (ник + действие ``bind``/``switch``) или None при отказе.
    ``yes`` — не задавать подтверждающие вопросы (для ``--yes``/не-TTY).
    """
    cur = current if current is not None else db.get_current_user()
    known = users if users is not None else db.list_users()

    if requested:
        canon = validate_nick(requested)
        if canon is None:
            return None
        if cur and cur["nick_lower"] != canon.lower():
            # конфликт: спросить про переключение (если не запрещено)
            if not yes:
                answer = ask(
                    f"Закрепить {canon} вместо текущего {cur['nick']}? [д/Н]: "
                )
                if _confirm(answer) != E_YES:
                    return None
            return _Picked(canon.lower(), canon, A_SWITCH)
        if cur:
            return _Picked(cur["nick_lower"], cur["nick"], A_NONE)
        return _Picked(canon.lower(), canon, A_BIND)

    if cur:
        # закреплён есть: пустой ввод брать текущего
        if not yes:
            answer = ask(f"Кто ты? [{cur['nick']}]: ")
            nick = validate_nick(answer)
            if nick and nick.lower() != cur["nick_lower"]:
                return _Picked(
                    nick.lower(), nick, A_SWITCH
                )
        return _Picked(cur["nick_lower"], cur["nick"], A_NONE)

    if known:
        # юзеры есть, но закреплённого нет: нумерованный список
        if not yes:
            lines = "\n".join(
                f"  {i + 1}. {u['nick']}" for i, u in enumerate(known)
            )
            answer = ask(f"Кто ты?\n{lines}\n[номер или Enter]: ")
            pick = answer.strip()
            if pick:
                if not pick.isdigit() or not (1 <= int(pick) <= len(known)):
                    return None
                u = known[int(pick) - 1]
                return _Picked(u["nick_lower"], u["nick"], A_SWITCH)
        first = known[0]
        return _Picked(first["nick_lower"], first["nick"], A_SWITCH)

    # никого нет: ввод нового ника
    state, _ = handle(START, E_ENTER)
    assert state == ENTER_NICK
    while state not in (DONE, ABORT):
        if state == ENTER_NICK:
            if yes:
                return None  # без TTY и без --user нового ника не узнать
            answer = ask("Ник на lichess: ")
            nick = validate_nick(answer)
            if nick is None:
                state, _ = handle(ENTER_NICK, E_INVALID)
                continue
            state, _ = handle(ENTER_NICK, E_NICK_OK)
            continue
        if state == CONFIRM_NICK:
            if yes:
                state, _ = handle(CONFIRM_NICK, E_YES)
            else:
                answer = ask(f"Привязать ник {nick!r} как {nick}? [д/Н]: ")
                state, _ = handle(
                    CONFIRM_NICK,
                    E_YES if _confirm(answer) == E_YES else E_NO,
                )
            continue
    if state == ABORT:
        return None
    assert state == DONE
    return _Picked(nick.lower(), nick, A_BIND)  # type: ignore[possibly-undefined]


def apply_identity(
    db: Database, picked: _Picked | None
) -> None:
    """Записывает результат FSM в БД: bind нового или смена закреплённого."""
    if picked is None:
        return
    if picked.action == A_SWITCH:
        db.set_current_user(picked.nick)
    elif picked.action == A_BIND:
        db.set_current_user(picked.nick)


def resolve_identity(
    db: Database,
    args_user: str | None,
    *,
    isatty: bool,
    ask: Callable[[str], str],
    notify: Callable[[str], None],
    yes: bool = False,
) -> tuple[str | None, str | None]:
    """Для команды из списка «нужен юзер» возвращает (ник, ошибка).

    ``args_user`` может быть None; в не-TTY не вызывает ``ask``: либо тихо
    берёт закреплённого, либо при конфликте уведомляет в ``notify`` (stderr)
    и работает от ``--user``. Ошибка возвращается для аккуратного кода 1.
    """
    if args_user:
        canon = validate_nick(args_user)
        if canon is None:
            return None, f"невалидный ник: {args_user!r}"
        current = db.get_current_user()
        if current and current["nick_lower"] != canon.lower():
            confirmed = yes
            if isatty and not yes:
                answer = ask(f"Переключиться на {canon}? [д/Н]: ")
                confirmed = _confirm(answer) == E_YES
            if confirmed:
                db.set_current_user(canon)
            else:
                notify(
                    f"Закреплён {current['nick']}; работаем от --user {canon}."
                )
        return canon, None

    current = db.get_current_user()
    if current:
        if isatty and not yes:
            answer = ask(f"Кто ты? [{current['nick']}]: ")
            nick = validate_nick(answer)
            if nick and nick.lower() != current["nick_lower"]:
                db.set_current_user(nick)
                return nick, None
        return current["nick"], None

    # закреплённого нет: интерактивно — FSM, без TTY — честная ошибка
    if isatty and not yes:
        picked = run(db, ask=ask, requested=None, yes=False)
        if picked is None:
            return None, "отказался привязывать ник (нужен --user или user add)"
        apply_identity(db, picked)
        return picked.nick, None
    return None, "укажи --user или user add"