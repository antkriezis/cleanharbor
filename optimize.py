#!/usr/bin/env python3
"""
Waste Disposal Optimization Module

Implements MIP optimization for allocating waste codes to facilities,
minimizing total processing + transportation costs.

Model:
- Decision variables:
  - x_{i,f} >= 0: tons of code i sent to facility f
  - n_f integer >= 0: number of trucks to facility f
  
- Constraints:
  1) sum_f x_{i,f} = W_i for each code i (all waste must be allocated)
  2) sum_i x_{i,f} <= Q * n_f for each facility f (truck capacity)
  
- Objective:
  min sum_{i,f} cost_per_ton * x_{i,f} + sum_f transport_cost * n_f

Can be run standalone or imported by api/optimize/process.py
"""

import csv
import io
import json
import os
from typing import Optional, Callable

from supabase import create_client, Client

# Try to import PuLP - will fail gracefully if not available
try:
    import pulp
    PULP_AVAILABLE = True
except ImportError:
    PULP_AVAILABLE = False
    print("[OPTIMIZE] Warning: PuLP not available. Install with: pip install pulp")


# ----------------------------
# Constants
# ----------------------------
TRUCK_CAPACITY_TONNES = 25.0  # Q = 25 tons per truck
SOLVER_TIME_LIMIT_SECONDS = 120  # Max solver time


# ----------------------------
# Data Loading
# ----------------------------

def normalize_code(code_value) -> str:
    """
    Normalize a code value to string, preserving leading zeros.
    NEVER cast to int.
    """
    if code_value is None:
        return ""
    # Convert to string and strip whitespace
    code_str = str(code_value).strip()
    return code_str


def parse_weight(weight_value) -> float:
    """Parse weight value to float, handling various formats."""
    if weight_value is None or weight_value == '':
        return 0.0
    try:
        # Handle comma as decimal separator
        return float(str(weight_value).replace(',', '.'))
    except (ValueError, TypeError):
        return 0.0


def load_supply_from_csv(csv_text: str) -> tuple[list[dict], list[str]]:
    """
    Load and normalize the supply CSV (aggregated weights by code).
    
    Input CSV columns might be:
    - "Code" / "code" / "EWC Code"
    - "Estimated Weight (tonnes)" / "estimated_weight_tonnes_total" / "weight" / "W"
    
    Returns:
        Tuple of (rows, errors) where each row has {'code': str, 'W': float}
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []
    
    # Find code column (case-insensitive)
    code_col = None
    for h in headers:
        h_lower = h.lower().strip()
        if h_lower in ('code', 'ewc code', 'ewc_code'):
            code_col = h
            break
    
    # Find weight column (case-insensitive)
    weight_col = None
    for h in headers:
        h_lower = h.lower().strip()
        if any(kw in h_lower for kw in ['weight', 'tonnes', 'w']):
            weight_col = h
            break
    
    if not code_col:
        raise ValueError(f"CSV must contain a code column. Found: {headers}")
    if not weight_col:
        raise ValueError(f"CSV must contain a weight column. Found: {headers}")
    
    rows = []
    errors = []
    
    for idx, row in enumerate(reader):
        code = normalize_code(row.get(code_col))
        weight = parse_weight(row.get(weight_col))
        
        if not code:
            errors.append({'row': idx + 1, 'error': 'Empty code value'})
            continue
        
        if weight <= 0:
            errors.append({'row': idx + 1, 'error': f'Invalid weight for code {code}: {row.get(weight_col)}'})
            # Still include with 0 weight - optimizer will handle
        
        rows.append({'code': code, 'W': weight})
    
    # Aggregate by code (in case of duplicates)
    aggregated = {}
    for r in rows:
        code = r['code']
        if code not in aggregated:
            aggregated[code] = 0.0
        aggregated[code] += r['W']
    
    supply_rows = [{'code': c, 'W': w} for c, w in aggregated.items()]
    
    print(f"[OPTIMIZE] Loaded supply: {len(supply_rows)} codes, total weight: {sum(r['W'] for r in supply_rows):.3f} tonnes")
    
    return supply_rows, errors


def load_facility_code_mapping(supabase: Client) -> list[dict]:
    """
    Load facility_code_mapping table from Supabase.
    
    Note: cost_per_ton is stored in thousands in DB, so we multiply by 1000.
    
    Returns list of {'code': str, 'facility_id': str, 'cost_per_ton': float}
    """
    response = supabase.table('facility_code_mapping').select(
        'code, facility_id, cost_per_ton'
    ).execute()
    
    rows = []
    for r in response.data:
        # DB stores cost in thousands, convert to actual dollars
        cost_per_ton_raw = parse_weight(r.get('cost_per_ton'))
        cost_per_ton_actual = cost_per_ton_raw * 1000
        
        rows.append({
            'code': normalize_code(r.get('code')),
            'facility_id': str(r.get('facility_id', '')),
            'cost_per_ton': cost_per_ton_actual
        })
    
    print(f"[OPTIMIZE] Loaded facility_code_mapping: {len(rows)} entries")
    return rows


def load_facility_locations(supabase: Client) -> list[dict]:
    """
    Load facility_locations table from Supabase.
    
    Note: transportation_costs is stored in thousands in DB, so we multiply by 1000.
    
    Returns list of {'facility_id': str, 'name': str, 'transportation_costs': float}
    """
    response = supabase.table('facility_locations').select(
        'facility_id, name, transportation_costs'
    ).execute()
    
    rows = []
    for r in response.data:
        # DB stores cost in thousands, convert to actual dollars
        transport_raw = r.get('transportation_costs')
        transport_actual = transport_raw * 1000 if transport_raw is not None else None
        
        rows.append({
            'facility_id': str(r.get('facility_id', '')),
            'name': r.get('name', ''),
            'transportation_costs': transport_actual
        })
    
    print(f"[OPTIMIZE] Loaded facility_locations: {len(rows)} facilities")
    return rows


def prepare_model_data(
    df_supply: list[dict],
    df_proc: list[dict],
    df_truck: list[dict]
) -> tuple[list[dict], list[str]]:
    """
    Join supply, processing costs, and truck costs to create model data.
    
    Returns:
        Tuple of (model_data, errors)
        
        model_data: List of dicts with:
        - code
        - facility_id
        - facility_name
        - cost_per_ton
        - W (weight for this code)
        - transportation_costs (per truck)
        
        errors: List of error strings
    """
    errors = []
    
    # Build lookups
    supply_lookup = {r['code']: r['W'] for r in df_supply}
    truck_lookup = {r['facility_id']: r for r in df_truck}
    
    # Track which codes have at least one facility
    codes_with_facilities = set()
    
    # Join: proc + supply + truck
    model_data = []
    
    for proc_row in df_proc:
        code = proc_row['code']
        facility_id = proc_row['facility_id']
        cost_per_ton = proc_row['cost_per_ton']
        
        # Check if this code is in our supply
        if code not in supply_lookup:
            continue  # Skip codes not in our supply
        
        codes_with_facilities.add(code)
        
        # Get truck info
        truck_info = truck_lookup.get(facility_id, {})
        facility_name = truck_info.get('name', f'Facility {facility_id}')
        transportation_costs = truck_info.get('transportation_costs')
        
        model_data.append({
            'code': code,
            'facility_id': facility_id,
            'facility_name': facility_name,
            'cost_per_ton': cost_per_ton,
            'W': supply_lookup[code],
            'transportation_costs': transportation_costs
        })
    
    # Check for codes without any facilities
    codes_without_facilities = set(supply_lookup.keys()) - codes_with_facilities
    if codes_without_facilities:
        errors.append(f"Codes without facilities: {sorted(codes_without_facilities)}")
    
    # Check for facilities with missing transportation costs
    facilities_used = set(r['facility_id'] for r in model_data)
    facilities_missing_costs = set()
    for r in model_data:
        if r['transportation_costs'] is None:
            facilities_missing_costs.add(r['facility_id'])
    
    if facilities_missing_costs:
        errors.append(f"Facilities missing transportation_costs: {sorted(facilities_missing_costs)}")
    
    print(f"[OPTIMIZE] Model data: {len(model_data)} code-facility pairs")
    
    return model_data, errors


# ----------------------------
# Optimization Solver
# ----------------------------

def solve_optimization(
    model_data: list[dict],
    df_supply: list[dict],
    progress_callback: Optional[Callable[[int], None]] = None
) -> dict:
    """
    Solve the waste allocation optimization problem using PuLP.
    
    Args:
        model_data: List of allowed code-facility pairs with costs
        df_supply: Supply data with total weight per code
        progress_callback: Optional progress callback
        
    Returns:
        Dictionary with solution details
        
    Raises:
        ValueError: If problem is infeasible or solver fails
    """
    if not PULP_AVAILABLE:
        raise ValueError("PuLP is not installed. Cannot run optimization.")
    
    # Extract unique codes and facilities
    codes = list(set(r['code'] for r in model_data))
    facilities = list(set(r['facility_id'] for r in model_data))
    
    supply_lookup = {r['code']: r['W'] for r in df_supply}
    
    # Build cost lookups
    # processing_cost[(code, facility)] = cost per ton
    # transport_cost[facility] = cost per truck
    processing_cost = {}
    transport_cost = {}
    facility_names = {}
    
    for r in model_data:
        code = r['code']
        fid = r['facility_id']
        processing_cost[(code, fid)] = r['cost_per_ton']
        if r['transportation_costs'] is not None:
            transport_cost[fid] = r['transportation_costs']
        facility_names[fid] = r['facility_name']
    
    # Check for missing transport costs
    missing_transport = [f for f in facilities if f not in transport_cost]
    if missing_transport:
        raise ValueError(f"Missing transportation_costs for facilities: {missing_transport}")
    
    # Build set of allowed (code, facility) pairs
    allowed_pairs = set(processing_cost.keys())
    
    print(f"[OPTIMIZE] Building model: {len(codes)} codes, {len(facilities)} facilities, {len(allowed_pairs)} pairs")
    
    if progress_callback:
        progress_callback(40)
    
    # ----------------------------
    # Create PuLP model
    # ----------------------------
    prob = pulp.LpProblem("WasteOptimization", pulp.LpMinimize)
    
    # Decision variables
    # x[(code, facility)] = tons sent
    x = {}
    for (code, fid) in allowed_pairs:
        x[(code, fid)] = pulp.LpVariable(f"x_{code}_{fid}", lowBound=0, cat='Continuous')
    
    # n[facility] = number of trucks (integer)
    n = {}
    for fid in facilities:
        n[fid] = pulp.LpVariable(f"n_{fid}", lowBound=0, cat='Integer')
    
    # ----------------------------
    # Objective function
    # ----------------------------
    # min sum_{i,f} cost_per_ton * x_{i,f} + sum_f transport_cost * n_f
    processing_terms = pulp.lpSum(
        processing_cost[(code, fid)] * x[(code, fid)]
        for (code, fid) in allowed_pairs
    )
    transport_terms = pulp.lpSum(
        transport_cost[fid] * n[fid]
        for fid in facilities
    )
    prob += processing_terms + transport_terms, "TotalCost"
    
    # ----------------------------
    # Constraints
    # ----------------------------
    
    # 1) All waste must be allocated: sum_f x_{i,f} = W_i for each code i
    for code in codes:
        W_i = supply_lookup.get(code, 0)
        code_pairs = [(c, f) for (c, f) in allowed_pairs if c == code]
        if code_pairs:
            prob += (
                pulp.lpSum(x[(c, f)] for (c, f) in code_pairs) == W_i,
                f"Supply_{code}"
            )
    
    # 2) Truck capacity: sum_i x_{i,f} <= Q * n_f for each facility f
    for fid in facilities:
        fac_pairs = [(c, f) for (c, f) in allowed_pairs if f == fid]
        if fac_pairs:
            prob += (
                pulp.lpSum(x[(c, f)] for (c, f) in fac_pairs) <= TRUCK_CAPACITY_TONNES * n[fid],
                f"Capacity_{fid}"
            )
    
    if progress_callback:
        progress_callback(50)
    
    # ----------------------------
    # Solve
    # ----------------------------
    print(f"[OPTIMIZE] Solving MIP problem...")
    
    # Use CBC solver with time limit
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=SOLVER_TIME_LIMIT_SECONDS)
    
    try:
        prob.solve(solver)
    except Exception as e:
        raise ValueError(f"Solver error: {str(e)}")
    
    if progress_callback:
        progress_callback(70)
    
    # Check solution status
    status = pulp.LpStatus[prob.status]
    print(f"[OPTIMIZE] Solver status: {status}")
    
    if prob.status != pulp.LpStatusOptimal:
        if prob.status == pulp.LpStatusInfeasible:
            raise ValueError("Optimization problem is infeasible. Check that all codes have at least one allowed facility.")
        elif prob.status == pulp.LpStatusUnbounded:
            raise ValueError("Optimization problem is unbounded.")
        else:
            raise ValueError(f"Solver did not find optimal solution. Status: {status}")
    
    # ----------------------------
    # Extract solution
    # ----------------------------
    
    # Shipment plan: which code goes where
    shipment_plan = []
    for (code, fid) in allowed_pairs:
        tons = pulp.value(x[(code, fid)])
        if tons and tons > 0.0001:  # Only include non-zero allocations
            proc_cost = tons * processing_cost[(code, fid)]
            shipment_plan.append({
                'code': code,
                'facility_id': fid,
                'facility_name': facility_names.get(fid, fid),
                'tons_sent': round(tons, 4),
                'processing_cost': round(proc_cost, 2)
            })
    
    # Sort by code then facility
    shipment_plan.sort(key=lambda r: (r['code'], r['facility_id']))
    
    # Truck plan: trucks per facility
    truck_plan = []
    for fid in facilities:
        n_trucks = pulp.value(n[fid])
        if n_trucks and n_trucks > 0:
            truck_cost = n_trucks * transport_cost[fid]
            truck_plan.append({
                'facility_id': fid,
                'facility_name': facility_names.get(fid, fid),
                'n_trucks': int(round(n_trucks)),
                'truck_cost': round(truck_cost, 2)
            })
    
    # Sort by facility
    truck_plan.sort(key=lambda r: r['facility_id'])
    
    # Calculate totals
    total_processing_cost = sum(r['processing_cost'] for r in shipment_plan)
    total_transport_cost = sum(r['truck_cost'] for r in truck_plan)
    total_cost = total_processing_cost + total_transport_cost
    facilities_used = len(truck_plan)
    total_trucks = sum(r['n_trucks'] for r in truck_plan)
    total_weight = sum(r['tons_sent'] for r in shipment_plan)
    
    print(f"[OPTIMIZE] Solution found:")
    print(f"  Total processing cost: ${total_processing_cost:.2f}")
    print(f"  Total transport cost: ${total_transport_cost:.2f}")
    print(f"  Total cost: ${total_cost:.2f}")
    print(f"  Facilities used: {facilities_used}")
    print(f"  Total trucks: {total_trucks}")
    print(f"  Total weight allocated: {total_weight:.3f} tonnes")
    
    return {
        'status': 'optimal',
        'shipment_plan': shipment_plan,
        'truck_plan': truck_plan,
        'summary': {
            'total_processing_cost': round(total_processing_cost, 2),
            'total_transport_cost': round(total_transport_cost, 2),
            'total_cost': round(total_cost, 2),
            'facilities_used': facilities_used,
            'total_trucks': total_trucks,
            'total_weight_tonnes': round(total_weight, 3)
        }
    }


# ----------------------------
# CSV Output Utilities
# ----------------------------

def shipment_plan_to_csv(shipment_plan: list[dict]) -> str:
    """Convert shipment plan to CSV string."""
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=['code', 'facility_id', 'facility_name', 'tons_sent', 'processing_cost']
    )
    writer.writeheader()
    writer.writerows(shipment_plan)
    return output.getvalue()


def truck_plan_to_csv(truck_plan: list[dict]) -> str:
    """Convert truck plan to CSV string."""
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=['facility_id', 'facility_name', 'n_trucks', 'truck_cost']
    )
    writer.writeheader()
    writer.writerows(truck_plan)
    return output.getvalue()


# ----------------------------
# Main Optimization Function
# ----------------------------

def run_optimization(
    csv_text: str,
    supabase: Client,
    progress_callback: Optional[Callable[[int], None]] = None
) -> dict:
    """
    Run the full optimization pipeline.
    
    Args:
        csv_text: Input CSV with codes and weights
        supabase: Supabase client for loading facility data
        progress_callback: Optional callback for progress updates
        
    Returns:
        Dictionary with all outputs:
        - shipment_plan: List of allocations
        - truck_plan: List of truck assignments
        - summary: Cost totals and stats
        - shipment_csv: CSV string for shipment plan
        - truck_csv: CSV string for truck plan
        - errors: List of any errors/warnings
        
    Raises:
        ValueError: If optimization fails
    """
    errors = []
    
    # Progress: Starting
    if progress_callback:
        progress_callback(10)
    
    # 1) Load supply data from CSV
    print("[OPTIMIZE] Loading supply data from CSV...")
    df_supply, supply_errors = load_supply_from_csv(csv_text)
    errors.extend(supply_errors)
    
    if not df_supply:
        raise ValueError("No valid supply data in CSV")
    
    if progress_callback:
        progress_callback(20)
    
    # 2) Load facility data from Supabase
    print("[OPTIMIZE] Loading facility data from Supabase...")
    df_proc = load_facility_code_mapping(supabase)
    df_truck = load_facility_locations(supabase)
    
    if not df_proc:
        raise ValueError("No facility_code_mapping data found in Supabase")
    if not df_truck:
        raise ValueError("No facility_locations data found in Supabase")
    
    if progress_callback:
        progress_callback(30)
    
    # 3) Prepare model data (join tables)
    print("[OPTIMIZE] Preparing model data...")
    model_data, prep_errors = prepare_model_data(df_supply, df_proc, df_truck)
    
    # Check for critical errors
    codes_without_facilities = [e for e in prep_errors if "Codes without facilities" in e]
    if codes_without_facilities:
        raise ValueError(codes_without_facilities[0])
    
    missing_costs = [e for e in prep_errors if "missing transportation_costs" in e]
    if missing_costs:
        raise ValueError(missing_costs[0])
    
    errors.extend(prep_errors)
    
    if not model_data:
        raise ValueError("No valid code-facility pairs after joining. Check that codes in CSV exist in facility_code_mapping.")
    
    if progress_callback:
        progress_callback(40)
    
    # 4) Run optimization
    print("[OPTIMIZE] Running optimization solver...")
    solution = solve_optimization(model_data, df_supply, progress_callback)
    
    if progress_callback:
        progress_callback(80)
    
    # 5) Generate output CSVs
    print("[OPTIMIZE] Generating output files...")
    shipment_csv = shipment_plan_to_csv(solution['shipment_plan'])
    truck_csv = truck_plan_to_csv(solution['truck_plan'])
    
    if progress_callback:
        progress_callback(90)
    
    return {
        'shipment_plan': solution['shipment_plan'],
        'truck_plan': solution['truck_plan'],
        'summary': solution['summary'],
        'shipment_csv': shipment_csv,
        'truck_csv': truck_csv,
        'errors': errors
    }


# ----------------------------
# Standalone Testing
# ----------------------------

if __name__ == '__main__':
    import sys
    from pathlib import Path
    
    # Load .env file if available
    try:
        from dotenv import load_dotenv
        load_dotenv()
        print("[OK] Loaded .env file")
    except ImportError:
        print("[WARN] python-dotenv not installed, using system env vars")
    
    # Check for PuLP
    if not PULP_AVAILABLE:
        print("ERROR: PuLP is required. Install with: pip install pulp")
        sys.exit(1)
    print("[OK] PuLP is available")
    
    # Check environment
    if not os.getenv('SUPABASE_URL') or not os.getenv('SUPABASE_SERVICE_ROLE_KEY'):
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
        print("       Create a .env file or set environment variables")
        sys.exit(1)
    print("[OK] Supabase credentials found")
    
    # Get CSV file path from command line or use default
    if len(sys.argv) > 1:
        csv_path = Path(sys.argv[1])
    else:
        # Default to the user's file
        csv_path = Path(__file__).parent / "data" / "weight_aggregated_2026-02-12.csv"
    
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}")
        print(f"Usage: python optimize.py [path/to/weights.csv]")
        sys.exit(1)
    
    print(f"[OK] Reading CSV: {csv_path}")
    csv_text = csv_path.read_text(encoding='utf-8')
    
    # Show first few lines
    lines = csv_text.strip().split('\n')
    print(f"\n--- CSV Preview ({len(lines)} lines) ---")
    for line in lines[:5]:
        print(f"  {line}")
    if len(lines) > 5:
        print(f"  ... ({len(lines) - 5} more rows)")
    print()
    
    # Connect to Supabase
    print("[...] Connecting to Supabase...")
    supabase = create_client(
        os.getenv('SUPABASE_URL'),
        os.getenv('SUPABASE_SERVICE_ROLE_KEY')
    )
    print("[OK] Connected to Supabase")
    
    # Run optimization
    print("\n" + "="*60)
    print("RUNNING OPTIMIZATION")
    print("="*60 + "\n")
    
    try:
        result = run_optimization(csv_text, supabase)
        
        print("\n" + "="*60)
        print("OPTIMIZATION COMPLETE")
        print("="*60)
        
        # Summary
        print("\n--- SUMMARY ---")
        summary = result['summary']
        print(f"  Total Processing Cost: ${summary['total_processing_cost']:,.2f}")
        print(f"  Total Transport Cost:  ${summary['total_transport_cost']:,.2f}")
        print(f"  TOTAL COST:            ${summary['total_cost']:,.2f}")
        print(f"  Facilities Used:       {summary['facilities_used']}")
        print(f"  Total Trucks:          {summary['total_trucks']}")
        print(f"  Total Weight:          {summary['total_weight_tonnes']:.3f} tonnes")
        
        # Shipment Plan
        print(f"\n--- SHIPMENT PLAN ({len(result['shipment_plan'])} allocations) ---")
        for r in result['shipment_plan'][:10]:
            print(f"  {r['code']} -> {r['facility_name']}: {r['tons_sent']:.3f} tonnes (${r['processing_cost']:.2f})")
        if len(result['shipment_plan']) > 10:
            print(f"  ... ({len(result['shipment_plan']) - 10} more allocations)")
        
        # Truck Plan
        print(f"\n--- TRUCK PLAN ({len(result['truck_plan'])} facilities) ---")
        for r in result['truck_plan']:
            print(f"  {r['facility_name']}: {r['n_trucks']} trucks (${r['truck_cost']:.2f})")
        
        # Errors/Warnings
        if result.get('errors'):
            print(f"\n--- WARNINGS ({len(result['errors'])}) ---")
            for err in result['errors']:
                print(f"  - {err}")
        
        # Save outputs
        output_dir = Path(__file__).parent / "outputs"
        output_dir.mkdir(exist_ok=True)
        
        shipment_path = output_dir / "shipment_plan.csv"
        shipment_path.write_text(result['shipment_csv'], encoding='utf-8')
        print(f"\n[SAVED] {shipment_path}")
        
        truck_path = output_dir / "truck_plan.csv"
        truck_path.write_text(result['truck_csv'], encoding='utf-8')
        print(f"[SAVED] {truck_path}")
        
        print("\nDone!")
        
    except ValueError as e:
        print(f"\n[ERROR] Optimization failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] Unexpected error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
