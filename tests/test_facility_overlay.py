# -*- coding: utf-8 -*-
"""무장애 속성 3소스 결합 (v1.24.0).

종전에는 mv_poi(경기관광공사) 하나만 봐서 안양 관광 POI 228건 중 25건만
추천 후보가 됐다. 통합DB 에 이미 적재된 한국관광공사·한국사회보장정보원
데이터를 덧씌운다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.poi.store import PoiStore, TOUR_FIELDS  # noqa: E402


def _spot(name, lat=37.3904, lng=126.9258, fac=None):
    base = {k: False for k in TOUR_FIELDS}
    base.update(fac or {})
    return {"poi_id": "1", "type": "tour", "name": name, "addr": "경기도 안양시",
            "lat": lat, "lng": lng, "facilities": base, "entrance": None}


def _store(bf=None, facl=None):
    st = PoiStore(backend="db")
    st._cache["overlay:안양시"] = (bf or [], facl or [])
    return st


BF_MUNHWAWON = {
    "fclt_id": 47, "fclt_name": "안양문화원", "latitude": 37.39047, "longitude": 126.92584,
    "toilet_yn": "Y", "elevator_yn": "Y", "parking_yn": "Y", "slope_yn": "Y",
    "subway_yn": None, "bus_stop_yn": None, "wheelchair_rent_yn": None,
    "tactile_map_yn": "Y", "audio_guide_yn": None, "nursing_room_yn": None,
    "accessible_room_yn": None, "stroller_rent_yn": None,
}

FACL_SAMDEOK = {
    "facl_id": 398, "facl_name": "삼덕공원관리사무소", "facl_type": "공중화장실",
    "latitude": 37.39741, "longitude": 126.91641,
    "dis_toilet_yn": "Y", "elevator_yn": "N", "dis_parking_yn": "N",
    "entrance_ramp_yn": "Y", "approach_road_yn": "Y",
    "accessible_room_yn": None, "guide_facility_yn": None,
}


def test_kto_overlay_fills_empty_poi():
    """mv_poi 에 속성이 없어도 한국관광공사 값으로 후보가 된다."""
    st = _store(bf=[BF_MUNHWAWON])
    out = st._apply_overlays([_spot("안양문화원")], ["안양시"])
    assert len(out) == 1
    fac = out[0]["facilities"]
    assert fac["toilet_yn"] and fac["elevator_yn"] and fac["slope_yn"]
    assert out[0]["facility_sources"]["slope_yn"] == ["kto"]
    assert out[0]["facility_match"]["kto"]["by"] == "name"


def test_conflict_is_recorded_not_hidden():
    """한쪽이 Y·다른 쪽이 N 이면 값은 Y 로 두되 상충을 남긴다."""
    bf = dict(BF_MUNHWAWON, elevator_yn="N")
    st = _store(bf=[bf])
    out = st._apply_overlays([_spot("안양문화원", fac={"elevator_yn": True})], ["안양시"])
    assert out[0]["facilities"]["elevator_yn"] is True
    conflicts = out[0]["facility_conflicts"]
    assert any(c["field"] == "elevator_yn" and c["no"] == "kto" for c in conflicts)


def test_facl_matches_by_name_beyond_same_building():
    """이름이 맞으면 30m 안에서 붙는다 — 삼덕공원 ↔ 삼덕공원관리사무소 2m."""
    st = _store(facl=[FACL_SAMDEOK])
    out = st._apply_overlays([_spot("삼덕공원", lat=37.39740, lng=126.91640)], ["안양시"])
    assert len(out) == 1
    assert out[0]["facilities"]["slope_yn"] and out[0]["facilities"]["toilet_yn"]
    assert out[0]["facility_sources"]["slope_yn"] == ["kowsi"]


def test_facl_rejects_neighbour_building():
    """이름이 다르고 15m 를 넘으면 붙이지 않는다 — 식당에 이웃 모텔이 붙던 사례."""
    neighbour = dict(FACL_SAMDEOK, facl_name="버킹검모텔", facl_type="일반숙박시설",
                     latitude=37.39740, longitude=126.91663)   # 약 20m
    st = _store(facl=[neighbour])
    out = st._apply_overlays([_spot("해조", lat=37.39740, lng=126.91640)], ["안양시"])
    assert out == []          # 붙을 근거가 없으므로 후보에서 빠진다


def test_facl_accepts_same_building_without_name():
    """이름이 달라도 같은 건물(15m 이내)이면 받아들인다 — 백화점 입점 매장."""
    dept = dict(FACL_SAMDEOK, facl_name="롯데백화점 평촌점", facl_type="판매시설",
                latitude=37.389985, longitude=126.950421)
    st = _store(facl=[dept])
    out = st._apply_overlays([_spot("보브", lat=37.389985, lng=126.950421)], ["안양시"])
    assert len(out) == 1
    assert out[0]["facility_match"]["kowsi"]["by"] == "coords"


def test_no_information_is_dropped():
    st = _store()
    assert st._apply_overlays([_spot("정보없는곳")], ["안양시"]) == []


def test_file_backend_is_untouched():
    st = PoiStore(backend="file")
    spots = [_spot("픽스처")]
    assert st._apply_overlays(spots, ["안양시"]) == spots


def test_one_facility_row_binds_to_one_poi():
    """한 건물의 편의시설은 대표 POI 하나에만 붙는다 — 백화점 입점 매장 40여 개 방지."""
    dept = dict(FACL_SAMDEOK, facl_name="롯데백화점 평촌점", facl_type="판매시설",
                latitude=37.389985, longitude=126.950421)
    spots = [_spot("보브", lat=37.389985, lng=126.950421),
             _spot("롯데백화점 평촌점", lat=37.389985, lng=126.950421),
             _spot("듀엘", lat=37.389985, lng=126.950421)]
    out = _store(facl=[dept])._apply_overlays(spots, ["안양시"])
    assert [o["name"] for o in out] == ["롯데백화점 평촌점"]
    assert out[0]["facility_match"]["kowsi"]["by"] == "name"
