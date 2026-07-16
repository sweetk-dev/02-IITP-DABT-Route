# -*- coding: utf-8 -*-
"""OSM 횡단보도 병합.

수치지형도 1:1,000 에는 횡단보도 레이어가 없다(2026-07-16 안양 211매 전수 스캔 0건).
인도 폴리곤 하나가 이미 한 블록 전체의 보도라서, 중심선을 뽑으면 블록마다 독립된
선이 되고 교차로(실측 중앙값 13m 틈)에서 끊긴다. 즉 횡단보도 없이는 그래프가 이어지지 않는다.

⚠️ 위상 구축 **이전에** 실행해야 한다. 노드는 블록 끝에만 있어(평균 52m 간격)
횡단보도 끝점을 노드에 붙이려 하면 대부분 실패한다(실측: 623건 중 57건만 접합).
대신 보도 **중심선 위의 최근접 지점**으로 투영해 T자 접합을 만들고,
분할·스냅은 topology.build_topology 가 처리하게 한다.

샌드박스는 overpass 접근이 차단되므로 scripts/fetch_osm_crossings.py 로
로컬(Windows venv)에서 미리 받아 GeoJSON 으로 넘긴다.
"""
from __future__ import annotations

import json
import math

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

SNAP_RADIUS = 30.0     # m — 횡단보도 끝점에서 보도 중심선을 찾는 반경
MAX_SPAN = 60.0        # m — 이보다 긴 횡단보도 링크는 오접합으로 보고 버림


def load_crossings(geojson_path: str, to_5186) -> list:
    """OSM 횡단보도 GeoJSON(EPSG:4326) -> EPSG:5186 지오메트리 목록."""
    with open(geojson_path, "r", encoding="utf-8") as f:
        gj = json.load(f)
    out = []
    for feat in gj.get("features", []):
        g = feat.get("geometry") or {}
        if g.get("type") == "LineString":
            pts = [to_5186(x, y) for x, y in g["coordinates"]]
            if len(pts) >= 2:
                out.append(LineString(pts))
        elif g.get("type") == "Point":
            out.append(Point(*to_5186(*g["coordinates"])))
    return out


def crossing_features(feats: list[dict], crossings: list,
                      radius: float = SNAP_RADIUS) -> tuple[list[dict], dict]:
    """보도 중심선 피처 + OSM 횡단보도 -> 횡단보도 링크 피처 목록.

    반환: (crossing 피처 목록, 통계)
    """
    lines = [f for f in feats if f.get("kind") == "sidewalk"]
    if not lines:
        return [], {"linear": 0, "point": 0, "skipped": len(crossings)}
    geoms = [f["geom"] for f in lines]
    tree = STRtree(geoms)

    out = []
    stat = {"linear": 0, "point": 0, "skipped": 0}
    for c in crossings:
        if c.geom_type == "LineString":
            seg = _from_line(c, tree, geoms, radius)
        else:
            seg = _from_point(c, tree, geoms, radius)
        if seg is None:
            stat["skipped"] += 1
            continue
        out.append({"kind": "crossing", "geom": seg, "width": None, "surface": None,
                    "name": None, "accessible": None, "sheet": None, "osm": True})
        stat["linear" if c.geom_type == "LineString" else "point"] += 1
    return out, stat


def _project(p: Point, tree, geoms, radius, exclude=None):
    """점을 반경 안 최근접 보도 중심선 위로 투영. (투영점, 선 인덱스) 반환."""
    best, bd = None, 1e18
    for i in tree.query(p.buffer(radius)):
        if exclude is not None and i == exclude:
            continue
        d = geoms[i].distance(p)
        if d < bd:
            best, bd = i, d
    if best is None or bd > radius:
        return None, None
    g = geoms[best]
    return g.interpolate(g.project(p)), best


def _from_line(c: LineString, tree, geoms, radius):
    """선형 횡단보도: 양 끝점을 각각 최근접 보도 중심선에 투영해 잇는다."""
    a, ia = _project(Point(c.coords[0]), tree, geoms, radius)
    b, ib = _project(Point(c.coords[-1]), tree, geoms, radius)
    if a is None or b is None or ia == ib:
        return None
    if a.distance(b) < 1.0 or a.distance(b) > MAX_SPAN:
        return None
    return LineString([(a.x, a.y), (b.x, b.y)])


def _from_point(c: Point, tree, geoms, radius):
    """점형 횡단보도: 마주보는 두 보도 중심선에 투영해 잇는다."""
    a, ia = _project(c, tree, geoms, radius)
    if a is None:
        return None
    b, ib = _project(c, tree, geoms, radius, exclude=ia)
    if b is None:
        return None
    # 같은 방향이면 도로를 건너는 게 아니다 (같은 쪽 보도 두 조각)
    v1 = (a.x - c.x, a.y - c.y)
    v2 = (b.x - c.x, b.y - c.y)
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 > 0.5 and n2 > 0.5:
        cos = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
        if cos > -0.2:          # 100도 미만으로 벌어짐 = 마주보지 않음
            return None
    d = a.distance(b)
    if d < 1.0 or d > MAX_SPAN:
        return None
    return LineString([(a.x, a.y), (b.x, b.y)])
