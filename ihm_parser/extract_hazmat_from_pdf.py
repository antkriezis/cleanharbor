#!/usr/bin/env python3
"""
Extract hazardous-material rows from an IHM PDF:
1) Extract the entire PDF to text (page markers included)
2) Call the Chat Completions API to keep only table rows (5.2+ / Part II & III)
3) Return a single JSON matching the schema

Run (from the ihm_parser/ folder):
  $env:OPENAI_API_KEY="sk-..."  # Windows PowerShell
  export OPENAI_API_KEY=sk-...  # macOS/Linux

  python extract_hazmat_from_pdf.py \
      --pdf "data/MV_EUROFERRY_OLYMPIA_IHM.pdf" \
      --out "outputs/ihm_extract.json" \
      --model "gpt-5"
"""

import argparse
import json
import os
from pathlib import Path

import pdfplumber
from openai import OpenAI

# Optional .env loading
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ----------------------------
# JSON Schema
# ----------------------------
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "document_meta": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "pages_total": {"type": "integer"},
            },
            "required": []
        },
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    # provenance
                    "chapter": {"type": "string"},
                    "section_title": {"type": "string"},

                    # parsed content
                    "material": {"type": "string"},
                    "item_name": {"type": "string"},
                    "location": {"type": "string"},
                    "quantity_value": {"type": ["number", "string"]},
                    "quantity_unit": {"type": "string"},
                    "hazard_flags": {"type": "array", "items": {"type": "string"}},
                    "remarks": {"type": "string"},

                    # audit trail
                    "page": {"type": "integer"},
                    "row_index": {"type": "integer"},
                    "source_text": {"type": "string"},
                },
                "required": ["chapter", "material", "location", "page"]
            }
        }
    },
    "required": ["rows"]
}

PROMPT_INSTRUCTIONS = """You are an expert marine-compliance analyst.
Task: From the provided IHM PDF text, extract ONLY hazardous-material rows listed in TABLES in PART I/II/III).

Return ONLY a JSON object following the provided JSON Schema exactly (no extra commentary).

Parsing rules & scope:
- Focus on table-like content (e.g., columns like: No., Location, Name of item, Approx. quantity, Remarks).
- For each hazardous material/store/waste table row, capture:
  chapter, section_title, table_id (or "unknown"), material, item_name (if any),
  location, quantity_value, quantity_unit, hazard_flags (keywords like lead, HFC, PFOS, PCB, oil, sludge, battery),
  remarks (short), page number, row_index (1-based within that table), and a short source_text snippet.
- Normalize obvious units to one of: pcs, L, m3, kg (keep original text if ambiguous).
- If quantity is "~" or a range, keep it as a string and explain briefly in remarks.
- Exclude clearly non-hazardous media (e.g., ballast water, fresh water) unless explicitly flagged as hazardous.
- If a section states "none" for a regulated substance, do NOT add a row.
- Keep numbers numeric when the document uses a precise value; otherwise use string.

Output formatting:
- Return ONLY valid JSON matching the provided schema.
"""

TIGHT_RULES = """
- Always include 'page' using the PAGE header like '--- PAGE 17 ---' if present.
- Batteries => include 'lead-battery' in hazard_flags; fuels/lube/sludge => include 'oil'; HFCs (e.g., R448) => include 'HFC'.
- Do not invent rows. If no table-like lines exist, return {"rows": []}.
"""

# ----------------------------
# PDF text extraction (entire doc)
# ----------------------------
def extract_full_pdf_text(pdf_path: Path) -> tuple[str, int]:
    """
    Return (whole_text, total_pages) with page markers.
    """
    pages_total = 0
    buf = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        pages_total = len(pdf.pages)
        for i, page in enumerate(pdf.pages, start=1):
            txt = (page.extract_text() or "").strip()
            buf.append(f"--- PAGE {i} ---\n{txt}")
    return "\n\n".join(buf), pages_total

# ----------------------------
# OpenAI helpers
# ----------------------------
def call_single(client: OpenAI, model: str, full_text: str, schema_str: str) -> dict:
    """
    Attempt one single call with the entire PDF text.
    """
    prompt = (
        f"{PROMPT_INSTRUCTIONS}\n{TIGHT_RULES}\n\n"
        f"JSON Schema:\n{schema_str}\n\n"
        f"FULL PDF TEXT:\n{full_text}"
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a marine-compliance analyst. Return ONLY valid JSON."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)

def call_chunked(client: OpenAI, model: str, full_text: str, schema_str: str, max_chars: int = 12000) -> dict:
    """
    Fallback: if entire text is too large, split by page marker and merge results.
    """
    # split by page markers to keep context meaningful
    parts = full_text.split("\n\n--- PAGE ")
    # Reattach the first marker if it started with it
    if parts and not parts[0].startswith("--- PAGE "):
        parts[0] = parts[0].lstrip()
        for i in range(1, len(parts)):
            parts[i] = f"--- PAGE {parts[i]}"

    chunks = []
    cur = ""
    for part in parts:
        if not part.strip():
            continue
        # ensure each small block starts on a page header
        block = part if part.startswith("--- PAGE ") else f"--- PAGE {part}"
        if len(cur) + len(block) + 2 > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
        cur += ("\n\n" + block) if cur else block
    if cur:
        chunks.append(cur)

    all_rows = []
    for c in chunks:
        prompt = (
            f"{PROMPT_INSTRUCTIONS}\n{TIGHT_RULES}\n\n"
            f"JSON Schema:\n{schema_str}\n\n"
            f"PDF TEXT CHUNK:\n{c}"
        )
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a marine-compliance analyst. Return ONLY valid JSON."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        all_rows.extend(data.get("rows", []))

    return {"document_meta": {}, "rows": all_rows}

# ----------------------------
# Main extraction pipeline
# ----------------------------
def extract(pdf_path: Path, out_path: Path, model: str = "gpt-5") -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not set (use .env or export in shell).")

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    full_text, pages_total = extract_full_pdf_text(pdf_path)
    schema_str = json.dumps(EXTRACTION_SCHEMA)

    # Try a single call first
    try:
        data = call_single(client, model, full_text, schema_str)
    except Exception as e:
        # If context too large / any failure, fallback to chunked without bothering you
        print(f"ℹ️  Falling back to chunked mode: {e}")
        data = call_chunked(client, model, full_text, schema_str, max_chars=12000)

    # Build consistent final JSON
    result = {
        "document_meta": {
            "title": "Inventory Hazardous Material (IHM)",
            "pages_total": pages_total,
        },
        "rows": data.get("rows", []),
    }

    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"✅ Wrote {out_path}  ({len(result['rows'])} rows)")

# ----------------------------
# CLI
# ----------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True, help="Path to the IHM PDF (relative or absolute)")
    ap.add_argument("--out", default="outputs/ihm_extract.json", help="Where to write the JSON")
    ap.add_argument("--model", default="gpt-5", help="Model name (e.g., gpt-5, gpt-4o, gpt-4o-mini)")
    args = ap.parse_args()

    pdf = Path(args.pdf).expanduser().resolve()
    
    pdf_stem = pdf.stem
    if args.out == "outputs/ihm_extract.json":
        out = Path(f"outputs/{pdf_stem}_extract.json").resolve()
    else:
        out = Path(args.out).expanduser().resolve()

    if not pdf.exists():
        raise SystemExit(f"PDF not found: {pdf}")

    extract(pdf, out, model=args.model)

# ----------------------------
# DEBUG: Just extract and show pdfplumber output
# ----------------------------
if __name__ == "__main__":
    '''
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True, help="Path to the IHM PDF (relative or absolute)")
    ap.add_argument("--out", default="outputs/pdfplumber_text.json", help="Where to save extracted text")
    args = ap.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()

    if not pdf_path.exists():
        raise SystemExit(f"❌ PDF not found: {pdf_path}")

    print(f"📖 Extracting text from {pdf_path.name} using pdfplumber...")

    import pdfplumber
    text_blocks = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = (page.extract_text() or "").strip()
            text_blocks.append(f"--- PAGE {i} ---\n{text}")
    full_text = "\n\n".join(text_blocks)

    # Save the raw text to a file for inspection
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(full_text, encoding="utf-8")

    # Print debug info
    print(f"✅ Extracted {len(text_blocks)} pages.")
    print(f"📄 Saved extracted text to: {out_path}")
    print("\n--- SAMPLE OUTPUT (first 1000 characters) ---")
    print(full_text[:1000])
    print("\n--------------------------------------------")
    '''
    main()
