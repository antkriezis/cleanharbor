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

def parse_weight_from_response(response_text: str) -> tuple[float, str]:
    """
    Robustly parse weight from LLM response.
    
    Returns:
        Tuple of (weight, error_message). If successful, error_message is None.
    """
    if not response_text:
        return None, "Empty response from LLM"
    
    text = response_text.strip()
    
    # Try direct float conversion first
    try:
        weight = float(text)
        return round(weight, 3), None
    except ValueError:
        pass
    
    # Try to extract first number from text using regex
    # Matches: 0.5, .5, 12.345, 100, etc.
    match = re.search(r'[\d]*\.?\d+', text)
    if match:
        try:
            weight = float(match.group())
            return round(weight, 3), None
        except ValueError:
            pass
    
    return None, f"Could not parse float from: '{text[:100]}'"


def estimate_weight_llm(ewc_code: str, row_context: dict, client: OpenAI, model: str = "gpt-5", debug: bool = False) -> tuple[float, str]:
    """
    Estimate weight in tonnes using LLM API call.
    
    Uses temperature=0 for deterministic results.
    
    Args:
        ewc_code: The EWC code (e.g., "17 01 01")
        row_context: Additional context from the row
        client: OpenAI client instance
        model: Model to use for estimation
        debug: If True, log detailed debug info
        
    Returns:
        Tuple of (weight, error_message). If successful, error_message is None.
        Returns (None, error) if estimation failed.
    """
    if not ewc_code or ewc_code in ('', 'N/A', 'Unknown', None):
        return 0.0, "Empty or invalid EWC code"
    
    # Build context string - check multiple column name variants
    material = (
        row_context.get('material') or 
        row_context.get('Material') or 
        row_context.get('item_name') or
        row_context.get('Item Name') or 
        ''
    )
    qty = (
        row_context.get('quantity_value') or 
        row_context.get('Qty') or 
        row_context.get('qty') or 
        ''
    )
    unit = (
        row_context.get('quantity_unit') or 
        row_context.get('Unit') or 
        row_context.get('unit') or 
        ''
    )
    location = (
        row_context.get('location') or 
        row_context.get('Location') or 
        ''
    )
    
    # Log inputs if debug mode
    if debug:
        _log_debug("LLM Input", {
            'ewc_code': ewc_code,
            'material': material,
            'qty': qty,
            'unit': unit,
            'location': location,
            'raw_row_keys': list(row_context.keys())
        })
    
    # Check for missing critical inputs
    input_warnings = []
    if not material:
        input_warnings.append("material is empty")
    if not qty:
        input_warnings.append("quantity is empty")
    if not unit:
        input_warnings.append("unit is empty")
    
    prompt = f"""You are a waste weight estimation expert for maritime/ship recycling.

Estimate the weight in TONNES (metric tons) for the following waste item.
Return ONLY a single decimal number (e.g., 0.250 or 12.500). No text, no units, no explanation - just the number.

EWC Code: {ewc_code}
Material: {material if material else '(not specified)'}
Quantity: {qty if qty else '(not specified)'} {unit if unit else ''}
Location: {location if location else '(not specified)'}

Rules:
- If quantity is given in pieces (pcs), estimate typical weight per piece for this material type.
- If quantity is in liters (L), use appropriate density for the material.
- If quantity is in cubic meters (m3), use appropriate density.
- If quantity is in kg, convert to tonnes (divide by 1000).
- If no quantity is given, estimate a typical amount for this waste type on a ship.
- Always return a positive number. If truly zero, return 0.001.

Return ONLY a decimal number (e.g., 2.500):"""

    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0,  # Deterministic
            messages=[
                {"role": "system", "content": "You are a precise waste weight estimator. You must respond with ONLY a single decimal number representing tonnes. No text, no units, no explanation."},
                {"role": "user", "content": prompt}
            ]
        )
        
        raw_response = response.choices[0].message.content
        
        if debug:
            _log_debug("LLM Response", {
                'raw': raw_response,
                'model': model
            })
        
        # Parse the response
        weight, parse_error = parse_weight_from_response(raw_response)
        
        if parse_error:
            if debug:
                _log_debug("Parse Error", {'error': parse_error, 'raw': raw_response})
            return None, f"Parse failed: {parse_error}"
        
        if debug:
            _log_debug("Parsed Weight", {'weight': weight, 'input_warnings': input_warnings})
        
        return weight, None
    
    except Exception as e:
        error_msg = f"LLM API call failed: {type(e).__name__}: {str(e)}"
        if debug:
            _log_debug("LLM Error", {'error': error_msg})
        return None, error_msg


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
        error_msg = "use_llm=True but openai_client is None! Falling back to stub."
        print(f"[WEIGHT_ERROR] {error_msg}")
        if debug:
            _log_debug("LLM Setup Error", {'error': error_msg})
        use_llm = False  # Force fallback
    
    # Process each row
    total_rows = len(rows)
    if debug and max_debug_rows:
        total_rows = min(total_rows, max_debug_rows)
        rows = rows[:total_rows]
        _log_debug("Debug mode: limiting rows", {'max_rows': total_rows})
    
    processed_rows = []
    errors = []
    
    for idx, row in enumerate(rows):
        ewc_code = row.get(ewc_column, '')
        
        # Pass the entire row as context (let the estimation function pick what it needs)
        row_context = dict(row)
        
        # Log first few rows in debug mode
        if debug and idx < 5:
            _log_debug(f"Processing row {idx+1}", {
                'ewc_code': ewc_code,
                'row_keys': list(row.keys()),
                'material': row.get('material') or row.get('Material'),
                'quantity_value': row.get('quantity_value') or row.get('Qty'),
                'quantity_unit': row.get('quantity_unit') or row.get('Unit'),
            })
        
        # Estimate weight
        weight = None
        error_msg = None
        
        if not ewc_code or ewc_code in ('', 'N/A', 'Unknown'):
            weight = 0.0
            error_msg = f'Missing or invalid EWC code: "{ewc_code}"'
        elif use_llm and openai_client:
            # Use LLM estimation
            weight, error_msg = estimate_weight_llm(
                ewc_code, row_context, openai_client, model, 
                debug=(debug and idx < 5)  # Only debug first 5
            )
            if error_msg:
                # LLM failed - use stub as fallback but record the error
                print(f"[WEIGHT_WARN] Row {idx+1}: LLM failed ({error_msg}), using stub")
                weight = estimate_weight_stub(ewc_code, row_context)
                errors.append({
                    'row': idx + 1,
                    'error': f'LLM failed, used stub: {error_msg}',
                    'ewc_code': ewc_code,
                    'fallback_weight': weight
                })
                error_msg = None  # Clear since we have a fallback
        else:
            # Use stub estimation
            weight = estimate_weight_stub(ewc_code, row_context)
        
        # Record any remaining errors
        if error_msg:
            errors.append({
                'row': idx + 1,
                'error': error_msg,
                'ewc_code': ewc_code
            })
        
        # Ensure weight is a number (never None in output)
        if weight is None:
            weight = 0.0
            if not error_msg:  # Don't double-record
                errors.append({
                    'row': idx + 1,
                    'error': 'Weight estimation returned None',
                    'ewc_code': ewc_code
                })
        
        # Add weight to row
        new_row = dict(row)
        new_row['estimated_weight_tonnes'] = weight
        processed_rows.append(new_row)
        
        # Report progress
        if progress_callback and idx % 10 == 0:
            progress = int((idx / total_rows) * 100)
            progress_callback(progress)
    
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
