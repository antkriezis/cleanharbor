#!/usr/bin/env python3
"""
CleanHarbor Pipeline - IHM Processing & Analysis

Run:
    python main.py --pdf "data/MV_EUROFERRY_OLYMPIA_IHM.pdf"
    python main.py --pdf "data/MV_EUROFERRY_OLYMPIA_IHM.pdf" --model "gpt-5"
"""

import argparse
import time
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

# Step 1: IHM Parser
from extract_hazmat_from_pdf import extract as extract_hazmat

# Step 2: EWC Classification
from classify_ewc import classify_json_file




def run_pipeline(pdf_path: Path, model: str = "gpt-5") -> Path:
    """
    Run the full CleanHarbor processing pipeline.
    
    Returns the path to the final JSON extraction.
    """
    outputs_dir = Path("outputs/JSON Extractions")
    date_str = datetime.now().strftime("%Y-%m-%d")
    json_output = outputs_dir / f"{pdf_path.stem}_extract_{date_str}.json"
    
    # ─────────────────────────────────────────────
    # Step 1: Extract hazmat data from IHM PDF
    # ─────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"STEP 1: Extracting hazmat from {pdf_path.name}")
    print(f"{'='*50}")
    extract_hazmat(pdf_path, json_output, model=model)
    
    # ─────────────────────────────────────────────
    # Step 2: Classify items with EWC codes
    # ─────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"STEP 2: Classifying with EWC codes")
    print(f"{'='*50}")
    ewc_output = classify_json_file(json_output, model=model)
    
    print(f"\n{'='*50}")
    print(f"✅ PIPELINE COMPLETE")
    print(f"{'='*50}")
    print(f"   Extraction: {json_output}")
    print(f"   EWC Output: {ewc_output}")
    
    return ewc_output


def main() -> None:
    ap = argparse.ArgumentParser(
        description="CleanHarbor - IHM Processing Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --pdf "data/MV_EUROFERRY_OLYMPIA_IHM.pdf"
  python main.py --pdf "data/ship.pdf" --model "gpt-5"
        """
    )
    ap.add_argument(
        "--pdf", 
        required=True, 
        help="Path to the IHM PDF file"
    )
    ap.add_argument(
        "--model", 
        default="gpt-5", 
        help="OpenAI model (default: gpt-5)"
    )
    args = ap.parse_args()
    
    pdf = Path(args.pdf).expanduser().resolve()
    
    if not pdf.exists():
        raise SystemExit(f"❌ PDF not found: {pdf}")
    
    start = time.perf_counter()
    run_pipeline(pdf, model=args.model)
    end = time.perf_counter()
    print(f"\n Total execution time: {end - start:.2f} seconds")



if __name__ == "__main__":
    main()

