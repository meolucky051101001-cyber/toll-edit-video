"""Offline NVENC render and subtitle visibility acceptance; creates a new output directory."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--baseline-style', action='store_true', help='Reproduce the previous unreadable style; expected to fail')
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    os.environ['AUTODUB_WORKSPACE'] = str(out / 'workspace')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
    import video_utils
    import cv2
    import numpy as np

    def run(cmd):
        return subprocess.run(cmd, check=True, capture_output=True, timeout=90)

    def frame(path, timestamp):
        data = run(['ffmpeg', '-v', 'error', '-ss', str(timestamp), '-i', str(path),
                    '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-']).stdout
        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)

    audio = out / 'audio.wav'
    run(['ffmpeg', '-v', 'error', '-n', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', str(audio)])
    subs = out / 'sample.srt'
    subs.write_text('1\n00:00:00,400 --> 00:00:01,600\nKiểm tra phụ đề tiếng Việt\n', encoding='utf-8')
    rows = []
    cases = [('landscape-light', 'white', '640x360', '&H00000000'),
             ('landscape-dark', 'black', '640x360', '&H00FFFFFF'),
             ('portrait-light', 'white', '360x640', '&H00000000'),
             ('portrait-dark', 'black', '360x640', '&H00FFFFFF'),
             ('black-on-black', 'black', '640x360', '&H00000000'),
             ('white-on-white', 'white', '640x360', '&H00FFFFFF'),
             ('two-lines', 'white', '640x360', '&H00000000'),
             ('ass-control', 'black', '360x640', '&H00FFFFFF')]
    for name, background, size, color in cases[:1] if args.baseline_style else cases:
        source, output = out / (name + '-source.mp4'), out / (name + '.mp4')
        subtitle = subs
        if name == 'two-lines':
            subtitle = out / 'two-lines.srt'
            subtitle.write_text('1\n00:00:00,400 --> 00:00:01,600\nKiểm tra phụ đề tiếng Việt\nDòng thứ hai: đủ dấu và rõ chữ\n', encoding='utf-8')
        if name == 'ass-control':
            subtitle = out / 'sample.ass'
            subtitle.write_text('[Script Info]\nScriptType: v4.00+\nPlayResX: 360\nPlayResY: 640\n'
                '[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
                'Style: Default,Arial,20,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,40,1\n'
                '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
                'Dialogue: 0,0:00:00.40,0:00:01.60,Default,,0,0,0,,Kiểm tra phụ đề tiếng Việt\n', encoding='utf-8')
        run(['ffmpeg', '-v', 'error', '-n', '-f', 'lavfi', '-i',
             f'color=c={background}:s={size}:r=25:d=2', '-c:v', 'libx264', str(source)])
        original_style = video_utils._srt_style
        def style(*a):
            if args.baseline_style:
                return f'FontName=Arial,FontSize=14,PrimaryColour={color},Bold=-1,Outline=2,Shadow=1,MarginV=40,BorderStyle=1'
            return original_style(*a)
        with patch.object(video_utils, 'clean_stale_temp_files'), patch.object(video_utils, '_srt_style', side_effect=style):
            ok = video_utils.process_video(source, subtitle, audio, output, font_color=color, delogo=False, timeout_seconds=60)
        assert ok, name
        before, during, after = [frame(output, t) for t in (.1, 1, 1.9)]
        delta = np.max(cv2.absdiff(before, during), axis=2) > 25
        ys, xs = np.where(delta)
        assert len(xs), 'No subtitle pixels: ' + name
        x0, x1, y0, y1 = xs.min(), xs.max()+1, ys.min(), ys.max()+1
        density = float(delta[y0:y1, x0:x1].mean())
        disappearance = float(np.mean(cv2.absdiff(before, after)))
        probe = json.loads(run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(output)]).stdout)
        streams = {s['codec_type'] for s in probe['streams']}
        run(['ffmpeg','-v','error','-i',str(output),'-f','null','-'])
        cv2.imwrite(str(out / (name + '-subtitle.png')), during[max(0,y0-10):y1+10, max(0,x0-10):x1+10])
        passed = (.05 < density < .72 and disappearance < 1 and x0 > 0 and y0 > 0
                  and x1 < during.shape[1] and y1 < during.shape[0]
                  and streams >= {'video','audio'} and abs(float(probe['format']['duration'])-2)<.2)
        rows.append(dict(case=name, density=density, disappearance=disappearance, passed=passed))
    report = dict(passed=all(r['passed'] for r in rows), cases=rows,
                  scope='Synthetic SRT visual and media checks, not full AI quality acceptance')
    (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
