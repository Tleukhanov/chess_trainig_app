"""Maia-2 ONNX-политика «человекоподобного» соперника (тренер дебютов).

Реализует протокол :class:`app.maia.HumanPolicy`: из позиции возвращает
(SAN, вероятность) для стороны, чей ход. В отличие от ``MaiaLitePolicy``
(мягкий Stockfish) здесь работает настоящая нейросеть Maia-2, исполняемая
через ONNX Runtime на CPU.

Модель всегда «думает» за белых: при ходе чёрных доска зеркалится
(вертикальный флип рангов + смена цвета), как в референсной реализации
(``maia2.inference.preprocessing``), а выбранные ходы отображаются обратно.
Кодирование доски — 18 плоскостей ``[18, 8, 8]``: 12 фигурных (P,N,B,R,Q,K
× белые/чёрные), ход, 4 рокировки и цель взятия на проходе.

Память: в 512MB-контейнере (Heroku eco) держать ONNX-сессию постоянно
накладно, поэтому модуль даёт ленивый singleton-фабрику :func:`get_policy`
и :func:`unload`. Перед запуском Stockfish (который сам просит сотни МБ)
вызовите ``unload()`` — сессия будет освобождена, а при следующем ходе
пересоздана лениво.

Загрузка модели: если файла модели (или словаря) нет в момент вызова
:func:`get_policy`, то при включённом ``MAIA_AUTO_FETCH`` (по умолчанию —
включён) артефакты автоматически скачиваются через
``tools/fetch_maia_onnx.py`` до загрузки. При выключенном флаге поведение
прежнее — понятный ``RuntimeError``.
"""

from __future__ import annotations

import gc
import json
import os
import threading
from pathlib import Path
from typing import Any

import chess
import numpy as np
import onnxruntime as ort

__all__ = [
    "MaiaOnnxPolicy",
    "apply_mask_and_softmax",
    "autofetch_enabled",
    "encode_board",
    "elo_bucket",
    "load_vocabulary",
    "map_to_category",
    "mirror_uci",
    "rank_moves",
    "get_policy",
    "unload",
]

CHANNELS = 18
BOARD_SHAPE = (CHANNELS, 8, 8)

_ELO_START = 1100
_ELO_END = 2000
_ELO_INTERVAL = 100

_PIECE_CHANNEL = {
    chess.PAWN: 0,
    chess.KNIGHT: 1,
    chess.BISHOP: 2,
    chess.ROOK: 3,
    chess.QUEEN: 4,
    chess.KING: 5,
}

_PROVIDERS = ["CPUExecutionProvider"]


# --------------------------------------------------------------------------- #
# Кодирование доски / ходов / Elo
# --------------------------------------------------------------------------- #
def mirror_uci(uci: str) -> str:
    """Зеркалит UCI-ход: меняет ранг обеих клеток (файл не трогает).

    Промо-суффикс сохраняется. ``e2e4 -> e7e5``, ``e8g8 -> e1g1``.
    """
    if len(uci) < 4:
        return uci
    flip = lambda r: str(9 - int(r))  # noqa: E731
    return uci[0] + flip(uci[1]) + uci[2] + flip(uci[3]) + uci[4:]


def _encode_white_to_move(board: chess.Board) -> np.ndarray:
    """Кодирует позицию, где ход белых, в тензор ``[18, 8, 8]``.

    Точный порт ``maia2.utils.board_to_tensor``: порядок фигур
    P,N,B,R,Q,K; белые — каналы 0..5, чёрные — 6..11; канал 12 — ход
    (всегда 1.0, т.к. доска уже приведена к ходу белых); каналы 13..16 —
    рокировки [WK, WQ, BK, BQ]; канал 17 — клетка взятия на проходе.
    """
    tensor = np.zeros(BOARD_SHAPE, dtype=np.float32)
    for square, piece in board.piece_map().items():
        channel = _PIECE_CHANNEL[piece.piece_type]
        if piece.color == chess.BLACK:
            channel += 6
        row, col = divmod(square, 8)
        tensor[channel, row, col] = 1.0

    tensor[12, :, :] = 1.0

    if board.has_kingside_castling_rights(chess.WHITE):
        tensor[13, :, :] = 1.0
    if board.has_queenside_castling_rights(chess.WHITE):
        tensor[14, :, :] = 1.0
    if board.has_kingside_castling_rights(chess.BLACK):
        tensor[15, :, :] = 1.0
    if board.has_queenside_castling_rights(chess.BLACK):
        tensor[16, :, :] = 1.0

    if board.ep_square is not None:
        row, col = divmod(board.ep_square, 8)
        tensor[17, row, col] = 1.0

    return tensor


def encode_board(board: chess.Board) -> np.ndarray:
    """Тензор ``[18, 8, 8]`` для модели Maia-2.

    При ходе чёрных доска зеркалится (``board.mirror()``), чтобы модель
    видела «ход белых» — как в референсе. Исходная доска не изменяется.
    """
    if board.turn == chess.BLACK:
        board = board.mirror()
    return _encode_white_to_move(board)


def elo_bucket(elo: int) -> int:
    """Индекс Elo-корзины Maia-2: ``<1100 -> 0``, далее по 100, ``>=2000 -> 10``."""
    if elo < _ELO_START:
        return 0
    if elo >= _ELO_END:
        return 10
    return (elo - _ELO_START) // _ELO_INTERVAL + 1


def map_to_category(elo: int, elo_dict: dict[str, int]) -> int:
    """Отображает сырой Elo в индекс корзины через загруженный ``elo_dict``."""
    if elo < _ELO_START:
        return elo_dict[f"<{_ELO_START}"]
    if elo >= _ELO_END:
        return elo_dict[f">={_ELO_END}"]
    lower = (elo // _ELO_INTERVAL) * _ELO_INTERVAL
    return elo_dict[f"{lower}-{lower + _ELO_INTERVAL - 1}"]


def load_vocabulary(vocab_dir: str | Path) -> tuple[list[str], dict[str, int]]:
    """Читает ``all_moves.txt`` и ``elo_dict.json`` из каталога.

    Возвращает (список UCI-ходов словаря, таблица Elo-корзин). Бросает
    понятный ``RuntimeError``, если файлов нет.
    """
    vocab_dir = Path(vocab_dir)
    moves_path = vocab_dir / "all_moves.txt"
    elo_path = vocab_dir / "elo_dict.json"
    if not moves_path.is_file():
        raise RuntimeError(
            f"Словарь ходов Maia не найден: {moves_path}. "
            "Запусти tools/fetch_maia_onnx.py."
        )
    if not elo_path.is_file():
        raise RuntimeError(
            f"Elo-корзины Maia не найдены: {elo_path}. "
            "Запусти tools/fetch_maia_onnx.py."
        )
    moves = [
        line.strip()
        for line in moves_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    elo_dict = json.loads(elo_path.read_text(encoding="utf-8"))
    return moves, elo_dict


# --------------------------------------------------------------------------- #
# Маскирование / softmax / выдача ходов
# --------------------------------------------------------------------------- #
def apply_mask_and_softmax(
    logits: np.ndarray,
    legal_indices: list[int],
    temperature: float = 1.0,
) -> np.ndarray:
    """Полное распределение вероятностей по словарю ходов.

    Неместные (нелегальные) индексы получают 0.0, легальные — softmax от
    ``logits / temperature``. Возвращает массив той же длины, что ``logits``.
    """
    logits = np.asarray(logits, dtype=np.float64)
    probs = np.zeros_like(logits)
    if not legal_indices:
        return probs
    idx = np.asarray(sorted(set(int(i) for i in legal_indices)), dtype=np.int64)
    temp = temperature if temperature and temperature > 0 else 1.0
    legal_logits = logits[idx] / temp
    shifted = legal_logits - np.max(legal_logits)
    exps = np.exp(shifted)
    total = exps.sum()
    if total <= 0.0:
        return probs
    probs[idx] = exps / total
    return probs


def rank_moves(
    board: chess.Board,
    probs: np.ndarray,
    legal: list[tuple[int, chess.Move]],
    k: int,
) -> list[tuple[str, float]]:
    """Топ-``k`` ходов как (SAN, вероятность), по убыванию, сумма ≈ 1.0.

    SAN считается в системе координат ``board`` (для чёрных она совпадает
    с исходной доской). Вероятности ре-нормируются по удержанным ходам,
    чтобы выполнялось соглашение протокола HumanPolicy (сумма ≈ 1.0).
    """
    items: list[tuple[str, float]] = []
    for index, move in legal:
        prob = float(probs[index])
        if prob <= 0.0:
            continue
        try:
            san = board.san(move)
        except ValueError:
            continue
        items.append((san, prob))
    items.sort(key=lambda item: item[1], reverse=True)
    items = items[: max(1, int(k))]
    total = sum(prob for _, prob in items)
    if total > 0.0:
        items = [(san, prob / total) for san, prob in items]
    return items


# --------------------------------------------------------------------------- #
# Политика
# --------------------------------------------------------------------------- #
class MaiaOnnxPolicy:
    """Человекоподобная политика Maia-2 на ONNX Runtime (CPU).

    Сессия создаётся лениво при первом обращении к :meth:`probs`: сам
    конструктор лишь проверяет наличие файлов и грузит словарь. Файл
    модели отсутствует -> понятный ``RuntimeError`` (импорт модуля при
    этом не падает).
    """

    def __init__(
        self,
        model_path: str | Path,
        vocab_dir: str | Path,
        self_elo: int,
        opp_elo: int,
        temperature: float = 1.0,
        threads: int = 1,
    ) -> None:
        self.model_path = Path(model_path)
        self.vocab_dir = Path(vocab_dir)
        self.self_elo = int(self_elo)
        self.opp_elo = int(opp_elo)
        self.temperature = float(temperature)
        self.threads = max(1, int(threads))
        if not self.model_path.is_file():
            raise RuntimeError(
                f"Файл модели Maia не найден: {self.model_path}. "
                "Запусти tools/fetch_maia_onnx.py (или Heroku-релиз-шаг)."
            )
        self.moves, self.elo_dict = load_vocabulary(self.vocab_dir)
        self._move_index = {move: i for i, move in enumerate(self.moves)}
        self._self_bucket = map_to_category(self.self_elo, self.elo_dict)
        self._opp_bucket = map_to_category(self.opp_elo, self.elo_dict)
        self._session: ort.InferenceSession | None = None
        self._lock = threading.Lock()

    # -- сессия ----------------------------------------------------------- #
    def _ensure_session(self) -> ort.InferenceSession:
        session = self._session
        if session is not None:
            return session
        with self._lock:
            if self._session is None:
                options = ort.SessionOptions()
                options.intra_op_num_threads = self.threads
                options.inter_op_num_threads = 1
                options.graph_optimization_level = (
                    ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                )
                self._session = ort.InferenceSession(
                    str(self.model_path),
                    sess_options=options,
                    providers=list(_PROVIDERS),
                )
            return self._session

    @property
    def session(self) -> ort.InferenceSession:
        """Лениво созданная ONNX-сессия (только CPU)."""
        return self._ensure_session()

    def close(self) -> None:
        """Освобождает ONNX-сессию (RAM возвращается ОС)."""
        with self._lock:
            self._session = None
        gc.collect()

    # -- кодирование позиции --------------------------------------------- #
    def _legal_moves(self, board: chess.Board) -> list[tuple[int, chess.Move]]:
        """Легальные ходы как (индекс в словаре модели, Move в координатах board)."""
        mirrored = board.turn == chess.BLACK
        legal: list[tuple[int, chess.Move]] = []
        for move in board.legal_moves:
            uci = move.uci()
            model_uci = mirror_uci(uci) if mirrored else uci
            index = self._move_index.get(model_uci)
            if index is not None:
                legal.append((index, move))
        return legal

    def _logits(self, board: chess.Board) -> np.ndarray:
        session = self._ensure_session()
        boards = encode_board(board)[None, :, :, :].astype(np.float32)
        elo_self = np.asarray([self._self_bucket], dtype=np.int64)
        elo_oppo = np.asarray([self._opp_bucket], dtype=np.int64)
        outputs = session.run(
            ["logits_maia"],
            {"boards": boards, "elo_self": elo_self, "elo_oppo": elo_oppo},
        )
        return np.asarray(outputs[0][0], dtype=np.float64)

    # -- публичный API ---------------------------------------------------- #
    def probs(self, board: chess.Board, *, k: int = 8) -> list[tuple[str, float]]:
        """Топ-``k`` (SAN, вероятность) по модели для стороны ``board.turn``."""
        legal = self._legal_moves(board)
        if not legal:
            raise RuntimeError("В позиции нет легальных ходов словаря Maia")
        logits = self._logits(board)
        distribution = apply_mask_and_softmax(
            logits, [index for index, _ in legal], self.temperature
        )
        return rank_moves(board, distribution, legal, k)

    def best(self, board: chess.Board) -> str:
        """SAN наиболее вероятного хода (аргмакс распределения)."""
        ranked = self.probs(board, k=1)
        return ranked[0][0] if ranked else ""

    def __enter__(self) -> MaiaOnnxPolicy:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Ленивый singleton (память под контролем)
# --------------------------------------------------------------------------- #
_POLICY: MaiaOnnxPolicy | None = None
_POLICY_KEY: tuple[Any, ...] | None = None
_POLICY_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Авто-загрузка модели (Heroku eco: эфемерный диск -> модель качается заново)
# --------------------------------------------------------------------------- #
_FALSY_ENV = frozenset({"0", "false", "no", "off"})


def autofetch_enabled() -> bool:
    """Флаг ``MAIA_AUTO_FETCH``: включён по умолчанию.

    Не задан, пустой, ``1`` или ``true`` (любой регистр) — включено;
    ``0`` / ``false`` / ``no`` / ``off`` — выключено. Любое иное значение
    трактуется как включено (безопасный дефолт для Heroku).
    """
    raw = os.environ.get("MAIA_AUTO_FETCH")
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() not in _FALSY_ENV


def _fetch_maia_artifacts(vocab_dir: Path) -> None:
    """Скачивает модель и словари через ``tools/fetch_maia_onnx.py``.

    Импорт ленивый: держим ``import app.maia_onnx`` лёгким и без циклов.
    Отдельная функция — шов для тестов (её подменяют заглушкой, чтобы не
    качать ~93 MiB). Инструмент кладёт модель и словари в ``vocab_dir``.
    """
    from tools.fetch_maia_onnx import download_all

    download_all(maia_dir=vocab_dir)


def _ensure_artifacts(model_path: Path, vocab_dir: Path) -> None:
    """Гарантирует наличие модели и словаря перед созданием политики.

    Если файлов нет и :func:`autofetch_enabled` — вызывает загрузчик; при
    выключенном флаге молча выходит (конструктор политики сам бросит
    понятный ``RuntimeError``). Сбои загрузки оборачиваются в
    ``RuntimeError`` с указанием на ``tools/fetch_maia_onnx.py``.
    """
    model_ok = model_path.is_file()
    vocab_ok = (vocab_dir / "all_moves.txt").is_file() and (
        vocab_dir / "elo_dict.json"
    ).is_file()
    if model_ok and vocab_ok:
        return
    if not autofetch_enabled():
        return
    try:
        _fetch_maia_artifacts(vocab_dir)
    except Exception as error:  # noqa: BLE001
        raise RuntimeError(
            "Не удалось автоматически скачать модель Maia "
            f"(tools/fetch_maia_onnx.py): {error}"
        ) from error


def _defaults() -> tuple[Path, Path, int]:
    from .config import settings

    return Path(settings.maia_onnx_path), Path(settings.maia_dir), settings.maia_default_elo


def get_policy(
    model_path: str | Path | None = None,
    vocab_dir: str | Path | None = None,
    self_elo: int | None = None,
    opp_elo: int | None = None,
    temperature: float = 1.0,
    threads: int = 1,
) -> MaiaOnnxPolicy:
    """Возвращает общий :class:`MaiaOnnxPolicy`, пересоздавая его при смене параметров.

    Значения по умолчанию берутся из :mod:`app.config`. Сессия внутри
    политики создаётся лениво, поэтому вызов фабрики дешёв.
    """
    d_model, d_vocab, d_elo = _defaults()
    model_path = Path(model_path) if model_path is not None else d_model
    vocab_dir = Path(vocab_dir) if vocab_dir is not None else d_vocab
    self_elo = d_elo if self_elo is None else int(self_elo)
    opp_elo = self_elo if opp_elo is None else int(opp_elo)
    key = (str(model_path), str(vocab_dir), self_elo, opp_elo, float(temperature), int(threads))

    _ensure_artifacts(model_path, vocab_dir)

    global _POLICY, _POLICY_KEY
    with _POLICY_LOCK:
        if _POLICY is not None and _POLICY_KEY == key:
            return _POLICY
        old = _POLICY
        _POLICY = MaiaOnnxPolicy(
            model_path, vocab_dir, self_elo, opp_elo, temperature, threads
        )
        _POLICY_KEY = key
    if old is not None:
        old.close()
    return _POLICY


def unload() -> None:
    """Сбрасывает singleton и освобождает ONNX-сессию (guard перед Stockfish)."""
    global _POLICY, _POLICY_KEY
    with _POLICY_LOCK:
        policy = _POLICY
        _POLICY = None
        _POLICY_KEY = None
    if policy is not None:
        policy.close()
