from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(prog="trainer", description="Chess Trainer CLI")
    parser.add_argument("--version", action="version", version="chess-trainer 0.1.0")
    args = parser.parse_args()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())