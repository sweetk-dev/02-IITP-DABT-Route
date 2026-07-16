# -*- coding: utf-8 -*-
"""보도 중심선 조각 -> 위상 그래프(node/link).

수치지형도는 위상이 없다. 도엽마다 독립 제작되어 경계에서 선이 끊기고,
스켈레톤 중심선도 폴리곤 단위로 쪼개져 나온다. 여기서 다음을 처리한다.

  1) 끝점 스냅   — tol 이내 끝점을 하나의 노드로 병합 (도엽 경계 접합 포함)
  2) 교차 분할   — 선이 교차하면 교차점에서 링크를 나눠 노드 생성
  3) 고립 제거   — min_component_len 미만 연결요소 폐기 (스켈레톤 잔가지)
"""
from __future__ import annotations

import math
from collections import defaultdict

from shapely.geometry import LineString, Point

SNAP_TOL = 1.5            # m — 도엽 경계 어긋남·스켈레톤 끝단 오차 흡수
MIN_COMPONENT_LEN = 30.0  # m — 이보다 짧은 고립 연결요소는 버림
BRIDGE_GAP = 6.0          # m — 차량 진출입로로 끊긴 보도를 잇는 최대 틈 (도로 횡단은 OSM 횡단보도 담당)


def _key(pt, tol):
    return (round(pt[0] / tol), round(pt[1] / tol))


def _split_at_intersections(feats: list[dict], tol: float) -> list[dict]:
    """다른 선의 끝점이 어떤 선의 중간에 닿으면(T자 접합) 그 지점에서 분할.

    스켈레톤 중심선은 이미 폴리곤 단위로 분절되어 있고 도엽 경계 접합은
    끝점 스냅이 처리한다. 따라서 전역 noding(unary_union + linemerge)은 불필요하며,
    오히려 linemerge 가 폭이 다른 보도들을 하나의 체인으로 병합해 속성을 뭉갠다.
    여기서는 T자 접합만 국소적으로 분할한다.
    """
    from shapely.strtree import STRtree

    work = [f for f in feats if f.get("kind") != "road"]
    keep = [f for f in feats if f.get("kind") == "road"]
    if not work:
        return keep

    geoms = [f["geom"] for f in work]
    tree = STRtree(geoms)
    out = list(keep)
    for i, f in enumerate(work):
        g = f["geom"]
        cuts = []
        for j in tree.query(g.buffer(tol)):
            if j == i:
                continue
            og = work[j]["geom"]
            for p in (Point(og.coords[0]), Point(og.coords[-1])):
                if g.distance(p) > tol:
                    continue
                d = g.project(p)
                if tol < d < g.length - tol:      # 끝점 근처는 스냅이 처리
                    cuts.append(d)
        if not cuts:
            out.append(f)
            continue
        for piece in _cut_line(g, sorted(set(round(c, 2) for c in cuts))):
            nf = dict(f)
            nf["geom"] = piece
            out.append(nf)
    return out


def _cut_line(line: LineString, dists: list[float]) -> list[LineString]:
    """선을 진행거리 목록에서 잘라 조각들로 반환."""
    pieces = []
    prev = 0.0
    for d in list(dists) + [line.length]:
        if d - prev < 0.5:
            continue
        seg = _substring(line, prev, d)
        if seg is not None and seg.length > 0.5:
            pieces.append(seg)
        prev = d
    return pieces or [line]


def _substring(line: LineString, s: float, e: float):
    """line 의 진행거리 [s, e] 구간을 잘라낸다 (중간 정점 보존)."""
    coords = list(line.coords)
    pts = [(line.interpolate(s).x, line.interpolate(s).y)]
    acc = 0.0
    for k in range(len(coords) - 1):
        seg_len = math.dist(coords[k], coords[k + 1])
        a = acc
        acc += seg_len
        if a > s and a < e:
            pts.append(tuple(coords[k]))
    pts.append((line.interpolate(e).x, line.interpolate(e).y))
    uniq = []
    for p in pts:
        if not uniq or p != uniq[-1]:
            uniq.append(p)
    if len(uniq) < 2:
        return None
    return LineString(uniq)


def build_topology(feats: list[dict], to_wgs84, tol: float = SNAP_TOL,
                   bridge_gap: float = BRIDGE_GAP,
                   min_component_len: float = MIN_COMPONENT_LEN):
    """피처 목록 -> (nodes, links).

    to_wgs84(x, y) -> (lon, lat) 변환 함수를 받는다 (EPSG:5186 -> 4326).
    """
    import networkx as nx

    feats = _split_at_intersections(feats, tol)
    feats = [f for f in feats if f.get("kind") != "road"]

    # 1) 끝점 스냅 -> 노드
    #    격자 반올림(round(x/tol))은 경계를 사이에 둔 두 점을 갈라놓는다
    #    (실측: 1.5m 이내인데 다른 노드가 된 끝점 734개). KD-tree + union-find 로 스냅.
    from scipy.spatial import cKDTree

    raw = []
    for f in feats:
        g = f["geom"]
        if g.is_empty or g.length <= 0:
            continue
        cs = list(g.coords)
        raw.append((cs[0], cs[-1], f, g.length))
    if not raw:
        return [], []

    endpts = []
    for a, b, _, _ in raw:
        endpts.append(a); endpts.append(b)
    tree = cKDTree(endpts)
    parent = list(range(len(endpts)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)

    for i, j in tree.query_pairs(tol):
        union(i, j)

    cluster_id, coords = {}, {}
    for i in range(len(endpts)):
        r = find(i)
        if r not in cluster_id:
            cluster_id[r] = len(cluster_id) + 1
            coords[cluster_id[r]] = endpts[r]
    nid_of = [cluster_id[find(i)] for i in range(len(endpts))]

    G = nx.Graph()
    edges = []
    for k, (a, b, f, ln) in enumerate(raw):
        na, nb = nid_of[2 * k], nid_of[2 * k + 1]
        if na == nb:
            continue
        edges.append((na, nb, f, ln))
        G.add_edge(na, nb, length=ln)

    # 1-b) 진출입로 수준의 짧은 틈 잇기.
    #      수치지형도의 인도 폴리곤은 차량 진출입로에서 물리적으로 끊긴다
    #      (실측: 다른 연결요소와의 최근접 거리 중앙값 13m). 도로 횡단(>bridge_gap)은
    #      OSM 횡단보도가 담당하고, 여기서는 진출입로만 잇는다.
    if bridge_gap > 0:
        cpts = [coords[i] for i in sorted(coords)]
        cids = sorted(coords)
        ct = cKDTree(cpts)
        seq = len(edges)
        for i, j in ct.query_pairs(bridge_gap):
            a, b = cids[i], cids[j]
            if G.has_edge(a, b):
                continue
            try:
                if nx.shortest_path_length(G, a, b) <= 3:
                    continue          # 이미 가까이 이어져 있으면 건너뜀
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                pass
            d = math.dist(cpts[i], cpts[j])
            f = {"kind": "sidewalk", "width": None, "surface": None,
                 "name": None, "accessible": None, "sheet": None, "bridged": True}
            edges.append((a, b, f, d))
            G.add_edge(a, b, length=d)

    # 2) 고립 연결요소 제거
    keep = set()
    for comp in nx.connected_components(G):
        sub = G.subgraph(comp)
        tot = sum(d["length"] for _, _, d in sub.edges(data=True))
        if tot >= min_component_len:
            keep |= set(comp)

    nodes, links = [], []
    used = set()
    for i, (a, b, f, ln) in enumerate(edges, 1):
        if a not in keep or b not in keep:
            continue
        used.add(a); used.add(b)
        lon1, lat1 = to_wgs84(*coords[a])
        lon2, lat2 = to_wgs84(*coords[b])
        links.append({
            "LINK_ID": f"L{i:07d}",
            "F_NODE": f"N{a:07d}",
            "T_NODE": f"N{b:07d}",
            "LENGTH": round(ln, 2),
            "LINK_TYPE": f.get("kind", "sidewalk"),
            "WIDTH": f.get("width"),
            "SURFACE": f.get("surface"),
            "LINK_NAME": f.get("name"),
            "ACCESSIBLE": f.get("accessible"),
            "SHEET": f.get("sheet"),
            "SOURCE": "bridge" if f.get("bridged") else "topomap",
        })
    for n in sorted(used):
        lon, lat = to_wgs84(*coords[n])
        nodes.append({"NODE_ID": f"N{n:07d}", "X": round(lon, 8), "Y": round(lat, 8),
                      "NODE_TYPE": "sidewalk"})
    return nodes, links
