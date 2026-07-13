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
