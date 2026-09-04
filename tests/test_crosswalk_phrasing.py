# -*- coding: utf-8 -*-
"""노드 부착 횡단보도 안내 문구 (v1.21.0).

실측 2026-09-04: 안양아트센터→안양문화원 1.4km 경로에서 "횡단보도를 건너세요"가
10번 나왔고, 그중 6번은 **안양로를 직진하는 도중**이었다. 이용자는 지시대로 길을
건너 경로를 이탈한다. 노드 부착 횡단보도는 경로가 그것을 건넌다는 뜻이 아니다.
"""
from __future__ import annotations

import networkx as nx

from route_service.engine.graph import NetworkStore
from route_service.engine.profiles import get_profile
from route_service.engine.steps import build_steps

LAT = 37.3900
LON = 126.9500
D50 = 50.0 / 88_400.0     # 경도로 약 50m


def _chain(turn: bool) -> NetworkStore:
    """A -(50m)- B(횡단보도 부착) -(50m)- C. turn=True 면 B 에서 북쪽으로 꺾는다."""
    G = nx.Graph()
    G.add_node("A", lat=LAT, lon=LON, node_type="sidewalk", crosswalk_cnt=0)
    G.add_node("B", lat=LAT, lon=LON + D50, node_type="crossing", crosswalk_cnt=2,
               cw_curb_cut=None, cw_tactile_paving=None)
    if turn:
        G.add_node("C", lat=LAT + 50.0 / 110_540.0, lon=LON + D50,
                   node_type="sidewalk", crosswalk_cnt=0)
    else:
        G.add_node("C", lat=LAT, lon=LON + 2 * D50, node_type="sidewalk", crosswalk_cnt=0)
    for u, v in (("A", "B"), ("B", "C")):
        G.add_edge(u, v, length=50.0, slope=0.5, link_type="sidewalk", width=2.0,
                   curb_cut=True, surface="asphalt", link_name="예시로", geometry=None)
    s = NetworkStore()
    s.load_graph_object(G, version="test-cw", region="테스트")
    return s


def _steps(turn: bool, profile_id: str = "wheelchair_manual"):
    return build_steps(_chain(turn).graph, ["A", "B", "C"], get_profile(profile_id))


def _crossing_points(steps):
    return [s for s in steps if s["maneuver"] == "crossing_point"]


def test_straight_through_crosswalk_node_is_silent():
    """직진 통과하는 지점의 횡단보도는 안내하지 않는다 — 안양로 6회 오안내의 원인."""
    assert _crossing_points(_steps(turn=False)) == []


def test_turning_at_crosswalk_node_is_announced():
    cw = _crossing_points(_steps(turn=True))
    assert len(cw) == 1


def test_node_attached_crosswalk_never_gives_an_order():
    """'건너세요' 는 실제 횡단 링크에서만. 노드 부착분은 존재만 알린다."""
    for turn in (True, False):
        for pid in ("wheelchair_manual", "visual"):
            for s in _crossing_points(_steps(turn, pid)):
                assert "건너세요" not in s["instruction"], (turn, pid, s["instruction"])


def test_visual_profile_keeps_information_even_when_straight():
    """시각장애 프로필에는 차도 접근 신호로서 값이 있어 정보형으로 유지한다."""
    cw = _crossing_points(_steps(turn=False, profile_id="visual"))
    assert len(cw) == 1
    assert "횡단보도" in cw[0]["instruction"]


def test_real_crossing_link_still_gives_the_order():
    """link_type='crossing' 인 실제 횡단 링크는 종전대로 지시형."""
    import networkx as nx2
    G = nx2.Graph()
    G.add_node("A", lat=LAT, lon=LON, node_type="sidewalk", crosswalk_cnt=0)
    G.add_node("B", lat=LAT, lon=LON + D50, node_type="crossing", crosswalk_cnt=0)
    G.add_edge("A", "B", length=12.0, slope=0.0, link_type="crossing", width=3.0,
               curb_cut=True, surface="asphalt", link_name=None, geometry=None)
    s = NetworkStore()
    s.load_graph_object(G, version="test-cwlink", region="테스트")
    texts = " ".join(x["instruction"] for x in build_steps(s.graph, ["A", "B"],
                                                          get_profile("wheelchair_manual")))
    assert "횡단보도를 건너" in texts
