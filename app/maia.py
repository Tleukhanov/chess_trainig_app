"""Политики «человеческого» выбора хода: мягкие распределения по линиям движка.

MaiaLitePolicy строит распределение вероятностей из оценок Stockfish,
когда бинарь Maia не подключён; MaiaBinaryPolicy — обёртка над реальной
UCI-моделью Maia (maia-chess.net). Обе реализуют протокол HumanPolicy:
из позиции возвращают (SAN, вероятность) для стороны, чей ход.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Callable, Protocol

import chess
import chess.engine

from .analyzer import Engine, win_percent

__all__ = [
    "EngineNotConfiguredError",
    "HumanPolicy",
    "MaiaLitePolicy",
    "MaiaBinaryPolicy",
]


class EngineNotConfiguredError(RuntimeError):
    """Бинарь Maia не настроен: пустой MAIA_PATH либо файла не существует."""


class HumanPolicy(Protocol):
    """Протокол «человекоподобной» политики выбора хода.

    probs(board) возвращает список пар (SAN, вероятность из (0, 1]) с
    суммой ≈ 1.0, отсортированный по убыванию вероятности. SAN выражен в
    системе координат board.turn — стороны, за которую строится политика.
    """

    def probs(self, board: chess.Board, *, k: int = 8) -> list[tuple[str, float]]: ...


def _softmax(
    lines: list[dict[str, Any]],
    *,
    side_is_white: bool,
    temperature: float = 15.0,
) -> list[tuple[str, float]]:
    """Мягкое распределение по линиям: prob ∝ exp(side_win / temperature).

    line["win"] — вероятность победы белых; side_win пересчитывается на
    сторону, для которой строятся вероятности. Линии без pv пропускаются.
    Возвращает пары (SAN, вероятность), отсортированные по убыванию.
    """
    logits: list[tuple[str, float]] = []
    for line in lines:
        pv = line.get("pv")
        win = line.get("win")
        if not pv or not isinstance(win, (int, float)):
            continue
        side_win = win if side_is_white else 100.0 - win
        logits.append((str(pv[0]), side_win / temperature))
    if not logits:
        return []
    max_logit = max(logit for _, logit in logits)
    exps = [math.exp(logit - max_logit) for _, logit in logits]
    total = sum(exps)
    result = [
        (san, exp / total)
        for (san, _), exp in zip(logits, exps)
        if exp / total > 0
    ]
    result.sort(key=lambda item: item[1], reverse=True)
    return result


class MaiaLitePolicy:
    """«Прокси» человека на мягком распределении Stockfish.

    Не требует подключённой Maia: чем выше оценка хода для стороны, чей
    ход, тем выше его вероятность в softmax-распределении. Температура
    задаёт «ширину» распределения между топ-ходами.
    """

    def __init__(
        self,
        *,
        temperature: float = 15.0,
        k_max: int = 16,
        depth: int = 12,
        time_ms: int = 400,
        engine_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.temperature = temperature
        self.k_max = k_max
        self.depth = depth
        self.time_ms = time_ms
        self._engine_factory = engine_factory or (
            lambda: Engine(depth=depth, multipv=k_max, time_ms=time_ms)
        )
        self._engine: Any = None

    def _effective_temperature(self) -> float:
        """Температура не ниже 1.0: размазывание не вырождается в argmax."""
        return self.temperature if self.temperature > 1.0 else 1.0

    def __enter__(self) -> MaiaLitePolicy:
        engine = self._engine_factory()
        if hasattr(engine, "__enter__"):
            engine = engine.__enter__()
        self._engine = engine
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Останавливает свой движок, если он запущен."""
        if self._engine is None:
            return
        close = getattr(self._engine, "close", None)
        if callable(close):
            close()
        self._engine = None

    def probs(self, board: chess.Board, *, k: int = 8) -> list[tuple[str, float]]:
        """Распределение вероятностей ходов из позиции board."""
        if self._engine is None:
            raise RuntimeError(
                "движок не запущен: используй контекстный менеджер или __enter__"
            )
        lines = self._engine.analyze_position(board)
        count = max(1, k)
        return _softmax(
            list(lines[:count]),
            side_is_white=board.turn == chess.WHITE,
            temperature=self._effective_temperature(),
        )


class MaiaBinaryPolicy:
    """Плагин реальной Maia: UCI-совместимый бинарь (например, Maia 2).

    Движок поднимается лениво в __enter__ через chess.engine.SimpleEngine.
    Если MAIA_PATH пуст или файла нет — EngineNotConfiguredError ещё при
    конструировании; без бинаря доступна только MaiaLitePolicy.
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        depth: int = 2,
        k_max: int = 16,
        timeout: float = 60.0,
    ) -> None:
        path_value = path if path is not None else os.environ.get("MAIA_PATH", "")
        self.path = Path(path_value)
        self.depth = depth
        self.k_max = k_max
        self.timeout = timeout
        if not path_value or not self.path.is_file():
            raise EngineNotConfiguredError(
                "Maia не настроена: задай MAIA_PATH (см. maia-chess.net). "
                "Без бинаря работает только MaiaLite."
            )
        self._engine: chess.engine.SimpleEngine | None = None

    def __enter__(self) -> MaiaBinaryPolicy:
        if self._engine is not None:
            return self
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(str(self.path))
        except Exception as exc:
            raise RuntimeError(f"Не удалось запустить Maia ({self.path}): {exc}") from exc
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Останавливает движок Maia, если он запущен."""
        if self._engine is None:
            return
        try:
            self._engine.quit()
        finally:
            self._engine = None

    @staticmethod
    def _move_san(board: chess.Board, move: chess.Move) -> str:
        """Переводит Move в SAN относительно текущей позиции board."""
        copy = board.copy()
        try:
            return copy.san(move)
        except ValueError:
            return ""

    @staticmethod
    def _white_win(score: Any) -> float | None:
        """Вероятность победы белых из score движка (cp/mate)."""
        score = score.white()
        if score.is_mate():
            return win_percent(None, score.mate())
        return win_percent(float(score.score()), None)

    def probs(self, board: chess.Board, *, k: int = 8) -> list[tuple[str, float]]:
        """Распределение вероятностей ходов реальной Maia из позиции board."""
        if self._engine is None:
            raise RuntimeError(
                "движок не запущен: используй контекстный менеджер или __enter__"
            )
        multipv = min(max(k, 2), self.k_max)
        try:
            infos = self._engine.analyse(
                board,
                chess.engine.Limit(depth=self.depth),
                multipv=multipv,
            )
        except Exception as exc:
            raise RuntimeError(f"Ошибка анализа Maia ({self.path}): {exc}") from exc
        lines: list[dict[str, Any]] = []
        for info in infos:
            score = info.get("score")
            pv = list(info.get("pv") or [])
            if score is None or not pv:
                continue
            san = self._move_san(board, pv[0])
            if not san:
                continue
            win = self._white_win(score)
            if win is None:
                continue
            lines.append({"pv": [san], "win": win})
        return _softmax(lines, side_is_white=board.turn == chess.WHITE, temperature=15.0)