"""
CleanHarbor Vercel Background Function - PDF Upload & Processing

Endpoint: POST /api/upload
Accepts: multipart/form-data with a PDF file under the field "file"
Returns: JSON with extracted hazmat objects and EWC classifications

This is a Background Function that supports long-running operations (up to 1800s).
"""

import json
import os
import sys

# Add project root to path so we can import our modules
# Background functions are in api/upload/, so we need to go up two levels
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


def create_response(status_code: int, body: dict, cors: bool = True) -> dict:
    """
    Create a properly formatted response for Vercel Background Functions.
    
    Args:
        status_code: HTTP status code
        body: Response body dictionary
        cors: Whether to include CORS headers
    
    Returns:
        Response dictionary with statusCode, headers, and body
    """
    headers = {
        'Content-Type': 'application/json'
    }
    
    if cors:
        headers.update({
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type'
        })
    
    return {
        'statusCode': status_code,
        'headers': headers,
        'body': json.dumps(body, ensure_ascii=False)
    }


def create_error(status_code: int, message: str) -> dict:
    """Create an error response."""
    return create_response(status_code, {
        'success': False,
        'error': message
    })


def handler(request):
    """
    Vercel Background Function handler for PDF processing.
    
    Handles POST requests with multipart/form-data containing a PDF file.
    Supports long-running operations up to 1800 seconds.
    
    Args:
        request: Vercel request object with method, headers, and body
    
    Returns:
        Response dictionary with statusCode, headers, and body
    """
    try:
        # Handle CORS preflight
        method = request.method if hasattr(request, 'method') else request.get('method', 'GET')
        
        if method == 'OPTIONS':
            return create_response(200, {})
        
        # Only allow POST
        if method != 'POST':
            return create_error(405, 'Method not allowed. Use POST with multipart/form-data.')
        
        # Get headers (handle both object and dict formats)
        if hasattr(request, 'headers'):
            headers = request.headers
            content_type = headers.get('content-type', '') or headers.get('Content-Type', '')
            content_length = int(headers.get('content-length', 0) or headers.get('Content-Length', 0))
        else:
            headers = request.get('headers', {})
            content_type = headers.get('content-type', '') or headers.get('Content-Type', '')
            content_length = int(headers.get('content-length', 0) or headers.get('Content-Length', 0))
        
        # Validate Content-Type
        if 'multipart/form-data' not in content_type:
            return create_error(400, 'Content-Type must be multipart/form-data')
        
        # Get request body
        if hasattr(request, 'body'):
            body = request.body
            if isinstance(body, str):
                body = body.encode('utf-8')
        else:
            body = request.get('body', b'')
            if isinstance(body, str):
                body = body.encode('utf-8')
        
        if not body:
            return create_error(400, 'Empty request body')
        
        # Parse multipart form data
        try:
            form_data = parse_multipart(content_type, body)
        except Exception as e:
            return create_error(400, f'Failed to parse form data: {str(e)}')
        
        # Check for required file field
        if 'file' not in form_data:
            return create_error(400, "Missing required field 'file'. Please upload a PDF file.")
        
        file_info = form_data['file']
        if not isinstance(file_info, dict) or 'data' not in file_info:
            return create_error(400, "Invalid file upload")
        
        pdf_bytes = file_info['data']
        filename = file_info.get('filename', 'uploaded.pdf')
        
        # Validate it looks like a PDF
        if not pdf_bytes.startswith(b'%PDF'):
            return create_error(400, 'Invalid file format. Please upload a valid PDF file.')
        
        # Get optional model parameter
        model = form_data.get('model', 'gpt-5')
        if isinstance(model, dict):
            model = 'gpt-5'  # Reset if somehow a file was uploaded for model
        
        # Validate environment variables
        required_env_vars = ['OPENAI_API_KEY', 'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY']
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            return create_error(500, 'Server configuration error: Missing environment variables')
        
        # Step 1: Extract hazmat data from PDF
        try:
            extracted_data = extract_from_bytes(pdf_bytes, model=model)
        except ValueError as e:
            return create_error(500, f'Extraction error: {str(e)}')
        except Exception as e:
            return create_error(500, f'Failed to extract data from PDF: {str(e)}')
        
        # Step 2: Classify materials with EWC codes
        try:
            classified_data = classify_materials(extracted_data, model=model)
        except ValueError as e:
            return create_error(500, f'Classification error: {str(e)}')
        except Exception as e:
            return create_error(500, f'Failed to classify materials: {str(e)}')
        
        # Return successful response
        return create_response(200, {
            'success': True,
            'filename': filename,
            'model_used': model,
            'document_meta': classified_data.get('document_meta', {}),
            'rows': classified_data.get('rows', []),
            'total_items': len(classified_data.get('rows', []))
        })
        
    except Exception as e:
        return create_error(500, f'Internal server error: {str(e)}')

