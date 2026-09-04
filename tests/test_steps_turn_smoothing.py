# -*- coding: utf-8 -*-
"""짧은 링크 회전 승격 억제 · 방위각 평활 (v1.21.0).

실증(2026-09-03) 안양문화원 도착 직전 구간을 실제 노드 좌표로 재현한다.
종전에는 6.1m 짜리 모퉁이 절점 하나 때문에 "우회전 후 6m" + "급좌회전 후 16m"
두 지시가 연달아 나왔다.
"""
from __future__ import annotations

import networkx as nx

from route_service.engine.geo import bearing_deg, lead_bearing, trail_bearing
from route_service.engine.graph import NetworkStore
from route_service.engine.profiles import get_profile
from route_service.engine.steps import build_steps

# 실측 좌표 (anyang-hybrid-2026Q3)
A = ("307943424", 37.3915878, 126.9274465)    # 현충로·안양로 교차 crossing 노드
B = ("TN0003186", 37.39159249, 126.92722667)
C = ("TN0003187", 37.39163781, 126.92718809)  # 6.1m 위로 튀어나온 모퉁이 절점
D = ("TN0008057", 37.3915104, 126.92716515)
E = ("TN0008056", 37.39130002, 126.926757)

_EDGES = [(A, B, 19.5), (B, C, 6.1), (C, D, 15.7), (D, E, 43.1)]


def _field_store() -> NetworkStore:
    G = nx.Graph()
    for nid, lat, lon in (A, B, C, D, E):
        G.add_node(nid, lat=lat, lon=lon, node_type="sidewalk", crosswalk_cnt=0)
    for (u, _, _), (v, _, _), ln in _EDGES:
        G.add_edge(u, v, length=ln, slope=0.5, link_type="sidewalk", width=2.0,
                   curb_cut=True, surface="asphalt", link_name=None, geometry=None)
    s = NetworkStore()
    s.load_graph_object(G, version="test-field", region="테스트")
    return s


def _maneuvers(store):
    p = get_profile("wheelchair_manual")
    path = [A[0], B[0], C[0], D[0], E[0]]
    return build_steps(store.graph, path, p)


def test_short_corner_node_does_not_produce_sharp_turn():
    steps = _maneuvers(_field_store())
    maneuvers = [s["maneuver"] for s in steps]
    assert "sharp_left" not in maneuvers, maneuvers
    assert "uturn" not in maneuvers, maneuvers


def test_short_link_turn_is_absorbed_not_announced():
    """6.1m 링크가 자기 지시를 갖지 못하고 앞 스텝에 흡수된다."""
    steps = _maneuvers(_field_store())
    turns = [s for s in steps if s["maneuver"] not in ("depart", "arrive", "straight")]
    assert all(s["distance_m"] >= 12 for s in turns), [
        (s["maneuver"], s["distance_m"]) for s in turns
    ]


def test_carried_angle_nets_out_the_zigzag():
    """이월된 +54도와 되꺾이는 -138도가 합산돼 급회전이 완만한 좌회전으로 내려온다."""
    steps = _maneuvers(_field_store())
    lefts = [s for s in steps if s["maneuver"] in ("left", "slight_left")]
    assert lefts, [s["maneuver"] for s in steps]


def test_step_count_is_reduced():
    steps = _maneuvers(_field_store())
    # depart + 좌회전 + 우회전 + arrive = 4. 종전에는 6개(우회전 6m·급좌회전 16m 포함)였다.
    assert len(steps) <= 4, [(s["maneuver"], s["distance_m"]) for s in steps]


def test_lead_bearing_ignores_micro_vertex_at_link_head():
    """링크 머리에 1m 짜리 절점이 붙어도 진입 방위각이 튀지 않는다."""
    # 정북으로 30m 가는 링크인데, 첫 1m 만 동쪽으로 삐져 있다.
    coords = [(37.3900000, 126.9500000), (37.3900000, 126.9500113),
              (37.3902700, 126.9500113)]
    naive = bearing_deg(coords[0][0], coords[0][1], coords[1][0], coords[1][1])
    smooth = lead_bearing(coords, 10.0)
    assert 80 < naive < 100          # 첫 조각만 보면 정동(90도)
    assert smooth < 15 or smooth > 345  # 10m 구간으로 보면 사실상 정북


def test_trail_bearing_ignores_micro_vertex_at_link_tail():
    coords = [(37.3900000, 126.9500000), (37.3902700, 126.9500000),
              (37.3902700, 126.9500113)]
    naive = bearing_deg(coords[-2][0], coords[-2][1], coords[-1][0], coords[-1][1])
    smooth = trail_bearing(coords, 10.0)
    assert 80 < naive < 100
    assert smooth < 15 or smooth > 345


def test_bearing_helpers_fall_back_on_short_links():
    """span 보다 짧은 링크는 링크 전체로 잰다 (양 끝점 방위각과 같아야 한다)."""
    coords = [(37.3900000, 126.9500000), (37.3900450, 126.9500000)]  # 약 5m
    whole = bearing_deg(coords[0][0], coords[0][1], coords[-1][0], coords[-1][1])
    assert abs(lead_bearing(coords, 10.0) - whole) < 1e-6
    assert abs(trail_bearing(coords, 10.0) - whole) < 1e-6
