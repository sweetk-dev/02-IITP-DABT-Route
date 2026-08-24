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

    (poi_dir / "transit_stops.json").write_text(
        json.dumps(
            [
                {"poi_id": "B1", "name": "테스트정류장", "lat": 37.3901,
                 "lng": 126.9503, "mobile_no": "09999", "routes": ["2", "11"]},
                {"poi_id": "B2", "name": "중앙차로정류장", "lat": 37.3902,
                 "lng": 126.9504, "mobile_no": "09998", "center_yn": "Y", "routes": ["1"]},
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


def test_transit_station_keeps_boolean_accessible(client):
    """역은 승강설비로 판정 가능하므로 accessible 이 계속 bool 이어야 한다."""
    body = client.get(
        "/transit/access-points",
        params={"lat": 37.3900, "lng": 126.9500, "radius_m": 800},
    ).json()
    by_id = {i["poi_id"]: i for i in body["items"]}
    assert by_id["S1"]["accessible"] is True
    assert by_id["S1"]["accessible_status"] == "yes"
    assert isinstance(by_id["S2"]["accessible"], bool)


def test_transit_access_points_flag_lift_only_station(client):
    body = client.get(
        "/transit/access-points",
        params={"lat": 37.3900, "lng": 126.9500, "radius_m": 800,
                "profile": "wheelchair_manual"},
    ).json()
    by_id = {i["poi_id"]: i for i in body["items"]}
    assert by_id["S1"]["warnings"] == []            # 엘리베이터 보유
    assert by_id["S2"]["warnings"]                  # 리프트만 -> 경고


def test_transit_access_points_include_bus_stops(client):
    """버스 정류장도 접근점으로 반환되어야 한다(실증 버스 구간 안내의 전제)."""
    body = client.get(
        "/transit/access-points",
        params={"lat": 37.3900, "lng": 126.9500, "radius_m": 800,
                "profile": "wheelchair_manual"},
    ).json()
    by_id = {i["poi_id"]: i for i in body["items"]}
    assert {"S1", "S2", "B1", "B2"} <= set(by_id)
    stop = by_id["B1"]
    assert stop["type"] == "transit_stop"
    assert stop["mobile_no"] == "09999"
    assert [r["name"] for r in stop["routes"]] == ["11", "2"]
    assert all("station_seq" in r for r in stop["routes"])
    # 정류장은 저상버스 여부를 알 수 없으므로 접근 가능으로 단정하지 않는다.
    # None 은 미판정이며 "접근 불가"가 아니다 — 상태를 별도 필드로 명시한다.
    assert stop["accessible"] is None
    assert stop["accessible_status"] == "unknown"
    assert any("저상버스" in w for w in stop["warnings"])


def test_transit_access_points_warn_center_lane_stop(client):
    """중앙차로 정류소는 승차장까지 횡단이 선행되므로 별도 경고가 필요하다."""
    body = client.get(
        "/transit/access-points",
        params={"lat": 37.3900, "lng": 126.9500, "radius_m": 800},
    ).json()
    by_id = {i["poi_id"]: i for i in body["items"]}
    assert by_id["B2"]["center_yn"] is True
    assert any("횡단" in w for w in by_id["B2"]["warnings"])
    assert by_id["B1"]["center_yn"] is False
    assert not any("횡단" in w for w in by_id["B1"]["warnings"])


def test_transit_access_points_respect_radius(client):
    """반경 밖 정류장·역은 제외되어야 한다."""
    far = client.get(
        "/transit/access-points",
        params={"lat": 37.5000, "lng": 127.1000, "radius_m": 800},
    ).json()
    assert far["count"] == 0

    near = client.get(
        "/transit/access-points",
        params={"lat": 37.3900, "lng": 126.9500, "radius_m": 800},
    ).json()
    assert near["count"] == 4
    assert all(i["dist_m"] <= 800 for i in near["items"])


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


def test_missing_building_index_does_not_kill_service(tmp_path):
    """건물 폴리곤 로드 실패는 부가 기능 손실일 뿐, 서비스를 죽이면 안 된다."""
    from route_service.engine.access import BuildingIndex

    bad = tmp_path / "broken.pkl"
    bad.write_bytes(b"not a pickle")
    idx = BuildingIndex(str(bad))
    assert idx.loaded is False
    assert idx.containing(37.39, 126.95) is None
