#!/usr/bin/env python3
"""Run the full agent-commerce-poc test suite and generate a failure report.

Usage:
    python tests/run_all.py                  # Run everything
    python tests/run_all.py --api-only       # API tests only (fast, no LLM)
    python tests/run_all.py --agent-only     # Agent simulations only (slower)
    python tests/run_all.py --report         # Generate JSON report
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

API_BASE = "http://localhost:8000"
AGENT_BASE = "http://localhost:8080"
TESTS_DIR = Path(__file__).parent
REPORT_PATH = TESTS_DIR / "test_report.json"


def check_health() -> bool:
    """Verify both services are healthy."""
    ok = True
    for name, url in [("API", f"{API_BASE}/health"), ("Agent", f"{AGENT_BASE}/health")]:
        try:
            r = httpx.get(url, timeout=5.0)
            if r.status_code == 200:
                print(f"  {name}: healthy")
            else:
                print(f"  {name}: unhealthy (status {r.status_code})")
                ok = False
        except Exception as e:
            print(f"  {name}: unreachable ({e})")
            ok = False
    return ok


def run_pytest(test_pattern: str, label: str) -> dict:
    """Run pytest with the given pattern and return results."""
    cmd = [
        sys.executable, "-m", "pytest",
        str(TESTS_DIR / test_pattern),
        "-v", "--tb=short", "--no-header",
        f"--junit-xml={TESTS_DIR / f'junit_{label}.xml'}",
    ]
    print(f"\n{'='*60}")
    print(f"Running: {label}")
    print(f"Command: {' '.join(cmd)}")
    print(f"{'='*60}\n")

    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(TESTS_DIR))
    elapsed = time.time() - t0

    print(result.stdout)
    if result.stderr:
        print(result.stderr)

    passed = failed = errors = 0
    for line in result.stdout.split("\n"):
        if " passed" in line:
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "passed" and i > 0:
                    try: passed = int(parts[i-1])
                    except ValueError: pass
                if p == "failed" and i > 0:
                    try: failed = int(parts[i-1])
                    except ValueError: pass
                if p == "error" in p and i > 0:
                    try: errors = int(parts[i-1])
                    except ValueError: pass

    failures = []
    in_failure = False
    current = []
    for line in result.stdout.split("\n"):
        if line.startswith("FAILED "):
            failures.append(line)
        elif "FAIL" in line and "::" in line:
            failures.append(line)

    return {
        "label": label,
        "exit_code": result.returncode,
        "elapsed_seconds": round(elapsed, 1),
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "failure_lines": failures,
        "stdout": result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout,
        "stderr": result.stderr[-1000:] if len(result.stderr) > 1000 else result.stderr,
    }


def main():
    args = set(sys.argv[1:])
    api_only = "--api-only" in args
    agent_only = "--agent-only" in args
    generate_report = "--report" in args or not args

    print("Agent Commerce POC — Test Suite")
    print("=" * 60)
    print("\nHealth checks:")

    if not check_health():
        print("\nServices not healthy. Run: docker compose up --build -d")
        sys.exit(1)

    results = []

    if not agent_only:
        for test_file, label in [
            ("test_api_catalog.py", "API: Catalog"),
            ("test_api_balance.py", "API: Balance"),
            ("test_api_checkout.py", "API: Checkout Lifecycle"),
            ("test_api_refund.py", "API: Refunds"),
            ("test_api_policy.py", "API: Policy Enforcement"),
            ("test_api_edge_cases.py", "API: Edge Cases"),
        ]:
            results.append(run_pytest(test_file, label))

    if not api_only:
        results.append(run_pytest("test_agent_scenarios.py", "Agent: Simulations"))

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    total_passed = sum(r["passed"] for r in results)
    total_failed = sum(r["failed"] for r in results)
    total_errors = sum(r["errors"] for r in results)
    total_time = sum(r["elapsed_seconds"] for r in results)

    for r in results:
        status = "PASS" if r["exit_code"] == 0 else "FAIL"
        print(f"  [{status}] {r['label']}: {r['passed']} passed, {r['failed']} failed ({r['elapsed_seconds']}s)")
        for fl in r["failure_lines"]:
            print(f"         {fl}")

    print(f"\nTotal: {total_passed} passed, {total_failed} failed, {total_errors} errors in {total_time:.1f}s")

    if generate_report:
        report = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total_passed": total_passed,
            "total_failed": total_failed,
            "total_errors": total_errors,
            "elapsed_seconds": round(total_time, 1),
            "suites": results,
        }
        REPORT_PATH.write_text(json.dumps(report, indent=2))
        print(f"\nReport written to: {REPORT_PATH}")

    sys.exit(0 if total_failed == 0 and total_errors == 0 else 1)


if __name__ == "__main__":
    main()
