import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.matching import run_matching, detect_duplicate_settlements, detect_duplicate_orders, detect_ambiguous_bank_matches
from app.services.classification import classify_all
from app.services.exception_diff import build_exception_list
from app.services.llm_layer import explain_exception, _explain_cache

def main():
    print("Pre-warming LLM cache for demo_seed.py data...")
    # demo_seed.py is a module containing raw lists. Let's import it directly.
    import data.demo_seed as demo_seed
    
    from app.services.normalisation import normalise_order, normalise_settlement, normalise_bank_txn
    
    orders = [normalise_order(r) for r in demo_seed.ORDERS]
    settlements = [normalise_settlement(r) for r in demo_seed.SETTLEMENTS]
    bank_txns = [normalise_bank_txn(r) for r in demo_seed.BANK_TXNS]
    
    match_results, _ = run_matching(orders, settlements, bank_txns)
    
    dupes = detect_duplicate_settlements(settlements)
    duplicate_ledger_ids = detect_duplicate_orders(orders)
    ambiguous_order_ids = detect_ambiguous_bank_matches(match_results)

    classified = classify_all(
        match_results,
        duplicate_settlement_order_ids=dupes,
        duplicate_ledger_order_ids=duplicate_ledger_ids,
        ambiguous_order_ids=ambiguous_order_ids,
    )
    
    exceptions = build_exception_list(classified)
    
    if not exceptions:
        print("No exceptions to warm in demo seed.")
        return
        
    print(f"Found {len(exceptions)} exceptions in demo seed. Warming cache...")
    for exc in exceptions:
        print(f" -> Explaining {exc.order_id}...")
        resp = explain_exception(exc)
        print(f"    Result: {resp.llm_status}")

    print(f"Done. Final Cache size: {len(_explain_cache)}")

if __name__ == '__main__':
    main()
