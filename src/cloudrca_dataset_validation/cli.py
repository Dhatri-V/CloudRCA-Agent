"""Command-line interface for deterministic dataset profiling."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .profiler import profile_directory


def main() -> None:
    """Profile the requested directory and write deterministic JSON output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Directory containing extracted validation files")
    parser.add_argument("--output", type=Path, required=True, help="JSON profile output path")
    args = parser.parse_args()
    result = profile_directory(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
