"""
CleanHarbor Vercel Serverless Function - PDF Upload & Processing

Endpoint: POST /api/upload
Accepts: multipart/form-data with a PDF file under the field "file"
Returns: JSON with extracted hazmat objects and EWC classifications
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

# Add project root to path so we can import our modules
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from extract_hazmat_from_pdf import extract_from_bytes
from classify_ewc import classify_materials


def parse_multipart(content_type: str, body: bytes) -> dict:
    """
    Parse multipart/form-data request body.
    
    Returns:
        Dictionary with field names as keys. File fields have dict values with
        'filename', 'content_type', and 'data' keys.
    """
    # Extract boundary from content type
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
        
        # Split headers from content
        if b'\r\n\r\n' in part:
            headers_section, content = part.split(b'\r\n\r\n', 1)
        elif b'\n\n' in part:
            headers_section, content = part.split(b'\n\n', 1)
        else:
            continue
        
        # Remove trailing boundary markers and whitespace
        content = content.rstrip(b'\r\n-')
        
        # Parse headers
        headers = {}
        for line in headers_section.decode('utf-8', errors='ignore').split('\n'):
            line = line.strip('\r')
            if ':' in line:
                key, value = line.split(':', 1)
                headers[key.strip().lower()] = value.strip()
        
        # Get content-disposition
        content_disposition = headers.get('content-disposition', '')
        if 'name=' not in content_disposition:
            continue
        
        # Extract field name
        name_start = content_disposition.find('name="') + 6
        name_end = content_disposition.find('"', name_start)
        field_name = content_disposition[name_start:name_end]
        
        # Check if this is a file field
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


class handler(BaseHTTPRequestHandler):
    """
    Vercel Python Serverless Function handler.
    
    Handles POST requests with multipart/form-data containing a PDF file.
    """
    
    def _send_json_response(self, status_code: int, data: dict):
        """Send a JSON response with proper headers."""
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    
    def _send_error(self, status_code: int, message: str):
        """Send an error response."""
        self._send_json_response(status_code, {
            'success': False,
            'error': message
        })
    
    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def do_GET(self):
        """GET requests are not allowed."""
        self._send_error(405, 'Method not allowed. Use POST with multipart/form-data.')
    
    def do_PUT(self):
        """PUT requests are not allowed."""
        self._send_error(405, 'Method not allowed. Use POST with multipart/form-data.')
    
    def do_DELETE(self):
        """DELETE requests are not allowed."""
        self._send_error(405, 'Method not allowed. Use POST with multipart/form-data.')
    
    def do_POST(self):
        """
        Handle PDF upload and processing.
        
        Expects:
            - Content-Type: multipart/form-data
            - Field: 'file' containing the PDF file
            - Optional field: 'model' (OpenAI model, defaults to 'gpt-5')
        
        Returns:
            JSON with:
            - success: boolean
            - data: extracted and classified hazmat data
            - document_meta: metadata about the processed document
        """
        try:
            # Validate Content-Type
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self._send_error(400, 'Content-Type must be multipart/form-data')
                return
            
            # Read request body
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self._send_error(400, 'Empty request body')
                return
            
            body = self.rfile.read(content_length)
            
            # Parse multipart form data
            try:
                form_data = parse_multipart(content_type, body)
            except Exception as e:
                self._send_error(400, f'Failed to parse form data: {str(e)}')
                return
            
            # Check for required file field
            if 'file' not in form_data:
                self._send_error(400, "Missing required field 'file'. Please upload a PDF file.")
                return
            
            file_info = form_data['file']
            if not isinstance(file_info, dict) or 'data' not in file_info:
                self._send_error(400, "Invalid file upload")
                return
            
            pdf_bytes = file_info['data']
            filename = file_info.get('filename', 'uploaded.pdf')
            
            # Validate it looks like a PDF
            if not pdf_bytes.startswith(b'%PDF'):
                self._send_error(400, 'Invalid file format. Please upload a valid PDF file.')
                return
            
            # Get optional model parameter
            model = form_data.get('model', 'gpt-5')
            if isinstance(model, dict):
                model = 'gpt-5'  # Reset if somehow a file was uploaded for model
            
            # Validate environment variables
            required_env_vars = ['OPENAI_API_KEY', 'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY']
            missing_vars = [var for var in required_env_vars if not os.getenv(var)]
            if missing_vars:
                self._send_error(500, 'Server configuration error: Missing environment variables')
                return
            
            # Step 1: Extract hazmat data from PDF
            try:
                extracted_data = extract_from_bytes(pdf_bytes, model=model)
            except ValueError as e:
                self._send_error(500, f'Extraction error: {str(e)}')
                return
            except Exception as e:
                self._send_error(500, f'Failed to extract data from PDF: {str(e)}')
                return
            
            # Step 2: Classify materials with EWC codes
            try:
                classified_data = classify_materials(extracted_data, model=model)
            except ValueError as e:
                self._send_error(500, f'Classification error: {str(e)}')
                return
            except Exception as e:
                self._send_error(500, f'Failed to classify materials: {str(e)}')
                return
            
            # Return successful response
            self._send_json_response(200, {
                'success': True,
                'filename': filename,
                'model_used': model,
                'document_meta': classified_data.get('document_meta', {}),
                'rows': classified_data.get('rows', []),
                'total_items': len(classified_data.get('rows', []))
            })
            
        except Exception as e:
            self._send_error(500, f'Internal server error: {str(e)}')

