# -*- coding: utf-8 -*-
"""계측 로그 (#73, v1.25.0) — 실증 정량지표 ②(응답시간)·③(추천 MAP) 원천.

  1) 미들웨어: 모든 응답에 X-Process-Time-Ms, request 행에 핸들러 태그(profile·mode·route_id) 합류
  2) /route/reroute → reroute 행(이전→신규 route_id·이탈 거리·사유)
  3) /tour/recommend → recommend 행(조건 + 순위·점수 스냅샷)
  4) JSONL 파일 append, /meta/latency 요약, 파일을 못 열어도 서비스 계속
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from route_service import metrics as mt  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import importlib
    import pickle
    from fastapi.testclient import TestClient
    from conftest import make_graph

    net = tmp_path / "network.gpickle"
    with open(net, "wb") as f:
        pickle.dump(make_graph(), f)
    poi_dir = tmp_path / "poi"
    poi_dir.mkdir()
    (poi_dir / "tour_bf.json").write_text(json.dumps([{
        "poi_id": "TBF-1", "name": "테스트 무장애 공원",
        "addr": "경기도 안양시 만안구 테스트로 1",
        "latitude": 37.3909, "longitude": 126.9511,
        "dis_toilet_yn": "Y", "elevator_yn": "Y", "slope_yn": "Y", "dis_parking_yn": "Y",
    }], ensure_ascii=False), encoding="utf-8")
    log = tmp_path / "metrics" / "events.jsonl"

    monkeypatch.setenv("NETWORK_PATH", str(net))
    monkeypatch.setenv("NETWORK_VERSION", "test-1")
    monkeypatch.setenv("POI_BACKEND", "file")
    monkeypatch.setenv("POI_DATA_DIR", str(poi_dir))
    monkeypatch.setenv("ROUTE_API_TOKEN", "")
    monkeypatch.setenv("METRICS_LOG_PATH", str(log))

    import route_service.config as cfg
    importlib.reload(cfg)
    cfg._settings = None
    import route_service.api.main as m
    importlib.reload(m)
    with TestClient(m.app) as c:
        c.log_path = log
        yield c


def _rows(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def test_header_and_request_row_with_tags(client):
    r = client.post("/route/plan", json={
        "origin": {"lat": 37.3900, "lng": 126.9500},
        "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511},
        "profile": "wheelchair_electric",
    })
    assert r.status_code == 200
    assert float(r.headers["X-Process-Time-Ms"]) >= 0
    rows = [x for x in _rows(client.log_path) if x["kind"] == "request" and x["path"] == "/route/plan"]
    assert rows, "request 행이 없다"
    row = rows[-1]
    assert row["status"] == 200 and row["ms"] >= 0
    assert row["profile"] == "wheelchair_electric" and row["mode"] == "walk"
    assert row["route_id"] == r.json()["route_id"]
    assert row["total_m"] == r.json()["routes"][0]["summary"]["total_distance_m"]


def test_tags_do_not_leak_between_requests(client):
    client.post("/route/plan", json={
        "origin": {"lat": 37.3900, "lng": 126.9500},
        "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511},
        "profile": "wheelchair_manual",
    })
    client.get("/health")
    rows = _rows(client.log_path)
    health = [x for x in rows if x.get("path") == "/health"][-1]
    assert "profile" not in health and "route_id" not in health


def test_reroute_event(client):
    first = client.post("/route/plan", json={
        "origin": {"lat": 37.3900, "lng": 126.9500},
        "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511},
    }).json()
    r = client.post("/route/reroute", json={
        "current": {"lat": 37.3905, "lng": 126.9500},
        "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511},
        "route_id": first["route_id"], "reason": "off_route",
    })
    assert r.status_code == 200
    ev = [x for x in _rows(client.log_path) if x["kind"] == "reroute"]
    assert len(ev) == 1
    assert ev[0]["prev_route_id"] == first["route_id"]
    assert ev[0]["route_id"] == r.json()["route_id"]
    assert ev[0]["reason"] == "off_route"
    assert "off_route_dist_m" in ev[0]


def test_recommend_snapshot(client):
    r = client.post("/tour/recommend", json={
        "disabilities": ["지체장애"], "sigungu": "안양", "topk": 5,
        "origin_lat": 37.3900, "origin_lng": 126.9500,
    })
    assert r.status_code == 200
    ev = [x for x in _rows(client.log_path) if x["kind"] == "recommend"]
    assert len(ev) == 1
    snap = ev[0]
    assert snap["sigungu"] == "안양" and snap["topk"] == 5 and snap["total"] == 1
    assert snap["items"][0]["poi_id"] == "TBF-1" and snap["items"][0]["score"] > 0
    assert snap["origin"] == [37.39, 126.95]


def test_latency_summary_endpoint(client):
    for _ in range(3):
        client.get("/health")
    body = client.get("/meta/latency").json()
    assert "/health" in body["paths"]
    h = body["paths"]["/health"]
    assert h["count"] >= 3 and h["p95_ms"] >= h["p50_ms"] >= 0 and h["over_3s"] == 0
    assert body["file"] == str(client.log_path)
    # 요약 조회 자체는 기록하지 않는다
    assert not any(x.get("path") == "/meta/latency" for x in _rows(client.log_path))


def test_unwritable_log_path_does_not_break_service(tmp_path):
    bad = tmp_path / "file_not_dir"
    bad.write_text("x")
    m = mt.Metrics(path=str(bad / "events.jsonl"))
    m.write("request", path="/x", ms=1.0)
    assert m.path == "" and len(m.recent) == 1
    assert m.summary()["paths"]["/x"]["count"] == 1


def test_disabled_metrics_records_nothing():
    m = mt.Metrics(path="", enabled=False)
    m.write("request", path="/x", ms=1.0)
    assert len(m.recent) == 0
