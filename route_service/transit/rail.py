# -*- coding: utf-8 -*-
"""지하철 구간 선형 (v1.39.0).

지금까지 지하철 leg 의 geometry 는 승차역 대표점 → 하차역 대표점 직선 두 점이었다. 실증(2026-10-20 점검)에서
"관악역 가운데에서 명학역 가운데로 직선을 그었다 — 노선을 따라야 한다"는 지적이 있었다.

rail_lines.json 의 노선별 선형(OSM 철도 본선, 북 → 남 한 줄)에 두 역의 기준점을 투영해 그 사이 구간을 잘라 쓴다.
역 기준점은 승강장 윤곽(station_exits.json platforms)이 있으면 그 중심, 없으면 역 대표점이다 — 역 대표점은
역사 건물 쪽에 찍혀 승강장에서 60~140m 떨어진 역이 있어(명학·관악), 승강장 중심이 선로에 더 가깝다.

투영 거리가 MAX_OFFSET_M 을 넘으면(선형 자료에 없는 노선·역) None — 호출자가 종전 직선으로 둔다.
"""
from __future__ import annotations

import json
import math
import os

_PATH = os.path.join(os.path.dirname(__file__), "rail_lines.json")
_LINES = None
MAX_OFFSET_M = 300.0           # 역 기준점과 선로 사이 허용 거리 — 이보다 멀면 다른 노선·자료 결함으로 본다


def _load() -> dict:
    global _LINES
    if _LINES is None:
        try:
            with open(_PATH, encoding="utf-8") as f:
                _LINES = json.load(f).get("lines", {}) or {}
        except (OSError, ValueError):
            _LINES = {}
    return _LINES


def _kx(lat: float) -> float:
    return 111320.0 * math.cos(math.radians(lat))


_KY = 110540.0


def _project(poly: list, pt):
    """pt 를 polyline 에 투영 — (선분 번호 i, 선분 안 비율 t, 투영점, 거리 m). poly 는 [[lat, lng], ...]."""
    kx = _kx(pt[0])
    px, py = pt[1] * kx, pt[0] * _KY
    best = None
    for i in range(len(poly) - 1):
        ax, ay = poly[i][1] * kx, poly[i][0] * _KY
        bx, by = poly[i + 1][1] * kx, poly[i + 1][0] * _KY
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        qx, qy = ax + t * dx, ay + t * dy
        d = math.hypot(px - qx, py - qy)
        if best is None or d < best[3]:
            best = (i, t, [qy / _KY, qx / kx], d)
    return best


def length_m(pts: list) -> float:
    s = 0.0
    for a, b in zip(pts, pts[1:]):
        s += math.hypot((b[0] - a[0]) * _KY, (b[1] - a[1]) * _kx(a[0]))
    return s


def segment(line: str, a, b):
    """노선 line 위에서 a → b 구간 선형 [[lat, lng], ...] (a 의 투영점에서 시작해 b 의 투영점에서 끝난다).

    a, b : (lat, lng) 역 기준점. 노선 선형이 없거나 어느 한쪽이 MAX_OFFSET_M 보다 멀면 None.
    """
    poly = _load().get(line or "")
    if not poly or len(poly) < 2:
        return None
    pa, pb = _project(poly, a), _project(poly, b)
    if pa is None or pb is None or pa[3] > MAX_OFFSET_M or pb[3] > MAX_OFFSET_M:
        return None
    fwd = (pa[0], pa[1]) <= (pb[0], pb[1])            # 선형 순서(북 → 남)로 a 가 앞인가
    lo, hi = (pa, pb) if fwd else (pb, pa)
    mid = [list(p) for p in poly[lo[0] + 1: hi[0] + 1]]   # 두 투영점 사이의 선형 꼭짓점
    pts = [lo[2]] + mid + [hi[2]]
    if not fwd:
        pts.reverse()
    out = []
    for p in pts:
        q = [round(p[0], 7), round(p[1], 7)]
        if not out or out[-1] != q:
            out.append(q)
    return out if len(out) >= 2 else None
