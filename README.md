# ReconAgent

**AI Finance Controller — Razorpay Buildathon**

---

## What is this?

ReconAgent is a payment reconciliation system built for the Razorpay Buildathon (Track: AI Finance Controller). It reconciles transactions across three data sources — a **bank statement**, a **payment gateway settlement report**, and an **internal order ledger** — using a **deterministic weighted matching engine**. An LLM layer (Google Gemini) is used exclusively to *explain* exceptions and answer natural-language questions about the reconciliation results. The LLM never makes matching decisions; all classification is rule-based and auditable.

---

## The Problem

For any merchant using a payment gateway, reconciliation is a daily headache. The gateway settles funds in batches, deducts fees and taxes, reformats transaction references, and may delay or even duplicate settlements. The bank statement uses its own description format, often truncating or re-hyphenating the UTR reference used in the settlement report. The internal order ledger is a third independent source. Manually cross-referencing these three datasets to find mismatches, failed payments, duplicate settlements, and phantom credits is slow, error-prone, and completely unscalable. ReconAgent automates this — deterministically and with a full audit trail — and uses Gemini to surface plain-language explanations for the anomalies that need human review.

---

## Architecture (brief)

```
order_ledger.csv  ─┐
settlement_report.csv ─┤──▶  Weighted Matching Engine ──▶  Classifications
bank_statement.csv ───┘           (deterministic)                │
                                                                  ▼
                                                       Gemini (explain + Q&A)
                                                           (LLM — never matches)
```

---

## Setup

### Prerequisites

- Python 3.11+
- A free **Gemini API key** from [Google AI Studio](https://aistudio.google.com/app/apikey) *(required for the explain/Q&A layer, added in Chunk 5)*

### Steps

```bash
# 1. Clone the repo and enter the project directory
git clone <repo-url>
cd recon-agent

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r backend/requirements.txt

# 4. Configure environment variables
cp backend/.env.example backend/.env
# Open backend/.env and fill in your GEMINI_API_KEY

# 6. Generate the curated demo dataset
python backend/data/demo_seed.py

# 7. Start the FastAPI backend
cd backend && source ../.venv/bin/activate
uvicorn app.main:app --port 8765

# 8. Start the Vite frontend in a new terminal
cd frontend
npm install
npm run dev
# → UI available at http://localhost:5173
```

---

## Project Status

| # | Chunk | Status |
|---|-------|--------|
| 1 | Project scaffold + synthetic data generator | ✅ **Done** |
| 2 | App state & API schema | ✅ **Done** |
| 3 | Weighted matching engine (deterministic, rule-based) | ✅ **Done** |
| 4 | Classification + exception handling | ✅ **Done** |
| 5 | Gemini-powered explain & Q&A layer | ✅ **Done** |
| 6 | FastAPI endpoints | ✅ **Done** |
| 7 | Frontend dashboard | ✅ **Done** |
| 8 | Audit trail + polish | ✅ **Done** |
| 9 | Edge cases + demo script | ✅ **Done** |

---

## Data Sources

You can generate a reproducible **synthetic dataset** (60 transactions) for testing using:
`python backend/data/generate_synthetic_data.py`

For live demos, use the **curated demo dataset** (5 illustrative edge cases):
`python backend/data/demo_seed.py`

UTR references are deliberately noisy across files (truncated, hyphenated, lowercased) to simulate real bank statement behaviour.

---

## Demo Script

A 3-minute live presentation script is available at [`DEMO.md`](DEMO.md).

---

## License

MIT
