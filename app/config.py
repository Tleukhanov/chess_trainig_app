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
    return os.environ.get(name, default)


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
    stockfish_depth: int = int(_cfg("STOCKFISH_DEPTH", "14"))
    stockfish_multipv: int = int(_cfg("STOCKFISH_MULTIPV", "3"))
    stockfish_threads: int = int(_cfg("STOCKFISH_THREADS", "4"))
    stockfish_hash_mb: int = int(_cfg("STOCKFISH_HASH_MB", "512"))
    stockfish_time_ms: int = int(_cfg("STOCKFISH_TIME_MS", "3000"))

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

    # --- LLM (M1) ---
    llm_api_key: str = _cfg("LLM_API_KEY", "")
    llm_base_url: str = _cfg("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    llm_model: str = _cfg("LLM_MODEL", "openai/gpt-4o-mini")
    llm_max_tokens: int = int(_cfg("LLM_MAX_TOKENS", "4000"))
    llm_reasoning_budget: int = int(_cfg("LLM_REASONING_BUDGET", "1024"))


settings = Settings()

__all__ = ["Settings", "settings"]