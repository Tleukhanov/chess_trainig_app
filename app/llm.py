"""Тонкий OpenAI-совместимый HTTP-клиент для чат-моделей.

Работает с OpenRouter, OpenAI и любым OpenAI-совместимым endpoint'ом,
включая локальный Ollama. Только стандартная библиотека (urllib).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import settings

USER_AGENT = "chess-trainer/0.1"
_VALID_ROLES = frozenset({"system", "user", "assistant"})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """Сообщение чата для LLM (системное, пользовательское или ответ модели)."""

    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in _VALID_ROLES:
            raise ValueError(
                f"Недопустимая роль сообщения: {self.role!r} "
                "(ожидаются system | user | assistant)"
            )
        if not self.content:
            raise ValueError("content не может быть пустой строкой")


def build_completion_url(base_url: str) -> str:
    """Нормализует base_url и добавляет '/chat/completions', если его ещё нет."""
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    return url


def _is_local_host(host: str | None) -> bool:
    if not host:
        return False
    return host.strip("[]").lower() in _LOCAL_HOSTS


class LLMClient:
    """Клиент чат-завершений над OpenAI-совместимым HTTP API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.api_key = settings.llm_api_key if api_key is None else api_key
        self.base_url = settings.llm_base_url if base_url is None else base_url
        self.model = settings.llm_model if model is None else model
        self.timeout = timeout
        if not self.model:
            raise ValueError("model не может быть пустым")
        self._completion_url = build_completion_url(self.base_url)

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = 500,
        json_mode: bool = False,
    ) -> str:
        if not messages:
            raise ValueError("messages не может быть пустым списком")
        for message in messages:
            if not isinstance(message, ChatMessage):
                raise ValueError("Все элементы messages должны быть ChatMessage")

        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "temperature": temperature,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        host = urllib.parse.urlparse(self._completion_url).hostname
        if not self.api_key and not _is_local_host(host):
            raise RuntimeError(
                "LLM_API_KEY не задан (переменная окружения или .env). "
                "Пример: LLM_API_KEY=sk-or-..."
            )

        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            self._completion_url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            snippet = exc.read().decode("utf-8", errors="replace")[:500]
            exc.close()
            raise RuntimeError(f"LLM: HTTP {exc.code} — {snippet}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"LLM: сетевая ошибка: {exc}") from exc

        try:
            data = json.loads(payload.decode("utf-8"))
        except ValueError as exc:
            raise RuntimeError("LLM: не удалось разобрать JSON-ответ") from exc

        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"LLM: {data['error']}")

        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            raise RuntimeError("LLM: пустой ответ (no choices)")
        content = choices[0].get("message", {}).get("content")
        if not isinstance(content, str):
            raise RuntimeError("LLM: пустой ответ (no choices)")
        return content