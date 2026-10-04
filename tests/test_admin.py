# -*- coding: utf-8 -*-
"""관리 엔드포인트(/admin/*) — 인증, 그래프 파일 경로 제한, 로드 실패 시 종전 그래프 유지."""
import importlib
import os
import pickle

import pytest
from fastapi.testclient import TestClient

from conftest import make_graph

TOKEN = "test-admin-token"
AUTH = {"Authorization": "Bearer %s" % TOKEN}


def _client(tmp_path, monkeypatch, token):
    net_dir = tmp_path / "net"
    net_dir.mkdir()
    net = net_dir / "network.gpickle"
    with open(net, "wb") as f:
        pickle.dump(make_graph(), f)
    monkeypatch.setenv("NETWORK_PATH", str(net))
    monkeypatch.setenv("NETWORK_VERSION", "test-1")
    monkeypatch.setenv("POI_BACKEND", "none")
    monkeypatch.setenv("POI_DB_DSN", "")
    monkeypatch.setenv("ROUTE_API_TOKEN", token)
    import route_service.config as cfg
    importlib.reload(cfg)
    cfg._settings = None
    import route_service.api.main as m
    importlib.reload(m)
    return m, net_dir


@pytest.fixture()
def admin(tmp_path, monkeypatch):
    m, net_dir = _client(tmp_path, monkeypatch, TOKEN)
    with TestClient(m.app) as c:
        c.main, c.net_dir, c.tmp = m, net_dir, tmp_path
        yield c


def test_admin_requires_matching_token(admin):
    assert admin.post("/admin/reload-network").status_code == 401
    assert admin.post("/admin/reload-overrides", headers={"Authorization": "Bearer x"}).status_code == 401
    r = admin.post("/admin/reload-network", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["node_cnt"] == 4 and "overrides" in r.json()
    assert admin.post("/admin/reload-overrides", headers=AUTH).status_code == 200


def test_reload_network_accepts_file_under_network_dir(admin):
    G = make_graph()
    G.add_node("N9", lat=37.3912, lon=126.9511, node_type="intersection")
    sub = admin.net_dir / "next"
    sub.mkdir()
    with open(sub / "v2.gpickle", "wb") as f:
        pickle.dump(G, f)
    r = admin.post("/admin/reload-network", params={"path": str(sub / "v2.gpickle"), "version": "v2"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["node_cnt"] == 5 and r.json()["network_version"] == "v2"


def test_reload_network_rejects_path_outside_network_dir(admin, monkeypatch):
    """범위 밖 경로는 열어 보지도 않는다 — `..` 와 심볼릭 링크도 실경로로 풀어 검사한다."""
    outside = admin.tmp / "evil.gpickle"
    with open(outside, "wb") as f:
        pickle.dump(make_graph(), f)
    opened = []
    real_load = admin.main.NET.load
    monkeypatch.setattr(admin.main.NET, "load", lambda *a, **k: opened.append(a) or real_load(*a, **k))
    link = admin.net_dir / "link.gpickle"
    os.symlink(outside, link)
    for p in (str(outside), str(admin.net_dir / ".." / "evil.gpickle"), str(link), "/etc/passwd",
              str(admin.net_dir)):
        r = admin.post("/admin/reload-network", params={"path": p}, headers=AUTH)
        assert r.status_code == 400, (p, r.status_code, r.text)
    assert opened == []
    assert admin.main.NET.meta["node_cnt"] == 4
    assert admin.post("/admin/reload-network", params={"path": str(admin.net_dir / "none.gpickle")},
                      headers=AUTH).status_code == 404


def test_reload_network_keeps_current_graph_when_file_is_broken(admin):
    bad = admin.net_dir / "broken.gpickle"
    bad.write_bytes(b"not a pickle")
    not_graph = admin.net_dir / "list.gpickle"
    with open(not_graph, "wb") as f:
        pickle.dump([1, 2, 3], f)
    import networkx as nx
    no_coord = nx.Graph()
    no_coord.add_node("X")                          # 좌표 없는 노드 — 색인을 만들다 실패한다
    no_coord.add_node("Y", lat=37.39, lon=126.95)
    with open(admin.net_dir / "nocoord.gpickle", "wb") as f:
        pickle.dump(no_coord, f)
    before = admin.main.NET.graph
    for name in ("broken.gpickle", "list.gpickle", "nocoord.gpickle"):
        r = admin.post("/admin/reload-network", params={"path": str(admin.net_dir / name)}, headers=AUTH)
        assert r.status_code == 422, (name, r.status_code, r.text)
        assert admin.main.NET.graph is before, "실패한 교체가 종전 그래프를 건드렸다: %s" % name
        assert admin.main.NET.meta["node_cnt"] == 4
        assert len(admin.main.NET.node_index[0]) == 4
    # 종전 그래프로 경로 탐색이 계속된다
    r = admin.post("/route/plan", headers=AUTH, json={
        "origin": {"lat": 37.3900, "lng": 126.9500},
        "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511}, "profile": "wheelchair_manual"})
    assert r.status_code == 200, r.text
