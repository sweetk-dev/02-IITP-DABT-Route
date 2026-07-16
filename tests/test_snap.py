# -*- coding: utf-8 -*-
from route_service.engine.profiles import get_profile
from route_service.engine.snap import snap


def test_snap_returns_nearest_node(store):
    r = snap(store, 37.39005, 126.95005, get_profile("walk"))
    assert r["node_id"] == "N1"
    assert r["dist_m"] < 20
    assert r["reachable"] is True


def test_snap_marks_unreachable_when_far(store):
    r = snap(store, 37.5000, 127.1000, get_profile("walk"), max_dist_m=300)
    assert r["reachable"] is False


def test_snap_skips_node_without_passable_edge(store):
    # N4 는 수동휠체어에게 계단(steps)·9도 언덕으로만 연결 -> 스냅 대상에서 제외되어야 한다
    r = snap(store, 37.3905, 126.9500, get_profile("wheelchair_manual"))
    assert r["node_id"] != "N4"
