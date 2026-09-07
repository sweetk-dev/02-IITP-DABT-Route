# -*- coding: utf-8 -*-
"""링크 투영 스냅 — 출발·도착 좌표를 최근접 **링크 위의 점**에 가상 노드로 붙인다 (#67, v1.23.0).

종전 `snap.snap()` 은 최근접 **노드**에만 붙였다. 링크 길이 중앙값 47m, 100m 초과 링크가
2,250개인 그래프에서는 좌표가 긴 링크 중간에 있으면 링크 한쪽 끝까지 갔다가 되돌아오는
경로가 구조적으로 나온다(실측: 안양아트센터 노드 60.4m 대 링크 위 점 35.5m, 우회비 1.55).

가상 노드는 공유 그래프를 건드리지 않는다 — 요청마다 그래프 사본(`attach()`)에 노드와
분할 링크 두 개를 얹고, 원 링크는 그대로 둔다(다른 경로에 영향 없음).

안전 필터 (교차검토 2026-09-07):
  · 계층 스냅 — 반경 LAYER_RADIUS_M 안에 보행 링크(sidewalk 계열)가 있으면 도로 링크는 후보에서 뺀다.
    간선(primary/secondary/trunk 로 재분류된 link_name 이 없으므로 link_type=='road' 전체를 2순위로 둔다)
  · crossing·steps·overpass·underpass 링크는 투영 대상에서 제외 — 차도 한복판·계단 위에서 안내가 시작되면 안 된다
  · LOS — 좌표→투영점 선분이 건물 폴리곤 또는 옹벽·담장(ObstacleIndex)과 교차하면 탈락
  · K 후보 — 상위 K 개 링크에 각각 투영해 두고, 호출 측이 "투영 거리 + 목적지까지 경로 비용" 최소를 고른다

실측 출입구(manual_survey·accessible_entrance)로 해석된 도착점은 종전 노드 스냅을 유지한다 —
출입구 스퍼 끝 노드가 곧 안내 종점이어야 하기 때문이다(호출 측 판단).
"""
from __future__ import annotations

import math

from .geo import haversine_m
from .planner import edge_passable

VIRTUAL_PREFIX = "V_"
DEFAULT_RADIUS_M = 60.0          # 투영 후보를 찾는 반경 (노드 스냅 상한 300m 보다 훨씬 작게 — 먼 링크는 노드 스냅이 맡는다)
LAYER_RADIUS_M = 20.0            # 이 안에 보행 링크가 있으면 도로 링크 제외
DEFAULT_K = 3
PEDESTRIAN_TYPES = ("sidewalk", "footway", "path", "pedestrian", "living_street", "derived")
NO_PROJECT_TYPES = ("crossing", "steps", "overpass", "underpass")
MIN_SPLIT_M = 1.0                # 링크 끝에서 이보다 가까우면 그냥 그 끝 노드로 본다
NODE_EXACT_M = 2.0               # 노드가 이 안에 있으면 노드 스냅만 쓴다
EDGE_OVER_NODE_TOL_M = 5.0       # 노드 스냅 거리보다 이만큼 넘게 먼 링크 후보는 버린다

_KY = 110_540.0


def _kx(lat):
    return 111_320.0 * math.cos(math.radians(lat))


def _project(lat, lng, a, b):
    """점을 선분 a-b((lat,lon))에 투영. (거리 m, t, (lat,lon))"""
    kx, ky = _kx(lat), _KY
    ax, ay = (a[1] - lng) * kx, (a[0] - lat) * ky
    bx, by = (b[1] - lng) * kx, (b[0] - lat) * ky
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / l2))
    px, py = ax + t * dx, ay + t * dy
    return math.hypot(px, py), t, (lat + py / ky, lng + px / kx)


def _segments(G, u, v):
    """링크의 좌표열을 (시작좌표, 끝좌표, 누적길이m) 선분 목록으로."""
    from .graph import edge_coords
    coords = edge_coords(G, u, v)
    out, acc = [], 0.0
    for a, b in zip(coords[:-1], coords[1:]):
        seg = haversine_m(a[0], a[1], b[0], b[1])
        out.append((a, b, acc, seg))
        acc += seg
    return out, acc


class LineOfSight:
    """좌표→투영점 선분이 건물·옹벽·담장을 가로지르는지. 색인이 없으면 항상 통과."""

    def __init__(self, buildings=None, obstacles=None):
        self.buildings = buildings if (buildings is not None and getattr(buildings, "loaded", False)) else None
        self.obstacles = obstacles
        self._btree = None

    def _building_tree(self):
        if self._btree is None and self.buildings is not None:
            try:
                from shapely.strtree import STRtree
                self._bgeoms = [p for p, _n in self.buildings.polys]
                self._btree = STRtree(self._bgeoms)
            except Exception:
                self._btree = False
        return self._btree

    def blocked(self, p_lat, p_lng, q_lat, q_lng) -> bool:
        if self.buildings is None and self.obstacles is None:
            return False
        try:
            from shapely.geometry import LineString
        except Exception:
            return False
        line = LineString([(p_lng, p_lat), (q_lng, q_lat)])
        if line.length == 0:
            return False
        tree = self._building_tree()
        if tree:
            for i in tree.query(line):
                g = self._bgeoms[i]
                # 좌표 자체가 건물 안(출입구·대표점)인 경우는 건물 경계를 한 번 나가는 것이라 허용
                if g.crosses(line) and not (g.contains(line.interpolate(0)) or g.contains(line.interpolate(1, normalized=True))):
                    return True
        if self.obstacles is not None:
            try:
                if self.obstacles.crosses_barrier(line):
                    return True
            except Exception:
                return False
        return False


def candidates(store, lat, lng, profile, max_slope_deg, allowed=None, radius_m=DEFAULT_RADIUS_M,
               k=DEFAULT_K, los: LineOfSight | None = None) -> list:
    """투영 후보 목록(가까운 순, 최대 k).

    각 항목: {"u","v","t","lat","lng","dist_m","link_type","seg_index"}.
    후보가 없으면 빈 목록 — 호출 측은 노드 스냅으로 폴백한다.
    """
    G = store.graph
    node_ids, coords = store.node_index
    # 반경 안 노드에 붙은 링크만 본다(전 링크 투영은 불필요)
    kx = _kx(lat)
    near_nodes = []
    for nid, (nlat, nlon) in zip(node_ids, coords):
        if abs(nlat - lat) * _KY > radius_m * 4 or abs(nlon - lng) * kx > radius_m * 4:
            continue
        near_nodes.append(nid)
    seen, cands = set(), []
    for n in near_nodes:
        for m, data in G[n].items():
            key = frozenset((n, m))
            if key in seen:
                continue
            seen.add(key)
            lt = data.get("link_type")
            if lt in NO_PROJECT_TYPES:
                continue
            if not edge_passable(data, profile, max_slope_deg):
                continue
            if allowed and (n not in allowed and m not in allowed):
                continue
            segs, total = _segments(G, n, m)
            best = None
            for i, (a, b, acc, seg) in enumerate(segs):
                d, t, p = _project(lat, lng, a, b)
                if best is None or d < best[0]:
                    best = (d, acc + t * seg, p, i)
            if best is None or best[0] > radius_m:
                continue
            d, along, p, si = best
            t_link = 0.0 if total <= 0 else max(0.0, min(1.0, along / total))
            cands.append({"u": n, "v": m, "t": t_link, "lat": p[0], "lng": p[1], "dist_m": d,
                          "link_type": lt, "seg_index": si, "length": total})
    if not cands:
        return []
    # 계층 스냅 — 보행 링크가 가까이 있으면 도로 링크 제외
    if any(c["link_type"] in PEDESTRIAN_TYPES and c["dist_m"] <= LAYER_RADIUS_M for c in cands):
        cands = [c for c in cands if c["link_type"] in PEDESTRIAN_TYPES or c["link_type"] != "road"]
    # LOS
    if los is not None:
        cands = [c for c in cands if not los.blocked(lat, lng, c["lat"], c["lng"])]
    cands.sort(key=lambda c: c["dist_m"])
    return cands[:k]


def attach(H, cand: dict, node_id: str) -> str:
    """그래프 사본 H 에 가상 노드와 분할 링크 두 개를 얹는다. 원 링크는 유지.

    링크 끝에서 MIN_SPLIT_M 이내면 가상 노드를 만들지 않고 그 끝 노드 id 를 돌려준다.
    """
    G = H
    u, v, t = cand["u"], cand["v"], cand["t"]
    total = float(cand.get("length") or G[u][v].get("length") or 0.0)
    if total <= 0 or t * total < MIN_SPLIT_M:
        return u
    if (1.0 - t) * total < MIN_SPLIT_M:
        return v
    from .graph import edge_coords
    data = dict(G[u][v])
    coords = edge_coords(G, u, v)
    # 좌표열을 투영점에서 자른다
    si = cand["seg_index"]
    p = (cand["lat"], cand["lng"])
    head = coords[:si + 1] + [p]
    tail = [p] + coords[si + 1:]
    G.add_node(node_id, lat=p[0], lon=p[1], node_type="virtual", virtual=True)
    d1 = dict(data); d1["length"] = max(t * total, 0.1); d1["geometry"] = head if len(head) > 2 else None
    d2 = dict(data); d2["length"] = max((1.0 - t) * total, 0.1); d2["geometry"] = tail if len(tail) > 2 else None
    G.add_edge(u, node_id, **d1)
    G.add_edge(node_id, v, **d2)
    return node_id


def virtual_graph(store):
    """요청 단위 그래프 사본 — 노드·링크 속성 dict 는 얕게 복사된다."""
    return store.graph.copy()
