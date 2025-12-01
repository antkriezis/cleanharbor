#!/usr/bin/env python3
"""
Extract hazardous-material rows from an IHM PDF:
1) Extract the entire PDF to text (page markers included)
2) Call the Chat Completions API to keep only table rows (Part I/II/III)
3) Return a single JSON matching the schema

Can be run standalone or imported by main.py
"""

import json
import os
from pathlib import Path

import pdfplumber
from openai import OpenAI

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
def _call_single(client: OpenAI, model: str, full_text: str, schema_str: str) -> dict:
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

def _call_chunked(client: OpenAI, model: str, full_text: str, schema_str: str, max_chars: int = 12000) -> dict:
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
    for idx, c in enumerate(chunks, start=1):
        print(f"   Processing chunk {idx}/{len(chunks)}...")
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
# Main extraction function
# ----------------------------
def extract(pdf_path: Path, out_path: Path, model: str = "gpt-5") -> dict:
    """
    Extract hazmat data from an IHM PDF and save to JSON.
    
    Args:
        pdf_path: Path to the input PDF
        out_path: Path where JSON output will be saved
        model: OpenAI model to use
        
    Returns:
        The extracted data dictionary
    """
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not set (use .env or export in shell).")

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"   Reading PDF: {pdf_path.name}")
    full_text, pages_total = extract_full_pdf_text(pdf_path)
    schema_str = json.dumps(EXTRACTION_SCHEMA)

    print(f"   Calling OpenAI ({model})...")
    # Try a single call first
    try:
        data = _call_single(client, model, full_text, schema_str)
    except Exception as e:
        # If context too large / any failure, fallback to chunked
        print(f"   ℹ️  Falling back to chunked mode: {e}")
        data = _call_chunked(client, model, full_text, schema_str, max_chars=12000)

    # Build consistent final JSON
    result = {
        "document_meta": {
            "title": "Inventory Hazardous Material (IHM)",
            "pages_total": pages_total,
        },
        "rows": data.get("rows", []),
    }

    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"   ✅ Wrote {out_path}  ({len(result['rows'])} rows)")
    
    return result


# ----------------------------
# Standalone CLI (for testing this module directly)
# ----------------------------
if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    
    ap = argparse.ArgumentParser(description="Extract hazmat from IHM PDF (standalone)")
    ap.add_argument("--pdf", required=True, help="Path to the IHM PDF")
    ap.add_argument("--out", default=None, help="Output JSON path")
    ap.add_argument("--model", default="gpt-5", help="OpenAI model")
    args = ap.parse_args()

    pdf = Path(args.pdf).expanduser().resolve()
    
    if args.out:
        out = Path(args.out).expanduser().resolve()
    else:
        out = Path(f"outputs/JSON Extractions/{pdf.stem}_extract.json").resolve()

    if not pdf.exists():
        raise SystemExit(f"❌ PDF not found: {pdf}")

    extract(pdf, out, model=args.model)

