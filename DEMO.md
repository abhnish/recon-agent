# ReconAgent: 3-Minute Demo Script

> **Prerequisites:** Ensure the servers are running (`uvicorn` and Vite). Before starting the demo, generate the curated dataset:
> ```bash
> cd backend && source ../.venv/bin/activate
> python data/demo_seed.py
> ```

## 0:00 - 0:10 | The Problem
"Payment reconciliation is spreadsheet hell. Today, finance teams at Razorpay merchants manually match order ledgers against settlement reports and bank statements. It's slow, error-prone, and scales terribly. Let's look at how ReconAgent solves this."

## 0:10 - 0:40 | The Pipeline
*(Navigate to the UI dashboard)*
1. **Click 'Run Reconciliation'.**
2. "Under the hood, we just ingested orders, settlements, and bank transactions. The system ran a 100% deterministic matching pipeline."
3. "Our matching engine evaluates date proximity, exact amount calculations (including fee/tax deductions), and UTR fuzzing to link the three disparate datasets."
4. "The result? High-confidence clean matches are instantly auto-reconciled. The ones that need human attention fall into our Exceptions Queue."

## 0:40 - 1:40 | The AI Assist (Partial Refund)
*(Navigate to the Exceptions Queue)*
1. **Click on the `PARTIAL_REFUND` row (Order DEMO-002).**
2. "Here's an exception. To a human, reading the raw diff is tedious. Let's ask Gemini to explain it."
3. **Click 'Generate Explanation'.**
4. "ReconAgent uses Gemini 2.5 Flash to translate the deterministic numerical diff into plain language. It explains exactly what happened: a partial refund where the settlement is less than the order amount. It tells the operator exactly how to resolve it."
5. *(Optional: Ask the Chat Assistant a follow-up question, like "Why did DEMO-002 fail?")*

## 1:40 - 2:10 | The Honest Exception (Failed Payment)
*(Still in Exceptions Queue)*
1. **Click on the `FAILED_PAYMENT` row (Order DEMO-003).**
2. "Crucially, the LLM layer NEVER decides if two transactions match. That is strictly prohibited by our architecture. If an order never settled, it stays unresolved. The AI's job is to explain the gap, not hallucinate a match."
3. "We also flag structural issues, like `AMBIGUOUS_MATCH` (DEMO-004A/B), where two orders share the same UTR. The system surfaces both candidates instead of silently guessing."

## 2:10 - 2:30 | The Audit Trail
*(Navigate to the Audit Trail)*
1. "In finance, traceability is non-negotiable."
2. "Every deterministic match decision, and every single LLM prompt and response, is logged in the append-only Audit Trail — you can trace exactly which rule fired, what score was assigned, and what Gemini was sent and returned. We even have a built-in hallucination detector that flags inconsistent LLM outputs."

## 2:30 - 2:40 | Conclusion
1. "ReconAgent processes ~79,000 orders/sec through its deterministic engine. End-to-end
   precision and recall are both 1.0 across all categories on the seed=42 benchmark: the
   composite scoring signal routes most cases, and a secondary anomaly-flag check catches
   any edge case where raw score alone would be ambiguous. Gemini handles the final mile —
   plain-language explanations for the exceptions that need human review."
2. "It's the best of both worlds: strict financial correctness, with AI-powered operational velocity."
