"""LLM-коуч: отбор ключевых моментов партии и построение объяснений.

Модуль самодостаточен: использует только стандартную библиотеку,
app.llm (LLMClient, ChatMessage) и app.games (Game). Входной анализ —
обычный dict, возвращаемый ``GameAnalysis.summary()`` (см. app.analyzer).
Записи ходов в ``moves`` анализируются через .get() и не требуют
обязательных ключей вроде best_line/played_line.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .games import Game
from .llm import ChatMessage, LLMClient

__all__ = [
    "SYSTEM_PROMPT",
    "select_key_moments",
    "build_request",
    "parse_game_answer",
    "run_coach",
    "played_line",
]

_DASH = "—"
_RESULT_RU = {"win": "победа", "draw": "ничья", "loss": "поражение"}
_COLOR_RU = {"white": "белые", "black": "чёрные"}
_ERROR_CLASSES = frozenset({"blunder", "mistake"})
_FALLBACK_LINE_RE = re.compile(r"^(?:Момент\s+)?(\d+)(?:\)|\.|:)\s*(.*)$")
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

SYSTEM_PROMPT = """Ты — шахматный тренер для сильного ученика (~2100, КМС). Говори кратко, по делу, по-русски.

Правила:
- Опирайся ТОЛЬКО на предоставленные факты: сыгранный ход, лучший ход движка, варианты, оценки до/после в win%. НЕ выдумывай оценок, матов, тактик и вариантов, которых нет в данных. НЕ вычисляй оценки сам.
- На каждый момент — 2-4 коротких предложения: почему ход ошибочен, чего игрок не увидел, на что обратить внимание, что менять в мышлении или паттерне.
- Без воды и без общих советов «решай больше тактики» на каждый чих.
- Summary — 2-4 предложения об игре в целом, про перспективу игрока (что застряло), без повторов деталей моментов."""


def _resolved(move: dict[str, Any] | None) -> dict[str, Any]:
    """Нормализует запись хода, защищаясь от отсутствующих ключей."""
    return move if move is not None else {}


def _fmt(value: float | None, digits: int = 1) -> str:
    """Форматирует число с одним знаком или ставит '—' для None."""
    if value is None:
        return _DASH
    return f"{value:.{digits}f}"


def _player_name(player: dict[str, Any] | None) -> str:
    """Имя игрока из словаря white/black партии (API или PGN), иначе '—'."""
    if not player:
        return _DASH
    user = player.get("user")
    if isinstance(user, dict) and user.get("name"):
        return str(user["name"])
    name = player.get("name") or player.get("username")
    return str(name) if name else _DASH


def _maybe_str(value: Any) -> str | None:
    """Возвращает value, только если это строка (иначе None)."""
    return value if isinstance(value, str) else None


def _json_load(raw: str) -> Any:
    """json.loads, что не бросает исключений (при провале — None)."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def select_key_moments(analysis: dict[str, Any], max_moments: int = 6) -> list[int]:
    """Отбирает ключевые моменты партии (полуходы игрока) для разбора.

    Кандидаты — ходы игрока с классификацией blunder/mistake либо входящие
    в blunders/mistakes/missed_wins. Сортировка по убыванию важности
    (blunder > mistake > прочее; внутри группы — по потере win.%), затем
    результат обрезается до max_moments и сортируется по возрастанию
    (порядок партии). Индексы — номера полуходов (ply) в ``moves``.
    """
    moves = analysis.get("moves") or []
    user_is_white = analysis.get("user_color") == "white"
    blunders = set(analysis.get("blunders") or [])
    mistakes = set(analysis.get("mistakes") or [])
    candidates = blunders | mistakes | set(analysis.get("missed_wins") or [])

    entries: list[tuple[int, int, float]] = []
    for ply in range(len(moves)):
        if (ply % 2 == 0) != user_is_white:
            continue
        move = _resolved(moves[ply])
        cls = move.get("classification")
        if cls not in _ERROR_CLASSES and ply not in candidates:
            continue
        if cls == "blunder" or ply in blunders:
            weight = 3
        elif cls == "mistake" or ply in mistakes:
            weight = 2
        else:
            weight = 1
        drop = float(move.get("drop") or 0.0)
        entries.append((ply, weight, drop))

    entries.sort(key=lambda item: (-item[1], -item[2]))
    selected = [item[0] for item in entries[:max_moments]]
    selected.sort()
    return selected


def played_line(game: Game, ply: int, limit: int = 4) -> list[str]:
    """САН-ходы, реально сыгранные после полухода ``ply`` (не более limit)."""
    return game.moves[ply + 1 : ply + 1 + limit]


def build_request(game: Game, analysis: dict[str, Any], max_moments: int = 6) -> tuple[list[ChatMessage], list[int]]:
    """Собирает (messages, idx) для LLM: системный промпт и моменты на русском.

    Если проблемных моментов нет, возвращает ([], []).
    """
    idx = select_key_moments(analysis, max_moments)
    if not idx:
        return [], []

    moves = analysis.get("moves") or []
    user_is_white = analysis.get("user_color") == "white"
    result = analysis.get("result_for_user") or game.result_for_user or "draw"
    opening = game.opening or _DASH
    lines = [
        f"Партия {_player_name(game.white)}—{_player_name(game.black)}, "
        f"ты — {_COLOR_RU['white' if user_is_white else 'black']}. "
        f"Результат: {_RESULT_RU.get(result, 'ничья')}. Дебют: {opening}. "
        f"Твой ACPL: {_fmt(analysis.get('acpl'))}, точность: {_fmt(analysis.get('accuracy'))}%."
    ]

    for number, ply in enumerate(idx, start=1):
        move = _resolved(moves[ply]) if ply < len(moves) else {}
        color = _COLOR_RU["white" if ply % 2 == 0 else "black"]
        block = (
            f"Момент {number} (полуход {ply + 1}, ход {color}): "
            f"сыграно {move.get('san') or _DASH}. "
            f"Было {_fmt(move.get('win_before'))}% → стало {_fmt(move.get('win_after'))}%, "
            f"потеря {_fmt(move.get('drop'))}% (пометка: {move.get('classification') or _DASH})."
        )
        best_move = move.get("best_move_san")
        best_line = move.get("best_line")
        if isinstance(best_line, list) and best_line:
            best_move = f"{best_move or _DASH} (линия: {' '.join(best_line)})"
        block += f"\nЛучший ход: {best_move or _DASH}."
        played = played_line(game, ply)
        block += f"\nВ партии далее: {' '.join(played) if played else _DASH}."
        lines.append(block)

    lines.append(
        'Ответь строго одним JSON-объектом: {"moments": {"<полуход>": "объяснение"}, '
        '"summary": "2-4 предложения об игре в целом"}. Summary — про перспективу игрока '
        '(что застряло), без повторов. Если для поля данные не идут — вставляй "—" или опускай.'
    )

    messages = [
        ChatMessage("system", SYSTEM_PROMPT),
        ChatMessage("user", "\n".join(lines)),
    ]
    return messages, idx


def _extract_json(text: str) -> dict | None:
    """Достаёт JSON-объект из текста: сперва ```-блоки, затем { ... }."""
    for raw in _FENCE_RE.findall(text):
        data = _json_load(raw)
        if isinstance(data, dict):
            return data
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        data = _json_load(text[start : end + 1])
        if isinstance(data, dict):
            return data
    return None


def parse_game_answer(text: str, moment_indices: list[int]) -> tuple[dict[int, str], str | None]:
    """Разбирает ответ модели в (объяснения по моментам, summary).

    Устойчив к ```-блокам, мусору вокруг JSON и отсутствию JSON вовсе
    (построчный fallback «Момент N: ...»/«N) ...»). Не бросает исключений:
    при полном провале — ({}, None). Отсутствующие в ответе моменты
    заполняются пустой строкой.
    """
    if not isinstance(text, str) or not text.strip():
        return {}, None

    data = _extract_json(text)
    if data is not None:
        raw_moments = data.get("moments")
        summary = _maybe_str(data.get("summary"))
        if not isinstance(raw_moments, dict):
            return {}, summary
        result: dict[int, str] = {}
        for key, value in raw_moments.items():
            try:
                ply = int(key)
            except (TypeError, ValueError):
                continue
            result[ply] = value if isinstance(value, str) else ("" if value is None else str(value))
        for ply in moment_indices:
            result.setdefault(ply, "")
        return result, summary

    matched: list[str] = []
    for line in text.splitlines():
        match = _FALLBACK_LINE_RE.match(line.strip())
        if match:
            matched.append(match.group(2).strip())
    if not matched:
        return {}, None
    result = {}
    for ply, content in zip(moment_indices, matched):
        result[ply] = content
    for ply in moment_indices:
        result.setdefault(ply, "")
    return result, None


def run_coach(game: Game, analysis: dict[str, Any], llm: LLMClient, max_moments: int = 6, temperature: float = 0.4) -> tuple[dict[int, str], str | None]:
    """Прогоняет партию через LLM-коуч и возвращает (объяснения, summary).

    Если объяснять нечего, сеть не вызывается и возвращается ({}, None).
    Ошибки LLM (RuntimeError) не перехватываются и уходят наверх.
    """
    messages, idx = build_request(game, analysis, max_moments)
    if not messages:
        return {}, None
    text = llm.chat(messages, temperature=temperature, json_mode=True, max_tokens=1200)
    return parse_game_answer(text, idx)