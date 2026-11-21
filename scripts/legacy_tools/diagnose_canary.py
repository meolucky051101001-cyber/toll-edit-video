import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

rep = Path(r"D:\workspace_v2\canary_batch_runs\canary_batch_20_report.json")
if not rep.is_file():
    print("Report file not found")
    sys.exit(1)

data = json.loads(rep.read_text(encoding="utf-8"))
for v in data["videos"]:
    jid = v["job_id"]
    base = Path(r"D:\workspace_v2") / jid / "pipeline_v2"
    qcf = base / "artifacts" / "qc" / "qc_report.json"
    recf = base / "artifacts" / "ocr" / "gap_reconciliation.json"
    rec_count = 0
    spoken_rec = 0
    unvoiced_rec = 0
    if recf.is_file():
        rdata = json.loads(recf.read_text(encoding="utf-8"))
        rec_count = rdata.get("added_segments_count", 0)
        spoken_rec = rdata.get("spoken_dialogue_count", 0)
        unvoiced_rec = rdata.get("unvoiced_scene_text_count", 0)

    if qcf.is_file():
        q = json.loads(qcf.read_text(encoding="utf-8"))
        err_details = {}
        for c in q.get("checks", []):
            if c.get("status") == "error":
                err_details[c["name"]] = c.get("metrics", {})
        err_keys = list(err_details.keys())
        print(f"{v['index']:2d}. {v['video_name']:<16} | AddedGaps: {rec_count:2d} (spoken={spoken_rec}, unvoiced={unvoiced_rec}) | Errors: {err_keys}")
        for ename, emet in err_details.items():
            if ename == "segment_timing":
                overflows = emet.get("timing_overflow_seconds") or emet.get("overflow_seconds") or {}
                print(f"    [timing] overflow count={len(overflows)}: {overflows}")
            elif ename == "source_cover":
                fails = emet.get("failures", [])
                print(f"    [source_cover] failure count={len(fails)}: {fails[:2]}")
            elif ename == "pixel_cover_qc":
                uncovered = emet.get("uncovered_source_frames", 0)
                reason = emet.get("reason", "")
                failed_items = emet.get("failed_items", [])
                print(f"    [pixel_cover_qc] uncovered_frames={uncovered}, reason={reason}")
    else:
        print(f"{v['index']:2d}. {v['video_name']:<16} | NO QC FILE: {v.get('reason')}")
