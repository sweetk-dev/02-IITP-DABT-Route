# -*- coding: utf-8 -*-
"""보도 면형 dissolve -> 연속 스트립 단위 스켈레톤화.

폴리곤을 개별로 스켈레톤화하면 맞닿은 보도끼리도 중심선이 이어지지 않는다.
스켈레톤은 폴리곤 경계에서 폭의 절반만큼 안쪽에서 끝나므로, 공유 경계를 사이에 둔
두 중심선의 끝점이 (w1+w2)/2 만큼 벌어져 스냅 허용오차를 넘긴다.
(2026-07-16 실측: 개별 스켈레톤화 시 연결요소 3,489개 / 최대 0.3% — 사용 불가)

따라서 인접 폴리곤을 먼저 합쳐 연속 스트립을 만든 뒤 스트립 단위로 골격을 뽑는다.
폭·재질은 골격 조각의 중점이 어느 원본 폴리곤에 속하는지 찾아 되돌려 붙인다.
"""
from __future__ import annotations

from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

SNAP_BUFFER = 0.05   # m — 부동소수 오차로 살짝 떨어진 인접 폴리곤을 붙인다


def _to_polygon(points, parts):
    ps = list(parts) + [len(points)]
    rings = []
    for i in range(len(ps) - 1):
        r = points[ps[i]:ps[i + 1]]
        if len(r) >= 4:
            rings.append(r)
    if not rings:
        return None
    poly = Polygon(rings[0], rings[1:] if len(rings) > 1 else None)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return None if poly.is_empty else poly


def dissolve_polys(records: list[dict]) -> tuple[list, STRtree, list[dict]]:
    """records: [{'points':..., 'parts':..., 'attrs':{...}, 'sheet':...}]

    반환: (스트립 폴리곤 목록, 원본 STRtree, 원본 record 목록)
    """
    polys, srcs = [], []
    for r in records:
        p = _to_polygon(r["points"], r["parts"])
        if p is None:
            continue
        polys.append(p)
        srcs.append(r)
    if not polys:
        return [], None, []
    merged = unary_union([p.buffer(SNAP_BUFFER) for p in polys])
    strips = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
    strips = [s.buffer(-SNAP_BUFFER) for s in strips]
    out = []
    for s in strips:
        if s.is_empty:
            continue
        if s.geom_type == "MultiPolygon":
            out.extend([g for g in s.geoms if not g.is_empty and g.area > 1.0])
        elif s.area > 1.0:
            out.append(s)
    return out, STRtree(polys), srcs


def grid_centerlines(poly_records: list[dict], cell: float = 800.0,
                     pad: float = 60.0, verbose: bool = True) -> list[dict]:
    """보도 폴리곤 레코드 -> 중심선 피처 목록 (격자 분할 스켈레톤화).

    전역 dissolve 한 보도는 도시 전체로 이어져 bbox 래스터가 수억 픽셀이 된다
    (안양 9.7x11km / 0.5m = 4.3억 px -> OOM). 그렇다고 폴리곤을 그냥 잘라 처리하면
    절단면에서 중심선이 폭의 절반만큼 물러나 다시 끊긴다.

    그래서 격자 셀마다 **pad 만큼 넉넉히 확장한 영역**을 스켈레톤화한 뒤
    **정확한 셀 경계로 선을 잘라낸다**. 골격이 경계를 가로질러 생성되므로
    이웃 셀의 골격과 경계에서 정확히 만나고, 끝점 스냅으로 이어진다.
    """
    from .centerline import poly_to_centerlines
    from .extract import _mk, SIDEWALK_AREA
    from shapely.geometry import box

    polys, srcs = [], []
    for r in poly_records:
        p = _to_polygon(r["points"], r["parts"])
        if p is None:
            continue
        polys.append(p)
        srcs.append(r)
    if not polys:
        return []
    merged = unary_union([p.buffer(SNAP_BUFFER) for p in polys]).buffer(-SNAP_BUFFER)
    tree = STRtree(polys)

    # 셀마다 merged 전체와 교차하면 O(셀수 x 전체정점) 이라 사실상 끝나지 않는다.
    # dissolve 결과를 조각 단위로 색인해 셀에 걸치는 조각만 교차시킨다.
    mparts = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
    mtree = STRtree(mparts)

    minx, miny, maxx, maxy = merged.bounds
    out = []
    nx_ = int((maxx - minx) // cell) + 1
    ny_ = int((maxy - miny) // cell) + 1
    done = 0
    for ix in range(nx_):
        for iy in range(ny_):
            done += 1
            if verbose and done % 20 == 0:
                print(f"    셀 {done}/{nx_*ny_}  중심선 {len(out)}", flush=True)
            x0 = minx + ix * cell
            y0 = miny + iy * cell
            cellbox = box(x0, y0, x0 + cell, y0 + cell)
            outer = box(x0 - pad, y0 - pad, x0 + cell + pad, y0 + cell + pad)
            hit = mtree.query(outer)
            if len(hit) == 0:
                continue
            work = unary_union([mparts[i] for i in hit]).intersection(outer)
            if work.is_empty:
                continue
            geoms = work.geoms if work.geom_type == "MultiPolygon" else [work]
            for g in geoms:
                if g.is_empty or g.area < 1.0:
                    continue
                pts, parts = _ring_coords(g)
                if not pts:
                    continue
                for line in poly_to_centerlines(pts, parts):
                    clipped = line.intersection(cellbox)
                    if clipped.is_empty:
                        continue
                    segs = clipped.geoms if clipped.geom_type == "MultiLineString" else [clipped]
                    for seg in segs:
                        if seg.geom_type != "LineString" or seg.length < 1.0:
                            continue
                        out.append(_attach(seg, tree, polys, srcs))
    return out


def _ring_coords(poly):
    pts = list(poly.exterior.coords)
    parts = [0]
    for it in poly.interiors:
        parts.append(len(pts))
        pts.extend(list(it.coords))
    return pts, parts


def _attach(line, tree, polys, srcs):
    """중심선 조각의 중점이 속한 원본 폴리곤에서 폭·재질·도엽을 되돌려 붙인다."""
    from .extract import _mk, SIDEWALK_AREA
    mid = line.interpolate(0.5, normalized=True)
    best, bd = None, 1e18
    for i in tree.query(mid.buffer(3.0)):
        d = polys[i].distance(mid)
        if d < bd:
            best, bd = i, d
        if d == 0:
            break
    attrs = srcs[best]["attrs"] if best is not None else {}
    sheet = srcs[best]["sheet"] if best is not None else ""
    return _mk(sheet, SIDEWALK_AREA, line, attrs, "sidewalk")
