"""LLM-разбор идей игрока: сравнивает его замысел с тем, что требовала позиция.

Заметки к ходам («твоя идея») хранятся в ``move_notes``. Этот модуль собирает
факты из анализа (ход, потеря, лучший ход/линия) и текст игрока, а затем просит
LLM объяснить, где именно мышление разошлось с позицией. Только по фактам.
"""

from __future__ import annotations

import hashlib
from typing import Any

import chess

from . import facttext
from .llm import ChatMessage, LLMClient, clean_llm_text, looks_like_refusal, looks_like_thinking

__all__ = ["build_intent_messages", "review_intents", "intent_cache_key"]

_ERROR_CLASSES = frozenset({"blunder", "mistake", "inaccuracy"})
# Версия промпта: меняется вместе с формулировками, чтобы старый кеш не
# возвращал прежний водянистый ответ.
INTENT_REVIEW_VERSION = "v3"

_SYSTEM = (
    "Ты — шахматный тренер. Игрок уже САМ понял, что ошибся, и написал, о чём "
    "думал. НЕ пересказывай его мысль и не объясняй ему его же план.\n"
    "Структура ответа — ровно 4 короткие фразы, каждая с новой строки:\n"
    "1) Признай, что он сам увидел ошибку — это главное (без лести).\n"
    "2) Скажи, была ли его идея разумной в принципе, и почему (одной фразой).\n"
    "3) Назови КОНКРЕТНЫЙ ресурс/угрозу, которую он не учёл — по FEN и лучшей "
    "линии, с полями и фигурами, а не «не заметил тактику».\n"
    "4) Одно конкретное правило проверки на будущее, привязанное к этой "
    "позиции (что именно смотреть перед таким ходом).\n"
    "Запрещено: общие советы («будь внимательнее», «учитывай все фигуры»), "
    "повтор его текста, выдуманные варианты и оценки. Только факты из данных."
)


def intent_cache_key(game_id: str, notes: dict[int, str], analysis: dict) -> str:
    """Ключ кеша: партия + заметки + ходы анализа (стабильный хеш)."""
    moves = analysis.get("moves") if isinstance(analysis, dict) else []
    payload = repr((sorted(notes.items()), moves))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"intent_review:{INTENT_REVIEW_VERSION}:{game_id}:{digest}"


def _intent_items(
    game: Any,
    analysis: dict,
    notes: dict[int, str],
    *,
    max_moments: int = 8,
) -> list[dict[str, Any]]:
    """Ошибочные ходы с заметками + FEN/линия движка (без LLM)."""
    moves = analysis.get("moves") if isinstance(analysis, dict) else []
    if not isinstance(moves, list):
        moves = []
    items: list[dict[str, Any]] = []
    previous: set[str] = set()
    board = chess.Board()
    for ply, san in enumerate(getattr(game, "moves", None) or []):
        entry = moves[ply] if ply < len(moves) else None
        note = (notes or {}).get(ply)
        if (
            note
            and isinstance(entry, dict)
            and entry.get("classification") in _ERROR_CLASSES
            and board.fen() not in previous
            and len(items) < max_moments
        ):
            previous.add(board.fen())
            items.append(
                {
                    "ply": ply + 1,
                    "san": entry.get("san") or str(san),
                    "classification": entry.get("classification"),
                    "drop": entry.get("drop"),
                    "best": entry.get("best_move_san"),
                    "best_line": entry.get("best_line") or [],
                    "fen": board.fen(),
                    "idea": str(note).strip(),
                }
            )
        try:
            board.push_san(str(san))
        except ValueError:
            break
    return items


def build_intent_messages(
    game: Any,
    analysis: dict,
    notes: dict[int, str],
    *,
    max_moments: int = 8,
) -> list[ChatMessage]:
    """Собирает промпт для LLM (совместимость); review_intents его не вызывает."""
    items = _intent_items(game, analysis, notes, max_moments=max_moments)
    color = "белые" if getattr(game, "user_color", "white") == "white" else "чёрные"
    lines = [
        f"Партия: ты играл за {color}; дебют: {getattr(game, 'opening', None) or '—'} "
        f"({getattr(game, 'eco', None) or '—'}); результат: "
        f"{getattr(game, 'result_for_user', None) or '—'}."
    ]
    for item in items:
        line_text = facttext.full_line(item.get("best"), item["best_line"])
        lines.append(
            f"Позиция перед ходом (FEN): {item['fen']}\n"
            f"Сыграно: {item['san']} ({item['classification']}, потеря "
            f"{item['drop'] if item['drop'] is not None else '—'} win%).\n"
            f"Замысел игрока: «{item['idea']}».\n"
            f"Лучший ход движка: {item['best'] or '—'}"
            + (f", продолжение: {line_text}" if line_text else "")
            + "."
        )
    if not items:
        lines.append("Замыслов к ошибочным ходам нет.")
    lines.append("Ответь строго по структуре из 4 фраз, с конкретными полями и фигурами.")
    return [ChatMessage("system", _SYSTEM), ChatMessage("user", "\n".join(lines))]

def review_intents(
    game: Any,
    analysis: dict,
    notes: dict[int, str],
    *,
    max_tokens: int = 900,
) -> str:
    """Детерминированный разбор идей по фактам движка.

    LLM здесь не используется: 2600-игрок не должен ловить выдуманные поля
    и варианты. Аргумент ``max_tokens`` сохранён для совместимости вызовов.
    """
    items = _intent_items(game, analysis, notes)
    if not items:
        return "К ошибочным ходам нет заметок — разбирать нечего."

    lines: list[str] = ["Ты сам увидел ошибку — это главное."]
    for item in items:
        label = facttext.CLASS_RU.get(str(item["classification"]), "ошибка")
        drop = facttext.fmt_num(item.get("drop"))
        best = str(item.get("best") or "").strip()
        line_text = facttext.full_line(best, item.get("best_line"))
        best_fact = facttext.move_fact(item.get("fen"), best) if best else None
        idea = item["idea"].replace("\n", " ").strip()
        if len(idea) > 180:
            idea = idea[:177].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"

        lines.append(
            f"Ход {item['ply']} — {item['san']} ({label}"
            + (f", потеря {drop}% win" if drop else "")
            + ")."
        )
        lines.append(f"Твоя идея: «{idea}».")
        engine_line = f"Движок видел {best}" if best else "Движок видел другой ресурс"
        if best_fact:
            engine_line += f" — {best_fact}"
        if line_text:
            engine_line += f"; линия: {line_text}"
        lines.append(engine_line + ".")
        lines.append("")
    return "\n".join(lines).strip()
