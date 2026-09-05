import os
import sys
import time
import csv
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api.reconcile import _load_csv, _DATA_DIR
from app.services.normalisation import normalise_order, normalise_settlement, normalise_bank_txn
from app.services.matching import (
    run_matching,
    detect_duplicate_settlements,
    detect_duplicate_orders,
    detect_unmatched_bank_credits,
    detect_ambiguous_bank_matches,
)
from app.services.classification import classify_all
from app.services.exception_diff import build_exception_list
from app.services.llm_layer import explain_exception, clear_explain_cache, _explain_cache

def main():
    print("Loading dataset...")
    raw_orders = _load_csv(_DATA_DIR / "order_ledger.csv")
    raw_settlements = _load_csv(_DATA_DIR / "settlement_report.csv")
    raw_bank = _load_csv(_DATA_DIR / "bank_statement.csv")
    
    orders = [normalise_order(r) for r in raw_orders]
    settlements = [normalise_settlement(r) for r in raw_settlements]
    bank_txns = [normalise_bank_txn(r) for r in raw_bank]
    
    match_results, _ = run_matching(orders, settlements, bank_txns)
    
    duplicate_settlement_ids = detect_duplicate_settlements(settlements)
    duplicate_ledger_ids = detect_duplicate_orders(orders)
    phantom_bank_ids = detect_unmatched_bank_credits(bank_txns, settlements)
    ambiguous_order_ids = detect_ambiguous_bank_matches(match_results)

    classified = classify_all(
        match_results,
        duplicate_settlement_order_ids=duplicate_settlement_ids,
        duplicate_ledger_order_ids=duplicate_ledger_ids,
        
        ambiguous_order_ids=ambiguous_order_ids,
    )
    
    exceptions = build_exception_list(classified)
    
    if not exceptions:
        print("No exceptions found in dataset!")
        return

    clear_explain_cache()
    
    test_cases = (exceptions * 20)[:20]
    print("Firing 20 explain requests in quick succession...")
    
    results_log = []
    def fire_req(i, exc):
        t0 = time.time()
        resp = explain_exception(exc)
        dt = time.time() - t0
        return i, resp.order_id, resp.llm_status, dt

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(fire_req, i, exc) for i, exc in enumerate(test_cases)]
        for f in futures:
            try:
                res = f.result()
                results_log.append(res)
                print(f"Request {res[0]:02d}: order={res[1]} | status={res[2]:8s} | time={res[3]:.2f}s")
            except Exception as e:
                print(f"Request failed unexpectedly: {e}")

    print("\nSummary:")
    status_counts = {}
    for r in results_log:
        status_counts[r[2]] = status_counts.get(r[2], 0) + 1
        
    for status, count in status_counts.items():
        print(f" - {status}: {count}")

    print(f"\nFinal Cache size: {len(_explain_cache)}")

if __name__ == '__main__':
    main()
