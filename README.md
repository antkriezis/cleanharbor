# CleanHarbor

**CleanHarbor** is an AI-powered toolkit for automating maritime compliance workflows: parsing, classifying, and structuring ship documentation for regulatory reporting.

---

## Overview

CleanHarbor processes **Inventory of Hazardous Materials (IHM)** PDF reports through a two-step pipeline:

1. **Hazmat Extraction** — Extracts hazardous material data from IHM PDFs into structured JSON
2. **EWC Classification** — Classifies each material with European Waste Catalogue (EWC) codes

---

## Project Structure

```
cleanharbor/
├── main.py                      # Main pipeline orchestrator
├── extract_hazmat_from_pdf.py   # Step 1: PDF extraction
├── classify_ewc.py              # Step 2: EWC classification
├── data/                        # Input PDFs
├── outputs/
│   └── JSON Extractions/        # Output JSON files
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

The EWC classification step requires an `ewc_codes` table in Supabase with the following columns:

| Column | Type | Description |
|--------|------|-------------|
| `code` | text | 6-digit EWC code |
| `chapter` | int | Chapter number |
| `subchapter` | text | Subchapter code |
| `description` | text | Waste description |
| `hazardous` | bool | Whether the waste is hazardous |
| `entry_type` | text | AN, AH, MH, or MN |
| `priority` | bool | Industry-relevant codes |

---

## Usage

### Run the full pipeline

```bash
python main.py --pdf "data/MV_EUROFERRY_OLYMPIA_IHM.pdf"
```

With a specific model:

```bash
python main.py --pdf "data/MV_EUROFERRY_OLYMPIA_IHM.pdf" --model "gpt-5"
```

### Run steps individually

**Step 1: Extract hazmat data from PDF**

```bash
python extract_hazmat_from_pdf.py --pdf "data/MV_EUROFERRY_OLYMPIA_IHM.pdf"
```

**Step 2: Classify with EWC codes**

```bash
python classify_ewc.py --json "outputs/JSON Extractions/MV_EUROFERRY_OLYMPIA_IHM_extract.json"
```

---

## Output

The pipeline produces JSON files in `outputs/JSON Extractions/`:

- `{filename}_extract_{date}.json` — Raw extraction from PDF
- `{filename}_extract_{date}_ewc.json` — With EWC classifications added

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
- Supabase project with `ewc_codes` table
