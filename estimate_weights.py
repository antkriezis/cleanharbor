#!/usr/bin/env python3
"""
Weight Estimation for EWC-Classified Materials

Estimates weights in tonnes for each EWC code based on material characteristics.
Produces both detailed (per-row) and aggregated (per-code) weight data.

Can be run standalone or imported by api/weights/process.py
Supports both file-based and in-memory estimation for serverless deployment.
"""

import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
from typing import Optional, Callable

from openai import OpenAI


# ----------------------------
# Weight Estimation Core Logic
# ----------------------------

def get_ewc_code_column(headers: list[str]) -> Optional[str]:
    """
    Find the EWC Code column in the CSV headers.
    Handles various naming conventions.
    """
    candidates = ['EWC Code', 'ewc_code', 'EWC_Code', 'ewc code', 'code', 'Code']
    for candidate in candidates:
        if candidate in headers:
            return candidate
    # Case-insensitive fallback
    for header in headers:
        if 'ewc' in header.lower() and 'code' in header.lower():
            return header
        if header.lower() == 'code':
            return header
    return None


def estimate_weight_stub(ewc_code: str, row_context: dict) -> float:
    """
    STUB: Estimate weight in tonnes using deterministic hash.
    
    This ensures same input → same output for testing.
    Replace with actual API call when ready.
    
    Args:
        ewc_code: The EWC code (e.g., "17 01 01")
        row_context: Additional context from the row
        
    Returns:
        Estimated weight in tonnes (float, 3 decimal places)
    """
    if not ewc_code or ewc_code in ('', 'N/A', 'Unknown', None):
        return 0.0
    
    # Create deterministic hash from code + relevant context
    hash_input = f"{ewc_code}"
    
    # Include quantity if available for more realistic estimation
    qty = row_context.get('Qty') or row_context.get('quantity_value') or row_context.get('qty')
    if qty:
        try:
            qty_float = float(str(qty).replace(',', '.'))
            hash_input += f"|{qty_float}"
        except (ValueError, TypeError):
            pass
    
    # Generate deterministic weight from hash
    hash_bytes = hashlib.sha256(hash_input.encode()).digest()
    hash_int = int.from_bytes(hash_bytes[:4], 'big')
    base_weight = (hash_int % 50000) / 1000.0  # 0.000 to 49.999
    
    # Ensure minimum weight of 0.001 if code exists
    weight = max(0.001, base_weight)
    
    return round(weight, 3)


import json
import re

# Debug log storage (populated during debug mode)
_debug_logs = []

def _log_debug(message: str, data: dict = None):
    """Add to debug log if in debug mode."""
    entry = {'message': message}
    if data:
        entry['data'] = data
    _debug_logs.append(entry)
    # Also print for Vercel logs
    print(f"[WEIGHT_DEBUG] {message}: {data if data else ''}")

def get_debug_logs() -> list:
    """Get accumulated debug logs."""
    return _debug_logs.copy()

def clear_debug_logs():
    """Clear debug logs."""
    _debug_logs.clear()


# ----------------------------
# Constants
# ----------------------------
MAX_WEIGHT_PER_ROW_TONNES = 5.0  # Sanity cap for single row
CHUNK_SIZE = 100  # Rows per LLM call
MAX_SOURCE_TEXT_LEN = 200
MAX_REMARKS_LEN = 120


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max length."""
    if not text:
        return ""
    text = str(text).strip()
    if len(text) > max_len:
        return text[:max_len-3] + "..."
    return text


def _safe_float(val, default=0.0) -> float:
    """Safely convert value to float."""
    if val is None or val == '':
        return default
    try:
        # Handle comma as decimal separator
        return float(str(val).replace(',', '.'))
    except (ValueError, TypeError):
        return default


def _build_row_context(row: dict, row_id: int, ewc_column: str) -> dict:
    """
    Build a compact row context dict for LLM prompt.
    Includes all relevant fields with truncation for token efficiency.
    """
    return {
        "row_id": row_id,
        "ewc_code": row.get(ewc_column, '') or row.get('EWC Code', '') or '',
        "material": row.get('Material', '') or row.get('material', '') or '',
        "item_name": row.get('Item Name', '') or row.get('item_name', '') or '',
        "qty": row.get('Qty', '') or row.get('quantity_value', '') or '',
        "unit": row.get('Unit', '') or row.get('quantity_unit', '') or '',
        "location": row.get('Location', '') or row.get('location', '') or '',
        "hazard_flags": row.get('Hazard Flags', '') or row.get('hazard_flags', '') or '',
        "remarks": _truncate(row.get('Remarks', '') or row.get('remarks', '') or '', MAX_REMARKS_LEN),
        "source_text": _truncate(row.get('Source Text', '') or row.get('source_text', '') or '', MAX_SOURCE_TEXT_LEN),
        "chapter": row.get('Chapter', '') or row.get('chapter', '') or '',
        "section": row.get('Section', '') or row.get('section', '') or '',
    }


def _build_batch_prompt(row_contexts: list[dict]) -> str:
    """
    Build the LLM prompt for batch weight estimation.
    Uses full row context for accurate estimation.
    """
    # Build items list with full context
    items_lines = []
    for ctx in row_contexts:
        qty_str = f"{ctx['qty']} {ctx['unit']}".strip() if ctx['qty'] else "(not specified)"
        
        item_desc = f"""Row {ctx['row_id']}:
  EWC Code: {ctx['ewc_code'] or '(none)'}
  Material: {ctx['material'] or '(none)'}
  Item Name: {ctx['item_name'] or '(none)'}
  Qty/Unit: {qty_str}
  Location: {ctx['location'] or '(none)'}
  Hazard Flags: {ctx['hazard_flags'] or '(none)'}
  Remarks: {ctx['remarks'] or '(none)'}
  Source: {ctx['source_text'] or '(none)'}"""
        items_lines.append(item_desc)
    
    items_text = "\n\n".join(items_lines)
    
    prompt = f"""You are a waste weight estimation expert for maritime ship recycling (IHM - Inventory of Hazardous Materials).

## Task
Estimate the weight in TONNES (metric tons) for EACH row below. These are individual line items from a ship's hazardous materials inventory.

## Items ({len(row_contexts)} rows):
{items_text}

## Estimation Rules (CRITICAL - follow exactly):

### Primary: Use Qty + Unit when available
- kg → divide by 1000 to get tonnes (e.g., 500 kg = 0.5 tonnes)
- L (liters) → use material-appropriate density:
  - Oil/fuel: ~0.85 kg/L → 0.00085 tonnes/L
  - Water-based: ~1.0 kg/L → 0.001 tonnes/L  
  - Paint: ~1.2-1.4 kg/L → 0.0013 tonnes/L
- m³ (cubic meters) → use density based on material type
- pcs/units/items → estimate typical mass per piece:
  - Small electronics (switches, sensors): 0.0001-0.001 tonnes each
  - Batteries (small): 0.001-0.01 tonnes each
  - Large equipment: 0.01-0.1 tonnes each
- m (linear meters) → estimate cross-section × length × density
- m² (area) → estimate thickness × area × density

### Secondary: When Qty/Unit missing or unclear
- Use Material, Item Name, Remarks, Source Text as context
- Estimate conservatively based on typical quantities for that item type on ships
- Most single items: 0.001 - 0.1 tonnes
- Small quantities of hazardous materials: 0.0001 - 0.01 tonnes

### Guardrails (IMPORTANT):
- Most per-row weights should be SMALL (< 0.5 tonnes)
- Values > 1 tonne per row are RARE and need clear justification from Qty
- Maximum allowed per row: {MAX_WEIGHT_PER_ROW_TONNES} tonnes (if calculation exceeds this, cap at {MAX_WEIGHT_PER_ROW_TONNES})
- Minimum: 0.0001 tonnes (even for tiny items)
- NEVER output large values (10+ tonnes) unless Qty explicitly shows bulk quantity

## Response Format (STRICT JSON):
Return ONLY a JSON object. No explanation, no prose.
{{
  "estimates": [
    {{"row_id": 0, "weight": 0.123}},
    {{"row_id": 1, "weight": 0.045}},
    ...
  ]
}}

You MUST return exactly {len(row_contexts)} entries, one per row.
"weight" must be a positive number (float) representing tonnes."""

    return prompt


def estimate_weights_batch_chunk(
    row_contexts: list[dict],
    client: OpenAI,
    model: str = "gpt-5",
    debug: bool = False
) -> tuple[list[dict], list[dict]]:
    """
    Estimate weights for a CHUNK of rows in a single API call.
    
    Args:
        row_contexts: List of row context dicts (from _build_row_context)
        client: OpenAI client instance
        model: Model to use
        debug: Enable detailed logging
        
    Returns:
        Tuple of (results, errors) where:
        - results: List of {"row_id": int, "weight": float, "capped": bool}
        - errors: List of {"row_id": int, "error": str}
    """
    prompt = _build_batch_prompt(row_contexts)
    
    if debug:
        _log_debug("Chunk prompt", {
            'num_rows': len(row_contexts),
            'prompt_length': len(prompt),
            'first_3_contexts': row_contexts[:3]
        })
    
    try:
        print(f"[WEIGHT] LLM call for {len(row_contexts)} rows...")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system", 
                    "content": "You are a precise waste weight estimator. Return ONLY valid JSON with weight estimates in tonnes. Be consistent and deterministic in your estimates."
                },
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            # Note: temperature=0 not supported by gpt-5, relying on structured output for consistency
        )
        
        raw_response = response.choices[0].message.content
        
        if debug:
            _log_debug("LLM raw response", {'response': raw_response[:1000]})
        
        # Parse JSON
        result = json.loads(raw_response)
        estimates = result.get("estimates", [])
        
        if debug:
            _log_debug("Parsed estimates", {'count': len(estimates), 'first_5': estimates[:5]})
        
        # Validate and process results
        results = []
        errors = []
        row_id_set = {ctx['row_id'] for ctx in row_contexts}
        found_ids = set()
        
        for est in estimates:
            row_id = est.get("row_id")
            weight = est.get("weight")
            
            if row_id is None:
                errors.append({"row_id": -1, "error": f"Missing row_id in estimate: {est}"})
                continue
                
            if row_id not in row_id_set:
                errors.append({"row_id": row_id, "error": f"Unexpected row_id {row_id}"})
                continue
            
            found_ids.add(row_id)
            
            # Validate weight
            try:
                weight_float = float(weight)
            except (ValueError, TypeError):
                errors.append({"row_id": row_id, "error": f"Invalid weight value: {weight}"})
                weight_float = 0.001  # Fallback
            
            if weight_float < 0:
                errors.append({"row_id": row_id, "error": f"Negative weight: {weight_float}"})
                weight_float = 0.0001
            
            # Apply cap guardrail
            capped = False
            if weight_float > MAX_WEIGHT_PER_ROW_TONNES:
                errors.append({
                    "row_id": row_id, 
                    "error": f"Weight capped from {weight_float:.3f} to {MAX_WEIGHT_PER_ROW_TONNES} tonnes"
                })
                weight_float = MAX_WEIGHT_PER_ROW_TONNES
                capped = True
            
            results.append({
                "row_id": row_id,
                "weight": round(weight_float, 4),
                "capped": capped
            })
        
        # Check for missing row_ids
        missing_ids = row_id_set - found_ids
        for missing_id in missing_ids:
            errors.append({"row_id": missing_id, "error": "No estimate returned by LLM"})
            # Add conservative fallback for missing
            results.append({
                "row_id": missing_id,
                "weight": 0.001,
                "capped": False
            })
        
        print(f"[WEIGHT] Chunk complete: {len(results)} weights, {len(errors)} errors")
        return results, errors
        
    except json.JSONDecodeError as e:
        error_msg = f"JSON parse error: {str(e)}"
        print(f"[WEIGHT_ERROR] {error_msg}")
        # Return errors for all rows in chunk
        errors = [{"row_id": ctx['row_id'], "error": error_msg} for ctx in row_contexts]
        raise ValueError(f"LLM response was not valid JSON: {error_msg}")
        
    except Exception as e:
        error_msg = f"LLM API error: {type(e).__name__}: {str(e)}"
        print(f"[WEIGHT_ERROR] {error_msg}")
        raise ValueError(error_msg)


def estimate_weights_batch(
    rows: list[dict],
    ewc_column: str,
    client: OpenAI,
    model: str = "gpt-5",
    debug: bool = False,
    progress_callback: Callable[[int], None] = None
) -> tuple[list[dict], list[dict]]:
    """
    Estimate weights for ALL rows using batch LLM calls with chunking.
    
    Uses full row context for accurate per-row estimation.
    Automatically chunks large datasets.
    
    Args:
        rows: List of row dictionaries from CSV
        ewc_column: Name of the EWC code column
        client: OpenAI client instance
        model: Model to use for estimation
        debug: If True, log detailed debug info
        progress_callback: Optional callback for progress updates
        
    Returns:
        Tuple of (results, all_errors) where:
        - results: List of {"row_id": int, "weight": float, "capped": bool}
        - all_errors: List of {"row_id": int, "error": str}
        
    Raises:
        ValueError: If API call fails catastrophically
    """
    total_rows = len(rows)
    print(f"[WEIGHT] Starting batch estimation for {total_rows} rows (chunk size: {CHUNK_SIZE})")
    
    # Build all row contexts
    row_contexts = [_build_row_context(row, i, ewc_column) for i, row in enumerate(rows)]
    
    if debug:
        _log_debug("Row contexts built", {
            'total': len(row_contexts),
            'sample': row_contexts[:2]
        })
    
    # Process in chunks
    all_results = []
    all_errors = []
    num_chunks = (total_rows + CHUNK_SIZE - 1) // CHUNK_SIZE
    
    for chunk_idx in range(num_chunks):
        start_idx = chunk_idx * CHUNK_SIZE
        end_idx = min(start_idx + CHUNK_SIZE, total_rows)
        chunk_contexts = row_contexts[start_idx:end_idx]
        
        print(f"[WEIGHT] Processing chunk {chunk_idx + 1}/{num_chunks} (rows {start_idx}-{end_idx-1})")
        
        chunk_results, chunk_errors = estimate_weights_batch_chunk(
            chunk_contexts, client, model, debug=(debug and chunk_idx == 0)
        )
        
        all_results.extend(chunk_results)
        all_errors.extend(chunk_errors)
        
        # Report progress
        if progress_callback:
            progress = int(((chunk_idx + 1) / num_chunks) * 80) + 10
            progress_callback(progress)
    
    # Sort results by row_id for consistent ordering
    all_results.sort(key=lambda x: x['row_id'])
    
    print(f"[WEIGHT] Batch complete: {len(all_results)} weights, {len(all_errors)} total errors")
    
    if debug:
        _log_debug("Final results summary", {
            'total_weights': len(all_results),
            'total_errors': len(all_errors),
            'capped_count': sum(1 for r in all_results if r.get('capped')),
            'weight_stats': {
                'min': min(r['weight'] for r in all_results) if all_results else 0,
                'max': max(r['weight'] for r in all_results) if all_results else 0,
                'sum': sum(r['weight'] for r in all_results) if all_results else 0
            }
        })
    
    return all_results, all_errors


# ----------------------------
# CSV Utilities
# ----------------------------

def rows_to_csv(rows: list[dict], columns: list[str]) -> str:
    """Convert list of dicts to CSV string."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def parse_csv_text(csv_text: str) -> tuple[list[dict], list[str]]:
    """
    Parse CSV text into rows and column headers.
    
    Returns:
        Tuple of (rows as list of dicts, column names)
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    columns = reader.fieldnames or []
    rows = list(reader)
    return rows, columns


def rows_from_extraction_result(result: dict) -> tuple[list[dict], list[str]]:
    """
    Convert extraction result JSON to rows and columns.
    
    Args:
        result: The 'result' field from an extraction job
        
    Returns:
        Tuple of (rows as list of dicts, column names)
    """
    rows = result.get('rows', [])
    if not rows:
        return [], []
    
    # Get columns from first row
    columns = list(rows[0].keys())
    return rows, columns


# ----------------------------
# Main Estimation Functions
# ----------------------------

def estimate_weights_from_rows(
    rows: list[dict],
    columns: list[str],
    progress_callback: Optional[Callable[[int], None]] = None,
    use_llm: bool = True,
    openai_client: Optional[OpenAI] = None,
    model: str = "gpt-5",
    debug: bool = False,
    max_debug_rows: int = 5
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """
    Estimate weights for all rows and produce aggregated results.
    
    Args:
        rows: List of row dictionaries
        columns: List of column names
        progress_callback: Optional callback(progress_percent) for updates
        use_llm: If True, use LLM for estimation; otherwise use stub
        openai_client: OpenAI client (required if use_llm=True)
        model: Model to use for LLM estimation
        debug: If True, enable detailed logging
        max_debug_rows: Max rows to process in debug mode
        
    Returns:
        Tuple of (processed_rows, aggregated_rows, errors, debug_info)
        - processed_rows: Original rows with 'estimated_weight_tonnes' added
        - aggregated_rows: List of {code, estimated_weight_tonnes_total}
        - errors: List of {row, error} for rows with issues
        - debug_info: Debug information if debug=True
    """
    # Clear debug logs at start
    if debug:
        clear_debug_logs()
        _log_debug("Starting estimation", {
            'total_rows': len(rows),
            'columns': columns,
            'use_llm': use_llm,
            'model': model,
            'has_openai_client': openai_client is not None
        })
    
    # Find EWC code column
    ewc_column = get_ewc_code_column(columns)
    if not ewc_column:
        raise ValueError(
            f'CSV must contain an "EWC Code" column. '
            f'Found columns: {", ".join(columns)}'
        )
    
    if debug:
        _log_debug("Found EWC column", {'column_name': ewc_column})
    
    # Validate LLM setup
    if use_llm and not openai_client:
        error_msg = "use_llm=True but openai_client is None! Cannot proceed."
        print(f"[WEIGHT_ERROR] {error_msg}")
        raise ValueError(error_msg)
    
    # Limit rows in debug mode
    total_rows = len(rows)
    if debug and max_debug_rows:
        total_rows = min(total_rows, max_debug_rows)
        rows = rows[:total_rows]
        _log_debug("Debug mode: limiting rows", {'max_rows': total_rows})
    
    processed_rows = []
    errors = []
    
    # Report progress: starting
    if progress_callback:
        progress_callback(10)
    
    if use_llm and openai_client:
        # ================================================================
        # BATCH LLM ESTIMATION - Per-row with full context, then aggregate
        # ================================================================
        print(f"[WEIGHT] Using batch LLM estimation for {len(rows)} rows...")
        
        # Call LLM with chunking support
        weight_results, batch_errors = estimate_weights_batch(
            rows=rows,
            ewc_column=ewc_column,
            client=openai_client,
            model=model,
            debug=debug,
            progress_callback=progress_callback
        )
        
        # Convert batch errors to our error format
        for err in batch_errors:
            errors.append({
                'row': err['row_id'] + 1,  # 1-indexed for user display
                'error': err['error'],
                'ewc_code': rows[err['row_id']].get(ewc_column, '') if err['row_id'] >= 0 else ''
            })
        
        # Build lookup for weights by row_id
        weight_lookup = {r['row_id']: r['weight'] for r in weight_results}
        
        # Apply weights to rows
        for idx, row in enumerate(rows):
            ewc_code = row.get(ewc_column, '')
            
            # Check for invalid EWC code (add to errors if not already there)
            if not ewc_code or ewc_code in ('', 'N/A', 'Unknown'):
                # Only add if not already in errors from batch
                if not any(e.get('row') == idx + 1 for e in errors):
                    errors.append({
                        'row': idx + 1,
                        'error': f'Missing or invalid EWC code: "{ewc_code}"',
                        'ewc_code': ewc_code
                    })
            
            # Get weight from batch results (default to 0.001 if missing)
            weight = weight_lookup.get(idx, 0.001)
            
            # Add weight to row
            new_row = dict(row)
            new_row['estimated_weight_tonnes'] = round(weight, 4)
            processed_rows.append(new_row)
        
        print(f"[WEIGHT] Applied {len(weight_results)} weights to rows")
        
    else:
        # ================================================================
        # STUB ESTIMATION - Per-row (only if LLM disabled)
        # ================================================================
        print(f"[WEIGHT] Using stub estimation for {len(rows)} rows...")
        
        for idx, row in enumerate(rows):
            ewc_code = row.get(ewc_column, '')
            
            if not ewc_code or ewc_code in ('', 'N/A', 'Unknown'):
                weight = 0.0
                errors.append({
                    'row': idx + 1,
                    'error': f'Missing or invalid EWC code: "{ewc_code}"',
                    'ewc_code': ewc_code
                })
            else:
                weight = estimate_weight_stub(ewc_code, row)
            
            new_row = dict(row)
            new_row['estimated_weight_tonnes'] = weight
            processed_rows.append(new_row)
            
            if progress_callback and idx % 10 == 0:
                progress = 10 + int((idx / total_rows) * 70)
                progress_callback(progress)
    
    # Report progress: processing complete
    if progress_callback:
        progress_callback(90)
    
    # Aggregate by EWC code
    aggregated = {}
    for row in processed_rows:
        code = row.get(ewc_column, 'Unknown')
        weight = row.get('estimated_weight_tonnes', 0.0)
        
        if code not in aggregated:
            aggregated[code] = 0.0
        aggregated[code] += weight
    
    # Round aggregated totals and convert to list
    aggregated_rows = [
        {
            'code': code,
            'estimated_weight_tonnes_total': round(total, 3)
        }
        for code, total in sorted(aggregated.items())
    ]
    
    # Collect debug info
    debug_info = get_debug_logs() if debug else []
    
    if debug:
        _log_debug("Estimation complete", {
            'processed_rows': len(processed_rows),
            'aggregated_codes': len(aggregated_rows),
            'errors': len(errors),
            'total_weight': sum(r['estimated_weight_tonnes_total'] for r in aggregated_rows)
        })
    
    return processed_rows, aggregated_rows, errors, debug_info


def estimate_weights_from_csv(
    csv_text: str,
    progress_callback: Optional[Callable[[int], None]] = None,
    use_llm: bool = True,
    model: str = "gpt-5",
    debug: bool = False
) -> dict:
    """
    Estimate weights from CSV text (for serverless deployment).
    
    Args:
        csv_text: CSV content as string
        progress_callback: Optional callback for progress updates
        use_llm: If True, use LLM for estimation
        model: Model to use for LLM estimation
        debug: If True, enable detailed logging and return debug info
        
    Returns:
        Dictionary with:
        - processed_rows: Rows with weight column added
        - aggregated_rows: Summary by EWC code
        - original_columns: Original column names
        - errors: List of row-level errors
        - stats: Summary statistics
        - debug_info: Debug logs (if debug=True)
    """
    # Initialize OpenAI client if using LLM
    openai_client = None
    if use_llm:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY required for LLM estimation")
        openai_client = OpenAI(api_key=api_key)
        if debug:
            print(f"[WEIGHT_DEBUG] OpenAI client initialized, key prefix: {api_key[:8]}...")
    
    # Parse CSV
    rows, columns = parse_csv_text(csv_text)
    
    if debug:
        print(f"[WEIGHT_DEBUG] Parsed CSV: {len(rows)} rows, columns: {columns}")
    
    if not rows:
        raise ValueError("CSV file is empty")
    
    # Run estimation
    processed_rows, aggregated_rows, errors, debug_info = estimate_weights_from_rows(
        rows=rows,
        columns=columns,
        progress_callback=progress_callback,
        use_llm=use_llm,
        openai_client=openai_client,
        model=model,
        debug=debug
    )
    
    # Calculate stats
    total_weight = sum(r['estimated_weight_tonnes_total'] for r in aggregated_rows)
    
    result = {
        'processed_rows': processed_rows,
        'aggregated_rows': aggregated_rows,
        'original_columns': columns,
        'errors': errors,
        'stats': {
            'total_rows': len(processed_rows),
            'total_codes': len(aggregated_rows),
            'total_weight_tonnes': round(total_weight, 3),
            'errors_count': len(errors)
        }
    }
    
    if debug:
        result['debug_info'] = debug_info
    
    return result


def estimate_weights_from_extraction(
    extraction_result: dict,
    progress_callback: Optional[Callable[[int], None]] = None,
    use_llm: bool = True,
    model: str = "gpt-5",
    debug: bool = False
) -> dict:
    """
    Estimate weights from extraction job result (for serverless deployment).
    
    Args:
        extraction_result: The 'result' field from an extraction job
        progress_callback: Optional callback for progress updates
        use_llm: If True, use LLM for estimation
        model: Model to use for LLM estimation
        debug: If True, enable detailed logging
        
    Returns:
        Same structure as estimate_weights_from_csv
    """
    # Initialize OpenAI client if using LLM
    openai_client = None
    if use_llm:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY required for LLM estimation")
        openai_client = OpenAI(api_key=api_key)
        if debug:
            print(f"[WEIGHT_DEBUG] OpenAI client initialized, key prefix: {api_key[:8]}...")
    
    # Get rows from extraction result
    rows, columns = rows_from_extraction_result(extraction_result)
    
    if debug:
        print(f"[WEIGHT_DEBUG] Extraction result: {len(rows)} rows, columns: {columns}")
        if rows:
            print(f"[WEIGHT_DEBUG] Sample row keys: {list(rows[0].keys())}")
    
    if not rows:
        raise ValueError("Extraction result has no rows")
    
    # Run estimation
    processed_rows, aggregated_rows, errors, debug_info = estimate_weights_from_rows(
        rows=rows,
        columns=columns,
        progress_callback=progress_callback,
        use_llm=use_llm,
        openai_client=openai_client,
        model=model,
        debug=debug
    )
    
    # Calculate stats
    total_weight = sum(r['estimated_weight_tonnes_total'] for r in aggregated_rows)
    
    result = {
        'processed_rows': processed_rows,
        'aggregated_rows': aggregated_rows,
        'original_columns': columns,
        'errors': errors,
        'stats': {
            'total_rows': len(processed_rows),
            'total_codes': len(aggregated_rows),
            'total_weight_tonnes': round(total_weight, 3),
            'errors_count': len(errors)
        }
    }
    
    if debug:
        result['debug_info'] = debug_info
    
    return result


# ----------------------------
# File-Based Functions (for CLI)
# ----------------------------

def estimate_weights_from_file(
    csv_path: Path,
    output_dir: Optional[Path] = None,
    use_llm: bool = True,
    model: str = "gpt-5"
) -> tuple[Path, Path]:
    """
    Estimate weights from a CSV file and save results.
    
    Args:
        csv_path: Path to input CSV
        output_dir: Directory for output files (default: same as input)
        use_llm: If True, use LLM for estimation
        model: Model to use for LLM estimation
        
    Returns:
        Tuple of (full_csv_path, aggregated_csv_path)
    """
    print(f"   Loading CSV: {csv_path.name}")
    
    csv_text = csv_path.read_text(encoding='utf-8')
    
    def progress_cb(pct):
        print(f"   Progress: {pct}%")
    
    print(f"   Estimating weights {'(LLM)' if use_llm else '(stub)'}...")
    result = estimate_weights_from_csv(
        csv_text=csv_text,
        progress_callback=progress_cb,
        use_llm=use_llm,
        model=model
    )
    
    # Prepare output paths
    if output_dir is None:
        output_dir = csv_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    
    stem = csv_path.stem
    full_csv_path = output_dir / f"{stem}_with_weights.csv"
    aggregated_csv_path = output_dir / f"{stem}_aggregated.csv"
    
    # Write full CSV
    full_columns = result['original_columns'] + ['estimated_weight_tonnes']
    full_csv = rows_to_csv(result['processed_rows'], full_columns)
    full_csv_path.write_text(full_csv, encoding='utf-8')
    print(f"   ✅ Wrote {full_csv_path}")
    
    # Write aggregated CSV
    aggregated_csv = rows_to_csv(
        result['aggregated_rows'],
        ['code', 'estimated_weight_tonnes_total']
    )
    aggregated_csv_path.write_text(aggregated_csv, encoding='utf-8')
    print(f"   ✅ Wrote {aggregated_csv_path}")
    
    # Print summary
    stats = result['stats']
    print(f"\n   Summary:")
    print(f"   - Rows processed: {stats['total_rows']}")
    print(f"   - Unique EWC codes: {stats['total_codes']}")
    print(f"   - Total weight: {stats['total_weight_tonnes']} tonnes")
    if stats['errors_count'] > 0:
        print(f"   - Rows with errors: {stats['errors_count']}")
    
    return full_csv_path, aggregated_csv_path


# ----------------------------
# Standalone CLI
# ----------------------------
if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    
    ap = argparse.ArgumentParser(description="Estimate weights for EWC-classified materials")
    ap.add_argument("--csv", required=True, help="Path to input CSV with EWC codes")
    ap.add_argument("--output-dir", default=None, help="Output directory (default: same as input)")
    ap.add_argument("--use-llm", action="store_true", help="Use LLM for estimation (default: stub)")
    ap.add_argument("--model", default="gpt-5", help="OpenAI model for LLM estimation")
    args = ap.parse_args()
    
    csv_path = Path(args.csv).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
    
    if not csv_path.exists():
        raise SystemExit(f"❌ CSV not found: {csv_path}")
    
    estimate_weights_from_file(
        csv_path=csv_path,
        output_dir=output_dir,
        use_llm=args.use_llm,
        model=args.model
    )
