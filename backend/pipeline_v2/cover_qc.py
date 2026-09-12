"""Check known source rectangles against emitted ASS covers, without model calls."""
import re


def seconds(value):
    h, m, s = value.split(':')
    return int(h)*3600 + int(m)*60 + float(s)


def parse_ass_covers(ass):
    width = re.search(r'^PlayResX:\s*(\d+)', ass, re.M)
    height = re.search(r'^PlayResY:\s*(\d+)', ass, re.M)
    if not width or not height:
        raise ValueError('ASS canvas is missing')
    w, h = int(width[1]), int(height[1])
    outline = 0.
    for line in ass.splitlines():
        if line.startswith('Style: BgStyle,'):
            style = line.split(':', 1)[1].strip().split(',')
            if len(style) > 16:
                outline = max(0., float(style[16]))
    covers = []
    for line in ass.splitlines():
        if not line.startswith('Dialogue:'):
            continue
        fields = line.split(',', 9)
        if len(fields) != 10 or fields[3] != 'BgStyle':
            continue
        if r'\an7' not in fields[9]:
            continue
        pos = re.search(r'\\pos\(([-\d.]+),([-\d.]+)\)', fields[9])
        drawing = re.search(r'\{\\p1\}(.*?)\{\\p0\}', fields[9])
        if not pos or not drawing:
            continue
        numbers = [float(x) for x in re.findall(r'-?\d+(?:\.\d+)?', drawing[1])]
        if len(numbers) < 4:
            continue
        x, y = float(pos[1]), float(pos[2])
        covers.append((seconds(fields[1]), seconds(fields[2]),
            x+min(numbers[::2])-outline, y+min(numbers[1::2])-outline,
            x+max(numbers[::2])+outline, y+max(numbers[1::2])+outline))
    return covers, w, h


def inspect_covers(segments, ass):
    covers, w, h = parse_ass_covers(ass)
    failures, unverified = [], []
    checked = 0
    for seg in segments:
        start, end = float(seg['start']), float(seg['end'])
        blocks = seg.get('tracking_blocks') or ([seg['best_block']] if seg.get('best_block') else [])
        if not blocks:
            unverified.append(seg.get('index'))
        for block in blocks:
            left, right = max(start,float(block['start'])), min(end,float(block['end']))
            if right-left <= .03:
                continue
            checked += 1
            valid = sorted((max(left,a), min(right,z)) for a,z,x,y,r,b in covers
                if z>left and a<right and x<=float(block['x_pct'])*w+2
                and r>=float(block['max_x_pct'])*w-2
                and y<=float(block['y_pct'])*h+2 and b>=float(block['max_y_pct'])*h-2)
            cursor, gap = left, 0.
            for a,z in valid:
                gap += max(0,a-cursor)
                cursor=max(cursor,z)
            gap += max(0,right-cursor)
            if gap > .03:
                failures.append(dict(segment=seg.get('index'),start=left,end=right,uncovered_seconds=round(gap,3)))
    return dict(checked_rectangles=checked, failures=failures, unverified_segments=unverified,
                diagnostic_times=sorted({round(f['start'],2) for f in failures})[:12])


def inspect_frame_pixel_coverage(frame_path, covers, canvas_w=1080, canvas_h=1920, timestamp=None):
    """Verify that active cover boxes contain valid background fill in rendered output pixels."""
    from pathlib import Path
    p = Path(frame_path)
    if not p.is_file():
        return {"checked": False, "reason": "frame_file_missing"}

    # Filter active covers if timestamp is provided
    active = covers
    if timestamp is not None:
        t = float(timestamp)
        active = [c for c in covers if c[0] <= t <= c[1]]
    if not active:
        return {"checked": False, "reason": "no_active_covers_at_timestamp"}

    try:
        from PIL import Image
        import numpy as np

        with Image.open(p) as img:
            img = img.convert("RGB")
            fw, fh = img.size
            arr = np.array(img)

        scale_x = fw / max(1, canvas_w)
        scale_y = fh / max(1, canvas_h)

        checked_boxes = []
        for cover in active:
            _, _, x1, y1, x2, y2 = cover
            px1 = max(0, min(fw - 1, int(x1 * scale_x)))
            py1 = max(0, min(fh - 1, int(y1 * scale_y)))
            px2 = max(0, min(fw, int(x2 * scale_x)))
            py2 = max(0, min(fh, int(y2 * scale_y)))
            if px2 <= px1 or py2 <= py1:
                continue

            patch = arr[py1:py2, px1:px2]
            # White sticker cover: check high brightness pixels
            is_white = (patch[:, :, 0] > 190) & (patch[:, :, 1] > 190) & (patch[:, :, 2] > 190)
            white_ratio = float(np.mean(is_white))
            checked_boxes.append({
                "bbox": [px1, py1, px2, py2],
                "white_ratio": round(white_ratio, 3),
                "has_cover_fill": white_ratio >= 0.35,
            })

        all_ok = all(b["has_cover_fill"] for b in checked_boxes) if checked_boxes else True
        return {
            "checked": True,
            "frame": p.name,
            "timestamp": timestamp,
            "boxes_checked": len(checked_boxes),
            "all_boxes_filled": all_ok,
            "details": checked_boxes,
        }
    except Exception as exc:
        return {"checked": False, "error": str(exc)}
