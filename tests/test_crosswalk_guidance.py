# -*- coding: utf-8 -*-
"""노드 지점 부착 횡단보도 안내 계층 (v1.13.0).

안양시 원천 횡단보도 다수는 crossing 링크가 아니라 최근접 노드에 지점 부착돼 있다.
안내 계층은 경로가 그 노드를 지날 때 횡단 안내 스텝을 만들어야 하고,
위상(경로·거리)과 기존 crossing_cnt 계약은 건드리지 않아야 한다.
"""
from __future__ import annotations

import networkx as nx
import pytest

from route_service.engine.graph import NetworkStore
from route_service.engine.planner import plan
from route_service.engine.profiles import get_profile
from route_service.engine.steps import build_steps


def _base_graph() -> nx.Graph:
    """N1 --sidewalk-- N2 --sidewalk-- N3 (일자 경로)."""
    G = nx.Graph()
    G.add_node("N1", lat=37.3900, lon=126.9500, node_type="intersection")
    G.add_node("N2", lat=37.3900, lon=126.9511, node_type="intersection")
    G.add_node("N3", lat=37.3909, lon=126.9511, node_type="entrance")
    G.add_edge("N1", "N2", length=100.0, slope=1.0, link_type="sidewalk",
               width=2.0, curb_cut=True, surface="asphalt", link_name="예시로", geometry=None)
    G.add_edge("N2", "N3", length=100.0, slope=1.5, link_type="sidewalk",
               width=2.0, curb_cut=True, surface="asphalt", link_name="예시2로", geometry=None)
    return G


def _store(G) -> NetworkStore:
    s = NetworkStore()
    s.load_graph_object(G, version="test-cw", region="테스트")
    return s


def _route(store, profile_id="wheelchair_manual"):
    p = get_profile(profile_id)
    r = plan(store, "N1", "N3", p)["routes"][0]
    return p, r


def test_crossing_point_step_generated_with_unknown_curb_cut():
    """부착 노드를 지나면 안내 스텝이 생기고, 턱낮춤 None 은 '미상'으로 표기돼야 한다."""
    G = _base_graph()
    G.nodes["N2"].update(crosswalk_cnt=1, cw_mgmt_nos=["2024000001"],
                         cw_curb_cut=None, cw_tactile_paving=None)
    p, r = _route(_store(G))
    steps = build_steps(_store(G).graph, r["path"], p)

    cw = [s for s in steps if s["maneuver"] == "crossing_point"]
    assert len(cw) == 1
    assert "횡단보도" in cw[0]["instruction"]
    assert "턱낮춤 미상" in cw[0]["warnings"]
    assert cw[0]["distance_m"] == 0
    assert cw[0]["crosswalk_cnt"] == 1
    # 노드 좌표에 붙어야 한다
    assert cw[0]["coord"] == [37.39, 126.9511]


def test_crossing_point_step_warns_when_no_curb_cut():
    """턱낮춤 False 는 미상이 아니라 명시 경고."""
    G = _base_graph()
    G.nodes["N2"].update(crosswalk_cnt=1, cw_curb_cut=False, cw_tactile_paving=False)
    p, r = _route(_store(G))
    steps = build_steps(_store(G).graph, r["path"], p)

    cw = [s for s in steps if s["maneuver"] == "crossing_point"][0]
    assert "턱낮춤 없음" in cw["warnings"]
    assert "점자블록 없음" in cw["warnings"]
    assert "턱낮춤 미상" not in cw["warnings"]


def test_multiple_crosswalks_on_node_mentioned_in_instruction():
    G = _base_graph()
    G.nodes["N2"].update(crosswalk_cnt=3, cw_curb_cut=None)
    p, r = _route(_store(G))
    steps = build_steps(_store(G).graph, r["path"], p)
    cw = [s for s in steps if s["maneuver"] == "crossing_point"][0]
    assert "3개" in cw["instruction"]
    assert cw["crosswalk_cnt"] == 3


def test_depart_arrive_and_idx_remain_consistent():
    """안내 스텝이 끼어들어도 출발/도착과 idx 연속성은 유지돼야 한다."""
    G = _base_graph()
    G.nodes["N2"].update(crosswalk_cnt=1, cw_curb_cut=None)
    p, r = _route(_store(G))
    steps = build_steps(_store(G).graph, r["path"], p)
    assert steps[0]["maneuver"] == "depart"
    assert steps[-1]["maneuver"] == "arrive"
    assert [s["idx"] for s in steps] == list(range(len(steps)))


def test_no_duplicate_announcement_next_to_crossing_link():
    """crossing 링크 앞뒤 노드의 지점 부착분은 링크 안내와 중복되므로 생략."""
    G = nx.Graph()
    G.add_node("N1", lat=37.3900, lon=126.9500, node_type="intersection")
    G.add_node("N2", lat=37.3900, lon=126.9511, node_type="crossing")
    G.add_node("N3", lat=37.3902, lon=126.9513, node_type="crossing")
    G.add_node("N4", lat=37.3909, lon=126.9513, node_type="entrance")
    G.add_edge("N1", "N2", length=100.0, slope=0.5, link_type="sidewalk",
               width=2.0, curb_cut=True, surface=None, link_name=None, geometry=None)
    G.add_edge("N2", "N3", length=20.0, slope=0.0, link_type="crossing",
               width=3.0, curb_cut=True, surface=None, link_name=None, geometry=None)
    G.add_edge("N3", "N4", length=80.0, slope=0.5, link_type="sidewalk",
               width=2.0, curb_cut=True, surface=None, link_name=None, geometry=None)
    G.nodes["N2"].update(crosswalk_cnt=1, cw_curb_cut=None)
    G.nodes["N3"].update(crosswalk_cnt=1, cw_curb_cut=None)

    store = _store(G)
    p = get_profile("wheelchair_manual")
    r = plan(store, "N1", "N4", p)["routes"][0]
    steps = build_steps(store.graph, r["path"], p)

    assert not [s for s in steps if s["maneuver"] == "crossing_point"]
    # crossing 링크 자체의 안내는 그대로 있어야 한다
    assert any(s["link_type"] == "crossing" for s in steps)


def test_summary_counts_point_crosswalks_separately():
    """summary.crossing_point_cnt 는 노드 부착분 합계, crossing_cnt 는 링크 수 그대로."""
    G = _base_graph()
    G.nodes["N2"].update(crosswalk_cnt=2, cw_curb_cut=None)
    G.nodes["N3"].update(crosswalk_cnt=1, cw_curb_cut=None)
    p, r = _route(_store(G))

    assert r["summary"]["crossing_point_cnt"] == 3   # 경로 노드 전체 합
    assert r["summary"]["crossing_cnt"] == 0         # crossing 링크는 없음 — 의미 불변


def test_summary_warns_when_attached_crosswalk_lacks_curb_cut():
    G = _base_graph()
    G.nodes["N2"].update(crosswalk_cnt=1, cw_curb_cut=False)
    p, r = _route(_store(G))
    assert "턱낮춤 없는 횡단보도 구간이 있습니다" in r["summary"]["warnings"]


def test_endpoint_crosswalks_get_informational_steps():
    """출발·도착 노드 부착분도 침묵하지 않아야 한다(실증 2b·3a: 2-노드 경로).

    다만 실제 횡단 여부를 단정할 수 없으므로 지시형("건너세요") 대신 정보형으로 알린다.
    """
    G = _base_graph()
    G.nodes["N1"].update(crosswalk_cnt=4, cw_curb_cut=None)
    G.nodes["N3"].update(crosswalk_cnt=1, cw_curb_cut=None)
    p, r = _route(_store(G))
    steps = build_steps(_store(G).graph, r["path"], p)

    cw = [s for s in steps if s["maneuver"] == "crossing_point"]
    assert len(cw) == 2
    assert "출발 지점에 횡단보도 4개가 있습니다" in cw[0]["instruction"]
    assert "도착 지점에 횡단보도가 있습니다" in cw[1]["instruction"]
    assert "건너세요" not in cw[0]["instruction"]
    # 순서 계약: depart 가 항상 처음, arrive 가 항상 마지막
    assert steps[0]["maneuver"] == "depart"
    assert steps[-1]["maneuver"] == "arrive"
    assert [s["idx"] for s in steps] == list(range(len(steps)))


def test_plain_graph_unchanged(store):
    """부착 정보가 없는 그래프(기존 픽스처)는 이전과 동일하게 동작해야 한다."""
    p = get_profile("wheelchair_manual")
    r = plan(store, "N1", "N3", p)["routes"][0]
    steps = build_steps(store.graph, r["path"], p)
    assert not [s for s in steps if s["maneuver"] == "crossing_point"]
    assert r["summary"]["crossing_point_cnt"] == 0
