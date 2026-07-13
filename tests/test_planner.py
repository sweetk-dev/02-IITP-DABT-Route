# -*- coding: utf-8 -*-
import pytest

from route_service.engine.planner import NoRouteError, off_route_distance_m, plan
from route_service.engine.profiles import get_profile


def test_manual_wheelchair_detours_around_steps(store):
    res = plan(store, "N1", "N3", get_profile("wheelchair_manual"))
    path = res["routes"][0]["path"]
    assert path == ["N1", "N2", "N3"]          # 계단 지름길(N4) 회피
    assert res["routes"][0]["summary"]["stairs_cnt"] == 0
    assert res["fallback"]["used"] is False


def test_general_walk_takes_shortcut(store):
    res = plan(store, "N1", "N3", get_profile("walk"))
    assert res["routes"][0]["path"] == ["N1", "N4", "N3"]   # 계단 허용 -> 최단


def test_summary_fields(store):
    s = plan(store, "N1", "N3", get_profile("wheelchair_manual"))["routes"][0]["summary"]
    assert s["total_distance_m"] == 200
    assert s["max_slope_deg"] == 1.5
    assert s["duration_sec"] > 0
    assert 0.0 <= s["accessibility_score"] <= 1.0


def test_geometry_is_coordinate_list(store):
    geom = plan(store, "N1", "N3", get_profile("wheelchair_manual"))["routes"][0]["geometry"]
    assert len(geom) >= 2
    assert all(len(c) == 2 for c in geom)


def test_no_route_raises_when_all_blocked(store):
    G = store.graph
    G["N1"]["N2"]["link_type"] = "steps"
    G["N2"]["N3"]["link_type"] = "steps"
    G["N1"]["N4"]["link_type"] = "steps"
    G["N4"]["N3"]["link_type"] = "steps"
    with pytest.raises(NoRouteError):
        plan(store, "N1", "N3", get_profile("wheelchair_manual"), relax=False)


def test_fallback_relaxes_slope_limit(store):
    G = store.graph
    G["N1"]["N2"]["slope"] = 5.0        # 수동휠체어 한계(4도) 초과
    G["N2"]["N3"]["slope"] = 5.0
    res = plan(store, "N1", "N3", get_profile("wheelchair_manual"))
    assert res["fallback"]["used"] is True
    assert res["fallback"]["applied_max_slope_deg"] >= 6.0


def test_off_route_distance(store):
    geom = plan(store, "N1", "N3", get_profile("wheelchair_manual"))["routes"][0]["geometry"]
    assert off_route_distance_m(geom, 37.3900, 126.9505) < 20
    assert off_route_distance_m(geom, 37.3850, 126.9505) > 200
