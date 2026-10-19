# -*- coding: utf-8 -*-
"""수동 확인 링크 적용 (#94)."""
import math

import networkx as nx
import pytest

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
    assert len(ids) == len(set(ids)) and len(ids) >= 3
    ml.validate_links(links)          # 레포 목록은 항상 검사를 통과해야 한다


def test_repo_links_exclude_gwanak_fare_zone_passage():
    """관악역 2층 대합실 통로는 개찰구 안(운임 구역)이다(사전답사 2026-10-01).

    출구를 바꾸려고 지나갈 수 있는 길이 아니므로 목록에 두지 않는다 — 두면 1번 출구로 들고 나는 경로가
    개찰구 안 대합실을 지나가게 안내한다. 1번 ↔ 2번 출구는 역사 아래 만안로(GWANAK-01·02)로 잇는다.
    """
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "manual_links", "anyang.json")
    links = ml.load_links(path)
    ids = {l["id"] for l in links}
    assert not ids & {"GWANAK-04", "GWANAK-05", "GWANAK-06"}
    assert {"GWANAK-01", "GWANAK-02", "GWANAK-03"} <= ids
    gwanak = [l for l in links if str(l["id"]).startswith("GWANAK")]
    assert not any("대합실" in (l.get("link_name") or "") or l.get("link_type") == "elevator" for l in gwanak)


def test_repo_links_route_gwanak_exits_via_street():
    """레포 목록을 관악역 주변 축소 그래프에 적용하면 1번 ↔ 2번 출구가 역사 아래 만안로로 이어진다.

    도로 노드 ID 는 운영 그래프와 같은 3903318504(역사 아래 만안로)·4696341341(경수대로1273번길).
    좌표는 출구 링크 길이(21m·42m·25m)에 맞춘 근삿값이다.
    """
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "manual_links", "anyang.json")
    G = nx.Graph()
    G.add_node(3903318504, lat=37.4185572, lon=126.9090177, node_type="unknown")      # 역사 아래 만안로
    G.add_node(4696341341, lat=37.4190700, lon=126.9094400, node_type="unknown")      # 경수대로1273번길
    G.add_node(1, lat=37.4183000, lon=126.9083000, node_type="unknown")               # 만안로 서쪽
    G.add_edge(3903318504, 1, length=80, slope=0.3, link_type="sidewalk")
    report = ml.apply_manual_links(G, ml.load_links(path))
    assert all(r["status"] == "added" for r in report if str(r["id"]).startswith("GWANAK"))
    by_id = {r["id"]: r for r in report}
    exit1, exit2 = by_id["GWANAK-01"]["u"], by_id["GWANAK-02"]["u"]
    assert exit1 != exit2
    path_nodes = nx.shortest_path(G, exit1, exit2, weight="length")
    assert path_nodes == [exit1, 3903318504, exit2]
    dist = nx.shortest_path_length(G, exit1, exit2, weight="length")
    assert 55 <= dist <= 70
    # 1번 출구에서 동쪽 이면도로(김중업 방면)까지 승강기·대합실 링크 없이 간다
    east = nx.shortest_path(G, exit1, 4696341341, weight="length")
    for u, v in zip(east, east[1:]):
        e = G.edges[u, v]
        assert e.get("link_type") != "elevator" and "대합실" not in (e.get("link_name") or "")


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
    # 저장 geometry 는 양 끝 노드 좌표를 포함한다(v1.37.0) — 끝점과 같은 중간점은 한 번만
    q = G.nodes[3]
    assert rep[0]["status"] == "added" and G.edges[1, 3]["geometry"] == [(p["lat"], p["lon"]), (q["lat"], q["lon"])]


# ── v1.37.0 (#118) 횡단보도 관리번호·경로선 끝점 ─────────────────────────────

def test_crossing_cw_mgmt_no_stored():
    G = _graph()
    rep = ml.apply_manual_links(G, [{"id": "X-01", "from": {"node": 1}, "to": {"node": 3},
                                     "link_type": "crossing", "cw_mgmt_no": " 2024121334 "}])
    assert rep[0]["status"] == "added"
    assert G.edges[1, 3]["cw_mgmt_no"] == "2024121334"


def test_cw_mgmt_no_absent_keeps_old_shape():
    G = _graph()
    ml.apply_manual_links(G, [{"id": "X-02", "from": {"node": 1}, "to": {"node": 3}, "link_type": "crossing"}])
    assert "cw_mgmt_no" not in G.edges[1, 3]


def test_cw_mgmt_no_only_on_crossing():
    with pytest.raises(ml.ManualLinkError):
        ml.validate_links([{"id": "X-03", "from": {"node": 1}, "to": {"node": 3},
                            "link_type": "sidewalk", "cw_mgmt_no": "2024121334"}])
    with pytest.raises(ml.ManualLinkError):
        ml.validate_links([{"id": "X-04", "from": {"node": 1}, "to": {"node": 3},
                            "link_type": "crossing", "cw_mgmt_no": ""}])
    with pytest.raises(ml.ManualLinkError):
        ml.validate_links([{"id": "X-05", "from": {"node": 1}, "to": {"node": 3},
                            "link_type": "crossing", "cw_mgmt_no": 2024121334}])


def test_geometry_stored_with_endpoints_so_route_line_is_continuous():
    """중간점만 저장하면 edge_coords 가 끝 노드를 빼고 그려 경로선이 끊긴다."""
    from route_service.engine.graph import edge_coords
    G = _graph()
    mid = _ll(25, 20)
    ml.apply_manual_links(G, [{"id": "X-06", "from": {"node": 1}, "to": {"node": 3},
                               "geometry": [[mid["lat"], mid["lon"]]]}])
    c = edge_coords(G, 1, 3)
    assert c[0] == (G.nodes[1]["lat"], G.nodes[1]["lon"])
    assert c[-1] == (G.nodes[3]["lat"], G.nodes[3]["lon"])
    assert len(c) == 3
    # 반대 방향으로 읽어도 끝점이 맞는다
    r = edge_coords(G, 3, 1)
    assert r[0] == (G.nodes[3]["lat"], G.nodes[3]["lon"])


# ── v1.38.0 명학역 1번 출구 ~ 안양아트센터 정문(MH-*) · 표시 없는 횡단(unmarked) ─────────────

def test_unmarked_crossing_stored():
    G = _graph()
    rep = ml.apply_manual_links(G, [{"id": "U-01", "from": {"node": 1}, "to": {"node": 3},
                                     "link_type": "crossing", "unmarked": True}])
    assert rep[0]["status"] == "added" and G.edges[1, 3]["unmarked"] is True
    # 키가 없거나 false 면 속성을 두지 않는다(종전 횡단 링크와 같은 모양)
    G2 = _graph()
    ml.apply_manual_links(G2, [{"id": "U-02", "from": {"node": 1}, "to": {"node": 3},
                                "link_type": "crossing", "unmarked": False}])
    assert "unmarked" not in G2.edges[1, 3]


def test_unmarked_validation():
    base = {"id": "U-03", "from": {"node": 1}, "to": {"node": 3}, "link_type": "crossing"}
    ml.validate_links([dict(base, unmarked=True)])
    with pytest.raises(ml.ManualLinkError, match="true/false"):
        ml.validate_links([dict(base, unmarked="false")])          # 문자열은 참으로 읽힐 수 있다
    with pytest.raises(ml.ManualLinkError, match="crossing 링크에만"):
        ml.validate_links([dict(base, link_type="sidewalk", unmarked=True)])
    with pytest.raises(ml.ManualLinkError, match="unmarked 일 수 없다"):
        ml.validate_links([dict(base, unmarked=True, cw_mgmt_no="2024121569")])


def test_unmarked_manual_crossing_is_guided_as_unmarked_and_not_listed():
    """수동 링크의 unmarked 는 정제 링크와 같은 키라서 안내 문구·횡단보도 목록이 그대로 따른다."""
    from route_service.engine import crosswalks as cwmod
    from route_service.engine.profiles import PROFILES
    from route_service.engine.steps import build_steps
    G = _graph()
    ml.apply_manual_links(G, [{"id": "U-04", "from": {"node": 2}, "to": {"node": 3},
                               "link_type": "crossing", "unmarked": True}])
    path = [1, 2, 3, 4]
    steps = build_steps(G, path, PROFILES["wheelchair_manual"])
    cross = [s for s in steps if s.get("link_type") == "crossing"]
    assert cross and "횡단보도 표시가 없으니" in cross[0]["instruction"]
    assert not [c for c in cwmod.for_path(G, path, steps) if c["role"] == "cross"]


def test_repo_links_myeonghak_exit_to_art_center_entrance():
    """레포 목록의 MH-* 는 명학역 1번 출구 승강기 → 안양아트센터 정문을 안양로 교차로 횡단보도로 잇는다.

    운영 그래프와 같은 ID·좌표의 노드 세 개(만안로·안양로112번길 교차점 457325894, 안양로111번길 북측 보도 끝
    TN0008712, 안양로111번길·문예로36번길 교차점 3832486056)만 둔 축소 그래프에 적용한다. 관리번호 1581(안양로112번길) → 1569(안양로, 21m) → 1570(안양로111번길)
    → 표시 없는 문예로36번길 입구 순서로 건너고, 교차점 도로 중심선 노드를 지나지 않는다.
    """
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "manual_links", "anyang.json")
    G = nx.Graph()
    G.add_node(457325894, lat=37.3853152, lon=126.9348889, node_type="unknown")
    G.add_node("TN0008712", lat=37.38438468, lon=126.93194379, node_type="sidewalk")
    G.add_node(3832486056, lat=37.3845648, lon=126.9323928, node_type="crossing")      # 안양로111번길·문예로36번길
    report = ml.apply_manual_links(G, ml.load_links(path))
    mh = {r["id"]: r for r in report if str(r["id"]).startswith("MH-")}
    assert len(mh) == 16 and all(r["status"] == "added" for r in mh.values())
    exit1, ent = mh["MH-01"]["u"], mh["MH-13"]["v"]
    nodes = nx.shortest_path(G, exit1, ent, weight="length")
    assert 457325894 not in nodes and 3832486056 not in nodes
    crossings = [G.edges[u, v] for u, v in zip(nodes, nodes[1:]) if G.edges[u, v]["link_type"] == "crossing"]
    # v1.39.0 — 출발점이 계단 출입구가 아니라 출구 승강기(만안로 서측)라 112번길 입구 횡단보도(1030)를 먼저 건넌다
    assert [c.get("cw_mgmt_no") for c in crossings] == ["2024121030", "2024121581", "2024121569", "2024121570", None]
    assert abs(G.nodes[exit1]["lat"] - 37.38507) < 1e-6 and abs(G.nodes[exit1]["lon"] - 126.93493) < 1e-6
    assert crossings[-1].get("unmarked") is True
    dist = nx.shortest_path_length(G, exit1, ent, weight="length")
    assert 330 <= dist <= 400
    # 정문 끝점은 실측 출입구 좌표(entrances.json 의 값)와 같은 자리다 — 실측 출입구는 노드에 스냅된다
    assert abs(G.nodes[ent]["lat"] - 37.384688) < 1e-6 and abs(G.nodes[ent]["lon"] - 126.931893) < 1e-6


def test_repo_links_gwanak_exit2_elevator_joins_plaza():
    """관악역 2번 출구 승강기(계단 출입구에서 남동쪽 약 20m)는 2번 출구 앞 광장·1253번길 갈림과 이어진다 (v1.39.0)."""
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "manual_links", "anyang.json")
    G = nx.Graph()
    G.add_node(3903318504, lat=37.4185572, lon=126.9090177, node_type="unknown")      # 역사 아래 만안로
    G.add_node(4696341341, lat=37.4189940, lon=126.9094900, node_type="crossing")     # 1253번길·1273번길 갈림
    report = ml.apply_manual_links(G, ml.load_links(path))
    by_id = {r["id"]: r for r in report}
    assert by_id["GW2-EV1"]["status"] == "added" and by_id["GW2-EV2"]["status"] == "added"
    ev = by_id["GW2-EV1"]["u"]
    assert abs(G.nodes[ev]["lat"] - 37.418838) < 1e-6
    assert set(G.neighbors(ev)) == {4696341341, by_id["GWANAK-02"]["u"]}   # 광장(계단 출입구 앞)과 같은 노드
