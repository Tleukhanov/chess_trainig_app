"""Гибридный соперник: Maia-2 (человечность) + фильтр Stockfish (сила).

Maia предсказывает человеческие ходы, но её потолок — бин ``>=2000`` Elo,
поэтому для сильных игроков чистая Maia не растёт. Фильтр Stockfish отбрасывает
ходы с большой потерей оценки (зевки/грубые ошибки), и из оставшихся
«человеческих, но не зевковых» ходов семплируется по вероятностям Maia.
Результат: соперник играет как сильный человек (~2200-2400), а не как движок.

Память: Maia (~179 MB ONNX) и Stockfish живут одновременно. На Heroku eco
(512 MB) это рискованно; рекомендуется сервер с >=1 GB RAM. При отсутствии
Stockfish/ошибке фильтрации политика деградирует в чистую Maia.
"""

from __future__ import annotations

import chess
from typing import Any

from .config import settings


def _maia_policy(self_elo: int, opp_elo: int) -> Any:
    from .maia_onnx import get_policy

    return get_policy(self_elo=self_elo, opp_elo=opp_elo)


class HybridPolicy:
    """Соперник: Maia-кандидаты, отфильтрованные по оценке Stockfish."""

    def __init__(
        self,
        self_elo: int,
        opp_elo: int,
        threshold_cp: int = 100,
        depth: int = 10,
        time_ms: int = 500,
    ) -> None:
        self.self_elo = int(self_elo)
        self.opp_elo = int(opp_elo)
        self.threshold_cp = int(threshold_cp)
        self.depth = int(depth)
        self.time_ms = int(time_ms)
        self._maia = None

    def _maia(self):  # type: ignore[override]
        if self._maia is None:
            self._maia = _maia_policy(self.self_elo, self.opp_elo)
        return self._maia

    def _eval_white(self, board: chess.Board) -> float | None:
        """Оценка позиции в centipawn (белые). None при ошибке."""
        try:
            from .analyzer import Engine

            with Engine(depth=self.depth, time_ms=self.time_ms, threads=1, hash_mb=16) as engine:
                lines = engine.analyze_position(board)
        except Exception:  # noqa: BLE001
            return None
        if not lines:
            return None
        ev = lines[0].get("eval")
        if ev is None:
            return None
        cp = getattr(ev, "cp", None)
        mate = getattr(ev, "mate", None)
        if mate is not None:
            # Мат: большое число со знаком.
            return 10000.0 if mate > 0 else -10000.0
        try:
            return float(cp) if cp is not None else None
        except (TypeError, ValueError):
            return None

    def probs(self, board: chess.Board, k: int = 8) -> list[tuple[str, float]]:
        maia_candidates = self._maia().probs(board, k=max(k, 12))
        if not maia_candidates:
            return []

        # Оценка текущей позиции (лучший ход движка) — базовая линия.
        side_is_white = board.turn == chess.WHITE
        base_eval = self._eval_white(board)
        if base_eval is None:
            # Stockfish недоступен — деградируем в чистую Maia.
            return maia_candidates[:k]

        survivors: list[tuple[str, float]] = []
        for san, prob in maia_candidates:
            try:
                move = board.parse_san(san)
            except ValueError:
                continue
            child = board.copy()
            child.push(move)
            child_eval = self._eval_white(child)
            if child_eval is None:
                # Не смогли оценить — оставляем ход (лучше сыграть, чем упасть).
                survivors.append((san, prob))
                continue
            # Потеря для стороны, чей ход: ухудшение оценки с её точки зрения.
            if side_is_white:
                loss = base_eval - child_eval
            else:
                loss = child_eval - base_eval
            if loss <= self.threshold_cp:
                survivors.append((san, prob))

        if not survivors:
            # Все ходы Maia — зевки: берём лучший ход движка.
            try:
                from .analyzer import Engine

                with Engine(depth=self.depth, time_ms=self.time_ms, threads=1, hash_mb=16) as engine:
                    lines = engine.analyze_position(board)
                if lines and lines[0].get("pv"):
                    best_san = lines[0]["pv"][0]
                    # pv уже в SAN
                    return [(best_san, 1.0)]
            except Exception:  # noqa: BLE001
                pass
            # Фолбэк: лучший кандидат Maia
            return [maia_candidates[0]]

        # Ренормализация вероятностей
        total = sum(p for _, p in survivors)
        if total <= 0:
            return survivors
        return [(san, prob / total) for san, prob in survivors]

    def close(self) -> None:
        # Maia-сессия общая (singleton) — не закрываем; просто сбрасываем ссылку.
        self._maia = None
