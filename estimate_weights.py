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


def estimate_weights_batch(
    rows: list[dict],
    ewc_column: str,
    client: OpenAI,
    model: str = "gpt-5",
    debug: bool = False
) -> list[dict]:
    """
    Estimate weights for ALL rows in a SINGLE batch API call.
    
    Args:
        rows: List of row dictionaries
        ewc_column: Name of the EWC code column
        client: OpenAI client instance
        model: Model to use for estimation
        debug: If True, log detailed debug info
        
    Returns:
        List of dicts with {item_index, weight_tonnes} for each row
        
    Raises:
        ValueError: If API call fails
    """
    # Build items list for prompt
    items_list_lines = []
    for i, row in enumerate(rows):
        ewc_code = row.get(ewc_column, '')
        material = row.get('material') or row.get('Material') or row.get('item_name') or ''
        qty = row.get('quantity_value') or row.get('Qty') or ''
        unit = row.get('quantity_unit') or row.get('Unit') or ''
        location = row.get('location') or row.get('Location') or ''
        
        items_list_lines.append(
            f"Item {i}: EWC={ewc_code}, Material={material}, Qty={qty} {unit}, Location={location}"
        )
    
    items_list = "\n".join(items_list_lines)
    
    prompt = f"""You are a waste weight estimation expert for maritime/ship recycling.

Estimate the weight in TONNES (metric tons) for each of the following waste items.

## Items to estimate:
{items_list}

## Rules:
- If quantity is in pieces (pcs), estimate typical weight per piece for this material type
- If quantity is in liters (L) or cubic meters (m3), use appropriate density
- If quantity is in kg, convert to tonnes (divide by 1000)
- If no quantity is given, estimate a typical small amount (0.01-0.1 tonnes)
- Use the EWC code to understand the waste type
- Return positive numbers only. Minimum 0.001 tonnes.

## Response format:
Return ONLY a JSON object with this exact structure:
{{
  "weights": [
    {{"item_index": 0, "weight_tonnes": 1.234}},
    {{"item_index": 1, "weight_tonnes": 0.567}},
    ...
  ]
}}

You MUST return exactly {len(rows)} weight entries, one for each item listed above.
Return ONLY valid JSON, no other text."""

    if debug:
        _log_debug("Batch prompt", {'num_items': len(rows), 'prompt_length': len(prompt)})
    
    try:
        print(f"[WEIGHT] Calling LLM for batch estimation of {len(rows)} items...")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a precise waste weight estimator. Return ONLY valid JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
        )
        
        raw_response = response.choices[0].message.content
        print(f"[WEIGHT] LLM response received, parsing...")
        
        if debug:
            _log_debug("Batch response", {'raw': raw_response[:500]})
        
        # Parse JSON response
        result = json.loads(raw_response)
        weights = result.get("weights", [])
        
        if len(weights) != len(rows):
            print(f"[WEIGHT_WARN] Expected {len(rows)} weights, got {len(weights)}")
        
        # Build results with validation
        weight_results = []
        for i in range(len(rows)):
            # Find weight for this index
            weight_entry = next(
                (w for w in weights if w.get("item_index") == i),
                None
            )
            
            if weight_entry:
                weight = weight_entry.get("weight_tonnes", 0.0)
                try:
                    weight = round(float(weight), 3)
                except (ValueError, TypeError):
                    weight = 0.001
            else:
                # Missing entry - use minimal default
                weight = 0.001
                print(f"[WEIGHT_WARN] Missing weight for item {i}, using 0.001")
            
            weight_results.append({
                "item_index": i,
                "weight_tonnes": weight
            })
        
        print(f"[WEIGHT] Successfully parsed {len(weight_results)} weights")
        return weight_results
        
    except json.JSONDecodeError as e:
        error_msg = f"Failed to parse LLM JSON response: {str(e)}"
        print(f"[WEIGHT_ERROR] {error_msg}")
        raise ValueError(error_msg)
    except Exception as e:
        error_msg = f"LLM batch API call failed: {type(e).__name__}: {str(e)}"
        print(f"[WEIGHT_ERROR] {error_msg}")
        raise ValueError(error_msg)


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
        # BATCH LLM ESTIMATION - Single API call for ALL rows
        # ================================================================
        print(f"[WEIGHT] Using batch LLM estimation for {len(rows)} rows...")
        
        # Call LLM once for all rows
        weight_results = estimate_weights_batch(
            rows=rows,
            ewc_column=ewc_column,
            client=openai_client,
            model=model,
            debug=debug
        )
        
        # Report progress: LLM complete
        if progress_callback:
            progress_callback(80)
        
        # Apply weights to rows
        for idx, row in enumerate(rows):
            ewc_code = row.get(ewc_column, '')
            
            # Check for invalid EWC code
            if not ewc_code or ewc_code in ('', 'N/A', 'Unknown'):
                errors.append({
                    'row': idx + 1,
                    'error': f'Missing or invalid EWC code: "{ewc_code}"',
                    'ewc_code': ewc_code
                })
            
            # Get weight from batch results
            weight_entry = next(
                (w for w in weight_results if w.get("item_index") == idx),
                {"weight_tonnes": 0.001}
            )
            weight = weight_entry.get("weight_tonnes", 0.001)
            
            # Add weight to row
            new_row = dict(row)
            new_row['estimated_weight_tonnes'] = weight
            processed_rows.append(new_row)
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
