# -*- coding: utf-8 -*-
"""직진으로 지나는 교차로의 횡단보도 안내 (v1.34.0).

도로 중심선 구간에서는 옆길 횡단이 링크로 잡히지 않는다. 노드에 붙은 횡단보도 가운데
가는 길을 가로막는 큰 것(진행 방향의 좌우에 놓이고 건너는 거리 9m 이상)은
직진 통과라도 알려야 하고, 따라가던 길을 건너는 횡단보도는 종전대로 알리지 않아야 한다.
"""
from __future__ import annotations

import networkx as nx

from route_service.engine.graph import NetworkStore
from route_service.engine.planner import plan
from route_service.engine.profiles import get_profile
from route_service.engine.steps import build_steps
from scripts.attach_crosswalk_points import attach

LAT = 37.3900
M_LAT = 1.0 / 110540.0            # 1m 의 위도
M_LON = 1.0 / (111320.0 * 0.7946)  # 1m 의 경도(위도 37.39)


def _graph(side_road: bool = True) -> nx.Graph:
    """A --100m-- X --100m-- B (동쪽으로 일직선). X 에서 남북으로 옆길이 갈린다."""
    G = nx.Graph()
    G.add_node("A", lat=LAT, lon=126.9500 - 100 * M_LON)
    G.add_node("X", lat=LAT, lon=126.9500)
    G.add_node("B", lat=LAT, lon=126.9500 + 100 * M_LON)
    for u, v in (("A", "X"), ("X", "B")):
        G.add_edge(u, v, length=100.0, slope=0.5, link_type="road", width=None,
                   curb_cut=None, surface=None, link_name="큰길", geometry=None)
    if side_road:
        for name, sign in (("N", 1), ("S", -1)):
            G.add_node(name, lat=LAT + sign * 80 * M_LAT, lon=126.9500)
            G.add_edge("X", name, length=80.0, slope=0.5, link_type="road", width=None,
                       curb_cut=None, surface=None, link_name="옆길", geometry=None)
    return G


def _pt(pid, east_m, north_m, length):
    return {"id": pid, "lat": LAT + north_m * M_LAT, "lon": 126.9500 + east_m * M_LON, "length_m": length}


def _steps(G, profile_id="wheelchair_electric"):
    s = NetworkStore()
    s.load_graph_object(G, version="test-ahead", region="테스트")
    p = get_profile(profile_id)
    r = plan(s, "A", "B", p)["routes"][0]
    return build_steps(s.graph, r["path"], p)


def _mark(G, pts):
    G.nodes["X"].update(crosswalk_cnt=len(pts), cw_mgmt_nos=[p["id"] for p in pts], cw_points=pts)


def test_side_road_crosswalks_on_both_sides_are_announced():
    G = _graph()
    _mark(G, [_pt("c-n", 1, 17, 18.0), _pt("c-s", -1, -17, 16.0),      # 옆길을 건너는 것(좌·우)
              _pt("c-w", -12, 0, 25.0), _pt("c-e", 12, 0, 25.0)])     # 큰길을 건너는 것(앞·뒤)
    cw = [s for s in _steps(G) if s["maneuver"] == "crossing_point"]
    assert len(cw) == 1
    assert cw[0]["crossing_ahead"] is True
    assert cw[0]["crossing_side"] == "both"
    assert sorted(cw[0]["crosswalk_ids"]) == ["c-n", "c-s"]
    assert cw[0]["crossing_length_m"] == 18
    assert cw[0]["instruction"] == "교차로입니다. 옆길 횡단보도를 건넙니다."
    assert cw[0]["warnings"] == []                    # 턱낮춤 미상은 이 스텝에 붙이지 않는다


def test_announcement_splits_the_long_straight_step():
    G = _graph()
    _mark(G, [_pt("c-n", 0, 17, 18.0)])
    steps = _steps(G)
    assert [s["maneuver"] for s in steps] == ["depart", "crossing_point", "straight", "arrive"]
    assert steps[0]["distance_m"] == 100 and steps[2]["distance_m"] == 100


def test_one_side_names_the_side():
    G = _graph()
    _mark(G, [_pt("c-n", 0, 17, 18.0)])        # 동쪽으로 갈 때 북쪽은 왼쪽
    cw = [s for s in _steps(G) if s["maneuver"] == "crossing_point"][0]
    assert cw["crossing_side"] == "left"
    assert cw["instruction"] == "교차로입니다. 왼쪽 보도로 가는 중이면 옆길 횡단보도를 건넙니다."
    G2 = _graph()
    _mark(G2, [_pt("c-s", 0, -17, 18.0)])
    cw2 = [s for s in _steps(G2) if s["maneuver"] == "crossing_point"][0]
    assert cw2["crossing_side"] == "right" and "오른쪽" in cw2["instruction"]


def test_crosswalk_across_the_followed_road_stays_silent():
    """따라가던 길을 건너는 횡단보도(진행 축 위)는 직진 통과 때 알리지 않는다 — v1.21.0 유지."""
    G = _graph()
    _mark(G, [_pt("c-w", -12, 0.5, 25.0), _pt("c-e", 12, -0.5, 25.0)])
    assert not [s for s in _steps(G) if s["maneuver"] == "crossing_point"]


def test_short_crosswalk_stays_silent():
    G = _graph()
    _mark(G, [_pt("c-n", 0, 12, 4.7), _pt("c-s", 0, -12, 8.9)])
    assert not [s for s in _steps(G) if s["maneuver"] == "crossing_point"]


def test_no_side_road_or_far_point_stays_silent():
    G = _graph(side_road=False)                 # 갈림 없는 지점
    _mark(G, [_pt("c-n", 0, 17, 18.0)])
    assert not [s for s in _steps(G) if s["maneuver"] == "crossing_point"]
    G2 = _graph()
    _mark(G2, [_pt("c-far", 0, 33, 18.0), _pt("c-diag", 17, 18, 18.0)])   # 다른 길 · 진행 방향으로 너무 멀다
    assert not [s for s in _steps(G2) if s["maneuver"] == "crossing_point"]


def test_graph_without_points_keeps_old_behaviour():
    G = _graph()
    G.nodes["X"].update(crosswalk_cnt=2, cw_mgmt_nos=["a", "b"])
    assert not [s for s in _steps(G) if s["maneuver"] == "crossing_point"]
    # 시각 프로필은 종전대로 정보형 안내
    cw = [s for s in _steps(G, "visual") if s["maneuver"] == "crossing_point"]
    assert len(cw) == 1 and "crossing_ahead" not in cw[0]
    assert "있는 지점입니다" in cw[0]["instruction"]


def test_visual_profile_gets_the_ahead_sentence_too():
    G = _graph()
    _mark(G, [_pt("c-n", 0, 17, 18.0), _pt("c-s", 0, -17, 18.0)])
    cw = [s for s in _steps(G, "visual") if s["maneuver"] == "crossing_point"]
    assert len(cw) == 1 and cw[0]["crossing_ahead"] is True


def test_bad_point_records_are_skipped():
    G = _graph()
    _mark(G, [{"id": "x", "lat": None, "lon": 126.95, "length_m": 20.0},
              {"id": "y", "lat": LAT, "lon": 126.95},
              _pt("c-n", 0, 17, None)])
    assert not [s for s in _steps(G) if s["maneuver"] == "crossing_point"]


def test_attach_fills_points_without_touching_topology():
    G = _graph()
    G.nodes["X"].update(crosswalk_cnt=2, cw_mgmt_nos=["k1", "k2", "gone"])
    feats = [
        {"geometry": {"type": "Point", "coordinates": [126.9500, LAT + 17 * M_LAT]},
         "properties": {"mgmt_no": "k1", "cw_length_m": 18.2}},
        {"geometry": {"type": "Point", "coordinates": [126.9500, LAT - 17 * M_LAT]},
         "properties": {"mgmt_no": "k2", "cw_length_m": None}},
    ]
    n, e = G.number_of_nodes(), G.number_of_edges()
    stat = attach(G, feats)
    assert (n, e) == (G.number_of_nodes(), G.number_of_edges())
    assert stat == {"nodes": 1, "points": 2, "missing": 1, "no_length": 1}
    pts = G.nodes["X"]["cw_points"]
    assert pts[0]["id"] == "k1" and pts[0]["length_m"] == 18.2 and pts[1]["length_m"] is None
    cw = [s for s in _steps(G) if s["maneuver"] == "crossing_point"]
    assert len(cw) == 1 and cw[0]["crossing_side"] == "left"


def test_parallel_road_crosswalk_without_branch_on_that_side_stays_silent():
    """그쪽으로 갈리는 길이 없으면 — 나란한 다른 길의 횡단보도가 붙은 것 — 알리지 않는다."""
    G = _graph(side_road=False)
    G.add_node("N", lat=LAT + 80 * M_LAT, lon=126.9500)
    G.add_edge("X", "N", length=80.0, slope=0.5, link_type="road", width=None,
               curb_cut=None, surface=None, link_name="옆길", geometry=None)      # 북쪽(왼쪽)으로만 갈린다
    _mark(G, [_pt("c-s", 0, -17, 18.0)])                                         # 횡단보도는 남쪽(오른쪽)
    assert not [s for s in _steps(G) if s["maneuver"] == "crossing_point"]
    _mark(G, [_pt("c-n", 0, 17, 18.0)])
    cw = [s for s in _steps(G) if s["maneuver"] == "crossing_point"]
    assert len(cw) == 1 and cw[0]["crossing_side"] == "left"


def test_confirmed_missing_curb_cut_is_still_warned():
    G = _graph()
    _mark(G, [_pt("c-n", 0, 17, 18.0), _pt("c-s", 0, -17, 18.0)])
    G.nodes["X"]["cw_curb_cut"] = False
    cw = [s for s in _steps(G) if s["maneuver"] == "crossing_point"][0]
    assert cw["warnings"] == ["턱낮춤 없음"] and cw["instruction"].endswith("(턱낮춤 없음)")


def test_short_kinked_node_inside_the_junction_still_counts_as_straight():
    """교차로 안 2m 절점에서 방위각이 튀어도, 앞뒤 25m 로 보면 직진이므로 알린다."""
    G = _graph()
    G.remove_edge("X", "B")
    G.add_node("K", lat=LAT + 1.2 * M_LAT, lon=126.9500 + 2 * M_LON)             # X 에서 2m, 31도 비껴 있다
    for u, v, ln in (("X", "K", 2.3), ("K", "B", 98.0)):
        G.add_edge(u, v, length=ln, slope=0.5, link_type="road", width=None,
                   curb_cut=None, surface=None, link_name="큰길", geometry=None)
    _mark(G, [_pt("c-n", 0, 17, 18.0), _pt("c-s", 0, -17, 18.0)])
    cw = [s for s in _steps(G) if s["maneuver"] == "crossing_point"]
    assert len(cw) == 1 and cw[0].get("crossing_ahead") is True and cw[0]["crossing_side"] == "both"


def test_real_turn_at_the_node_does_not_use_the_ahead_sentence():
    G = _graph()
    _mark(G, [_pt("c-n", 0, 17, 18.0), _pt("c-e", 12, 0, 25.0)])
    s = NetworkStore()
    s.load_graph_object(G, version="test-ahead", region="테스트")
    p = get_profile("wheelchair_electric")
    r = plan(s, "A", "N", p)["routes"][0]                                         # X 에서 왼쪽으로 꺾는다
    cw = [x for x in build_steps(s.graph, r["path"], p) if x["maneuver"] == "crossing_point"]
    assert len(cw) == 1 and "crossing_ahead" not in cw[0]
    assert "있는 지점입니다" in cw[0]["instruction"]


def test_diagonal_heading_and_reverse_direction():
    """북동쪽으로 비스듬히 가는 길 — 좌우 판정이 방위각과 무관하게 맞아야 한다."""
    import math
    G = nx.Graph()
    c, s_ = math.cos(math.radians(45)), math.sin(math.radians(45))

    def at(along, left):                    # 진행 방향(북동) 기준 좌표 -> 위경도
        e = along * s_ - left * c
        n = along * c + left * s_
        return LAT + n * M_LAT, 126.9500 + e * M_LON

    for name, (al, lf) in {"A": (-100, 0), "X": (0, 0), "B": (100, 0), "L": (0, 80), "R": (0, -80)}.items():
        la, lo = at(al, lf)
        G.add_node(name, lat=la, lon=lo)
    for u, v, ln in (("A", "X", 100.0), ("X", "B", 100.0), ("X", "L", 80.0), ("X", "R", 80.0)):
        G.add_edge(u, v, length=ln, slope=0.5, link_type="road", width=None,
                   curb_cut=None, surface=None, link_name="길", geometry=None)
    la, lo = at(1, 16)
    la2, lo2 = at(-13, 0)
    pts = [{"id": "left", "lat": la, "lon": lo, "length_m": 14.0},
           {"id": "behind", "lat": la2, "lon": lo2, "length_m": 20.0}]
    G.nodes["X"].update(crosswalk_cnt=2, cw_mgmt_nos=["left", "behind"], cw_points=pts)
    s = NetworkStore()
    s.load_graph_object(G, version="test-ahead", region="테스트")
    p = get_profile("wheelchair_electric")
    fwd = [x for x in build_steps(s.graph, plan(s, "A", "B", p)["routes"][0]["path"], p) if x.get("crossing_ahead")]
    assert len(fwd) == 1 and fwd[0]["crossing_side"] == "left" and fwd[0]["crosswalk_ids"] == ["left"]
    back = [x for x in build_steps(s.graph, plan(s, "B", "A", p)["routes"][0]["path"], p) if x.get("crossing_ahead")]
    assert len(back) == 1 and back[0]["crossing_side"] == "right"                 # 거꾸로 가면 오른쪽


def test_threshold_edges():
    for east, north, length, expect in (
        (0, 17, 9.0, True), (0, 17, 8.9, False),          # 건너는 거리
        (0, 5.0, 12.0, True), (0, 4.5, 12.0, False),      # 좌우로 벗어난 정도(하한)
        (0, 29.5, 12.0, True), (0, 31, 12.0, False),      # 상한
        (14, 16, 12.0, True), (16, 17, 12.0, False),      # 앞뒤 허용 폭
        (10, 9, 12.0, False),                             # 앞뒤로 더 치우친 것
    ):
        G = _graph()
        _mark(G, [_pt("c", east, north, length)])
        got = bool([s for s in _steps(G) if s.get("crossing_ahead")])
        assert got is expect, (east, north, length)


def test_kinked_node_without_side_crosswalk_stays_silent():
    """앞뒤 25m 로 보아 직진이면, 바로 붙은 링크의 방위각이 튀어도 정보형 안내를 내지 않는다(직진 통과 무음 유지)."""
    G = _graph()
    G.remove_edge("X", "B")
    G.add_node("K", lat=LAT + 1.2 * M_LAT, lon=126.9500 + 2 * M_LON)
    for u, v, ln in (("X", "K", 2.3), ("K", "B", 98.0)):
        G.add_edge(u, v, length=ln, slope=0.5, link_type="road", width=None,
                   curb_cut=None, surface=None, link_name="큰길", geometry=None)
    _mark(G, [_pt("c-e", 12, 0, 25.0)])            # 따라가던 길을 건너는 것뿐
    assert not [s for s in _steps(G) if s["maneuver"] == "crossing_point"]


def test_special_or_zero_length_links_are_not_side_roads():
    G = _graph(side_road=False)
    G.add_node("N", lat=LAT + 12 * M_LAT, lon=126.9500)
    G.add_edge("X", "N", length=12.0, slope=0.0, link_type="crossing", width=None,
               curb_cut=None, surface=None, link_name=None, geometry=None)       # 횡단보도 링크는 옆길이 아니다
    G.add_node("Z", lat=LAT, lon=126.9500)
    G.add_edge("X", "Z", length=0.0, slope=0.0, link_type="road", width=None,
               curb_cut=None, surface=None, link_name=None, geometry=None)       # 길이 없는 링크
    _mark(G, [_pt("c-n", 0, 17, 18.0)])
    assert not [s for s in _steps(G) if s.get("crossing_ahead")]


def test_path_neighbours_are_not_counted_as_side_roads():
    from route_service.engine.steps import _side_roads
    G = _graph(side_road=False)
    assert _side_roads(G, "X", 90.0) == set()
    assert _side_roads(G, "X", 40.0) == {"left", "right"}          # 건너뛰지 않으면 걸어온 길·갈 길이 옆길로 잡힌다
    assert _side_roads(G, "X", 40.0, skip=("A", "B")) == set()
