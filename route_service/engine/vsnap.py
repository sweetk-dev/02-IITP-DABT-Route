# -*- coding: utf-8 -*-
"""링크 투영 스냅 — 출발·도착 좌표를 최근접 **링크 위의 점**에 가상 노드로 붙인다 (#67, v1.23.0).

종전 `snap.snap()` 은 최근접 **노드**에만 붙였다. 링크 길이 중앙값 47m, 100m 초과 링크가
2,250개인 그래프에서는 좌표가 긴 링크 중간에 있으면 링크 한쪽 끝까지 갔다가 되돌아오는
경로가 구조적으로 나온다(실측: 안양아트센터 노드 60.4m 대 링크 위 점 35.5m, 우회비 1.55).

가상 노드는 공유 그래프를 건드리지 않는다 — 요청마다 그래프 사본(`attach()`)에 노드와
분할 링크 두 개를 얹고, 원 링크는 그대로 둔다(다른 경로에 영향 없음).

같은 원 링크 위에 가상 노드가 둘 이상 붙으면(출발·도착이 한 링크 위에 있는 경우) 그 둘을
잇는 직결 가상 링크도 함께 얹는다. 분할 링크만으로는 두 가상 노드가 원 링크의 끝 노드를
거쳐야만 이어져, 링크 끝까지 갔다가 되돌아오는 경로가 된다(`attach()` 주석 참고).

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


# 요청 단위 그래프 사본(H)의 graph 속성에 두는 등록부 키 —
# {frozenset((u, v)): {"origin": 기준 끝 노드, "items": [(기준 끝에서의 거리 m, 가상 노드 id, 좌표, 기준 방향 선분 번호)]}}
_REGISTRY_KEY = "_vsnap_on_edge"


def _orig_length(data: dict, fallback: float = 0.0) -> float:
    """가상 링크에 남길 원 링크 길이(m).

    원 링크 속성의 length 를 쓴다 — 경로 탐색이 원 링크를 "짧은 링크"로 보는지와 같은 값이어야
    실제 그래프의 짧은 링크(15m 미만)에서 잘린 가상 링크가 종전과 같이 예외를 받는다.
    length 가 없거나 0 이면 fallback(좌표열로 잰 길이)을 쓴다.
    """
    try:
        v = float(data.get("length") or 0.0)
    except (TypeError, ValueError):
        v = 0.0
    return v if v > 0 else float(fallback or 0.0)


def _link_between(G, coords, data, a, b):
    """같은 원 링크 위 두 가상 노드 a·b 를 잇는 직결 가상 링크를 얹는다.

    인자
      coords : 원 링크 좌표열(기준 끝 노드에서 시작하는 방향)
      data   : 원 링크 속성 dict — 경사·종류·폭 등은 그대로 상속한다
      a, b   : 등록부 항목 (기준 끝에서의 거리 m, 노드 id, (lat, lon), 선분 번호)
    길이는 두 투영점 사이 구간 길이, geometry 는 원 좌표열에서 그 구간만 잘라 쓴다.
    구간 안에 꺾임점이 없으면 geometry 는 None(두 노드를 잇는 직선) — 분할 링크와 같은 규칙.
    """
    if a[0] > b[0]:
        a, b = b, a
    mid = list(coords[a[3] + 1:b[3] + 1])        # 두 투영점 사이에 놓인 원 좌표열의 꺾임점
    geom = [a[2]] + mid + [b[2]]
    d = dict(data)
    d["length"] = max(b[0] - a[0], 0.1)          # 분할 링크와 같은 하한(0 길이 링크 방지)
    # 경사는 원 링크에서 상속하므로 "짧은 링크 경사 예외"는 원 링크 길이로 판정해야 한다
    # (planner.slope_ref_length). data 는 원 링크 속성이라 length 가 곧 원 링크 길이다.
    d["orig_length"] = _orig_length(data)
    d["geometry"] = geom if len(geom) > 2 else None
    G.add_edge(a[1], b[1], **d)


def attach(H, cand: dict, node_id: str) -> str:
    """그래프 사본 H 에 가상 노드와 분할 링크 두 개를 얹는다. 원 링크는 유지.

    링크 끝에서 MIN_SPLIT_M 이내면 가상 노드를 만들지 않고 그 끝 노드 id 를 돌려준다.

    같은 원 링크에 먼저 붙은 가상 노드가 있으면 그 노드들과 새 노드를 잇는 직결 링크도 얹는다.
    분할 링크(`u–V`, `V–v`)만 있으면 한 링크 위의 두 가상 노드는 끝 노드 u 나 v 를 거쳐야만
    이어진다 — 200m 링크의 40% 지점에서 60% 지점으로 가는 40m 이동이 "끝 노드까지 120m 간 뒤
    유턴해 80m" 로 계산된다. 후보의 (u, v) 표기 방향이 서로 뒤집혀 있어도 같은 링크로 보고,
    위치는 먼저 등록된 쪽의 u(기준 끝)에서 잰 거리로 환산해 비교한다.
    한쪽이 끝 노드로 스냅된 경우에는 가상 노드가 하나뿐이고 그 노드의 분할 링크가 곧
    끝 노드까지의 직결 구간이므로 추가 링크가 필요 없다.
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
    # 분할 링크는 원 링크의 경사를 상속하고 길이만 짧아진다. 경사 판정의 "짧은 링크" 예외가
    # 잘린 길이로 적용되지 않도록 원 링크 길이를 따로 남긴다(거리·시간 계산은 length 그대로).
    d1["orig_length"] = d2["orig_length"] = _orig_length(data, total)
    G.add_edge(u, node_id, **d1)
    G.add_edge(node_id, v, **d2)

    # 같은 원 링크 위의 가상 노드끼리 직결 — 등록부는 이 사본(H)에만 둔다(공유 그래프 불변)
    reg = G.graph.setdefault(_REGISTRY_KEY, {})
    entry = reg.setdefault(frozenset((u, v)), {"origin": u, "items": []})
    n_seg = len(coords) - 1
    if entry["origin"] == u:
        along, seg_i, base = t * total, si, coords
    else:
        # 먼저 등록된 후보와 표기 방향이 반대 — 거리·선분 번호·좌표열을 기준 방향으로 뒤집는다
        along, seg_i, base = (1.0 - t) * total, n_seg - 1 - si, list(reversed(coords))
    item = (along, node_id, p, seg_i)
    for other in entry["items"]:
        _link_between(G, base, data, other, item)
    entry["items"].append(item)
    return node_id


def virtual_graph(store):
    """요청 단위 그래프 사본 — 노드·링크 속성 dict 는 얕게 복사된다."""
    H = store.graph.copy()
    # 등록부는 사본마다 새로 시작한다 — copy() 는 graph 속성 값을 얕게 복사하므로,
    # 지우지 않으면 다른 요청이 남긴 가상 노드 id 를 참조할 수 있다.
    H.graph.pop(_REGISTRY_KEY, None)
    return H
