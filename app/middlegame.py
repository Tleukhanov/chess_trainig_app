"""Тренер миттельшпиля: позиции, сессия партии и промпт отчёта.

Чистая офлайн-логика без БД, HTTP, Stockfish и LLM:

* :data:`POSITIONS` / :data:`SOURCES` — позиции из ``assets/middlegame/*.json``;
* :class:`MiddlegameSession` — состояние партии (план, ходы, результат, отчёт)
  с машиной состояний поверх :class:`chess.Board`;
* :func:`build_report_prompt` / :func:`normalize_report` — контракт отчёта
  «человеческого тренера» для LLM.

Движок и модель соперника подключаются снаружи: модуль только проверяет
легальность UCI-ходов и хранит их. Ходы соперника (Maia) передаются готовыми.
"""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chess

MIDDLEGAME_DIR = Path(__file__).resolve().parent.parent / "assets" / "middlegame"

#: Жёсткий лимит полуходов в одной партии (30 ходов белых и чёрных).
MAX_PLIES = 60

MIN_PLAN_CHARS = 20
MAX_PLAN_CHARS = 4000

#: Схема ответа LLM. Строкой, чтобы её можно было целиком вставить в промпт.
REPORT_SCHEMA = """{
  "verdict": "good" | "partial" | "wrong",
  "summary": "почему план хорош/плох и что игрок доказал ходами",
  "plan_comment": "разбор плана по пунктам: что верно, что скорректировать",
  "moments": [
    {
      "ply": 1,
      "move_san": "e4",
      "title": "короткий заголовок момента",
      "comment": "что произошло и почему это важно",
      "alt_san": "d4" | null,
      "eval_before": "+0.20" | null,
      "eval_after": "+0.15" | null
    }
  ],
  "lesson": "один практический вывод"
}"""

__all__ = [
    "POSITIONS",
    "SOURCES",
    "POSITION_BY_KEY",
    "Position",
    "MAX_PLIES",
    "MIN_PLAN_CHARS",
    "MAX_PLAN_CHARS",
    "REPORT_SCHEMA",
    "validate_plan",
    "pick_position",
    "MiddlegameSession",
    "board_of",
    "legal_moves",
    "user_turn",
    "apply_user_move",
    "need_reply",
    "apply_opponent_move",
    "opponent_side",
    "build_report_prompt",
    "normalize_report",
    "new_session_id",
]


@dataclass(frozen=True, slots=True)
class Position:
    """Одна позиция бандла с метаданными источника."""

    key: str
    n: int
    white: str
    black: str
    fen: str
    source_id: str
    source_title: str
    source_url: str
    chapter_url: str
    chapter: str


def _load_positions() -> tuple[list[Position], list[dict[str, Any]]]:
    """Читает все JSON-бандлы миттельшпиля и собирает позиции + источники."""
    positions: list[Position] = []
    sources: list[dict[str, Any]] = []
    for path in sorted(MIDDLEGAME_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        source_id = str(data.get("id") or path.stem)
        source_title = str(data.get("title") or source_id)
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        source_url = str(source.get("url") or "")
        count = 0
        for item in data.get("positions") or []:
            if not isinstance(item, dict):
                continue
            try:
                n = int(item["n"])
                fen = str(item["fen"])
            except (KeyError, TypeError, ValueError):
                continue
            chapter = str(item.get("chapter") or "")
            chapter_url = f"{source_url}/{chapter}" if source_url and chapter else source_url
            positions.append(
                Position(
                    key=f"{source_id}:{n}",
                    n=n,
                    white=str(item.get("white") or ""),
                    black=str(item.get("black") or ""),
                    fen=fen,
                    source_id=source_id,
                    source_title=source_title,
                    source_url=source_url,
                    chapter_url=chapter_url,
                    chapter=chapter,
                )
            )
            count += 1
        sources.append({"id": source_id, "title": source_title, "count": count})
    positions.sort(key=lambda position: (position.source_id, position.n))
    sources.sort(key=lambda source: source["id"])
    return positions, sources


POSITIONS, SOURCES = _load_positions()
POSITION_BY_KEY: dict[str, Position] = {position.key: position for position in POSITIONS}


def validate_plan(text: str) -> str:
    """Нормализует план игрока и проверяет длину.

    Возвращает текст без окружающих пробелов. Слишком короткий/длинный план
    (или не строка) — :class:`ValueError` с русским сообщением.
    """
    clean = text.strip() if isinstance(text, str) else ""
    if len(clean) < MIN_PLAN_CHARS:
        raise ValueError(
            f"План слишком короткий: нужно минимум {MIN_PLAN_CHARS} символов"
        )
    if len(clean) > MAX_PLAN_CHARS:
        raise ValueError(
            f"План слишком длинный: максимум {MAX_PLAN_CHARS} символов"
        )
    return clean


def pick_position(
    user: str,
    *,
    avoid_keys: list[str],
    rng: random.Random | None = None,
) -> Position:
    """Случайная позиция, по возможности не из ``avoid_keys``.

    Если игрок уже видел все позиции, запрет игнорируется (иначе тренер
    перестал бы работать). ``user`` зарезервирован под будущую персонализацию.
    """
    avoid = set(avoid_keys or [])
    candidates = [position for position in POSITIONS if position.key not in avoid]
    if not candidates:
        candidates = list(POSITIONS)
    if not candidates:
        raise ValueError("Нет доступных позиций миттельшпиля")
    chooser = rng if rng is not None else random
    return chooser.choice(candidates)


def _now_iso() -> str:
    """Текущее UTC-время в ISO-8601 (миллисекунды)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class MiddlegameSession:
    """Состояние одной попытки «Тренера миттельшпиля»."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    user: str = ""
    position_key: str = ""
    fen: str = ""
    side: str = "white"
    opp_elo: int | None = None
    plan: str | None = None
    moves: list[str] = field(default_factory=list)
    status: str = "plan"
    result: str | None = None
    report: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    @property
    def game_over(self) -> bool:
        """Партия объективно окончена (мат/ничья) или упёрлась в лимит полуходов."""
        if len(self.moves) >= MAX_PLIES:
            return True
        try:
            board = board_of(self)
        except ValueError:
            return False
        return board.is_game_over(claim_draw=False)

    def to_dict(self) -> dict[str, Any]:
        """JSON-представление сессии по замороженному контракту API."""
        position = POSITION_BY_KEY.get(self.position_key)
        if position is not None:
            n = position.n
            source = {
                "title": position.source_title,
                "url": position.source_url,
                "chapter_url": position.chapter_url,
                "white": position.white,
                "black": position.black,
            }
        else:
            source_id, _, raw_n = self.position_key.partition(":")
            try:
                n = int(raw_n)
            except (TypeError, ValueError):
                n = 0
            source = {
                "title": source_id,
                "url": "",
                "chapter_url": "",
                "white": "",
                "black": "",
            }
        data = {
            "id": self.id,
            "position_key": self.position_key,
            "n": n,
            "fen": self.fen,
            "moves": list(self.moves),
            "side": self.side,
            "status": self.status,
            "result": self.result,
            "plan": self.plan,
            "source": source,
            "report": self.report,
            "error": self.error,
        }
        if self.opp_elo is not None:
            data["opp_elo"] = self.opp_elo
        return data


def board_of(session: MiddlegameSession) -> chess.Board:
    """Восстанавливает доску из стартового FEN и списка UCI-ходов.

    Недопустимый/нелегальный ход в истории — :class:`ValueError`.
    """
    board = chess.Board(session.fen)
    for uci in session.moves:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError as exc:
            raise ValueError(f"Недопустимый ход в истории: {uci!r}") from exc
        if move not in board.legal_moves:
            raise ValueError(f"Нелегальный ход в истории: {uci!r}")
        board.push(move)
    return board


def legal_moves(session: MiddlegameSession) -> list[str]:
    """Легальные ходы текущей позиции в UCI."""
    return [move.uci() for move in board_of(session).legal_moves]


def user_turn(session: MiddlegameSession) -> bool:
    """Сейчас ход игрока: белые, статус ``playing``, партия не окончена."""
    if session.side != "white" or session.status != "playing":
        return False
    if len(session.moves) >= MAX_PLIES:
        return False
    try:
        board = board_of(session)
    except ValueError:
        return False
    return board.turn == chess.WHITE and not board.is_game_over(claim_draw=False)


def _parse_move(board: chess.Board, uci: str) -> chess.Move:
    """Парсит UCI и проверяет легальность; ошибки — русским ``ValueError``."""
    try:
        move = chess.Move.from_uci(str(uci))
    except ValueError as exc:
        raise ValueError(f"Недопустимый ход: {uci!r}") from exc
    if move not in board.legal_moves:
        raise ValueError(f"Нелегальный ход: {uci!r}")
    return move


def _update_result(session: MiddlegameSession, board: chess.Board) -> None:
    """Фиксирует результат после хода: терминал доски важнее лимита полуходов."""
    if board.is_game_over(claim_draw=False):
        session.result = board.result(claim_draw=False)
    elif len(session.moves) >= MAX_PLIES:
        session.result = "*"


def apply_user_move(session: MiddlegameSession, uci: str) -> None:
    """Применяет ход игрока (UCI) и фиксирует результат при завершении партии.

    Статус остаётся ``playing`` и после мата: ``analyzed`` ставится только
    после прикрепления отчёта. Ход вне очереди/нелегальный — ``ValueError``.
    """
    if session.status == "plan":
        raise ValueError("Сначала сформулируйте план")
    if session.game_over:
        raise ValueError("Партия уже завершена")
    if not user_turn(session):
        raise ValueError("Сейчас не ход пользователя")
    board = board_of(session)
    move = _parse_move(board, uci)
    board.push(move)
    session.moves.append(move.uci())
    _update_result(session, board)


def need_reply(session: MiddlegameSession) -> bool:
    """Игрок только что сходил, партия жива и лимит полуходов не исчерпан."""
    if session.status != "playing" or session.game_over:
        return False
    if len(session.moves) >= MAX_PLIES or len(session.moves) % 2 == 0:
        return False
    try:
        board = board_of(session)
    except ValueError:
        return False
    return board.turn == chess.BLACK


def apply_opponent_move(session: MiddlegameSession, uci: str) -> None:
    """Применяет ход соперника (Maia, UCI); проверяет очередь и легальность."""
    if session.status != "playing":
        raise ValueError("Партия не в состоянии игры")
    if session.game_over:
        raise ValueError("Партия уже завершена")
    board = board_of(session)
    if board.turn != chess.BLACK:
        raise ValueError("Сейчас не ход соперника")
    move = _parse_move(board, uci)
    board.push(move)
    session.moves.append(move.uci())
    _update_result(session, board)


def opponent_side(session: MiddlegameSession) -> str:
    """Сторона соперника (при игре белыми — ``black``)."""
    return "black" if session.side == "white" else "white"


def _format_eval(value: Any) -> str:
    """Человекочитаемая оценка: cp/mate из анализа Stockfish или готовая строка."""
    if value is None:
        return "—"
    if isinstance(value, dict):
        mate = value.get("mate")
        if mate is not None:
            try:
                return f"#{int(mate):+d}"
            except (TypeError, ValueError):
                return f"#{mate}"
        cp = value.get("cp")
        if cp is not None:
            try:
                return f"{float(cp) / 100.0:+.2f}"
            except (TypeError, ValueError):
                return str(cp)
        return "—"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{float(value) / 100.0:+.2f}"
    return str(value)


def _moves_with_san(session: MiddlegameSession) -> list[tuple[int, str, str]]:
    """Пары (полуход с 1, UCI, SAN) для всех сыгранных ходов."""
    board = chess.Board(session.fen)
    result: list[tuple[int, str, str]] = []
    for index, uci in enumerate(session.moves, start=1):
        try:
            move = chess.Move.from_uci(uci)
            san = board.san(move)
            board.push(move)
        except ValueError:
            result.append((index, uci, "?"))
            continue
        result.append((index, uci, san))
    return result


def _facts_by_ply(engine_facts: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Индексирует факты движка по номеру полухода (1-based)."""
    indexed: dict[int, dict[str, Any]] = {}
    for index, fact in enumerate(engine_facts or [], start=1):
        if not isinstance(fact, dict):
            continue
        raw_ply = fact.get("ply", index)
        try:
            ply = int(raw_ply)
        except (TypeError, ValueError):
            ply = index
        indexed.setdefault(ply, fact)
    return indexed


def _fact_line(ply: int, uci: str, san: str, fact: dict[str, Any] | None) -> str:
    """Строка хода для промпта: UCI, SAN и известные оценки/подсказки движка."""
    line = f"{ply}. {uci} ({san})"
    if not fact:
        return line
    details: list[str] = []
    before = fact.get("eval_before", fact.get("before"))
    after = fact.get("eval_after", fact.get("after"))
    if before is not None:
        details.append(f"оценка до: {_format_eval(before)}")
    if after is not None:
        details.append(f"оценка после: {_format_eval(after)}")
    best = fact.get("best_move_san", fact.get("best_san"))
    if best:
        details.append(f"лучший ход: {best}")
    classification = fact.get("classification")
    if classification:
        details.append(f"класс хода: {classification}")
    drop = fact.get("drop")
    if drop is not None:
        details.append(f"потеря win.%: {drop}")
    comment = fact.get("comment")
    if isinstance(comment, str) and comment.strip():
        details.append(f"заметка движка: {comment.strip()}")
    if details:
        line += " — " + "; ".join(details)
    return line


def build_report_prompt(
    session: MiddlegameSession,
    *,
    engine_facts: list[dict[str, Any]],
    position: Position,
) -> str:
    """Русский промпт для LLM: разбор плана игрока и ходов партии.

    Позиция и план относятся к СТАРТОВОЙ позиции: план был сформулирован до
    первого хода, поэтому LLM обязан оценивать его в этом контексте, а не
    только по конечной позиции. ``engine_facts`` — словари с любым из полей
    ``ply``, ``uci``, ``san``, ``eval_before``/``before``,
    ``eval_after``/``after``, ``best_move_san``, ``classification``, ``drop``,
    ``comment``. Факты сопоставляются с ходами по ``ply`` (1-based) или по
    порядку в списке.
    """
    resolved = position if position is not None else POSITION_BY_KEY.get(session.position_key)
    white = resolved.white if resolved else ""
    black = resolved.black if resolved else ""
    title = resolved.source_title if resolved else session.position_key
    url = resolved.source_url if resolved else ""
    chapter_url = resolved.chapter_url if resolved else ""
    plan = (session.plan or "").strip() or "План не задан"

    moves = _moves_with_san(session)
    facts = _facts_by_ply(engine_facts)
    move_lines = [
        _fact_line(ply, uci, san, facts.get(ply)) for ply, uci, san in moves
    ]
    if not move_lines:
        move_lines = ["(ходов ещё нет)"]

    lines = [
        "Ты — человеческий шахматный тренер. Разбери, насколько план игрока "
        "в миттельшпиле оказался верным, и что показали его ходы.",
        "",
        "КОНТЕКСТ ПОЗИЦИИ",
        f"Партия: {white} — {black}.",
        f"Источник: {title}" + (f" ({url})." if url else "."),
        f"Ссылка на главу: {chapter_url}" if chapter_url else "",
        f"Стартовая позиция (ход белых), FEN: {session.fen}",
        "План игрока был составлен в СТАРТОВОЙ позиции, до первого хода. "
        "Оценивай его именно относительно этой позиции, даже если партия "
        "ушла далеко вперёд.",
        "",
        "ПЛАН ИГРОКА",
        f"«{plan}»",
        "",
        "ХОДЫ ПАРТИИ (UCI, SAN, данные Stockfish)",
        *move_lines,
        "",
        "ТРЕБОВАНИЯ К ОТВЕТУ",
        "- Верни СТРОГО один JSON-объект без markdown-обёрток, без текста до "
        "или после него.",
        "- Схема ответа:",
        REPORT_SCHEMA,
        '- verdict: "good" — план был верным и игрок его доказал; '
        '"partial" — идея верна, но порядок/детали требуют правки; '
        '"wrong" — план не соответствовал позиции.',
        "- summary: почему план хорош или плох и что игрок доказал ходами.",
        "- plan_comment: разбор плана по пунктам — что верно, что и как "
        "скорректировать.",
        "- moments: 3–5 ключевых моментов; ply — номер полухода с 1, "
        "move_san — ход в SAN, alt_san — лучшая альтернатива или null.",
        "- lesson: один практический вывод, который можно применить в "
        "следующей партии.",
        "- Пиши по-русски, как живой тренер: объясняй смысл, а не только "
        "оценки. Например: «Да, план хорош, но сначала создай форточку, "
        "иначе ферзь не успевает подключиться».",
        "- Если план был верным, но первый ход оказался неточным — это "
        "verdict \"partial\", а не \"wrong\".",
    ]
    return "\n".join(line for line in lines if line is not None)


_VERDICT_ALIASES = {
    "good": "good",
    "partial": "partial",
    "wrong": "wrong",
    "ok": "partial",
    "mixed": "partial",
    "bad": "wrong",
    "хорошо": "good",
    "частично": "partial",
    "плохо": "wrong",
}


def _clean_text(value: Any, default: str = "") -> str:
    """Строка без пробелов по краям; None/пустое — ``default``."""
    if value is None:
        return default
    text = value if isinstance(value, str) else str(value)
    return text.strip() or default


def _optional_text(value: Any) -> str | None:
    """Строка или None (пустые значения и не-строки без смысла -> None)."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    return text or None


def _clean_ply(value: Any) -> int:
    """Номер полухода как int; мусор -> 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_report(raw: dict[str, Any]) -> dict[str, Any]:
    """Приводит ответ LLM к схеме отчёта, подставляя безопасные значения.

    Не падает на неполном/мусорном JSON: отсутствующие поля заполняются,
    ``verdict`` приводится к одному из ``good``/``partial``/``wrong``,
    невалидные моменты отбрасываются.
    """
    data = raw if isinstance(raw, dict) else {}

    verdict_raw = _clean_text(data.get("verdict")).lower()
    verdict = _VERDICT_ALIASES.get(verdict_raw, "partial")

    raw_moments = data.get("moments")
    moments: list[dict[str, Any]] = []
    if isinstance(raw_moments, list):
        for item in raw_moments:
            if not isinstance(item, dict):
                continue
            moments.append(
                {
                    "ply": _clean_ply(item.get("ply")),
                    "move_san": _clean_text(item.get("move_san", item.get("san"))),
                    "title": _clean_text(item.get("title")),
                    "comment": _clean_text(item.get("comment")),
                    "alt_san": _optional_text(item.get("alt_san")),
                    "eval_before": _optional_text(item.get("eval_before")),
                    "eval_after": _optional_text(item.get("eval_after")),
                }
            )

    return {
        "verdict": verdict,
        "summary": _clean_text(data.get("summary"), "Итог не сформирован."),
        "plan_comment": _clean_text(
            data.get("plan_comment"), "Комментарий к плану не сформирован."
        ),
        "moments": moments,
        "lesson": _clean_text(
            data.get("lesson"), "Практический вывод не сформирован."
        ),
    }


def new_session_id() -> str:
    """Новый идентификатор сессии (uuid4 hex)."""
    return uuid.uuid4().hex


if __name__ == "__main__":
    raise SystemExit(0)
