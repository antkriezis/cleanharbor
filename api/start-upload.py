"""
CleanHarbor - Job Initiation Endpoint

POST /api/start-upload
- Accepts multipart/form-data with PDF file
- Creates a job record in Supabase
- Triggers background processing
- Returns jobId immediately
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
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from supabase import create_client, Client


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
                'content_type': headers.get('content-type', 'application/octet-stream'),
                'data': content
            }
        else:
            result[field_name] = content.decode('utf-8', errors='ignore')
    
    return result


def trigger_background_process(host: str, job_id: str):
    """
    Fire-and-forget HTTP call to trigger background processing.
    Uses a short timeout - we just need to initiate the request.
    """
    try:
        url = f"https://{host}/api/process"
        data = json.dumps({'jobId': job_id}).encode('utf-8')
        
        req = urllib.request.Request(
            url,
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        
        # Very short timeout - just enough to send the request
        # The process endpoint will continue running independently
        urllib.request.urlopen(req, timeout=2)
    except urllib.error.URLError:
        # Expected - we don't wait for the response
        pass
    except Exception:
        # Any error is fine - the request was likely sent
        pass


class handler(BaseHTTPRequestHandler):
    """Handles PDF upload and job creation."""
    
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
            # Validate Content-Type
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self._error(400, 'Content-Type must be multipart/form-data')
                return
            
            # Read body
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self._error(400, 'Empty request body')
                return
            
            body = self.rfile.read(content_length)
            
            # Parse form data
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
            
            pdf_bytes = file_info['data']
            filename = file_info.get('filename', 'uploaded.pdf')
            
            if not pdf_bytes.startswith(b'%PDF'):
                self._error(400, 'Invalid PDF file')
                return
            
            # Get optional model
            model = form_data.get('model', 'gpt-5')
            if isinstance(model, dict):
                model = 'gpt-5'
            
            # Validate environment
            if not os.getenv('SUPABASE_URL') or not os.getenv('SUPABASE_SERVICE_ROLE_KEY'):
                self._error(500, 'Server configuration error')
                return
            
            # Initialize Supabase
            supabase: Client = create_client(
                os.getenv('SUPABASE_URL'),
                os.getenv('SUPABASE_SERVICE_ROLE_KEY')
            )
            
            # Create job record
            job_id = str(uuid.uuid4())
            pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
            
            job_data = {
                'id': job_id,
                'status': 'processing',
                'filename': filename,
                'model': model,
                'pdf_data': pdf_base64,
                'result': None,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'error': None
            }
            
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
                'message': 'Job created. Poll /api/status?id=<jobId> for results.'
            })
            
        except Exception as e:
            self._error(500, f'Internal server error: {str(e)}')

