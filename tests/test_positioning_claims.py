"""Positioning / honest-claims checks (audit phase 3, subagent H).

Строковые проверки статики без запуска браузера: README, onboarding-копия
(frontend/app.js), frontend/index.html и PILOT_CHECKLIST.md должны говорить
одно и то же и не содержать причинных обещаний или выдуманных доказательств.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

SUPPORT_CONTACT = "tleukhanov0409@mail.ru"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class ReadmeClaimsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.readme = _read(ROOT / "README.md")

    def test_readme_positions_1600_2400_as_pilot_hypothesis(self) -> None:
        self.assertIn("1600–2400", self.readme)
        self.assertIn("гипотез", self.readme.lower())
        # Честная метка рядом с диапазоном, а не отдельно где-то в документе.
        hypothesis_ctx = [
            m.start() for m in re.finditer(r"1600–2400", self.readme)
        ]
        for pos in hypothesis_ctx:
            window = self.readme[max(0, pos - 400): pos + 400].lower()
            self.assertIn("гипотез", window)

    def test_readme_has_no_rating_gain_guarantee(self) -> None:
        lowered = self.readme.lower()
        for forbidden in (
            "гарантированный рост",
            "гарантия роста рейтинга",
            "рейтинг вырастет",
            "рост рейтинга гарантирован",
            "поднимет рейтинг",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_readme_states_no_substitute_and_no_proof(self) -> None:
        lowered = self.readme.lower()
        self.assertIn("не заменяет", lowered)  # тренера / Chess.com / Lichess
        self.assertIn("не гарантирует", lowered)
        self.assertIn("llm", lowered)

    def test_readme_license_status_private_commercial(self) -> None:
        lowered = self.readme.lower()
        self.assertIn("лицензи", lowered)
        self.assertIn("закрытой/коммерческой", lowered)
        self.assertIn("license", lowered)  # файла LICENSE нет — и это сказано

    def test_readme_links_pilot_checklist_and_contact(self) -> None:
        self.assertIn("PILOT_CHECKLIST.md", self.readme)
        self.assertIn(SUPPORT_CONTACT, self.readme)
        self.assertIn("/privacy.html", self.readme)

    def test_readme_does_not_claim_public_self_serve(self) -> None:
        lowered = self.readme.lower()
        self.assertIn("self-serve", lowered)
        self.assertIn("закрытая", lowered)


class PilotChecklistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checklist = _read(ROOT / "PILOT_CHECKLIST.md")

    def test_checklist_covers_pilot_scope_and_consent(self) -> None:
        text = self.checklist.lower()
        self.assertIn("10–20", self.checklist)
        for needle in (
            "baseline",
            "20",  # последние 20 анализов как рабочее окно
            "llm",
            "pgn",
            "lichess",
            "privacy",
            "day 7",
            "day 14",
            "insufficient_data",
            "rollback",
            "go/no-go",
            SUPPORT_CONTACT,
        ):
            self.assertIn(needle.lower(), text, needle)

    def test_checklist_denies_causal_claim(self) -> None:
        lowered = self.checklist.lower()
        self.assertIn("причин", lowered)
        self.assertIn("не доказательство", lowered)
        self.assertIn("описательные", lowered)

    def test_checklist_go_no_go_covers_required_gates(self) -> None:
        lowered = self.checklist.lower()
        for needle in (
            "phase 0–2",
            "privacy smoke-test",
            "deletion",
            "fallback",
            "no tracking",
            "support contact",
            "rollback",
        ):
            self.assertIn(needle.lower(), lowered, needle)


class FrontendCopyTests(unittest.TestCase):
    def test_index_html_has_honest_description_and_footer(self) -> None:
        html = _read(FRONTEND / "index.html")
        self.assertIn('name="description"', html)
        lowered = html.lower()
        self.assertIn("без гарантии роста рейтинга", lowered)
        self.assertIn("закрытая бета", lowered)
        self.assertIn("/privacy.html", html)

    def test_onboarding_copy_matches_readme_positioning(self) -> None:
        js = _read(FRONTEND / "app.js")
        self.assertIn("без гарантии роста рейтинга", js.lower())
        self.assertIn("не заменяет тренера", js.lower())
        # Верхняя копия на онбординге не обещает рейтинг.
        for forbidden in (
            "рейтинг вырастет",
            "гарантированный рост",
            "гарантия роста",
        ):
            self.assertNotIn(forbidden, js.lower())

    def test_privacy_notice_linked_and_consistent(self) -> None:
        html = _read(FRONTEND / "privacy.html")
        self.assertIn(SUPPORT_CONTACT, html)
        # Privacy notice продолжает использовать тот же контакт, что README/чеклист.
        readme = _read(ROOT / "README.md")
        self.assertIn(SUPPORT_CONTACT, readme)


if __name__ == "__main__":
    unittest.main()
