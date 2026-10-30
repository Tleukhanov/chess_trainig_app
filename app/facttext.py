"""Детерминированные пояснения по фактам движка.

Модуль не вызывает LLM и не додумывает шахматные причины. Он умеет только
то, что можно проверить по FEN и записи анализа: сыгранный ход, потерю win%,
лучший ход, линию движка и тип хода (взятие / шах / рокировка / ход фигурой).

Это страховка от «воды» и выдуманных вариантов в отчётах: если LLM не даёт
проверяемого текста, интерфейс показывает этот текст.
"""

from __future__ import annotations

from typing import Any

import chess

__all__ = [
    "PIECE_RU",
    "CLASS_RU",
    "fmt_num",
    "move_fact",
    "line_text",
    "error_text",
    "why_text",
    "opening_summary",
    "opening_recommendation",
    "task_text",
]

PIECE_RU: dict[int, str] = {
    chess.PAWN: "пешка",
    chess.KNIGHT: "конь",
    chess.BISHOP: "слон",
    chess.ROOK: "ладья",
    chess.QUEEN: "ферзь",
    chess.KING: "король",
}

# Падежи для фраз «взятие пешки», «ход конём» — иначе получается «взятие пешка».
PIECE_GEN: dict[int, str] = {
    chess.PAWN: "пешки",
    chess.KNIGHT: "коня",
    chess.BISHOP: "слона",
    chess.ROOK: "ладьи",
    chess.QUEEN: "ферзя",
    chess.KING: "короля",
}
PIECE_INSTR: dict[int, str] = {
    chess.PAWN: "пешкой",
    chess.KNIGHT: "конём",
    chess.BISHOP: "слоном",
    chess.ROOK: "ладьёй",
    chess.QUEEN: "ферзём",
    chess.KING: "королём",
}
# Винительный падеж для «Ты не забрал пешку/ладью/...».
PIECE_ACC: dict[int, str] = {
    chess.PAWN: "пешку",
    chess.KNIGHT: "коня",
    chess.BISHOP: "слона",
    chess.ROOK: "ладью",
    chess.QUEEN: "ферзя",
    chess.KING: "короля",
}

CLASS_RU: dict[str, str] = {
    "good": "ход без потери",
    "best": "лучший ход",
    "inaccuracy": "неточность",
    "mistake": "ошибка",
    "blunder": "зевок",
    "missed_win": "упущенный выигрыш",
}


def _num(value: Any, digits: int = 1) -> str | None:
    """Число в русской записи; None/мусор — None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return f"{float(value):.{digits}f}".replace(".", ",")


def fmt_num(value: Any, digits: int = 1) -> str | None:
    """Публичная обёртка над ``_num`` для текстовых модулей."""
    return _num(value, digits)


def move_fact(fen: str | None, san: str | None) -> str | None:
    """Что делает ход: взятие, шах, рокировка или ход фигурой на поле.

    Возвращает None, если FEN/SAN не разбираются. Никаких оценок и планов.
    """
    if not fen or not san:
        return None
    try:
        board = chess.Board(fen)
        move = board.parse_san(str(san))
    except (ValueError, TypeError):
        return None

    target = chess.square_name(move.to_square)
    if board.is_castling(move):
        return "рокировка"
    if board.is_en_passant(move):
        return f"взятие на проходе на {target}"
    if board.is_capture(move):
        captured = board.piece_at(move.to_square)
        piece = PIECE_GEN.get(captured.piece_type, "фигуры") if captured else "фигуры"
        promotion = ""
        if move.promotion:
            promotion = f" с превращением в {PIECE_RU.get(move.promotion, 'фигуру').lower()}"
        return f"взятие {piece} на {target}{promotion}"
    if board.gives_check(move):
        return f"шах на {target}"
    piece = board.piece_at(move.from_square)
    piece_name = PIECE_INSTR.get(piece.piece_type, "фигурой") if piece else "фигурой"
    promotion = ""
    if move.promotion:
        promotion = f" с превращением в {PIECE_RU.get(move.promotion, 'фигуру').lower()}"
    return f"ход {piece_name} на {target}{promotion}"


def line_text(line: Any, limit: int = 4) -> str:
    """SAN-линия движка через пробел (без выдумывания)."""
    if not isinstance(line, (list, tuple)):
        return ""
    return " ".join(str(move) for move in line[:limit] if str(move).strip())


def full_line(best: str | None, line: Any, limit: int = 4) -> str:
    """Полная линия: лучший ход + продолжение (analyzer хранит продолжение)."""
    moves = [
        str(move).strip()
        for move in (line if isinstance(line, (list, tuple)) else [])
        if str(move).strip()
    ]
    best = str(best or "").strip()
    if best and (not moves or moves[0].rstrip("+#") != best.rstrip("+#")):
        moves.insert(0, best)
    return " ".join(moves[:limit])


def _error_plural(count: int) -> str:
    """«1 ошибочный ход», «2 ошибочных хода», «5 ошибочных ходов»."""
    if count % 10 == 1 and count % 100 != 11:
        return "ошибочный ход"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "ошибочных хода"
    return "ошибочных ходов"


def error_text(entry: dict[str, Any], fen: str | None = None) -> str:
    """Короткий проверяемый текст об ошибке: ход, потеря, лучший ход, линия."""
    san = str(entry.get("san") or "—")
    classification = str(entry.get("classification") or "").lower()
    label = CLASS_RU.get(classification, "ошибка")
    drop = _num(entry.get("drop"))
    best = entry.get("best_move_san") or entry.get("best")
    best = str(best).strip() if best else ""
    line = line_text(entry.get("best_line") or entry.get("pv"))

    parts = [f"{san} — {label}"]
    if drop is not None and float(entry.get("drop") or 0) > 0:
        parts[0] += f", потеря {drop}% win"
    parts[0] += "."

    if best and best.rstrip("+#") != san.rstrip("+#"):
        fact = move_fact(fen, best)
        parts.append(
            f"Движок предлагал {best}" + (f" — {fact}" if fact else "") + "."
        )
    if line:
        parts.append(f"Линия движка: {full_line(best, entry.get('best_line') or entry.get('pv'))}.")
    alternatives = [
        str(move).strip()
        for move in (entry.get("alternatives") or [])
        if str(move).strip()
    ]
    if alternatives:
        parts.append(f"Рядом по оценке: {', '.join(alternatives[:2])}.")
    return " ".join(parts)


def why_text(
    entry: dict[str, Any],
    fen: str | None,
    played: str | None = None,
) -> str:
    """Короткое «почему ход плох» только из фактов движка и FEN.

    Опирается на лучший ход и его линию. Если лучший ход — мат, превращение,
    взятие или шах, это называется прямо; иначе остаётся факт «движок видел
    X; после него ...». Никаких угроз, мотивов и планов, которых нет в данных.

    ``played`` — сыгранный ход (для контекста), текст от него не зависит:
    если лучшего хода нет, возвращается пустая строка, а не догадка.
    """
    best = entry.get("best_move_san") or entry.get("best")
    best = str(best).strip() if best else ""
    if not best:
        return ""
    played_clean = str(played or "").strip()
    if played_clean and best.rstrip("+#") == played_clean.rstrip("+#"):
        # Лучший ход совпал с сыгранным — «почему» не существует.
        return ""
    continuation = line_text(entry.get("best_line") or entry.get("pv"))
    line = full_line(best, entry.get("best_line") or entry.get("pv"))

    # Мат и превращение видны прямо в SAN — их можно назвать без разбора FEN.
    if "#" in best:
        return f"Ты не поставил мат: {best}."
    if "=" in best:
        return f"Ты не провёл превращение: {best}."

    board: chess.Board | None = None
    move: chess.Move | None = None
    if fen:
        try:
            board = chess.Board(fen)
            move = board.parse_san(best)
        except (ValueError, TypeError):
            board = None
            move = None
    if board is not None and move is not None:
        if board.is_capture(move):
            target = chess.square_name(move.to_square)
            if board.is_en_passant(move):
                captured_square = move.to_square + (
                    -8 if board.turn == chess.WHITE else 8
                )
            else:
                captured_square = move.to_square
            captured = board.piece_at(captured_square)
            piece = PIECE_ACC.get(captured.piece_type, "фигуру") if captured else "фигуру"
            return f"Ты не забрал {piece} на {target}: {best}."
        if board.gives_check(move):
            return f"Ты не дал шах: {best}."
    # Для тихого лучшего хода честный контраст: что сыграно и что движок
    # считал важнее. Это не выдуманная угроза, а сравнение двух ходов.
    if played:
        contrast = f"Ты сыграл {played}, но движок считал важнее {best}"
        # Показываем только продолжение, без повторения best.
        return contrast + (f": {continuation}." if continuation else ".")
    # Продолжение добавляем, только если движок его дал: без линии
    # «после него Nf3» было бы повтором, а не фактом.
    if len(line.split()) > 1:
        return f"Движок видел {best}; после него {line}."
    return f"Движок видел {best}."


def opening_summary(entries: list[dict[str, Any]], opening_name: str | None) -> str:
    """Итог дебюта только по фактам: сколько ошибок, средняя потеря, худший ход."""
    errors = [
        item
        for item in entries
        if item.get("classification") in ("inaccuracy", "mistake", "blunder")
    ]
    name = opening_name or "Дебют"
    if not errors:
        return f"{name}: критических ошибок по движку нет."

    drops = [
        float(item["drop"])
        for item in errors
        if isinstance(item.get("drop"), (int, float))
        and not isinstance(item.get("drop"), bool)
    ]
    avg = sum(drops) / len(drops) if drops else None
    worst = max(
        errors,
        key=lambda item: float(item.get("drop") or 0.0),
    )
    worst_drop = _num(worst.get("drop"))
    text = f"{name}: {len(errors)} {_error_plural(len(errors))}"
    if avg is not None:
        text += f", средняя потеря {_num(avg)}% win"
    text += "."
    if worst_drop is not None:
        text += (
            f" Худший — {worst.get('san')} "
            f"({CLASS_RU.get(str(worst.get('classification')), 'ошибка')}, "
            f"потеря {worst_drop}%)."
        )
    if len(errors) > 4:
        text += " В отчёте — 4 худших."
    return text


def opening_recommendation(entries: list[dict[str, Any]]) -> str:
    """Что повторить: линия движка после худшей ошибки, без общих советов."""
    errors = [
        item
        for item in entries
        if item.get("classification") in ("inaccuracy", "mistake", "blunder")
    ]
    if not errors:
        return "Критических ошибок в дебюте нет — можно идти дальше по плану."
    worst = max(errors, key=lambda item: float(item.get("drop") or 0.0))
    best = str(worst.get("best") or worst.get("best_move_san") or "").strip()
    line = full_line(best, worst.get("best_line") or worst.get("pv"))
    if line:
        return f"Вместо {worst.get('san')}: {line}."
    if best:
        return f"Вместо {worst.get('san')}: {best}."
    return "Повтори позиции, где движок видел лучший ход."


def task_text(payload: dict[str, Any]) -> str:
    """Объяснение задачи по фактам: сыграно, потеря, лучший ход и линия."""
    fen = payload.get("fen")
    played = str(payload.get("san_played") or "—")
    best = str(payload.get("best_san") or "").strip()
    line = full_line(best, payload.get("best_line"))
    before = _num(payload.get("win_before"))
    after = _num(payload.get("win_after"))
    drop = _num(payload.get("drop"))

    played_fact = move_fact(fen, played)
    best_fact = move_fact(fen, best) if best else None

    first = f"В партии ты сыграл {played}"
    if played_fact and ("взятие" in played_fact or "шах" in played_fact or "рокировка" in played_fact):
        first += f" ({played_fact})"
    first += "."
    if before is not None and after is not None:
        first += f" Win% изменился с {before} до {after}"
        if drop is not None:
            first += f" — потеря {drop}%"
        first += "."

    parts = [first]
    if best:
        parts.append(
            f"Движок считал лучшим {best}" + (f" — {best_fact}" if best_fact else "") + "."
        )
    if line:
        parts.append(f"Линия движка: {line}.")
    return " ".join(parts)
