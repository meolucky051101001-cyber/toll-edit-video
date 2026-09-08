"""Check known source rectangles against emitted ASS covers, without model calls."""
import re


def seconds(value):
    h, m, s = value.split(':')
    return int(h)*3600 + int(m)*60 + float(s)


def inspect_covers(segments, ass):
    width = re.search(r'^PlayResX:\s*(\d+)', ass, re.M)
    height = re.search(r'^PlayResY:\s*(\d+)', ass, re.M)
    if not width or not height:
        raise ValueError('ASS canvas is missing')
    w, h = int(width[1]), int(height[1])
    outline = 0.
    for line in ass.splitlines():
        if line.startswith('Style: BgStyle,'):
            style = line.split(':', 1)[1].strip().split(',')
            # ASS v4+ Outline is field 16. The generator subtracts this
            # border from its drawing dimensions, so QC must include it.
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
