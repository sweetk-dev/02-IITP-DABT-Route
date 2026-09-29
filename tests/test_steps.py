# -*- coding: utf-8 -*-
from route_service.engine.planner import plan
from route_service.engine.profiles import get_profile
from route_service.engine.steps import build_steps


def test_steps_start_with_depart_and_end_with_arrive(store):
    p = get_profile("wheelchair_manual")
    path = plan(store, "N1", "N3", p)["routes"][0]["path"]
    steps = build_steps(store.graph, path, p)
    assert steps[0]["maneuver"] == "depart"
    assert steps[-1]["maneuver"] == "arrive"
    assert steps[-1]["instruction"] == "목적지에 도착했습니다."


def test_steps_have_korean_instructions_and_distance(store):
    p = get_profile("wheelchair_manual")
    path = plan(store, "N1", "N3", p)["routes"][0]["path"]
    steps = build_steps(store.graph, path, p)
    assert all(s["instruction"] for s in steps)
    assert sum(s["distance_m"] for s in steps) > 0
    assert all("idx" in s and "coord" in s for s in steps)


def test_turn_is_detected(store):
    # N1->N2 는 동쪽, N2->N3 는 북쪽 -> 좌회전 계열이 나와야 한다
    p = get_profile("wheelchair_manual")
    path = plan(store, "N1", "N3", p)["routes"][0]["path"]
    maneuvers = [s["maneuver"] for s in build_steps(store.graph, path, p)]
    assert any(m in ("left", "slight_left", "sharp_left") for m in maneuvers)


def test_warning_for_steep_segment(store):
    p = get_profile("walk")
    steps = build_steps(store.graph, ["N1", "N4", "N3"], p)
    texts = " ".join(s["instruction"] for s in steps)
    assert "계단" in texts


def test_josa_selection_for_road_names(store):
    """받침 유무에 따라 '을/를'을 골라야 음성 안내가 어색해지지 않는다."""
    from route_service.engine.steps import _josa

    assert _josa("평촌대로254번길", "을", "를") == "을"   # 받침 있음
    assert _josa("달안로", "을", "를") == "를"           # 받침 없음
    assert _josa("경수대로", "을", "를") == "를"
    assert _josa(None, "을", "를") == "를"


def test_crossing_step_carries_crosswalk_id():
    """v1.33.1 — crossing 스텝은 cw_mgmt_no 를 crosswalk_id 로 싣고, 그 밖의 스텝은 None."""
    import networkx as nx
    from route_service.engine.steps import build_steps
    from route_service.engine.profiles import get_profile
    G = nx.Graph()
    G.add_node(1, lat=37.39, lon=126.95); G.add_node(2, lat=37.39, lon=126.9505)
    G.add_node(3, lat=37.39, lon=126.9507); G.add_node(4, lat=37.39, lon=126.9512)
    G.add_edge(1, 2, length=44, slope=0.5, link_type="sidewalk")
    G.add_edge(2, 3, length=18, slope=0.2, link_type="crossing", cw_mgmt_no="2024120776")
    G.add_edge(3, 4, length=44, slope=0.5, link_type="sidewalk")
    out = build_steps(G, [1, 2, 3, 4], get_profile("wheelchair_electric"))
    ids = [(x["maneuver"], x.get("crosswalk_id")) for x in out if x["maneuver"] in ("crossing", "depart", "arrive")]
    assert ("crossing", "2024120776") in ids
    assert all(cid is None for m, cid in ids if m != "crossing")
