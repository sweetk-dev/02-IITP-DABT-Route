# -*- coding: utf-8 -*-
import pytest

from route_service.engine.planner import NoRouteError, off_route_distance_m, plan
from route_service.engine.profiles import get_profile
from route_service.engine.snap import snap


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
    G["N1"]["N2"]["slope"] = 9.0        # 수동휠체어 하드 상한(8도) 초과 (v1.20.0: 권장 4도 초과는 가중, 8도 초과가 차단)
    G["N2"]["N3"]["slope"] = 9.0
    res = plan(store, "N1", "N3", get_profile("wheelchair_manual"))
    assert res["fallback"]["used"] is True
    assert res["fallback"]["applied_max_slope_deg"] >= 10.0


def test_over_recommended_slope_is_penalized_not_blocked(store):
    """v1.20.0 — 권장 경사(4도) 초과·하드 상한(8도) 이하 링크는 통행 가능하되 경고가 붙는다.
    (실증 2026-09-03: 5~8도 링크 하나 때문에 205m 직행이 649m 우회로 바뀌었다)"""
    G = store.graph
    G["N1"]["N2"]["slope"] = 5.0
    G["N2"]["N3"]["slope"] = 5.0
    res = plan(store, "N1", "N3", get_profile("wheelchair_manual"))
    assert res["fallback"]["used"] is False
    assert res["routes"][0]["path"] == ["N1", "N2", "N3"]
    assert any("권장 경사" in w for w in res["routes"][0]["summary"]["warnings"])


def test_short_link_is_never_blocked_by_slope(store):
    """v1.20.0 — 15m 미만 짧은 링크의 DEM 경사(격자 보간 튐)는 통행을 막지 않는다."""
    G = store.graph
    G["N1"]["N2"]["slope"] = 12.0
    G["N1"]["N2"]["length"] = 8.0
    res = plan(store, "N1", "N3", get_profile("wheelchair_manual"), relax=False)
    assert res["routes"][0]["path"] == ["N1", "N2", "N3"]
    assert not any("권장 경사" in w for w in res["routes"][0]["summary"]["warnings"])


def test_profile_hard_slope_and_constraint_override():
    from route_service.engine.profiles import get_profile as gp
    from dataclasses import replace
    assert gp("wheelchair_manual").hard_slope() == 8.0
    assert gp("wheelchair_electric").hard_slope() == 10.0
    assert gp("visual").hard_slope() == 12.0          # hard 미지정 → max 와 동일
    assert replace(gp("wheelchair_manual"), max_slope_deg=20.0).hard_slope() == 20.0   # 제약으로 올리면 같이 오른다


def test_off_route_distance(store):
    geom = plan(store, "N1", "N3", get_profile("wheelchair_manual"))["routes"][0]["geometry"]
    assert off_route_distance_m(geom, 37.3900, 126.9505) < 20
    assert off_route_distance_m(geom, 37.3850, 126.9505) > 200


def test_no_slope_data_does_not_score_perfect(store):
    """경사 데이터가 없는 네트워크에서 '경사 0 = 만점'은 거짓 안심을 준다."""
    G = store.graph
    for _u, _v, d in G.edges(data=True):
        d["slope"] = 0.0
    store.load_graph_object(G, version="no-slope")
    assert store.meta["slope_coverage"] == 0.0

    s = plan(store, "N1", "N3", get_profile("wheelchair_manual"))["routes"][0]["summary"]
    assert s["accessibility_score"] <= 0.6
    assert any("경사 데이터가 없어" in w for w in s["warnings"])


def test_snap_avoids_isolated_component(store):
    """프로필 제약을 걸면 보행망이 조각난다. 가장 가까운 노드가 고립 조각이면
    조금 더 걸어서라도 갈 수 있는 노드에 붙여야 한다 — 안 그러면 '경로 없음'이 된다."""
    G = store.graph
    # N3 바로 옆에 계단으로만 연결된 섬(N9)을 만든다
    G.add_node("N9", lat=37.39091, lon=126.95111, node_type="intersection")
    G.add_node("N10", lat=37.39095, lon=126.95115, node_type="intersection")
    G.add_edge("N9", "N10", length=10.0, slope=0.5, link_type="sidewalk",
               width=2.0, curb_cut=True, surface=None, link_name="섬길", geometry=None)
    G.add_edge("N3", "N9", length=5.0, slope=0.5, link_type="steps",
               width=1.0, curb_cut=None, surface=None, link_name="섬계단", geometry=None)
    store.load_graph_object(G, version="island")

    p = get_profile("wheelchair_manual")
    allowed = store.reachable_nodes(p, p.max_slope_deg + 4.0)
    assert "N9" not in allowed and "N3" in allowed

    # N9 바로 옆 좌표로 스냅해도 도달 가능한 N3 에 붙어야 한다
    s = snap(store, 37.39091, 126.95112, p, allowed=allowed)
    assert s["node_id"] == "N3"

    res = plan(store, "N1", s["node_id"], p)
    assert res["routes"][0]["path"][-1] == "N3"
