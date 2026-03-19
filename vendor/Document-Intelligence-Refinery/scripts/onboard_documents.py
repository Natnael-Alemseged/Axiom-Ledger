#!/usr/bin/env python3
"""
Run triage + extraction on a directory of PDFs so outputs appear in .refinery/profiles/ and .refinery/extractions/.

Usage:
  uv run python scripts/onboard_documents.py data/
  uv run python scripts/onboard_documents.py data/ --max-docs 12
  uv run python scripts/onboard_documents.py data/ --max-docs 12 --no-save  # dry run (triage only, no write)

Use this for Step 1 of the Master Thinker flow: process 12+ documents so build_artifacts.py can build PageIndex and example Q&A.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from refinery.triage.agent import run_triage
from refinery.triage.exceptions import RefineryTriageError
from refinery.agents.extractor import run_extraction


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run triage + extraction on PDFs in a directory; write profiles and extractions to .refinery/"
    )
    parser.add_argument(
        "pdf_dir",
        type=Path,
        help="Directory containing PDF files (e.g. data/)",
    )
    parser.add_argument(
        "--max-docs",
        type=int,
        default=12,
        help="Maximum number of documents to process (default 12)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Do not write profiles or extractions (dry run)",
    )
    args = parser.parse_args()

    pdf_dir = args.pdf_dir.resolve()
    if not pdf_dir.is_dir():
        print(f"Error: not a directory: {pdf_dir}", file=sys.stderr)
        sys.exit(1)

    pdfs = sorted(pdf_dir.glob("*.pdf"))[: args.max_docs]
    if not pdfs:
        print(f"No PDFs found in {pdf_dir}", file=sys.stderr)
        sys.exit(1)

    save = not args.no_save
    ok = 0
    for path in pdfs:
        try:
            profile = run_triage(path, save=save)
            doc = run_extraction(path, profile=profile, save=save)
            print(f"  {path.name} -> {profile.doc_id} ({doc.status})")
            ok += 1
        except RefineryTriageError as e:
            print(f"  {path.name} triage error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"  {path.name} error: {e}", file=sys.stderr)

    print(f"Processed {ok}/{len(pdfs)} documents.")
    if save and ok:
        print("Profiles: .refinery/profiles/")
        print("Extractions: .refinery/extractions/")


if __name__ == "__main__":
    main()
