"""Валидация и обогащение анализа, посчитанного на клиенте (Stockfish WASM).

Модуль чистый: не ходит в сеть и не запускает движок. На вход получает
партию (``Game``) и payload запроса ``{analysis, engine_meta}`` (или сам
анализ) и возвращает summary в том же формате, что и серверный анализатор,
но с ``analysis_source='client'`` и ``engine='stockfish-wasm'``.

Все числовые и шахматные проверки выполняются здесь; при любой проблеме
бросается ``ClientAnalysisError`` (подкласс ``ValueError``), чтобы эндпоинт
мог вернуть 400.
"""

from __future__ import annotations

import math
from typing import Any

import chess

from .analyzer import classify_move, win_percent
from .games import Game
from .themes import classify_move_themes

__all__ = ["ClientAnalysisError", "validate_and_enrich"]

SANITY_CP_LIMIT = 100000.0
SANITY_MATE_LIMIT = 1000
WIN_MIN = 0.0
WIN_MAX = 100.0
DROP_MIN = -100.0
DROP_MAX = 100.0


class ClientAnalysisError(ValueError):
    """Ошибка валидации клиентского анализа."""


def _fail(message: str) -> None:
    raise ClientAnalysisError(message)


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not _is_bool(value)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not _is_bool(value) and math.isfinite(value)


def _game_attr(game: Any, name: str, default: Any = None) -> Any:
    if isinstance(game, dict):
        return game.get(name, default)
    return getattr(game, name, default)


def _validate_eval(eval_obj: Any, label: str) -> dict[str, Any]:
    if not isinstance(eval_obj, dict):
        _fail(f"{label}: ожидается объект {{cp, mate}}")
    if "cp" not in eval_obj or "mate" not in eval_obj:
        _fail(f"{label}: нужны поля cp и mate")
    cp = eval_obj["cp"]
    mate = eval_obj["mate"]
    if cp is not None:
        if not _is_number(cp):
            _fail(f"{label}.cp: ожидается число или null")
        if abs(float(cp)) > SANITY_CP_LIMIT:
            _fail(f"{label}.cp: вне допустимых границ")
    if mate is not None:
        if not _is_int(mate):
            _fail(f"{label}.mate: ожидается целое число или null")
        if abs(int(mate)) > SANITY_MATE_LIMIT:
            _fail(f"{label}.mate: вне допустимых границ")
    return {"cp": (None if cp is None else float(cp)), "mate": (None if mate is None else int(mate))}


def _validate_win(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if not _is_number(value):
        _fail(f"{label}: ожидается число или null")
    number = float(value)
    if number < WIN_MIN or number > WIN_MAX:
        _fail(f"{label}: вне диапазона 0..100")
    return round(number, 2) if False else number


def _validate_drop(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if not _is_number(value):
        _fail(f"{label}: ожидается число или null")
    number = float(value)
    if number < DROP_MIN or number > DROP_MAX:
        _fail(f"{label}: вне диапазона -100..100")
    return number


def _validate_san_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        _fail(f"{label}: ожидается массив строк")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            _fail(f"{label}[{index}]: ожидается непустая строка SAN")
        result.append(item.strip())
    return result


def _validate_client_line(line: Any, index: int, board: chess.Board) -> dict[str, Any]:
    if not isinstance(line, dict):
        _fail(f"client_lines[{index}]: ожидается объект")
    if "pv" not in line or "eval" not in line or "win" not in line or "multipv" not in line:
        _fail(f"client_lines[{index}]: нужны поля pv, eval, win, multipv")
    pv_raw = _validate_san_list(line["pv"], f"client_lines[{index}].pv")
    eval_obj = _validate_eval(line["eval"], f"client_lines[{index}].eval")
    win = _validate_win(line["win"], f"client_lines[{index}].win")
    if not _is_int(line["multipv"]) or int(line["multipv"]) < 1:
        _fail(f"client_lines[{index}].multipv: положительное целое")
    multipv = int(line["multipv"])
    # Проверяем легальность PV с текущей позиции.
    board_copy = board.copy(stack=False)
    pv_norm: list[str] = []
    for san in pv_raw:
        try:
            move = board_copy.parse_san(san)
        except ValueError:
            _fail(f"client_lines[{index}].pv: нелегальный ход {san}")
        pv_norm.append(board_copy.san(move))
        board_copy.push(move)
    return {
        "pv": pv_norm,
        "eval": eval_obj,
        "win": (None if win is None else float(win)),
        "multipv": multipv,
    }


def _validate_client_lines(value: Any, board: chess.Board) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        _fail("client_lines: ожидается массив")
    return [_validate_client_line(line, i, board) for i, line in enumerate(value)]


def _normalize_meta(payload: dict[str, Any], analysis: dict[str, Any]) -> tuple[int, int]:
    meta = payload.get("engine_meta")
    if not isinstance(meta, dict):
        meta = analysis.get("engine_meta")
    if not isinstance(meta, dict):
        meta = {}
    if "depth" not in meta and "depth" in payload:
        meta = dict(meta)
        meta["depth"] = payload.get("depth")
    if "depth" not in meta and "depth" in analysis:
        meta = dict(meta)
        meta["depth"] = analysis.get("depth")
    if "multipv" not in meta and "multipv" in payload:
        meta = dict(meta)
        meta["multipv"] = payload.get("multipv")
    if "multipv" not in meta and "multipv" in analysis:
        meta = dict(meta)
        meta["multipv"] = analysis.get("multipv")
    depth = meta.get("depth", 10)
    multipv = meta.get("multipv", 1)
    if not _is_int(depth) or depth < 1 or depth > 1000:
        _fail("engine_meta.depth: положительное целое (1..1000)")
    if not _is_int(multipv) or multipv < 1 or multipv > 100:
        _fail("engine_meta.multipv: положительное целое (1..100)")
    return int(depth), int(multipv)


def _mover_eval_to_white(eval_dict: dict[str, Any], mover_is_white: bool) -> dict[str, Any]:
    cp = eval_dict.get("cp")
    mate = eval_dict.get("mate")
    if not mover_is_white:
        if cp is not None:
            cp = -float(cp)
        if mate is not None:
            mate = -int(mate)
    return {"cp": (None if cp is None else float(cp)), "mate": (None if mate is None else int(mate))}


def validate_and_enrich(game: Any, payload: Any) -> dict[str, Any]:
    if game is None:
        _fail("game: партия не найдена")

    game_moves = _game_attr(game, "moves", None)
    if not isinstance(game_moves, list) or not game_moves:
        _fail("game: в партии нет ходов")
    user_color = _game_attr(game, "user_color", "white")
    if user_color not in ("white", "black"):
        _fail("game.user_color: white или black")
    game_id = _game_attr(game, "id", None)
    result_for_user = _game_attr(game, "result_for_user", None)
    opponent = _game_attr(game, "opponent", None)

    if not isinstance(payload, dict):
        _fail("payload: ожидается объект")

    # Payload may be either {analysis, engine_meta} or the analysis itself.
    if isinstance(payload.get("analysis"), dict):
        analysis = dict(payload["analysis"])
        outer = payload
    else:
        analysis = dict(payload)
        outer = payload

    # game_id / user_color checks if present.
    if "game_id" in analysis and game_id is not None and analysis.get("game_id") != game_id:
        _fail("analysis.game_id не совпадает с партией")
    if "user_color" in analysis and analysis.get("user_color") not in (None, user_color):
        _fail("analysis.user_color не совпадает с партией")

    moves_raw = analysis.get("moves")
    if not isinstance(moves_raw, list):
        _fail("analysis.moves: ожидается массив")
    if len(moves_raw) != len(game_moves):
        _fail(
            f"analysis.moves: ожидалось {len(game_moves)} ходов, получено {len(moves_raw)}"
        )

    depth, multipv = _normalize_meta(outer, analysis)

    # Validate game moves themselves are legal SAN.
    board0 = chess.Board()
    for san in game_moves:
        try:
            move = board0.parse_san(str(san))
        except ValueError:
            _fail(f"game.moves: нелегальный SAN {san}")
        board0.push(move)

    enriched_moves: list[dict[str, Any]] = []
    board = chess.Board()
    # We'll validate move-by-move; keep a copy for themes.
    for ply, raw_move in enumerate(moves_raw):
        if not isinstance(raw_move, dict):
            _fail(f"moves[{ply}]: ожидается объект")

        move = dict(raw_move)

        # --- SAN of the played move ---
        san_raw = move.get("san")
        if not isinstance(san_raw, str) or not san_raw.strip():
            _fail(f"moves[{ply}].san: ожидается непустая строка")
        try:
            played_move = board.parse_san(san_raw.strip())
        except ValueError:
            _fail(f"moves[{ply}].san: нелегальный ход {san_raw}")
        game_move = None
        try:
            game_move = board.parse_san(str(game_moves[ply]))
        except ValueError:
            _fail(f"game.moves[{ply}]: нелегальный SAN {game_moves[ply]}")
        if played_move != game_move:
            _fail(f"moves[{ply}].san: ход не совпадает с партией")
        san = board.san(game_move)

        # --- evals ---
        before = _validate_eval(move.get("before"), f"moves[{ply}].before")
        after = _validate_eval(move.get("after"), f"moves[{ply}].after")
        best_eval_raw = move.get("best_eval")
        best_eval = (
            _validate_eval(best_eval_raw, f"moves[{ply}].best_eval")
            if best_eval_raw is not None
            else None
        )

        # --- numerics ---
        win_before = _validate_win(move.get("win_before"), f"moves[{ply}].win_before")
        win_after = _validate_win(move.get("win_after"), f"moves[{ply}].win_after")
        best_win = _validate_win(move.get("best_win"), f"moves[{ply}].best_win")
        drop_raw = _validate_drop(move.get("drop"), f"moves[{ply}].drop")
        # Recompute drop from win values when both are available.
        if win_before is not None and win_after is not None:
            drop = float(win_before) - float(win_after)
        else:
            drop = drop_raw

        clock_used = move.get("clock_used")
        if clock_used is not None:
            if not _is_number(clock_used) or float(clock_used) < 0:
                _fail(f"moves[{ply}].clock_used: неотрицательное число или null")
            clock_used = float(clock_used)
        time_pressure = move.get("time_pressure")
        if time_pressure is not None and not isinstance(time_pressure, bool):
            _fail(f"moves[{ply}].time_pressure: bool или null")

        # --- best move ---
        best_move_san_raw = move.get("best_move_san")
        best_move_san: str | None = None
        if best_move_san_raw is not None:
            if not isinstance(best_move_san_raw, str) or not best_move_san_raw.strip():
                _fail(f"moves[{ply}].best_move_san: строка или null")
            try:
                bm = board.parse_san(best_move_san_raw.strip())
            except ValueError:
                _fail(f"moves[{ply}].best_move_san: нелегальный ход {best_move_san_raw}")
            best_move_san = board.san(bm)

        # --- best_line ---
        best_line_raw = move.get("best_line", [])
        if best_line_raw is None:
            best_line_raw = []
        best_line = _validate_san_list(best_line_raw, f"moves[{ply}].best_line")
        # legality: starting after best move if present
        if best_move_san is not None:
            b = board.copy(stack=False)
            b.push(b.parse_san(best_move_san))
            norm_bl: list[str] = []
            for s in best_line:
                try:
                    m = b.parse_san(s)
                except ValueError:
                    _fail(f"moves[{ply}].best_line: нелегальный ход {s}")
                norm_bl.append(b.san(m))
                b.push(m)
            best_line = norm_bl
        else:
            if best_line:
                _fail(f"moves[{ply}].best_line: без best_move_san должен быть пустым")
            best_line = []

        # --- played_line ---
        played_line_raw = move.get("played_line", [])
        if played_line_raw is None:
            played_line_raw = []
        played_line = _validate_san_list(played_line_raw, f"moves[{ply}].played_line")
        b = board.copy(stack=False)
        b.push(played_move)
        norm_pl: list[str] = []
        for s in played_line:
            try:
                m = b.parse_san(s)
            except ValueError:
                _fail(f"moves[{ply}].played_line: нелегальный ход {s}")
            norm_pl.append(b.san(m))
            b.push(m)
        played_line = norm_pl

        # --- client_lines ---
        client_lines_key = None
        for key in ("client_lines", "clientLines", "lines"):
            if key in move:
                client_lines_key = key
                break
        client_lines = None
        if client_lines_key is not None:
            client_lines = _validate_client_lines(move.get(client_lines_key), board)
            # normalize PVs already done in _validate_client_line

        # --- recompute classification ---
        def _san_key(s: str) -> str:
            return s.rstrip("+#")
        is_best = best_move_san is not None and _san_key(best_move_san) == _san_key(san)
        classification = classify_move(win_before, win_after, is_best=is_best)

        # --- enrich themes ---
        mover_is_white = board.turn == chess.WHITE
        if client_lines:
            lines_for_themes = client_lines
        elif best_move_san is not None:
            pv = [best_move_san] + best_line
            base_eval = best_eval if best_eval is not None else before
            if base_eval is None:
                base_eval = {"cp": None, "mate": None}
            eval_white = _mover_eval_to_white(base_eval, mover_is_white)
            win_white = best_win if best_win is not None else win_before
            if win_white is not None and not mover_is_white:
                win_white = 100.0 - float(win_white)
            lines_for_themes = [
                {
                    "pv": pv,
                    "eval": eval_white,
                    "win": (None if win_white is None else float(win_white)),
                    "multipv": 1,
                }
            ]
        else:
            lines_for_themes = []

        try:
            themes = classify_move_themes(board, san, lines_for_themes, drop=drop)
        except Exception:
            themes = None

        # --- assemble enriched move (preserve extra fields? keep known) ---
        new_move: dict[str, Any] = dict(move)
        new_move["san"] = san
        new_move["before"] = before
        new_move["after"] = after
        new_move["win_before"] = (None if win_before is None else round(float(win_before), 2))
        new_move["win_after"] = (None if win_after is None else round(float(win_after), 2))
        new_move["drop"] = (None if drop is None else round(float(drop), 2))
        new_move["classification"] = classification
        new_move["best_move_san"] = best_move_san
        new_move["best_eval"] = best_eval
        new_move["best_win"] = (None if best_win is None else round(float(best_win), 2))
        new_move["clock_used"] = clock_used
        new_move["time_pressure"] = time_pressure
        new_move["best_line"] = best_line
        new_move["played_line"] = played_line
        new_move["themes"] = themes
        if client_lines is not None:
            new_move["client_lines"] = client_lines
        # remove alias keys
        for alias in ("clientLines", "lines"):
            new_move.pop(alias, None)

        enriched_moves.append(new_move)

        # advance board
        board.push(played_move)

    # --- aggregates ---
    user_is_white = user_color == "white"
    user_indices = [i for i in range(len(enriched_moves)) if (i % 2 == 0) == user_is_white]

    loss_values = [enriched_moves[i]["drop"] for i in user_indices if enriched_moves[i].get("drop") is not None]
    avg_win_loss = round(sum(loss_values) / len(loss_values), 2) if loss_values else 0.0

    before_values = [
        enriched_moves[i]["win_before"] for i in user_indices if enriched_moves[i].get("win_before") is not None
    ]
    avg_win_before = round(sum(before_values) / len(before_values), 2) if before_values else 0.0

    blunders = [i for i in user_indices if enriched_moves[i].get("classification") == "blunder"]
    mistakes = [i for i in user_indices if enriched_moves[i].get("classification") == "mistake"]
    inaccuracies = [i for i in user_indices if enriched_moves[i].get("classification") == "inaccuracy"]
    missed_wins = [
        i
        for i in user_indices
        if enriched_moves[i].get("win_before") is not None
        and enriched_moves[i]["win_before"] >= 95.0
        and enriched_moves[i].get("win_after") is not None
        and enriched_moves[i]["win_after"] <= 90.0
    ]
    time_pressure_blunders = [i for i in blunders if enriched_moves[i].get("time_pressure") is True]

    summary: dict[str, Any] = {
        "game_id": game_id,
        "user_color": user_color,
        "result_for_user": result_for_user,
        "opponent": opponent,
        "avg_win_loss": avg_win_loss,
        "avg_win_before": avg_win_before,
        "blunders": blunders,
        "mistakes": mistakes,
        "inaccuracies": inaccuracies,
        "missed_wins": missed_wins,
        "time_pressure_blunders": time_pressure_blunders,
        "moves": enriched_moves,
        "analysis_source": "client",
        "engine": "stockfish-wasm",
        "depth": depth,
        "multipv": multipv,
    }

    # Preserve optional top-level fields from analysis if present and not conflicting.
    for key in ("result_for_user", "opponent"):
        if key in analysis and analysis.get(key) is not None:
            summary[key] = analysis.get(key)

    return summary
