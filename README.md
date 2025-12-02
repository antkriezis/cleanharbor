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
├── api/                         # Vercel serverless functions
│   ├── start-upload.py          # POST /api/start-upload - Job initiation
│   ├── process.py               # POST /api/process - Background processing
│   └── status.py                # GET /api/status - Job status polling
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

## Requirements

- Python 3.10+
- OpenAI API key (GPT-5 recommended)
- Supabase project with `ewc_codes` and `jobs` tables
- Vercel account (for API deployment)
