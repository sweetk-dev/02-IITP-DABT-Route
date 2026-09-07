# -*- coding: utf-8 -*-
"""우회 삼각형 직결 링크 정제 (#67, v1.23.0).

A–M, M–B 인접 링크는 있는데 A–B 직결이 없어 짧은 거리를 크게 우회하는 지점에, 규칙만으로
직결 링크를 신설한다. 사람 검수는 없다 — 안전은 하드 게이트가, 선호는 신뢰도(비용)가 맡는다.
(설계서 90-리포트_아카이브/보행망_우회삼각형_자동정제_설계_2026-09-04.md, 교차검토 2026-09-07)

  후보   : 차수 무관. 우회비(경로/직선) ≥ 1.41, 우회량(경로−직선) < 20m, 직선 ≤ 25m
  하드   : H1 인도 면형 밖(간격 0.3m 흡수) · H3/H4/H6 계단·옹벽·담장·가로시설물(ObstacleIndex)
           · H8 신설선 종단경사 > 8° · H9 25m 초과 · H10 고도차 > 1m & 인접 경사 > 5%(지그재그 경사로)
           · H11 우회 구간이 횡단보도 링크 · H12 점형 지물 통과 간격 < 0.8m
  신뢰도 : 1.0 에서 곱셈 감점 — 인셋 0.2m 밖 ×0.55 · 회랑폭 1.5m 미만 ×0.70 · 파편화(A·B 다른 폴리곤,
           **맞닿지 않은** 경우만) ×0.60 · 접합각 60° 미만 ×0.70
  반영   : topo_source='derived', confidence=c. 활성은 프로필별 하한(planner.edge_passable)이 판단한다.
           링크는 **추가만** 한다 — 기존 링크(지그재그 경사로 포함)는 절대 지우지 않는다.

산식 정정 근거 (2026-09-07): 1:1000 도화 허용오차 0.2~0.5m 에 0.5m 인셋을 양쪽에 주면 1.5m 표준
보도의 유효폭이 0.5m 만 남아 정상 링크도 떨어진다. 휠체어 반폭 0.35m + 도화 오차 → 0.2m.
도엽 경계에서 맞닿은 두 폴리곤(간격 ≤ 0.1m)은 실제 단절이 아니다.
"""
from __future__ import annotations

import math

from ..engine.geo import haversine_m

RATIO_MIN = 1.41
DETOUR_MAX_M = 20.0
NEW_LINK_MAX_M = 25.0
GAP_TOL_M = 0.3            # 인도 폴리곤 도엽 sliver gap 흡수 (H1)
INSET_M = 0.2              # 경계 인셋 감점 기준 (0.5 → 0.2)
TOUCH_TOL_M = 0.1          # 두 폴리곤이 이 간격 안이면 '맞닿음' — 파편화 감점 면제
END_TRIM_M = 1.0           # 인셋 판정에서 제외하는 양 끝 길이(끝점은 기존 노드)
MIN_WIDTH_M = 1.5
HARD_SLOPE_DEG = 8.0
ZIGZAG_DZ_M = 1.0
ZIGZAG_SLOPE_PCT = 5.0
BOLLARD_GAP_M = 0.8
ANGLE_MIN_DEG = 60.0
CROSSING_TYPES = ("crossing",)

_KY = 110_540.0


def _kx(lat):
    return 111_320.0 * math.cos(math.radians(lat))


def _bearing(a, b):
    """(lat,lon) a→b 방위각(도)."""
    dy = (b[0] - a[0]) * _KY
    dx = (b[1] - a[1]) * _kx(a[0])
    return math.degrees(math.atan2(dx, dy)) % 360.0


def _angle_between(b1, b2):
    d = abs(b1 - b2) % 360.0
    return min(d, 360.0 - d)


class SidewalkIndex:
    """인도 면형 색인 — covers/인셋/파편화/폭 판정."""

    def __init__(self, features: list):
        from shapely.strtree import STRtree
        self.geoms = [f["geom"] for f in features]
        self.widths = [f.get("width") for f in features]
        self.tree = STRtree(self.geoms) if self.geoms else None

    def _deg(self, m, lat):
        return m / _kx(lat)

    def _hits(self, geom):
        if self.tree is None:
            return []
        return [int(i) for i in self.tree.query(geom) if self.geoms[int(i)].intersects(geom)]

    def covers_line(self, line, tol_m=GAP_TOL_M) -> bool:
        """선분이 인도 면형(합집합, 간격 tol 흡수) 안에 완전히 들어가는가 (H1)."""
        from shapely.ops import unary_union
        # 폴리곤을 tol/2 씩 부풀리면 tol 까지의 틈이 닫힌다 (도엽 경계 sliver 흡수, 그 이상은 실제 단절)
        half = self._deg(tol_m / 2.0, line.centroid.y)
        idx = self._hits(line.buffer(half))
        if not idx:
            return False
        union = unary_union([self.geoms[i].buffer(half) for i in idx])
        return union.covers(line)

    def inside_inset(self, line, inset_m=INSET_M) -> bool:
        """경계에서 inset 만큼 안쪽 영역에도 들어가는가 (감점 항목)."""
        from shapely.geometry import LineString
        from shapely.ops import unary_union
        half = self._deg(GAP_TOL_M / 2.0, line.centroid.y)
        idx = self._hits(line.buffer(half))
        if not idx:
            return False
        # 닫힘(closing: +tol/2 → −tol/2)으로 도엽 경계 이음새를 지운 뒤, 바깥 경계 기준 inset 만큼 안쪽으로.
        # (부풀린 채로 크게 깎으면 이음새가 다시 벌어져 정상 링크가 인셋 밖으로 판정된다 — 실측 2026-09-07)
        closed = unary_union([self.geoms[i].buffer(half) for i in idx]).buffer(-half)
        inner = closed.buffer(-self._deg(inset_m, line.centroid.y))
        if inner.is_empty:
            return False
        # 양 끝 END_TRIM_M 은 뺀다 — 끝점은 이미 보행망이 쓰는 노드이고, 접속 노드는 흔히 폴리곤 경계 위에
        # 놓인다(실측: 안양문화원 앞 A 노드 경계 거리 0.00m). 인셋은 신설선 **내부**의 여유폭을 보는 것이다.
        L = line.length
        trim = min(self._deg(END_TRIM_M, line.centroid.y), L * 0.2)
        core = LineString([line.interpolate(trim), line.interpolate(L - trim)]) if L > 2 * trim else line
        return inner.covers(core)

    def fragmented(self, p_a, p_b) -> bool:
        """A·B 가 서로 다른 폴리곤에 속하고 그 폴리곤들이 맞닿아 있지 않은가."""
        from shapely.geometry import Point
        ia = set(self._hits(Point(p_a[1], p_a[0]).buffer(self._deg(GAP_TOL_M, p_a[0]))))
        ib = set(self._hits(Point(p_b[1], p_b[0]).buffer(self._deg(GAP_TOL_M, p_b[0]))))
        if not ia or not ib or (ia & ib):
            return False
        tol = self._deg(TOUCH_TOL_M, p_a[0])
        for i in ia:
            for j in ib:
                if self.geoms[i].distance(self.geoms[j]) <= tol:
                    return False        # 맞닿음 — 도엽 경계일 뿐
        return True

    def min_width(self, line):
        idx = self._hits(line)
        ws = [self.widths[i] for i in idx if self.widths[i]]
        return min(ws) if ws else None

    @classmethod
    def from_geojson(cls, path: str) -> "SidewalkIndex":
        import json
        from shapely.geometry import shape
        with open(path, encoding="utf-8") as fp:
            fc = json.load(fp)
        feats = []
        for f in fc.get("features", []):
            g = shape(f["geometry"])
            if not g.is_valid:
                g = g.buffer(0)
            if g.is_empty:
                continue
            feats.append({"geom": g, "width": (f.get("properties") or {}).get("width")})
        return cls(feats)


def find_candidates(G) -> list:
    """직결 링크가 없는 A–M–B 삼각형. 차수 제한 없음."""
    out, seen = [], set()
    for m in G.nodes:
        nbrs = list(G[m])
        if len(nbrs) < 2:
            continue
        pm = (G.nodes[m]["lat"], G.nodes[m]["lon"])
        for i in range(len(nbrs)):
            for j in range(i + 1, len(nbrs)):
                a, b = nbrs[i], nbrs[j]
                if G.has_edge(a, b):
                    continue
                key = (min(str(a), str(b)), max(str(a), str(b)), str(m))
                if key in seen:
                    continue
                seen.add(key)
                pa = (G.nodes[a]["lat"], G.nodes[a]["lon"]); pb = (G.nodes[b]["lat"], G.nodes[b]["lon"])
                straight = haversine_m(pa[0], pa[1], pb[0], pb[1])
                if straight <= 0.5 or straight > NEW_LINK_MAX_M:
                    continue
                via = float(G[a][m].get("length") or 0) + float(G[m][b].get("length") or 0)
                if via <= 0:
                    continue
                ratio = via / straight
                if ratio < RATIO_MIN or via - straight >= DETOUR_MAX_M:
                    continue
                out.append({"a": a, "m": m, "b": b, "pa": pa, "pb": pb, "pm": pm,
                            "straight_m": straight, "via_m": via, "ratio": ratio,
                            "type_am": G[a][m].get("link_type"), "type_mb": G[m][b].get("link_type")})
    return out


def _node_elev(G, n, dem):
    if dem is not None:
        try:
            z = dem.elevation(G.nodes[n]["lat"], G.nodes[n]["lon"])
            if z is not None:
                return float(z)
        except Exception:
            pass
    return None


def hard_gate(G, c: dict, sidewalks: SidewalkIndex, obstacles, dem=None) -> str | None:
    """배제 사유 문자열, 통과면 None."""
    from shapely.geometry import LineString
    a, m, b = c["a"], c["m"], c["b"]
    line = LineString([(c["pa"][1], c["pa"][0]), (c["pb"][1], c["pb"][0])])
    if c["straight_m"] > NEW_LINK_MAX_M:
        return "H9 25m 초과"
    # H11: 우회 구간(A–M·M–B) 자체가 횡단보도 링크면 신설선이 유도선·진입 경사로를 건너뛴다.
    # A·B 가 다른 횡단보도에 접해 있는 것만으로는 배제하지 않는다 — 보도 안 여부는 H1 이 본다.
    if c.get("type_am") in CROSSING_TYPES or c.get("type_mb") in CROSSING_TYPES:
        return "H11 횡단보도 링크 우회"
    if not sidewalks.covers_line(line):
        return "H1 인도 면형 밖"
    if obstacles is not None:
        why = obstacles.blocks_new_link(line)
        if why:
            return "H3/H4/H6 " + why
        try:
            if obstacles.furniture_gap_below(line, BOLLARD_GAP_M):
                return "H12 점형 지물 통과 간격 부족"
        except AttributeError:
            pass
    za, zb = _node_elev(G, a, dem), _node_elev(G, b, dem)
    if za is not None and zb is not None:
        dz = abs(za - zb)
        slope = math.degrees(math.atan2(dz, max(c["straight_m"], 0.1)))
        c["slope_deg"] = round(slope, 2)
        if slope > HARD_SLOPE_DEG:
            return "H8 종단경사 %.1f도" % slope
        if dz > ZIGZAG_DZ_M:
            for u, v in ((a, m), (m, b)):
                if float(G[u][v].get("slope") or 0) > math.degrees(math.atan(ZIGZAG_SLOPE_PCT / 100.0)):
                    return "H10 지그재그 경사로"
    return None


def confidence(G, c: dict, sidewalks: SidewalkIndex, dem=None) -> tuple:
    """(c0, 감점 사유 목록)"""
    from shapely.geometry import LineString
    a, m, b = c["a"], c["m"], c["b"]
    line = LineString([(c["pa"][1], c["pa"][0]), (c["pb"][1], c["pb"][0])])
    val, why = 1.0, []
    if not sidewalks.inside_inset(line):
        val *= 0.55; why.append("인셋 %.1fm 밖" % INSET_M)
    w = sidewalks.min_width(line)
    if w is not None and w < MIN_WIDTH_M:
        val *= 0.70; why.append("폭 %.1fm" % w)
    if sidewalks.fragmented(c["pa"], c["pb"]):
        val *= 0.60; why.append("파편화(비접촉)")
    # 고도차 감점은 두지 않는다 — 신설선의 양 끝은 우회로 A–M–B 의 양 끝과 같은 점이라 고도차가 항상 같다.
    # 단차·계단 위험은 H8(종단경사)·H10(지그재그)·H4(계단 면형)가 본다. (2026-09-07 정정)
    # 접합각 — A·B 에서 신설 링크와 (A–M·M–B 를 뺀) 다른 링크 사이 최소 각
    bab = _bearing(c["pa"], c["pb"])
    worst = None
    for n, other, base in ((a, b, bab), (b, a, (bab + 180.0) % 360.0)):
        for x in G[n]:
            if x == m:
                continue
            ang = _angle_between(base, _bearing((G.nodes[n]["lat"], G.nodes[n]["lon"]),
                                                 (G.nodes[x]["lat"], G.nodes[x]["lon"])))
            worst = ang if worst is None else min(worst, ang)
    if worst is not None and worst < ANGLE_MIN_DEG:
        val *= 0.70; why.append("접합각 %.0f도" % worst)
    return round(val, 3), why


def refine(G, sidewalks: SidewalkIndex, obstacles=None, dem=None) -> dict:
    """전체 파이프라인. 반환 {"candidates": [...], "adopted": [...], "reasons": Counter}"""
    from collections import Counter
    cands = find_candidates(G)
    reasons, adopted = Counter(), []
    for c in cands:
        why = hard_gate(G, c, sidewalks, obstacles, dem)
        c["gate"] = why
        if why:
            reasons[why.split(" ")[0]] += 1
            continue
        c0, pen = confidence(G, c, sidewalks, dem)
        c["confidence"], c["penalties"] = c0, pen
        adopted.append(c)
    reasons["adopted"] = len(adopted)
    return {"candidates": cands, "adopted": adopted, "reasons": reasons}


def apply(G, adopted: list, min_confidence: float = 0.0) -> int:
    """채택 후보를 그래프에 **추가**한다. 속성은 A–M 링크를 상속(길이·경사·지오메트리는 새로)."""
    n = 0
    for c in adopted:
        if c["confidence"] < min_confidence or G.has_edge(c["a"], c["b"]):
            continue
        base = dict(G[c["a"]][c["m"]])
        za, zb = base.get("elev_start"), base.get("elev_end")
        data = {k: v for k, v in base.items() if k not in ("geometry", "cw_mgmt_no", "cw_length_m", "stitched", "bridged")}
        data.update({"length": float(c["straight_m"]), "geometry": None,
                     "slope": float(c.get("slope_deg") or 0.0),
                     "topo_source": "derived", "confidence": float(c["confidence"]),
                     "derived_via": str(c["m"]), "link_type": base.get("link_type") or "sidewalk"})
        if data["link_type"] in ("crossing", "steps", "overpass", "underpass"):
            data["link_type"] = "sidewalk"
        G.add_edge(c["a"], c["b"], **data)
        n += 1
    return n


# ────────────────────────── 이면도로 횡단 교량 (gap bridge) ──────────────────────────
# 우회 삼각형과는 다른 결함: 좁은 이면도로 양쪽 보도 끝이 십수 m 거리인데 횡단 링크가 없어
# 블록을 한 바퀴 돈다(안양문화원 → 소방서 정류장: 직선 61m 를 374m, 실측 2026-09-07).
# 보도 폴리곤 안이 아니므로 우회 삼각형 게이트로는 절대 통과하지 못한다. 별도 규칙:
#   · 양 끝이 수치지형도 보도 노드(topo1k 링크에 붙은 노드), 직선 ≤ GAP_MAX_M
#   · 두 노드 사이 보행망 경로가 없거나 직선의 GAP_RATIO_MIN 배 이상
#   · 신설선이 **도로 링크를 정확히 하나**만 가로지르고, 그 도로가 이면도로(이름이 없거나 '…길'로 끝남 — 간선은 '…로'·'…대로')
#   · 장애물(옹벽·담장·계단·시설물) 저촉 없음, 종단경사 ≤ 8°
# 반영: link_type='crossing', unmarked=True(안내 문구 분기), curb_cut=None(미확인 — 수동휠체어는 False 일 때만 막힌다),
#       topo_source='derived', confidence=GAP_CONFIDENCE(0.65: 수동 0.60 활성·시각 0.70 비활성 — 시각장애 이용자는 표시된 횡단보도만).
GAP_MAX_M = 20.0
GAP_RATIO_MIN = 4.0
GAP_CONFIDENCE = 0.65
MINOR_ROAD_SUFFIX = ("길",)
MAJOR_ROAD_SUFFIX = ("대로", "로")


def _is_minor_road(name) -> bool:
    n = (name or "").strip()
    if not n:
        return True
    if n.endswith(MINOR_ROAD_SUFFIX):
        return True
    return False


def _sidewalk_node(G, n) -> bool:
    return any(G[n][x].get("topo_source") == "topo1k" and G[n][x].get("link_type") == "sidewalk" for x in G[n])


def _road_links_crossed(G, pa, pb, cell_nodes):
    """신설선이 가로지르는 도로 링크 목록 [(u, v, name)]."""
    from shapely.geometry import LineString
    seg = LineString([(pa[1], pa[0]), (pb[1], pb[0])])
    out, seen = [], set()
    for n in cell_nodes:
        for x in G[n]:
            key = frozenset((n, x))
            if key in seen:
                continue
            seen.add(key)
            d = G[n][x]
            if d.get("link_type") not in ("road", "unknown"):
                continue
            from ..engine.graph import edge_coords
            coords = edge_coords(G, n, x)
            line = LineString([(c[1], c[0]) for c in coords])
            if line.crosses(seg):
                out.append((n, x, d.get("link_name")))
    return out


def find_gap_bridges(G, obstacles=None, dem=None) -> list:
    """이면도로 횡단 교량 후보. 반환 항목은 우회 삼각형 후보와 같은 키 + gate/confidence."""
    import networkx as nx
    nodes = [n for n in G.nodes if _sidewalk_node(G, n)]
    # 격자 색인(약 30m 셀)
    grid = {}
    def cell(lat, lon):
        return (int(lat / 0.00027), int(lon / 0.00034))
    for n in G.nodes:
        grid.setdefault(cell(G.nodes[n]["lat"], G.nodes[n]["lon"]), []).append(n)
    def around(lat, lon):
        c = cell(lat, lon)
        out = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                out += grid.get((c[0] + di, c[1] + dj), [])
        return out
    out, seen = [], set()
    for a in nodes:
        pa = (G.nodes[a]["lat"], G.nodes[a]["lon"])
        near = around(*pa)
        for b in near:
            if b == a or b in G[a] or not _sidewalk_node(G, b):
                continue
            key = (min(str(a), str(b)), max(str(a), str(b)))
            if key in seen:
                continue
            seen.add(key)
            pb = (G.nodes[b]["lat"], G.nodes[b]["lon"])
            straight = haversine_m(pa[0], pa[1], pb[0], pb[1])
            if straight < 3.0 or straight > GAP_MAX_M:
                continue
            crossed = _road_links_crossed(G, pa, pb, near)
            if len(crossed) != 1:
                continue
            c = {"a": a, "m": None, "b": b, "pa": pa, "pb": pb, "pm": None, "straight_m": straight,
                 "via_m": None, "ratio": None, "type_am": "sidewalk", "type_mb": "sidewalk",
                 "kind": "gap_bridge", "road_name": crossed[0][2]}
            # 기존 경로 길이 (없으면 무한)
            try:
                via = nx.shortest_path_length(G, a, b, weight="length")
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                via = float("inf")
            c["via_m"], c["ratio"] = (None if via == float("inf") else via), (None if via == float("inf") else via / straight)
            if via != float("inf") and via / straight < GAP_RATIO_MIN:
                continue
            if not _is_minor_road(crossed[0][2]):
                c["gate"] = "G1 간선 횡단(%s)" % crossed[0][2]
                out.append(c); continue
            if obstacles is not None:
                from shapely.geometry import LineString
                why = obstacles.blocks_new_link(LineString([(pa[1], pa[0]), (pb[1], pb[0])]))
                if why:
                    c["gate"] = "G3 " + why; out.append(c); continue
            za, zb = _node_elev(G, a, dem), _node_elev(G, b, dem)
            if za is not None and zb is not None:
                slope = math.degrees(math.atan2(abs(za - zb), max(straight, 0.1)))
                c["slope_deg"] = round(slope, 2)
                if slope > HARD_SLOPE_DEG:
                    c["gate"] = "G8 종단경사 %.1f도" % slope; out.append(c); continue
            c["gate"] = None
            c["confidence"], c["penalties"] = GAP_CONFIDENCE, ["이면도로 횡단(횡단보도 표시 없음)"]
            out.append(c)
    return out


def apply_gap_bridges(G, cands: list) -> int:
    n = 0
    for c in cands:
        if c.get("gate") or G.has_edge(c["a"], c["b"]):
            continue
        G.add_edge(c["a"], c["b"], length=float(c["straight_m"]), slope=float(c.get("slope_deg") or 0.0),
                   link_type="crossing", width=None, curb_cut=None, surface=None, link_name=c.get("road_name"),
                   geometry=None, tactile_paving=None, topo_source="derived", confidence=float(c["confidence"]),
                   unmarked=True, derived_kind="gap_bridge")
        n += 1
    return n
