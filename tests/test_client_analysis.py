from __future__ import annotations

import unittest

import chess

from app.client_analysis import validate_and_enrich, ClientAnalysisError
from app.db import Database
from app.games import Game
from tests.test_api import mkgame_moves


def _move(san, win_before=50.0, win_after=50.0, best=None, best_line=None,
          client_lines=None, drop=None):
    if best is None:
        best = san
    return {
        "san": san,
        "before": {"cp": 20.0, "mate": None},
        "after": {"cp": 18.0, "mate": None},
        "win_before": win_before,
        "win_after": win_after,
        "drop": 0.0 if drop is None else drop,
        "classification": "good",
        "best_move_san": best,
        "best_eval": {"cp": 20.0, "mate": None},
        "best_win": win_before,
        "clock_used": None,
        "time_pressure": False,
        "best_line": best_line or [],
        "played_line": [],
        "client_lines": client_lines,
    }


class ClientAnalysisUnitTests(unittest.TestCase):
    def setUp(self):
        self.game = mkgame_moves("g1", ["e4", "e5", "Nf3", "Nc6"], user_color="white")

    def _payload(self, moves):
        return {"analysis": {"game_id": self.game.id, "user_color": "white",
                             "moves": moves},
                "engine_meta": {"depth": 12, "multipv": 2}}

    def test_valid_accepted_themes(self):
        moves = [
            _move("e4", client_lines=[{"pv": ["e4", "e5"], "eval": {"cp": 20.0, "mate": None},
                                      "win": 55.0, "multipv": 1}]),
            _move("e5"),
            _move("Nf3"),
            _move("Nc6"),
        ]
        # fix played sans for black moves? game moves already; _move uses provided san
        summary = validate_and_enrich(self.game, self._payload(moves))
        self.assertEqual(summary["analysis_source"], "client")
        self.assertEqual(summary["engine"], "stockfish-wasm")
        self.assertEqual(summary["depth"], 12)
        self.assertEqual(summary["multipv"], 2)
        for m in summary["moves"]:
            self.assertIn("themes", m)
            self.assertIsInstance(m["themes"], dict)

    def test_wrong_san_rejected(self):
        moves = [_move("d4")] + [_move(s) for s in ["e5", "Nf3", "Nc6"]]
        with self.assertRaises(ClientAnalysisError):
            validate_and_enrich(self.game, self._payload(moves))

    def test_illegal_best_move_rejected(self):
        moves = [_move("e4", best="Qh5")] + [_move(s) for s in ["e5", "Nf3", "Nc6"]]
        with self.assertRaises(ClientAnalysisError):
            validate_and_enrich(self.game, self._payload(moves))

    def test_bad_numbers_rejected(self):
        moves = [_move("e4", win_before=150.0)] + [_move(s) for s in ["e5", "Nf3", "Nc6"]]
        with self.assertRaises(ClientAnalysisError):
            validate_and_enrich(self.game, self._payload(moves))

    def test_aggregates_classification(self):
        moves = [
            _move("e4", win_before=80.0, win_after=50.0, drop=30.0),
            _move("e5", win_before=60.0, win_after=58.0),
            _move("Nf3", win_before=70.0, win_after=69.0),
            _move("Nc6", win_before=65.0, win_after=64.0),
        ]
        summary = validate_and_enrich(self.game, self._payload(moves))
        # user is white -> plies 0,2
        self.assertEqual(summary["blunders"], [0])
        self.assertAlmostEqual(summary["avg_win_loss"], 15.5, places=1)



if __name__ == "__main__":
    unittest.main()
