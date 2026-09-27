#!/usr/bin/env python3
"""Generate APNs copy from ReciApp's Localizable.xcstrings source of truth."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT.parent / "ReciApp-iOS" / "ReciApp" / "Localizable.xcstrings"
DEFAULT_OUTPUT = ROOT / "app" / "apns_localizations.json"
KEYS = ("Your recipe is ready", "Open ReciApp to find it in your library.")


def main() -> None:
    import sys

    sys.path.insert(0, str(ROOT))
    from app.localization import SUPPORTED_LANGUAGE_CODES

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    catalog = json.loads(args.source.read_text(encoding="utf-8"))
    result: dict[str, dict[str, str]] = {}
    for language in SUPPORTED_LANGUAGE_CODES:
        values: list[str] = []
        for key in KEYS:
            localization = catalog["strings"][key].get("localizations", {}).get(language)
            unit = localization.get("stringUnit", {}) if localization else {}
            value = unit.get("value") or (key if language.startswith("en-") else None)
            if not value:
                raise ValueError(f"Missing APNs translation: {language}/{key}")
            values.append(value)
        result[language] = {"title": values[0], "body": values[1]}

    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(result)} APNs localizations to {args.output}")


if __name__ == "__main__":
    main()
