# -*- coding: utf-8 -*-
"""엔진 보호 동작.

  1) 에스컬레이터(highway=steps + conveying)는 계단으로 분류돼 휠체어 프로필에서 회피된다
  2) 유턴 억제 재탐색은 후보가 나아지지 않으면 멈춘다(같은 탐색을 되풀이하지 않는다)
  3) 짧은 링크로 이월된 회전각은 ±180° 로 되돌려 판정한다
  4) 실측 출입구 파일이 깨져 있어도 기동은 계속된다
"""
import math

import networkx as nx
import pytest

from route_service.engine import planner
from route_service.engine.access import ManualEntrances
from route_service.engine.graph import NetworkStore
from route_service.engine.profiles import get_profile
from route_service.engine.sources.osm import classify_link
from route_service.engine.steps import build_steps

LAT0, LON0 = 37.3900, 126.9500
KY = 110_540.0
KX = 111_320.0 * math.cos(math.radians(LAT0))


def _ll(east_m, north_m):
    return LAT0 + north_m / KY, LON0 + east_m / KX


def _store(nodes, edges):
    G = nx.Graph()
    for nid, (e, n) in nodes.items():
        lat, lon = _ll(e, n)
        G.add_node(nid, lat=lat, lon=lon, node_type="intersection")
    for u, v, length, extra in edges:
        d = dict(length=length, slope=0.5, link_type="sidewalk", width=2.0, curb_cut=True,
                 surface="asphalt", link_name=None, geometry=None)
        d.update(extra)
        G.add_edge(u, v, **d)
    s = NetworkStore()
    s.load_graph_object(G, version="t", region="t")
    return s


# ── 1. 에스컬레이터 ─────────────────────────────────────────
@pytest.mark.parametrize("conveying", ["yes", "forward", "backward"])
def test_escalator_is_classified_as_steps(conveying):
    assert classify_link({"highway": "steps", "conveying": conveying}) == "steps"
    assert classify_link({"highway": "steps"}) == "steps"
    assert classify_link({"highway": "elevator"}) == "elevator"


def test_wheelchair_route_avoids_escalator_link():
    """에스컬레이터 지름길(40m)과 보도 우회로(120m) — 휠체어는 우회로, 안내에 '경사로'가 없다."""
    esc = classify_link({"highway": "steps", "conveying": "yes"})
    st = _store({"A": (0, 0), "B": (40, 0), "C": (20, 50)},
                [("A", "B", 40.0, {"link_type": esc}), ("A", "C", 60.0, {}), ("C", "B", 60.0, {})])
    for pid in ("wheelchair_manual", "wheelchair_electric"):
        p = get_profile(pid)
        assert not planner.edge_passable(st.graph["A"]["B"], p, p.hard_slope())
        r = planner.plan(st, "A", "B", p)["routes"][0]
        assert r["path"] == ["A", "C", "B"]
        assert r["summary"]["ramp_cnt"] == 0 and r["summary"]["stairs_cnt"] == 0
        assert not any("경사로" in s["instruction"] for s in build_steps(st.graph, r["path"], p))
    # 계단을 지날 수 있는 프로필에는 계단 구간으로 안내된다
    walk = get_profile("walk")
    r = planner.plan(st, "A", "B", walk)["routes"][0]
    assert r["path"] == ["A", "B"]
    assert build_steps(st.graph, r["path"], walk)[0]["link_type"] == "steps"


# ── 2. 유턴 억제 재탐색 ─────────────────────────────────────
def test_uturn_retry_stops_when_candidate_does_not_improve(monkeypatch):
    """두 경로 모두 막다른 가지 끝에서 되돌아오는 모양 — 재탐색 후보가 나아지지 않으면 한 번으로 끝낸다."""
    st = _store({"S": (0, 0), "G": (60, 3), "B1": (120, 0), "B2": (125, 6)},
                [("S", "B1", 120.0, {}), ("B1", "G", 60.0, {}), ("S", "B2", 126.0, {}), ("B2", "G", 66.0, {})])
    p = get_profile("walk")
    assert len(planner._uturn_edges(st.graph, ["S", "B1", "G"])) == 2
    assert len(planner._uturn_edges(st.graph, ["S", "B2", "G"])) == 2
    calls = []
    real = planner._astar

    def spy(G, a, b, profile, lvl, penalized=None):
        calls.append(set(penalized or ()))
        return real(G, a, b, profile, lvl, penalized)
    monkeypatch.setattr(planner, "_astar", spy)
    r = planner.plan(st, "S", "G", p)["routes"][0]
    assert r["path"] == ["S", "B1", "G"], "나아진 후보가 없으면 처음 경로를 유지한다"
    assert len(calls) == 2, "처음 탐색 1회 + 재탐색 1회여야 한다(같은 재탐색 반복 없음): %d회" % len(calls)


def test_uturn_retry_still_adopts_better_candidate():
    """유턴 없는 우회로가 있으면 종전대로 그쪽을 채택한다."""
    st = _store({"S": (0, 0), "G": (60, 3), "B1": (120, 0), "M": (30, 40)},
                [("S", "B1", 120.0, {}), ("B1", "G", 60.0, {}), ("S", "M", 50.0, {}), ("M", "G", 150.0, {})])
    r = planner.plan(st, "S", "G", get_profile("walk"))["routes"][0]
    assert r["path"] == ["S", "M", "G"]


# ── 3. 이월 회전각 정규화 ───────────────────────────────────
def _polyline_store(headings_lengths):
    """(방위각°, 길이 m) 목록대로 이어지는 일자 경로 그래프와 노드열."""
    nodes, edges, x, y = {"P0": (0.0, 0.0)}, [], 0.0, 0.0
    for i, (brg, ln) in enumerate(headings_lengths):
        x += ln * math.sin(math.radians(brg))
        y += ln * math.cos(math.radians(brg))
        nodes["P%d" % (i + 1)] = (x, y)
        edges.append(("P%d" % i, "P%d" % (i + 1), float(ln), {}))
    return _store(nodes, edges), ["P%d" % i for i in range(len(headings_lengths) + 1)]


def test_carried_turn_angle_is_normalized_before_judging():
    """우 120° → 8m 짧은 링크 → 우 120°: 합 240° 는 좌 120° 와 같다 — 유턴이 아니라 급좌회전."""
    st, path = _polyline_store([(0, 50), (120, 8), (240, 40)])
    steps = build_steps(st.graph, path, get_profile("walk"))
    mans = [s["maneuver"] for s in steps]
    assert "uturn" not in mans, mans
    assert mans == ["depart", "sharp_left", "arrive"], mans
    assert not any("유턴" in s["instruction"] for s in steps)


def test_carried_turn_angle_over_two_short_links():
    """우 100° 를 세 번(짧은 링크 둘 경유): 합 300° = 좌 60° — 좌회전으로 안내한다."""
    st, path = _polyline_store([(0, 50), (100, 8), (200, 8), (300, 40)])
    mans = [s["maneuver"] for s in build_steps(st.graph, path, get_profile("walk"))]
    assert mans == ["depart", "left", "arrive"], mans


def test_real_uturn_is_still_a_uturn():
    st, path = _polyline_store([(0, 50), (90, 8), (180, 40)])       # 우 90° 두 번 = 180°
    mans = [s["maneuver"] for s in build_steps(st.graph, path, get_profile("walk"))]
    assert mans == ["depart", "uturn", "arrive"], mans


# ── 4. 실측 출입구 파일 ─────────────────────────────────────
@pytest.mark.parametrize("content", ['{"A": {"lat": 37.39, "lng": 126.95},}', "[1, 2]", "", '"x"'])
def test_broken_entrances_file_does_not_stop_startup(tmp_path, content):
    f = tmp_path / "entrances.json"
    f.write_text(content, encoding="utf-8")
    ent = ManualEntrances(str(f))                  # 예외 없이 빈 값
    assert ent.items == {} and ent.get("A") is None


def test_entrances_file_with_bad_encoding_or_bad_item(tmp_path):
    f = tmp_path / "entrances.json"
    f.write_bytes(b'{"A": "\xff\xfe"}')
    assert ManualEntrances(str(f)).items == {}
    f.write_text('{"A": "정문", "B": {"lat": 37.39, "lng": 126.95, "note": "정문"}}', encoding="utf-8")
    ent = ManualEntrances(str(f))
    assert ent.get("A") is None                    # 형식이 다른 항목은 없는 것으로 본다
    assert ent.get("B") == {"lat": 37.39, "lng": 126.95, "source": "manual_survey", "note": "정문"}
