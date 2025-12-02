#!/usr/bin/env python3
"""
EWC Code Classification for IHM Extraction Results

Classifies extracted hazardous materials into European Waste Catalogue (EWC) codes
using GPT-5 and the official EWC classification rules.

Can be run standalone or imported by main.py
Supports both file-based and in-memory classification for serverless deployment.
"""

import json
import os
from pathlib import Path
from typing import Optional

from openai import OpenAI
from supabase import create_client, Client

# ----------------------------
# EWC Classification Rules (from LoW guidance)
# ----------------------------
EWC_CLASSIFICATION_RULES = """
## EWC Code Selection Rules (Commission Decision 2000/532/EC)

### Order of Precedence for Chapter Selection:

**Step 1 - Identification by Waste Source:**
- Chapters 01-12 and 17-20 refer specifically to industry process waste and municipal waste
- If your waste falls into one of these chapters, use the most appropriate code
- Do NOT use a 99 code at Step 1 if a more specific entry exists in other chapters

**Step 2 - Identification by Waste Type:**
- If no appropriate entry found in Step 1, check chapters 13, 14, and 15
- Chapter 13: Oil wastes and liquid fuel wastes
- Chapter 14: Waste organic solvents, refrigerants and propellants
- Chapter 15: Waste packaging, absorbents, wiping cloths, filter materials, protective clothing

**Step 3 - Other General Wastes:**
- If not found in chapters 01-15 or 17-20, check chapter 16
- Chapter 16 contains: vehicles, electronic equipment, batteries, catalysts, laboratory chemicals, oxidisers

**Step 4 - Non-Specific Wastes:**
- Only use 99 codes (e.g., 20 01 99) if no suitable alternative exists in another chapter

### Entry Types (Hazardous Classification):

**AH (Absolute Hazardous):**
- Always hazardous regardless of composition
- Examples: fuel oil, diesel, PCBs, asbestos

**AN (Absolute Non-Hazardous):**
- Never hazardous
- No link to mirror entries
- Examples: waste bark and cork, uncontaminated soil

**MH (Mirror Hazardous):**
- Hazardous if contains dangerous substances above threshold
- Description contains "dangerous substances" reference
- Examples: sludges containing dangerous substances

**MN (Mirror Non-Hazardous):**
- Non-hazardous version of mirror entry
- Often described as "other than those mentioned in..."
- Examples: sludges other than those mentioned in the hazardous entry

### Ship Recycling Specific Guidance:
- Lead-acid batteries → 16 06 01 (lead batteries) - AH
- Other batteries → 16 06 02 (nickel-cadmium) or 16 06 04 (alkaline)
- Fuel oils, diesel → 13 07 01 (fuel oil and diesel) - AH
- Lubricating oils → 13 02 XX codes - typically AH
- Bilge water/oily water → 13 05 XX codes
- Sludges → 13 05 02 (sludges from oil/water separators)
- HFC refrigerants → 14 06 01 (CFCs, HCFCs, HFCs) - AH
- Paints → 08 01 11 (containing organic solvents/hazardous) or 08 01 12 (other)
- Chemical products → various chapter 06/07 codes depending on type
"""

PROMPT_TEMPLATE = """You are an expert waste classification analyst specializing in the European Waste Catalogue (EWC).

Your task: Classify each of the following hazardous material items into the correct 6-digit EWC code.

{rules}

## EWC Codes Reference (priority codes listed first):
{ewc_codes}

## Items to Classify:
{items_list}

## Instructions:
1. Analyze each material based on its description, hazard flags, and context
2. Follow the chapter precedence rules (Step 1-4)
3. Consider whether hazardous (AH/MH) or non-hazardous (AN/MN) entry applies
4. Select the BEST matching 6-digit EWC code for each item
5. Also identify up to 3 alternative candidate codes that could reasonably apply

Return ONLY a JSON object with this exact structure:
{{
  "classifications": [
    {{
      "item_index": 0,
      "ewc_code": "XXXXXX",
      "ewc_candidates": ["YYYYYY", "ZZZZZZ"]
    }},
    ...
  ]
}}

Rules for ewc_candidates:
- Include 0-3 alternative codes that could also apply
- Do NOT include the main ewc_code in this list
- Only include codes that are genuinely plausible alternatives
- If no alternatives fit, use an empty array []

IMPORTANT: Return exactly {item_count} classifications in the same order as the items listed above.
"""


def fetch_ewc_codes(supabase: Client) -> list[dict]:
    """
    Fetch all EWC codes from Supabase, sorted by priority.
    
    Returns codes with priority=True first, then the rest.
    """
    response = supabase.table("ewc_codes").select("*").execute()
    codes = response.data
    
    # Sort: priority=True first, then by code
    priority_codes = [c for c in codes if c.get("priority")]
    other_codes = [c for c in codes if not c.get("priority")]
    
    # Sort each group by code
    priority_codes.sort(key=lambda x: x["code"])
    other_codes.sort(key=lambda x: x["code"])
    
    return priority_codes + other_codes


def format_ewc_codes_for_prompt(codes: list[dict]) -> str:
    """
    Format EWC codes into a readable reference for the LLM prompt.
    """
    lines = []
    current_chapter = None
    
    for c in codes:
        chapter = c.get("chapter")
        if chapter != current_chapter:
            current_chapter = chapter
            lines.append(f"\n### Chapter {chapter}")
        
        # Format: CODE | description | entry_type | hazardous
        hazard_marker = "⚠️" if c.get("hazardous") else ""
        priority_marker = "★" if c.get("priority") else ""
        entry_type = c.get("entry_type", "")
        
        lines.append(
            f"{priority_marker}{c['code']} | {c['description'][:80]}... | {entry_type} {hazard_marker}"
            if len(c.get("description", "")) > 80
            else f"{priority_marker}{c['code']} | {c.get('description', '')} | {entry_type} {hazard_marker}"
        )
    
    return "\n".join(lines)


def classify_batch(
    client: OpenAI,
    model: str,
    items: list[dict],
    ewc_codes_text: str,
    all_codes: list[dict]
) -> list[dict]:
    """
    Classify all extracted items into EWC codes in a single API call.
    
    Returns list of dicts with ewc_code and ewc_candidates for each item.
    """
    # Build items list for prompt
    items_list_lines = []
    for i, item in enumerate(items):
        items_list_lines.append(
            f"### Item {i}:\n"
            f"- Material: {item.get('material', 'Unknown')}\n"
            f"- Item Name: {item.get('item_name', '')}\n"
            f"- Location: {item.get('location', '')}\n"
            f"- Quantity: {item.get('quantity_value', '')} {item.get('quantity_unit', '')}\n"
            f"- Hazard Flags: {', '.join(item.get('hazard_flags', []))}\n"
            f"- Remarks: {item.get('remarks', '')}\n"
            f"- Source Context: {item.get('source_text', '')}"
        )
    items_list = "\n\n".join(items_list_lines)
    
    # Build the prompt
    prompt = PROMPT_TEMPLATE.format(
        rules=EWC_CLASSIFICATION_RULES,
        ewc_codes=ewc_codes_text,
        items_list=items_list,
        item_count=len(items)
    )
    
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a waste classification expert. Return ONLY valid JSON."
            },
            {"role": "user", "content": prompt}
        ],
        response_format={"type": "json_object"},
    )
    
    result = json.loads(resp.choices[0].message.content)
    classifications = result.get("classifications", [])
    
    # Validate and clean results
    valid_codes = {c["code"] for c in all_codes}
    cleaned_results = []
    
    for i in range(len(items)):
        # Find classification for this index
        classification = next(
            (c for c in classifications if c.get("item_index") == i),
            {"ewc_code": "", "ewc_candidates": []}
        )
        
        ewc_code = classification.get("ewc_code", "")
        if ewc_code and ewc_code not in valid_codes:
            print(f"   ⚠️  Invalid code {ewc_code} returned for item {i}, keeping as-is")
        
        # Clean candidates - remove main code if accidentally included
        candidates = classification.get("ewc_candidates", [])
        candidates = [c for c in candidates if c != ewc_code and c in valid_codes][:3]
        
        cleaned_results.append({
            "ewc_code": ewc_code,
            "ewc_candidates": candidates
        })
    
    return cleaned_results


def classify_materials(data: dict, model: str = "gpt-5") -> dict:
    """
    Classify all items in extracted hazmat data with EWC codes (for serverless deployment).
    
    Args:
        data: Dictionary containing 'rows' and optionally 'document_meta'
        model: OpenAI model to use for classification
        
    Returns:
        The same dictionary with ewc_code and ewc_candidates added to each row
        
    Raises:
        ValueError: If required environment variables are not set
    """
    # Validate environment
    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY environment variable is not set.")
    if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")
    
    # Initialize clients
    openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    supabase: Client = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    )
    
    rows = data.get("rows", [])
    if not rows:
        return data
    
    # Fetch EWC codes from Supabase
    ewc_codes = fetch_ewc_codes(supabase)
    
    # Format codes for prompt
    ewc_codes_text = format_ewc_codes_for_prompt(ewc_codes)
    
    # Classify all rows in a single batch call
    classifications = classify_batch(
        openai_client,
        model,
        rows,
        ewc_codes_text,
        ewc_codes
    )
    
    # Apply classifications to rows
    for row, classification in zip(rows, classifications):
        row["ewc_code"] = classification["ewc_code"]
        row["ewc_candidates"] = classification["ewc_candidates"]
    
    return data


def classify_json_file(json_path: Path, model: str = "gpt-5") -> Path:
    """
    Classify all items in a JSON extraction file with EWC codes.
    
    Args:
        json_path: Path to the input JSON file (from extraction pipeline)
        model: OpenAI model to use for classification
        
    Returns:
        Path to the output JSON file with _ewc appended to filename
    """
    # Validate environment
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not set (use .env or export in shell).")
    if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")
    
    # Initialize clients
    openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    supabase: Client = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    )
    
    # Load input JSON
    print(f"   Loading JSON: {json_path.name}")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    rows = data.get("rows", [])
    if not rows:
        print("   ⚠️  No rows to classify")
        output_path = json_path.with_stem(f"{json_path.stem}_ewc")
        output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return output_path
    
    # Fetch EWC codes from Supabase
    print("   Fetching EWC codes from Supabase...")
    ewc_codes = fetch_ewc_codes(supabase)
    print(f"   Loaded {len(ewc_codes)} EWC codes ({sum(1 for c in ewc_codes if c.get('priority'))} priority)")
    
    # Format codes for prompt
    ewc_codes_text = format_ewc_codes_for_prompt(ewc_codes)
    
    # Classify all rows in a single batch call
    print(f"   Classifying {len(rows)} items with {model} (batch call)...")
    classifications = classify_batch(
        openai_client,
        model,
        rows,
        ewc_codes_text,
        ewc_codes
    )
    
    # Apply classifications to rows
    for row, classification in zip(rows, classifications):
        row["ewc_code"] = classification["ewc_code"]
        row["ewc_candidates"] = classification["ewc_candidates"]
    
    # Build output path
    output_path = json_path.with_stem(f"{json_path.stem}_ewc")
    
    # Save result
    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"   ✅ Wrote {output_path} ({len(rows)} items classified)")
    
    return output_path


# ----------------------------
# Standalone CLI (for testing this module directly)
# ----------------------------
if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    
    ap = argparse.ArgumentParser(description="Classify IHM extraction JSON with EWC codes")
    ap.add_argument("--json", required=True, help="Path to the extraction JSON file")
    ap.add_argument("--model", default="gpt-5", help="OpenAI model (default: gpt-5)")
    args = ap.parse_args()
    
    json_path = Path(args.json).expanduser().resolve()
    
    if not json_path.exists():
        raise SystemExit(f"❌ JSON file not found: {json_path}")
    
    classify_json_file(json_path, model=args.model)

