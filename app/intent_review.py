"""LLM-разбор идей игрока: сравнивает его замысел с тем, что требовала позиция.

Заметки к ходам («твоя идея») хранятся в ``move_notes``. Этот модуль собирает
факты из анализа (ход, потеря, лучший ход/линия) и текст игрока, а затем просит
LLM объяснить, где именно мышление разошлось с позицией. Только по фактам.
"""

from __future__ import annotations

import hashlib
from typing import Any

import chess

from .llm import ChatMessage, LLMClient, clean_llm_text, looks_like_refusal, looks_like_thinking

__all__ = ["build_intent_messages", "review_intents", "intent_cache_key"]

_ERROR_CLASSES = frozenset({"blunder", "mistake", "inaccuracy"})
# Версия промпта: меняется вместе с формулировками, чтобы старый кеш не
# возвращал прежний водянистый ответ.
INTENT_REVIEW_VERSION = "v2"

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


def build_intent_messages(
    game: Any,
    analysis: dict,
    notes: dict[int, str],
    *,
    max_moments: int = 8,
) -> list[ChatMessage]:
    """Собирает промпт: ошибки с замыслом игрока + лучшие ходы движка."""
    moves = analysis.get("moves") if isinstance(analysis, dict) else []
    if not isinstance(moves, list):
        moves = []
    color = "белые" if getattr(game, "user_color", "white") == "white" else "чёрные"
    lines = [
        f"Партия: ты играл за {color}; дебют: {getattr(game, 'opening', None) or '—'} "
        f"({getattr(game, 'eco', None) or '—'}); результат: "
        f"{getattr(game, 'result_for_user', None) or '—'}.",
    ]

    def _fen_before(ply: int) -> str:
        board = chess.Board()
        for san in (getattr(game, "moves", None) or [])[:ply]:
            try:
                board.push_san(str(san))
            except ValueError:
                return "—"
        return board.fen()

    previous: set[str] = set()
    shown = 0
    for ply, text in sorted((notes or {}).items()):
        if not text or shown >= max_moments:
            continue
        entry = moves[ply] if 0 <= ply < len(moves) else None
        if not isinstance(entry, dict):
            continue
        classification = entry.get("classification")
        if classification not in _ERROR_CLASSES:
            continue
        san = entry.get("san") or "?"
        drop = entry.get("drop")
        best = entry.get("best_move_san") or "—"
        best_line = entry.get("best_line") or []
        line_text = " ".join(str(x) for x in best_line[:5])
        fen = _fen_before(ply)
        if fen in previous:
            continue
        previous.add(fen)
        lines.append(
            f"Позиция перед ходом (FEN): {fen}\n"
            f"Сыграно: {san} ({classification}, потеря "
            f"{drop if drop is not None else '—'} win%).\n"
            f"Замысел игрока: «{text}».\n"
            f"Лучший ход движка: {best}"
            + (f", продолжение: {line_text}" if line_text else "")
            + "."
        )
        shown += 1
    if shown == 0:
        lines.append("Замыслов к ошибочным ходам нет.")
    lines.append(
        "Ответь строго по структуре из 4 фраз. Не повторяй текст игрока. "
        "Третья фраза должна называть конкретные поля/фигуры."
    )
    return [ChatMessage("system", _SYSTEM), ChatMessage("user", "\n".join(lines))]


def review_intents(
    game: Any,
    analysis: dict,
    notes: dict[int, str],
    *,
    max_tokens: int = 900,
) -> str:
    """Один вызов LLM; возвращает очищенный текст. Ошибки пробрасываются."""
    messages = build_intent_messages(game, analysis, notes)
    client = LLMClient()
    raw = client.chat(messages, max_tokens=max_tokens, temperature=0.2)
    text = clean_llm_text(raw)
    if looks_like_refusal(text) or looks_like_thinking(text):
        raise RuntimeError("LLM вернула служебный/отказной текст")
    if len(text.strip()) < 80:
        raise RuntimeError("LLM вернула слишком короткий разбор")
    return text.strip()
