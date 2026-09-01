# -*- coding: utf-8 -*-
"""이름으로 장소 찾기 — /poi/search 와 그 세 출처.

관광지·지하철역만으로는 이용자가 말하는 목적지(시청·복지관·도서관)를 좌표로 바꿀 수
없어, 서비스 지역 안의 장소가 "지역 밖"으로 잘못 안내됐다. 건물 폴리곤 이름을 세 번째
출처로 더한 것이 이 검사의 대상이다.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from route_service.engine.access import BuildingIndex  # noqa: E402
from route_service.poi.store import PoiStore  # noqa: E402

from test_api import client  # noqa: E402,F401  (픽스처 재사용)


# ── 건물 이름 인덱스 ──
def _index(names_and_boxes):
    shapely = pytest.importorskip("shapely")
    from shapely.geometry import box

    idx = BuildingIndex()
    idx.polys = [(box(lng, lat, lng + 0.0005, lat + 0.0005), nm)
                 for nm, lat, lng in names_and_boxes]
    idx.loaded = True
    return idx


BUILDINGS = [
    ("안양시청", 37.3943, 126.9568),
    ("안양시노인종합복지관", 37.4000, 126.9300),
    ("안양시 노인종합복지회관", 37.4010, 126.9310),
    ("평촌도서관", 37.3940, 126.9640),
    ("체육관", 37.3900, 126.9500),
]


def test_building_exact_match_ranks_first():
    idx = _index(BUILDINGS)
    hits = idx.search_by_name("안양시청")
    assert hits and hits[0]["name"] == "안양시청"
    assert hits[0]["type"] == "building" and hits[0]["match_rank"] == 0
    # 대표점은 폴리곤 내부 좌표여야 한다 (스냅 대상이 되므로)
    assert 37.394 <= hits[0]["lat"] <= 37.395


def test_building_space_insensitive_match():
    """'노인종합복지관' 은 띄어쓰기가 다른 표기와도 매칭돼야 한다."""
    idx = _index(BUILDINGS)
    names = [h["name"] for h in idx.search_by_name("노인종합복지관")]
    assert "안양시노인종합복지관" in names


def test_building_reverse_match_allows_suffix():
    """'안양시청 민원실' 처럼 질의가 이름을 포함하는 경우도 찾는다."""
    idx = _index(BUILDINGS)
    hits = idx.search_by_name("안양시청 민원실")
    assert hits and hits[0]["name"] == "안양시청"


def test_building_short_query_rejected():
    """한 글자 질의는 오탐이 너무 많아 검색하지 않는다."""
    assert _index(BUILDINGS).search_by_name("관") == []


def test_building_index_empty_when_not_loaded():
    assert BuildingIndex().search_by_name("안양시청") == []


# ── 관광 POI 이름 검색 ──
class _RecordingStore(PoiStore):
    def __init__(self, rows):
        super().__init__(backend="db", dsn="postgresql+psycopg2://x/y")
        self._rows = rows
        self.last_sql = None
        self.last_params = None

    def _query(self, sql, params):
        self.last_sql = sql
        self.last_params = params
        return self._rows


TOUR_ROW = {
    "poi_id": 991, "name": "안양예술공원",
    "addr": "경기도 안양시 만안구 예술공원로 180",
    "latitude": 37.4020, "longitude": 126.9210,
    "fac_text": "", "tourist_type": None,
}


def test_tour_name_search_drops_facility_filter():
    """이름 검색은 무장애 시설 정보가 비어 있는 POI 도 찾아야 한다.

    목록(list_tour_spots)의 accessible_facilities 조건을 그대로 쓰면
    좌표 해석 용도로는 지나치게 좁다.
    """
    store = _RecordingStore([TOUR_ROW])
    out = store.search_tour_by_name("안양예술공원", sigungu="안양")
    assert len(out) == 1 and out[0]["poi_id"] == "991"
    assert "COALESCE(detail_json->>'accessible_facilities', '') <> ''" not in store.last_sql
    assert "ILIKE" in store.last_sql
    # 지역 조건은 유지된다 (#26 타지역 '안양면' 오탐 방지)
    assert store.last_params["sg0"] == "안양시"


def test_tour_name_search_short_query_rejected():
    store = _RecordingStore([TOUR_ROW])
    assert store.search_tour_by_name("안") == []
    assert store.last_sql is None      # 질의 자체를 보내지 않는다


def test_station_name_search_strips_suffix():
    store = PoiStore(backend="file", data_dir="")
    store._cache["stations.json"] = [
        {"poi_id": "S1", "name": "범계", "latitude": 37.3899, "longitude": 126.9509,
         "elevator_cnt": 2, "wheelchair_lift_cnt": 0},
        {"poi_id": "S2", "name": "평촌", "latitude": 37.3942, "longitude": 126.9638,
         "elevator_cnt": 1, "wheelchair_lift_cnt": 0},
    ]
    out = store.search_stations_by_name("범계역")
    assert len(out) == 1 and out[0]["name"] == "범계"
    assert out[0]["type"] == "transit_station"


# ── 엔드포인트 ──
def test_poi_search_endpoint_finds_tour_and_station(client):
    body = client.get("/poi/search", params={"q": "테스트역"}).json()
    names = [i["name"] for i in body["items"]]
    assert "테스트역" in names
    assert body["region"] == "안양시"

    body = client.get("/poi/search", params={"q": "무장애 공원"}).json()
    assert any(i["type"] == "tour" and i["poi_id"] == "TBF-1" for i in body["items"])


def test_poi_search_filters_out_of_bbox(client):
    """네트워크 bbox 밖 좌표는 결과에서 제외된다 — 경로를 만들 수 없기 때문."""
    import route_service.api.main as m

    far = m.poi_store.STORE
    far._cache["stations.json"] = list(far._load_file("stations.json")) + [
        {"poi_id": "S9", "name": "서울역", "latitude": 37.5547, "longitude": 126.9707,
         "elevator_cnt": 5, "wheelchair_lift_cnt": 0},
    ]
    body = client.get("/poi/search", params={"q": "서울역"}).json()
    assert body["items"] == []


def test_poi_search_include_outside_flags_instead_of_dropping(client):
    """범위 밖 결과를 버리면 소비 측이 '못 찾음'과 '범위 밖'을 구분할 수 없다."""
    import route_service.api.main as m

    st = m.poi_store.STORE
    st._cache["stations.json"] = list(st._load_file("stations.json")) + [
        {"poi_id": "S9", "name": "서울역", "latitude": 37.5547, "longitude": 126.9707,
         "elevator_cnt": 5, "wheelchair_lift_cnt": 0},
    ]
    body = client.get("/poi/search",
                      params={"q": "서울역", "include_outside": "true"}).json()
    assert body["count"] == 1
    assert body["items"][0]["in_service_area"] is False


def test_poi_search_puts_in_area_results_first(client):
    """범위 밖 항목은 사유 설명용일 뿐 — 범위 안 결과를 밀어내면 안 된다."""
    import route_service.api.main as m

    st = m.poi_store.STORE
    st._cache["stations.json"] = list(st._load_file("stations.json")) + [
        {"poi_id": "S8", "name": "테스트역앞", "latitude": 37.5548, "longitude": 126.9708,
         "elevator_cnt": 1, "wheelchair_lift_cnt": 0},
    ]
    body = client.get("/poi/search",
                      params={"q": "테스트역", "include_outside": "true"}).json()
    assert body["items"][0]["in_service_area"] is True
    assert body["items"][0]["name"] == "테스트역"


def test_poi_search_empty_for_unknown_place(client):
    body = client.get("/poi/search", params={"q": "존재하지않는장소이름"}).json()
    assert body["count"] == 0 and body["items"] == []


def test_plan_to_building_destination_resolves_access_point(client):
    """building 목적지는 좌표를 그대로 쓰지 않고 접근점 해석을 거친다."""
    r = client.post(
        "/route/plan",
        json={
            "origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "building", "lat": 37.3909, "lng": 126.9511},
            "profile": "wheelchair_manual",
        },
    )
    assert r.status_code == 200
    dest = r.json()["destination"]
    assert dest["type"] == "building"
    # 건물 폴리곤이 없는 테스트 환경에서는 대표점으로 떨어지되, coord 와 구분된다
    assert dest["resolved_by"] in ("building_access", "facility_centroid")


def test_plan_building_without_coord_is_400(client):
    r = client.post(
        "/route/plan",
        json={
            "origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "building"},
            "profile": "wheelchair_manual",
        },
    )
    assert r.status_code == 400
