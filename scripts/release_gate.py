#!/usr/bin/env python3
"""Release Gate & Automated Quality Verification Harness.

Evaluates all system invariants before production deployment:
1. Environment & Dependencies Check
2. Database Schema Integrity Check
3. Ruff Code Quality Linter
4. Mypy Static Type Checker
5. Frontend ESLint Quality Check
6. Frontend TypeScript Verification (tsc --noEmit)
7. Frontend Production Build Check
8. Backend Pytest Regression Suite
9. Evaluation Benchmark
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def print_banner(title: str) -> None:
    print("\n" + "=" * 75)
    print(f" {title.upper()}")
    print("=" * 75)


def check_environment() -> tuple[bool, str]:
    v = sys.version_info
    if v.major != 3 or v.minor < 11:
        return False, f"Python version {v.major}.{v.minor} is below 3.11"
    return True, f"Python {v.major}.{v.minor}.{v.micro} verified"


def check_database() -> tuple[bool, str]:
    from sqlalchemy import text

    from backend.core.database import Base, SessionLocal, engine

    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        res = db.execute(text("SELECT 1")).scalar()
        if res != 1:
            return False, "Database ping failed"

        table_rows = db.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
        existing = {r[0] for r in table_rows}
        required = {"videos", "search_jobs", "job_videos", "dubbing_jobs", "video_metric_snapshots"}
        missing = required - existing
        if missing:
            return False, f"Missing required database tables: {missing}"

    return True, f"SQLite database schema verified ({len(required)} core tables present)"


def run_command(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd or ROOT_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout + "\n" + p.stderr).strip()
    return p.returncode, out


def parse_pytest_summary(output: str) -> dict[str, int]:
    match = re.search(r"(\d+)\s+passed", output)
    passed = int(match.group(1)) if match else 0
    fail_match = re.search(r"(\d+)\s+failed", output)
    failed = int(fail_match.group(1)) if fail_match else 0
    return {"passed": passed, "failed": failed}


def main() -> int:
    print_banner("AI Video Research Tool - Comprehensive Release Gate (9 Stages)")
    start_time = time.time()
    results = {}
    all_passed = True
    npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
    npx_cmd = "npx.cmd" if os.name == "nt" else "npx"
    python_bin = sys.executable

    # Stage 1: Runtime Environment
    print("[1/9] Checking runtime environment...", end=" ", flush=True)
    t0 = time.time()
    env_ok, env_msg = check_environment()
    t_env = round(time.time() - t0, 2)
    results["1_environment"] = {"passed": env_ok, "detail": env_msg, "duration_s": t_env}
    if env_ok:
        print(f"OK ({env_msg}) [{t_env}s]")
    else:
        print(f"FAIL ({env_msg})")
        all_passed = False

    # Stage 2: Database Schema & Core Tables
    print("[2/9] Checking database schema & core tables...", end=" ", flush=True)
    t0 = time.time()
    try:
        db_ok, db_msg = check_database()
        t_db = round(time.time() - t0, 2)
        results["2_database"] = {"passed": db_ok, "detail": db_msg, "duration_s": t_db}
        if db_ok:
            print(f"OK ({db_msg}) [{t_db}s]")
        else:
            print(f"FAIL ({db_msg})")
            all_passed = False
    except Exception as exc:
        t_db = round(time.time() - t0, 2)
        results["2_database"] = {"passed": False, "detail": str(exc), "duration_s": t_db}
        print(f"FAIL ({exc})")
        all_passed = False

    # Stage 3: Ruff Code Quality Linter
    print("[3/9] Running Ruff code linter...", end=" ", flush=True)
    t0 = time.time()
    code, out = run_command([python_bin, "-m", "ruff", "check", "backend/", "tests/", "scripts/"])
    t_ruff = round(time.time() - t0, 2)
    ruff_ok = (code == 0)
    results["3_ruff_linter"] = {"passed": ruff_ok, "exit_code": code, "duration_s": t_ruff}
    if ruff_ok:
        print(f"PASS (0 lint errors) [{t_ruff}s]")
    else:
        print(f"FAIL (exit code {code})")
        print(out[:500])
        all_passed = False

    # Stage 4: Mypy Static Type Checker
    print("[4/9] Running Mypy static type checker...", end=" ", flush=True)
    t0 = time.time()
    code, out = run_command([python_bin, "-m", "mypy", "backend"])
    t_mypy = round(time.time() - t0, 2)
    mypy_ok = (code == 0)
    results["4_mypy_types"] = {"passed": mypy_ok, "exit_code": code, "duration_s": t_mypy}
    if mypy_ok:
        print(f"PASS (0 type errors across backend) [{t_mypy}s]")
    else:
        print(f"FAIL (exit code {code})")
        print(out[:500])
        all_passed = False

    # Stage 5: Frontend ESLint
    print("[5/9] Running Frontend ESLint...", end=" ", flush=True)
    t0 = time.time()
    code, out = run_command([npm_cmd, "run", "lint"], cwd=ROOT_DIR / "frontend")
    t_eslint = round(time.time() - t0, 2)
    eslint_ok = (code == 0)
    results["5_frontend_eslint"] = {"passed": eslint_ok, "exit_code": code, "duration_s": t_eslint}
    if eslint_ok:
        print(f"PASS (0 lint errors/warnings) [{t_eslint}s]")
    else:
        print(f"FAIL (exit code {code})")
        print(out[:500])
        all_passed = False

    # Stage 6: Frontend TypeScript Check
    print("[6/9] Running Frontend TypeScript check (tsc --noEmit)...", end=" ", flush=True)
    t0 = time.time()
    code, out = run_command([npx_cmd, "tsc", "--noEmit"], cwd=ROOT_DIR / "frontend")
    t_tsc = round(time.time() - t0, 2)
    tsc_ok = (code == 0)
    results["6_frontend_typecheck"] = {"passed": tsc_ok, "exit_code": code, "duration_s": t_tsc}
    if tsc_ok:
        print(f"PASS (0 type errors) [{t_tsc}s]")
    else:
        print(f"FAIL (exit code {code})")
        print(out[:500])
        all_passed = False

    # Stage 7: Frontend Production Build
    print("[7/9] Verifying Frontend Production build...", end=" ", flush=True)
    t0 = time.time()
    code, out = run_command([npm_cmd, "run", "build"], cwd=ROOT_DIR / "frontend")
    t_build = round(time.time() - t0, 2)
    build_ok = (code == 0)
    results["7_frontend_build"] = {"passed": build_ok, "exit_code": code, "duration_s": t_build}
    if build_ok:
        print(f"PASS (Production bundle built successfully) [{t_build}s]")
    else:
        print(f"FAIL (exit code {code})")
        print(out[:500])
        all_passed = False

    # Stage 8: Backend Pytest Regression Suite
    print("[8/9] Running Backend Pytest regression suite...", end=" ", flush=True)
    t0 = time.time()
    code, out = run_command([python_bin, "-m", "pytest", "-q"])
    t_pytest = round(time.time() - t0, 2)
    test_ok = (code == 0)
    summary = parse_pytest_summary(out)
    results["8_backend_pytest"] = {
        "passed": test_ok,
        "exit_code": code,
        "tests_passed": summary["passed"],
        "tests_failed": summary["failed"],
        "duration_s": t_pytest,
    }
    if test_ok:
        print(f"PASS ({summary['passed']} tests passed, 0 failed) [{t_pytest}s]")
    else:
        print(f"FAIL ({summary['failed']} failed out of {summary['passed'] + summary['failed']})")
        print(out[:600])
        all_passed = False

    # Stage 9: Evaluation Benchmark
    print("[9/9] Running evaluation benchmark (60 queries across 5 domains)...", end=" ", flush=True)
    t0 = time.time()
    code, out = run_command([python_bin, "-m", "pytest", "tests/test_evaluation_benchmark.py", "-q"])
    t_bench = round(time.time() - t0, 2)
    bench_ok = (code == 0)
    bench_summary = parse_pytest_summary(out)
    results["9_evaluation_benchmark"] = {
        "passed": bench_ok,
        "exit_code": code,
        "queries_verified": bench_summary["passed"],
        "duration_s": t_bench,
    }
    if bench_ok:
        print(f"PASS ({bench_summary['passed']} benchmark test cases passed) [{t_bench}s]")
    else:
        print(f"FAIL (exit code {code})")
        print(out[:500])
        all_passed = False

    total_duration = round(time.time() - start_time, 2)
    verdict = "APPROVED (GREEN)" if all_passed else "REJECTED (RED)"
    print_banner(f"Release Gate Verdict: {verdict}")
    print(f"Total verification time: {total_duration}s across 9 automated stages")

    report_path = ROOT_DIR / "release_gate_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "verdict": "APPROVED" if all_passed else "REJECTED",
                "duration_seconds": total_duration,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "stages": results,
            },
            f,
            indent=2,
        )
    print(f"Release scorecard saved to: {report_path}\n")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
