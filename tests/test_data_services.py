# -*- coding: utf-8 -*-
"""음식점·건물 편의시설·공공건물 화장실·명부 조회 (v1.31.0).

  1) /food/nearby — 휠체어 출입 확인(yes) 먼저, 같은 상태 안에서 거리순. 정보 없음(unknown)도 남긴다.
     실태조사 음식점 건물은 관광 음식점과 겹치면 뺀다. 업종명뿐인 이름은 뺀다
  2) /facility/accessibility — 이름·옛 이름·근처 검색, 칸이 빈 항목은 원문으로 보완(basis=text),
     주거 건물 제외
  3) /toilet/nearby — 공공건물의 장애인 화장실을 합친다(건물 운영시간 표시). 학교·업무시설은 넣지 않는다
  4) /directory/providers · /directory/workplaces — 서비스·구·업종 필터, 같은 기관 묶기
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = (37.3900, 126.9500)


def _facl(fid, name, ftype, lat, lng, **kw):
    row = {"facl_id": fid, "facl_name": name, "facl_type": ftype, "addr": "경기도 안양시 동안구 %d" % fid,
           "latitude": lat, "longitude": lng, "entrance_ramp_yn": None, "entrance_door_yn": None,
           "approach_road_yn": None, "elevator_yn": None, "dis_toilet_yn": None, "dis_parking_yn": None,
           "guide_facility_yn": None, "eval_info_raw": "", "base_dt": "2026-09-22"}
    row.update(kw)
    return row


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import importlib
    import pickle
    from fastapi.testclient import TestClient
    from conftest import make_graph

    net = tmp_path / "network.gpickle"
    with open(net, "wb") as f:
        pickle.dump(make_graph(), f)
    d = tmp_path / "poi"
    d.mkdir()

    def w(name, rows):
        (d / name).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    w("tour_bf.json", [
        {"poi_id": "F1", "name": "경사로식당", "addr": "경기도 안양시 동안구 1", "lat": 37.3930, "lng": 126.9500,
         "category": "food", "category_detail": "한식", "slope_yn": "Y", "toilet_yn": "Y"},
        {"poi_id": "F2", "name": "가까운식당", "addr": "경기도 안양시 동안구 2", "lat": 37.3902, "lng": 126.9500,
         "category": "food", "category_detail": "카페"},
        {"poi_id": "F3", "name": "먼식당", "addr": "경기도 안양시 동안구 3", "lat": 37.4500, "lng": 126.9500,
         "category": "food", "slope_yn": "Y"},
        {"poi_id": "T1", "name": "관광지", "addr": "경기도 안양시 만안구 4", "lat": 37.3901, "lng": 126.9501,
         "category": "tour", "slope_yn": "Y"},
    ])
    w("facility_accessibility.json", [
        _facl(1, "흥부가", "일반음식점", 37.3910, 126.9500, entrance_ramp_yn="Y", approach_road_yn="Y"),
        _facl(2, "일반음식점", "일반음식점", 37.3905, 126.9500, entrance_ramp_yn="Y"),
        _facl(3, "가까운식당", "휴게음식점·제과점", 37.3902, 126.9500, entrance_ramp_yn="Y"),
        _facl(4, "박달복합청사", "국가 또는 지자체 청사", 37.3904, 126.9504,
              eval_info_raw="주출입구 접근로, 주출입구높이차이제거(경사로), 승강기, 장애인사용가능화장실"),
        _facl(5, "비산2동 행정복지센터", "국가 또는 지자체 청사", 37.3950, 126.9500,
              dis_toilet_yn="Y", entrance_ramp_yn="N"),
        _facl(6, "평촌아파트", "아파트", 37.3901, 126.9501, dis_toilet_yn="Y"),
        _facl(7, "안양초등학교", "초등학교", 37.3903, 126.9503, dis_toilet_yn="Y"),
        _facl(8, "관양도서관", "도서관", 37.3960, 126.9500, dis_toilet_yn="N"),
    ])
    w("public_toilets.json", [
        {"toilet_id": 1, "toilet_name": "공원 공중화장실", "toilet_type": "공중화장실", "addr_road": "a",
         "m_dis_toilet_count": 1, "f_dis_toilet_count": 1, "open_time": "24시간",
         "latitude": 37.3906, "longitude": 126.9500},
    ])
    w("selfdiag_provider.json", [
        {"provider_name": "㈜아이공간 안양평촌점", "service_name": "발달장애인 주간활동 및 방과후활동서비스",
         "address": "동안구 경수대로 523", "phone": "031-422-6958", "created_at": "2025-09-23"},
        {"provider_name": "㈜아이공간 안양평촌점", "service_name": "장애아동 발달재활 서비스 바우처 사용처",
         "address": "경기도 안양시 동안구 흥안대로94", "phone": "031-422-6958", "created_at": "2025-09-23"},
        {"provider_name": "관악장애인 공동생활가정", "service_name": "장애인 거주시설",
         "address": "경기도 안양시 만안구 예술공원로 59", "phone": None, "created_at": "2025-09-23"},
    ])
    w("dev_support_org.json", [
        {"org_name": "(주)아이공간평촌점", "region": "경기도 안양시 동안구", "day_activity": True,
         "afterschool": False, "emergency_care": True, "created_at": "2025-07-10"},
        {"org_name": "바름아동센터", "region": "경기도 안양시 만안구", "day_activity": True,
         "afterschool": True, "created_at": "2025-07-10"},
        {"org_name": "수원기관", "region": "경기도 수원시", "day_activity": True, "created_at": "2025-07-10"},
    ])
    w("std_workplace.json", [
        {"company_name": "㈜고운누리", "address": "경기도 안양시 만안구 박달로 337", "tel": "043-261-7376",
         "business_item": "서비스업(카페, 매점 등)", "type": "자회사", "cert_date": "2021-11-22",
         "created_at": "2025-07-10"},
        {"company_name": "라이트팹", "address": "경기도 안양시 만안구 덕천로34번길 35", "tel": "031-447-1165",
         "business_item": "제조업(조명기구)", "type": "일반", "cert_date": "2018-10-31", "created_at": "2025-07-10"},
        {"company_name": "수원회사", "address": "경기도 수원시 1", "business_item": "카페", "created_at": "2025-07-10"},
    ])

    monkeypatch.setenv("NETWORK_PATH", str(net))
    monkeypatch.setenv("NETWORK_VERSION", "test-1")
    monkeypatch.setenv("POI_BACKEND", "file")
    monkeypatch.setenv("POI_DATA_DIR", str(d))
    monkeypatch.setenv("ROUTE_API_TOKEN", "")
    monkeypatch.setenv("METRICS_LOG_PATH", "")

    import route_service.config as cfg
    importlib.reload(cfg)
    cfg._settings = None
    import route_service.api.main as m
    importlib.reload(m)
    with TestClient(m.app) as c:
        yield c


def test_food_confirmed_first_unknown_kept(client):
    r = client.get("/food/nearby", params={"lat": BASE[0], "lng": BASE[1], "radius_m": 1000})
    assert r.status_code == 200, r.text
    b = r.json()
    names = [i["name"] for i in b["items"]]
    # 관광 음식점 확인(yes) → 실태조사 건물 확인(yes) → 정보 없음(unknown)
    assert names == ["경사로식당", "흥부가", "가까운식당"], names
    assert b["total"] == 3 and b["confirmed"] == 2 and b["unknown"] == 1
    assert "먼식당" not in names and "관광지" not in names       # 반경 밖 · 관광 분류
    assert "일반음식점" not in names                              # 업종명뿐인 이름
    f2 = [i for i in b["items"] if i["name"] == "가까운식당"]
    assert len(f2) == 1 and f2[0]["record_type"] == "tour_listing"   # 실태조사 중복은 빠진다
    assert f2[0]["entry_status"] == "unknown"
    hb = [i for i in b["items"] if i["name"] == "흥부가"][0]
    assert hb["record_type"] == "building_survey" and hb["survey_note"]
    assert b["items"][0]["facilities"] == ["접근로·경사로", "장애인 화장실"]


def test_food_toilet_pairing(client):
    """v1.33.0 — 식당마다 자체 장애인 화장실(own) 또는 200m 안 접근 가능 화장실(nearby)을 붙인다."""
    r = client.get("/food/nearby", params={"lat": BASE[0], "lng": BASE[1], "radius_m": 1000})
    b = r.json()
    assert b["toilet_radius_m"] == 200
    by = {i["name"]: i["toilet"] for i in b["items"]}
    assert by["경사로식당"]["status"] == "own" and by["경사로식당"]["nearby"] is None     # 공원 화장실 267m 밖
    assert by["흥부가"]["status"] == "nearby" and by["흥부가"]["nearby"]["name"] == "공원 공중화장실"
    assert 30 <= by["흥부가"]["nearby"]["dist_m"] <= 60 and by["흥부가"]["nearby"]["open_time"] == "24시간"
    assert by["가까운식당"]["status"] == "nearby"
    r = client.get("/food/nearby", params={"lat": BASE[0], "lng": BASE[1], "radius_m": 1000, "toilet_radius_m": 0})
    assert all(i["toilet"]["nearby"] is None for i in r.json()["items"])
    assert [i["toilet"]["status"] for i in r.json()["items"]] == ["own", "none", "none"]


def test_food_toilet_pairing_single_query_and_unknown(monkeypatch):
    """DB 는 한 번만 부르고, 좌표 없는 식당은 unknown 이다."""
    from route_service.poi import food as poi_food
    calls = []

    def fake_toilets_near(store, lat, lng, radius_m, limit, accessible_only):
        calls.append((round(lat, 4), round(lng, 4), round(radius_m), limit))
        return [{"name": "화장실A", "lat": 37.3906, "lng": 126.95, "source": "PUBLIC_TOILET", "open_time": "24시간"}]
    monkeypatch.setattr(poi_food.poi_support, "toilets_near", fake_toilets_near)
    items = [{"name": "a", "lat": 37.3905, "lng": 126.95, "facilities": []},
             {"name": "b", "lat": 37.3910, "lng": 126.95, "facilities": ["장애인 화장실"]},
             {"name": "c", "lat": None, "lng": None, "facilities": []},
             {"name": "d", "lat": 37.4200, "lng": 126.95, "facilities": None}]
    poi_food.attach_toilets(None, items, 200)
    assert len(calls) == 1 and calls[0][3] == 500
    assert items[0]["toilet"]["status"] == "nearby" and items[0]["toilet"]["nearby"]["dist_m"] == 11
    assert items[1]["toilet"]["status"] == "own" and items[1]["toilet"]["nearby"]["name"] == "화장실A"
    assert items[2]["toilet"]["status"] == "unknown" and items[2]["toilet"]["nearby"] is None
    assert items[3]["toilet"]["status"] == "none"                      # 3.3km 밖 — 반경 안 화장실 없음


def test_food_accessible_only_and_no_origin(client):
    r = client.get("/food/nearby", params={"lat": BASE[0], "lng": BASE[1], "accessible_only": "true"})
    assert [i["entry_status"] for i in r.json()["items"]] == ["yes", "yes"]
    r = client.get("/food/nearby")                                   # 위치 없이 지역 전체
    b = r.json()
    assert b["total"] == 4 and b["radius_m"] is None and "먼식당" in [i["name"] for i in b["items"]]


def test_building_search_text_basis_and_alias(client):
    r = client.get("/facility/accessibility", params={"q": "박달복합청사"})
    it = r.json()["items"][0]
    assert it["status"]["dis_toilet"] == "yes" and it["basis"]["dis_toilet"] == "text"
    assert it["entry_status"] == "yes" and it["status"]["elevator"] == "yes"
    assert it["status"]["dis_parking"] == "unknown"                  # 자료 없음은 없음이 아니다
    r = client.get("/facility/accessibility", params={"q": "비산2동 주민센터"})   # 옛 이름
    it = r.json()["items"][0]
    assert it["name"] == "비산2동 행정복지센터" and it["entry_status"] == "no"
    assert "주출입구 턱 없음(경사로)" in it["lacks"]
    assert client.get("/facility/accessibility").status_code == 400


def test_building_near_excludes_housing(client):
    r = client.get("/facility/accessibility", params={"lat": BASE[0], "lng": BASE[1], "radius_m": 100})
    names = [i["name"] for i in r.json()["items"]]
    assert "평촌아파트" not in names and "일반음식점" not in names
    assert names[0] == "가까운식당"                                    # 거리순


def test_toilet_includes_public_buildings(client):
    r = client.get("/toilet/nearby", params={"lat": BASE[0], "lng": BASE[1], "radius_m": 1000, "limit": 10})
    items = r.json()["items"]
    names = [i["name"] for i in items]
    assert names[0] == "박달복합청사 (건물 안 장애인화장실)", names
    bd = items[0]
    assert bd["facility_toilet"] is True and bd["building_toilet"] is True
    assert bd["open_time"] == "건물 운영시간 내" and bd["source"] == "KOWSI_FACL"
    assert "공원 공중화장실" in names and "비산2동 행정복지센터 (건물 안 장애인화장실)" in names
    assert not any("초등학교" in n or "아파트" in n or "도서관" in n for n in names)
    r = client.get("/toilet/nearby", params={"lat": BASE[0], "lng": BASE[1], "accessible_only": "false"})
    assert not any(i.get("building_toilet") for i in r.json()["items"])


def test_providers_grouped_and_filtered(client):
    r = client.get("/directory/providers", params={"service": "주간활동"})
    b = r.json()
    names = [i["name"] for i in b["items"]]
    assert names == ["㈜아이공간 안양평촌점", "바름아동센터"], names
    ai = b["items"][0]
    assert ai["tel"] == "031-422-6958" and "긴급돌봄" in ai["services"]
    assert len(ai["sources"]) == 2 and b["base_date"] == "2025-09-23"
    r = client.get("/directory/providers", params={"district": "만안구"})
    assert sorted(i["name"] for i in r.json()["items"]) == ["관악장애인 공동생활가정", "바름아동센터"]
    r = client.get("/directory/providers", params={"sigungu": "수원"})
    assert [i["name"] for i in r.json()["items"]] == ["수원기관"]


def test_workplaces_filter(client):
    r = client.get("/directory/workplaces", params={"q": "카페"})
    b = r.json()
    assert [i["name"] for i in b["items"]] == ["㈜고운누리"] and b["base_date"] == "2025-07-10"
    r = client.get("/directory/workplaces")
    assert r.json()["total"] == 2
