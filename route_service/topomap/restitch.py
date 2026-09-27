# -*- coding: utf-8 -*-
"""보도망·도로망 접합부 보강 (#87).

하이브리드 그래프(scripts/build_corridor_hybrid.py)는 회랑 경계에 남은 도로망 노드를 **가장 가까운
보도 노드 하나**에만 잇는다(경계 스티칭). 교차로 한가운데 있는 도로 노드는 네 모퉁이 보도 노드와
거리가 거의 같기 때문에, 0.2m 차이로 길 건너 모퉁이 하나만 이어지는 일이 생긴다. 그러면 이쪽 보도에서
도로망으로 나갈 연결이 없어 횡단보도를 건너갔다가 되돌아오는 경로(유턴)가 만들어진다.

접합 보강 (find_restitch / apply_restitch) — 링크는 **추가만** 한다
  후보 : 기존 스티칭 (도로 노드 o → 보도 노드 t0, 거리 d0) 마다, o 에서 d0 + MARGIN_M 안이고
         STITCH_MAX_M 이하인 다른 보도 노드
  게이트: G1 이미 o 와 이어진 노드 제외 · G2 신설선이 o·t 에 닿지 않는 기존 링크와 교차하면 제외
         · G3 o 당 최대 MAX_PER_NODE 개, 가까운 순
         · G4 o 를 거쳐 t ↔ (o 에 이미 이어진 보도 노드) 로 가는 길이 보도망 안의 길보다 짧아지고
           그 보도망 길이 횡단보도를 건넌다면 제외
         · G5 새 연결 뒤 t 에서 주변(BYPASS_RADIUS_M) 보도 노드까지의 길이 짧아지면서 건너는 횡단보도 수가
           줄면 제외 — 도로망을 몇 홉 거쳐 횡단보도를 건너뛰는 '표시 없는 횡단' 지름길을 막는다
  속성 : link_type=sidewalk, stitched=True, topo_source=derived, derived_kind=restitch,
         derived_via=t0, confidence=CONFIDENCE, 경사는 같은 o 의 기존 스티칭 경사를 따른다
         (신설선은 기존 스티칭과 길이 차가 MARGIN_M 이내이고 같은 교차로 안에 있다)

보도 끝 연결 (connect_dead_ends) — 회랑 경계에서 끊긴 보도 끝(차수 1)을 DEADEND_MAX_M 안의 도로망 링크 위
최근접점에 잇는다(필요하면 링크를 그 점에서 나눈다). G2·G5 게이트를 똑같이 적용한다. derived_kind=deadend_join.

링크 탐색은 링크가 지나는 격자 칸 전부에 링크를 넣은 색인으로 한다 — 양 끝이 멀리 있는 긴 도로 링크
(예술공원로 746m 등)도 옆을 지나면 찾는다.
"""
from __future__ import annotations

import math

MARGIN_M = 3.0
STITCH_MAX_M = 25.0
MAX_PER_NODE = 2
CONFIDENCE = 0.9
DEADEND_MAX_M = 10.0
BYPASS_RADIUS_M = 60.0
BYPASS_CUTOFF_M = 250.0
NO_JOIN_TYPES = ("crossing", "steps", "elevator", "underpass", "overpass")

_KY = 110_540.0
_CELL = 30.0


def _is_topo(n) -> bool:
    return str(n).startswith("T")


def _dist(p, q):
    return math.hypot(p[0] - q[0], p[1] - q[1])


def _ccw(a, b, c):
    return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(p1, p2, p3, p4) -> bool:
    """두 선분이 서로의 내부에서 교차하는가(끝점 접촉은 교차가 아니다)."""
    d1, d2 = _ccw(p3, p4, p1), _ccw(p3, p4, p2)
    d3, d4 = _ccw(p1, p2, p3), _ccw(p1, p2, p4)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def _proj(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    q = (a[0] + t * dx, a[1] + t * dy)
    return t, q, _dist(p, q)


class _Index:
    """노드·링크 격자 색인(평면 m 좌표). 링크는 지나는 칸 전부에 넣는다."""

    def __init__(self, G):
        n = G.number_of_nodes()
        self.lat0 = sum(a["lat"] for _, a in G.nodes(data=True)) / n
        self.kx = 111_320.0 * math.cos(math.radians(self.lat0))
        self.XY = {v: (a["lon"] * self.kx, a["lat"] * _KY) for v, a in G.nodes(data=True)}
        self.nodes, self.edges = {}, {}
        for v, p in self.XY.items():
            self.nodes.setdefault(self._c(p), set()).add(v)
        for a, b in G.edges():
            self.add_edge(a, b)

    @staticmethod
    def _c(p):
        return (int(p[0] // _CELL), int(p[1] // _CELL))

    def _cells(self, a, b):
        pa, pb = self.XY[a], self.XY[b]
        L = _dist(pa, pb)
        steps = max(1, int(L // (_CELL / 2)) + 1)
        out = set()
        for i in range(steps + 1):
            t = i / steps
            out.add(self._c((pa[0] + t * (pb[0] - pa[0]), pa[1] + t * (pb[1] - pa[1]))))
        return out

    def add_node(self, v, p):
        self.XY[v] = p
        self.nodes.setdefault(self._c(p), set()).add(v)

    def remove_node(self, v):
        p = self.XY.pop(v)
        self.nodes[self._c(p)].discard(v)

    def add_edge(self, a, b):
        k = frozenset((a, b))
        for c in self._cells(a, b):
            self.edges.setdefault(c, set()).add(k)

    def remove_edge(self, a, b):
        k = frozenset((a, b))
        for c in self._cells(a, b):
            s = self.edges.get(c)
            if s:
                s.discard(k)

    def near_nodes(self, p, r=1):
        cx, cy = self._c(p)
        out = []
        for i in range(-r, r + 1):
            for j in range(-r, r + 1):
                out.extend(self.nodes.get((cx + i, cy + j), ()))
        return out

    def near_edges(self, p, r=1):
        cx, cy = self._c(p)
        out = set()
        for i in range(-r, r + 1):
            for j in range(-r, r + 1):
                out |= self.edges.get((cx + i, cy + j), set())
        return out

    def crosses_any(self, p, q, allow_nodes=(), allow_edges=()):
        """p–q 선분이 기존 링크와 교차하는가. allow_nodes 에 닿는 링크·allow_edges 는 무시."""
        cand = self.near_edges(p, 1) | self.near_edges(q, 1)
        for k in cand:
            if k in allow_edges or any(n in k for n in allow_nodes):
                continue
            a, b = tuple(k)
            if segments_cross(p, q, self.XY[a], self.XY[b]):
                return True
        return False


def _w(a, b, e):
    return float(e.get("length") or 0.0)


def _paths_from(G, src, cutoff=BYPASS_CUTOFF_M):
    import networkx as nx
    return nx.single_source_dijkstra(G, src, cutoff=cutoff, weight=_w)


def _crossings_on(G, path) -> int:
    return sum(1 for a, b in zip(path[:-1], path[1:]) if G.edges[a, b].get("link_type") == "crossing")


def _snapshot(G, idx, t):
    """t 에서 주변 보도 노드까지의 (거리, 횡단보도 수) — G5 비교용. G 는 **보강 전 원본**을 넘긴다
    (앞서 채택한 연결이 만든 지름길을 기준으로 삼으면 연쇄로 차도 횡단이 열린다)."""
    pt = idx.XY[t]
    near = [k for k in idx.near_nodes(pt, 2) if _is_topo(k) and k != t and _dist(pt, idx.XY[k]) <= BYPASS_RADIUS_M]
    d, p = _paths_from(G, t)
    return {k: (d[k], _crossings_on(G, p[k])) for k in near if k in d}


def _creates_bypass(G, t, before) -> bool:
    d, p = _paths_from(G, t)
    for k, (d0, c0) in before.items():
        if k in d and d[k] + 1.0 < d0 and _crossings_on(G, p[k]) < c0:
            return True
    return False


def _bypasses_crossing(G, o, t, d_ot) -> bool:
    """G4 — o 를 거친 t↔k 가 보도망 길(o 제외)보다 짧고, 그 보도망 길이 횡단보도를 포함하면 True."""
    import networkx as nx
    Gv = nx.restricted_view(G, [o], [])
    for k in list(G[o]):
        if k == t or not _is_topo(k):
            continue
        via = d_ot + float(G.edges[o, k].get("length") or 0.0)
        try:
            length, path = nx.single_source_dijkstra(Gv, t, target=k, cutoff=via * 3.0 + 30.0, weight=_w)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue
        if via < length and any(Gv.edges[a, b].get("link_type") == "crossing"
                                for a, b in zip(path[:-1], path[1:])):
            return True
    return False


def find_restitch(G, margin_m: float = MARGIN_M, max_m: float = STITCH_MAX_M,
                  max_per_node: int = MAX_PER_NODE) -> list:
    """추가할 접합 링크 목록 [{o, t, via, length, slope, d0}]. G 는 바꾸지 않는다."""
    if not G.number_of_nodes():
        return []
    H = G.copy()     # 채택분을 누적해 G2·G4·G5 를 판정한다(새 링크끼리 만드는 지름길도 막는다)
    idx = _Index(H)
    out = []
    for u, v, e in list(G.edges(data=True)):
        if not e.get("stitched") or e.get("derived_kind") in ("restitch", "deadend_join"):
            continue
        if _is_topo(u) == _is_topo(v):
            continue
        o, t0 = (v, u) if _is_topo(u) else (u, v)
        po = idx.XY[o]
        d0 = _dist(po, idx.XY[t0])
        lim = min(d0 + margin_m, max_m)
        cands = sorted((_dist(po, idx.XY[t]), t) for t in idx.near_nodes(po)
                       if t != t0 and _is_topo(t) and t not in H[o] and _dist(po, idx.XY[t]) <= lim)
        added = 0
        for d, t in cands:
            if added >= max_per_node:
                break
            pt = idx.XY[t]
            if idx.crosses_any(po, pt, allow_nodes=(o, t)):
                continue
            if _bypasses_crossing(H, o, t, d):
                continue
            item = {"o": o, "t": t, "via": t0, "length": round(max(d, 0.5), 2),
                    "slope": float(e.get("slope") or 0.0), "d0": round(d0, 2)}
            before = _snapshot(G, idx, t)
            H.add_edge(o, t, length=item["length"], slope=item["slope"], link_type="sidewalk")
            if _creates_bypass(H, t, before):
                H.remove_edge(o, t)
                continue
            idx.add_edge(o, t)
            out.append(item)
            added += 1
    return out


def apply_restitch(G, items: list) -> int:
    n = 0
    for it in items:
        if G.has_edge(it["o"], it["t"]):
            continue
        G.add_edge(it["o"], it["t"], length=it["length"], slope=it["slope"], link_type="sidewalk",
                   width=None, curb_cut=None, tactile_paving=None, surface=None, link_name=None,
                   geometry=None, topo_source="derived", stitched=True, derived_kind="restitch",
                   derived_via=str(it["via"]), confidence=CONFIDENCE)
        n += 1
    return n


def connect_dead_ends(G, max_m: float = DEADEND_MAX_M) -> list:
    """G 를 직접 고친다. 반환: [{t, target, length, split}]"""
    if not G.number_of_nodes():
        return []
    G0 = G.copy()      # G5 기준 — 보강 전 원본
    idx = _Index(G)
    report = []
    dead = [n for n in list(G.nodes) if _is_topo(n) and G.degree(n) == 1]
    for t in dead:
        if G.degree(t) != 1:
            continue
        pt = idx.XY[t]
        best = None
        for k in idx.near_edges(pt, 1):
            a, b = tuple(k)
            if _is_topo(a) or _is_topo(b) or not G.has_edge(a, b):
                continue
            if G.edges[a, b].get("link_type") in NO_JOIN_TYPES:
                continue
            tt, q, d = _proj(pt, idx.XY[a], idx.XY[b])
            if d <= max_m and (best is None or d < best[0]):
                best = (d, a, b, tt, q)
        if best is None:
            continue
        d, a, b, tt, q = best
        target = None
        if tt <= 0.05 or _dist(q, idx.XY[a]) < 2.0:
            target = a
        elif tt >= 0.95 or _dist(q, idx.XY[b]) < 2.0:
            target = b
        if target is not None:
            if target in G[t]:
                continue
            q = idx.XY[target]
            d = _dist(pt, q)
        if idx.crosses_any(pt, q, allow_nodes=(t,) + ((target,) if target is not None else ()),
                           allow_edges=(frozenset((a, b)),)):
            continue
        e = dict(G.edges[a, b])
        before = _snapshot(G0, idx, t)
        split = target is None
        if split:
            target = "S%s_%s" % tuple(sorted((str(a), str(b))))
            i = 1
            while target in G:
                i += 1
                target = "S%s_%s_%d" % (tuple(sorted((str(a), str(b)))) + (i,))
            G.add_node(target, lat=q[1] / _KY, lon=q[0] / idx.kx, node_type="split")
            L = float(e.get("length") or _dist(idx.XY[a], idx.XY[b]))
            G.remove_edge(a, b)
            ea, eb = dict(e), dict(e)
            ea["length"], eb["length"] = round(max(L * tt, 0.5), 2), round(max(L * (1 - tt), 0.5), 2)
            ea["geometry"] = eb["geometry"] = None
            G.add_edge(a, target, **ea)
            G.add_edge(target, b, **eb)
        G.add_edge(t, target, length=round(max(d, 0.5), 2), slope=float(e.get("slope") or 0.0),
                   link_type="sidewalk", width=None, curb_cut=None, tactile_paving=None, surface=None,
                   link_name=None, geometry=None, topo_source="derived", stitched=True,
                   derived_kind="deadend_join", confidence=CONFIDENCE)
        if _creates_bypass(G, t, before):          # G5 — 되돌린다
            G.remove_edge(t, target)
            if split:
                G.remove_node(target)
                G.add_edge(a, b, **e)
            continue
        if split:
            idx.add_node(target, q)
            idx.remove_edge(a, b)
            idx.add_edge(a, target)
            idx.add_edge(target, b)
        idx.add_edge(t, target)
        report.append({"t": t, "target": target, "length": round(d, 2), "split": split})
    return report
