# -*- coding: utf-8 -*-
"""End-to-end test for Phase 3 Scene Composer in Tool V2."""

import os
import sys
import time
import tempfile
from pathlib import Path
import numpy as np
from PIL import Image

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# Ensure backend root is on path
backend_dir = Path(__file__).parent.resolve()
sys.path.insert(0, str(backend_dir))

from fastapi.testclient import TestClient
from dashboard_monitor import app

client = TestClient(app)

def test_scene_composer():
    print("=== Testing Scene Composer API ===")
    
    # 1. Test motions endpoint
    res = client.get("/api/scene-composer/motions")
    assert res.status_code == 200, f"Status code: {res.status_code}"
    data = res.json()
    assert data["status"] == "success"
    assert len(data["motions"]) >= 8
    print(f"[OK] Motions endpoint returned {len(data['motions'])} motions.")

    # 2. Setup temp assets (synthetic frames + synthetic audio + srt)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        scenes_dir = tmp_path / "scenes"
        scenes_dir.mkdir()

        colors = [(220, 50, 50), (50, 180, 50), (50, 100, 220)]
        for i, c in enumerate(colors):
            img = Image.new("RGB", (720, 1280), color=c)
            img.save(scenes_dir / f"scene_{i+1:02d}.jpg")

        audio_path = tmp_path / "speech.wav"
        srt_path = tmp_path / "subtitles.srt"

        # Generate 4-second WAV
        import wave, struct
        sample_rate = 16000
        dur_sec = 4.5
        n_samples = int(sample_rate * dur_sec)
        with wave.open(str(audio_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            raw = [int(1000 * np.sin(2 * np.pi * 440 * t / sample_rate)) for t in range(n_samples)]
            wf.writeframes(struct.pack(f"<{len(raw)}h", *raw))

        # Generate sample SRT
        srt_content = """1
00:00:00,000 --> 00:00:01,500
Chao mung den voi Tool V2.

2
00:00:01,500 --> 00:00:03,000
Tinh nang Scene Composer AI sieu muot.

3
00:00:03,000 --> 00:00:04,500
Camera perspective sub-pixel easing.
"""
        srt_path.write_text(srt_content, encoding="utf-8")

        # 3. Test preview timeline endpoint
        preview_req = {
            "image_dir": str(scenes_dir),
            "audio_path": str(audio_path),
            "srt_path": str(srt_path),
            "default_motion": "auto"
        }
        res = client.post("/api/scene-composer/preview-timeline", json=preview_req)
        assert res.status_code == 200, f"Preview failed: {res.text}"
        prev_data = res.json()
        assert prev_data["status"] == "success"
        assert prev_data["total_scenes"] == 3
        print(f"[OK] Preview timeline returned {prev_data['total_scenes']} scenes, duration: {prev_data['audio_duration']}s")
        for item in prev_data["timeline"]:
            print(f"     - Scene {item['scene_index']}: {item['start_time']}s -> {item['end_time']}s | {item['motion_type']}")

        # 4. Test render endpoint
        out_video = tmp_path / "final_output.mp4"
        render_req = {
            "image_dir": str(scenes_dir),
            "audio_path": str(audio_path),
            "srt_path": str(srt_path),
            "output_path": str(out_video),
            "aspect_ratio": "9:16",
            "default_motion": "zoom_in",
            "fps": 24
        }
        res = client.post("/api/scene-composer/render", json=render_req)
        assert res.status_code == 200, f"Render failed: {res.text}"
        task_id = res.json()["task_id"]
        print(f"[OK] Render started with task_id: {task_id}")

        # Poll status
        max_wait = 60
        start_t = time.time()
        completed = False
        while time.time() - start_t < max_wait:
            st_res = client.get(f"/api/scene-composer/status/{task_id}")
            assert st_res.status_code == 200
            st = st_res.json()
            pct = st.get("percent", 0)
            status = st.get("status")
            print(f"     Poll status: {status} ({pct}%) - {st.get('status_text')}")
            if status == "completed":
                completed = True
                break
            elif status == "error":
                raise RuntimeError(f"Render task failed: {st.get('error')}")
            time.sleep(1)

        assert completed, "Render task timed out"
        assert out_video.is_file(), f"Output video {out_video} does not exist"
        size = out_video.stat().st_size
        assert size > 10000, f"Output video is too small ({size} bytes)"
        print(f"[OK] Render completed successfully! Video file size: {size:,} bytes.")

    print("=== All Phase 3 E2E Tests PASSED! ===")

if __name__ == "__main__":
    test_scene_composer()
