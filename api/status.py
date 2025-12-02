"""
CleanHarbor - Job Status Endpoint

GET /api/status?id=<jobId>
- Returns current job status
- If done, includes the result
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from supabase import create_client, Client


class handler(BaseHTTPRequestHandler):
    """Handles job status queries."""
    
    def _send_json(self, status_code: int, data: dict):
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    
    def _error(self, status_code: int, message: str):
        self._send_json(status_code, {'success': False, 'error': message})
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def do_POST(self):
        self._error(405, 'Method not allowed. Use GET.')
    
    def do_GET(self):
        try:
            # Validate environment
            if not os.getenv('SUPABASE_URL') or not os.getenv('SUPABASE_SERVICE_ROLE_KEY'):
                self._error(500, 'Server configuration error')
                return
            
            # Parse query params
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            
            job_id = params.get('id', [None])[0]
            if not job_id:
                self._error(400, 'Missing required parameter: id')
                return
            
            # Initialize Supabase
            supabase: Client = create_client(
                os.getenv('SUPABASE_URL'),
                os.getenv('SUPABASE_SERVICE_ROLE_KEY')
            )
            
            # Fetch job (excluding pdf_data to reduce response size)
            try:
                response = supabase.table('jobs').select(
                    'id, status, filename, model, result, error, created_at'
                ).eq('id', job_id).single().execute()
                job = response.data
            except Exception as e:
                self._error(404, f'Job not found: {job_id}')
                return
            
            if not job:
                self._error(404, f'Job not found: {job_id}')
                return
            
            # Build response based on status
            response_data = {
                'success': True,
                'jobId': job['id'],
                'status': job['status'],
                'filename': job.get('filename'),
                'created_at': job.get('created_at')
            }
            
            if job['status'] == 'done':
                response_data['result'] = job.get('result')
            elif job['status'] == 'error':
                response_data['error'] = job.get('error')
            
            self._send_json(200, response_data)
            
        except Exception as e:
            self._error(500, f'Internal server error: {str(e)}')

