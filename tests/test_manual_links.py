# -*- coding: utf-8 -*-
"""수동 확인 링크 적용 (#94)."""
import math

import networkx as nx

from route_service.topomap import manual_links as ml

LAT0, LON0 = 37.4187, 126.9090
KX = 111_320.0 * math.cos(math.radians(LAT0))
KY = 110_540.0


def _ll(dx, dy):
    return {"lat": LAT0 + dy / KY, "lon": LON0 + dx / KX}


def _graph():
    """도로 노드 1(만안로)·2, 이면도로 노드 3·4 — 두 무리는 서로 이어져 있지 않다."""
    G = nx.Graph()
    G.add_node(1, **_ll(0, 0), node_type="unknown")
    G.add_node(2, **_ll(-60, 30), node_type="unknown")
    G.add_node(3, **_ll(50, 40), node_type="unknown")
    G.add_node(4, **_ll(80, 120), node_type="unknown")
    G.add_edge(1, 2, length=67, slope=0.4, link_type="road")
    G.add_edge(3, 4, length=85, slope=0.3, link_type="road")
    return G


def _links():
    exit2 = _ll(25, 35)
    return [
        {"id": "T-01", "from": {"lat": exit2["lat"], "lon": exit2["lon"], "snap_m": 5, "node_type": "entrance"},
         "to": {"node": 1}, "link_type": "sidewalk", "slope": 0.5, "confidence": 0.8},
        {"id": "T-02", "from": {"lat": exit2["lat"], "lon": exit2["lon"], "snap_m": 5},
         "to": {"node": 3}, "link_type": "sidewalk", "slope": 0.3},
        {"id": "T-03", "from": {"node": 999}, "to": {"node": 1}, "link_type": "sidewalk"},
        {"id": "T-04", "from": {"node": 1}, "to": {"node": 2}, "link_type": "sidewalk"},
    ]


def test_creates_node_once_and_joins_clusters():
    G = _graph()
    rep = {r["id"]: r for r in ml.apply_manual_links(G, _links())}
    assert rep["T-01"]["status"] == "added" and rep["T-01"]["created"] == ["MT-01_a"]
    # 같은 좌표는 두 번째 링크에서 새 노드가 아니라 방금 만든 노드로 스냅된다
    assert rep["T-02"]["status"] == "added" and rep["T-02"]["created"] == []
    assert rep["T-02"]["u"] == "MT-01_a"
    assert nx.has_path(G, 2, 4)
    e = G.edges["MT-01_a", 1]
    assert e["topo_source"] == "manual" and e["manual_id"] == "T-01" and e["link_type"] == "sidewalk"
    assert 40 < e["length"] < 46
    assert G.nodes["MT-01_a"]["node_type"] == "entrance"


def test_created_node_is_removed_when_link_collapses():
    G = _graph()
    p = _ll(30, 30)
    rep = ml.apply_manual_links(G, [
        {"id": "T-10", "from": {"lat": p["lat"], "lon": p["lon"], "snap_m": 5},
         "to": {"lat": p["lat"] + 0.00001, "lon": p["lon"], "snap_m": 5}, "link_type": "sidewalk"}])
    assert rep[0]["status"] == "exists"
    assert not [n for n in G if str(n).startswith("MT-10")]


def test_string_node_id_resolves_to_int_node():
    G = _graph()
    rep = ml.apply_manual_links(G, [{"id": "T-11", "from": {"node": "1"}, "to": {"node": 3}, "link_type": "sidewalk"}])
    assert rep[0]["status"] == "added" and G.has_edge(1, 3)


def test_validate_rejects_bad_items():
    import pytest
    bad = [
        [{"id": "A", "from": {"node": 1}, "to": {"node": 2}, "link_type": "escalator"}],
        [{"id": "A", "from": {"node": 1}, "to": {"lat": 91, "lon": 0}}],
        [{"id": "A", "from": {"node": 1}, "to": {"node": 2}}, {"id": "A", "from": {"node": 1}, "to": {"node": 3}}],
        [{"id": "A", "from": {"node": 1}}],
        [{"id": "A", "from": {"node": 1}, "to": {"node": 2}, "confidence": 1.5}],
        [{"id": "X" * 20, "from": {"node": 1}, "to": {"node": 2}}],
    ]
    for links in bad:
        with pytest.raises(ml.ManualLinkError):
            ml.validate_links(links)
    ml.validate_links([{"id": "A", "from": {"node": 1}, "to": {"lat": 37.4, "lon": 126.9}, "link_type": "elevator"}])


def test_missing_and_existing_are_skipped():
    G = _graph()
    rep = {r["id"]: r for r in ml.apply_manual_links(G, _links())}
    assert rep["T-03"]["status"] == "missing_node"
    assert rep["T-04"]["status"] == "exists"
    assert G.edges[1, 2]["link_type"] == "road"       # 기존 링크는 건드리지 않는다
    assert "MT-03_a" not in G


def test_manual_links_pass_profile_gate():
    from route_service.engine.planner import edge_passable
    from route_service.engine.profiles import get_profile
    G = _graph()
    ml.apply_manual_links(G, _links())
    e = G.edges["MT-01_a", 1]
    for pid in ("wheelchair_electric", "wheelchair_manual"):
        p = get_profile(pid)
        assert edge_passable(e, p, p.hard_slope())


def test_repo_links_file_is_valid():
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "manual_links", "anyang.json")
    links = ml.load_links(path)
    ids = [l["id"] for l in links]
    assert len(ids) == len(set(ids)) and len(ids) >= 6
    ml.validate_links(links)          # 레포 목록은 항상 검사를 통과해야 한다


def test_validate_rejects_swapped_or_far_geometry():
    """geometry 가 [lon, lat] 로 뒤집히거나 끝점에서 멀리 떨어진 점을 담으면 거부한다."""
    import pytest
    base = {"id": "G-01", "from": {"lat": 37.4184789, "lon": 126.9087988},
            "to": {"lat": 37.4188986, "lon": 126.9092237}, "link_type": "sidewalk"}
    ok = dict(base, geometry=[[37.4186, 126.9089], [37.4188, 126.9091]])
    ml.validate_links([ok])
    swapped = dict(base, geometry=[[126.9089, 37.4186], [126.9091, 37.4188]])       # [lon, lat]
    with pytest.raises(ml.ManualLinkError, match="좌표 범위 밖"):
        ml.validate_links([swapped])
    far = dict(base, geometry=[[37.4186, 126.9089], [37.5186, 126.9089]])           # 약 11km 밖
    with pytest.raises(ml.ManualLinkError, match="끝점에서"):
        ml.validate_links([far])
    not_number = dict(base, geometry=[["a", "b"]])
    with pytest.raises(ml.ManualLinkError):
        ml.validate_links([not_number])
    # 두 끝점이 모두 node 참조면 geometry 첫 점을 기준으로 흩어짐만 본다
    by_node = {"id": "G-02", "from": {"node": 1}, "to": {"node": 2}}
    ml.validate_links([dict(by_node, geometry=[[37.4186, 126.9089], [37.4188, 126.9091]])])
    with pytest.raises(ml.ManualLinkError, match="끝점에서"):
        ml.validate_links([dict(by_node, geometry=[[37.4186, 126.9089], [37.9, 126.9089]])])
    with pytest.raises(ml.ManualLinkError, match="좌표 범위 밖"):
        ml.validate_links([dict(by_node, geometry=[[126.9089, 37.4186]])])


def test_valid_geometry_still_applies():
    G = _graph()
    p = G.nodes[1]
    rep = ml.apply_manual_links(G, [{"id": "G-03", "from": {"node": 1}, "to": {"node": 3},
                                     "geometry": [[p["lat"], p["lon"]]]}])
    ml.validate_links([{"id": "G-03", "from": {"node": 1}, "to": {"node": 3},
                        "geometry": [[p["lat"], p["lon"]]]}])
    assert rep[0]["status"] == "added" and G.edges[1, 3]["geometry"] == [(p["lat"], p["lon"])]
