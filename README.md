# 🌊 CleanHarbor

**CleanHarbor** is a lightweight AI-powered toolkit for automating maritime compliance workflows — including the parsing, classification, and structuring of ship documentation for regulatory reporting.

---

## ⚙️ IHM Parser

The **IHM Parser** extracts hazardous-material data from *Inventory of Hazardous Materials (IHM)* PDF reports.

It performs:
1. **Text Extraction** — Converts entire IHM PDFs to text using `pdfplumber`.
2. **AI Processing** — Uses the OpenAI API to identify and structure hazardous-material tables (from Chapter 5.2 onward) into clean JSON.

### Example Usage
```bash
python ihm_parser/extract_hazmat_from_pdf.py \
  --pdf "data/MV_EUROFERRY_OLYMPIA_IHM.pdf" \
  --out "outputs/MV_EUROFERRY_OLYMPIA_IHM_extract.json" \
  --model "gpt-5"
