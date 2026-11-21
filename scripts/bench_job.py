#!/usr/bin/env python3
"""Print stage timings and metrics from a Tool V1 job manifest."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path


def _clean_job_id(job_id: str) -> str:
    return "".join(char for char in job_id if char.isalnum() or char in ("-", "_")).strip()[:120]


def _path_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _candidate_manifest_files(job_dir: Path) -> list[Path]:
    roots = []
    if job_dir.name.endswith("manifests"):
        roots.append(job_dir)
    else:
        roots.extend((job_dir / "bot_system" / "manifests",
                      job_dir.parent / "bot_system" / "manifests"))
        roots.extend(parent / "bot_system" / "manifests" for parent in job_dir.parents)
        roots.append(Path(__file__).resolve().parent.parent / "workspace" / "bot_system" / "manifests")
        configured_workspace = os.getenv("AUTODUB_WORKSPACE")
        if configured_workspace:
            roots.append(Path(configured_workspace) / "bot_system" / "manifests")

    files = {}
    clean_id = _clean_job_id(job_dir.name)
    for root in roots:
        if root.is_dir():
            for candidate in root.glob("*.manifest.json"):
                files.setdefault(candidate.resolve(), candidate)
    expected_name = f"{clean_id}.manifest.json"
    for root in roots:
        expected = root / expected_name
        if expected.is_file():
            files.setdefault(expected.resolve(), expected)
    return sorted(files.values(), key=lambda path: str(path.resolve()))


def _read_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise TypeError(f"invalid manifest format: {path}")
    return data


def _resolve_manifest(argument: str | Path) -> tuple[Path, dict]:
    path = Path(argument)
    if path.is_file():
        return path, _read_manifest(path)
    if not path.exists():
        raise FileNotFoundError(f"manifest or job output directory does not exist: {path}")
    if not path.is_dir():
        raise FileNotFoundError(f"manifest or job output directory does not exist: {path}")

    candidates = _candidate_manifest_files(path)
    matches = []
    target_key = _path_key(path)
    target_job_id = path.name
    for candidate in candidates:
        try:
            data = _read_manifest(candidate)
        except ValueError:
            continue
        output_dir = (data.get("effective_config") or {}).get("output_dir")
        if (
            (output_dir and _path_key(output_dir) == target_key)
            or data.get("job_id") == target_job_id
            or candidate.name == f"{_clean_job_id(target_job_id)}.manifest.json"
        ):
            matches.append((candidate, data))
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise FileNotFoundError(f"multiple manifests match job output directory: {path}")
    raise FileNotFoundError(f"no manifest found for job output directory: {path}")


def _duration(stage: dict) -> float:
    try:
        value = float(stage.get("duration_s", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _stage_rows(manifest: dict) -> list[dict]:
    stages = manifest.get("stages")
    if not isinstance(stages, dict):
        return []
    rows = []
    for name, stage in stages.items():
        if not isinstance(stage, dict):
            continue
        metrics = stage.get("metrics")
        rows.append({
            "stage": name,
            "status": stage.get("status", "unknown"),
            "duration_s": _duration(stage),
            "metrics": metrics if isinstance(metrics, dict) else {},
        })
    return rows


def _compare_rows(before_rows: list[dict], after_rows: list[dict]) -> list[dict]:
    before = {row["stage"]: row["duration_s"] for row in before_rows}
    after = {row["stage"]: row["duration_s"] for row in after_rows}
    rows = []
    for stage in dict.fromkeys([*before, *after]):
        before_s = before.get(stage, 0.0)
        after_s = after.get(stage, 0.0)
        delta_s = after_s - before_s
        rows.append({
            "stage": stage,
            "before_s": round(before_s, 3),
            "after_s": round(after_s, 3),
            "delta_s": round(delta_s, 3),
            "delta_percent": round(delta_s / before_s * 100.0, 2) if before_s else None,
        })
    return rows


def _report(path: Path, manifest: dict, compare: tuple[Path, dict] | None = None) -> dict:
    rows = _stage_rows(manifest)
    report = {
        "manifest": str(path),
        "job_id": manifest.get("job_id"),
        "stages": rows,
        "total_duration_s": round(sum(row["duration_s"] for row in rows), 3),
    }
    if compare is not None:
        before_path, before_manifest = compare
        before_rows = _stage_rows(before_manifest)
        comparison_rows = _compare_rows(before_rows, rows)
        before_total = round(sum(row["duration_s"] for row in before_rows), 3)
        after_total = report["total_duration_s"]
        delta = round(after_total - before_total, 3)
        report["compare"] = {
            "manifest": str(before_path),
            "stages": comparison_rows,
            "total": {
                "before_s": before_total,
                "after_s": after_total,
                "delta_s": delta,
                "delta_percent": round(delta / before_total * 100.0, 2) if before_total else None,
            },
        }
    return report


def _format_metrics(metrics: dict) -> str:
    return ", ".join(
        f"{key}={json.dumps(value, ensure_ascii=False, sort_keys=True)}"
        for key, value in metrics.items()
        if value is None or isinstance(value, (str, int, float, bool, dict))
    )


def _print_table(report: dict) -> None:
    if "compare" in report:
        print("Stage | Status | Duration (s) | Key metrics | Before (s) | After (s) | Delta (s) | Delta (%)")
        comparisons = {row["stage"]: row for row in report["compare"]["stages"]}
        after_rows = {row["stage"]: row for row in report["stages"]}
        for stage, comparison in comparisons.items():
            row = after_rows.get(stage)
            status = row["status"] if row else "missing"
            duration_s = row["duration_s"] if row else 0.0
            metrics = row["metrics"] if row else {}
            percent = "n/a" if comparison["delta_percent"] is None else f'{comparison["delta_percent"]:.2f}%'
            print(
                f'{stage} | {status} | {duration_s:.3f} | '
                f'{_format_metrics(metrics)} | {comparison["before_s"]:.3f} | '
                f'{comparison["after_s"]:.3f} | {comparison["delta_s"]:.3f} | {percent}'
            )
        total = report["compare"]["total"]
        percent = "n/a" if total["delta_percent"] is None else f'{total["delta_percent"]:.2f}%'
        print(
            f'TOTAL | | {report["total_duration_s"]:.3f} | | {total["before_s"]:.3f} | '
            f'{total["after_s"]:.3f} | {total["delta_s"]:.3f} | {percent}'
        )
        return

    print("Stage | Status | Duration (s) | Key metrics")
    for row in report["stages"]:
        print(
            f'{row["stage"]} | {row["status"]} | {row["duration_s"]:.3f} | '
            f'{_format_metrics(row["metrics"])}'
        )
    print(f'TOTAL | | {report["total_duration_s"]:.3f} |')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", help="manifest JSON file or job output directory")
    parser.add_argument("--compare", help="manifest JSON file or job output directory to compare")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)
    try:
        path, manifest = _resolve_manifest(args.job)
        comparison = _resolve_manifest(args.compare) if args.compare else None
    except (FileNotFoundError, TypeError, ValueError) as exc:
        print(f"bench_job: {exc}", file=sys.stderr)
        return 2

    report = _report(path, manifest, comparison)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
