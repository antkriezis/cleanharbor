"""
CleanHarbor - Background Processing Endpoint

POST /api/process
- Accepts { jobId: string }
- Fetches PDF from Supabase jobs table
- Runs extraction + classification
- Updates job with result

This is a long-running function (up to 800 seconds).
"""

import base64
import json
import os
import sys
from http.server import BaseHTTPRequestHandler

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from supabase import create_client, Client
from extract_hazmat_from_pdf import extract_from_bytes
from classify_ewc import classify_materials


def process_job(job_id: str) -> dict:
    """
    Process a job: extract hazmat from PDF and classify with EWC codes.
    
    Args:
        job_id: UUID of the job to process
        
    Returns:
        Result dictionary with extracted and classified data
    """
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
    
    if job['status'] == 'done':
        return job['result']
    
    # Decode PDF
    pdf_base64 = job.get('pdf_data')
    if not pdf_base64:
        raise ValueError('No PDF data in job')
    
    pdf_bytes = base64.b64decode(pdf_base64)
    model = job.get('model', 'gpt-5')
    filename = job.get('filename', 'uploaded.pdf')
    
    # Step 1: Extract hazmat data
    extracted_data = extract_from_bytes(pdf_bytes, model=model)
    
    # Step 2: Classify with EWC codes
    classified_data = classify_materials(extracted_data, model=model)
    
    # Build result
    result = {
        'success': True,
        'filename': filename,
        'model_used': model,
        'document_meta': classified_data.get('document_meta', {}),
        'rows': classified_data.get('rows', []),
        'total_items': len(classified_data.get('rows', []))
    }
    
    # Update job with result and clear PDF data to save space
    supabase.table('jobs').update({
        'status': 'done',
        'result': result,
        'pdf_data': None  # Clear PDF to save storage
    }).eq('id', job_id).execute()
    
    return result


class handler(BaseHTTPRequestHandler):
    """Handles background job processing."""
    
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
            
            # Process the job
            try:
                result = process_job(job_id)
                self._send_json(200, {
                    'success': True,
                    'jobId': job_id,
                    'result': result
                })
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
                        'pdf_data': None
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
                        'pdf_data': None
                    }).eq('id', job_id).execute()
                except:
                    pass
                self._error(500, f'Processing failed: {str(e)}')
            
        except Exception as e:
            self._error(500, f'Internal server error: {str(e)}')

