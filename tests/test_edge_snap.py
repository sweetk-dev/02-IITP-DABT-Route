# -*- coding: utf-8 -*-
"""링크 투영 스냅 (#67, v1.23.0).

  1) 긴 링크 중간의 좌표는 링크 위 점(가상 노드)에 붙고, 경로가 링크 끝으로 되돌아가지 않는다
  2) 안전 필터 — 보행 링크가 가까이 있으면 도로 링크 제외 / crossing·steps 는 투영 금지 / 건물을 가로지르는 투영 탈락
  3) 링크 끝 1m 이내면 가상 노드 대신 끝 노드
  4) API — 출발지가 링크 중간이면 첫 안내가 목적지 방향(우회비 ≈ 1), 실측 출입구 도착지는 노드 스냅 유지
"""
import json
import os
import sys

import networkx as nx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from route_service.engine import vsnap  # noqa: E402
from route_service.engine.graph import NetworkStore  # noqa: E402
from route_service.engine import profiles as prof  # noqa: E402

WM = prof.PROFILES["wheelchair_manual"]


def _edge(**kw):
    d = dict(length=100.0, slope=1.0, link_type="sidewalk", width=2.0, curb_cut=True, surface="asphalt",
             link_name=None, geometry=None)
    d.update(kw)
    return d


def long_graph():
    """A ──(sidewalk 200m, 동서)── B ──(sidewalk 100m, 북)── C(목적지)
       A 의 서쪽 끝. 출발지는 A–B 의 3/4 지점 남쪽 5m — 노드 스냅이면 B(50m) 가 아니라 ... 아니, 노드 스냅은 B.
       그러나 목적지가 A 쪽에 있는 경우를 만들기 위해 D 를 A 북쪽에 둔다."""
    G = nx.Graph()
    G.add_node("A", lat=37.3900, lon=126.9500, node_type="intersection")
    G.add_node("B", lat=37.3900, lon=126.9523, node_type="intersection")   # 약 203m 동쪽
    G.add_node("D", lat=37.3909, lon=126.9500, node_type="entrance")       # A 북쪽 100m (목적지)
    G.add_edge("A", "B", **_edge(length=203.0))
    G.add_edge("A", "D", **_edge(length=100.0))
    return G


def _store(G):
    s = NetworkStore()
    s.load_graph_object(G, version="t", region="t")
    return s


def test_projects_to_link_and_route_does_not_backtrack():
    st = _store(long_graph())
    # 출발지: A–B 링크의 3/4 지점(B 에서 50m) 남쪽 4m
    lat, lng = 37.3900 - 0.00004, 126.9500 + 0.0023 * 0.75
    cands = vsnap.candidates(st, lat, lng, WM, WM.hard_slope(), allowed=set(st.graph.nodes))
    assert cands and cands[0]["u"] in ("A", "B") and 0.7 < cands[0]["t"] < 0.8 or 0.2 < cands[0]["t"] < 0.3
    assert cands[0]["dist_m"] < 6
    H = vsnap.virtual_graph(st)
    vid = vsnap.attach(H, cands[0], "V_o_0")
    assert vid.startswith("V_") and H.nodes[vid]["virtual"]
    assert st.graph.number_of_nodes() == 3, "공유 그래프는 그대로여야 한다"
    # 가상 노드에서 D 까지: V→A→D = 152m + 100m. 노드 스냅(B)이면 B→A→D = 303m
    from route_service.engine.planner import plan
    r = plan(st, vid, "D", WM, 1, graph=H)
    assert 240 < r["routes"][0]["summary"]["total_distance_m"] < 265


def test_layered_snap_prefers_sidewalk_and_skips_crossing_steps():
    G = long_graph()
    # A–B 와 나란한 도로 링크(남쪽 6m)와 계단·횡단보도 링크를 더한다
    G.add_node("R1", lat=37.38995, lon=126.9500); G.add_node("R2", lat=37.38995, lon=126.9523)
    G.add_edge("R1", "R2", **_edge(link_type="road", length=203.0))
    G.add_node("S1", lat=37.3901, lon=126.9510); G.add_node("S2", lat=37.3903, lon=126.9510)
    G.add_edge("S1", "S2", **_edge(link_type="steps", length=22.0))
    G.add_node("X1", lat=37.3899, lon=126.9512); G.add_node("X2", lat=37.3901, lon=126.9512)
    G.add_edge("X1", "X2", **_edge(link_type="crossing", length=22.0))
    st = _store(G)
    lat, lng = 37.38997, 126.9511          # 도로(3m)가 보도(3m)만큼 가깝고 계단·횡단보도가 바로 옆
    cands = vsnap.candidates(st, lat, lng, WM, WM.hard_slope(), allowed=set(G.nodes), k=5)
    types = [c["link_type"] for c in cands]
    assert "steps" not in types and "crossing" not in types
    assert "road" not in types, "보행 링크가 20m 안에 있으면 도로 링크는 후보에서 빠진다"
    assert types[0] == "sidewalk"
    # 보행 링크가 없는 곳에서는 도로 링크에 붙는다
    G2 = nx.Graph(); G2.add_node("R1", lat=37.38995, lon=126.9500); G2.add_node("R2", lat=37.38995, lon=126.9523)
    G2.add_edge("R1", "R2", **_edge(link_type="road", length=203.0))
    st2 = _store(G2)
    assert vsnap.candidates(st2, lat, lng, WM, WM.hard_slope(), allowed=set(G2.nodes))[0]["link_type"] == "road"


def test_line_of_sight_blocks_projection_through_building():
    from shapely.geometry import Polygon
    G = long_graph(); st = _store(G)

    class B:
        loaded = True
        polys = [(Polygon([(126.9508, 37.38985), (126.9514, 37.38985), (126.9514, 37.38999), (126.9508, 37.38999)]), "건물")]
    los = vsnap.LineOfSight(buildings=B())
    lat, lng = 37.3898, 126.9511            # 건물 남쪽 — 보도(북쪽)까지 선분이 건물을 가로지른다
    assert los.blocked(lat, lng, 37.3900, 126.9511)
    assert vsnap.candidates(st, lat, lng, WM, WM.hard_slope(), allowed=set(G.nodes), los=los) == []
    # 건물 안 좌표(대표점)는 경계를 한 번 나가는 것이라 허용
    assert not los.blocked(37.3899, 126.9511, 37.3900, 126.9511)


def test_attach_near_link_end_returns_end_node():
    st = _store(long_graph()); H = vsnap.virtual_graph(st)
    c = vsnap.candidates(st, 37.3900, 126.95001, WM, WM.hard_slope(), allowed=set(st.graph.nodes))[0]
    assert vsnap.attach(H, c, "V_x") == "A"
    assert H.number_of_nodes() == 3


# ── API ──
@pytest.fixture()
def client(tmp_path, monkeypatch):
    import pickle
    net = tmp_path / "network.gpickle"
    with open(net, "wb") as f:
        pickle.dump(long_graph(), f)
    poi_dir = tmp_path / "poi"; poi_dir.mkdir()
    (poi_dir / "tour_bf.json").write_text(json.dumps([{
        "poi_id": "TBF-D", "name": "목적지", "addr": "안양", "latitude": 37.3909, "longitude": 126.9500,
        "entrance": {"lat": 37.3909, "lng": 126.9500},
    }], ensure_ascii=False), encoding="utf-8")
    (poi_dir / "entrances.json").write_text(json.dumps(
        {"TBF-D": {"lat": 37.3909, "lng": 126.9500, "note": "정문"}}), encoding="utf-8")
    monkeypatch.setenv("NETWORK_PATH", str(net)); monkeypatch.setenv("NETWORK_VERSION", "test-1")
    monkeypatch.setenv("POI_BACKEND", "file"); monkeypatch.setenv("POI_DATA_DIR", str(poi_dir))
    monkeypatch.setenv("ENTRANCES_PATH", str(poi_dir / "entrances.json"))
    monkeypatch.setenv("ROUTE_API_TOKEN", "")
    import importlib
    import route_service.config as cfg
    importlib.reload(cfg); cfg._settings = None
    import route_service.api.main as m
    importlib.reload(m)
    with TestClient(m.app) as c:
        yield c


def test_api_origin_mid_link_heads_toward_destination(client):
    body = {"origin": {"lat": 37.3900 - 0.00004, "lng": 126.9500 + 0.0023 * 0.75},
            "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9500}, "profile": "wheelchair_manual"}
    r = client.post("/route/plan", json=body); assert r.status_code == 200, r.text
    d = r.json()
    assert d["origin"]["snap_kind"] == "edge" and d["origin"]["snap_dist_m"] < 6
    dist = d["routes"][0]["summary"]["total_distance_m"]
    assert dist < 270, "링크 중간 출발이 B 까지 갔다 되돌아오면 300m 를 넘는다: %s" % dist
    first = d["routes"][0]["steps"][0]
    assert first["maneuver"] == "depart" and first["distance_m"] < 160
    # 도착지(coord)도 링크 투영 — 노드 D 에 0m 이므로 노드 자체
    assert d["destination"]["snap_dist_m"] < 1


def test_api_entrance_destination_keeps_node_snap(client):
    body = {"origin": {"lat": 37.3900 - 0.00004, "lng": 126.9500 + 0.0023 * 0.75},
            "destination": {"type": "tour", "poi_id": "TBF-D"}, "profile": "wheelchair_manual"}
    r = client.post("/route/plan", json=body); assert r.status_code == 200, r.text
    d = r.json()
    assert d["destination"]["resolved_by"] == "manual_survey"
    assert d["destination"]["snap_kind"] == "node"


def test_api_edge_snap_can_be_disabled(client, monkeypatch):
    import route_service.api.main as m
    monkeypatch.setattr(m.settings, "edge_snap", False)
    body = {"origin": {"lat": 37.3900 - 0.00004, "lng": 126.9500 + 0.0023 * 0.75},
            "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9500}, "profile": "wheelchair_manual"}
    d = client.post("/route/plan", json=body).json()
    assert d["origin"]["snap_kind"] == "node"
    assert d["routes"][0]["summary"]["total_distance_m"] > 290
