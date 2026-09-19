"""Точка входа для запуска через `python -m trainer`.

Репозиторий собирается как корневой пакет приложения, поэтому
``python -m trainer`` просто переисполняет корневой ``__main__.py``.
"""

from __future__ import annotations

import runpy
from pathlib import Path

runpy.run_path(
    str(Path(__file__).resolve().parent / "__main__.py"), run_name="__main__"
)