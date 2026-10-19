# -*- coding: utf-8 -*-
"""장애인 문화·체육 시설 근접 조회 /culture/nearby (v1.36.0).

  1) 종류별 반경 안 가까운 순·limit
  2) 반경 안이 0건인 종류만 반경 밖 가까운 곳을 beyond_radius 로 붙인다(fallback)
  3) fallback=false 면 붙이지 않는다
  4) 서비스 지역(경로망 범위) 안/밖 표시, 잘못된 유형 400, 논리삭제·좌표 없음 제외(파일 백엔드는 원천 행 그대로)
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = {"lat": 37.3900, "lng": 126.9500}


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
    rows = [
        # 책나래 — 반경 안 3곳(가까운 순 확인용), 1곳은 반경 밖
        {"facility_id": 1, "facility_type": "booknarae_lib", "name": "가까운 도서관", "sigun_nm": "안양시",
         "addr_road": "경기도 안양시 a로 1", "latitude": 37.3905, "longitude": 126.9500, "tel": "031-1"},
        {"facility_id": 2, "facility_type": "booknarae_lib", "name": "중간 도서관", "sigun_nm": "안양시",
         "addr_road": "경기도 안양시 b로 2", "latitude": 37.3950, "longitude": 126.9500},
        {"facility_id": 3, "facility_type": "booknarae_lib", "name": "먼 도서관", "sigun_nm": "안양시",
         "addr_road": "경기도 안양시 c로 3", "latitude": 37.4000, "longitude": 126.9500},
        {"facility_id": 4, "facility_type": "booknarae_lib", "name": "수원 도서관", "sigun_nm": "수원시",
         "addr_road": "경기도 수원시 d로 4", "latitude": 37.2700, "longitude": 127.0100},
        # 점자도서관 — 반경 밖에만 있다(수원·성남)
        {"facility_id": 10, "facility_type": "braille_lib", "name": "경기도시각장애인도서관", "sigun_nm": "수원시",
         "addr_road": "경기도 수원시 팔달구 수성로157번길 27-4", "latitude": 37.2870, "longitude": 127.0060,
         "tel": "031-213-5045", "homepage": "lib.eyes1004.com"},
        {"facility_id": 11, "facility_type": "braille_lib", "name": "한마음점자도서관", "sigun_nm": "성남시",
         "addr_road": "경기도 성남시 분당구 야탑로 227", "latitude": 37.4100, "longitude": 127.1270},
        {"facility_id": 12, "facility_type": "braille_lib", "name": "해밀도서관", "sigun_nm": "부천시",
         "addr_road": "경기도 부천시 원미구 옥산로10번길 4", "latitude": 37.5040, "longitude": 126.7700},
        # 체육시설 — 반경 안 1곳
        {"facility_id": 20, "facility_type": "sports_fac", "name": "반다비체육센터", "sigun_nm": "안양시",
         "addr_road": "경기도 안양시 만안구 냉천로 39", "latitude": 37.3920, "longitude": 126.9450},
        # 좌표 없음 — 제외
        {"facility_id": 30, "facility_type": "sports_org", "name": "좌표 없는 체육회", "latitude": None, "longitude": None},
        {"facility_id": 31, "facility_type": "sports_org", "name": "안양시장애인체육회", "sigun_nm": "안양시",
         "addr_road": "경기도 안양시 만안구 x 1", "latitude": 37.3930, "longitude": 126.9300},
        # 논리삭제 행 — 파일 백엔드에서도 빠져야 한다
        {"facility_id": 32, "facility_type": "sports_org", "name": "폐지된 체육회", "del_yn": "Y",
         "addr_road": "경기도 안양시 y 1", "latitude": 37.3901, "longitude": 126.9501},
        # 체육시설 — 아주 먼 곳(연천, 약 75km) — 폴백 상한 밖
        {"facility_id": 21, "facility_type": "sports_fac", "name": "연천 체육관", "sigun_nm": "연천군",
         "addr_road": "경기도 연천군 z 1", "latitude": 38.0960, "longitude": 127.0750},
    ]
    (poi_dir / "culture_sports_facility.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

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


def _get(client, **kw):
    params = dict(BASE, **kw)
    r = client.get("/culture/nearby", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_within_radius_sorted_and_limited(client):
    d = _get(client, types="booknarae_lib", radius_m=2000, limit=2)
    names = [i["name"] for i in d["items"]]
    assert names == ["가까운 도서관", "중간 도서관"]           # 가까운 순, limit 2
    assert all(i["beyond_radius"] is False for i in d["items"])
    assert d["within_by_type"]["booknarae_lib"] == 2
    assert d["items"][0]["type_label"] == "책나래 신청 도서관"
    assert "택배" in d["items"][0]["type_desc"]


def test_fallback_only_for_empty_type(client):
    d = _get(client, radius_m=2000)
    by = {}
    for i in d["items"]:
        by.setdefault(i["facility_type"], []).append(i)
    # 점자도서관: 반경 안 0 → 가장 가까운 2곳을 반경 밖으로
    assert [i["name"] for i in by["braille_lib"]] == ["경기도시각장애인도서관", "한마음점자도서관"]
    assert all(i["beyond_radius"] for i in by["braille_lib"])
    assert by["braille_lib"][0]["dist_m"] > 2000
    assert d["within_by_type"]["braille_lib"] == 0 and d["count_by_type"]["braille_lib"] == 2
    # 책나래: 반경 안이 있으면 먼 곳을 섞지 않는다
    assert all(not i["beyond_radius"] for i in by["booknarae_lib"])
    assert "수원 도서관" not in [i["name"] for i in by["booknarae_lib"]]
    # 좌표 없는 행 제외
    assert [i["name"] for i in by["sports_org"]] == ["안양시장애인체육회"]
    assert "경기도" in d["region_note"]


def test_fallback_off(client):
    d = _get(client, types="braille_lib", radius_m=2000, fallback="false")
    assert d["items"] == [] and d["count"] == 0


def test_fallback_limit(client):
    d = _get(client, types="braille_lib", radius_m=2000, fallback_limit=1)
    assert [i["name"] for i in d["items"]] == ["경기도시각장애인도서관"]


def test_item_fields_and_service_area_flag(client):
    d = _get(client, types="braille_lib,sports_fac", radius_m=1000)
    fac = next(i for i in d["items"] if i["facility_type"] == "sports_fac")
    for k in ("facility_id", "name", "addr", "tel", "homepage", "dist_m", "lat", "lng",
              "source_label", "in_service_area", "type_desc"):
        assert k in fac
    assert isinstance(fac["in_service_area"], bool)
    lib = next(i for i in d["items"] if i["facility_type"] == "braille_lib")
    assert lib["homepage"] == "lib.eyes1004.com"


def test_deleted_rows_excluded(client):
    d = _get(client, types="sports_org", radius_m=20000)
    assert "폐지된 체육회" not in [i["name"] for i in d["items"]]


def test_fallback_max_distance(client):
    # 반경 100m — 체육시설은 반경 안 0건. 반다비(약 0.5km)는 폴백, 연천(약 75km)은 상한 밖이라 빠진다
    d = _get(client, types="sports_fac", radius_m=100, fallback_limit=5)
    names = [i["name"] for i in d["items"]]
    assert names == ["반다비체육센터"], names
    d = _get(client, types="sports_fac", radius_m=100, fallback_limit=5, fallback_max_m=100000)
    assert [i["name"] for i in d["items"]] == ["반다비체육센터", "연천 체육관"]


def test_radius_boundary_inclusive(client):
    # 반경 경계 — dist_m 은 반올림 값이라 실제 거리와 0.5m 이내로 다르다. 1m 여유를 두면 반경 안이다
    items = _get(client, types="booknarae_lib", radius_m=50000, limit=10)["items"]
    edge = next(i for i in items if i["dist_m"] >= 100)          # 반경 최솟값(100m) 이상인 첫 시설
    d = _get(client, types="booknarae_lib", radius_m=edge["dist_m"] + 1, limit=10, fallback="false")
    names = [i["name"] for i in d["items"]]
    assert edge["name"] in names and names[-1] == edge["name"]   # 경계 시설까지 들어오고 그 너머는 없다
    assert all(not i["beyond_radius"] for i in d["items"])


def test_mixed_counts_consistent(client):
    d = _get(client, radius_m=2000)
    for t, n in d["count_by_type"].items():
        assert n == sum(1 for i in d["items"] if i["facility_type"] == t)
        assert d["within_by_type"][t] == sum(1 for i in d["items"] if i["facility_type"] == t and not i["beyond_radius"])


def test_bad_type_400(client):
    r = client.get("/culture/nearby", params=dict(BASE, types="museum"))
    assert r.status_code == 400
