"""Тренер эндшпиля: собственные ошибки из анализа партий + эталонные позиции.

Источники задач (поле ``source`` в API):

* ``own`` — эндшпильные ошибки самого игрока (blunder/mistake) из кеша
  анализа. Отбор ходов и фаза — через ``app.patterns``; лучший ход и линия —
  только из записи анализа (``GameAnalysis.summary()``). Модуль не запускает
  движок сам и оценок не придумывает.
* ``master`` — эталонные учебные позиции известной эндшпильной теории
  (Lucena, Philidor, оппозиция, треугольник, ключевые поля, активный король,
  крепость, гонка пешек, размен ладей, матовая сетка). Лучшие ходы и линии
  зафиксированы после проверки Stockfish 19 (depth 28, multipv 3,
  2026-11-03, бинаррь STOCKFISH_PATH). Ходы, равные лучшему по оценке
  движка, перечислены в ``alt_moves`` и тоже считаются верным ответом.

Судья ходов (``judge_position``): если задан env ``SYZYGY_PATH`` и каталог
существует — тоаблицы Syzygy через ``chess.syzygy`` (опциональная зависимость,
импортируется только при настроенном пути); иначе в кратце??: иначе Stockfish
через ``app.analyzer.Engine``. Любой сбой судьи — ``None``, ответ API не
ломается.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import dataclasses
from typing import Any, Callable

import chess

from . import facttext, patterns
from .analyzer import Engine
from .config import settings
from .games import Game

__all__ = [
    "SOURCES",
    "MASTER_TASKS",
    "MASTER_TASK_BY_KEY",
    "MasterTask",
    "master_payload",
    "master_solution",
    "own_task_rows",
    "own_payload",
    "own_solution",
    "parse_move",
    "same_san",
    "judge_position",
]

SOURCES = ("master", "own")

#: Максимум задач на партию (худшие по потере win.%) и на пользователя.
MAX_TASKS_PER_GAME = 2
MAX_OWN_TASKS = 30

_GOAL_WIN = "Доведи до победы"
_GOAL_DRAW = "Не упусти ничью"
_GOAL_HOLD = "Удержи позицию"


def same_san(left: Any, right: Any) -> bool:
    """Сравнение SAN-ходов без суффиксов шаха/мата (как в app.analyzer)."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return left.strip().rstrip("+#") == right.strip().rstrip("+#")


def _own_goal(win_before: Any, win_after: Any) -> str:
    """Цель задачи по цифрам анализа: победа / не упустить ничью / удержать."""
    try:
        before = float(win_before)
    except (TypeError, ValueError):
        return _GOAL_HOLD
    if before >= 75.0:
        return _GOAL_WIN
    try:
        after = float(win_after)
    except (TypeError, ValueError):
        after = None
    if 35.0 <= before <= 65.0 and after is not None and after < 35.0:
        return _GOAL_DRAW
    return _GOAL_HOLD


@dataclass(frozen=True, slots=True)
class MasterTask:
    """Эталонная учебная позиция с зафиксированным решением.

    ``alt_moves`` — ходы, равные лучшему по оценке движка (проверено при
    подготовке), они тоже считаются верным ответом. ``best_line`` —
    продолжение без первого хода (как в ``app.analyzer``).
    """

    key: str
    title: str
    concept: str
    concept_title: str
    fen: str
    goal: str
    best_move_san: str
    alt_moves: tuple[str, ...]
    best_line: tuple[str, ...]
    why: str
    # Движок не "исправляет" идею автора, а показывает свой вариант отдельно.
    engine_best_move_san: str | None = None
    engine_best_line: tuple[str, ...] = ()


# --- эталонные (master) позиции --------------------------------------------

MASTER_TASKS: tuple[MasterTask, ...] = (
    MasterTask(
        key="lucena",
        title="Позиция Лусены: ладья и «мост»",
        concept="lucena",
        concept_title="мост ладьёй",
        fen="1K1k4/1P6/8/8/8/8/r7/2R5 w - - 0 1",
        goal="Победи",
        best_move_san="Rd1+",
        alt_moves=("Rc4", "Re1"),
        best_line=("Ke7", "Rd4", "Ke6", "Kc7", "Rc2+"),
        why="После 1.Rd1+ Ke7 ладья уходит на d4 — «мост»: она прячет короля от шахов, "
            "чёрной ладье рано или поздно придётся размен, и пешка b проходит.",
    ),
    MasterTask(
        key="philidor",
        title="Защита Филидора: ладья на линии пешки",
        concept="philidor",
        concept_title="защита третьей линии",
        fen="4k3/R7/3K4/4P3/5r2/8/8/8 b - - 0 1",
        goal="Спаси ничью",
        best_move_san="Re4",
        alt_moves=(),
        best_line=("Ke6",),
        why="Ладья остаётся на линии пешки (Re4): пешка не идёт, а при её продвижении "
            "ладья издалека шахует королю сзади. Прочие защиты (Rd4+ и выше) по движку "
            "проигрывают.",
    ),
    MasterTask(
        key="triangulation",
        title="Треугольник короля (Дворецкий)",
        concept="triangulation",
        concept_title="треугольник / потеря темпа",
        fen="8/1p1k4/1P6/2PK4/8/8/8/8 w - - 0 1",
        goal="Победи",
        best_move_san="Ke5",
        alt_moves=("Kd4", "Ke4", "Kc4"),
        best_line=("Kc6", "Kd4", "Kb5", "Kd5", "Ka5"),
        why="Король описывает треугольник d5–e5–d4: та же картина возникает с ходом "
            "чёрных — цугцванг, у чёрных нет ожиданий.",
    ),
    MasterTask(
        key="opposition",
        title="Прямая оппозиция: держи ключевые поля",
        concept="opposition",
        concept_title="оппозиция",
        fen="3k4/8/8/3K4/3P4/8/8/8 b - - 0 1",
        goal="Спаси ничью",
        best_move_san="Kd7",
        alt_moves=(),
        best_line=("Ke5", "Ke7", "Kf5", "Kd6", "Ke4"),
        why="Kd7 — прямая оппозиция: король держит поля перед пешкой. Шаг в сторону "
            "(Kc7/Ke7) отдаёт ключевые поля, и белые прорываются (по движку — мат).",
    ),
    MasterTask(
        key="kpk_outflanking",
        title="Ключевые поля: король обходит пешку сбоку",
        concept="key_squares",
        concept_title="ключевые поля",
        fen="8/5k2/8/4K3/4P3/8/8/8 w - - 0 1",
        goal="Победи",
        best_move_san="Kd6",
        alt_moves=("Kf5", "Kd5"),
        best_line=("Kf8", "Kd7", "Kf7", "e5", "Kg6"),
        why="Пешке e4 с королём на e5 нужен король на ключевых полях: ход вглубь "
            "(Kd6/Kf5/Kd5) — и после отхода чёрного короля пешке открывается путь.",
    ),
    MasterTask(
        key="active_king",
        title="Активный король: треугольник (Альбурт–Каспаров, 1978)",
        concept="active_king",
        concept_title="активный король",
        fen="8/8/8/8/4k2p/5p1P/8/5K2 b - - 0 1",
        goal="Победи",
        best_move_san="Kf5",
        alt_moves=("Kd5", "Ke5"),
        best_line=("Kg1", "Ke5", "Kf1", "Ke4", "Kf2"),
        why="Чёрный король описывает треугольник f5–e5–e4 и отдаёт темп белому королю: "
            "после Ke4 белым ходить нельзя, и чёрный король забирает пешку h3 "
            "(в партии: 55...Kf5!).",
    ),
    MasterTask(
        key="fortress",
        title="Крепость в углу (Серпер–Накамура, 2004)",
        concept="fortress",
        concept_title="крепость",
        fen="8/8/8/3k2p1/4p3/2b1P1pP/4KNP1/8 w - - 0 1",
        goal="Держи крепость",
        best_move_san="Nxe4",
        alt_moves=("Ng4", "Nd3"),
        best_line=("Kxe4", "Kf1", "Be5", "Kg1", "Kd3"),
        why="Жертва коня за пешку: после Kxe4 король уходит к углу (Kf1). Пешки и король "
            "в углу образуют крепость — чёрного короля из неё не выманить, и все ходы "
            "чёрных держат равновесие (по движку: Ng4/Nd3/Nxe4 в пределах ничьей).",
    ),
    MasterTask(
        key="pawn_race",
        title="Гонка пешек: счёт полуходов",
        concept="pawn_race",
        concept_title="гонка пешек",
        fen="8/1k6/8/8/p7/1K6/1P6/8 w - - 0 1",
        goal="Победи",
        best_move_san="Kxa4",
        alt_moves=("Kb4", "Ka3"),
        best_line=("Ka6", "Kb4", "Kb6", "b3", "Ka6"),
        why="Пешка b2 пока закрыта собственным королём, а чёрная пешка a4 уже мчится к a1: "
            "король обязан немедленно остановить её (Kxa4/Kb4/Ka3). Отход короля вдоль "
            "(Kc3??) даёт чёрным 1...a3! — обе пешки доходят одновременно, ничья.",
    ),
    MasterTask(
        key="rook_exchange",
        title="Размен ладей выводит на выигранную пешку",
        concept="rook_exchange",
        concept_title="упрощение при перевесе",
        fen="r1R5/6p1/4k3/2P5/3K4/8/6P1/8 w - - 0 1",
        goal="Победи",
        best_move_san="Rxa8",
        alt_moves=(),
        best_line=("Kd7", "Kd5", "g6", "c6+", "Kc7"),
        why="Размен ладей (Rxa8) упрощает в пешковое окончание с лишней проходной c5: "
            "дальше король ведёт пешку сам. Прочие ходы ладьи по движку слабее.",
    ),
    MasterTask(
        key="mate_net",
        title="Матовая сетка: ладья и король вместе",
        concept="mate_net",
        concept_title="мат ладьёй",
        fen="7k/8/5K2/8/8/8/1R6/8 w - - 0 1",
        goal="Замати",
        best_move_san="Kg6",
        alt_moves=("Kf7",),
        best_line=("Kg8", "Rb8#"),
        why="Король сам занимает поля g6/f7 — не выпускает чёрного короля из угла, и ладья "
            "ставит мат на 8-й линии (1.Kg6 Kg8 2.Rb8#).",
    ),
)

def _engine_best_for_task(fen: str) -> tuple[str | None, tuple[str, ...]]:
    """Лучший ход Stockfish для master-позиции, если движок доступен.

    Возвращает (None, ()) при отсутствии бинаря или ошибке. Для чтения
    это не критично: идея автора остаётся главной, а движок только
    показывает свой вариант.
    """
    try:
        with Engine(depth=14, multipv=1, time_ms=800) as engine:
            lines = engine.analyze_position(chess.Board(fen))
    except Exception:
        return None, ()
    if not lines or not lines[0].get("pv"):
        return None, ()
    board = chess.Board(fen)
    move_san = lines[0]["pv"][0]
    try:
        move = board.parse_san(str(move_san))
        best = board.san(move)
    except (ValueError, TypeError):
        return None, ()
    line: list[str] = []
    try:
        board.push_san(best)
        for next_san in lines[0]["pv"][1:7]:
            move = board.parse_san(str(next_san))
            line.append(board.san(move))
            board.push(move)
    except (ValueError, TypeError):
        line = []
    return best, tuple(line)


MASTER_TASKS = tuple(
    dataclasses.replace(
        task,
        engine_best_move_san=engine_best,
        engine_best_line=engine_line,
    )
    for task in MASTER_TASKS
    for engine_best, engine_line in [_engine_best_for_task(task.fen)]
)
MASTER_TASK_BY_KEY: dict[str, MasterTask] = {task.key: task for task in MASTER_TASKS}


def _turn_color(board: chess.Board) -> str:
    """Сторона, чей ход, как строка ``white``/``black``."""
    return "white" if board.turn == chess.WHITE else "black"


def master_payload(task: MasterTask, *, with_solution: bool = False) -> dict[str, Any]:
    """JSON задачи: без решения для списка, с решением — при ответе."""
    board = chess.Board(task.fen)
    payload: dict[str, Any] = {
        "source": "master",
        "task_key": task.key,
        "title": task.title,
        "concept": task.concept,
        "concept_title": task.concept_title,
        "fen": task.fen,
        "goal": task.goal,
        "turn": _turn_color(board),
        "user_color": _turn_color(board),
        "game_id": None,
        "ply": None,
    }
    if with_solution:
        payload.update(master_solution(task))
    return payload


def master_solution(task: MasterTask) -> dict[str, Any]:
    """Решение master-позиции: идея автора, альтернативы и вариант компьютера."""
    return {
        "best_move_san": task.best_move_san,
        "alt_moves_san": list(task.alt_moves),
        "best_line": list(task.best_line),
        "why": task.why,
        "engine_best_move_san": task.engine_best_move_san,
        "engine_best_line": list(task.engine_best_line or ()),
    }


# --- собственные (own) задачи из анализа ------------------------------------

_OWN_TITLE_FALLBACK = "Партия соперника"


def _own_title(game: Game) -> str:
    """Человекочитаемый заголовок задачи из партии."""
    opponent = (game.opponent or "").strip()
    if opponent:
        return f"Партия против {opponent}"
    if game.id:
        return f"Партия {game.id}"
    return _OWN_TITLE_FALLBACK


def _fens_before(game: Game) -> list[str] | None:
    """FEN перед каждым полуходом партии; битая SAN-история — None."""
    board = chess.Board()
    fens: list[str] = []
    for san in game.moves:
        fens.append(board.fen())
        try:
            board.push_san(san)
        except ValueError:
            return None
    return fens


def own_task_rows(
    pairs: list[tuple[Game, dict]],
    *,
    per_game: int = MAX_TASKS_PER_GAME,
    limit: int = MAX_OWN_TASKS,
) -> list[dict[str, Any]]:
    """Строки задач: эндшпильные ошибки игрока (blunder/mistake) из анализа.

    Отбор ходов и фаза — через ``app.patterns.analyze_user_moves``; лучший ход,
    линия и оценки (win_before/win_after/drop) — только из записи анализа.
    Битые партии и записи пропускаются. На партию берётся не более ``per_game``
    худших по потере ходов, всего не более ``limit`` задач.
    """
    rows: list[dict[str, Any]] = []
    for game, analysis in pairs:
        records = [
            record
            for record in patterns.analyze_user_moves(game, analysis)
            if record.get("phase") == "endgame"
        ]
        if not records:
            continue
        moves = analysis.get("moves") if isinstance(analysis, dict) else []
        moves = moves if isinstance(moves, list) else []
        fens = _fens_before(game)
        if fens is None:
            continue
        candidates: list[dict[str, Any]] = []
        for record in records:
            ply = int(record.get("ply") or 0)
            entry = moves[ply] if ply < len(moves) else {}
            entry = entry if isinstance(entry, dict) else {}
            best = entry.get("best_move_san")
            if not isinstance(best, str) or not best.strip():
                continue
            win_before = entry.get("win_before")
            win_after = entry.get("win_after")
            candidates.append(
                {
                    "task_key": f"{game.id}|{ply}",
                    "game_id": game.id,
                    "ply": ply,
                    "fen": fens[ply],
                    "title": _own_title(game),
                    "user_color": game.user_color,
                    "opponent": game.opponent,
                    "link": game.link,
                    "opening": game.opening,
                    "eco": game.eco,
                    "san_played": record.get("san") or "",
                    "classification": record.get("classification"),
                    "drop": entry.get("drop"),
                    "win_before": win_before,
                    "win_after": win_after,
                    "best_move_san": best,
                    "best_line": entry.get("best_line") or [],
                    "goal": _own_goal(win_before, win_after),
                }
            )
        candidates.sort(key=lambda row: (-float(row.get("drop") or 0.0), row["ply"]))
        rows.extend(candidates[:per_game])
    return rows[:limit]


def own_payload(row: dict[str, Any]) -> dict[str, Any]:
    """JSON задачи для мета — без решения."""
    turn = _turn_color(chess.Board(row["fen"]))
    return {
        "source": "own",
        "task_key": row["task_key"],
        "title": row["title"],
        "fen": row["fen"],
        "goal": row["goal"],
        "turn": turn,
        "user_color": row["user_color"],
        "game_id": row["game_id"],
        "ply": row["ply"],
        "link": row["link"],
        "opening": row["opening"],
        "eco": row["eco"],
        "opponent": row["opponent"],
        "san_played": row["san_played"],
        "classification": row["classification"],
        "drop": row["drop"],
        "win_before": row["win_before"],
        "win_after": row["win_after"],
    }


def own_solution(row: dict[str, Any]) -> dict[str, Any]:
    """Решение own-задачи: всё из записи анализа + детерминированный текст.

    «Почему» собирается ``app.facttext.task_text`` из фактов: сыгранный ход,
    потеря win.%, лучший ход и линия движка. Никаких домыслов.
    """
    why = facttext.task_text(
        {
            "fen": row["fen"],
            "san_played": row["san_played"],
            "best_san": row["best_move_san"],
            "best_line": row["best_line"],
            "win_before": row["win_before"],
            "win_after": row["win_after"],
            "drop": row["drop"],
        }
    )
    return {
        "best_move_san": row["best_move_san"],
        "alt_moves_san": [],
        "best_line": list(row["best_line"] or []),
        "win_before": row["win_before"],
        "win_after": row["win_after"],
        "drop": row["drop"],
        "classification": row["classification"],
        "why": why,
    }


# --- разбор хода и судья ----------------------------------------------------

def parse_move(board: chess.Board, raw: Any) -> tuple[chess.Move | None, str | None]:
    """Ход игрока: SAN или UCI. Нечитаемое/нелегальное — (None, None)."""
    text = str(raw or "").strip()
    if not text:
        return None, None
    try:
        move = board.parse_san(text)
        return move, board.san(move)
    except ValueError:
        pass
    try:
        move = chess.Move.from_uci(text)
    except ValueError:
        return None, None
    if move not in board.legal_moves:
        return None, None
    return move, board.san(move)


def _open_syzygy():
    """Таблицы Syzygy, если настроен SYZYGY_PATH и каталог существует.

    ``chess.syzygy`` импортируется только при настроенном пути: без него
    путь любой сбой — тихий переход на Stockfish.
    """
    raw = (settings.syzygy_path or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_dir():
        return None
    try:
        import chess.syzygy

        return chess.syzygy.open_tablebase(str(path))
    except Exception:  # noqa: BLE001 — без таблиц тихо идём в Stockfish
        return None


def _judge_engine():
    """Лёгкий Stockfish для судьбы: малую глубину, без multipv.

    Судья дополнение к ответу, а не сам анализ: конкретную глубину задаёт
    STOCKFISH_DEPTH, но для трейнера она излишня и замедляет запрос.
    """
    return Engine(depth=10, multipv=1, time_ms=800)


def judge_position(
    board: chess.Board,
    *,
    engine_factory: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """Судья позиции: Syzygy по SYZYGY_PATH, иначе Stockfish.

    Syzygy: ``wdl`` — исход для стороны, чей ход (>0 выигрывает, 0 ничья,
    <0 проигрыш), и ``dtz`` — полуходы до обнуления. Stockfish: оценка
    (cp/mate), win.% и до 4 ходов лучшей линии.

    Любой сбой (нет бинаря, нет таблиц под число фигур) — ``None``: судья
    опционален и не должен ломать ответ.
    """
    if not board.is_valid():
        return None
    tablebase = _open_syzygy()
    if tablebase is not None:
        try:
            return {
                "kind": "syzygy",
                "wdl": int(tablebase.probe_wdl(board)),
                "dtz": int(tablebase.probe_dtz(board)),
            }
        except Exception:  # noqa: BLE001 — нет таблиц этой эндшпильной игры
            pass
        finally:
            try:
                tablebase.close()
            except Exception:  # noqa: BLE001
                pass
    factory = engine_factory or _judge_engine
    try:
        with factory() as engine:
            lines = engine.analyze_position(board)
    except Exception:  # noqa: BLE001 — движок недоступен, судьи нет
        return None
    if not lines:
        return None
    top = lines[0]
    return {
        "kind": "stockfish",
        "eval": top.get("eval"),
        "win": top.get("win"),
        "pv": (top.get("pv") or [])[:4],
    }
