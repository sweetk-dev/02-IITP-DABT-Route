# -*- coding: utf-8 -*-
"""경로 품질 회귀 테스트 (#26, #27).

- #26: sigungu 부분일치가 타지역 '안양면'까지 포함하던 문제
- #27: 지하차도(road 분류) 경유·유턴 경로가 최단으로 뽑히던 문제
"""
from __future__ import annotations

import json
import os
import sys

import networkx as nx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.engine.graph import NetworkStore  # noqa: E402
from route_service.engine.planner import _uturn_edges, plan  # noqa: E402
from route_service.engine.profiles import get_profile  # noqa: E402
from route_service.poi.store import PoiStore, sigungu_variants  # noqa: E402


# ---------- #26 sigungu 정합 매칭 ----------

def test_sigungu_variants_expand_bare_name():
    assert sigungu_variants("안양") == ["안양시", "안양군", "안양구"]


def test_sigungu_variants_keep_administrative_suffix():
    assert sigungu_variants("안양시") == ["안양시"]
    assert sigungu_variants("만안구") == ["만안구"]
    assert sigungu_variants("") == []


def test_tour_filter_excludes_other_region_myeon(tmp_path):
    """전남 장흥군 '안양면' 소재 POI 가 sigungu='안양' 조회에 포함되면 안 된다."""
    rows = [
        {
            "poi_id": "1", "name": "안양예술공원",
            "addr": "경기도 안양시 만안구 예술공원로 131",
            "latitude": 37.4093, "longitude": 126.9316,
            "elevator_yn": "Y",
        },
        {
            "poi_id": "4318", "name": "수문해수욕장",
            "addr": "전라남도 장흥군 안양면 수문리",
            "latitude": 34.5583, "longitude": 126.9843,
            "elevator_yn": "Y",
        },
    ]
    (tmp_path / "tour_bf.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8"
    )
    store = PoiStore(backend="file", data_dir=str(tmp_path))
    out = store.list_tour_spots(sigungu="안양")
    ids = {r["poi_id"] for r in out}
    assert ids == {"1"}


# ---------- #27 링크 이름 기반 재분류 ----------

def _one_edge_graph(link_type: str, link_name: str) -> nx.Graph:
    G = nx.Graph()
    G.add_node("A", lat=37.39, lon=126.95, node_type="unknown")
    G.add_node("B", lat=37.39, lon=126.951, node_type="unknown")
    G.add_edge("A", "B", length=90.0, slope=0.0, link_type=link_type,
               width=None, curb_cut=None, surface=None,
               link_name=link_name, geometry=None)
    return G


@pytest.mark.parametrize("lt,name,expected", [
    ("road", "일번가지하차도", "underpass"),
    ("sidewalk", "만안지하도", "underpass"),
    ("road", "안양육교", "overpass"),
    ("unknown", "비산고가교", "overpass"),
    ("road", "예시로", "road"),               # 일반 도로는 그대로
    ("crossing", "중앙지하차도앞", "crossing"),  # 명시 조사 타입은 보존
])
def test_normalize_reclassifies_underpass_by_name(lt, name, expected):
    s = NetworkStore()
    s.load_graph_object(_one_edge_graph(lt, name))
    assert s.graph["A"]["B"]["link_type"] == expected


def test_wheelchair_avoids_named_underpass_link():
    """이름으로 재분류된 지하차도 링크는 휠체어 경로에서 하드 차단된다."""
    G = nx.Graph()
    G.add_node("A", lat=37.3900, lon=126.9500, node_type="unknown")
    G.add_node("B", lat=37.3900, lon=126.9520, node_type="unknown")
    G.add_node("C", lat=37.3910, lon=126.9510, node_type="unknown")
    # 지름길: 차량 지하차도 (기존 빌드에서 road 로 들어온 케이스)
    G.add_edge("A", "B", length=100.0, slope=0.0, link_type="road",
               width=None, curb_cut=None, surface=None,
               link_name="일번가지하차도", geometry=None)
    # 우회로: 보도
    G.add_edge("A", "C", length=120.0, slope=0.0, link_type="sidewalk",
               width=2.0, curb_cut=True, surface=None, link_name=None, geometry=None)
    G.add_edge("C", "B", length=120.0, slope=0.0, link_type="sidewalk",
               width=2.0, curb_cut=True, surface=None, link_name=None, geometry=None)
    s = NetworkStore()
    s.load_graph_object(G)
    res = plan(s, "A", "B", get_profile("wheelchair_manual"))
    assert res["routes"][0]["path"] == ["A", "C", "B"]


# ---------- #27 유턴 억제 ----------

def _uturn_graph() -> NetworkStore:
    """유턴 지름길 vs 유턴 없는 우회로.

        S ==(60m)== B     (동쪽으로 갔다가)
        C ==(60m)== T     (되돌아 나오는 평행로 — B 에서 유턴)
        S --(110m)-- D --(110m)-- T   (북쪽 우회, 유턴 없음)
    """
    G = nx.Graph()
    G.add_node("S", lat=37.39000, lon=126.95000, node_type="unknown")
    G.add_node("B", lat=37.39000, lon=126.95200, node_type="unknown")
    G.add_node("C", lat=37.39005, lon=126.95000, node_type="unknown")
    G.add_node("T", lat=37.39010, lon=126.94900, node_type="unknown")
    G.add_node("D", lat=37.39060, lon=126.95000, node_type="unknown")
    common = dict(slope=0.0, link_type="sidewalk", width=2.0, curb_cut=True,
                  surface=None, link_name=None, geometry=None)
    G.add_edge("S", "B", length=60.0, **common)   # 동진
    G.add_edge("B", "C", length=60.0, **common)   # 서진 (S-B 와 역방향 = 유턴)
    G.add_edge("C", "T", length=60.0, **common)
    G.add_edge("S", "D", length=110.0, **common)
    G.add_edge("D", "T", length=110.0, **common)
    s = NetworkStore()
    s.load_graph_object(G)
    return s


def test_uturn_edges_detected_on_switchback():
    s = _uturn_graph()
    edges = _uturn_edges(s.graph, ["S", "B", "C", "T"])
    assert frozenset(("S", "B")) in edges
    assert frozenset(("B", "C")) in edges


def test_plan_prefers_route_without_uturn():
    """길이상 최단(180m)이라도 유턴 경로 대신 유턴 없는 220m 우회로를 채택한다."""
    s = _uturn_graph()
    res = plan(s, "S", "T", get_profile("wheelchair_manual"))
    path = res["routes"][0]["path"]
    assert path == ["S", "D", "T"]
    assert len(_uturn_edges(s.graph, path)) == 0
