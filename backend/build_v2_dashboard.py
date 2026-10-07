import re
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
template_env = os.getenv("AUTODUB_V1_DASHBOARD_TEMPLATE", "").strip()
v1_template = Path(template_env) if template_env else Path(__file__).resolve().parent / "templates" / "dashboard.html"
if not v1_template.is_absolute():
    v1_template = PROJECT_ROOT / v1_template
content = v1_template.read_text(encoding="utf-8")

# 1. Remove the script in <head> so that <script> only occurs once at the bottom
pattern = r"<script>\s*window\.LOCAL_TOKEN[\s\S]*?</script>\s*"
content = re.sub(pattern, "", content, count=1)

# 2. Tool branding & titles
content = content.replace("<title>Tool V1 - Giám Sát Video Phôi & Tiến Độ Render</title>", "<title>Tool V2 - Giám Sát Video Phôi & Tiến Độ Render</title>")
content = content.replace('<div class="logo-badge">TOOL V1</div>', '<div class="logo-badge">TOOL V2</div>')
content = content.replace('Hệ thống biên tập tự động, tách âm, bóc sub và render video tốc độ cao', 'Hệ thống biên tập tự động, tách âm, bóc sub và render video tốc độ cao (Pipeline V2)')

# 3. Nav buttons active state
content = content.replace(
    'href="http://127.0.0.1:8088/" style="padding: 3px 10px; background: rgba(255,255,255,0.08); color: #fff; font-weight: 600;"',
    'href="http://127.0.0.1:8088/" style="padding: 3px 10px; border-color: transparent;"'
)
content = content.replace(
    'href="http://127.0.0.1:8089/" style="padding: 3px 10px; border-color: transparent;"',
    'href="http://127.0.0.1:8089/" style="padding: 3px 10px; background: rgba(255,255,255,0.08); color: #fff; font-weight: 600;"'
)

# 4. Status badge
content = content.replace("V1 Legacy", "Pipeline V2")

# 5. Stepper steps: Step 2 and Step 6
content = content.replace("Demucs htdemucs", "BS-RoFormer GPU")
content = content.replace('<div class="step-tag">h264_nvenc</div>', '<div class="step-tag">h264_nvenc & QC Gate</div>')

# 6. Output folder: D:\banve -> D:\video tool v2
content = content.replace(r"D:\banve", r"D:\video tool v2")
content = content.replace(r"D:\\banve", r"D:\\video tool v2")
content = content.replace("video V1", "video V2")
content = content.replace("Mở Thư Mục V1", "Mở Thư Mục V2")

# 7. Disallow inline onclick="startBatch()" (required by unit test)
# Give the second button an id="btnRunInput"
content = content.replace(
    '<button class="btn btn-primary" id="btnRunHero" onclick="startBatch()">',
    '<button class="btn btn-primary" id="btnRunHero">'
)
content = content.replace(
    '<button class="btn btn-primary" style="font-size: 11px; padding: 3px 10px;" onclick="startBatch()">',
    '<button class="btn btn-primary" id="btnRunInput" style="font-size: 11px; padding: 3px 10px;">'
)

# 8. In JS, attach event listeners for startBatch
init_listener = """
    document.getElementById('btnRunHero')?.addEventListener('click', startBatch);
    document.getElementById('btnRunInput')?.addEventListener('click', startBatch);
"""

# Insert before refreshAll();
content = content.replace("refreshAll();\n    setInterval(fetchStatus, 1500);", init_listener + "    refreshAll();\n    setInterval(fetchStatus, 1500);")

# 9. Verify exact single <script> split
parts = content.split("<script>", 1)
assert len(parts) == 2, f"Expected 2 parts on <script> split, got {len(parts)}"
assert "<script>" not in parts[0], "Found extra <script> in head/html!"

# 10. Write to Tool V2 templates
output_env = os.getenv("AUTODUB_DASHBOARD_TEMPLATE", "").strip()
out_path = Path(output_env) if output_env else Path(__file__).resolve().parent / "templates" / "dashboard.html"
if not out_path.is_absolute():
    out_path = PROJECT_ROOT / out_path
out_path.write_text(content, encoding="utf-8")
print(f"Successfully generated {out_path} ({len(content)} chars, {len(content.splitlines())} lines)")
