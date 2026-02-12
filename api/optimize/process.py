"""
CleanHarbor - Optimization Processing Endpoint

POST /api/optimize/process
- Accepts { jobId: string }
- Loads CSV from job data
- Fetches facility data from Supabase
- Runs MIP optimization
- Stores results (shipment plan, truck plan, summary)

This is a long-running function (up to 300 seconds).
"""

import base64
import json
import os
import sys
from http.server import BaseHTTPRequestHandler

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from supabase import create_client, Client
from optimize import run_optimization


def process_optimization_job(job_id: str) -> dict:
    """
    Process an optimization job.
    
    Args:
        job_id: UUID of the optimization job to process
        
    Returns:
        Result dictionary with outputs
    """
    print(f"[OPTIMIZE_PROCESS] Starting job {job_id}")
    
    # Initialize Supabase
    supabase: Client = create_client(
        os.getenv('SUPABASE_URL'),
        os.getenv('SUPABASE_SERVICE_ROLE_KEY')
    )
    
    # Fetch job
    response = supabase.table('jobs').select('*').eq('id', job_id).single().execute()
    job = response.data
    
    if not job:
        raise ValueError(f'Job not found: {job_id}')
    
    if job.get('status') == 'done':
        return job.get('result', {})
    
    job_type = job.get('job_type')
    if job_type != 'optimize':
        raise ValueError(f'Invalid job type: {job_type}. Expected "optimize"')
    
    # Create progress callback that updates Supabase
    def update_progress(pct: int):
        supabase.table('jobs').update({'progress': pct}).eq('id', job_id).execute()
    
    # Update progress: starting
    update_progress(5)
    
    # ========================================================================
    # Load CSV data
    # ========================================================================
    
    csv_data = job.get('csv_data')
    if not csv_data:
        raise ValueError('No CSV data in job')
    
    csv_bytes = base64.b64decode(csv_data)
    csv_text = csv_bytes.decode('utf-8', errors='ignore')
    
    print(f"[OPTIMIZE_PROCESS] Loaded CSV: {len(csv_text)} bytes")
    update_progress(10)
    
    # ========================================================================
    # Run optimization
    # ========================================================================
    
    try:
        result = run_optimization(
            csv_text=csv_text,
            supabase=supabase,
            progress_callback=update_progress
        )
    except ValueError as e:
        # Optimization failed with a clear error
        raise ValueError(str(e))
    
    update_progress(90)
    
    # ========================================================================
    # Store outputs
    # ========================================================================
    
    # Encode CSVs as base64
    shipment_csv_base64 = base64.b64encode(
        result['shipment_csv'].encode('utf-8')
    ).decode('utf-8')
    
    truck_csv_base64 = base64.b64encode(
        result['truck_csv'].encode('utf-8')
    ).decode('utf-8')
    
    # Build preview JSON (first 20 rows of shipment plan + truck plan)
    preview_json = {
        'shipment_preview': result['shipment_plan'][:20],
        'truck_plan': result['truck_plan'],  # Usually small, include all
        'summary': result['summary']
    }
    
    # Build final result
    final_result = {
        'shipment_csv_base64': shipment_csv_base64,
        'truck_csv_base64': truck_csv_base64,
        'summary': result['summary'],
        'shipment_count': len(result['shipment_plan']),
        'facility_count': len(result['truck_plan'])
    }
    
    # Update job with results
    supabase.table('jobs').update({
        'status': 'done',
        'progress': 100,
        'result': final_result,
        'preview_json': preview_json,
        'row_errors': result.get('errors') if result.get('errors') else None,
        'csv_data': None  # Clear to save space
    }).eq('id', job_id).execute()
    
    print(f"[OPTIMIZE_PROCESS] Job {job_id} completed successfully")
    return final_result


class handler(BaseHTTPRequestHandler):
    """Handles optimization processing requests."""
    
    def _send_json(self, status_code: int, data: dict):
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    
    def _error(self, status_code: int, message: str):
        self._send_json(status_code, {'success': False, 'error': message})
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def do_GET(self):
        self._error(405, 'Method not allowed. Use POST.')
    
    def do_POST(self):
        # Validate environment
        required_vars = ['SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY']
        missing = [v for v in required_vars if not os.getenv(v)]
        if missing:
            self._error(500, f'Server configuration error: missing {", ".join(missing)}')
            return
        
        try:
            # Parse request
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self._error(400, 'Empty request body')
                return
            
            body = self.rfile.read(content_length)
            
            try:
                data = json.loads(body.decode('utf-8'))
            except json.JSONDecodeError:
                self._error(400, 'Invalid JSON')
                return
            
            job_id = data.get('jobId') or data.get('job_id')
            if not job_id:
                self._error(400, 'Missing required field: jobId')
                return
            
            # Process the job
            result = process_optimization_job(job_id)
            
            self._send_json(200, {
                'success': True,
                'jobId': job_id,
                'result': result
            })
            
        except ValueError as e:
            # Known error - update job status and return error
            print(f"[OPTIMIZE_ERROR] {str(e)}")
            
            # Try to update job status to error
            try:
                supabase: Client = create_client(
                    os.getenv('SUPABASE_URL'),
                    os.getenv('SUPABASE_SERVICE_ROLE_KEY')
                )
                supabase.table('jobs').update({
                    'status': 'error',
                    'error': str(e)
                }).eq('id', job_id).execute()
            except Exception:
                pass
            
            self._error(400, str(e))
            
        except Exception as e:
            print(f"[OPTIMIZE_ERROR] Unexpected error: {type(e).__name__}: {str(e)}")
            
            # Try to update job status to error
            try:
                supabase: Client = create_client(
                    os.getenv('SUPABASE_URL'),
                    os.getenv('SUPABASE_SERVICE_ROLE_KEY')
                )
                supabase.table('jobs').update({
                    'status': 'error',
                    'error': f'Internal error: {str(e)}'
                }).eq('id', job_id).execute()
            except Exception:
                pass
            
            self._error(500, f'Internal server error: {str(e)}')
