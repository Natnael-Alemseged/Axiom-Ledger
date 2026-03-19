"""CLI for running triage on a PDF."""

import argparse
import json
import sys
from pathlib import Path

from refinery.triage.agent import run_triage
from refinery.triage.exceptions import RefineryTriageError


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run document triage on a PDF and print or save the profile."
    )
    parser.add_argument(
        "pdf_path",
        type=Path,
        help="Path to the PDF file",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Do not save profile to .refinery/profiles/",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print profile as JSON to stdout",
    )
    parser.add_argument(
        "--doc-id",
        type=str,
        default=None,
        help="Override doc_id (default: SHA-256 of file content)",
    )
    args = parser.parse_args()

    path = args.pdf_path.resolve()
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)
    if not path.is_file():
        print(f"Error: not a file: {path}", file=sys.stderr)
        sys.exit(1)

    try:
        profile = run_triage(
            path,
            doc_id=args.doc_id,
            save=not args.no_save,
        )
    except RefineryTriageError as e:
        print(f"Triage error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        # Exclude source_path for clean JSON; include computed estimated_extraction_cost
        data = profile.model_dump(mode="json")
        data.pop("source_path", None)
        print(json.dumps(data, indent=2))
    else:
        print(f"doc_id:        {profile.doc_id}")
        print(f"origin_type:   {profile.origin_type}")
        print(f"layout:        {profile.layout_complexity}")
        print(f"domain_hint:   {profile.domain_hint}")
        print(f"language:      {profile.language.code} ({profile.language.confidence})")
        print(f"cost:          {profile.estimated_extraction_cost}")
        print(f"page_count:    {profile.page_count}")
        if not args.no_save:
            out = Path(".refinery/profiles") / f"{profile.doc_id}.json"
            print(f"saved:         {out}")


if __name__ == "__main__":
    main()
