"""
CleanHarbor - Optimization Job Initiation

POST /api/optimize/start
- Creates an optimization job in Supabase
- Accepts two input modes:
  - "reuse_previous": Use aggregated CSV from a prior weight estimation job
  - "upload_new": Upload a new CSV file with codes and weights
- Returns jobId for status polling
"""

import base64
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
import urllib.request
import urllib.error

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from supabase import create_client, Client


def trigger_background_process(host: str, job_id: str):
    """
    Fire-and-forget HTTP call to trigger background processing.
    Uses a short timeout - we just need to initiate the request.
    """
    try:
        url = f"https://{host}/api/optimize/process"
        data = json.dumps({'jobId': job_id}).encode('utf-8')
        
        req = urllib.request.Request(
            url,
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        
        # Very short timeout - just enough to send the request
        urllib.request.urlopen(req, timeout=2)
    except urllib.error.URLError:
        # Expected - we don't wait for the response
        pass
    except Exception:
        # Any error is fine - the request was likely sent
        pass


def parse_multipart(content_type: str, body: bytes) -> dict:
    """Parse multipart/form-data request body."""
    if 'boundary=' not in content_type:
        raise ValueError("Missing boundary in Content-Type header")
    
    boundary = content_type.split('boundary=')[1].strip()
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]
    
    boundary_bytes = f'--{boundary}'.encode()
    
    result = {}
    parts = body.split(boundary_bytes)
    
    for part in parts:
        if not part or part.strip() in (b'', b'--', b'--\r\n'):
            continue
        
        if b'\r\n\r\n' in part:
            headers_section, content = part.split(b'\r\n\r\n', 1)
        elif b'\n\n' in part:
            headers_section, content = part.split(b'\n\n', 1)
        else:
            continue
        
        content = content.rstrip(b'\r\n-')
        
        headers = {}
        for line in headers_section.decode('utf-8', errors='ignore').split('\n'):
            line = line.strip('\r')
            if ':' in line:
                key, value = line.split(':', 1)
                headers[key.strip().lower()] = value.strip()
        
        content_disposition = headers.get('content-disposition', '')
        if 'name=' not in content_disposition:
            continue
        
        name_start = content_disposition.find('name="') + 6
        name_end = content_disposition.find('"', name_start)
        field_name = content_disposition[name_start:name_end]
        
        if 'filename="' in content_disposition:
            filename_start = content_disposition.find('filename="') + 10
            filename_end = content_disposition.find('"', filename_start)
            filename = content_disposition[filename_start:filename_end]
            
            result[field_name] = {
                'filename': filename,
                'content_type': headers.get('content-type', 'text/csv'),
                'data': content
            }
        else:
            result[field_name] = content.decode('utf-8', errors='ignore')
    
    return result


class handler(BaseHTTPRequestHandler):
    """Handles optimization job creation."""
    
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
            if not os.getenv('SUPABASE_URL') or not os.getenv('SUPABASE_SERVICE_ROLE_KEY'):
                self._error(500, 'Server configuration error')
                return
            
            content_type = self.headers.get('Content-Type', '')
            content_length = int(self.headers.get('Content-Length', 0))
            
            if content_length == 0:
                self._error(400, 'Empty request body')
                return
            
            body = self.rfile.read(content_length)
            
            # Initialize Supabase
            supabase: Client = create_client(
                os.getenv('SUPABASE_URL'),
                os.getenv('SUPABASE_SERVICE_ROLE_KEY')
            )
            
            # Handle JSON payload or multipart
            if 'application/json' in content_type:
                # JSON mode - either reuse_previous or upload_new without file
                try:
                    data = json.loads(body.decode('utf-8'))
                except json.JSONDecodeError:
                    self._error(400, 'Invalid JSON')
                    return
                
                input_mode = data.get('input_mode')
                if input_mode not in ('reuse_previous', 'upload_new'):
                    self._error(400, 'input_mode must be "reuse_previous" or "upload_new"')
                    return
                
                if input_mode == 'reuse_previous':
                    source_job_id = data.get('source_job_id')
                    if not source_job_id:
                        self._error(400, 'source_job_id is required when input_mode is "reuse_previous"')
                        return
                    
                    # Verify source job exists and is complete (should be a weights job)
                    try:
                        source_response = supabase.table('jobs').select(
                            'id, job_type, status, result'
                        ).eq('id', source_job_id).single().execute()
                        source_job = source_response.data
                    except Exception:
                        self._error(404, f'Source job not found: {source_job_id}')
                        return
                    
                    if not source_job:
                        self._error(404, f'Source job not found: {source_job_id}')
                        return
                    
                    if source_job['status'] != 'done':
                        self._error(400, f'Source job is not complete. Status: {source_job["status"]}')
                        return
                    
                    # Get the aggregated CSV from the weights job result
                    result = source_job.get('result', {})
                    aggregated_csv_base64 = result.get('aggregated_csv_base64')
                    
                    if not aggregated_csv_base64:
                        self._error(400, 'Source job has no aggregated CSV data')
                        return
                    
                    # Create optimization job referencing source
                    job_id = str(uuid.uuid4())
                    job_data = {
                        'id': job_id,
                        'job_type': 'optimize',
                        'status': 'processing',
                        'input_mode': 'reuse_previous',
                        'source_job_id': source_job_id,
                        'csv_data': aggregated_csv_base64,  # Store the CSV for processing
                        'filename': 'aggregated_weights.csv',
                        'result': None,
                        'preview_json': None,
                        'row_errors': None,
                        'progress': 0,
                        'created_at': datetime.now(timezone.utc).isoformat(),
                        'error': None
                    }
                
                else:  # upload_new with JSON
                    self._error(400, 'For upload_new mode, use multipart/form-data with the CSV file')
                    return
            
            elif 'multipart/form-data' in content_type:
                # Multipart mode - uploading a new CSV file
                try:
                    form_data = parse_multipart(content_type, body)
                except Exception as e:
                    self._error(400, f'Failed to parse form data: {str(e)}')
                    return
                
                # Validate file
                if 'file' not in form_data:
                    self._error(400, "Missing required field 'file'")
                    return
                
                file_info = form_data['file']
                if not isinstance(file_info, dict) or 'data' not in file_info:
                    self._error(400, "Invalid file upload")
                    return
                
                csv_bytes = file_info['data']
                filename = file_info.get('filename', 'uploaded.csv')
                
                # Basic CSV validation - check for code and weight columns
                try:
                    csv_preview = csv_bytes[:2000].decode('utf-8', errors='ignore').lower()
                    has_code = 'code' in csv_preview
                    has_weight = 'weight' in csv_preview or 'tonnes' in csv_preview
                    
                    if not has_code:
                        self._error(400, 'CSV must contain a "code" column')
                        return
                    if not has_weight:
                        self._error(400, 'CSV must contain a weight column (e.g., "estimated_weight_tonnes_total")')
                        return
                except Exception:
                    self._error(400, 'Invalid CSV file: cannot decode as UTF-8')
                    return
                
                # Create optimization job with uploaded CSV
                job_id = str(uuid.uuid4())
                csv_base64 = base64.b64encode(csv_bytes).decode('utf-8')
                
                job_data = {
                    'id': job_id,
                    'job_type': 'optimize',
                    'status': 'processing',
                    'input_mode': 'upload_new',
                    'source_job_id': None,
                    'csv_data': csv_base64,
                    'filename': filename,
                    'result': None,
                    'preview_json': None,
                    'row_errors': None,
                    'progress': 0,
                    'created_at': datetime.now(timezone.utc).isoformat(),
                    'error': None
                }
            
            else:
                self._error(400, 'Content-Type must be application/json or multipart/form-data')
                return
            
            # Insert job into Supabase
            try:
                supabase.table('jobs').insert(job_data).execute()
            except Exception as e:
                self._error(500, f'Failed to create job: {str(e)}')
                return
            
            # Trigger background processing
            host = self.headers.get('Host', '')
            if host:
                trigger_background_process(host, job_id)
            
            # Return job ID immediately
            self._send_json(200, {
                'success': True,
                'jobId': job_id,
                'message': 'Optimization job created. Poll /api/optimize/status?job_id=<jobId> for results.'
            })
            
        except Exception as e:
            self._error(500, f'Internal server error: {str(e)}')
