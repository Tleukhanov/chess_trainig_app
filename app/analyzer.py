"""Stockfish-анализ партий: модель оценки ходов и классификация ошибок игрока.

Модуль самодостаточен и опирается только на python-chess, app.config и app.games.
Оценки движка нормализуются через ``score.white()`` и, где необходимо,
пересчитываются на сторону, чей ход, чтобы мерить «за себя», а не «за белых».
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chess
import chess.engine

from .config import settings
from .games import Game

__all__ = ["Eval", "win_percent", "classify_move", "MoveAnalysis", "GameAnalysis", "Engine"]


@dataclass(frozen=True, slots=True)
class Eval:
    """Оценка позиции: либо cp, либо mate (модуль ходов до мата).

    cp задан в центипешках (как у движка): 100 == 1 пешка.
    Объект всегда выражен в перспективе той стороны, «за которую» он построен:
    положительный mate означает, что эта сторона ставит мат.
    """

    cp: float | None = None
    mate: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"cp": self.cp, "mate": self.mate}


def win_percent(cp_white: float | None, mate: int | None = None) -> float:
    """Переводит оценку в вероятность победы (0..100) по личессовской логистике.

    Аргументы должны быть в перспективе стороны, для которой считается вероятность.
    """
    if mate is not None:
        if mate > 0:
            return 100.0
        if mate < 0:
            return 0.0
        return 50.0
    if cp_white is None:
        return 50.0
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-0.00368208 * cp_white)) - 1.0)


def classify_move(win_before: float | None, win_after: float | None, is_best: bool = False) -> str:
    """Классифицирует ход по потере win.% (пороги личесса: 20/10/5).

    Значения win_before/win_after — вероятности для стороны, чей ход.
    """
    if win_before is None or win_after is None:
        return "good"
    if win_before >= 99.9 and win_after < 95.0:
        return "blunder"
    drop = win_before - win_after
    if is_best and drop < 2.0:
        return "best"
    if drop >= 20.0:
        return "blunder"
    if drop >= 10.0:
        return "mistake"
    if drop >= 5.0:
        return "inaccuracy"
    return "good"


@dataclass(frozen=True, slots=True)
class MoveAnalysis:
    """Анализ одного полухода партии.

    before/after — оценки позиции до/после хода в перспективе стороны, чей ход.
    win_before/win_after — вероятность победы для той же стороны.
    drop — потеря win.% (win_before - win_after).
    cp_loss — потеря относительно лучшей линии: так как оценка позиции движком
        и есть проекция лучшей линии, cp_loss совпадает с drop в win.п.
    """

    san: str
    before: Eval
    after: Eval
    win_before: float | None
    win_after: float | None
    drop: float | None
    classification: str
    best_move_san: str | None = None
    best_eval: Eval | None = None
    best_win: float | None = None
    cp_loss: float | None = None
    clock_used: float | None = None
    time_pressure: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "san": self.san,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "win_before": self.win_before,
            "win_after": self.win_after,
            "drop": self.drop,
            "classification": self.classification,
            "best_move_san": self.best_move_san,
            "best_eval": self.best_eval.to_dict() if self.best_eval else None,
            "best_win": self.best_win,
            "cp_loss": self.cp_loss,
            "clock_used": self.clock_used,
            "time_pressure": self.time_pressure,
        }


@dataclass(frozen=True, slots=True)
class GameAnalysis:
    """Результат анализа партии; агрегаты считаются по ходам пользователя.

    Индексы в blunders/mistakes/inaccuracies/missed_wins и пр. — это номера
    полуходов в ``moves`` (ply), но только тех, что сделал пользователь.
    """

    game_id: str
    user_color: str
    moves: list[MoveAnalysis]
    acpl: float
    accuracy: float
    blunders: list[int]
    mistakes: list[int]
    inaccuracies: list[int]
    missed_wins: list[int]
    time_pressure_blunders: list[int]
    result_for_user: str | None = None
    opponent: str | None = None

    def summary(self) -> dict[str, Any]:
        """Резюме анализа для сериализации в JSON (только плоские типы)."""
        return {
            "game_id": self.game_id,
            "user_color": self.user_color,
            "result_for_user": self.result_for_user,
            "opponent": self.opponent,
            "acpl": self.acpl,
            "accuracy": self.accuracy,
            "blunders": self.blunders,
            "mistakes": self.mistakes,
            "inaccuracies": self.inaccuracies,
            "missed_wins": self.missed_wins,
            "time_pressure_blunders": self.time_pressure_blunders,
            "moves": [move.to_dict() for move in self.moves],
        }


def _eval_from_white_score(score: Any) -> Eval:
    """Из score.white() движка строит Eval в перспективе белых (cp в центипешках)."""
    if score.is_mate():
        return Eval(cp=None, mate=score.mate())
    return Eval(cp=float(score.score()), mate=None)


def _flip(eval_: Eval) -> Eval:
    """Меняет перспективу оценки на противоположную сторону."""
    if eval_.mate is not None:
        return Eval(cp=None, mate=-eval_.mate)
    return Eval(cp=(-eval_.cp if eval_.cp is not None else None), mate=None)


def _eval_for_side(eval_white: Eval, side_is_white: bool) -> Eval:
    """Переводит оценку «за белых» в оценку для указанной стороны."""
    if side_is_white:
        return eval_white
    return _flip(eval_white)


def _side_win(eval_side: Eval) -> float:
    """Вероятность победы стороны, за которую выражена оценка."""
    return win_percent(eval_side.cp, eval_side.mate)


def _pv_to_san(board: chess.Board, pv: list[chess.Move]) -> list[str]:
    """Переводит линии движка в SAN относительно текущей позиции."""
    copy = board.copy()
    result: list[str] = []
    for move in pv:
        try:
            result.append(copy.san(move))
        except ValueError:
            break
        copy.push(move)
    return result


def _san_key(san: str) -> str:
    """Нормализация SAN для сравнения: убирает суффиксы шаха/мата."""
    return san.rstrip("+#")


class Engine:
    """Обёртка над Stockfish: контекстный менеджер для анализа позиций и партий."""

    def __init__(
        self,
        path: str | Path | None = None,
        depth: int | None = None,
        multipv: int | None = None,
        threads: int | None = None,
        hash_mb: int | None = None,
    ) -> None:
        self.path = Path(path or settings.stockfish_path)
        if not self.path.is_file():
            raise RuntimeError(
                f"Stockfish не найден по пути {self.path}. "
                "Задай STOCKFISH_PATH (например, в .env или переменной окружения)."
            )
        self.depth = depth if depth is not None else settings.stockfish_depth
        self.multipv = multipv if multipv is not None else settings.stockfish_multipv
        self.threads = threads if threads is not None else settings.stockfish_threads
        self.hash_mb = hash_mb if hash_mb is not None else settings.stockfish_hash_mb
        self._engine: chess.engine.SimpleEngine | None = None

    def _ensure_started(self) -> None:
        if self._engine is not None:
            return
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(str(self.path))
        except Exception as exc:
            raise RuntimeError(f"Не удалось запустить Stockfish ({self.path}): {exc}") from exc
        self._engine.configure({"Threads": self.threads, "Hash": self.hash_mb})

    def close(self) -> None:
        if self._engine is None:
            return
        try:
            self._engine.quit()
        finally:
            self._engine = None

    def __enter__(self) -> Engine:
        self._ensure_started()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self.close()

    def analyze_position(self, board: chess.Board) -> list[dict[str, Any]]:
        """Анализирует позицию на глубину depth и возвращает multipv-линии.

        Каждая линия: {"pv": [SAN...], "eval": {cp, mate}, "win": win% для белых}.
        """
        self._ensure_started()
        assert self._engine is not None
        infos = self._engine.analyse(
            board, chess.engine.Limit(depth=self.depth), multipv=self.multipv
        )
        lines: list[dict[str, Any]] = []
        for info in infos:
            score = info.get("score")
            if score is None:
                continue
            eval_white = _eval_from_white_score(score.white())
            pv = list(info.get("pv", []) or [])
            lines.append(
                {
                    "pv": _pv_to_san(board, pv),
                    "eval": eval_white.to_dict(),
                    "win": win_percent(eval_white.cp, eval_white.mate),
                    "multipv": int(info.get("multipv", 1)),
                }
            )
        lines.sort(key=lambda line: line["multipv"])
        return lines

    def analyze_game(self, game: Game) -> GameAnalysis:
        """Анализирует партию: перед ходом анализирует позицию и после него.

        win/drop/classification считаются для стороны, чей ход; агрегаты
        (acpl, blunders и т.п.) — только по ходам пользователя.
        """
        board = chess.Board()
        analyses: list[MoveAnalysis] = []
        user_is_white = game.user_color == "white"

        for ply, san in enumerate(game.moves):
            mover_is_white = ply % 2 == 0
            mover = "white" if mover_is_white else "black"

            before_lines = self.analyze_position(board)
            if not before_lines:
                break
            top = before_lines[0]
            top_eval = top["eval"]
            best_eval_white = Eval(cp=top_eval["cp"], mate=top_eval["mate"])
            best_move_san: str | None = top["pv"][0] if top["pv"] else None
            before_side = _eval_for_side(best_eval_white, mover_is_white)
            win_before = _side_win(before_side)

            try:
                board.push_san(san)
            except Exception as exc:
                raise RuntimeError(
                    f"Не удалось применить ход SAN '{san}' (полуход {ply})"
                ) from exc

            if board.is_game_over():
                if board.is_checkmate():
                    after_side = Eval(cp=None, mate=1)
                    win_after = 100.0
                else:
                    after_side = Eval(cp=0.0, mate=None)
                    win_after = 50.0
            else:
                after_lines = self.analyze_position(board)
                if not after_lines:
                    break
                after_eval = after_lines[0]["eval"]
                after_eval_white = Eval(cp=after_eval["cp"], mate=after_eval["mate"])
                after_opp = _eval_for_side(after_eval_white, not mover_is_white)
                after_side = _flip(after_opp)
                win_after = 100.0 - _side_win(after_opp)

            drop = win_before - win_after
            is_best = best_move_san is not None and _san_key(best_move_san) == _san_key(san)
            classification = classify_move(win_before, win_after, is_best=is_best)

            clock_used: float | None = None
            time_pressure: bool | None = None
            if ply < len(game.clocks):
                current = game.clocks[ply]
                if current is not None:
                    time_pressure = current < 30.0
                    if ply >= 2 and ply - 2 < len(game.clocks):
                        prev = game.clocks[ply - 2]
                        if prev is not None:
                            clock_used = max(0.0, prev - current)

            analyses.append(
                MoveAnalysis(
                    san=san,
                    before=before_side,
                    after=after_side,
                    win_before=round(win_before, 2),
                    win_after=round(win_after, 2),
                    drop=round(drop, 2),
                    classification=classification,
                    best_move_san=best_move_san,
                    best_eval=before_side,
                    best_win=round(win_before, 2),
                    cp_loss=round(drop, 2),
                    clock_used=clock_used,
                    time_pressure=time_pressure,
                )
            )

        user_indices = [i for i in range(len(analyses)) if (i % 2 == 0) == user_is_white]

        cpl_values = [analyses[i].cp_loss for i in user_indices if analyses[i].cp_loss is not None]
        acpl = round(sum(cpl_values) / len(cpl_values), 2) if cpl_values else 0.0

        acc_values = [analyses[i].win_before for i in user_indices if analyses[i].win_before is not None]
        accuracy = round(sum(acc_values) / len(acc_values), 2) if acc_values else 0.0

        blunders = [i for i in user_indices if analyses[i].classification == "blunder"]
        mistakes = [i for i in user_indices if analyses[i].classification == "mistake"]
        inaccuracies = [i for i in user_indices if analyses[i].classification == "inaccuracy"]
        missed_wins = [
            i
            for i in user_indices
            if analyses[i].win_before is not None
            and analyses[i].win_before >= 95.0
            and analyses[i].win_after is not None
            and analyses[i].win_after <= 90.0
        ]
        time_pressure_blunders = [i for i in blunders if analyses[i].time_pressure is True]

        return GameAnalysis(
            game_id=game.id,
            user_color=game.user_color,
            moves=analyses,
            acpl=acpl,
            accuracy=accuracy,
            blunders=blunders,
            mistakes=mistakes,
            inaccuracies=inaccuracies,
            missed_wins=missed_wins,
            time_pressure_blunders=time_pressure_blunders,
            result_for_user=game.result_for_user,
            opponent=game.opponent,
        )