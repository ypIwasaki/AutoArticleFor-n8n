#!/usr/bin/env python3
"""Generate the latest AI guidance from reviewed article feedback."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

from article_feedback_service import write_article_feedback_instruction


def main() -> int:
    path = write_article_feedback_instruction(PROJECT_ROOT)
    print(f"Generated article feedback instruction: {path.relative_to(PROJECT_ROOT)}")
    print(f"Generated full feedback snapshot: {path.with_suffix('.json').relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
