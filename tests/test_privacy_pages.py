"""Проверки фронтенда и privacy-страницы без запуска браузера.

В проекте нет JS-тестов: этот модуль фиксирует наличие обязательных
строк/ссылок в статике (privacy/ToS, ссылка в футере, отсутствие
скрытого трекинга). Ручная приемка описана в README.md
(«Ручная приемка фронтенда»).
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"


def _read(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


class PrivacyPageTests(unittest.TestCase):
    def test_privacy_page_exists_and_covers_required_topics(self) -> None:
        html = _read("privacy.html")
        for needle in (
            "tleukhanov0409@mail.ru",  # контакт поддержки/приватности
            "PBKDF2",  # как хранится пароль
            "LLM_API_KEY",  # как отключить LLM (env-level)
            "OpenRouter",  # кто LLM-обработчик по умолчанию
            "FEN",  # что уходит LLM
            "скрыт",  # скрытой аналитики нет
            "Удалить аккаунт",  # путь удаления
            "не юридическая консультация",  # без претензии на юрсовет
            "проприетарный",  # права не раздаются
        ):
            self.assertIn(needle, html, needle)

    def test_privacy_page_grants_no_open_source_license(self) -> None:
        html = _read("privacy.html").lower()
        for forbidden in ("mit license", "apache license", "gpl", "creative commons"):
            self.assertNotIn(forbidden, html)

    def test_index_footer_links_privacy_page(self) -> None:
        html = _read("index.html")
        self.assertIn("/privacy.html", html)

    def test_app_js_has_no_hidden_ping(self) -> None:
        js = _read("app.js")
        self.assertNotIn("track/ping", js)
        self.assertNotIn("startTrackPing", js)
        self.assertNotIn("trackStarted", js)
        # Минимальная длина пароля отражена в UI-копиях.
        self.assertIn("мин. 8", js)
        # Удаление аккаунта видно в UI и требует подтверждения.
        self.assertIn("delete-account-btn", js)
        self.assertIn("Удалить аккаунт и данные", js)
        # Ссылка на privacy-страницу из аккаунта и входа.
        self.assertIn("/privacy.html", js)
        self.assertIn("tleukhanov0409@mail.ru", js)

    def test_registration_form_mentions_eight_chars_and_privacy(self) -> None:
        js = _read("app.js")
        self.assertIn("мин. 8 символов", js)
        self.assertIn('minlength="8"', js)

    def test_password_inputs_keep_autocomplete(self) -> None:
        js = _read("app.js")
        self.assertIn('autocomplete="new-password"', js)
        self.assertIn('autocomplete="current-password"', js)


if __name__ == "__main__":
    unittest.main()
