# -*- coding: utf-8 -*-
"""경로 위 횡단보도 위치 목록 (v1.37.0, #118)."""
import networkx as nx

from route_service.engine import crosswalks as cw


def _g():
    G = nx.Graph()
    for n, lon in enumerate([126.9100, 126.9102, 126.9104, 126.9106, 126.9108]):
        G.add_node(n, lat=37.4180, lon=lon)
    G.add_edge(0, 1, length=17.7, slope=0, link_type="sidewalk")
    G.add_edge(1, 2, length=17.7, slope=0, link_type="crossing", cw_mgmt_no="A1",
               geometry=[(37.4180, 126.9102), (37.4181, 126.9103), (37.4180, 126.9104)])
    G.add_edge(2, 3, length=17.7, slope=0, link_type="crossing", unmarked=True)
    G.add_edge(3, 4, length=17.7, slope=0, link_type="crossing")
    G.nodes[1]["cw_points"] = [{"id": "A1", "lat": 37.41795, "lon": 126.9102, "length_m": 9.0},
                               {"id": "B2", "lat": 37.41805, "lon": 126.9101, "length_m": 12.0}]
    G.nodes[3]["cw_points"] = [{"id": "C3", "lat": 37.4181, "lon": 126.9106, "length_m": 8.0}]
    return G


def test_roles_and_order():
    G = _g()
    steps = [{"maneuver": "crossing_point", "crossing_ahead": True, "crosswalk_ids": ["C3"]}]
    out = cw.for_path(G, [0, 1, 2, 3, 4], steps)
    ids = [(x["id"], x["role"]) for x in out]
    # A1 은 노드 부착이지만 경로가 그 횡단 링크를 건너므로 cross, B2 는 near, C3 는 옆길(side),
    # 표시 없는 이면도로 횡단은 제외, 관리번호 없는 횡단 링크는 id None 으로 cross
    assert ids == [("A1", "cross"), ("B2", "near"), ("C3", "side"), (None, "cross")]


def test_cross_point_is_link_midpoint():
    G = _g()
    out = cw.for_path(G, [1, 2])
    a1 = out[0]
    assert a1["role"] == "cross" and abs(a1["lat"] - 37.4181) < 2e-5 and abs(a1["lng"] - 126.9103) < 2e-5
    assert a1["length_m"] == 17.7


def test_no_crosswalks():
    G = _g()
    assert cw.for_path(G, [0]) == []
    # 진행 순서: 4→3 횡단 링크를 먼저 건너고 노드 3 의 부착분
    assert cw.for_path(G, [4, 3]) == [{"id": None, "lat": 37.418, "lng": 126.9107, "length_m": 17.7, "role": "cross"},
                                     {"id": "C3", "lat": 37.4181, "lng": 126.9106, "length_m": 8.0, "role": "near"}]
