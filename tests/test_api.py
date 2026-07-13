# -*- coding: utf-8 -*-
import json
import os

import pytest
from fastapi.testclient import TestClient

from conftest import make_graph


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import pickle

    # 합성 그래프를 파일로 저장하고 서비스가 그것을 로드하게 한다
    net = tmp_path / "network.gpickle"
    with open(net, "wb") as f:
        pickle.dump(make_graph(), f)

    poi_dir = tmp_path / "poi"
    poi_dir.mkdir()
    (poi_dir / "tour_bf.json").write_text(
        json.dumps(
            [
                {
                    "poi_id": "TBF-1",
                    "name": "테스트 무장애 공원",
                    "addr": "경기도 안양시 만안구 테스트로 1",
                    "latitude": 37.3909,
                    "longitude": 126.9511,
                    "dis_toilet_yn": "Y",
                    "elevator_yn": "Y",
                    "slope_yn": "Y",
                    "dis_parking_yn": "Y",
                    "wheelchair_rent_yn": "N",
                    "tactile_map_yn": "N",
                    "audio_guide_yn": "N",
                    "entrance": {"lat": 37.3909, "lng": 126.9511},
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (poi_dir / "stations.json").write_text(
        json.dumps(
            [
                {"poi_id": "S1", "name": "테스트역", "latitude": 37.3902,
                 "longitude": 126.9505, "elevator_cnt": 1, "wheelchair_lift_cnt": 0},
                {"poi_id": "S2", "name": "리프트역", "latitude": 37.3903,
                 "longitude": 126.9506, "elevator_cnt": 0, "wheelchair_lift_cnt": 1},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("NETWORK_PATH", str(net))
    monkeypatch.setenv("NETWORK_VERSION", "test-1")
    monkeypatch.setenv("POI_BACKEND", "file")
    monkeypatch.setenv("POI_DATA_DIR", str(poi_dir))
    monkeypatch.setenv("ROUTE_API_TOKEN", "")

    import route_service.config as cfg

    cfg._settings = None
    import importlib

    import route_service.api.main as m

    importlib.reload(m)
    with TestClient(m.app) as c:
        yield c


def test_health_and_meta(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["graph_loaded"] is True

    meta = client.get("/meta/network").json()
    assert meta["node_cnt"] == 4 and meta["edge_cnt"] == 4
    assert meta["link_type_available"] is True


def test_profiles_endpoint(client):
    body = client.get("/profiles").json()
    assert body["default"] == "wheelchair_manual"
    assert len(body["profiles"]) >= 5


def test_plan_to_tour_poi(client):
    r = client.post(
        "/route/plan",
        json={
            "origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "tour", "poi_id": "TBF-1"},
            "profile": "wheelchair_manual",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["destination"]["resolved_by"] == "accessible_entrance"
    route = body["routes"][0]
    assert route["summary"]["stairs_cnt"] == 0       # 계단 회피
    assert route["steps"][0]["maneuver"] == "depart"
    assert route["steps"][-1]["maneuver"] == "arrive"
    assert len(route["geometry"]) >= 2


def test_plan_unknown_profile_400(client):
    r = client.post(
        "/route/plan",
        json={
            "origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511},
            "profile": "unknown_profile",
        },
    )
    assert r.status_code == 400


def test_plan_missing_poi_404(client):
    r = client.post(
        "/route/plan",
        json={
            "origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "tour", "poi_id": "NOPE"},
        },
    )
    assert r.status_code == 404


def test_snap_endpoint(client):
    r = client.post("/route/snap", json={"lat": 37.39005, "lng": 126.95005})
    assert r.status_code == 200 and r.json()["node_id"] == "N1"


def test_reroute_reports_off_route(client):
    first = client.post(
        "/route/plan",
        json={
            "origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511},
        },
    ).json()
    r = client.post(
        "/route/reroute",
        json={
            "current": {"lat": 37.3905, "lng": 126.9500},
            "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511},
            "route_id": first["route_id"],
        },
    )
    assert r.status_code == 200
    assert "off_route" in r.json()


def test_tour_list_and_recommend(client):
    items = client.get("/tour/bf-spots", params={"sigungu": "안양"}).json()
    assert items["count"] == 1

    rec = client.post(
        "/tour/recommend", json={"disabilities": ["지체장애"], "sigungu": "안양", "topk": 5}
    ).json()
    assert rec["count"] == 1
    assert rec["items"][0]["score"] > 0


def test_transit_access_points_flag_lift_only_station(client):
    body = client.get(
        "/transit/access-points",
        params={"lat": 37.3900, "lng": 126.9500, "radius_m": 800,
                "profile": "wheelchair_manual"},
    ).json()
    by_id = {i["poi_id"]: i for i in body["items"]}
    assert by_id["S1"]["warnings"] == []            # 엘리베이터 보유
    assert by_id["S2"]["warnings"]                  # 리프트만 -> 경고


def test_entrance_endpoint_reports_source(client):
    """목적지 좌표를 무엇으로 정했는지 반드시 알려야 한다(건물 중심 안내 방지)."""
    r = client.get("/tour/bf-spots/TBF-1/entrance")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "accessible_entrance"
    assert body["note"]


def test_plan_reports_destination_note(client):
    r = client.post(
        "/route/plan",
        json={
            "origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "tour", "poi_id": "TBF-1"},
        },
    )
    d = r.json()["destination"]
    assert d["resolved_by"] in ("manual_survey", "accessible_entrance",
                                "building_access", "facility_centroid")
    assert d["note"]
