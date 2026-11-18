"""Pure hardware-independent subtitle OCR track coalescing, bridging, and speech-alignment kernel.

Shared across Local V1 (`ocr_utils`) and Cloud V1 (`cloud/gpu_scripts`) with zero
OpenCV / ONNX / PyTorch runtime dependencies.
"""
from __future__ import annotations

import copy
import sys
from bisect import bisect_left
from datetime import timedelta
from difflib import SequenceMatcher


def _ocr_field(block, name, default=None):
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _set_ocr_field(block, name, value):
    if isinstance(block, dict):
        block[name] = value
    else:
        setattr(block, name, value)


def _visual_chinese_count(text):
    return sum(1 for char in str(text or "") if "\u3400" <= char <= "\u9fff")


def _visual_watermark_text(text):
    raw = str(text or "")
    if raw.lstrip().startswith(("@", "＠")):
        return True
    return any(token in raw for token in ("抖音", "小红书", "快手", "关注", "点赞", "收藏"))


def _visual_caption_key(text):
    return "".join(
        char.lower()
        for char in str(text or "")
        if char.isalnum() or "\u3400" <= char <= "\u9fff"
    )


def _same_visual_caption(first, second, threshold=0.78):
    """Match OCR jitter and typing effects without joining different captions."""
    left = _visual_caption_key(first)
    right = _visual_caption_key(second)
    if not left or not right:
        return False
    if left == right:
        return True
    if SequenceMatcher(None, left, right, autojunk=False).ratio() >= threshold:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 2 and longer.startswith(shorter)


def _has_chinese_overlap(candidate_text: str, segment_text: str) -> bool:
    """Check whether OCR and ASR share meaningful Chinese characters."""
    candidate = "".join(char for char in str(candidate_text or "") if "\u3400" <= char <= "\u9fff")
    segment = "".join(char for char in str(segment_text or "") if "\u3400" <= char <= "\u9fff")
    if not candidate or not segment:
        return False
    shared = set(candidate) & set(segment)
    if len(shared) >= 2 or (len(candidate) >= 2 and len(shared) / len(candidate) >= 0.3):
        return True
    return SequenceMatcher(None, candidate, segment).ratio() >= 0.3


def stabilize_samples(samples):
    """Lock small OCR jitter without freezing real subtitle motion."""
    result = [dict(row) for row in samples]
    groups = []
    keys = ("x_pct", "max_x_pct", "y_pct", "max_y_pct")
    for row in result:
        group = groups[-1] if groups else []
        compatible = bool(group) and row["sample_time"] - group[-1]["sample_time"] <= .75001
        if compatible:
            for key in keys:
                tolerance = .025 if "x_pct" in key else .012
                values = [item[key] for item in group] + [row[key]]
                if max(values) - min(values) > tolerance:
                    compatible = False
                    break
        if compatible:
            group.append(row)
        else:
            groups.append([row])
    for group in groups:
        bounds = {key: (max if key.startswith("max_") else min)(
            row[key] for row in group) for key in keys}
        for row in group:
            row.update(bounds)
    return result


def _merge_colinear_frame_blocks(blocks):
    """Merge horizontal fragments of the same caption row within one slice/frame."""
    if len(blocks) <= 1:
        return [copy.copy(b) for b in blocks]
    items = [
        copy.copy(b)
        for b in sorted(
            blocks,
            key=lambda b: (
                float(_ocr_field(b, "y_pct", 0.0) or 0.0),
                float(_ocr_field(b, "x_pct", 0.0) or 0.0),
            ),
        )
    ]
    changed = True
    while changed:
        changed = False
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                ay1 = float(_ocr_field(a, "y_pct", 0.0) or 0.0)
                ay2 = float(_ocr_field(a, "max_y_pct", 0.0) or 0.0)
                by1 = float(_ocr_field(b, "y_pct", 0.0) or 0.0)
                by2 = float(_ocr_field(b, "max_y_pct", 0.0) or 0.0)
                ha, hb = ay2 - ay1, by2 - by1
                if ha <= 0.0 or hb <= 0.0:
                    continue
                cy_a, cy_b = (ay1 + ay2) * 0.5, (by1 + by2) * 0.5
                ax1 = float(_ocr_field(a, "x_pct", 0.0) or 0.0)
                ax2 = float(_ocr_field(a, "max_x_pct", 0.0) or 0.0)
                bx1 = float(_ocr_field(b, "x_pct", 0.0) or 0.0)
                bx2 = float(_ocr_field(b, "max_x_pct", 0.0) or 0.0)
                gap_x = max(0.0, bx1 - ax2, ax1 - bx2)
                if (
                    abs(cy_a - cy_b) <= min(0.016, max(ha, hb) * 0.35)
                    and gap_x <= 0.045
                    and max(ha, hb) <= min(ha, hb) * 1.8
                ):
                    ta = str(_ocr_field(a, "text", "") or "")
                    tb = str(_ocr_field(b, "text", "") or "")
                    left_t, right_t = (ta, tb) if ax1 <= bx1 else (tb, ta)
                    sep = (
                        " "
                        if (
                            left_t
                            and right_t
                            and left_t[-1].isascii()
                            and left_t[-1].isalnum()
                            and right_t[0].isascii()
                            and right_t[0].isalnum()
                        )
                        else ""
                    )
                    _set_ocr_field(a, "x_pct", min(ax1, bx1))
                    _set_ocr_field(a, "max_x_pct", max(ax2, bx2))
                    _set_ocr_field(a, "y_pct", min(ay1, by1))
                    _set_ocr_field(a, "max_y_pct", max(ay2, by2))
                    _set_ocr_field(a, "text", f"{left_t}{sep}{right_t}".strip())
                    pa = float(_ocr_field(a, "prob", 0.0) or 0.0)
                    pb = float(_ocr_field(b, "prob", 0.0) or 0.0)
                    _set_ocr_field(a, "prob", max(pa, pb))
                    for color_key in ("bg_color_ass", "text_contrast_color_ass"):
                        if not _ocr_field(a, color_key, None) and _ocr_field(b, color_key, None):
                            _set_ocr_field(a, color_key, _ocr_field(b, color_key, None))
                    items.pop(j)
                    changed = True
                    break
            if changed:
                break
    return items


def _coalesce_stationary_source_tracks(tracks, max_chunk_s=0.9):
    """Coalesce touching micro-intervals of a stationary caption without bridging empty gaps."""
    if not tracks:
        return []
    by_slice = {}
    for b in tracks:
        s = float(_ocr_field(b, "start", 0.0) or 0.0)
        e = float(_ocr_field(b, "end", 0.0) or 0.0)
        by_slice.setdefault((round(s, 6), round(e, 6)), []).append(b)
    merged_slices = []
    for key in sorted(by_slice):
        merged_slices.extend(_merge_colinear_frame_blocks(by_slice[key]))
    ordered = sorted(
        merged_slices,
        key=lambda b: (
            float(_ocr_field(b, "start", 0.0) or 0.0),
            float(_ocr_field(b, "end", 0.0) or 0.0),
            float(_ocr_field(b, "y_pct", 0.0) or 0.0),
            float(_ocr_field(b, "x_pct", 0.0) or 0.0),
        ),
    )
    active = []
    finished = []
    for cur in ordered:
        still_open = []
        matched = False
        cur_s = float(_ocr_field(cur, "start", 0.0) or 0.0)
        cur_e = float(_ocr_field(cur, "end", 0.0) or 0.0)
        cur_x1 = float(_ocr_field(cur, "x_pct", 0.0) or 0.0)
        cur_x2 = float(_ocr_field(cur, "max_x_pct", 0.0) or 0.0)
        cur_y1 = float(_ocr_field(cur, "y_pct", 0.0) or 0.0)
        cur_y2 = float(_ocr_field(cur, "max_y_pct", 0.0) or 0.0)
        cur_text = str(_ocr_field(cur, "text", "") or "")
        cur_prob = float(_ocr_field(cur, "prob", 0.0) or 0.0)
        k2 = _visual_caption_key(cur_text)
        for prev in active:
            prev_s = float(_ocr_field(prev, "start", 0.0) or 0.0)
            prev_e = float(_ocr_field(prev, "end", 0.0) or 0.0)
            if prev_e < cur_s - 1e-5:
                finished.append(prev)
                continue
            if not matched and cur_s <= prev_e + 1e-5:
                prev_x1 = float(_ocr_field(prev, "x_pct", 0.0) or 0.0)
                prev_x2 = float(_ocr_field(prev, "max_x_pct", 0.0) or 0.0)
                prev_y1 = float(_ocr_field(prev, "y_pct", 0.0) or 0.0)
                prev_y2 = float(_ocr_field(prev, "max_y_pct", 0.0) or 0.0)
                edge_diff = max(
                    abs(prev_x1 - cur_x1),
                    abs(prev_x2 - cur_x2),
                    abs(prev_y1 - cur_y1),
                    abs(prev_y2 - cur_y2),
                )
                y_diff = max(abs(prev_y1 - cur_y1), abs(prev_y2 - cur_y2))
                k1 = _visual_caption_key(_ocr_field(prev, "text", ""))
                same_caption = bool(k1 and k2) and (
                    k1 == k2
                    or (
                        edge_diff <= 0.015
                        and min(len(k1), len(k2)) >= 4
                        and SequenceMatcher(None, k1, k2, autojunk=False).ratio() >= 0.84
                    )
                    or (
                        edge_diff <= 0.012
                        and len(k1) == len(k2)
                        and 2 <= len(k1) <= 3
                        and (k1[0] == k2[0] or k1[-1] == k2[-1])
                    )
                )
                partial_occlusion_same = False
                if not (edge_diff <= 0.025 and same_caption) and y_diff <= 0.022 and k1 and k2:
                    cjk1 = "".join(c for c in k1 if "\u3400" <= c <= "\u9fff")
                    cjk2 = "".join(c for c in k2 if "\u3400" <= c <= "\u9fff")
                    if len(k1) <= len(k2):
                        sk, lk, scjk, lcjk = k1, k2, cjk1, cjk2
                        sx1, sx2, lx1, lx2 = prev_x1, prev_x2, cur_x1, cur_x2
                    else:
                        sk, lk, scjk, lcjk = k2, k1, cjk2, cjk1
                        sx1, sx2, lx1, lx2 = cur_x1, cur_x2, prev_x1, prev_x2
                    if (
                        len(sk) >= 4
                        and len(lk) > len(sk)
                        and sx1 >= lx1 - 0.025
                        and sx2 <= lx2 + 0.025
                        and (sk in lk or (len(scjk) >= 3 and scjk in lcjk))
                    ):
                        partial_occlusion_same = True
                    elif (
                        y_diff <= 0.020
                        and len(scjk) >= 4
                        and len(lcjk) > len(scjk)
                    ):
                        ov_w = min(prev_x2, cur_x2) - max(prev_x1, cur_x1)
                        min_w = max(1e-4, min(prev_x2 - prev_x1, cur_x2 - cur_x1))
                        if ov_w >= 0.75 * min_w:
                            sm = SequenceMatcher(None, scjk, lcjk, autojunk=False)
                            m = sm.find_longest_match(0, len(scjk), 0, len(lcjk))
                            if m.size >= max(3, len(scjk) - 1) or (len(scjk) >= 6 and sm.ratio() >= 0.85):
                                partial_occlusion_same = True
                if (edge_diff <= 0.025 and same_caption) or partial_occlusion_same:
                    _set_ocr_field(prev, "start", min(prev_s, cur_s))
                    _set_ocr_field(prev, "end", max(prev_e, cur_e))
                    _set_ocr_field(prev, "x_pct", min(prev_x1, cur_x1))
                    _set_ocr_field(prev, "max_x_pct", max(prev_x2, cur_x2))
                    _set_ocr_field(prev, "y_pct", min(prev_y1, cur_y1))
                    _set_ocr_field(prev, "max_y_pct", max(prev_y2, cur_y2))
                    prev_prob = float(_ocr_field(prev, "prob", 0.0) or 0.0)
                    if len(k2) > len(k1) or (len(k2) == len(k1) and cur_prob > prev_prob):
                        _set_ocr_field(prev, "text", cur_text)
                        _set_ocr_field(prev, "prob", cur_prob)
                    matched = True
            still_open.append(prev)
        if not matched:
            still_open.append(copy.copy(cur))
        active = still_open
    finished.extend(active)
    ordered_finished = sorted(
        finished,
        key=lambda b: (
            float(_ocr_field(b, "start", 0.0) or 0.0),
            float(_ocr_field(b, "end", 0.0) or 0.0),
            float(_ocr_field(b, "y_pct", 0.0) or 0.0),
            float(_ocr_field(b, "x_pct", 0.0) or 0.0),
        ),
    )
    if not (max_chunk_s and max_chunk_s > 0):
        return ordered_finished
    chunked = []
    for blk in ordered_finished:
        s = float(_ocr_field(blk, "start", 0.0) or 0.0)
        e = float(_ocr_field(blk, "end", 0.0) or 0.0)
        if (e - s) <= max_chunk_s * 1.15:
            chunked.append(blk)
            continue
        cur = s
        while cur < e - 1e-6:
            nxt = e if (e - cur) <= max_chunk_s * 1.15 else (cur + max_chunk_s)
            piece = copy.copy(blk)
            _set_ocr_field(piece, "start", cur)
            _set_ocr_field(piece, "end", nxt)
            raw_st = _ocr_field(piece, "sample_time", None)
            if raw_st is not None:
                try:
                    _set_ocr_field(piece, "sample_time", max(cur, min(nxt, float(raw_st))))
                except (TypeError, ValueError):
                    pass
            chunked.append(piece)
            cur = nxt
    return chunked


def _bridge_source_caption_gaps(tracks):
    """Cover brief OCR dropouts between observations of the same stationary row."""
    ordered = sorted(tracks, key=lambda block: block.start)
    starts = [block.start for block in ordered]
    recent = {}
    bridges = []

    def nearby(first, second):
        if max(abs(getattr(first, field) - getattr(second, field)) for field in
               ("x_pct", "max_x_pct", "y_pct", "max_y_pct")) <= .025:
            return True
        if (abs(first.y_pct - second.y_pct) <= .025
                and abs(first.max_y_pct - second.max_y_pct) <= .025):
            w1 = max(0.001, first.max_x_pct - first.x_pct)
            w2 = max(0.001, second.max_x_pct - second.x_pct)
            ov_x = min(first.max_x_pct, second.max_x_pct) - max(first.x_pct, second.x_pct)
            return ov_x >= 0.55 * min(w1, w2)
        return False

    for block in ordered:
        text = _visual_caption_key(block.text)
        chinese = "".join(char for char in text if "\u3400" <= char <= "\u9fff")
        key = chinese or text
        if len(key) < 4:
            continue
        previous = recent.get(key)
        if previous is None or not 0 < block.start - previous.end <= .25 or not nearby(previous, block):
            fuzzy_candidates = [
                cand
                for prev_k, cand in recent.items()
                if prev_k != key
                and 0 < block.start - cand.end <= .25
                and nearby(cand, block)
                and SequenceMatcher(None, prev_k, key, autojunk=False).ratio() >= 0.65
            ]
            if fuzzy_candidates:
                previous = max(fuzzy_candidates, key=lambda b: b.end)
        recent[key] = block
        if previous is None or not 0 < block.start - previous.end <= .25 or not nearby(previous, block):
            continue
        intervening = ordered[bisect_left(starts, previous.end):bisect_left(starts, block.start)]
        if any(nearby(previous, other) for other in intervening):
            continue
        bridge = copy.copy(block)
        bridge.start, bridge.end = previous.end, block.start
        bridge.x_pct = min(previous.x_pct, block.x_pct)
        bridge.y_pct = min(previous.y_pct, block.y_pct)
        bridge.max_x_pct = max(previous.max_x_pct, block.max_x_pct)
        bridge.max_y_pct = max(previous.max_y_pct, block.max_y_pct)
        bridges.append(bridge)
    return ordered + bridges


def _attach_source_caption_tracks(segments, tracks):
    """Keep the source display clock independent of speech and TTS timings."""
    if not tracks:
        raise RuntimeError("Source caption lifetime could not be verified for the located subtitle band")
    grouped = {id(seg): [] for seg in segments}
    ordered_segs = sorted(
        segments,
        key=lambda s: (
            s.start.total_seconds() if hasattr(s.start, "total_seconds") else float(s.start),
            s.end.total_seconds() if hasattr(s.end, "total_seconds") else float(s.end),
        ),
    )
    seg_bounds = [
        (
            s,
            s.start.total_seconds() if hasattr(s.start, "total_seconds") else float(s.start),
            s.end.total_seconds() if hasattr(s.end, "total_seconds") else float(s.end),
        )
        for s in ordered_segs
    ]
    scored = []
    coalesced_input = _coalesce_stationary_source_tracks(
        _bridge_source_caption_gaps(tracks), max_chunk_s=None
    )
    for track in coalesced_input:
        t_start, t_end = float(track.start), float(track.end)
        overlapping = [
            (s, s0, s1)
            for s, s0, s1 in seg_bounds
            if min(s1, t_end) - max(s0, t_start) > 1e-6
        ]
        if len(overlapping) >= 2:
            max_ov = max(min(s1, t_end) - max(s0, t_start) for _, s0, s1 in overlapping)
            tcjk = "".join(c for c in str(getattr(track, "text", "") or "") if "\u3400" <= c <= "\u9fff")
            substantial = []
            for s, s0, s1 in overlapping:
                ov_s = min(s1, t_end) - max(s0, t_start)
                scjk = "".join(c for c in str(getattr(s, "content", "") or "") if "\u3400" <= c <= "\u9fff")
                if ov_s >= 0.15 or max_ov < 0.25 or (len(scjk) >= 2 and scjk in tcjk):
                    substantial.append((s, s0, s1))
            if len(substantial) == 1:
                overlapping = substantial
        if len(overlapping) >= 2:
            cur = t_start
            for pos, (s, s0, s1) in enumerate(overlapping):
                nxt_cut = overlapping[pos + 1][1] if pos + 1 < len(overlapping) else t_end
                piece_end = min(t_end, max(cur, min(s1, nxt_cut))) if pos + 1 < len(overlapping) else t_end
                if piece_end > cur + 1e-6:
                    piece = copy.copy(track)
                    _set_ocr_field(piece, "start", cur)
                    _set_ocr_field(piece, "end", piece_end)
                    raw_st = _ocr_field(piece, "sample_time", None)
                    if raw_st is not None:
                        try:
                            _set_ocr_field(piece, "sample_time", max(cur, min(piece_end, float(raw_st))))
                        except (TypeError, ValueError):
                            pass
                    ov = max(0., min(s1, piece_end) - max(s0, cur))
                    scored.append((piece, s, ov, 0.0))
                    cur = piece_end
            continue
        middle = (t_start + t_end) / 2

        def score(item):
            _, start, end = item
            overlap = max(0., min(end, t_end) - max(start, t_start))
            distance = max(start - middle, middle - end, 0.)
            return overlap, -distance

        best_item = max(seg_bounds, key=score)
        ov, _ = score(best_item)
        edge_dist = max(best_item[1] - t_end, t_start - best_item[2], 0.)
        scored.append((track, best_item[0], ov, edge_dist))
    first_seg_obj = seg_bounds[0][0] if seg_bounds else None
    first_seg_start = seg_bounds[0][1] if seg_bounds else 0.0
    has_nearby = any(ov > 0.0 or dist <= 2.2 for _, _, ov, dist in scored)
    ov_cxs = sorted(
        (float(t.x_pct) + float(t.max_x_pct)) * 0.5
        for t, _, ov, _ in scored
        if ov > 0.0
    )
    track_anchor_cx = ov_cxs[len(ov_cxs) // 2] if ov_cxs else 0.5
    for track, owner, ov, dist in scored:
        t_dur = float(track.end) - float(track.start)
        t_cx = (float(track.x_pct) + float(track.max_x_pct)) * 0.5
        is_locked_centered_subtitle = (
            t_dur >= 1.20
            and abs(t_cx - track_anchor_cx) <= 0.04
            and _visual_chinese_count(getattr(track, "text", "")) >= 3
        )
        max_dist = 3.0 if (owner is first_seg_obj and float(track.end) <= first_seg_start + 0.05) else 2.2
        if has_nearby and ov == 0.0 and dist > max_dist and not is_locked_centered_subtitle:
            continue
        grouped[id(owner)].append(track)
    for seg in segments:
        observed = sorted(grouped[id(seg)], key=lambda b: (b.start, b.y_pct, b.x_pct))
        observed = _coalesce_stationary_source_tracks(
            _bridge_source_caption_gaps(observed), max_chunk_s=None
        )
        if observed:
            seg_cjk_main = "".join(
                c for c in str(getattr(seg, "content", "") or "") if "\u3400" <= c <= "\u9fff"
            )
            seg_cjk_alt = "".join(
                c for c in str(getattr(seg, "paraformer_alt", "") or "") if "\u3400" <= c <= "\u9fff"
            )
            seg_cjk_pool = f"{seg_cjk_main} {seg_cjk_alt}".strip()
            filler_chars = {"嗯", "啊", "哦", "呃", "噢", "诶"}
            consumed_main = 0
            for b in observed:
                b_txt = str(_ocr_field(b, "text", "") or "").strip()
                b_cjk = "".join(c for c in b_txt if "\u3400" <= c <= "\u9fff")
                missing_left = 0
                if len(b_cjk) >= 4 and not b_cjk.startswith("一") and ("一" + b_cjk[:3]) in seg_cjk_pool:
                    missing_left = 1
                rem_main = seg_cjk_main[consumed_main:]
                if len(b_cjk) >= 4 and rem_main:
                    pos = rem_main.find(b_cjk[:4])
                    if 1 <= pos <= 2 and not (set(rem_main[:pos]) <= filler_chars):
                        missing_left = max(missing_left, pos)
                if missing_left > 0:
                    bx1 = float(_ocr_field(b, "x_pct", 0.0) or 0.0)
                    bx2 = float(_ocr_field(b, "max_x_pct", 0.0) or 0.0)
                    if bx2 > bx1:
                        char_w = (bx2 - bx1) / max(len(b_txt), 4)
                        pad_left = min(0.058 * missing_left, char_w * (missing_left * 1.15))
                        _set_ocr_field(b, "x_pct", max(0.0, bx1 - pad_left))
                if b_cjk and rem_main:
                    m = SequenceMatcher(None, rem_main, b_cjk, autojunk=False).find_longest_match(
                        0, len(rem_main), 0, len(b_cjk)
                    )
                    if m.size >= 3 and m.a <= 4:
                        consumed_main += m.a + m.size
        chunked_observed = _coalesce_stationary_source_tracks(observed, max_chunk_s=0.9)
        seg.tracking_blocks = chunked_observed
        seg.source_caption_timing_verified = True
        seg.geometry_verified = bool(chunked_observed)
        if chunked_observed:
            seg_text = str(getattr(seg, "content", "") or "")
            text_totals = {}
            for b in observed:
                k = _visual_caption_key(getattr(b, "text", ""))
                text_totals[k] = text_totals.get(k, 0.0) + max(0.0, float(b.end - b.start))
            seg.best_block = max(
                observed,
                key=lambda b: (
                    1 if _has_chinese_overlap(getattr(b, "text", ""), seg_text) else 0,
                    round(text_totals.get(_visual_caption_key(getattr(b, "text", "")), float(b.end - b.start)), 3),
                    float(b.end - b.start),
                    float(b.prob),
                ),
            )
            seg.ocr_mask_status = "located"
            seg.y_pct, seg.max_y_pct = seg.best_block.y_pct, seg.best_block.max_y_pct
        else:
            seg.best_block = None
            seg.ocr_mask_status = "no_caption_evidence"


def _visual_segment_center_y(seg):
    blocks = list(getattr(seg, "tracking_blocks", None) or [])
    if not blocks and getattr(seg, "best_block", None) is not None:
        blocks = [seg.best_block]
    centers = []
    for block in blocks:
        try:
            y1 = float(_ocr_field(block, "y_pct", 0.0) or 0.0)
            y2 = float(_ocr_field(block, "max_y_pct", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if y2 > y1:
            centers.append((y1 + y2) * 0.5)
    if centers:
        return sum(centers) / len(centers)
    return float(getattr(seg, "y_pct", 0.85) or 0.85)


def normalize_visual_speech_timing(segments, duration=None):
    """Merge micro-captions and extend speech time, not visual cover time."""
    visual = [seg for seg in segments if getattr(seg, "visual_sub_to_speech", False)]
    if not visual:
        return segments

    kept = []
    removed = set()
    ordered_all = sorted(segments, key=lambda item: item.start)
    for position, seg in enumerate(ordered_all):
        if not getattr(seg, "visual_sub_to_speech", False) or position == 0:
            kept.append(seg)
            continue
        previous = ordered_all[position - 1]
        if id(previous) in removed:
            previous = next(
                (item for item in reversed(ordered_all[:position]) if id(item) not in removed),
                None,
            )
        if previous is not None and getattr(previous, "visual_sub_to_speech", False):
            gap = (seg.start - previous.end).total_seconds()
            previous_duration = (previous.end - previous.start).total_seconds()
            total_chars = _visual_chinese_count(previous.content) + _visual_chinese_count(seg.content)
            same_band = abs(_visual_segment_center_y(previous) - _visual_segment_center_y(seg)) < 0.035
            if -0.5 < gap < 0.25 and same_band and total_chars <= 14 and previous_duration < 0.85:
                left_text = str(previous.content or "").rstrip("，, ")
                right_text = str(seg.content or "").lstrip("，, ")
                previous.content = f"{left_text}，{right_text}"
                previous.end = max(previous.end, seg.end)
                previous.tracking_blocks = sorted(
                    list(getattr(previous, "tracking_blocks", None) or [])
                    + list(getattr(seg, "tracking_blocks", None) or []),
                    key=lambda block: float(_ocr_field(block, "start", 0.0) or 0.0),
                )
                previous.visual_start_sec = min(
                    float(getattr(previous, "visual_start_sec", previous.start.total_seconds())),
                    float(getattr(seg, "visual_start_sec", seg.start.total_seconds())),
                )
                previous.visual_end_sec = max(
                    float(getattr(previous, "visual_end_sec", previous.end.total_seconds())),
                    float(getattr(seg, "visual_end_sec", seg.end.total_seconds())),
                )
                if getattr(previous, "best_block", None) is not None:
                    previous.best_block.end = previous.visual_end_sec
                removed.add(id(seg))
                continue
        kept.append(seg)

    if removed:
        segments[:] = [seg for seg in segments if id(seg) not in removed]

    ordered = sorted(segments, key=lambda item: item.start)
    for position, seg in enumerate(ordered):
        if not getattr(seg, "visual_sub_to_speech", False):
            continue
        current_duration = (seg.end - seg.start).total_seconds()
        min_speech_duration = max(1.10, _visual_chinese_count(seg.content) * 0.18)
        next_start = (
            ordered[position + 1].start.total_seconds()
            if position + 1 < len(ordered)
            else float(duration or seg.end.total_seconds())
        )
        gap = next_start - seg.end.total_seconds()
        if current_duration >= min_speech_duration or gap <= 0.12:
            continue
        target_end = min(seg.start.total_seconds() + min_speech_duration, next_start - 0.08)
        if duration:
            target_end = min(target_end, float(duration))
        if target_end > seg.end.total_seconds():
            seg.end = timedelta(seconds=target_end)
            seg.speech_end_sec = target_end
    return segments


sys.modules.setdefault("ocr_track_coalescer", sys.modules[__name__])
sys.modules.setdefault("backend.ocr_track_coalescer", sys.modules[__name__])
