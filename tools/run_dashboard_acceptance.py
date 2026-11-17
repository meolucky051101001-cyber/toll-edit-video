"""Run dashboard regression suites with explicit isolation and saved evidence."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
TESTS = [
    "tests/test_dashboard_batch_plans.py", "tests/test_dashboard_acceptance_api.py",
    "tests/test_dashboard_hardening.py", "tests/test_dashboard_and_tracker.py",
    "tests/test_queue_schedule_regressions.py", "tests/test_queue_control_followup.py",
    "tests/test_durable_queue_hardening.py", "tests/test_v1_queue_resilience.py",
    "tests/test_mixer_dashboard_integration.py",
    "tests/test_v1_telegram_orchestrator.py", "tests/test_v1_telegram_network_retry.py",
    "tests/test_stop_service.py", "tests/test_stop_flag_hygiene.py",
    "tests/test_v1_shutdown_scope.py", "tests/test_shutdown_review_regressions.py",
    "tests/test_recovery_service.py", "tests/test_v1_recovery_dispatch.py",
    "tests/test_v1_checkpoint.py", "tests/test_v1_checkpoint_orchestrator.py",
    "tests/test_tracker_path_binding.py",
    "tests/test_dashboard_worker_foundation.py", "tests/test_dashboard_worker_bridge.py",
    "tests/test_dashboard_gpu_foundation.py", "tests/test_dashboard_soak.py",
    "tests/test_dashboard_migration_rehearsal.py", "tests/test_phase_c_stability.py",
    "tests/test_v1_resource_control.py", "tests/test_v1_bs_roformer_audio.py",
    "tests/test_phase7_batch_contracts.py", "tests/test_phase7_orchestrator_golden.py",
    "tests/test_tracker_remediation.py", "tests/test_telegram_remediation.py",
    "tests/test_cloud_remediation.py", "tests/test_dashboard_batch_lifecycle.py",
    "tests/test_ocr_source_mask_contract.py",
]


def source_inventory():
    suffixes = {".py", ".html", ".js", ".cjs"}
    result = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for folder in ("backend", "tools", "tests") for path in sorted((ROOT / folder).rglob("*"))
            if path.is_file() and (path.suffix in suffixes or (folder == "tests" and path.suffix == ".json")) and not any(
                part in {"venv", "node_modules", "__pycache__", ".venv"} for part in path.parts)}
    for name in ("conftest.py", "requirements.txt", "requirements-dev.txt", "requirements.lock.txt"):
        path = ROOT / name
        if path.is_file():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "test_evidence" / "dashboard")
    parser.add_argument("--tests", nargs="*")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source_before = source_inventory()
    (args.output / "source-hashes.json").write_text(json.dumps(source_before, indent=2), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="dashboard_acceptance_") as temporary:
        env = dict(os.environ)
        for key, folder in {"AUTODUB_WORKSPACE": "workspace", "AUTODUB_INPUT_DIR": "input", "AUTODUB_OUTPUT_DIR": "output",
                            "AUTODUB_DATA_DIR": "data", "AUTODUB_MODEL_CACHE": "models", "AUTODUB_SHARED_ASSETS_DIR": "assets"}.items():
            env[key] = str(Path(temporary) / folder)
            Path(env[key]).mkdir()
        env.update(AUTODUB_TESTING="1", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", AUTODUB_LOCAL_TOKEN="isolated-acceptance-test-token")
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        started = time.monotonic()
        result = subprocess.run([sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", "--junitxml", str(args.output / "results.xml"), "-q", *(args.tests or TESTS)],
                                cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        (args.output / "pytest.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        (args.output / "run.json").write_text(json.dumps({"started_at": started_at,
                "duration_seconds": round(time.monotonic()-started, 3), "returncode": result.returncode,
                "isolated": True, "tests": args.tests or TESTS, "python": sys.version,
                "source_hashes": "source-hashes.json", "source_unchanged_during_run": source_before == source_inventory(),
                "production_render": False, "real_telegram_delivery": False, "soak_completed": False}, indent=2), encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        print(result.stdout[-18000:])
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
