"""Теория дебютов: главная линия позиции и LLM-объяснение идеи.

Архитектура намеренно провайдер-независимая: API и кеш работают с
:class:`TheoryProvider`, а источник данных (Lichess Explorer, офлайн-книга
ECO, «выключено») выбирается настройкой ``THEORY_PROVIDER``. Чтобы поднять
качество, достаточно добавить нового провайдера и ветку в :func:`get_provider`
— вызывающий код не меняется.

Провайдеры синхронные и не знают про FastAPI. Сеть — только стандартная
библиотека (urllib), как в ``app.games``/``app.llm``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol, runtime_checkable

import chess

from .config import settings
from .db import Database
from .games import USER_AGENT
from .llm import (
    ChatMessage,
    LLMClient,
    clean_llm_text,
    looks_like_refusal,
    looks_like_thinking,
)
from .openings import classify_opening, theory_moves, theory_moves_fen

logger = logging.getLogger(__name__)

#: Публичный Lichess Explorer (база masters, без токена).
EXPLORER_URL = "https://explorer.lichess.ovh/masters"

#: Разумные границы глубины линии: 1 полуход — 60 полуходов.
MIN_PLIES = 1
MAX_PLIES = 60

_IDEAS_MAX_TOKENS = 500

_IDEAS_SYSTEM = """Ты — шахматный тренер сильного любителя. Отвечай ТОЛЬКО по-русски,
готовым текстом, без промежуточного разбора и без английских слов.
Опирайся ИСКЛЮЧИТЕЛЬНО на переданные факты: FEN, ходы главной линии,
статистику Lichess Explorer и название дебюта (если оно дано).
Категорически запрещено выдумывать ходы, варианты, угрозы, оценки, проценты
и названия дебютов, которых нет в фактах. Если фактов мало — говори только
то, что подтверждено. Напиши 3–5 коротких предложений: идея главной линии и
планы сторон."""


class TheoryUnavailable(RuntimeError):
    """Провайдер теории недоступен (сеть, HTTP, битый ответ)."""


@runtime_checkable
class TheoryProvider(Protocol):
    """Источник главной линии дебюта для позиции.

    ``position`` возвращает::

        {"source": str, "fen": str, "main_line": [
            {"san": str, "uci": str, "fen": str,
             "white": int | None, "draws": int | None,
             "black": int | None, "games": int | None}, ...
        ]}

    ``main_line`` — ходы от переданной позиции, не больше ``plies`` штук;
    ``fen`` в шаге — позиция ПОСЛЕ хода.
    """

    source: str

    def position(self, fen: str, plies: int, moves: list[str] | None = None) -> dict[str, Any]:
        ...


def _history(moves: Any) -> list[str] | None:
    """Нормализует историю SAN из запроса: список строк или None."""
    if moves is None:
        return None
    if isinstance(moves, str):
        return [part.strip() for part in moves.split(",") if part.strip()]
    if isinstance(moves, list):
        return [str(part).strip() for part in moves if str(part).strip()]
    return None


def _board(fen: str) -> chess.Board:
    """Доска из FEN; ValueError с понятным текстом для невалидной строки."""
    if not isinstance(fen, str) or not fen.strip():
        raise ValueError("Пустой FEN")
    try:
        return chess.Board(fen.strip())
    except ValueError as exc:
        raise ValueError(f"Невалидный FEN: {exc}") from exc


def _clamp_plies(plies: Any) -> int:
    """Глубина линии из запроса/настроек, зажатая в [MIN_PLIES, ceiling].

    Потолок — ``settings.theory_max_plies`` (не больше технического
    ``MAX_PLIES``): настройка ограничивает, сколько запросов к Explorer
    делает один вызов.
    """
    try:
        configured = int(settings.theory_max_plies)
    except (TypeError, ValueError):
        configured = 12
    ceiling = max(MIN_PLIES, min(MAX_PLIES, configured))
    try:
        value = int(plies)
    except (TypeError, ValueError):
        value = ceiling
    return max(MIN_PLIES, min(ceiling, value))


def _stat(value: Any) -> int | None:
    """Неотрицательное целое из статистики Explorer; None — данных нет."""
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _step(
    *,
    san: str,
    uci: str,
    fen: str,
    white: int | None,
    draws: int | None,
    black: int | None,
    games: int | None,
) -> dict[str, Any]:
    """Один шаг линии в едином формате для всех провайдеров."""
    return {
        "san": san,
        "uci": uci,
        "fen": fen,
        "white": white,
        "draws": draws,
        "black": black,
        "games": games,
    }


def _normalize_line(steps: Any) -> list[dict[str, Any]]:
    """Чистит линию от мусора: только валидные шаги известной формы.

    Нужна и для ответа провайдера, и для данных из кеша/тела запроса: наружу
    не должны утекать произвольные поля или полупустые шаги.
    """
    if not isinstance(steps, list):
        return []
    result: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        san = str(step.get("san") or "").strip()
        step_fen = str(step.get("fen") or "").strip()
        if not san or not step_fen:
            continue
        white = _stat(step.get("white"))
        draws = _stat(step.get("draws"))
        black = _stat(step.get("black"))
        games = _stat(step.get("games"))
        if games is None and white is not None and draws is not None and black is not None:
            games = white + draws + black
        result.append(
            _step(
                san=san,
                uci=str(step.get("uci") or ""),
                fen=step_fen,
                white=white,
                draws=draws,
                black=black,
                games=games,
            )
        )
    return result


class LichessExplorerProvider:
    """Lichess Explorer: самый популярный ход на каждом полуходе.

    На каждый полуход — отдельный GET (Explorer отдаёт ходы только для одной
    позиции). Ошибки сети/HTTP/JSON поднимаются как :class:`TheoryUnavailable`,
    чтобы :func:`main_line` мог уйти в офлайн-fallback.
    """

    source = "lichess"

    def __init__(
        self,
        *,
        timeout: float | None = None,
        base_url: str = EXPLORER_URL,
    ) -> None:
        self.timeout = float(settings.theory_timeout if timeout is None else timeout)
        self.base_url = base_url

    def _fetch_moves(self, fen: str) -> list[dict[str, Any]]:
        """Список ходов Explorer для одной позиции (пустой — данных нет)."""
        query = urllib.parse.urlencode(
            {"fen": fen, "topGames": 0, "moves": 12},
            quote_via=urllib.parse.quote,
        )
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        # С февраля 2026 explorer.lichess.ovh требует авторизацию.
        if settings.lichess_token:
            headers["Authorization"] = f"Bearer {settings.lichess_token}"
        request = urllib.request.Request(
            f"{self.base_url}?{query}",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                exc.close()
            except Exception:  # noqa: BLE001 - закрытие не должно маскировать ошибку
                pass
            raise TheoryUnavailable(f"Explorer: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TheoryUnavailable(f"Explorer: сетевая ошибка: {exc}") from exc

        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TheoryUnavailable("Explorer: не удалось разобрать JSON") from exc
        if not isinstance(data, dict):
            raise TheoryUnavailable("Explorer: неожиданный формат ответа")
        moves = data.get("moves")
        if not isinstance(moves, list):
            return []
        return [move for move in moves if isinstance(move, dict)]

    def position(self, fen: str, plies: int, moves: list[str] | None = None) -> dict[str, Any]:
        board = _board(fen)
        depth = _clamp_plies(plies)
        steps: list[dict[str, Any]] = []

        for _ in range(depth):
            moves = self._fetch_moves(board.fen())
            if not moves:
                break
            # Самый популярный ход: max(white + draws + black), при равенстве
            # стабильно первый из ответа Explorer.
            ranked = sorted(
                moves,
                key=lambda move: (
                    (_stat(move.get("white")) or 0)
                    + (_stat(move.get("draws")) or 0)
                    + (_stat(move.get("black")) or 0)
                ),
                reverse=True,
            )
            pushed = False
            for item in ranked:
                uci = str(item.get("uci") or "").strip()
                try:
                    move = chess.Move.from_uci(uci)
                except ValueError:
                    continue
                if move not in board.legal_moves:
                    continue
                san = str(item.get("san") or "").strip() or board.san(move)
                white = _stat(item.get("white"))
                draws = _stat(item.get("draws"))
                black = _stat(item.get("black"))
                games = None
                if white is not None and draws is not None and black is not None:
                    games = white + draws + black
                board.push(move)
                steps.append(
                    _step(
                        san=san,
                        uci=move.uci(),
                        fen=board.fen(),
                        white=white,
                        draws=draws,
                        black=black,
                        games=games,
                    )
                )
                pushed = True
                break
            if not pushed:
                break

        return {"source": self.source, "fen": _board(fen).fen(), "main_line": steps}


class EcoProvider:
    """Офлайн-провайдер по локальной книге ECO.

    Линия строится по FEN-индексу (``theory_moves_fen``), поэтому работает
    из произвольной позиции и ловит транспозиции. Статистики партий у ECO
    нет — только ходы. История SAN из запроса нужна лишь как проверка
    согласованности и не обязательна.
    """

    source = "eco"

    def position(self, fen: str, plies: int, moves: list[str] | None = None) -> dict[str, Any]:
        board = _board(fen)
        history = _history(moves) or []
        if history:
            # Мягкая проверка: если история и FEN расходятся, доверяем FEN.
            replay = chess.Board()
            try:
                for san in history:
                    replay.push_san(san)
                if replay.fen() != board.fen():
                    logger.debug("theory: история SAN не совпала с FEN, использую FEN")
            except ValueError:
                logger.debug("theory: невалидная история SAN, использую FEN")

        depth = _clamp_plies(plies)
        steps: list[dict[str, Any]] = []

        for _ in range(depth):
            # Сначала индекс по FEN (ловит транспозиции), затем префикс SAN.
            options = theory_moves_fen(board.fen()) or theory_moves(history)
            if not options:
                break
            ranked = sorted(options, key=lambda pair: pair[1], reverse=True)
            pushed = False
            for san, _weight in ranked:
                try:
                    move = board.parse_san(san)
                except ValueError:
                    continue
                board.push(move)
                history.append(san)
                steps.append(
                    _step(
                        san=san,
                        uci=move.uci(),
                        fen=board.fen(),
                        white=None,
                        draws=None,
                        black=None,
                        games=None,
                    )
                )
                pushed = True
                break
            if not pushed:
                break

        return {"source": self.source, "fen": chess.Board().fen(), "main_line": steps}


class OffProvider:
    """Теория выключена: всегда пустая линия, без сети и без fallback."""

    source = "off"

    def position(self, fen: str, plies: int, moves: list[str] | None = None) -> dict[str, Any]:
        board = _board(fen)
        return {"source": self.source, "fen": board.fen(), "main_line": []}


def get_provider() -> TheoryProvider:
    """Провайдер по ``settings.theory_provider`` (неизвестное → lichess)."""
    name = (settings.theory_provider or "lichess").strip().lower()
    if name == "eco":
        return EcoProvider()
    if name == "off":
        return OffProvider()
    return LichessExplorerProvider()


# --------------------------------------------------------------------------- #
# Кеш (app.db.theory_cache)
# --------------------------------------------------------------------------- #
def _database(db_path: Any = None) -> Database | None:
    """БД для кеша: None, если открыть/инициализировать не удалось."""
    try:
        db = Database(db_path)
        db.init_db()
        return db
    except Exception as exc:  # noqa: BLE001 - кеш не должен ронять теорию
        logger.debug("theory: БД кеша недоступна: %s", exc)
        return None


def _cache_get(key: str, db_path: Any = None) -> Any | None:
    """Значение из кеша в пределах ``theory_cache_hours`` (0 — кеш выключен)."""
    try:
        hours = float(settings.theory_cache_hours)
    except (TypeError, ValueError):
        hours = 168.0
    if hours <= 0:
        return None
    db = _database(db_path)
    if db is None:
        return None
    try:
        return db.get_theory_cache(key, max_age_hours=hours)
    except Exception as exc:  # noqa: BLE001
        logger.debug("theory: чтение кеша %s не удалось: %s", key, exc)
        return None


def _cache_set(key: str, payload: Any, db_path: Any = None) -> None:
    """Пишет значение в кеш; ошибки БД только логируются."""
    db = _database(db_path)
    if db is None:
        return
    try:
        db.set_theory_cache(key, payload)
    except Exception as exc:  # noqa: BLE001
        logger.debug("theory: запись кеша %s не удалась: %s", key, exc)


def main_line(
    fen: str,
    plies: int | None = None,
    *,
    db_path: Any = None,
    moves: list[str] | None = None,
) -> dict[str, Any]:
    """Главная линия позиции: провайдер → fallback ECO → пустой результат.

    Возвращает ``{"source", "fen", "main_line", "setup_fen"}``. ``setup_fen`` —
    FEN после последнего хода линии (``None`` для пустой линии). Сетевые/HTTP
    ошибки провайдера не пробрасываются: API всегда получает 200, а источник
    деградирует до ``"eco"`` или ``"none"``. Невалидный FEN — ValueError.
    """
    board = _board(fen)
    fen = board.fen()
    depth = _clamp_plies(settings.theory_max_plies if plies is None else plies)
    provider = get_provider()
    cache_key = f"explorer:{fen}:{depth}"

    raw: dict[str, Any] | None = None
    if provider.source == "lichess":
        cached = _cache_get(cache_key, db_path)
        if isinstance(cached, dict) and isinstance(cached.get("main_line"), list):
            raw = cached

    if raw is None:
        try:
            raw = provider.position(fen, depth, _history(moves))
        except Exception as exc:  # noqa: BLE001 - любой сбой = fallback
            logger.debug("theory: провайдер %s недоступен: %s", provider.source, exc)
            raw = None
        if not isinstance(raw, dict):
            raw = None
        if raw is not None:
            steps = _normalize_line(raw.get("main_line"))
            if steps and provider.source == "lichess":
                payload = {
                    "source": str(raw.get("source") or provider.source),
                    "fen": str(raw.get("fen") or fen),
                    "main_line": steps,
                }
                _cache_set(cache_key, payload, db_path)

    steps = _normalize_line(raw.get("main_line")) if raw is not None else []
    source = str(raw.get("source") or provider.source) if raw is not None else provider.source

    # Lichess может ответить пустой позицией (редкий дебют) — пробуем ECO.
    if not steps and provider.source not in ("eco", "off"):
        try:
            eco = EcoProvider().position(fen, depth, _history(moves))
        except ValueError:
            eco = None
        if eco is not None:
            eco_steps = _normalize_line(eco.get("main_line"))
            if eco_steps:
                steps = eco_steps
                source = str(eco.get("source") or "eco")

    if not steps:
        source = "off" if provider.source == "off" else "none"

    return {
        "source": source,
        "fen": fen,
        "main_line": steps,
        "setup_fen": steps[-1]["fen"] if steps else None,
    }


# --------------------------------------------------------------------------- #
# LLM-объяснение идеи линии
# --------------------------------------------------------------------------- #
def ideas_cache_key(fen: str, line: list[dict[str, Any]]) -> str:
    """Ключ кеша идей: FEN + стабильный хеш нормализованной линии."""
    canonical = json.dumps(line, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"ideas:{fen}:{digest}"


def _ideas_prompt(fen: str, steps: list[dict[str, Any]]) -> str:
    """Строгий промпт: только факты из FEN/линии, без просторов для фантазии."""
    board = _board(fen)
    sans = [step["san"] for step in steps]
    opening, eco = classify_opening(sans)

    facts = [
        f"FEN начальной позиции: {board.fen()}",
        f"Ход в начальной позиции: {'белые' if board.turn == chess.WHITE else 'чёрные'}",
        f"Глубина линии: {len(steps)} полуход(ов)",
    ]
    if opening:
        facts.append(f"Дебют по книге ECO: {opening}" + (f" ({eco})" if eco else ""))
    else:
        facts.append("Дебют по книге ECO: не определён")

    move_facts: list[str] = []
    for index, step in enumerate(steps, start=1):
        stats = ""
        if step.get("games") is not None:
            stats = (
                f", партий всего {step['games']}: "
                f"белые {step.get('white')}, ничьи {step.get('draws')}, "
                f"чёрные {step.get('black')}"
            )
        move_facts.append(f"{index}. {step['san']} ({step.get('uci') or '?'}{stats})")
    facts.append("Главная линия (от заданной позиции):")
    facts.extend(move_facts)
    facts.append(
        "Напиши 3–5 предложений по-русски об идее этой линии и планах сторон. "
        "Не добавляй ходы, варианты и оценки, которых нет в фактах."
    )
    return "\n".join(facts)


def _ideas_impl(
    fen: str,
    line: list[dict[str, Any]] | list[str],
    db_path: Any = None,
) -> tuple[str | None, bool]:
    """(текст идей | None, взят ли из кеша). Общая реализация для API."""
    if not settings.theory_ideas_enabled:
        return None, False

    board = _board(fen)
    fen = board.fen()

    # API принимает line как список объектов; допускаем и строки SAN.
    prepared: list[Any] = []
    for item in line or []:
        if isinstance(item, str):
            prepared.append({"san": item})
        elif isinstance(item, dict):
            prepared.append(item)
    steps = _normalize_line(prepared)
    if not steps:
        return None, False

    key = ideas_cache_key(fen, steps)
    cached = _cache_get(key, db_path)
    if isinstance(cached, dict):
        text = cached.get("ideas")
        if isinstance(text, str) and text.strip():
            return text, True
    elif isinstance(cached, str) and cached.strip():
        return cached, True

    try:
        client = LLMClient()
        answer = client.chat(
            [
                ChatMessage("system", _IDEAS_SYSTEM),
                ChatMessage("user", _ideas_prompt(fen, steps)),
            ],
            temperature=0.2,
            max_tokens=_IDEAS_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001 - LLM опционален
        logger.debug("theory: LLM недоступен: %s", exc)
        return None, False

    text = clean_llm_text(answer) if isinstance(answer, str) else ""
    if not text or looks_like_refusal(text) or looks_like_thinking(text):
        return None, False

    _cache_set(key, {"ideas": text}, db_path)
    return text, False


def ideas(
    fen: str,
    line: list[dict[str, Any]] | list[str],
    db_path: Any = None,
) -> str | None:
    """Короткое объяснение идеи линии (3–5 предложений) или None.

    None — если идеи выключены настройкой, линия пустая, LLM недоступен или
    ответ похож на отказ/служебный reasoning. Исключения наружу не уходят.
    """
    try:
        return _ideas_impl(fen, line, db_path)[0]
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("theory: идеи не сгенерированы: %s", exc)
        return None


def ideas_with_cache(
    fen: str,
    line: list[dict[str, Any]] | list[str],
    db_path: Any = None,
) -> tuple[str | None, bool]:
    """Как :func:`ideas`, но возвращает ещё и признак попадания в кеш."""
    try:
        return _ideas_impl(fen, line, db_path)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("theory: идеи не сгенерированы: %s", exc)
        return None, False


__all__ = [
    "EXPLORER_URL",
    "EcoProvider",
    "LichessExplorerProvider",
    "OffProvider",
    "TheoryProvider",
    "TheoryUnavailable",
    "get_provider",
    "ideas",
    "ideas_cache_key",
    "ideas_with_cache",
    "main_line",
]
