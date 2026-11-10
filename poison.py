#!/usr/bin/env python3
"""Original PDF injector, exposed as both a CLI and a web-app backend."""

from __future__ import annotations

import argparse
from pathlib import Path

from subliminal_poison.injector import poison


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject a hidden instruction into an authorized PDF.")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", nargs="?", type=Path)
    parser.add_argument("-p", "--prompt", required=True, help="Exact injection prompt")
    parser.add_argument(
        "-r",
        "--repetitions",
        type=int,
        default=6,
        help="total hidden-text copies per page (default: 6)",
    )
    args = parser.parse_args()

    output = args.output or args.input.with_name(f"{args.input.stem}--injected.pdf")
    instruction, pages = poison(
        args.input,
        output,
        args.prompt,
        reps=args.repetitions,
    )
    print(f"Created {output} ({pages} pages)")
    print(f"Hidden instruction: {instruction}")


if __name__ == "__main__":
    main()

