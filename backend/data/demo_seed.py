"""
demo_seed.py
────────────
Generates a curated, hand-picked dataset for live demonstrations.
This is not a random synthetic generator. It intentionally produces a small
number of highly illustrative examples to tell a clear story during a demo.

The generated files are placed in the backend/data directory, overwriting
any existing data.
"""

import csv
from pathlib import Path

# Data directory
DATA_DIR = Path(__file__).parent
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── 1. Clean Match ────────────────────────────────────────────────────────────
# Perfect match across the board.
clean_order = {
    "order_id": "DEMO-001",
    "order_date": "2024-03-01",
    "customer_name": "Alice Retail",
    "amount": "1500.00",
    "currency": "INR",
}
clean_settlement = {
    "settlement_id": "SETL-001",
    "order_id": "DEMO-001",
    "settled_date": "2024-03-02",
    "settled_amount": "1464.60",
    "fee": "30.00",
    "tax_on_fee": "5.40",
    "utr_number": "UTR2024HDFC001",
}
clean_bank = {
    "txn_date": "2024-03-02",
    "description": "NEFT CR-UTR2024HDFC001 RAZORPAY",
    "credit_amount": "1464.60",
    "utr_reference": "UTR2024HDFC001",
}

# ── 2. Partial Refund (Shortfall) ─────────────────────────────────────────────
# Customer returned a partial order, but the refund wasn't recorded in our ledger.
refund_order = {
    "order_id": "DEMO-002",
    "order_date": "2024-03-01",
    "customer_name": "Bob's Electronics",
    "amount": "5000.00",
    "currency": "INR",
}
refund_settlement = {
    "settlement_id": "SETL-002",
    "order_id": "DEMO-002",
    "settled_date": "2024-03-02",
    "settled_amount": "3902.40", # 4000 gross - fee (80) - tax (17.6)
    "fee": "80.00",
    "tax_on_fee": "17.60",
    "utr_number": "UTR2024ICIC002",
}
refund_bank = {
    "txn_date": "2024-03-02",
    "description": "IMPS-UTR2024ICIC002-RZP",
    "credit_amount": "3902.40",
    "utr_reference": "UTR2024ICIC002",
}

# ── 3. Unresolved (Failed Payment) ────────────────────────────────────────────
# Order exists, but no settlement was ever created.
failed_order = {
    "order_id": "DEMO-003",
    "order_date": "2024-03-01",
    "customer_name": "Charlie Coffee",
    "amount": "350.00",
    "currency": "INR",
}
# No settlement or bank txn.

# ── 4. Ambiguous Match ────────────────────────────────────────────────────────
# Two orders for the exact same amount on the same day.
# They both map to settlements, but they have the same UTR and credit amount.
ambig_order_1 = {
    "order_id": "DEMO-004A",
    "order_date": "2024-03-01",
    "customer_name": "Dave's Diner A",
    "amount": "800.00",
    "currency": "INR",
}
ambig_settlement_1 = {
    "settlement_id": "SETL-004A",
    "order_id": "DEMO-004A",
    "settled_date": "2024-03-02",
    "settled_amount": "781.12",
    "fee": "16.00",
    "tax_on_fee": "2.88",
    "utr_number": "UTR2024SBIN004", # Same UTR!
}
ambig_order_2 = {
    "order_id": "DEMO-004B",
    "order_date": "2024-03-01",
    "customer_name": "Dave's Diner B",
    "amount": "800.00",
    "currency": "INR",
}
ambig_settlement_2 = {
    "settlement_id": "SETL-004B",
    "order_id": "DEMO-004B",
    "settled_date": "2024-03-02",
    "settled_amount": "781.12",
    "fee": "16.00",
    "tax_on_fee": "2.88",
    "utr_number": "UTR2024SBIN004", # Same UTR!
}
ambig_bank = {
    "txn_date": "2024-03-02",
    "description": "NEFT UTR2024SBIN004",
    "credit_amount": "781.12",
    "utr_reference": "UTR2024SBIN004",
}


def write_csv(filename: str, fieldnames: list, rows: list) -> None:
    path = DATA_DIR / filename
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {filename}")


def generate() -> None:
    print("Generating curated demo dataset...")

    orders = [clean_order, refund_order, failed_order, ambig_order_1, ambig_order_2]
    write_csv(
        "order_ledger.csv",
        ["order_id", "order_date", "customer_name", "amount", "currency"],
        orders
    )

    settlements = [clean_settlement, refund_settlement, ambig_settlement_1, ambig_settlement_2]
    write_csv(
        "settlement_report.csv",
        ["settlement_id", "order_id", "settled_date", "settled_amount", "fee", "tax_on_fee", "utr_number"],
        settlements
    )

    bank_txns = [clean_bank, refund_bank, ambig_bank]
    write_csv(
        "bank_statement.csv",
        ["txn_date", "description", "credit_amount", "utr_reference"],
        bank_txns
    )
    
    print("\nDemo dataset generation complete!")
    print("Run `pytest` to ensure nothing is broken, then proceed to run the server.")


if __name__ == "__main__":
    generate()
