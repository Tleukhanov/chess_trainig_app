#!/usr/bin/env python
"""Идемпотентная загрузка модели Maia-2 ONNX, словарей и parity-фикстур.

Качает в ``MAIA_DIR`` (по умолчанию ``data/maia``) саму ONNX-модель
(``maia2-rapid.onnx``, ~89 MiB, Git-LFS) и вспомогательные файлы
(``all_moves.txt``, ``elo_dict.json``, ``config.yaml``), а в
``tests/fixtures_maia`` — фикстуры для тестов паритета. Существующие
файлы пропускаются; ``--force`` перекачивает заново.

Пригоден и локально, и на старте Heroku (эфемерный диск -> каждый релиз
качает заново). Без внешних зависимостей: сначала ``urllib``, при сбое —
``curl``, если он есть. Завершается ненулевым кодом при ошибке.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import config as _config  # noqa: E402

MAIA_RAW_BASE = "https://raw.githubusercontent.com/ChessWaveIO/maia2-uci/main/models"
FIXTURES_RAW_BASE = (
    "https://raw.githubusercontent.com/ChessWaveIO/maia2-uci/main/tests/fixtures"
)
_USER_AGENT = "chess-trainer-maia-fetch/1.0"


def _human(num_bytes: float) -> str:
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _download_urllib(url: str, dest: Path) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"}
    )
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        last_reported = -1
        with open(tmp, "wb") as handle:
            while True:
                chunk = response.read(1024 * 512)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if total:
                    percent = int(downloaded * 100 / total)
                    if percent >= last_reported + 5:
                        last_reported = percent
                        print(
                            f"    {percent:3d}%  {_human(downloaded)} / {_human(total)}",
                            flush=True,
                        )
    os.replace(tmp, dest)


def _download_curl(url: str, dest: Path) -> None:
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("curl не найден для резервной загрузки")
    tmp = dest.with_name(dest.name + ".part")
    result = subprocess.run(
        [
            curl,
            "--fail",
            "--location",
            "--retry",
            "3",
            "--user-agent",
            _USER_AGENT,
            "-o",
            str(tmp),
            url,
        ]
    )
    if result.returncode != 0 or not tmp.is_file():
        raise RuntimeError(f"curl завершился с кодом {result.returncode}")
    os.replace(tmp, dest)


def fetch_file(url: str, dest: Path, *, force: bool = False) -> bool:
    """Качает ``url`` в ``dest``. Возвращает True, если что-то скачано.

    Существующий непустой файл пропускается, если не задан ``force``.
    """
    dest = Path(dest)
    if dest.is_file() and dest.stat().st_size > 0 and not force:
        print(f"  = есть  {dest} ({_human(dest.stat().st_size)})")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  > качаю {dest.name} <- {url}", flush=True)
    try:
        _download_urllib(url, dest)
    except Exception as urllib_error:  # noqa: BLE001
        print(f"    urllib не смог ({urllib_error}); пробую curl", flush=True)
        _download_curl(url, dest)
    size = dest.stat().st_size
    print(f"  + готово {dest} ({_human(size)})", flush=True)
    return True


def _plan(model_url: str, maia_dir: Path, fixtures_dir: Path) -> list[tuple[str, Path]]:
    model_name = Path(model_url).name or "maia2-rapid.onnx"
    return [
        (model_url, maia_dir / model_name),
        (f"{MAIA_RAW_BASE}/all_moves.txt", maia_dir / "all_moves.txt"),
        (f"{MAIA_RAW_BASE}/elo_dict.json", maia_dir / "elo_dict.json"),
        (f"{MAIA_RAW_BASE}/config.yaml", maia_dir / "config.yaml"),
        (f"{FIXTURES_RAW_BASE}/fixtures_rapid.json", fixtures_dir / "fixtures_rapid.json"),
        (f"{MAIA_RAW_BASE}/all_moves.txt", fixtures_dir / "all_moves.txt"),
        (f"{MAIA_RAW_BASE}/elo_dict.json", fixtures_dir / "elo_dict.json"),
    ]


def download_all(
    *,
    model_url: str | None = None,
    maia_dir: str | Path | None = None,
    fixtures_dir: str | Path | None = None,
    force: bool = False,
) -> list[Path]:
    """Скачивает все артефакты. Возвращает список скачанных путей."""
    settings = _config.settings
    model_url = model_url or settings.maia_model_url
    maia_dir = Path(maia_dir) if maia_dir is not None else Path(settings.maia_dir)
    fixtures_dir = (
        Path(fixtures_dir)
        if fixtures_dir is not None
        else PROJECT_ROOT / "tests" / "fixtures_maia"
    )
    downloaded: list[Path] = []
    for url, dest in _plan(model_url, maia_dir, fixtures_dir):
        if fetch_file(url, dest, force=force):
            downloaded.append(dest)
    return downloaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Скачать модель Maia-2 ONNX и словари.")
    parser.add_argument("--force", action="store_true", help="перекачать даже существующие файлы")
    parser.add_argument("--dir", dest="maia_dir", default=None, help="каталог модели (MAIA_DIR)")
    parser.add_argument(
        "--fixtures-dir", dest="fixtures_dir", default=None, help="каталог тестовых фикстур"
    )
    parser.add_argument("--model-url", dest="model_url", default=None, help="URL ONNX-модели")
    args = parser.parse_args(argv)

    try:
        downloaded = download_all(
            model_url=args.model_url,
            maia_dir=args.maia_dir,
            fixtures_dir=args.fixtures_dir,
            force=args.force,
        )
    except Exception as error:  # noqa: BLE001
        print(f"ОШИБКА загрузки Maia: {error}", file=sys.stderr, flush=True)
        return 1

    if downloaded:
        print(f"\nСкачано файлов: {len(downloaded)}")
    else:
        print("\nВсё уже на месте.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
