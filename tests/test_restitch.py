# -*- coding: utf-8 -*-
"""보도망·도로망 접합부 보강 (#87)."""
import math

import networkx as nx

from route_service.topomap import restitch as rs

LAT0, LON0 = 37.4174404, 126.9179724
KX = 111_320.0 * math.cos(math.radians(LAT0))
KY = 110_540.0


def _ll(dx, dy):
    return {"lat": LAT0 + dy / KY, "lon": LON0 + dx / KX}


def _graph():
    """교차로 한가운데 도로 노드 O, 모퉁이 보도 노드 A(서남 9.9m)·B(북 10.1m).
    A–B 사이에는 횡단보도가 있고, 기존 스티칭은 O–A 하나뿐이다."""
    G = nx.Graph()
    G.add_node(1, **_ll(0, 0), node_type="unknown")          # O (도로)
    G.add_node(2, **_ll(300, 300), node_type="unknown")      # 도로 먼 끝
    G.add_node("TA", **_ll(-9.7, -1.9), node_type="sidewalk")
    G.add_node("TB", **_ll(-0.4, 10.1), node_type="sidewalk")
    G.add_node("TC", **_ll(-9.7, 3.1), node_type="sidewalk")
    G.add_node("TB2", **_ll(40, 60), node_type="sidewalk")
    G.add_edge(1, 2, length=424, slope=1.0, link_type="road")
    G.add_edge(1, "TA", length=9.9, slope=1.9, link_type="sidewalk", stitched=True)
    G.add_edge("TA", "TC", length=5.0, slope=3.0, link_type="sidewalk")
    G.add_edge("TC", "TB", length=11.6, slope=0.6, link_type="crossing")
    G.add_edge("TB", "TB2", length=72, slope=1.0, link_type="sidewalk")
    return G


def test_adds_near_equal_corner():
    G = _graph()
    items = rs.find_restitch(G)
    pairs = {(it["o"], it["t"]) for it in items}
    assert (1, "TB") in pairs
    it = [x for x in items if x["t"] == "TB"][0]
    assert it["via"] == "TA" and abs(it["length"] - 10.1) < 0.3 and it["slope"] == 1.9


def test_apply_marks_derived_and_passable_for_all_profiles():
    from route_service.engine.planner import edge_passable
    from route_service.engine.profiles import get_profile
    G = _graph()
    n = rs.apply_restitch(G, rs.find_restitch(G))
    assert n >= 1
    e = G.edges[1, "TB"]
    assert e["topo_source"] == "derived" and e["derived_kind"] == "restitch" and e["stitched"]
    for pid in ("wheelchair_electric", "wheelchair_manual", "visual"):
        p = get_profile(pid)
        assert edge_passable(e, p, p.hard_slope())


def test_route_no_longer_crosses_and_returns():
    """보강 전: TB → 도로로 가려면 횡단보도(TC)를 건너 TA 에서 되돌아 나간다. 보강 후: 바로 나간다."""
    G = _graph()
    before = nx.shortest_path(G, "TB", 2, weight="length")
    assert "TC" in before
    rs.apply_restitch(G, rs.find_restitch(G))
    after = nx.shortest_path(G, "TB", 2, weight="length")
    assert after == ["TB", 1, 2]


def test_skip_beyond_margin():
    G = _graph()
    G.nodes["TB"].update(_ll(-0.4, 14.0))    # 9.9 + 3m 초과
    assert not [it for it in rs.find_restitch(G) if it["t"] == "TB"]


def test_skip_when_crossing_existing_link():
    G = _graph()
    # O–TB 선분을 가로지르는 다른 보도 링크
    G.add_node("TX", **_ll(-5, 5), node_type="sidewalk")
    G.add_node("TY", **_ll(5, 5), node_type="sidewalk")
    G.add_edge("TX", "TY", length=10, slope=0.0, link_type="sidewalk")
    assert not [it for it in rs.find_restitch(G) if it["t"] == "TB"]


def test_idempotent_on_restitched_graph():
    G = _graph()
    rs.apply_restitch(G, rs.find_restitch(G))
    assert rs.find_restitch(G) == []


def test_segments_cross_ignores_endpoint_touch():
    assert rs.segments_cross((0, 0), (2, 2), (0, 2), (2, 0))
    assert not rs.segments_cross((0, 0), (2, 2), (2, 2), (3, 0))


def test_reject_when_road_node_would_bypass_crosswalk():
    """o 를 거치는 두 모퉁이 사이가 횡단보도 길보다 짧아지면 '표시 없는 횡단' 지름길이라 넣지 않는다."""
    G = nx.Graph()
    G.add_node(1, **_ll(0, 0))
    G.add_node(2, **_ll(300, 0))
    G.add_node("TW", **_ll(-6, 0))
    G.add_node("TE", **_ll(6.5, 0))
    G.add_node("TW2", **_ll(-6, 20))
    G.add_node("TE2", **_ll(6.5, 20))
    G.add_edge(1, 2, length=300, slope=0.0, link_type="road")
    G.add_edge(1, "TW", length=6, slope=0.0, link_type="sidewalk", stitched=True)
    G.add_edge("TW", "TW2", length=20, slope=0.0, link_type="sidewalk")
    G.add_edge("TW2", "TE2", length=12.5, slope=0.0, link_type="crossing")
    G.add_edge("TE2", "TE", length=20, slope=0.0, link_type="sidewalk")
    assert not [it for it in rs.find_restitch(G) if it["t"] == "TE"]


def _dead_end_graph():
    """도로 O(0,0)→R(300,0). 보도 TA(-10,8)–TB(40,8) 이 TB 에서 끊겨 있고, 도로와는 O–TA 스티칭만 있다."""
    G = nx.Graph()
    G.add_node(1, **_ll(0, 0))
    G.add_node(2, **_ll(300, 0))
    G.add_node("TA", **_ll(-10, 8))
    G.add_node("TB", **_ll(40, 8))
    G.add_edge(1, 2, length=300, slope=1.0, link_type="road", link_name="예술공원로")
    G.add_edge(1, "TA", length=12.8, slope=1.0, link_type="sidewalk", stitched=True)
    G.add_edge("TA", "TB", length=50, slope=0.5, link_type="sidewalk")
    return G


def test_dead_end_joins_road_by_splitting():
    G = _dead_end_graph()
    before = nx.shortest_path(G, "TB", 2, weight="length")
    assert before[:3] == ["TB", "TA", 1]                 # 되돌아가 교차로를 거친다
    rep = rs.connect_dead_ends(G)
    assert len(rep) == 1 and rep[0]["t"] == "TB" and rep[0]["split"] and abs(rep[0]["length"] - 8) < 0.5
    s = rep[0]["target"]
    assert not G.has_edge(1, 2) and G.has_edge(1, s) and G.has_edge(s, 2)
    assert abs(G.edges[1, s]["length"] - 40) < 1 and abs(G.edges[s, 2]["length"] - 260) < 1
    assert G.edges[1, s]["link_name"] == "예술공원로"
    after = nx.shortest_path(G, "TB", 2, weight="length")
    assert after == ["TB", s, 2]
    assert G.edges["TB", s]["derived_kind"] == "deadend_join"


def test_dead_end_too_far_or_blocked():
    G = _dead_end_graph()
    G.nodes["TB"].update(_ll(40, 20))                    # 도로에서 20m
    assert rs.connect_dead_ends(G) == []
    G = _dead_end_graph()
    G.add_node("TX", **_ll(30, 4)); G.add_node("TY", **_ll(50, 4))
    G.add_edge("TX", "TY", length=20, slope=0.0, link_type="sidewalk")   # 사이를 가로막는 다른 보도
    assert [r for r in rs.connect_dead_ends(G) if r["t"] == "TB"] == []


def test_dead_end_near_node_joins_node_without_split():
    G = _dead_end_graph()
    G.nodes["TB"].update(_ll(299, 6))
    rep = rs.connect_dead_ends(G)
    assert rep and rep[0]["target"] == 2 and not rep[0]["split"] and G.has_edge(1, 2)


def test_dead_end_join_rejected_when_it_bypasses_crosswalk():
    """길 건너 보도 끝을 도로 중심선에 이으면 횡단보도 없이 건너는 지름길이 된다 — 되돌린다."""
    G = nx.Graph()
    G.add_node(1, **_ll(0, 0)); G.add_node(2, **_ll(300, 0))
    G.add_edge(1, 2, length=300, slope=0.0, link_type="road")
    # 북쪽 보도 TN1–TN2 (도로와 스티칭 TN1–1), 남쪽 보도 TS1–TS2(TS2 끝이 끊김), 횡단보도 TN1–TS1
    for n, xy in {"TN1": (0, 8), "TN2": (60, 8), "TS1": (0, -8), "TS2": (60, -8)}.items():
        G.add_node(n, **_ll(*xy))
    G.add_edge(1, "TN1", length=8, slope=0.0, link_type="sidewalk", stitched=True)
    G.add_edge("TN1", "TN2", length=60, slope=0.0, link_type="sidewalk")
    G.add_edge("TN1", "TS1", length=16, slope=0.0, link_type="crossing")
    G.add_edge("TS1", "TS2", length=60, slope=0.0, link_type="sidewalk")
    G.add_node("TN3", **_ll(90, 8)); G.add_edge("TN2", "TN3", length=30, slope=0.0, link_type="sidewalk")
    G0 = G.copy()
    rep = rs.connect_dead_ends(G)
    # 보도 끝 두 곳(남 TS2·북 TN3)을 모두 도로 중심선에 이으면 TN3 → TS2 가 횡단보도 없이 46m 로 열린다(원래 166m·횡단 1).
    assert {r["t"] for r in rep} != {"TS2", "TN3"}, rep
    d0 = nx.shortest_path_length(G0, "TN3", "TS2", weight="length")
    path = nx.shortest_path(G, "TN3", "TS2", weight="length")
    d1 = nx.path_weight(G, path, "length")
    assert not (d1 + 1 < d0 and not any(G.edges[a, b]["link_type"] == "crossing" for a, b in zip(path, path[1:])))


def test_reject_multi_hop_road_shortcut_around_crosswalk():
    """도로 노드 두 개를 거쳐 길 건너 보도로 가는 지름길(횡단보도 우회)도 막는다(G5)."""
    G = nx.Graph()
    for n, xy in {1: (0, 0), 2: (20, 0)}.items():
        G.add_node(n, **_ll(*xy))
    G.add_edge(1, 2, length=20, slope=0.0, link_type="road")
    for n, xy in {"TA": (0, 8), "TN2": (20, 8), "TS": (20, -9), "TSW": (-30, -9), "TNW": (-30, 8)}.items():
        G.add_node(n, **_ll(*xy))
    G.add_edge(1, "TA", length=8, slope=0.0, link_type="sidewalk", stitched=True)
    G.add_edge(2, "TN2", length=8, slope=0.0, link_type="sidewalk", stitched=True)
    G.add_edge("TS", "TSW", length=50, slope=0.0, link_type="sidewalk")
    G.add_edge("TSW", "TNW", length=17, slope=0.0, link_type="crossing")
    G.add_edge("TNW", "TA", length=30, slope=0.0, link_type="sidewalk")
    assert not [it for it in rs.find_restitch(G) if it["t"] == "TS"]


def test_dead_end_finds_long_link_with_far_endpoints():
    """양 끝이 멀리 있는 긴 도로 링크(끝점이 격자 이웃 칸 밖)도 옆을 지나면 찾는다."""
    G = _dead_end_graph()
    G.nodes["TB"].update(_ll(150, 6))
    G.edges["TA", "TB"]["length"] = 160
    rep = rs.connect_dead_ends(G)
    assert rep and rep[0]["t"] == "TB" and rep[0]["split"] and abs(rep[0]["length"] - 6) < 0.5


def test_dead_end_near_far_endpoint_not_snapped_beyond_limit():
    """링크 끝 쪽 비율이어도 실제 거리가 10m 를 넘는 끝점에는 잇지 않는다."""
    G = _dead_end_graph()
    G.nodes["TB"].update(_ll(-12, 5))          # 도로 시작점(0,0) 바깥 12m 쪽 — 투영은 끝점, 거리 13m
    G.edges["TA", "TB"]["length"] = 4
    assert [r for r in rs.connect_dead_ends(G) if r["t"] == "TB"] == []


def test_dead_end_join_blocked_when_it_newly_reaches_other_side():
    """보강 전에는 닿지 못하던 길 건너 보도에 횡단보도 없이 새로 닿게 하는 연결은 막는다."""
    G = nx.Graph()
    G.add_node(1, **_ll(0, 0)); G.add_node(2, **_ll(300, 0))
    G.add_edge(1, 2, length=300, slope=0.0, link_type="road")
    for n, xy in {"TN1": (0, 8), "TN2": (60, 8), "TS1": (120, -8), "TS2": (60, -8)}.items():
        G.add_node(n, **_ll(*xy))
    G.add_edge(1, "TN1", length=8, slope=0.0, link_type="sidewalk", stitched=True)
    G.add_edge("TN1", "TN2", length=60, slope=0.0, link_type="sidewalk")
    G.add_edge("TS1", "TS2", length=60, slope=0.0, link_type="sidewalk")   # 남쪽 보도는 따로 떨어져 있다
    G.add_node("TS0", **_ll(180, -30)); G.add_edge("TS1", "TS0", length=64, slope=0.0, link_type="sidewalk")
    rep = rs.connect_dead_ends(G)
    assert not [r for r in rep if r["t"] == "TS2"], rep
