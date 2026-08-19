#!/usr/bin/env python3
"""Session-scoped CLI used by the current agent to durably report one turn."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.project_core_runtime import submit_checkpoint


def main() -> int:
    parser = argparse.ArgumentParser(prog="report_checkpoint")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = json.load(sys.stdin)
        result = submit_checkpoint(args.config, report)
    except Exception as exc:  # noqa: BLE001 - narrow CLI boundary
        print(f"report_checkpoint failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
