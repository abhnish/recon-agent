"""
generate_synthetic_data.py
──────────────────────────
Generates three reproducible CSV files that simulate a real payment reconciliation
scenario for 60 transactions across three data sources:

  1. order_ledger.csv         — internal order records
  2. settlement_report.csv    — payment gateway settlement report
  3. bank_statement.csv       — bank account credits

Category breakdown (fixed seed, reproducible):
  ├── ~70%  CLEAN MATCH        — fully traceable end-to-end via UTR
  ├── ~15%  HARD MISMATCH      — partial refund / rounding diff / delayed settlement
  └── ~15%  EXCEPTION          — no settlement, phantom credit, or duplicate settlement

Run from the repo root:
    python backend/data/generate_synthetic_data.py

Output files are written to the same directory as this script (backend/data/).
"""

from __future__ import annotations

import argparse
import random
import re
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ─── Reproducibility ────────────────────────────────────────────────────────
RANDOM_SEED = 42
rng = np.random.default_rng(RANDOM_SEED)
random.seed(RANDOM_SEED)

# ─── Output directory (same dir as this script) ─────────────────────────────
OUTPUT_DIR = Path(__file__).parent

# ─── Constants & Parameters ──────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Generate synthetic data")
parser.add_argument(
    "--size", type=int, default=60, help="Total number of orders to generate"
)
args = parser.parse_args()

TOTAL_ORDERS = args.size
# Maintain ~70% clean, ~15% mismatch, ~15% exception
N_CLEAN = int(TOTAL_ORDERS * 0.70)
N_MISMATCH = int(TOTAL_ORDERS * 0.15)
N_EXCEPTION = TOTAL_ORDERS - N_CLEAN - N_MISMATCH

# Ensure we have at least minimum numbers if size is small
if N_MISMATCH < 9:
    N_MISMATCH = min(TOTAL_ORDERS, 9)
    N_CLEAN = TOTAL_ORDERS - N_MISMATCH - N_EXCEPTION

START_DATE = date(2024, 6, 1)
END_DATE = date(2024, 8, 31)

CURRENCIES = ["INR"]  # single-currency for v1; multi-currency in a later chunk

# Razorpay-style fee structure: 2% + GST (18%) on fee
GATEWAY_FEE_RATE = 0.02
GST_RATE = 0.18  # GST is applied only to the fee, not the principal

CUSTOMER_NAMES = [
    "Arjun Mehta",
    "Priya Sharma",
    "Rohit Verma",
    "Sneha Iyer",
    "Vikram Nair",
    "Ananya Krishnan",
    "Karan Gupta",
    "Meera Pillai",
    "Nikhil Joshi",
    "Pooja Rao",
    "Suresh Patel",
    "Divya Reddy",
    "Amit Bose",
    "Kavitha Menon",
    "Rahul Singhania",
    "Shruti Desai",
    "Aditya Kulkarni",
    "Nisha Agarwal",
    "Manish Tiwari",
    "Ritu Saxena",
    "Deepak Malhotra",
    "Swati Choudhury",
    "Varun Bansal",
    "Leela Nambiar",
    "Sanjay Kapoor",
    "Aarti Bhatt",
    "Rajesh Dubey",
    "Preethi Subramaniam",
    "Mohit Arora",
    "Sunita Yadav",
]

# ─── Helpers ─────────────────────────────────────────────────────────────────


def random_date(start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=int(rng.integers(0, delta + 1)))


def make_order_id(n: int) -> str:
    return f"ORD{2024_000 + n:06d}"


def make_utr() -> str:
    """Generate a UTR number that mimics IMPS/NEFT format."""
    bank_codes = ["HDFC", "ICIC", "SBIN", "AXIS", "KOTK"]
    bank = random.choice(bank_codes)
    suffix = "".join([str(rng.integers(0, 10)) for _ in range(8)])
    alpha = "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=4))
    return f"UTR{2024}{bank}{alpha}{suffix}"


def make_settlement_id(n: int) -> str:
    return f"SETL{2024_000 + n:07d}"


def compute_fee_and_tax(amount: float) -> tuple[float, float]:
    """Return (fee, tax_on_fee) rounded to 2dp, matching Razorpay's published breakdown."""
    fee = round(amount * GATEWAY_FEE_RATE, 2)
    tax = round(fee * GST_RATE, 2)
    return fee, tax


def noisy_utr_format(utr: str, style: int) -> str:
    """
    Simulate the way different bank systems reformat the same UTR.
    style=0 → canonical     e.g. "UTR2024HDFC..."
    style=1 → hyphenated    e.g. "UTR-2024-HDFC-..."
    style=2 → truncated     e.g. first 16 chars
    style=3 → lowercase     e.g. all lowercase
    """
    if style == 0:
        return utr
    if style == 1:
        # Insert hyphens after "UTR", after year, after bank code
        m = re.match(r"(UTR)(\d{4})([A-Z]{4})(.+)", utr)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}-{m.group(4)}"
        return utr
    if style == 2:
        return utr[:16]  # truncation — some banks show partial UTR in description
    if style == 3:
        return utr.lower()
    return utr


def noisy_description(customer_name: str, utr: str, style: int) -> str:
    """
    Produce a bank statement description line — real banks use wildly inconsistent formats.
    """
    utr_display = noisy_utr_format(utr, style=rng.integers(0, 4).item())
    templates = [
        f"NEFT CR-{utr_display}-{customer_name.upper()[:12]}-RAZORPAY",
        f"IMPS/{utr_display}/{customer_name.split()[0]}/RAZORPAY SOFTWARE",
        f"CR RAZORPAY SETTLEMENTS {utr_display}",
        f"CREDIT {utr_display} {customer_name[:10].upper()}",
        f"INW RAZORPAY {utr_display}",
    ]
    return templates[style % len(templates)]


# ─── Data containers ─────────────────────────────────────────────────────────

orders: list[dict] = []
settlements: list[dict] = []
bank_txns: list[dict] = []

# Track ground truth for the summary printout
ground_truth: dict[str, list[str]] = {
    "CLEAN_MATCH": [],
    "HARD_MISMATCH": [],
    "EXCEPTION": [],
}

order_counter = 1
settlement_counter = 1


# ─── Category 1: CLEAN MATCHES (42 transactions) ────────────────────────────

for _ in range(N_CLEAN):
    oid = make_order_id(order_counter)
    order_counter += 1

    order_date = random_date(START_DATE, END_DATE - timedelta(days=7))
    customer = random.choice(CUSTOMER_NAMES)
    amount = round(float(rng.integers(500, 50_001)), 2)  # ₹500 – ₹50,000
    utr = make_utr()

    fee, tax = compute_fee_and_tax(amount)
    settled_amount = round(amount - fee - tax, 2)

    # Settlement: 1–3 business days after order
    settlement_lag = int(rng.integers(1, 4))
    settled_date = order_date + timedelta(days=settlement_lag)

    sid = make_settlement_id(settlement_counter)
    settlement_counter += 1

    # Bank credit on the same day as settlement
    desc_style = int(rng.integers(0, 5))
    bank_desc = noisy_description(customer, utr, style=desc_style)

    orders.append(
        {
            "order_id": oid,
            "order_date": order_date.isoformat(),
            "customer_name": customer,
            "amount": amount,
            "currency": "INR",
        }
    )
    settlements.append(
        {
            "settlement_id": sid,
            "order_id": oid,
            "settled_date": settled_date.isoformat(),
            "settled_amount": settled_amount,
            "fee": fee,
            "tax_on_fee": tax,
            "utr_number": utr,
        }
    )
    bank_txns.append(
        {
            "txn_date": settled_date.isoformat(),
            "description": bank_desc,
            "credit_amount": settled_amount,
            "utr_reference": noisy_utr_format(utr, style=int(rng.integers(0, 4))),
        }
    )

    ground_truth["CLEAN_MATCH"].append(oid)


# ─── Category 2: HARD MISMATCHES (9 transactions) ────────────────────────────
# Sub-types: partial_refund, rounding_diff, delayed_settlement (3 each)

mismatch_subtypes = (
    ["partial_refund"] * 3 + ["rounding_diff"] * 3 + ["delayed_settlement"] * 3
)
random.shuffle(mismatch_subtypes)

for subtype in mismatch_subtypes:
    oid = make_order_id(order_counter)
    order_counter += 1

    order_date = random_date(START_DATE, END_DATE - timedelta(days=10))
    customer = random.choice(CUSTOMER_NAMES)
    amount = round(float(rng.integers(500, 50_001)), 2)
    utr = make_utr()

    fee, tax = compute_fee_and_tax(amount)
    clean_settled = round(amount - fee - tax, 2)

    sid = make_settlement_id(settlement_counter)
    settlement_counter += 1

    if subtype == "partial_refund":
        # Settlement is for ~70–90% of the order (partial refund, no explicit refund record)
        refund_fraction = float(rng.uniform(0.70, 0.90))
        effective_amount = round(amount * refund_fraction, 2)
        fee_adj, tax_adj = compute_fee_and_tax(effective_amount)
        settled_amount = round(effective_amount - fee_adj - tax_adj, 2)
        settlement_lag = int(rng.integers(1, 4))
        settled_date = order_date + timedelta(days=settlement_lag)

        settlements.append(
            {
                "settlement_id": sid,
                "order_id": oid,
                "settled_date": settled_date.isoformat(),
                "settled_amount": settled_amount,
                "fee": fee_adj,
                "tax_on_fee": tax_adj,
                "utr_number": utr,
            }
        )

    elif subtype == "rounding_diff":
        # Rounding mismatch: ₹0.01 – ₹2.00 difference (common in GST rounding)
        rounding_noise = round(float(rng.uniform(0.01, 2.00)), 2)
        settled_amount = round(clean_settled - rounding_noise, 2)
        settlement_lag = int(rng.integers(1, 4))
        settled_date = order_date + timedelta(days=settlement_lag)

        settlements.append(
            {
                "settlement_id": sid,
                "order_id": oid,
                "settled_date": settled_date.isoformat(),
                "settled_amount": settled_amount,
                "fee": fee,
                "tax_on_fee": tax,
                "utr_number": utr,
            }
        )

    else:  # delayed_settlement
        # Settlement is >5 days after order (T+6 to T+14)
        settlement_lag = int(rng.integers(6, 15))
        settled_date = order_date + timedelta(days=settlement_lag)
        settled_amount = clean_settled

        settlements.append(
            {
                "settlement_id": sid,
                "order_id": oid,
                "settled_date": settled_date.isoformat(),
                "settled_amount": settled_amount,
                "fee": fee,
                "tax_on_fee": tax,
                "utr_number": utr,
            }
        )

    desc_style = int(rng.integers(0, 5))
    bank_desc = noisy_description(customer, utr, style=desc_style)

    orders.append(
        {
            "order_id": oid,
            "order_date": order_date.isoformat(),
            "customer_name": customer,
            "amount": amount,
            "currency": "INR",
        }
    )
    bank_txns.append(
        {
            "txn_date": settled_date.isoformat(),
            "description": bank_desc,
            "credit_amount": settlements[-1]["settled_amount"],
            "utr_reference": noisy_utr_format(utr, style=int(rng.integers(0, 4))),
        }
    )

    ground_truth["HARD_MISMATCH"].append(f"{oid} ({subtype})")


# ─── Category 3: GENUINE EXCEPTIONS (9 transactions) ────────────────────────
# Sub-types:
#   (a) 3 × failed_payment    — order exists, NO settlement, NO bank credit
#   (b) 3 × phantom_credit    — bank credit with NO matching UTR anywhere
#   (c) 3 × duplicate_settle    # N_EXCEPTION (15%) - Divided into 3 categories
num_failed = max(1, N_EXCEPTION // 3)
num_phantom = max(1, N_EXCEPTION // 3)
num_duplicate = N_EXCEPTION - num_failed - num_phantom

# 1. Failed Payments (No settlement, no bank credit)
for i in range(num_failed):
    oid = make_order_id(order_counter)
    order_counter += 1
    order_date = random_date(START_DATE, END_DATE)
    customer = random.choice(CUSTOMER_NAMES)
    amount = round(float(rng.integers(500, 50_001)), 2)

    orders.append(
        {
            "order_id": oid,
            "order_date": order_date.isoformat(),
            "customer_name": customer,
            "amount": amount,
            "currency": "INR",
        }
    )
    ground_truth["EXCEPTION"].append(f"{oid} (failed_payment)")

# (b) Phantom bank credits — no UTR in settlement report matches these
for _ in range(num_phantom):
    phantom_utr = make_utr()
    credit_date = random_date(START_DATE, END_DATE)
    credit_amount = round(float(rng.integers(1000, 100_001)), 2)
    desc_style = int(rng.integers(0, 5))

    # Pick a plausible-sounding customer-like name for the description
    customer = random.choice(CUSTOMER_NAMES)
    bank_desc = noisy_description(customer, phantom_utr, style=desc_style)

    bank_txns.append(
        {
            "txn_date": credit_date.isoformat(),
            "description": bank_desc,
            "credit_amount": credit_amount,
            "utr_reference": noisy_utr_format(
                phantom_utr, style=int(rng.integers(0, 4))
            ),
        }
    )
    ground_truth["EXCEPTION"].append(f"phantom_credit UTR={phantom_utr[:20]}...")

# (c) Duplicate settlements
for i in range(num_duplicate):
    # Re-use an existing order from the clean batch
    base_idx = int(rng.integers(0, N_CLEAN))
    src_order = orders[base_idx]
    oid = src_order["order_id"]
    amount = src_order["amount"]
    fee, tax = compute_fee_and_tax(amount)
    settled_amount = round(amount - fee - tax, 2)

    original_utr = settlements[base_idx]["utr_number"]

    # Duplicate gets a new settlement_id and a slightly different UTR
    dup_utr = make_utr()
    sid = make_settlement_id(settlement_counter)
    settlement_counter += 1

    # Duplicate settled 1–3 days after the original
    original_settled = date.fromisoformat(settlements[base_idx]["settled_date"])
    dup_settled = original_settled + timedelta(days=int(rng.integers(1, 4)))

    settlements.append(
        {
            "settlement_id": sid,
            "order_id": oid,  # ← same order_id as an existing settlement
            "settled_date": dup_settled.isoformat(),
            "settled_amount": settled_amount,
            "fee": fee,
            "tax_on_fee": tax,
            "utr_number": dup_utr,
        }
    )

    # Bank credit for the duplicate settlement
    customer = src_order["customer_name"]
    desc_style = int(rng.integers(0, 5))
    bank_txns.append(
        {
            "txn_date": dup_settled.isoformat(),
            "description": noisy_description(customer, dup_utr, style=desc_style),
            "credit_amount": settled_amount,
            "utr_reference": noisy_utr_format(dup_utr, style=int(rng.integers(0, 4))),
        }
    )

    ground_truth["EXCEPTION"].append(f"{oid} (duplicate_settlement new_sid={sid})")


# ─── Build DataFrames & shuffle ───────────────────────────────────────────────

df_orders = (
    pd.DataFrame(orders).sample(frac=1, random_state=RANDOM_SEED).reset_index(drop=True)
)
df_settlements = (
    pd.DataFrame(settlements)
    .sample(frac=1, random_state=RANDOM_SEED)
    .reset_index(drop=True)
)
df_bank = (
    pd.DataFrame(bank_txns)
    .sample(frac=1, random_state=RANDOM_SEED)
    .reset_index(drop=True)
)

# Sort bank statement chronologically (as a real export would be)
df_bank = df_bank.sort_values("txn_date").reset_index(drop=True)


# ─── Write CSVs ───────────────────────────────────────────────────────────────

df_orders.to_csv(OUTPUT_DIR / "order_ledger.csv", index=False)
df_settlements.to_csv(OUTPUT_DIR / "settlement_report.csv", index=False)
df_bank.to_csv(OUTPUT_DIR / "bank_statement.csv", index=False)


# ─── Ground-truth summary ────────────────────────────────────────────────────

print("=" * 70)
print("ReconAgent — Synthetic Data Generator")
print("=" * 70)
print(f"\nOutput directory : {OUTPUT_DIR.resolve()}")
print(f"Random seed      : {RANDOM_SEED}  (all results reproducible)")
print()

total_bank_rows = len(df_bank)
# bank rows = N_CLEAN + N_MISMATCH + 3 phantom + 3 duplicate
expected_bank = N_CLEAN + N_MISMATCH + 3 + 3

print("─" * 70)
print(f"{'File':<30} {'Rows':>8}")
print("─" * 70)
print(f"{'order_ledger.csv':<30} {len(df_orders):>8}")
print(f"{'settlement_report.csv':<30} {len(df_settlements):>8}")
print(f"{'bank_statement.csv':<30} {len(df_bank):>8}")
print("─" * 70)
print()

print("CATEGORY BREAKDOWN (ground truth)")
print("─" * 70)

print(f"\n✅  CLEAN MATCH  ({N_CLEAN} orders)  — fully traceable via UTR")
for item in ground_truth["CLEAN_MATCH"]:
    print(f"    {item}")

print(f"\n⚠️   HARD MISMATCH  ({N_MISMATCH} orders)  — resolvable with context")
for item in ground_truth["HARD_MISMATCH"]:
    print(f"    {item}")

print(
    f"\n❌  EXCEPTION  ({len(ground_truth['EXCEPTION'])} entries)  — unresolvable without manual action"
)
for item in ground_truth["EXCEPTION"]:
    print(f"    {item}")

print()
print("─" * 70)
print(
    f"Total orders       : {len(df_orders)}  "
    f"(expected {N_CLEAN + N_MISMATCH + num_failed + num_duplicate})"
)
print(
    f"Total settlements  : {len(df_settlements)}  "
    f"(expected {N_CLEAN + N_MISMATCH + num_duplicate * 2})"
)
print(f"Total bank rows    : {len(df_bank)}  ")
print("=" * 70)
print("✓ CSVs written successfully.")
