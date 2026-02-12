"""
CleanHarbor - Weight Estimation Processing Endpoint

POST /api/weights/process
- Accepts { jobId: string }
- Fetches CSV from source job or uploaded data
- Estimates weights for each EWC code
- Generates full CSV (with weight column) and aggregated CSV
- Stores results in Supabase

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
from estimate_weights import (
    estimate_weights_from_csv,
    estimate_weights_from_extraction,
    rows_to_csv
)


def process_weight_job(job_id: str, debug_mode: bool = False) -> dict:
    """
    Process a weight estimation job.
    
    Args:
        job_id: UUID of the weight job to process
        debug_mode: If True, enable detailed logging and limit to 5 rows
        
    Returns:
        Result dictionary with outputs and any errors
    """
    print(f"[WEIGHT_PROCESS] Starting job {job_id}, debug_mode={debug_mode}")
    
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
    if job_type != 'weights':
        raise ValueError(f'Invalid job type: {job_type}. Expected "weights"')
    
    input_mode = job.get('input_mode')
    print(f"[WEIGHT_PROCESS] input_mode={input_mode}")
    
    # Create progress callback that updates Supabase
    def update_progress(pct: int):
        # Scale to 20-80% range (loading=0-20%, final steps=80-100%)
        scaled = 20 + int(pct * 0.6)
        supabase.table('jobs').update({'progress': scaled}).eq('id', job_id).execute()
    
    # Update progress: starting
    supabase.table('jobs').update({'progress': 10}).eq('id', job_id).execute()
    
    # ========================================================================
    # Load and process based on input mode
    # ========================================================================
    
    if input_mode == 'reuse_previous':
        # Fetch source job result
        source_job_id = job.get('source_job_id')
        if not source_job_id:
            raise ValueError('Missing source_job_id for reuse_previous mode')
        
        source_response = supabase.table('jobs').select('result').eq('id', source_job_id).single().execute()
        source_job = source_response.data
        
        if not source_job or not source_job.get('result'):
            raise ValueError(f'Source job {source_job_id} has no result')
        
        # Update progress: loaded source
        supabase.table('jobs').update({'progress': 20}).eq('id', job_id).execute()
        
        # Run weight estimation from extraction result
        estimation_result = estimate_weights_from_extraction(
            extraction_result=source_job['result'],
            progress_callback=update_progress,
            use_llm=True,  # Use LLM API for real estimation
            model='gpt-5',
            debug=debug_mode
        )
        
    elif input_mode == 'upload_new':
        # Decode uploaded CSV
        csv_data = job.get('csv_data')
        if not csv_data:
            raise ValueError('No CSV data in job')
        
        csv_bytes = base64.b64decode(csv_data)
        csv_text = csv_bytes.decode('utf-8', errors='ignore')
        
        # Update progress: loaded CSV
        supabase.table('jobs').update({'progress': 20}).eq('id', job_id).execute()
        
        # Run weight estimation from CSV
        estimation_result = estimate_weights_from_csv(
            csv_text=csv_text,
            progress_callback=update_progress,
            use_llm=True,  # Use LLM API for real estimation
            model='gpt-5',
            debug=debug_mode
        )
        
    else:
        raise ValueError(f'Invalid input_mode: {input_mode}')
    
    # Update progress: estimation complete
    supabase.table('jobs').update({'progress': 85}).eq('id', job_id).execute()
    
    # ========================================================================
    # Generate output CSVs
    # ========================================================================
    
    # Full CSV with weight column
    full_columns = estimation_result['original_columns'] + ['estimated_weight_tonnes']
    full_csv = rows_to_csv(estimation_result['processed_rows'], full_columns)
    full_csv_base64 = base64.b64encode(full_csv.encode('utf-8')).decode('utf-8')
    
    # Aggregated CSV (two columns)
    aggregated_csv = rows_to_csv(
        estimation_result['aggregated_rows'],
        ['code', 'estimated_weight_tonnes_total']
    )
    aggregated_csv_base64 = base64.b64encode(aggregated_csv.encode('utf-8')).decode('utf-8')
    
    # ========================================================================
    # Create preview (first 20 rows of aggregated table)
    # ========================================================================
    
    preview_rows = estimation_result['aggregated_rows'][:20]
    preview_json = {
        'columns': ['code', 'estimated_weight_tonnes_total'],
        'rows': preview_rows,
        'total_codes': estimation_result['stats']['total_codes'],
        'total_weight_tonnes': estimation_result['stats']['total_weight_tonnes']
    }
    
    # ========================================================================
    # Build and store result
    # ========================================================================
    
    result = {
        'success': True,
        'filename': job.get('filename', 'weights.csv'),
        'total_rows': estimation_result['stats']['total_rows'],
        'total_codes': estimation_result['stats']['total_codes'],
        'total_weight_tonnes': estimation_result['stats']['total_weight_tonnes'],
        'errors_count': estimation_result['stats']['errors_count'],
        'full_csv_base64': full_csv_base64,
        'aggregated_csv_base64': aggregated_csv_base64,
    }
    
    # Include debug info if available
    debug_info = estimation_result.get('debug_info')
    if debug_info:
        result['debug_info'] = debug_info
        print(f"[WEIGHT_DEBUG] Debug info has {len(debug_info)} entries")
    
    # Log final summary
    print(f"[WEIGHT_PROCESS] Complete: {result['total_rows']} rows, {result['total_codes']} codes, {result['total_weight_tonnes']} tonnes, {result['errors_count']} errors")
    
    # Update job with result
    update_data = {
        'status': 'done',
        'progress': 100,
        'result': result,
        'preview_json': preview_json,
        'row_errors': estimation_result['errors'] if estimation_result['errors'] else None,
        'csv_data': None  # Clear input to save storage
    }
    
    # Store debug info separately if present (for inspection)
    if debug_info:
        # Store first 10 debug entries in a separate field for easy inspection
        update_data['preview_json']['debug_sample'] = debug_info[:10]
    
    supabase.table('jobs').update(update_data).eq('id', job_id).execute()
    
    return result


class handler(BaseHTTPRequestHandler):
    """Handles weight estimation background processing."""
    
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
        try:
            # Validate environment
            required_vars = ['OPENAI_API_KEY', 'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY']
            missing = [v for v in required_vars if not os.getenv(v)]
            if missing:
                self._error(500, 'Server configuration error')
                return
            
            # Read body
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
            
            job_id = data.get('jobId')
            if not job_id:
                self._error(400, 'Missing jobId')
                return
            
            # Check for debug mode
            debug_mode = data.get('debug', False) or os.getenv('DEBUG_WEIGHTS') == '1'
            
            # Process the job
            try:
                result = process_weight_job(job_id, debug_mode=debug_mode)
                response_data = {
                    'success': True,
                    'jobId': job_id,
                    'result': {
                        'total_rows': result.get('total_rows'),
                        'total_codes': result.get('total_codes'),
                        'total_weight_tonnes': result.get('total_weight_tonnes'),
                        'errors_count': result.get('errors_count', 0)
                    }
                }
                # Include debug info in response if present
                if result.get('debug_info'):
                    response_data['debug_info'] = result['debug_info']
                
                self._send_json(200, response_data)
            except ValueError as e:
                # Update job with error
                try:
                    supabase: Client = create_client(
                        os.getenv('SUPABASE_URL'),
                        os.getenv('SUPABASE_SERVICE_ROLE_KEY')
                    )
                    supabase.table('jobs').update({
                        'status': 'error',
                        'error': str(e),
                        'csv_data': None
                    }).eq('id', job_id).execute()
                except:
                    pass
                self._error(400, str(e))
            except Exception as e:
                # Update job with error
                try:
                    supabase: Client = create_client(
                        os.getenv('SUPABASE_URL'),
                        os.getenv('SUPABASE_SERVICE_ROLE_KEY')
                    )
                    supabase.table('jobs').update({
                        'status': 'error',
                        'error': str(e),
                        'csv_data': None
                    }).eq('id', job_id).execute()
                except:
                    pass
                self._error(500, f'Processing failed: {str(e)}')
            
        except Exception as e:
            self._error(500, f'Internal server error: {str(e)}')
