# -*- coding: utf-8 -*-
"""우회 삼각형 직결 링크 정제 (#67, v1.23.0) — 합성 그래프·폴리곤으로 게이트·신뢰도·반영·라우팅 활성 하한을 검증한다."""
import math
import os
import sys

import networkx as nx
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from route_service.topomap import refine as rf  # noqa: E402
from route_service.topomap.obstacles import ObstacleIndex  # noqa: E402
from route_service.engine.graph import NetworkStore  # noqa: E402
from route_service.engine.planner import plan, edge_passable, edge_cost  # noqa: E402
from route_service.engine import profiles as prof  # noqa: E402

shapely = pytest.importorskip("shapely")
from shapely.geometry import Polygon, LineString, Point  # noqa: E402

LAT, LON = 37.3900, 126.9500
KX, KY = 111_320.0 * math.cos(math.radians(LAT)), 110_540.0


def _pt(dx, dy):
    """원점에서 동쪽 dx m, 북쪽 dy m 인 (lat, lon)."""
    return (LAT + dy / KY, LON + dx / KX)


def _poly(x0, y0, x1, y1):
    return Polygon([(LON + x0 / KX, LAT + y0 / KY), (LON + x1 / KX, LAT + y0 / KY),
                    (LON + x1 / KX, LAT + y1 / KY), (LON + x0 / KX, LAT + y1 / KY)])


def _edge(a, b, **kw):
    d = dict(slope=1.0, link_type="sidewalk", width=2.0, curb_cut=True, surface="블록", link_name=None, geometry=None)
    d.update(kw)
    return d


def triangle_graph(mid_type="sidewalk"):
    """A(0,0) ─ M(6,8) ─ B(12,0): 우회 20m, 직선 12m (우회비 1.67). D 는 A 서쪽 30m 의 직진 이웃."""
    G = nx.Graph()
    for n, (dx, dy) in {"A": (0, 0), "M": (6, 8), "B": (12, 0), "D": (-30, 0), "E": (42, 0)}.items():
        la, lo = _pt(dx, dy)
        G.add_node(n, lat=la, lon=lo, node_type="intersection")
    G.add_edge("A", "M", length=10.0, **_edge("A", "M", link_type=mid_type))
    G.add_edge("M", "B", length=10.0, **_edge("M", "B", link_type=mid_type))
    G.add_edge("D", "A", length=30.0, **_edge("D", "A"))
    G.add_edge("B", "E", length=30.0, **_edge("B", "E"))
    return G


def test_find_candidates_detour_triangle():
    G = triangle_graph()
    c = rf.find_candidates(G)
    assert len(c) == 1 and {c[0]["a"], c[0]["b"]} == {"A", "B"} and c[0]["m"] == "M"
    assert 11.5 < c[0]["straight_m"] < 12.5 and abs(c[0]["ratio"] - 20 / c[0]["straight_m"]) < 0.01


def test_hard_gate_sidewalk_coverage_and_crossing():
    G = triangle_graph()
    c = rf.find_candidates(G)[0]
    inside = rf.SidewalkIndex([{"geom": _poly(-40, -2, 50, 10), "width": 2.5}])
    assert rf.hard_gate(G, c, inside, None) is None
    outside = rf.SidewalkIndex([{"geom": _poly(-40, 3, 50, 10), "width": 2.5}])   # 직결선(y=0)이 폴리곤 밖
    assert rf.hard_gate(G, c, outside, None).startswith("H1")
    # 우회 구간이 횡단보도면 H11
    G2 = triangle_graph(mid_type="crossing")
    assert rf.hard_gate(G2, rf.find_candidates(G2)[0], inside, None).startswith("H11")


def test_hard_gate_obstacles_block():
    G = triangle_graph(); c = rf.find_candidates(G)[0]
    sw = rf.SidewalkIndex([{"geom": _poly(-40, -2, 50, 10), "width": 2.5}])
    wall = LineString([(LON + 6 / KX, LAT - 3 / KY), (LON + 6 / KX, LAT + 3 / KY)])   # 직결선을 가로지르는 옹벽
    ob = ObstacleIndex([{"obstacle": "wall", "geom": wall}])
    assert "옹벽" in rf.hard_gate(G, c, sw, ob)
    # 볼라드 두 개 간격 0.5m 가 선분 위 — 통과 간격 부족
    ob2 = ObstacleIndex([{"obstacle": "street_furniture", "geom": Point(LON + 6 / KX, LAT + 0.3 / KY)},
                         {"obstacle": "street_furniture", "geom": Point(LON + 6 / KX, LAT - 0.2 / KY)}])
    assert rf.hard_gate(G, c, sw, ob2) is not None


def test_confidence_inset_fragmentation_touch_exemption():
    G = triangle_graph(); c = rf.find_candidates(G)[0]
    wide = rf.SidewalkIndex([{"geom": _poly(-40, -2, 50, 10), "width": 2.5}])
    assert rf.confidence(G, c, wide)[0] == 1.0
    # 폴리곤 경계가 선분에 0.1m — 인셋(0.2m) 밖 → ×0.55
    edge = rf.SidewalkIndex([{"geom": _poly(-40, -0.1, 50, 10), "width": 2.5}])
    v, why = rf.confidence(G, c, edge)
    assert v == pytest.approx(0.55) and any("인셋" in w for w in why)
    # 도엽 경계에서 맞닿은 두 폴리곤(간격 0) — 파편화 감점 없음
    touching = rf.SidewalkIndex([{"geom": _poly(-40, -2, 6, 10), "width": 2.5}, {"geom": _poly(6, -2, 50, 10), "width": 2.5}])
    assert rf.confidence(G, c, touching)[0] == 1.0
    # 0.5m 벌어진 두 폴리곤 — 파편화 ×0.60 (H1 은 0.3m 까지만 흡수하므로 게이트도 걸린다)
    gap = rf.SidewalkIndex([{"geom": _poly(-40, -2, 5.7, 10), "width": 2.5}, {"geom": _poly(6.2, -2, 50, 10), "width": 2.5}])
    assert rf.hard_gate(G, c, gap, None).startswith("H1")
    # 폭 1.2m 보도 → ×0.70
    narrow = rf.SidewalkIndex([{"geom": _poly(-40, -2, 50, 10), "width": 1.2}])
    assert rf.confidence(G, c, narrow)[0] == pytest.approx(0.70)


def test_apply_adds_derived_link_and_profiles_gate_by_confidence():
    G = triangle_graph(); c = rf.find_candidates(G)[0]
    c["confidence"], c["penalties"] = 0.58, []
    assert rf.apply(G, [c]) == 1 and G.has_edge("A", "B")
    d = G["A"]["B"]
    assert d["topo_source"] == "derived" and d["confidence"] == 0.58 and d["link_type"] == "sidewalk"
    assert G.has_edge("A", "M") and G.has_edge("M", "B"), "기존 링크는 지우지 않는다"
    wm, we, walk = prof.PROFILES["wheelchair_manual"], prof.PROFILES["wheelchair_electric"], prof.PROFILES["walk"]
    assert not edge_passable(d, wm, wm.hard_slope()), "수동 휠체어 하한 0.60 미만"
    assert edge_passable(d, we, we.hard_slope()) and edge_passable(d, walk, walk.hard_slope())
    # 비용: length × (1 + 4(1−c))
    assert edge_cost(d, walk) == pytest.approx(d["length"] * (1 + 4 * 0.42), rel=1e-3)   # DEM 없이 slope 0
    st = NetworkStore(); st.load_graph_object(G, version="t", region="t")
    assert plan(st, "D", "E", wm, 1)["routes"][0]["path"] == ["D", "A", "M", "B", "E"]
    # 도보 프로필은 통행 가능하지만 c=0.58 이면 비용 2.7배라 우회로가 여전히 싸다 — 증거가 쌓여야 이긴다
    assert plan(st, "D", "E", walk, 1)["routes"][0]["path"] == ["D", "A", "M", "B", "E"]
    # 신뢰도가 높으면 수동 휠체어도 지름길
    d["confidence"] = 0.9
    st2 = NetworkStore(); st2.load_graph_object(G, version="t", region="t")
    assert plan(st2, "D", "E", wm, 1)["routes"][0]["path"] == ["D", "A", "B", "E"]
    assert plan(st2, "D", "E", walk, 1)["routes"][0]["path"] == ["D", "A", "B", "E"]


def test_refine_pipeline_end_to_end():
    G = triangle_graph()
    sw = rf.SidewalkIndex([{"geom": _poly(-40, -2, 50, 10), "width": 2.5}])
    res = rf.refine(G, sw, ObstacleIndex([]))
    assert res["reasons"]["adopted"] == 1 and res["adopted"][0]["confidence"] == 1.0
    assert rf.apply(G, res["adopted"], 0.6) == 1


# ---- 이면도로 횡단 교량 (gap bridge) ----

def gap_graph(road_name="현충로52번길", ratio_ok=True):
    """A(0,0)·B(12,0) 는 보도 노드, 사이를 남북 도로(R1–R2, x=6)가 가른다.
    A·B 는 도로를 따라가는 먼 우회로(A–P–Q–B, 총 200m)로만 연결된다."""
    G = nx.Graph()
    pts = {"A": (0, 0), "B": (12, 0), "A0": (-20, 0), "B0": (32, 0), "R1": (6, -40), "R2": (6, 40),
           "P": (0, 100), "Q": (12, 100)}
    for n, (dx, dy) in pts.items():
        la, lo = _pt(dx, dy)
        G.add_node(n, lat=la, lon=lo, node_type="intersection")
    G.add_edge("A0", "A", length=20.0, topo_source="topo1k", **_edge("A0", "A"))
    G.add_edge("B", "B0", length=20.0, topo_source="topo1k", **_edge("B", "B0"))
    G.add_edge("R1", "R2", length=80.0, **_edge("R1", "R2", link_type="road", link_name=road_name))
    via = 100.0 if ratio_ok else 12.0
    G.add_edge("A", "P", length=via / 2 if not ratio_ok else 100.0, **_edge("A", "P"))
    G.add_edge("P", "Q", length=12.0 if ratio_ok else 1.0, **_edge("P", "Q"))
    G.add_edge("Q", "B", length=100.0 if ratio_ok else 5.0, **_edge("Q", "B"))
    return G


def test_gap_bridge_minor_road_adopted():
    G = gap_graph()
    c = rf.find_gap_bridges(G)
    hit = [x for x in c if {x["a"], x["b"]} == {"A", "B"}]
    assert len(hit) == 1 and hit[0]["gate"] is None and hit[0]["road_name"] == "현충로52번길"
    assert hit[0]["confidence"] == rf.GAP_CONFIDENCE and hit[0]["ratio"] > rf.GAP_RATIO_MIN
    n = rf.apply_gap_bridges(G, c)
    assert n == 1 and G.has_edge("A", "B")
    d = G["A"]["B"]
    assert d["link_type"] == "crossing" and d["unmarked"] is True and d["topo_source"] == "derived"
    # 수동 휠체어(하한 0.60)는 통과, 시각장애(0.70)는 불가
    wm, vi = prof.PROFILES["wheelchair_manual"], prof.PROFILES["visual"]
    assert edge_passable(d, wm, wm.hard_slope())
    assert not edge_passable(d, vi, vi.hard_slope())


def test_gap_bridge_major_road_gated():
    G = gap_graph(road_name="소곡로")
    c = [x for x in rf.find_gap_bridges(G) if {x["a"], x["b"]} == {"A", "B"}]
    assert len(c) == 1 and c[0]["gate"].startswith("G1")
    assert rf.apply_gap_bridges(G, c) == 0 and not G.has_edge("A", "B")


def test_gap_bridge_short_detour_skipped():
    G = gap_graph(ratio_ok=False)
    c = [x for x in rf.find_gap_bridges(G) if {x["a"], x["b"]} == {"A", "B"}]
    assert c == []


def test_unmarked_crossing_sentence():
    from route_service.engine.steps import _sentence
    s = _sentence("straight", 17.2, "현충로52번길", {"link_type": "crossing", "unmarked": True}, [])
    assert "이면도로를 건너" in s and "횡단보도 표시가 없으니" in s
    s2 = _sentence("straight", 17.2, None, {"link_type": "crossing"}, [])
    assert "이면도로" not in s2
