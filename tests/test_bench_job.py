import json
from pathlib import Path

from scripts import bench_job


def write_manifest(path: Path, job_id: str, output_dir: Path, stages: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "job_id": job_id,
        "effective_config": {"output_dir": str(output_dir)},
        "stages": stages,
    }), encoding="utf-8")
    return path


def fixture_runs(tmp_path):
    workspace = tmp_path / "workspace"
    before_output = workspace / "before-output"
    after_output = workspace / "after-output"
    before_output.mkdir(parents=True)
    after_output.mkdir(parents=True)
    manifests = workspace / "bot_system" / "manifests"
    before = write_manifest(manifests / "before.manifest.json", "before", before_output, {
        "extract_audio": {"status": "completed", "duration_s": 2.0},
        "visual_ocr": {"status": "completed", "duration_s": 3.0, "metrics": {"calls": 4}},
    })
    after = write_manifest(manifests / "after.manifest.json", "after", after_output, {
        "extract_audio": {"status": "completed", "duration_s": 2.0, "metrics": {"encoder": "n/a"}},
        "visual_ocr": {"status": "completed", "duration_s": 6.0, "metrics": {"calls": 7}},
    })
    return before, after, after_output


def test_table_prints_stage_rows_and_total(tmp_path, capsys):
    _, after, _ = fixture_runs(tmp_path)

    assert bench_job.main([str(after)]) == 0
    output = capsys.readouterr().out
    assert "Stage | Status | Duration (s) | Key metrics" in output
    assert "visual_ocr | completed | 6.000 | calls=7" in output
    assert "TOTAL | | 8.000 |" in output


def test_json_output_contains_stage_metrics_and_total(tmp_path, capsys):
    _, after, _ = fixture_runs(tmp_path)

    assert bench_job.main([str(after), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total_duration_s"] == 8.0
    assert report["stages"][1]["metrics"] == {"calls": 7}


def test_compare_resolves_job_output_directory_and_reports_deltas(tmp_path, capsys, monkeypatch):
    before, _, after_output = fixture_runs(tmp_path)

    assert bench_job.main([str(after_output), "--compare", str(before)]) == 0
    table = capsys.readouterr().out
    assert "Before (s) | After (s) | Delta (s) | Delta (%)" in table
    assert "visual_ocr | completed | 6.000 | calls=7 | 3.000 | 6.000 | 3.000 | 100.00%" in table
    assert "TOTAL | | 8.000 | | 5.000 | 8.000 | 3.000 | 60.00%" in table

    _, after, _ = fixture_runs(tmp_path / "json")
    assert bench_job.main([str(after), "--compare", str(before), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["compare"]["total"]["delta_s"] == 3.0
    assert report["compare"]["stages"][1]["delta_percent"] == 100.0


def test_compare_table_includes_stages_present_only_in_baseline(tmp_path, capsys):
    before_output = tmp_path / "before"
    after_output = tmp_path / "after"
    before_output.mkdir()
    after_output.mkdir()
    before = write_manifest(
        tmp_path / "before.manifest.json",
        "before",
        before_output,
        {"removed_stage": {"status": "completed", "duration_s": 4.0}},
    )
    after = write_manifest(
        tmp_path / "after.manifest.json",
        "after",
        after_output,
        {"added_stage": {"status": "completed", "duration_s": 2.0}},
    )

    assert bench_job.main([str(after), "--compare", str(before)]) == 0
    table = capsys.readouterr().out
    assert "removed_stage | missing | 0.000 |  | 4.000 | 0.000 | -4.000 | -100.00%" in table


def test_output_directory_resolves_from_configured_workspace(tmp_path, capsys, monkeypatch):
    workspace = tmp_path / "workspace"
    output_dir = tmp_path / "published" / "job-output"
    output_dir.mkdir(parents=True)
    manifest = write_manifest(
        workspace / "bot_system" / "manifests" / "job.manifest.json",
        "job",
        output_dir,
        {"video_rendering": {"status": "completed", "duration_s": 9.0}},
    )
    monkeypatch.setenv("AUTODUB_WORKSPACE", str(workspace))

    assert bench_job.main([str(output_dir), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["manifest"] == str(manifest)
    assert report["total_duration_s"] == 9.0


def test_relative_output_directory_deduplicates_manifest_candidates(tmp_path, capsys, monkeypatch):
    _, _, output_dir = fixture_runs(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTODUB_WORKSPACE", str(tmp_path / "workspace"))

    assert bench_job.main(["workspace/after-output", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total_duration_s"] == 8.0
    assert Path(report["manifest"]).resolve().name == "after.manifest.json"


def test_missing_manifest_exits_two_with_clear_message(tmp_path, capsys):
    missing = tmp_path / "not-here"

    assert bench_job.main([str(missing)]) == 2
    assert "manifest or job output directory does not exist" in capsys.readouterr().err
