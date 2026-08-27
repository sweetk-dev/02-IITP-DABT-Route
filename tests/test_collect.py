# -*- coding: utf-8 -*-
"""수집 장치화 (v1.16.0) — 트랙·제보·오버라이드.

자동화 수위 계약:
  1) 제보 접수 즉시 warning 오버라이드('미확인') — 안내 경고에 자동 노출
  2) 속성 변경(curb_cut 등)은 관리자 apply 로만 생성
  3) 오버라이드는 좌표 앵커 — 그래프 재적용(revert+apply)이 멱등
"""
from __future__ import annotations

import networkx as nx
import pytest

from route_service.collect.store import CollectStore
from route_service.engine.graph import NetworkStore
from route_service.engine.overrides import apply_overrides
from route_service.engine.planner import edge_passable, plan, NoRouteError
from route_service.engine.profiles import get_profile
from route_service.engine.steps import build_steps
from scripts.analyze_tracks import analyze_route


def _graph() -> nx.Graph:
    G = nx.Graph()
    G.add_node("N1", lat=37.3900, lon=126.9500, node_type="intersection")
    G.add_node("N2", lat=37.3900, lon=126.9511, node_type="intersection")
    G.add_node("N3", lat=37.3909, lon=126.9511, node_type="entrance")
    G.add_edge("N1", "N2", length=100.0, slope=1.0, link_type="sidewalk",
               width=2.0, curb_cut=True, surface=None, link_name="예시로", geometry=None)
    G.add_edge("N2", "N3", length=100.0, slope=1.5, link_type="sidewalk",
               width=2.0, curb_cut=True, surface=None, link_name="예시2로", geometry=None)
    return G


def _store(G):
    s = NetworkStore()
    s.load_graph_object(G, version="t", region="")
    return s


# ───────────── CollectStore (memory backend) ─────────────

def test_report_auto_creates_unconfirmed_warning_override():
    cs = CollectStore()
    out = cs.add_report(37.39, 126.9505, "curb", None, None, None, None)
    assert out["report_id"] == 1
    ovs = cs.active_overrides()
    assert len(ovs) == 1
    assert ovs[0]["attr"] == "warning"
    assert "미확인" in ovs[0]["value"]


def test_reject_retires_override():
    cs = CollectStore()
    rid = cs.add_report(37.39, 126.9505, "curb", None, None, None, None)["report_id"]
    cs.review_report(rid, "reject", note="현장 확인 결과 이상 없음")
    assert cs.active_overrides() == []
    assert cs.list_reports()[0]["status"] == "rejected"


def test_confirm_replaces_with_confirmed_warning():
    cs = CollectStore()
    rid = cs.add_report(37.39, 126.9505, "steep", None, None, None, None)["report_id"]
    cs.review_report(rid, "confirm")
    ovs = cs.active_overrides()
    assert len(ovs) == 1
    assert "미확인" not in ovs[0]["value"]


def test_apply_is_approval_only_and_creates_attr_override():
    cs = CollectStore()
    rid = cs.add_report(37.39, 126.9505, "curb", None, None, None, None)["report_id"]
    with pytest.raises(ValueError):
        cs.review_report(rid, "apply", attr="warning", value="x")   # 승인제 목록 밖
    cs.review_report(rid, "apply", attr="curb_cut", value="false")
    ovs = cs.active_overrides()
    assert len(ovs) == 1
    assert ovs[0]["attr"] == "curb_cut" and ovs[0]["value"] == "false"
    assert cs.list_reports()[0]["status"] == "applied"


def test_track_log_dedupes_by_seq():
    cs = CollectStore()
    pts = [{"seq": 0, "lat": 37.39, "lng": 126.95},
           {"seq": 1, "lat": 37.3901, "lng": 126.9501}]
    assert cs.log_track("r_x", pts, {"planned_dist_m": 200}) == 2
    assert cs.log_track("r_x", pts, None) == 2      # 재업로드 멱등


# ───────────── 오버라이드 그래프 적용 ─────────────

def test_warning_override_reaches_steps_and_summary():
    G = _graph()
    # N1-N2 링크 중간쯤 좌표
    ovs = [{"lat": 37.3900, "lon": 126.9505, "radius_m": 20,
            "attr": "warning", "value": "이용자 제보: 턱 있음 (미확인)"}]
    stat = apply_overrides(G, ovs)
    assert stat["warnings"] == 1
    store = _store(G)
    p = get_profile("wheelchair_manual")
    r = plan(store, "N1", "N3", p)["routes"][0]
    assert "이용자 제보: 턱 있음 (미확인)" in r["summary"]["warnings"]
    steps = build_steps(store.graph, r["path"], p)
    assert any("이용자 제보" in w for s in steps for w in s["warnings"])


def test_passable_false_blocks_routing():
    G = _graph()
    ovs = [{"lat": 37.3900, "lon": 126.9505, "radius_m": 20,
            "attr": "passable", "value": "false"}]
    apply_overrides(G, ovs)
    d = G["N1"]["N2"]
    assert d.get("blocked") is True
    assert edge_passable(d, get_profile("walk"), 20.0) is False
    with pytest.raises(NoRouteError):
        plan(_store(G), "N1", "N3", get_profile("walk"), relax=False)


def test_reapply_is_idempotent_and_revertible():
    G = _graph()
    ovs = [{"lat": 37.3900, "lon": 126.9505, "radius_m": 20,
            "attr": "curb_cut", "value": "false"},
           {"lat": 37.3900, "lon": 126.9505, "radius_m": 20,
            "attr": "warning", "value": "경고"}]
    apply_overrides(G, ovs)
    assert G["N1"]["N2"]["curb_cut"] is False
    apply_overrides(G, ovs)                        # 재적용해도 동일
    assert G["N1"]["N2"]["curb_cut"] is False
    assert G["N1"]["N2"]["report_warnings"] == ["경고"]
    stat = apply_overrides(G, [])                  # 전부 철회
    assert stat["applied"] == 0
    assert G["N1"]["N2"]["curb_cut"] is True       # 원복
    assert "report_warnings" not in G["N1"]["N2"]


def test_override_outside_radius_is_unmatched():
    G = _graph()
    ovs = [{"lat": 37.5000, "lon": 127.1000, "radius_m": 20,
            "attr": "warning", "value": "멀리"}]
    stat = apply_overrides(G, ovs)
    assert stat["unmatched"] == 1 and stat["applied"] == 0


# ───────────── 트랙 분석 ─────────────

def test_track_analysis_detects_honest_and_off_route():
    geom = [[37.3900, 126.9500], [37.3900, 126.9511], [37.3909, 126.9511]]
    on_route = [{"seq": i, "lat": 37.3900, "lng": 126.9500 + i * 0.0001}
                for i in range(12)]
    r = analyze_route("r_ok", geom, 200, on_route)
    assert r["off_route_clusters"] == []

    off = [{"seq": i, "lat": 37.3930, "lng": 126.9500 + i * 0.0001}   # 300m 북쪽
           for i in range(12)]
    r2 = analyze_route("r_off", geom, 200, off)
    assert len(r2["off_route_clusters"]) >= 1
    assert r2["off_route_clusters"][0]["max_off_m"] > 100


# ───────────── API 계약 (memory backend) ─────────────

def test_collect_api_end_to_end(tmp_path, monkeypatch):
    import json as _json
    import pickle
    from fastapi.testclient import TestClient

    net = tmp_path / "network.gpickle"
    with open(net, "wb") as f:
        pickle.dump(_graph(), f)
    monkeypatch.setenv("NETWORK_PATH", str(net))
    monkeypatch.setenv("NETWORK_VERSION", "test-collect")
    monkeypatch.setenv("POI_BACKEND", "none")
    monkeypatch.setenv("POI_DB_DSN", "")
    monkeypatch.setenv("ROUTE_API_TOKEN", "")

    import route_service.config as config
    config._settings = None
    import importlib
    import route_service.api.main as main
    importlib.reload(main)

    with TestClient(main.app) as client:
        # 제보 → 즉시 경고 반영
        res = client.post("/report/accessibility",
                          json={"lat": 37.3900, "lng": 126.9505, "reason": "curb"})
        assert res.status_code == 200
        rid = res.json()["report_id"]

        res = client.get("/report/accessibility", params={"status": "new"})
        assert res.json()["count"] == 1

        # 경로 요약에 경고 노출
        res = client.post("/route/plan", json={
            "origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511},
            "profile": "wheelchair_manual"})
        warns = res.json()["routes"][0]["summary"]["warnings"]
        assert any("이용자 제보" in w for w in warns)

        # 기각 → 경고 사라짐
        res = client.patch("/report/accessibility/%d" % rid,
                           json={"action": "reject", "note": "이상 없음"})
        assert res.status_code == 200
        res = client.post("/route/plan", json={
            "origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511},
            "profile": "wheelchair_manual"})
        warns = res.json()["routes"][0]["summary"]["warnings"]
        assert not any("이용자 제보" in w for w in warns)

        # 트랙 업로드
        res = client.post("/track/log", json={
            "route_id": "r_demo", "points": [
                {"seq": 0, "lat": 37.39, "lng": 126.95},
                {"seq": 1, "lat": 37.3901, "lng": 126.9502}],
            "meta": {"planned_dist_m": 200,
                     "geometry": [[37.39, 126.95], [37.3909, 126.9511]],
                     "outcome": "arrived"}})
        assert res.status_code == 200
        assert res.json()["stored_points"] == 2
