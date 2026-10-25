"""Гибридный соперник: Maia-2 (человечность) + фильтр Stockfish (сила).

Maia предсказывает человеческие ходы, но её потолок — бин ``>=2000`` Elo,
поэтому для сильных игроков чистая Maia не растёт. Фильтр Stockfish отбрасывает
ходы с большой потерей оценки (зевки/грубые ошибки), и из оставшихся
«человеческих, но не зевковых» ходов семплируется по вероятностям Maia.
Результат: соперник играет как сильный человек (~2200-2400), а не как движок.

Алгоритм отбора:

* базовая оценка позиции — лучшая линия Stockfish (это и есть «лучшая оценка»);
* ход отбрасывается, если потеря относительно неё больше ``threshold_cp``;
* но всегда остаётся минимум ``min_candidates`` ходов (топ по вероятности Maia),
  иначе соперник всегда играл бы один и тот же лучший ход движка;
* добивка до ``min_candidates`` ограничена жёстким потолком ``2*threshold_cp``;
* если все кандидаты Maia — грубые зевки (потеря > ``2*threshold_cp``), играется
  лучший ход движка;
* вероятности оставшихся ходов нормализуются к 1.

Оценки Stockfish кэшируются в пределах одного вызова :meth:`HybridPolicy.probs`
(``dict`` FEN -> eval), между вызовами кэш не сохраняется. Если Stockfish
недоступен/упал/не уложился во время — политика деградирует в чистую Maia.

Память: Maia (~179 MB ONNX) и Stockfish живут одновременно. На Heroku eco
(512 MB) это рискованно; рекомендуется сервер с >=1 GB RAM.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import chess

from .config import settings

__all__ = ["HybridPolicy"]

logger = logging.getLogger(__name__)

# Сколько кандидатов запрашивать у Maia: даже при k=1 нужен запас для фильтра.
_MAIA_CANDIDATE_FLOOR = 12
_ENGINE_THREADS = 1
_ENGINE_HASH_MB = 16
_MATE_CP = 10000.0


def _maia_policy(self_elo: int, opp_elo: int) -> Any:
    from .maia_onnx import get_policy

    return get_policy(self_elo=self_elo, opp_elo=opp_elo)


def _bounded_int(value: Any, default: int, minimum: int) -> int:
    """Приводит настройку к int и не даёт уйти ниже ``minimum``."""
    if value is None:
        value = default
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = int(default)
    return max(minimum, number)


def _normalize(candidates: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Оставляет валидные пары (SAN, вес) и нормирует сумму весов к 1."""
    cleaned: list[tuple[str, float]] = []
    seen: set[str] = set()
    for san, prob in candidates:
        if not san or san in seen:
            continue
        try:
            weight = float(prob)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(weight) or weight < 0:
            continue
        seen.add(san)
        cleaned.append((str(san), weight))
    if not cleaned:
        return []
    total = sum(weight for _, weight in cleaned)
    if total <= 0:
        share = 1.0 / len(cleaned)
        return [(san, share) for san, _ in cleaned]
    return [(san, weight / total) for san, weight in cleaned]


def _eval_white_from_lines(lines: list[dict[str, Any]] | None) -> tuple[float | None, str | None]:
    """Достаёт (оценка белых в cp, лучший ход SAN) из линий движка.

    ``app.analyzer.Engine`` кладёт eval как dict ``{"cp", "mate"}``, поэтому
    поддерживаем и dict, и объект с атрибутами (на случай другой реализации).
    Мат кодируется как ±10000 cp в перспективе белых.
    """
    if not lines:
        return None, None
    top = lines[0]
    if not isinstance(top, dict):
        return None, None

    best_san: str | None = None
    pv = top.get("pv") or []
    if pv:
        best_san = str(pv[0])

    raw = top.get("eval")
    cp: Any = None
    mate: Any = None
    if isinstance(raw, dict):
        cp, mate = raw.get("cp"), raw.get("mate")
    elif raw is not None:
        cp, mate = getattr(raw, "cp", None), getattr(raw, "mate", None)

    if mate is not None:
        try:
            mate_value = int(mate)
        except (TypeError, ValueError):
            mate_value = None
        if mate_value is not None:
            return (_MATE_CP if mate_value > 0 else -_MATE_CP), best_san

    if cp is None:
        return None, best_san
    try:
        cp_value = float(cp)
    except (TypeError, ValueError):
        return None, best_san
    if not math.isfinite(cp_value):
        return None, best_san
    return cp_value, best_san


class HybridPolicy:
    """Соперник: Maia-кандидаты, отфильтрованные по оценке Stockfish."""

    def __init__(
        self,
        self_elo: int,
        opp_elo: int,
        threshold_cp: int | None = None,
        depth: int | None = None,
        time_ms: int | None = None,
        min_candidates: int | None = None,
    ) -> None:
        self.self_elo = int(self_elo)
        self.opp_elo = int(opp_elo)
        # Порог >0: потеря 0 cp — это и есть лучший ход.
        self.threshold_cp = _bounded_int(threshold_cp, settings.hybrid_threshold_cp, 1)
        self.depth = _bounded_int(depth, settings.hybrid_depth, 1)
        # time_ms == 0 допустим: тогда ограничение только по глубине.
        self.time_ms = _bounded_int(time_ms, settings.hybrid_time_ms, 0)
        self.min_candidates = _bounded_int(min_candidates, settings.hybrid_min_candidates, 1)
        self._maia_cached = None

    def _maia(self):
        if self._maia_cached is None:
            self._maia_cached = _maia_policy(self.self_elo, self.opp_elo)
        return self._maia_cached

    def _analyze_cached(
        self,
        engine: Any,
        board: chess.Board,
        cache: dict[str, tuple[float | None, str | None]],
    ) -> tuple[float | None, str | None]:
        """Оценка позиции с кэшем в рамках одного вызова ``probs``."""
        fen = board.fen()
        if fen in cache:
            return cache[fen]
        try:
            lines = engine.analyze_position(board)
        except Exception as exc:  # noqa: BLE001
            # Таймаут/падение движка на одной позиции не должны ронять ход.
            logger.debug("Hybrid: Stockfish analysis failed for %s: %s", fen, exc)
            result: tuple[float | None, str | None] = (None, None)
        else:
            result = _eval_white_from_lines(lines)
        cache[fen] = result
        return result

    def _select(
        self,
        board: chess.Board,
        maia_candidates: list[tuple[str, float]],
        limit: int,
        engine: Any,
    ) -> list[tuple[str, float]]:
        """Фильтрует кандидатов Maia по оценкам Stockfish и нормирует веса."""
        cache: dict[str, tuple[float | None, str | None]] = {}
        base_eval, engine_best = self._analyze_cached(engine, board, cache)
        if base_eval is None:
            logger.warning(
                "Hybrid: Stockfish недоступен для позиции %s — играю чистую Maia",
                board.fen(),
            )
            return _normalize(maia_candidates[:limit])

        side_is_white = board.turn == chess.WHITE
        evaluated: list[tuple[str, float, float | None]] = []
        for san, prob in maia_candidates:
            try:
                move = board.parse_san(san)
            except ValueError:
                logger.debug("Hybrid: пропускаю нелегальный SAN %r", san)
                continue
            try:
                weight = float(prob)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(weight) or weight <= 0:
                continue

            child = board.copy()
            child.push(move)
            child_eval, _ = self._analyze_cached(engine, child, cache)
            if child_eval is None:
                # Оценки нет — не наказываем ход, но и не отбрасываем его.
                loss: float | None = None
            elif side_is_white:
                loss = base_eval - child_eval
            else:
                loss = child_eval - base_eval
            evaluated.append((str(san), weight, loss))

        if not evaluated:
            if engine_best:
                try:
                    board.parse_san(engine_best)
                except ValueError:
                    pass
                else:
                    return [(engine_best, 1.0)]
            return _normalize(maia_candidates[:1])

        # Топ по вероятности Maia — и для фильтра, и для добивки.
        evaluated.sort(key=lambda item: item[1], reverse=True)

        threshold = float(self.threshold_cp)
        hard_cap = 2.0 * threshold
        survivors: list[tuple[str, float]] = [
            (san, weight) for san, weight, loss in evaluated if loss is None or loss <= threshold
        ]

        if len(survivors) < self.min_candidates:
            chosen = {san for san, _ in survivors}
            for san, weight, loss in evaluated:
                if len(survivors) >= self.min_candidates:
                    break
                if san in chosen:
                    continue
                if loss is not None and loss > hard_cap:
                    continue
                survivors.append((san, weight))
                chosen.add(san)

        if not survivors:
            # Все ходы Maia — грубые зевки: играем лучший ход движка.
            logger.debug("Hybrid: все кандидаты Maia — зевки, играю лучший ход движка")
            if engine_best:
                try:
                    board.parse_san(engine_best)
                except ValueError:
                    pass
                else:
                    return [(engine_best, 1.0)]
            # Фолбэк: самый вероятный ход Maia.
            return _normalize(maia_candidates[:1])

        survivors.sort(key=lambda item: item[1], reverse=True)
        return _normalize(survivors)

    def probs(self, board: chess.Board, k: int = 8) -> list[tuple[str, float]]:
        limit = max(1, int(k))
        try:
            maia_candidates = list(
                self._maia().probs(board, k=max(limit, _MAIA_CANDIDATE_FLOOR))
            )
        except Exception:  # noqa: BLE001
            logger.exception("Hybrid: Maia не смогла предложить ходы")
            return []
        if not maia_candidates:
            return []

        try:
            from .analyzer import Engine

            with Engine(
                depth=self.depth,
                time_ms=self.time_ms,
                threads=_ENGINE_THREADS,
                hash_mb=_ENGINE_HASH_MB,
            ) as engine:
                return self._select(board, maia_candidates, limit, engine)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Hybrid: Stockfish недоступен (%s) — играю чистую Maia", exc
            )
            return _normalize(maia_candidates[:limit])

    def close(self) -> None:
        # Maia-сессия общая (singleton) — не закрываем; просто сбрасываем ссылку.
        self._maia_cached = None
