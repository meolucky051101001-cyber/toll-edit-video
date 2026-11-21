import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

rep = json.loads(Path(r"D:\workspace_v2\canary_batch_runs\canary_batch_20_report.json").read_text(encoding="utf-8"))
for v in rep["videos"]:
    jid = v["job_id"]
    base = Path(r"D:\workspace_v2") / jid / "pipeline_v2" / "artifacts"
    qcf = base / "qc" / "qc_report.json"
    ttsf = base / "tts" / "segments.json"
    if qcf.is_file() and ttsf.is_file():
        q = json.loads(qcf.read_text(encoding="utf-8"))
        t = json.loads(ttsf.read_text(encoding="utf-8"))["runtime_segments"]
        seg_map = {s["index"]: s for s in t}
        for c in q.get("checks", []):
            if c.get("name") == "source_cover" and c.get("status") == "error":
                print("===", v["video_name"], "source_cover errors ===")
                for fail in c.get("metrics", {}).get("failures", []):
                    s_id = fail.get("segment")
                    s_obj = seg_map.get(s_id, {})
                    is_gap = s_obj.get("is_gap_reconciled")
                    text = s_obj.get("orig_content")
                    dur = s_obj.get("end", 0) - s_obj.get("start", 0)
                    uncovered = fail.get("uncovered_seconds")
                    print(f"   Seg {s_id}: is_gap={is_gap}, text={text}, dur={dur:.2f}s, uncovered={uncovered}s")
