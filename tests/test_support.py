# -*- coding: utf-8 -*-
"""긴급대응 지원시설·화장실 근접 조회 (#75, v1.26.0).

  1) /support/nearby — 유형 필터·반경·유형별 limit·거리순·운영시간 미상 표시
  2) /toilet/nearby — 장애인 화장실 보유분만(기본), 거리순
  3) 전동 프로필 경로 응답의 support_hint(1km 회랑 충전기 수·최근접), 수동 프로필은 없음
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


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
    (poi_dir / "tour_bf.json").write_text("[]", encoding="utf-8")
    (poi_dir / "emergency_support.json").write_text(json.dumps([
        {"support_id": 1, "support_type": "charge", "name": "안양시청", "addr_road": "안양시 동안구 시민대로 235",
         "install_desc": "본관 1층 로비", "latitude": 37.3903, "longitude": 126.9504, "tel": "031-000-0000",
         "open_hours": "09:00~18:00", "source": "STD_WCHAIR_CHARGER", "confidence": "H"},
        {"support_id": 2, "support_type": "charge", "name": "먼 충전기", "addr_road": "x",
         "latitude": 37.4300, "longitude": 126.9504, "open_hours": "24시간", "source": "STD_WCHAIR_CHARGER"},
        {"support_id": 3, "support_type": "repair", "name": "테스트 수리센터", "addr_road": "안양시 만안구 1",
         "latitude": 37.3906, "longitude": 126.9508, "tel": "031-111-1111", "open_hours": "",
         "source": "GG_ASSIST_REPAIR", "confidence": "H", "note": "좌표 의심 — 동일 좌표 3개소"},
        {"support_id": 4, "support_type": "repair", "name": "가까운 수리", "addr_road": "y",
         "latitude": 37.3901, "longitude": 126.9501, "source": "NHIS_ASSIST_STORE", "confidence": "L"},
        {"support_id": 5, "support_type": "calltaxi", "name": "경기도 광역이동지원센터", "addr_road": "z",
         "latitude": 37.3902, "longitude": 126.9503, "tel": "1666-0420", "open_hours": "24시간", "source": "MANUAL"},
        {"support_id": 6, "support_type": "charge", "name": "좌표 없음", "latitude": None, "longitude": None},
    ], ensure_ascii=False), encoding="utf-8")
    (poi_dir / "public_toilets.json").write_text(json.dumps([
        {"toilet_id": 1, "toilet_name": "시청 화장실", "toilet_type": "공중화장실", "addr_road": "a",
         "m_dis_toilet_count": 1, "f_dis_toilet_count": 1, "unisex_yn": "N", "open_time": "24시간",
         "emg_bell_yn": "Y", "latitude": 37.3904, "longitude": 126.9505},
        {"toilet_id": 2, "toilet_name": "일반 화장실", "toilet_type": "공중화장실", "addr_road": "b",
         "m_dis_toilet_count": 0, "f_dis_toilet_count": 0, "latitude": 37.3901, "longitude": 126.9502},
        {"toilet_id": 3, "toilet_name": "먼 장애인 화장실", "toilet_type": "개방화장실", "addr_road": "c",
         "m_dis_toilet_count": 0, "m_dis_urinal_count": 1, "f_dis_toilet_count": 0,
         "latitude": 37.4100, "longitude": 126.9502},
    ], ensure_ascii=False), encoding="utf-8")

    monkeypatch.setenv("NETWORK_PATH", str(net))
    monkeypatch.setenv("NETWORK_VERSION", "test-1")
    monkeypatch.setenv("POI_BACKEND", "file")
    monkeypatch.setenv("POI_DATA_DIR", str(poi_dir))
    monkeypatch.setenv("ROUTE_API_TOKEN", "")
    monkeypatch.setenv("METRICS_LOG_PATH", "")

    import route_service.config as cfg
    importlib.reload(cfg)
    cfg._settings = None
    import route_service.api.main as m
    importlib.reload(m)
    with TestClient(m.app) as c:
        yield c


def test_support_nearby_all_types_sorted(client):
    r = client.get("/support/nearby", params={"lat": 37.3900, "lng": 126.9500, "radius_m": 2000})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["count"] == 4 and b["count_by_type"] == {"charge": 1, "repair": 2, "calltaxi": 1}
    names = [i["name"] for i in b["items"]]
    assert names[0] == "가까운 수리", names                   # 거리순
    assert "먼 충전기" not in names and "좌표 없음" not in names
    ch = [i for i in b["items"] if i["support_type"] == "charge"][0]
    assert ch["install_desc"] == "본관 1층 로비" and ch["open_hours_status"] == "known"
    assert ch["source_label"].startswith("행정안전부") and ch["type_label"] == "전동보장구 충전기"
    rp = [i for i in b["items"] if i["name"] == "테스트 수리센터"][0]
    assert rp["open_hours"] is None and rp["open_hours_status"] == "unknown"
    assert rp["coord_suspect"] is True
    tx = [i for i in b["items"] if i["support_type"] == "calltaxi"][0]
    assert tx["tel"] == "1666-0420"


def test_support_nearby_type_filter_and_limit(client):
    r = client.get("/support/nearby", params={"lat": 37.3900, "lng": 126.9500, "types": "repair", "limit": 1})
    b = r.json()
    assert b["count"] == 1 and b["items"][0]["support_type"] == "repair"
    r = client.get("/support/nearby", params={"lat": 37.3900, "lng": 126.9500, "types": "bus"})
    assert r.status_code == 400


def test_support_nearby_radius(client):
    r = client.get("/support/nearby", params={"lat": 37.3900, "lng": 126.9500, "radius_m": 60})
    names = [i["name"] for i in r.json()["items"]]
    assert "가까운 수리" in names and "테스트 수리센터" not in names


def test_toilet_nearby_accessible_only(client):
    r = client.get("/toilet/nearby", params={"lat": 37.3900, "lng": 126.9500})
    b = r.json()
    assert b["count"] == 1 and b["items"][0]["name"] == "시청 화장실"
    it = b["items"][0]
    assert it["accessible"] is True and it["dis_male_cnt"] == 1 and it["emg_bell"] is True
    r = client.get("/toilet/nearby", params={"lat": 37.3900, "lng": 126.9500, "accessible_only": "false"})
    assert r.json()["count"] == 2
    assert r.json()["items"][0]["name"] == "일반 화장실"          # 거리순


def test_plan_support_hint_electric_only(client):
    body = {"origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511}}
    r = client.post("/route/plan", json=dict(body, profile="wheelchair_electric"))
    assert r.status_code == 200, r.text
    h = r.json()["support_hint"]
    assert h["charge_cnt"] == 1 and h["nearest"]["name"] == "안양시청"
    assert h["nearest"]["dist_to_route_m"] <= 1000
    r = client.post("/route/plan", json=dict(body, profile="wheelchair_manual"))
    assert r.json()["support_hint"] is None
    # 기본 프로필(전동)로도 붙는다
    r = client.post("/route/plan", json=body)
    assert r.json()["support_hint"]["charge_cnt"] == 1
