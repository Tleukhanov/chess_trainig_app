"""Проверки ассетов локального движка в браузере (Agent A).

Синтетические/статические проверки: файлы vendor и контрактные строки
JS-клиента. Сетевые вызовы и реальные партии не используются.
"""

from __future__ import annotations

from pathlib import Path

import unittest

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
ENGINE = FRONTEND / "engine"
VENDOR_STOCKFISH = FRONTEND / "vendor" / "stockfish"
VENDOR_CHESS = FRONTEND / "vendor" / "chess"


class VendorStockfishTests(unittest.TestCase):
    def test_required_files_exist_and_non_empty(self) -> None:
        for name in ("stockfish.js", "stockfish.wasm", "COPYING.txt", "package.json"):
            path = VENDOR_STOCKFISH / name
            self.assertTrue(path.is_file(), name)
            self.assertGreater(path.stat().st_size, 0, name)

    def test_license_files_present(self) -> None:
        copying = (VENDOR_STOCKFISH / "COPYING.txt").read_text(encoding="utf-8", errors="replace")
        self.assertIn("GNU GENERAL PUBLIC LICENSE", copying)
        package = (VENDOR_STOCKFISH / "package.json").read_text(encoding="utf-8")
        self.assertIn("GPL-3.0", package)

    def test_stockfish_js_is_worker_script(self) -> None:
        js = (VENDOR_STOCKFISH / "stockfish.js").read_text(encoding="utf-8", errors="replace")
        self.assertIn("onmessage", js)
        self.assertIn("postMessage", js)


class VendorChessTests(unittest.TestCase):
    def test_required_files_exist_and_non_empty(self) -> None:
        for name in ("chess.js", "LICENSE"):
            path = VENDOR_CHESS / name
            self.assertTrue(path.is_file(), name)
            self.assertGreater(path.stat().st_size, 0, name)

    def test_chess_js_is_es_module(self) -> None:
        js = (VENDOR_CHESS / "chess.js").read_text(encoding="utf-8", errors="replace")
        self.assertIn("export", js)


class StockfishClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client_path = ENGINE / "stockfish-client.js"
        self.text = self.client_path.read_text(encoding="utf-8")

    def test_file_exists_and_non_empty(self) -> None:
        self.assertTrue(self.client_path.is_file())
        self.assertGreater(self.client_path.stat().st_size, 0)

    def test_exports_contract(self) -> None:
        for needle in ("export function createStockfishClient",
                      "export function analyzePosition",
                      "export function stopClient"):
            self.assertIn(needle, self.text, needle)

    def test_worker_and_vendor_paths(self) -> None:
        self.assertIn("vendor/stockfish/stockfish.js", self.text)
        self.assertIn("vendor/chess/chess.js", self.text)
        self.assertIn("new Worker", self.text)

    def test_uci_sequence_contract(self) -> None:
        for needle in ("'uci'", "'isready'", "'readyok'", "position fen", "'go depth", "bestmove"):
            self.assertIn(needle, self.text, needle)

    def test_multipv_and_win_percent_contract(self) -> None:
        for needle in ("MultiPV", "multiPv", "multipv", "winPercent"):
            self.assertIn(needle, self.text, needle)

    def test_error_and_timeout_handling(self) -> None:
        for needle in ("timeout", "Invalid FEN", "destroyed", "terminate"):
            self.assertIn(needle, self.text, needle)

    def test_readme_exists(self) -> None:
        readme = ENGINE / "README.md"
        self.assertTrue(readme.is_file())
        self.assertGreater(readme.stat().st_size, 0)
        content = readme.read_text(encoding="utf-8")
        for needle in ("createStockfishClient", "analyzePosition", "stopClient"):
            self.assertIn(needle, content, needle)


if __name__ == "__main__":
    unittest.main()
