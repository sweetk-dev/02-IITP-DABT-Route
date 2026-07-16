# -*- coding: utf-8 -*-
"""보도 면형 -> 스켈레톤 중심선.

수치지형도의 인도(A0033320)는 면형이라 그대로는 경로 그래프가 되지 않는다.
폴리곤을 래스터화 후 skimage.skeletonize 로 골격을 뽑고 선형으로 되돌린다.
폭(width)은 원본 속성을 그대로 전이하므로 면 폭에서 추정할 필요가 없다.
"""
from __future__ import annotations

import numpy as np
from shapely.geometry import Polygon, LineString
from shapely.ops import linemerge, unary_union

DEFAULT_RES = 1.0      # m/px. 보도 폭 중앙값 2.5m -> 2~3px, 골격 추출 가능한 최소 해상도
MIN_LEN = 2.0          # 이보다 짧은 골격 조각은 버림 (스켈레톤 잔가지)
SIMPLIFY_TOL = 1.0     # 픽셀 계단 제거 (res 에 맞춤)


def _rings(points, parts):
    ps = list(parts) + [len(points)]
    out = []
    for i in range(len(ps) - 1):
        r = points[ps[i]:ps[i + 1]]
        if len(r) >= 4:
            out.append(r)
    return out


def _to_polygon(points, parts):
    rings = _rings(points, parts)
    if not rings:
        return None
    poly = Polygon(rings[0], rings[1:] if len(rings) > 1 else None)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return None if poly.is_empty else poly


def poly_to_centerlines(points, parts, res: float = DEFAULT_RES,
                        max_px: int = 20_000_000) -> list[LineString]:
    """폴리곤 좌표열 -> 중심선 LineString 목록 (실패 시 빈 목록)."""
    from skimage.draw import polygon as rr_polygon
    from skimage.morphology import skeletonize

    poly = _to_polygon(points, parts)
    if poly is None:
        return []
    minx, miny, maxx, maxy = poly.bounds
    pad = 2
    W = int((maxx - minx) / res) + 2 * pad
    H = int((maxy - miny) / res) + 2 * pad
    if W < 3 or H < 3 or W * H > max_px:
        return []
    img = np.zeros((H, W), dtype=bool)
    geoms = poly.geoms if poly.geom_type == "MultiPolygon" else [poly]
    for g in geoms:
        xs, ys = g.exterior.coords.xy
        r, c = rr_polygon((np.array(ys) - miny) / res + pad,
                          (np.array(xs) - minx) / res + pad, img.shape)
        img[r, c] = True
        for it in g.interiors:
            xs, ys = it.coords.xy
            r, c = rr_polygon((np.array(ys) - miny) / res + pad,
                              (np.array(xs) - minx) / res + pad, img.shape)
            img[r, c] = False
    sk = skeletonize(img)
    ys, xs = np.nonzero(sk)
    if len(ys) < 2:
        return []
    pix = set(zip(ys.tolist(), xs.tolist()))
    segs = []
    for (y, x) in pix:
        for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
            if (y + dy, x + dx) in pix:
                segs.append(((minx + (x - pad) * res, miny + (y - pad) * res),
                             (minx + (x + dx - pad) * res, miny + (y + dy - pad) * res)))
    if not segs:
        return []
    merged = unary_union([LineString(s) for s in segs])
    if merged.geom_type == "MultiLineString":
        try:
            merged = linemerge(merged)
        except ValueError:      # 조각이 하나뿐이면 linemerge 가 거부한다
            pass
    lines = list(merged.geoms) if merged.geom_type == "MultiLineString" else [merged]
    lines = [l for l in lines if l.geom_type == "LineString"]
    out = []
    for l in lines:
        s = l.simplify(SIMPLIFY_TOL, preserve_topology=False)
        if s.length > MIN_LEN:
            out.append(s)
    return out
