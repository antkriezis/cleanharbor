# CleanHarbor

**CleanHarbor** is an AI-powered toolkit for automating maritime compliance workflows: parsing, classifying, and structuring ship documentation for regulatory reporting.

---

## Overview

CleanHarbor processes **Inventory of Hazardous Materials (IHM)** PDF reports through a two-step pipeline:

1. **Hazmat Extraction** — Extracts hazardous material data from IHM PDFs into structured JSON
2. **EWC Classification** — Classifies each material with European Waste Catalogue (EWC) codes

The tool can be used via **CLI** for local processing or deployed as a **serverless API** on Vercel.

---

## Project Structure

```
cleanharbor/
├── main.py                      # CLI pipeline orchestrator
├── extract_hazmat_from_pdf.py   # Step 1: PDF extraction
├── classify_ewc.py              # Step 2: EWC classification
├── estimate_weights.py          # Step 3: Weight estimation (core logic)
├── api/                         # Vercel serverless functions
│   ├── start-upload.py          # POST /api/start-upload - Job initiation
│   ├── process.py               # POST /api/process - Background processing
│   ├── status.py                # GET /api/status - Job status polling
│   └── weights/                 # Weight estimation endpoints
│       ├── start.py             # POST /api/weights/start - Create weight job
│       ├── process.py           # POST /api/weights/process - Calls estimate_weights
│       └── status.py            # GET /api/weights/status - Job status + download
├── vercel.json                  # Vercel configuration
├── data/                        # Input PDFs (local)
├── outputs/
│   └── JSON Extractions/        # Output JSON files (local)
├── requirements.txt
└── README.md
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

Create a `.env` file in the project root:

```env
OPENAI_API_KEY=your_openai_api_key
SUPABASE_URL=your_supabase_project_url
SUPABASE_ANON_KEY=your_supabase_anon_key
SUPABASE_SERVICE_ROLE_KEY=your_supabase_service_role_key
```

### 3. Supabase setup

The project requires two tables in Supabase:

#### `ewc_codes` table (for EWC classification)

| Column | Type | Description |
|--------|------|-------------|
| `code` | text | 6-digit EWC code |
| `chapter` | int | Chapter number |
| `subchapter` | text | Subchapter code |
| `description` | text | Waste description |
| `hazardous` | bool | Whether the waste is hazardous |
| `entry_type` | text | AN, AH, MH, or MN |
| `priority` | bool | Industry-relevant codes |

#### `jobs` table (for API deployment)

| Column | Type | Description |
|--------|------|-------------|
| `id` | uuid | Job ID (primary key) |
| `status` | text | `processing`, `done`, or `error` |
| `filename` | text | Original PDF filename |
| `model` | text | OpenAI model used |
| `pdf_data` | text | Base64-encoded PDF (cleared after processing) |
| `result` | jsonb | Processing result (when done) |
| `error` | text | Error message (if failed) |
| `created_at` | timestamptz | Job creation timestamp |

---

## Usage

### CLI (Local Processing)

#### Run the full pipeline

```bash
python main.py --pdf "data/MV_EUROFERRY_OLYMPIA_IHM.pdf"
```

With a specific model:

```bash
python main.py --pdf "data/MV_EUROFERRY_OLYMPIA_IHM.pdf" --model "gpt-5"
```

#### Run steps individually

**Step 1: Extract hazmat data from PDF**

```bash
python extract_hazmat_from_pdf.py --pdf "data/MV_EUROFERRY_OLYMPIA_IHM.pdf"
```

**Step 2: Classify with EWC codes**

```bash
python classify_ewc.py --json "outputs/JSON Extractions/MV_EUROFERRY_OLYMPIA_IHM_extract.json"
```

**Step 3: Estimate weights**

```bash
python estimate_weights.py --csv "outputs/materials_with_ewc.csv"
```

With LLM-based estimation:

```bash
python estimate_weights.py --csv "outputs/materials_with_ewc.csv" --use-llm --model gpt-4
```

---

### API (Vercel Deployment)

The API provides asynchronous PDF processing via three endpoints:

#### 1. Upload PDF — `POST /api/start-upload`

Accepts a PDF file and creates a processing job.

**Request:**
```bash
curl -X POST https://your-app.vercel.app/api/start-upload \
  -F "file=@ship_ihm.pdf" \
  -F "model=gpt-5"
```

**Response:**
```json
{
  "success": true,
  "jobId": "550e8400-e29b-41d4-a716-446655440000",
  "message": "Job created. Poll /api/status?id=<jobId> for results."
}
```

#### 2. Check Status — `GET /api/status?id=<jobId>`

Poll this endpoint to check job progress and retrieve results.

**Request:**
```bash
curl https://your-app.vercel.app/api/status?id=550e8400-e29b-41d4-a716-446655440000
```

**Response (processing):**
```json
{
  "success": true,
  "jobId": "550e8400-e29b-41d4-a716-446655440000",
  "status": "processing",
  "filename": "ship_ihm.pdf",
  "created_at": "2025-12-02T10:30:00Z"
}
```

**Response (done):**
```json
{
  "success": true,
  "jobId": "550e8400-e29b-41d4-a716-446655440000",
  "status": "done",
  "filename": "ship_ihm.pdf",
  "result": {
    "success": true,
    "filename": "ship_ihm.pdf",
    "model_used": "gpt-5",
    "document_meta": { ... },
    "rows": [ ... ],
    "total_items": 42
  }
}
```

#### 3. Process Job — `POST /api/process`

Internal endpoint triggered automatically by `start-upload`. Can be called manually to retry failed jobs.

**Request:**
```bash
curl -X POST https://your-app.vercel.app/api/process \
  -H "Content-Type: application/json" \
  -d '{"jobId": "550e8400-e29b-41d4-a716-446655440000"}'
```

---

### Vercel Deployment

1. Connect your repository to Vercel
2. Add environment variables in Vercel dashboard:
   - `OPENAI_API_KEY`
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
3. Deploy

The `vercel.json` configures function resources:

| Endpoint | Memory | Max Duration |
|----------|--------|--------------|
| `/api/start-upload` | 1024 MB | 30s |
| `/api/process` | 3008 MB | 800s |
| `/api/status` | 256 MB | 10s |
| `/api/weights/start` | 512 MB | 30s |
| `/api/weights/process` | 1024 MB | 300s |
| `/api/weights/status` | 256 MB | 10s |

---

## Output

### CLI Output

The pipeline produces JSON files in `outputs/JSON Extractions/`:

- `{filename}_extract_{date}.json` — Raw extraction from PDF
- `{filename}_extract_{date}_ewc.json` — With EWC classifications added

### API Output

The API returns results in the `result` field when job status is `done`.

### Example output row

```json
{
  "chapter": "Part II",
  "section_title": "PART II – OPERATIONALLY GENERATED WASTE",
  "material": "Very Low Sulphur Fuel Oil",
  "item_name": "VLSFO",
  "location": "WT 8 STB",
  "quantity_value": 2.7,
  "quantity_unit": "m3",
  "hazard_flags": ["oil"],
  "remarks": "UNPUMPABLE",
  "page": 13,
  "row_index": 1,
  "source_text": "1 WT 8 STB Very Low Sulphur Fuel Oil 2,7 UNPUMPABLE",
  "ewc_code": "130701",
  "ewc_candidates": ["130703", "130502"]
}
```

---

## EWC Classification

The classification follows the **List of Waste (LoW)** rules from Commission Decision 2000/532/EC:

- **Chapter precedence**: Steps 1-4 for selecting the appropriate chapter
- **Entry types**:
  - **AH** — Absolute Hazardous (always hazardous)
  - **AN** — Absolute Non-Hazardous (never hazardous)
  - **MH** — Mirror Hazardous (hazardous if contains dangerous substances)
  - **MN** — Mirror Non-Hazardous (non-hazardous mirror entry)

Priority codes (industry-relevant) are ranked first during classification.

---

---

## Weight Estimation API

The Weight Estimation module provides a separate pipeline for estimating waste weights based on EWC codes. It uses the same job-based architecture as the extraction pipeline.

### Overview

The weight estimation pipeline:
1. Takes input from a previous extraction job OR a newly uploaded CSV
2. Estimates weight in tonnes for each EWC code
3. Produces two outputs:
   - **Full CSV**: Original data with an added `estimated_weight_tonnes` column
   - **Aggregated CSV**: Two-column summary (`code`, `estimated_weight_tonnes_total`)

### Endpoints

#### 1. Start Weight Estimation — `POST /api/weights/start`

Creates a weight estimation job. Supports two input modes:

**Option A: Reuse Previous Extraction**

```bash
curl -X POST https://your-app.vercel.app/api/weights/start \
  -H "Content-Type: application/json" \
  -d '{
    "input_mode": "reuse_previous",
    "source_job_id": "550e8400-e29b-41d4-a716-446655440000"
  }'
```

**Option B: Upload New CSV**

```bash
curl -X POST https://your-app.vercel.app/api/weights/start \
  -F "file=@materials.csv"
```

The CSV must contain an `EWC Code` column (or `ewc_code`, `code`).

**Response:**
```json
{
  "success": true,
  "jobId": "660e8400-e29b-41d4-a716-446655440001",
  "message": "Weight estimation job created. Poll /api/weights/status?job_id=<jobId> for results."
}
```

#### 2. Check Status — `GET /api/weights/status?job_id=<jobId>`

Poll this endpoint to check job progress and retrieve results.

**Request:**
```bash
curl "https://your-app.vercel.app/api/weights/status?job_id=660e8400-e29b-41d4-a716-446655440001"
```

**Response (processing):**
```json
{
  "success": true,
  "jobId": "660e8400-e29b-41d4-a716-446655440001",
  "job_type": "weights",
  "status": "processing",
  "progress": 45,
  "filename": "ship_ihm.pdf",
  "created_at": "2025-12-02T11:00:00Z"
}
```

**Response (done):**
```json
{
  "success": true,
  "jobId": "660e8400-e29b-41d4-a716-446655440001",
  "job_type": "weights",
  "status": "done",
  "progress": 100,
  "filename": "ship_ihm.pdf",
  "summary": {
    "total_rows": 42,
    "total_codes": 15,
    "total_weight_tonnes": 127.450,
    "errors_count": 2
  },
  "preview": {
    "columns": ["code", "estimated_weight_tonnes_total"],
    "rows": [
      { "code": "130701", "estimated_weight_tonnes_total": 25.500 },
      { "code": "160107", "estimated_weight_tonnes_total": 12.300 }
    ],
    "total_codes": 15,
    "total_weight_tonnes": 127.450
  },
  "download_urls": {
    "full_csv": "https://your-app.vercel.app/api/weights/status?job_id=...&download=full",
    "aggregated_csv": "https://your-app.vercel.app/api/weights/status?job_id=...&download=aggregated"
  },
  "row_errors": [
    { "row": 5, "error": "Missing or invalid EWC code: \"\"" }
  ]
}
```

#### 3. Download Results

**Download full CSV (original + weight column):**
```bash
curl -O "https://your-app.vercel.app/api/weights/status?job_id=<jobId>&download=full"
```

**Download aggregated CSV (code + total weight):**
```bash
curl -O "https://your-app.vercel.app/api/weights/status?job_id=<jobId>&download=aggregated"
```

#### 4. Process Job — `POST /api/weights/process`

Internal endpoint triggered automatically. Can be called manually to retry.

```bash
curl -X POST https://your-app.vercel.app/api/weights/process \
  -H "Content-Type: application/json" \
  -d '{"jobId": "660e8400-e29b-41d4-a716-446655440001"}'
```

### Frontend Integration

Recommended flow:

```javascript
// 1. Start weight estimation (using previous extraction)
const startRes = await fetch('/api/weights/start', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    input_mode: 'reuse_previous',
    source_job_id: extractionJobId
  })
});
const { jobId } = await startRes.json();

// 2. Poll for status
let status = 'processing';
while (status === 'processing') {
  await new Promise(r => setTimeout(r, 2000));
  const statusRes = await fetch(`/api/weights/status?job_id=${jobId}`);
  const data = await statusRes.json();
  status = data.status;
  
  if (status === 'done') {
    // Display preview table
    console.log('Preview:', data.preview.rows);
    // Download URLs available
    console.log('Download:', data.download_urls);
  } else if (status === 'error') {
    console.error('Error:', data.error);
  }
}
```

### Jobs Table Schema Updates

The weight estimation module uses the existing `jobs` table with additional columns.

**Existing columns used:**
| Column | Type | Description |
|--------|------|-------------|
| `id` | uuid | Primary key |
| `status` | text | `processing`, `done`, or `error` |
| `filename` | text | Original filename |
| `result` | jsonb | Processing result when done |
| `error` | text | Error message if failed |
| `created_at` | timestamptz | Job creation timestamp |

**New columns to add:**
| Column | Type | Description |
|--------|------|-------------|
| `job_type` | text | `"extraction"` (default) or `"weights"` |
| `input_mode` | text | `"reuse_previous"` or `"upload_new"` |
| `source_job_id` | uuid | Reference to source extraction job |
| `csv_data` | text | Base64-encoded CSV (cleared after processing) |
| `progress` | int | Processing progress 0-100 |
| `preview_json` | jsonb | First N rows of aggregated table |
| `row_errors` | jsonb | Array of row-level errors |

**Status values:** `processing` → `done` / `error`

**SQL to add columns:**
```sql
ALTER TABLE jobs 
ADD COLUMN IF NOT EXISTS job_type text DEFAULT 'extraction',
ADD COLUMN IF NOT EXISTS input_mode text,
ADD COLUMN IF NOT EXISTS source_job_id uuid REFERENCES jobs(id),
ADD COLUMN IF NOT EXISTS csv_data text,
ADD COLUMN IF NOT EXISTS progress integer DEFAULT 0,
ADD COLUMN IF NOT EXISTS preview_json jsonb,
ADD COLUMN IF NOT EXISTS row_errors jsonb;
```

### Vercel Configuration

| Endpoint | Memory | Max Duration |
|----------|--------|--------------|
| `/api/weights/start` | 512 MB | 30s |
| `/api/weights/process` | 1024 MB | 300s |
| `/api/weights/status` | 256 MB | 10s |

---

## Requirements

- Python 3.10+
- OpenAI API key (GPT-5 recommended)
- Supabase project with `ewc_codes` and `jobs` tables
- Vercel account (for API deployment)
