#!/usr/bin/env python3
"""Generate the latest AI guidance from reviewed article feedback."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "talent-dashboard"))

from server import write_article_feedback_instruction  # noqa: E402


def main() -> int:
    path = write_article_feedback_instruction()
    print(f"Generated article feedback instruction: {path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
