"""Настройки приложения. Все значения переопределяются переменными окружения."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    dotenv_path = PROJECT_ROOT / ".env"
    if not dotenv_path.exists():
        return
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _cfg(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _csv_list(name: str, default: str = "") -> tuple[str, ...]:
    """Список из env через запятую (пустые элементы отбрасываются)."""
    return tuple(
        part.strip() for part in _cfg(name, default).split(",") if part.strip()
    )


def _is_heroku() -> bool:
    """True, если процесс запущен на Heroku (есть DYNO в окружении, напр. web.1)."""
    return bool(os.environ.get("DYNO"))


def _clamp_stockfish(
    *,
    heroku: bool,
    hash_mb: int,
    threads: int,
    time_ms: int,
    depth: int,
    multipv: int,
) -> tuple[int, int, int, int, int]:
    """Зажимает профиль Stockfish под 512MB на Heroku; локально — без изменений.

    На Heroku (eco, 512MB RAM) большие значения роняют процесс R14, поэтому
    потолки: ``hash_mb<=32``, ``threads<=1``, ``time_ms<=800``, ``depth<=12``,
    ``multipv<=1``. Семантика — ``min``: маленькие входные значения НЕ растут.
    Локально (``heroku=False``) аргументы возвращаются как есть — полная сила.

    Чистая функция: не читает окружение, тестируется напрямую с
    ``heroku=True/False``.
    """
    if not heroku:
        return hash_mb, threads, time_ms, depth, multipv
    return (
        min(hash_mb, 32),
        min(threads, 1),
        min(time_ms, 800),
        min(depth, 12),
        min(multipv, 1),
    )


_SF_HASH_MB, _SF_THREADS, _SF_TIME_MS, _SF_DEPTH, _SF_MULTIPV = _clamp_stockfish(
    heroku=_is_heroku(),
    hash_mb=int(_cfg("STOCKFISH_HASH_MB", "512")),
    threads=int(_cfg("STOCKFISH_THREADS", "4")),
    time_ms=int(_cfg("STOCKFISH_TIME_MS", "3000")),
    depth=int(_cfg("STOCKFISH_DEPTH", "14")),
    multipv=int(_cfg("STOCKFISH_MULTIPV", "3")),
)


def _resolve_stockfish_path() -> str:
    """Путь к бинарю Stockfish: env STOCKFISH_PATH → системный PATH → как есть.

    Явный ``STOCKFISH_PATH`` на существующий файл побеждает всегда. Если env
    пуст или файла по нему нет — пробуем ``shutil.which("stockfish")``
    (Linux/Heroku: ``/usr/games/stockfish`` из Aptfile; находится без
    хардкода пути). Иначе возвращаем значение env как есть (пусть даже
    пустое): :class:`app.analyzer.Engine` сам кинет понятную ошибку про
    STOCKFISH_PATH при попытке анализа.
    """
    configured = _cfg("STOCKFISH_PATH", "")
    if configured.strip() and Path(configured).is_file():
        return configured
    found = shutil.which("stockfish")
    if found:
        return found
    return configured


@dataclass(frozen=True)
class Settings:
    # --- аналитика ---
    stockfish_path: str = _resolve_stockfish_path()
    # На Heroku значения уже урезаны _clamp_stockfish (см. выше); локально —
    # это конфиг как есть (полная сила).
    stockfish_depth: int = _SF_DEPTH
    stockfish_multipv: int = _SF_MULTIPV
    stockfish_threads: int = _SF_THREADS
    stockfish_hash_mb: int = _SF_HASH_MB
    stockfish_time_ms: int = _SF_TIME_MS

    # --- lichess ---
    lichess_base_url: str = _cfg("LICHESS_BASE_URL", "https://lichess.org")
    lichess_token: str = _cfg("LICHESS_TOKEN", "")
    games_max: int = int(_cfg("GAMES_MAX", "50"))
    # Для чужих партий берём меньше: анализ каждой новой партии Stockfish
    # занимает минуты, а 30 партий уже хватает, чтобы понять репертуар.
    games_opponent_max: int = int(_cfg("GAMES_OPPONENT_MAX", "30"))

    # --- FIDE (через публичный Lichess-прокси, без токена) ---
    fide_base_url: str = _cfg("FIDE_BASE_URL", "https://lichess.org/api/fide")
    fide_id: str = _cfg("FIDE_ID", "")
    fide_timeout: int = int(_cfg("FIDE_TIMEOUT", "30"))

    # --- данные ---
    data_dir: Path = PROJECT_ROOT / "data"
    db_path: Path = PROJECT_ROOT / "data" / "trainer.db"

    # --- календарь и админка демо ---
    # Все «даты дня» (задача дня, трекер, план, прогресс) считаются в этом
    # поясе, а не в поясе сервера (Heroku живёт в UTC).
    app_tz: str = _cfg("APP_TZ", "Asia/Almaty")
    # Ники, которые видят админку, даже если role в БД не выставлена.
    admin_nicks: tuple[str, ...] = _csv_list("ADMIN_NICKS", "")

    # --- Maia-2 (тренер дебютов) ---
    maia_onnx_path: Path = Path(
        _cfg("MAIA_ONNX_PATH", str(PROJECT_ROOT / "data" / "maia" / "maia2-rapid.onnx"))
    )
    maia_dir: Path = Path(_cfg("MAIA_DIR", str(PROJECT_ROOT / "data" / "maia")))
    maia_model_url: str = _cfg(
        "MAIA_MODEL_URL",
        "https://media.githubusercontent.com/media/ChessWaveIO/maia2-uci/main/"
        "models/maia2-rapid.onnx",
    )
    maia_default_elo: int = int(_cfg("MAIA_DEFAULT_ELO", "1500"))
    opening_plies: int = int(_cfg("OPENING_PLIES", "40"))

    # --- гибридный соперник Maia + фильтр Stockfish ---
    # Порог потери в cp: ходы хуже лучшей оценки больше чем на threshold
    # отбрасываются. min_candidates — сколько ходов Maia минимум оставить,
    # чтобы соперник не играл всегда один и тот же лучший ход движка.
    hybrid_threshold_cp: int = int(_cfg("HYBRID_THRESHOLD_CP", "80"))
    hybrid_depth: int = int(_cfg("HYBRID_DEPTH", "12"))
    hybrid_time_ms: int = int(_cfg("HYBRID_TIME_MS", "800"))
    hybrid_min_candidates: int = int(_cfg("HYBRID_MIN_CANDIDATES", "2"))

    # --- теория дебютов (explorer/ECO) ---
    # "lichess" | "eco" | "off": источник главной линии. Провайдер можно
    # заменить, не трогая API (см. app.theory.TheoryProvider).
    theory_provider: str = _cfg("THEORY_PROVIDER", "lichess")
    theory_max_plies: int = int(_cfg("THEORY_MAX_PLIES", "12"))
    theory_timeout: int = int(_cfg("THEORY_TIMEOUT", "8"))
    theory_cache_hours: int = int(_cfg("THEORY_CACHE_HOURS", "168"))
    theory_ideas_enabled: bool = _cfg("THEORY_IDEAS_ENABLED", "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    # --- LLM (M1) ---
    llm_api_key: str = _cfg("LLM_API_KEY", "")
    llm_base_url: str = _cfg("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    llm_model: str = _cfg("LLM_MODEL", "openai/gpt-4o-mini")
    llm_max_tokens: int = int(_cfg("LLM_MAX_TOKENS", "4000"))
    llm_reasoning_budget: int = int(_cfg("LLM_REASONING_BUDGET", "1024"))
    llm_timeout: int = int(_cfg("LLM_TIMEOUT", "120"))


settings = Settings()

__all__ = ["Settings", "settings"]