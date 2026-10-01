from pathlib import Path

cdx_dir = Path(r"C:\Users\admin\.codex\visualizations\2026\09\23\01a0cf5f-6ad4-7f43-8ec9-969da76c05bf\ui-redesign")

v1_html = (cdx_dir / "1_Tool_V1_dashboard.html").read_text(encoding="utf-8")
v2_html = (cdx_dir / "2_Tool_V2_dashboard.html").read_text(encoding="utf-8")
wf_html = (cdx_dir / "3_Quy_Trinh_workflow.html").read_text(encoding="utf-8")
sk_html = (cdx_dir / "4_Kich_Ban_script_studio.html").read_text(encoding="utf-8")

print("V1 length:", len(v1_html), "has __REPLACE_TOKEN__:", "__REPLACE_TOKEN__" in v1_html)
print("V2 length:", len(v2_html), "split script count:", len(v2_html.split("<script>", 1)))
print("V2 has TOOL V2:", "TOOL V2" in v2_html)
print("V2 has 8088:", "127.0.0.1:8088/" in v2_html)
print("V2 has 8089:", "127.0.0.1:8089/" in v2_html)
print('V2 has onclick=startBatch:', 'onclick="startBatch()"' in v2_html)
print("V2 has QC:", "QC" in v2_html)
print("V2 has logSearch:", "logSearch" in v2_html)
print("Workflow length:", len(wf_html))
print("Script studio length:", len(sk_html))
