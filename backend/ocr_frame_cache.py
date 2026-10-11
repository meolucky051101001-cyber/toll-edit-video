"""Reuse OCR only while the observed subtitle pixels remain nearly identical."""
import cv2
import hashlib
import numpy as np


class SubtitleFrameCache:
    def __init__(self, top, bottom, max_age=1.0):
        self.top, self.bottom = top, bottom
        self.max_age = max_age
        self.entries = []
        self.hits = 0

    def signature(self, frame):
        h, w = frame.shape[:2]
        crop = frame[max(0, int(h*self.top)):min(h, int(h*self.bottom)), :]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (360, max(12, round(crop.shape[0]*360/w))))
        edges = cv2.Canny(gray, 60, 160) > 0
        return gray, edges, hashlib.sha256(crop.tobytes()).digest()

    def lookup(self, signature, timestamp):
        gray, edges, digest = signature
        for when, old_gray, old_edges, old_digest, result in reversed(self.entries):
            # Periodic recognition bounds accumulated visual drift.
            if abs(timestamp-when) > self.max_age or gray.shape != old_gray.shape:
                continue
            # Whole-frame averages can hide one changed glyph or a small move.
            if digest == old_digest:
                self.hits += 1
                return result
        return None

    def remember(self, signature, timestamp, result):
        self.entries.append((timestamp, *signature, result))
        self.entries = self.entries[-32:]

    def visual_diff(self, sig1, sig2):
        if not sig1 or not sig2:
            return 999.0, 1.0
        gray1, edges1 = sig1[:2]
        gray2, edges2 = sig2[:2]
        if gray1.shape != gray2.shape:
            return 999.0, 1.0
        delta = np.abs(gray1.astype(np.int16) - gray2.astype(np.int16))
        changed = np.logical_xor(edges1, edges2).sum()
        ink = max(1, np.logical_or(edges1, edges2).sum())
        return float(delta.mean()), float(changed / ink)

    def is_visual_transition(self, sig1, sig2):
        mean_delta, edge_ratio = self.visual_diff(sig1, sig2)
        return edge_ratio > 0.20 or mean_delta > 10.0
