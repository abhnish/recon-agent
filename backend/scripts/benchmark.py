import subprocess
import sys
import time
from pathlib import Path

import requests

backend_dir = Path(__file__).parent.parent

def generate_data(size: int):
    print(f"\n--- Generating {size} records ---")
    script = backend_dir / "data" / "generate_synthetic_data.py"
    subprocess.run([sys.executable, str(script), "--size", str(size)], check=True, capture_output=True)

def run_benchmark(size: int):
    generate_data(size)
    print(f"Running pipeline for {size} records...")
    
    t0 = time.perf_counter()
    resp = requests.post("http://localhost:8765/api/reconcile/run")
    resp.raise_for_status()
    t1 = time.perf_counter()
    
    data = resp.json()
    print(f"API latency: {(t1 - t0) * 1000:.2f} ms")
    print(f"Pipeline latency: {data['runtime_ms']} ms")
    print(f"Orders: {data['orders_loaded']}")
    print(f"Auto-matched: {data['auto_matched']}, Needs Review: {data['needs_review']}, Unresolved: {data['unresolved']}")

if __name__ == "__main__":
    run_benchmark(60)
    run_benchmark(600)
