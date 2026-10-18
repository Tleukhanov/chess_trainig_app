"""Тесты ONNX-политики Maia-2 (app.maia_onnx).

Два уровня:

1. Всегда (модель не нужна): паритет кодирования доски с зафиксированными
   тензорами из fixtures_rapid.json, загрузка словаря/Elo-корзин,
   зеркалирование чёрных, маскирование нелегальных ходов, softmax и
   отображение в SAN.
2. Требует скачанной модели (``data/maia/maia2-rapid.onnx``): полный
   пайплайн против распределений фикстур (после нормировки к 1.0, т.к.
   фикстуры хранят ненормированные вероятности) и здравый смысл на
   начальной позиции. Там же печатается бенчмарк латентности и пикового
   RSS (информационно, без падения теста).
"""

from __future__ import annotations

import json
import statistics
import time
import unittest
from pathlib import Path

import chess
import numpy as np

from app.config import settings
from app.maia_onnx import (
    MaiaOnnxPolicy,
    apply_mask_and_softmax,
    encode_board,
    elo_bucket,
    load_vocabulary,
    map_to_category,
    mirror_uci,
    rank_moves,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures_maia"
FIXTURES_PATH = FIXTURES_DIR / "fixtures_rapid.json"
_MAIN_OPENINGS = frozenset({"e4", "d4", "Nf3", "c4"})


def _fixture_cases() -> list[dict]:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))["cases"]


def _model_ready() -> bool:
    return (
        Path(settings.maia_onnx_path).is_file()
        and (Path(settings.maia_dir) / "all_moves.txt").is_file()
        and (Path(settings.maia_dir) / "elo_dict.json").is_file()
    )


def _peak_rss_mb() -> float:
    """Пиковый RSS процесса в МиБ (resource на POSIX, psapi на Windows)."""
    try:
        import resource  # noqa: PLC0415

        # На Linux ru_maxrss в КиБ.
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:  # noqa: BLE001
        pass
    try:
        import ctypes  # noqa: PLC0415
        import ctypes.wintypes  # noqa: PLC0415

        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.wintypes.DWORD),
                ("PageFaultCount", ctypes.wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        get_info = kernel32.K32GetProcessMemoryInfo
        get_info.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.POINTER(_Counters),
            ctypes.wintypes.DWORD,
        ]
        get_info.restype = ctypes.wintypes.BOOL
        counters = _Counters()
        counters.cb = ctypes.sizeof(counters)
        handle = kernel32.GetCurrentProcess()
        if get_info(handle, ctypes.byref(counters), counters.cb):
            return counters.PeakWorkingSetSize / (1024.0 * 1024.0)
        return float("nan")
    except Exception:  # noqa: BLE001
        return float("nan")


# --------------------------------------------------------------------------- #
# Уровень 1: без модели
# --------------------------------------------------------------------------- #
class EncodeBoardParityTests(unittest.TestCase):
    def test_shape_and_dtype(self) -> None:
        tensor = encode_board(chess.Board())
        self.assertEqual(tensor.shape, (18, 8, 8))
        self.assertEqual(tensor.dtype, np.float32)

    def test_tensor_matches_all_fixtures(self) -> None:
        for case in _fixture_cases():
            with self.subTest(label=case["label"]):
                board = chess.Board(case["fen"])
                got = encode_board(board).reshape(-1)
                want = np.asarray(case["board_input"], dtype=np.float32)
                self.assertEqual(got.shape, want.shape)
                self.assertLessEqual(float(np.abs(got - want).max()), 1e-6)

    def test_side_to_move_channel(self) -> None:
        tensor = encode_board(chess.Board())
        self.assertTrue(np.all(tensor[12] == 1.0))

    def test_castling_and_ep_channels(self) -> None:
        # Белые оба права, чёрных нет; цель взятия на проходе e3.
        board = chess.Board("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1")
        tensor = encode_board(board)
        self.assertTrue(np.all(tensor[13] == 1.0))  # WK
        self.assertTrue(np.all(tensor[14] == 1.0))  # WQ
        self.assertTrue(np.all(tensor[15] == 1.0))  # BK
        self.assertTrue(np.all(tensor[16] == 1.0))  # BQ
        self.assertEqual(float(tensor[17].sum()), 1.0)


class MirroringTests(unittest.TestCase):
    def test_mirror_uci_examples(self) -> None:
        self.assertEqual(mirror_uci("e2e4"), "e7e5")
        self.assertEqual(mirror_uci("e8g8"), "e1g1")
        self.assertEqual(mirror_uci("a2a1q"), "a7a8q")
        self.assertEqual(mirror_uci("b4c3"), "b5c6")

    def test_mirror_uci_is_involution(self) -> None:
        for uci in ("e2e4", "e8g8", "a2a1q", "g1f3", "e1c1"):
            self.assertEqual(mirror_uci(mirror_uci(uci)), uci)

    def test_black_board_equals_mirrored_white_board(self) -> None:
        black = chess.Board("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1")
        white_mirror = black.mirror()
        self.assertEqual(white_mirror.turn, chess.WHITE)
        self.assertTrue(np.array_equal(encode_board(black), encode_board(white_mirror)))

    def test_black_to_move_fixture_encoding(self) -> None:
        for case in _fixture_cases():
            board = chess.Board(case["fen"])
            if board.turn != chess.BLACK:
                continue
            with self.subTest(label=case["label"]):
                got = encode_board(board).reshape(-1)
                want = np.asarray(case["board_input"], dtype=np.float32)
                self.assertLessEqual(float(np.abs(got - want).max()), 1e-6)


class VocabularyTests(unittest.TestCase):
    def test_load_from_fixtures(self) -> None:
        moves, elo_dict = load_vocabulary(FIXTURES_DIR)
        self.assertEqual(len(moves), 1880)
        self.assertEqual(len(elo_dict), 11)
        self.assertEqual(len(set(moves)), 1880)

    def test_roundtrip_sample_moves(self) -> None:
        moves, _ = load_vocabulary(FIXTURES_DIR)
        index = {move: i for i, move in enumerate(moves)}
        for sample in ("g1f3", "e2e4", "e1g1", "e1c1", "a7a8q", "b7a8n"):
            self.assertIn(sample, index)
            self.assertEqual(moves[index[sample]], sample)

    def test_missing_dir_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            load_vocabulary(FIXTURES_DIR / "does-not-exist")

    def test_elo_bucket_boundaries(self) -> None:
        cases = [
            (800, 0), (1099, 0), (1100, 1), (1199, 1), (1200, 2),
            (1599, 5), (1600, 6), (1999, 9), (2000, 10), (3000, 10),
        ]
        for elo, want in cases:
            self.assertEqual(elo_bucket(elo), want, elo)

    def test_map_to_category_uses_dict(self) -> None:
        _, elo_dict = load_vocabulary(FIXTURES_DIR)
        for elo in (900, 1100, 1500, 1599, 1600, 1999, 2000, 2500):
            self.assertEqual(map_to_category(elo, elo_dict), elo_bucket(elo), elo)


class MaskingSoftmaxTests(unittest.TestCase):
    def test_illegal_masked_and_normalized(self) -> None:
        logits = np.array([2.0, 5.0, -1.0, 0.5, 3.0], dtype=np.float64)
        probs = apply_mask_and_softmax(logits, [0, 2, 4])
        self.assertEqual(probs[1], 0.0)
        self.assertEqual(probs[3], 0.0)
        self.assertAlmostEqual(float(probs.sum()), 1.0, places=9)
        self.assertGreater(probs[4], probs[0])
        self.assertGreater(probs[0], probs[2])

    def test_temperature_flattens(self) -> None:
        logits = np.array([0.0, 4.0], dtype=np.float64)
        sharp = apply_mask_and_softmax(logits, [0, 1], temperature=1.0)
        flat = apply_mask_and_softmax(logits, [0, 1], temperature=4.0)
        self.assertLess(flat[1], sharp[1])

    def test_no_legal_moves_returns_zeros(self) -> None:
        probs = apply_mask_and_softmax(np.array([1.0, 2.0]), [])
        self.assertTrue(np.all(probs == 0.0))

    def test_rank_moves_returns_san_topk_normalized(self) -> None:
        board = chess.Board()
        legal_moves = list(board.legal_moves)
        legal = [(i, move) for i, move in enumerate(legal_moves)]
        probs = np.full(len(legal), 0.01)
        for i, move in legal:
            if move.uci() == "e2e4":
                probs[i] = 5.0
        ranked = rank_moves(board, probs, legal, k=3)
        self.assertEqual(len(ranked), 3)
        self.assertEqual(ranked[0][0], "e4")
        self.assertAlmostEqual(sum(p for _, p in ranked), 1.0, places=9)
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_rank_moves_black_san_coordinates(self) -> None:
        board = chess.Board("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1")
        legal_moves = list(board.legal_moves)
        legal = [(i, move) for i, move in enumerate(legal_moves)]
        probs = np.ones(len(legal))
        for i, move in legal:
            if move.uci() == "g8f6":
                probs[i] = 100.0
        ranked = rank_moves(board, probs, legal, k=1)
        self.assertEqual(ranked[0][0], "Nf6")


class PolicyConstructionTests(unittest.TestCase):
    def test_missing_model_raises_with_hint(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            MaiaOnnxPolicy(FIXTURES_DIR / "absent.onnx", FIXTURES_DIR, 1500, 1500)
        self.assertIn("fetch_maia_onnx", str(ctx.exception))


# --------------------------------------------------------------------------- #
# Уровень 2: требует модель
# --------------------------------------------------------------------------- #
@unittest.skipUnless(_model_ready(), "модель Maia не скачана (см. tools/fetch_maia_onnx.py)")
class MaiaModelParityTests(unittest.TestCase):
    def test_full_pipeline_matches_fixture_distributions(self) -> None:
        for case in _fixture_cases():
            with self.subTest(label=case["label"]):
                policy = MaiaOnnxPolicy(
                    settings.maia_onnx_path,
                    settings.maia_dir,
                    case["elo_self"],
                    case["elo_oppo"],
                )
                board = chess.Board(case["fen"])
                ranked = policy.probs(board, k=len(case["legal_uci_board_space"]) + 3)
                got = dict(ranked)

                expected: dict[str, float] = {}
                for uci, prob in case["move_probs"].items():
                    move = chess.Move.from_uci(uci)
                    if not board.is_legal(move):
                        continue
                    san = board.san(move)
                    if san in got:
                        expected[san] = prob
                total = sum(expected.values())
                self.assertGreater(total, 0.5)
                for san, prob in expected.items():
                    self.assertAlmostEqual(got[san], prob / total, delta=1e-3, msg=san)

                best_san = board.san(chess.Move.from_uci(case["best_move"]))
                self.assertEqual(ranked[0][0], best_san)

    def test_startpos_sanity(self) -> None:
        policy = MaiaOnnxPolicy(
            settings.maia_onnx_path, settings.maia_dir, 1500, 1500
        )
        ranked = policy.probs(chess.Board(), k=8)
        self.assertAlmostEqual(sum(p for _, p in ranked), 1.0, places=6)
        self.assertIn(ranked[0][0], _MAIN_OPENINGS)

    def test_best_matches_probs_top(self) -> None:
        policy = MaiaOnnxPolicy(
            settings.maia_onnx_path, settings.maia_dir, 1500, 1500
        )
        board = chess.Board()
        self.assertEqual(policy.best(board), policy.probs(board, k=1)[0][0])


@unittest.skipUnless(_model_ready(), "модель Maia не скачана (см. tools/fetch_maia_onnx.py)")
class MaiaBenchmarkTests(unittest.TestCase):
    def test_latency_and_peak_rss_report(self) -> None:
        policy = MaiaOnnxPolicy(
            settings.maia_onnx_path, settings.maia_dir, 1500, 1500, threads=1
        )
        fens = [case["fen"] for case in _fixture_cases()]
        boards = [chess.Board(fen) for fen in fens]
        boards = [b for b in boards if b.legal_moves][:10]
        policy.probs(boards[0], k=8)  # прогрев (включая загрузку сессии)

        latencies: list[float] = []
        for board in boards:
            start = time.perf_counter()
            policy.probs(board, k=8)
            latencies.append((time.perf_counter() - start) * 1000.0)

        median = statistics.median(latencies)
        peak = _peak_rss_mb()
        print(
            f"\n[Maia benchmark] ходов={len(latencies)} "
            f"латентность median={median:.1f} ms, max={max(latencies):.1f} ms; "
            f"пиковый RSS={peak:.1f} MiB"
        )
        self.assertGreaterEqual(len(latencies), 1)
        self.assertGreater(median, 0.0)


if __name__ == "__main__":
    unittest.main()
