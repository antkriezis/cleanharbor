"""
CleanHarbor - Weight Estimation Status Endpoint

GET /api/weights/status?job_id=<jobId>
- Returns current job status and progress
- If complete, includes preview data and downloadable CSV URLs
- Preview shows first N rows of aggregated weight table
"""

import base64
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from supabase import create_client, Client


class handler(BaseHTTPRequestHandler):
    """Handles weight estimation job status queries."""
    
    def _send_json(self, status_code: int, data: dict):
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    
    def _send_csv(self, filename: str, csv_data: bytes):
        """Send CSV file as download."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/csv')
        self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(csv_data)
    
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
            
            # Support both 'job_id' and 'id' parameter names for flexibility
            job_id = params.get('job_id', [None])[0] or params.get('id', [None])[0]
            if not job_id:
                self._error(400, 'Missing required parameter: job_id')
                return
            
            # Check for download parameter
            download = params.get('download', [None])[0]
            
            # Initialize Supabase
            supabase: Client = create_client(
                os.getenv('SUPABASE_URL'),
                os.getenv('SUPABASE_SERVICE_ROLE_KEY')
            )
            
            # Fetch job (excluding csv_data to reduce response size for status checks)
            try:
                response = supabase.table('jobs').select(
                    'id, job_type, status, progress, filename, result, preview_json, row_errors, error, created_at'
                ).eq('id', job_id).single().execute()
                job = response.data
            except Exception as e:
                self._error(404, f'Job not found: {job_id}')
                return
            
            if not job:
                self._error(404, f'Job not found: {job_id}')
                return
            
            # Verify job type
            if job.get('job_type') != 'weights':
                self._error(400, f'Job {job_id} is not a weight estimation job')
                return
            
            # Handle download requests
            if download and job['status'] == 'done':
                result = job.get('result', {})
                
                if download == 'full':
                    # Download full CSV with weights
                    csv_base64 = result.get('full_csv_base64')
                    if csv_base64:
                        csv_bytes = base64.b64decode(csv_base64)
                        filename = job.get('filename', 'weights')
                        if not filename.endswith('.csv'):
                            filename = filename.rsplit('.', 1)[0] if '.' in filename else filename
                        self._send_csv(f'{filename}_with_weights.csv', csv_bytes)
                        return
                    else:
                        self._error(404, 'Full CSV not available')
                        return
                
                elif download == 'aggregated':
                    # Download aggregated CSV
                    csv_base64 = result.get('aggregated_csv_base64')
                    if csv_base64:
                        csv_bytes = base64.b64decode(csv_base64)
                        filename = job.get('filename', 'weights')
                        if not filename.endswith('.csv'):
                            filename = filename.rsplit('.', 1)[0] if '.' in filename else filename
                        self._send_csv(f'{filename}_aggregated.csv', csv_bytes)
                        return
                    else:
                        self._error(404, 'Aggregated CSV not available')
                        return
                
                else:
                    self._error(400, f'Invalid download type: {download}. Use "full" or "aggregated".')
                    return
            
            # Build status response
            response_data = {
                'success': True,
                'jobId': job['id'],
                'job_type': 'weights',
                'status': job['status'],
                'progress': job.get('progress', 0),
                'filename': job.get('filename'),
                'created_at': job.get('created_at')
            }
            
            if job['status'] == 'done':
                result = job.get('result', {})
                
                # Include summary statistics
                response_data['summary'] = {
                    'total_rows': result.get('total_rows'),
                    'total_codes': result.get('total_codes'),
                    'total_weight_tonnes': result.get('total_weight_tonnes'),
                    'errors_count': result.get('errors_count', 0)
                }
                
                # Include preview data (aggregated table first N rows)
                preview = job.get('preview_json')
                if preview:
                    response_data['preview'] = preview
                
                # Include download URLs (construct from current host)
                host = self.headers.get('Host', '')
                if host:
                    base_url = f"https://{host}/api/weights/status?job_id={job_id}"
                    response_data['download_urls'] = {
                        'full_csv': f"{base_url}&download=full",
                        'aggregated_csv': f"{base_url}&download=aggregated"
                    }
                
                # Include row-level errors if any
                row_errors = job.get('row_errors')
                if row_errors:
                    response_data['row_errors'] = row_errors
            
            elif job['status'] == 'error':
                response_data['error'] = job.get('error')
            
            elif job['status'] == 'processing':
                # Include any partial errors accumulated so far
                row_errors = job.get('row_errors')
                if row_errors:
                    response_data['partial_errors'] = row_errors
            
            self._send_json(200, response_data)
            
        except Exception as e:
            self._error(500, f'Internal server error: {str(e)}')
