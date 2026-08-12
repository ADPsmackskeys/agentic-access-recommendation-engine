#!/usr/bin/env python
"""Convert the client CSV extracts into `seed/*.json`.

`seed/client/*.csv` is what the client sent. `seed/*.json` is the ground truth
the project loads from. This script is the only thing that maps one to the
other, and it is a faithful transliteration: one JSON file per CSV, the same
column names, the same row order, every row.

Numeric columns are emitted as JSON numbers rather than strings so that the
loader never has to guess; everything else is passed through verbatim.

The conversion is deterministic - stable ordering, no timestamps - so a re-run
produces either a clean diff or none at all. `--check` asserts that the JSON on
disk still matches the CSVs, which is what catches a partial or truncated
conversion.

Usage:
    python scripts/convert_client_csv.py
    python scripts/convert_client_csv.py --check
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

# Columns that must be emitted as JSON numbers. Anything not listed here stays
# a string, including ids that merely look numeric.
INTEGER_COLUMNS = frozenset({"risk_score", "peer_count", "total_peers"})
FLOAT_COLUMNS = frozenset({"affinity_score"})

# Every extract the project expects. Listing them explicitly means a missing
# file is an error rather than a silently smaller corpus.
EXTRACTS = (
    "entitlement_catalog",
    "entitlement_risk_scores",
    "identities",
    "new_joiners",
    "peer_affinity_scores",
    "policy_rules",
    "sod_rules",
)


def _coerce(column: str, value: str) -> Any:
    value = value.strip()
    if not value:
        return None
    if column in INTEGER_COLUMNS:
        return int(value)
    if column in FLOAT_COLUMNS:
        number = float(value)
        # The client writes whole percentages; keep them as ints so the JSON
        # reads the same as the CSV rather than gaining a spurious `.0`.
        return int(number) if number.is_integer() else number
    return value


def read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Expected client extract not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = [
            {column: _coerce(column, value or "") for column, value in row.items()}
            for row in reader
            if any((value or "").strip() for value in row.values())
        ]
    if not rows:
        raise ValueError(f"{path.name} contained no data rows")
    return rows


def convert(source_dir: Path) -> dict[str, list[dict[str, Any]]]:
    return {name: read_csv(source_dir / f"{name}.csv") for name in EXTRACTS}


def _serialise(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="seed/client", help="Directory of client CSV extracts.")
    parser.add_argument("--out", default="seed", help="Directory to write the JSON corpus into.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if the JSON on disk differs from the CSVs.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    source_dir = Path(args.dir) if Path(args.dir).is_absolute() else root / args.dir
    out_dir = Path(args.out) if Path(args.out).is_absolute() else root / args.out

    try:
        outputs = convert(source_dir)
    except Exception as exc:
        print(f"Conversion failed: {exc}", file=sys.stderr)
        return 1

    if args.check:
        stale: list[str] = []
        for name, rows in outputs.items():
            path = out_dir / f"{name}.json"
            if not path.exists() or path.read_text(encoding="utf-8") != _serialise(rows):
                stale.append(f"{name}.json")
        if stale:
            print("Out of date with seed/client (re-run without --check):", file=sys.stderr)
            for name in stale:
                print(f"  {name}", file=sys.stderr)
            return 1
        print(f"seed JSON matches the client CSVs ({len(outputs)} files).")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Wrote to {out_dir}:")
    for name, rows in outputs.items():
        (out_dir / f"{name}.json").write_text(_serialise(rows), encoding="utf-8")
        print(f"  {name + '.json':32s} {len(rows):4d} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
