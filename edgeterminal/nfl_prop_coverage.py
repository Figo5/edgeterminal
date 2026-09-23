"""Write an immutable, research-only coverage report from stored prop captures."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import db, nfl_props


def write_report(batch_id: str, output: Path) -> dict:
    report = nfl_props.coverage_report(batch_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Report stored NFL prop coverage without API calls")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = write_report(args.batch_id, args.output)
    print(json.dumps({"output": str(args.output), "events_seen": report["events_seen"],
                      "events_mapped": report["events_mapped"],
                      "quotes": report["total_quotes"]}, sort_keys=True))


if __name__ == "__main__":
    main()
