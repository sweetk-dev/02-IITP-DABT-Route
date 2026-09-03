# -*- coding: utf-8 -*-
"""실시간 버스(GBIS)·역 편의시설 (v1.19.0).

  1) GBIS 응답 정규화 — 빈 문자열("") 항목·저상 판정·가장 빠른 저상 차량
  2) 실패는 예외가 아니라 status=unavailable 로 (경로 안내는 계속)
  3) API 계약 — /transit/bus/arrivals·/transit/bus/locations·/transit/station/facilities
  4) /route/plan realtime=true 는 버스 leg 에 realtime 을 붙이고 고정 경고를 실측 문구로 바꾼다
  5) 지하철 leg 승·하차 역에 설비 요약(승강기 출입구·장애인화장실 3상태)이 붙는다
"""
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import make_graph  # noqa: E402

from route_service.transit import gbis_live  # noqa: E402


# 2026-09-02 실측 응답 축약 — 빈 항목은 "" 로 온다
ARRIVAL_BODY = {"response": {"msgHeader": {"resultCode": 0, "resultMessage": "정상"},
                "msgBody": {"busArrivalList": [
                    {"flag": "PASS", "locationNo1": 10, "locationNo2": "", "lowPlate1": 0,
                     "lowPlate2": "", "plateNo1": "경기71바1468", "plateNo2": "",
                     "predictTime1": 11, "predictTime2": "", "predictTimeSec1": 660,
                     "routeDestName": "구로디지털단지역(중)", "routeId": 208000007,
                     "routeName": 1, "staOrder": 23, "stationId": 208000069,
                     "stationNm1": "안양우편물류센터", "stationNm2": ""},
                    {"flag": "PASS", "locationNo1": 6, "locationNo2": 14, "lowPlate1": 1,
                     "lowPlate2": 1, "plateNo1": "경기71바1118", "plateNo2": "경기71바1119",
                     "predictTime1": 9, "predictTime2": 17, "routeDestName": "충훈부종점",
                     "routeId": 208000096, "routeName": 51, "staOrder": 30,
                     "stationId": 208000069, "stationNm1": "남부시장", "stationNm2": "박달"},
                ]}}}

LOCATION_BODY = {"response": {"msgHeader": {"resultCode": 0},
                 "msgBody": {"busLocationList": [
                     {"crowded": 0, "lowPlate": 0, "plateNo": "경기71바8004", "remainSeatCnt": -1,
                      "routeId": 241253001, "stateCd": 0, "stationId": 208000305,
                      "stationSeq": 3, "vehId": 289000652},
                     {"crowded": 0, "lowPlate": 1, "plateNo": "경기71바9299", "remainSeatCnt": -1,
                      "routeId": 241253001, "stateCd": 1, "stationId": 208000069,
                      "stationSeq": 10, "vehId": 289000656},
                 ]}}}

EMPTY_BODY = {"response": {"msgHeader": {"resultCode": 4, "resultMessage": "결과가 존재하지 않습니다"},
                           "msgBody": None}}


def _live(responses):
    calls = []

    def fetch(url):
        calls.append(url)
        for key, body in responses.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError("예상치 못한 URL: %s" % url)
    live = gbis_live.GbisLive(api_key="k", fetch=fetch, cache_ttl_sec=60)
    live.calls = calls
    return live


# ── 1. 정규화 ────────────────────────────────────────────────
def test_arrivals_normalizes_blank_fields_and_low_floor():
    live = _live({"getBusArrivalListv2": ARRIVAL_BODY})
    out = live.arrivals(208000069, route_meta={"208000096": {"name": "51", "type": "일반형시내버스",
                                                             "end_station": "충훈부"}})
    assert out["status"] == "success"
    by_route = {it["route_id"]: it for it in out["items"]}
    r1 = by_route["208000007"]
    assert len(r1["vehicles"]) == 1, "2번째 차량 항목이 전부 빈 문자열이면 차량이 없는 것"
    assert r1["vehicles"][0]["low_floor"] is False
    assert r1["route_name"] == "1" and r1["end_station"] == "구로디지털단지역(중)"
    r51 = by_route["208000096"]
    assert r51["route_type"] == "일반형시내버스" and r51["end_station"] == "충훈부"
    assert [v["low_floor"] for v in r51["vehicles"]] == [True, True]
    assert [v["predict_min"] for v in r51["vehicles"]] == [9, 17]
    # 가장 빨리 오는 저상 차량
    assert out["next_low_floor"]["route_name"] == "51"
    assert out["next_low_floor"]["predict_min"] == 9
    assert out["next_low_floor"]["stops_away"] == 6


def test_arrivals_route_filter_and_no_low_floor():
    live = _live({"getBusArrivalListv2": ARRIVAL_BODY})
    out = live.arrivals(208000069, route_id="208000007")
    assert [it["route_id"] for it in out["items"]] == ["208000007"]
    assert out["next_low_floor"] is None, "일반 차량뿐이면 저상 없음(None)"


def test_arrivals_empty_result_code_4_is_success_with_no_items():
    live = _live({"getBusArrivalListv2": EMPTY_BODY})
    out = live.arrivals(1)
    assert out["status"] == "success" and out["items"] == [] and out["next_low_floor"] is None


def test_locations_normalizes_and_joins_stop_index():
    live = _live({"getBusLocationListv2": LOCATION_BODY})
    idx = {"208000069": {"name": "안양역", "lat": 37.4019, "lng": 126.9226, "station_seq": 10}}
    out = live.locations(241253001, stop_index=idx)
    assert out["status"] == "success" and out["low_floor_cnt"] == 1
    v = [x for x in out["vehicles"] if x["station_id"] == "208000069"][0]
    assert v["low_floor"] is True and v["state"] == "정류소 도착"
    assert v["station_name"] == "안양역" and v["lat"] == 37.4019


# ── 2. 실패 처리 ──────────────────────────────────────────────
def test_fetch_failure_returns_unavailable_not_exception():
    live = _live({"getBusArrivalListv2": OSError("connection refused")})
    out = live.arrivals(208000069)
    assert out["status"] == "unavailable" and "OSError" in out["reason"]
    assert out["items"] == [] and out["next_low_floor"] is None


def test_no_api_key_is_unavailable_without_calling():
    live = gbis_live.GbisLive(api_key="", fetch=lambda u: (_ for _ in ()).throw(AssertionError("호출 금지")))
    out = live.arrivals(1)
    assert out["status"] == "unavailable" and "인증키" in out["reason"]


def test_cache_absorbs_repeated_polling():
    live = _live({"getBusArrivalListv2": ARRIVAL_BODY})
    live.arrivals(208000069)
    live.arrivals(208000069)
    live.arrivals(208000069, route_id="208000007")   # 필터만 달라도 같은 정류장 캐시
    assert len(live.calls) == 1


# ── 3~5. API 계약 ─────────────────────────────────────────────
@pytest.fixture()
def client(tmp_path, monkeypatch):
    import pickle

    net = tmp_path / "network.gpickle"
    with open(net, "wb") as f:
        pickle.dump(make_graph(), f)
    poi_dir = tmp_path / "poi"
    poi_dir.mkdir()
    (poi_dir / "tour_bf.json").write_text(json.dumps([{
        "poi_id": "TBF-1", "name": "테스트 무장애 공원",
        "addr": "경기도 안양시 만안구 테스트로 1",
        "latitude": 37.3909, "longitude": 126.9511,
        "entrance": {"lat": 37.3909, "lng": 126.9511},
    }], ensure_ascii=False), encoding="utf-8")
    (poi_dir / "stations.json").write_text(json.dumps([
        {"poi_id": "ST-A", "name": "안양", "latitude": 37.3901, "longitude": 126.9502,
         "elevator_cnt": 4, "wheelchair_lift_cnt": 0, "dis_toilet_yn": "Y", "line": "1호선"},
        {"poi_id": "ST-M", "name": "명학", "latitude": 37.3908, "longitude": 126.9510,
         "elevator_cnt": 4, "wheelchair_lift_cnt": 0, "line": "1호선"},   # 유무 자료 없음(NULL)
    ], ensure_ascii=False), encoding="utf-8")
    (poi_dir / "station_facilities.json").write_text(json.dumps([
        {"stn_cd": "ST-A", "name": "안양", "line": "1호선", "latitude": 37.3901, "longitude": 126.9502,
         "elevator_cnt": 4, "escalator_cnt": 6, "wheelchair_lift_cnt": 0,
         "dis_slope_yn": "Y", "dis_toilet_yn": "Y", "gen_toilet_yn": "Y",
         "elevators": [{"exit_no": "2", "detail_loc": "(2F) 1번출구 맞이방 서쪽", "capacity_person": 11},
                       {"exit_no": "내부", "detail_loc": "(1F) 관악역 방향 승강장 4-3 출입문앞"}],
         "toilets": [{"gate_inout": "외", "exit_no": "2", "detail_loc": "(2층) 대합실내 북쪽 게이트 좌측",
                      "toilet_kind": "여자", "disabled_yn": "Y"}],
         "platforms": [{"platform_no": "1", "updown": "상행", "safety_plate_yn": "Y",
                        "screen_door_yn": "N", "gap_min_cm": 9.5, "gap_max_cm": 11.0}]},
        {"stn_cd": "ST-M", "name": "명학", "line": "1호선", "latitude": 37.3908, "longitude": 126.9510,
         "elevator_cnt": 4, "wheelchair_lift_cnt": 0,
         "elevators": [{"exit_no": "1", "detail_loc": "(1F) 1번 출입구 앞"}]},
    ], ensure_ascii=False), encoding="utf-8")
    (poi_dir / "transit_stops.json").write_text(json.dumps([
        {"poi_id": "208000069", "name": "출발정류장", "lat": 37.3900, "lng": 126.9502,
         "mobile_no": "09213",
         "routes": [{"route_id": "208000096", "name": "51", "type": "일반형시내버스",
                     "end_station": "충훈부", "station_seq": [5]}]},
        {"poi_id": "BS-D", "name": "도착정류장", "lat": 37.3900, "lng": 126.9512,
         "mobile_no": "09002",
         "routes": [{"route_id": "208000096", "name": "51", "type": "일반형시내버스",
                     "end_station": "충훈부", "station_seq": [9]}]},
    ], ensure_ascii=False), encoding="utf-8")
    (poi_dir / "transit_route_paths.json").write_text(json.dumps([
        {"route_id": "208000096", "station_id": "208000069", "station_seq": 5, "name": "출발정류장",
         "mobile_no": "09213", "lat": 37.3900, "lng": 126.9502},
        {"route_id": "208000096", "station_id": "BS-1", "station_seq": 6, "name": "경유1",
         "mobile_no": "09011", "lat": 37.3899, "lng": 126.9505},
        {"route_id": "208000096", "station_id": "BS-D", "station_seq": 9, "name": "도착정류장",
         "mobile_no": "09002", "lat": 37.3900, "lng": 126.9512},
    ], ensure_ascii=False), encoding="utf-8")

    monkeypatch.setenv("NETWORK_PATH", str(net))
    monkeypatch.setenv("NETWORK_VERSION", "test-1")
    monkeypatch.setenv("POI_BACKEND", "file")
    monkeypatch.setenv("POI_DATA_DIR", str(poi_dir))
    monkeypatch.setenv("ROUTE_API_TOKEN", "")
    monkeypatch.setenv("DATA_GO_KR_API_KEY", "test-key")

    import importlib
    import route_service.config as cfg
    importlib.reload(cfg)
    cfg._settings = None
    import route_service.api.main as m
    importlib.reload(m)
    with TestClient(m.app) as c:
        # 외부 호출을 실측 응답으로 대체
        live = _live({"getBusArrivalListv2": ARRIVAL_BODY, "getBusLocationListv2": LOCATION_BODY})
        m.gbis_live.LIVE = live
        c.live = live
        yield c


def test_health_reports_realtime_flag(client):
    assert client.get("/health").json()["bus_realtime"] is True


def test_bus_arrivals_endpoint_joins_static_route_meta(client):
    r = client.get("/transit/bus/arrivals", params={"station_id": "208000069"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success" and body["source"] == "gbis"
    r51 = [it for it in body["items"] if it["route_id"] == "208000096"][0]
    assert r51["route_type"] == "일반형시내버스", "정적 DB 의 노선 유형이 덧입혀져야 한다"
    assert body["next_low_floor"]["route_name"] == "51"


def test_bus_locations_endpoint_joins_stop_coords(client):
    r = client.get("/transit/bus/locations", params={"route_id": "208000096"})
    assert r.status_code == 200
    v = [x for x in r.json()["vehicles"] if x["station_id"] == "208000069"][0]
    assert v["station_name"] == "출발정류장" and v["lat"] == 37.39


def test_station_facilities_by_name_and_three_state(client):
    r = client.get("/transit/station/facilities", params={"name": "안양역"})
    assert r.status_code == 200
    fac = r.json()
    assert fac["poi_id"] == "ST-A" and fac["counts"]["elevator"] == 4
    assert fac["elevators"][0]["exit_no"] == "2"
    assert fac["toilets"][0]["disabled"] is True
    assert fac["status"]["dis_toilet"] == "yes" and fac["status"]["safety_plate"] == "yes"
    assert fac["platforms"][0]["gap_min_cm"] == 9.5

    r = client.get("/transit/station/facilities", params={"stn_cd": "ST-M"})
    fac = r.json()
    # 코레일 API 미응답 역 — 유무는 unknown 이지 no 가 아니다
    assert fac["status"]["dis_toilet"] == "unknown" and fac["status"]["dis_slope"] == "unknown"
    assert fac["status"]["safety_plate"] == "unknown"

    assert client.get("/transit/station/facilities", params={"name": "없는역"}).status_code == 404
    assert client.get("/transit/station/facilities").status_code == 400


def test_access_points_carry_three_state_toilet(client):
    r = client.get("/transit/access-points", params={"lat": 37.3901, "lng": 126.9502, "radius_m": 300})
    st = {it["poi_id"]: it for it in r.json()["items"] if it["type"] == "transit_station"}
    assert st["ST-A"]["dis_toilet_status"] == "yes"
    assert st["ST-M"]["dis_toilet_status"] == "unknown"


def test_plan_realtime_attaches_arrivals_and_replaces_fixed_warning(client):
    body = {"origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "tour", "poi_id": "TBF-1"},
            "profile": "wheelchair_manual", "mode": "walk_bus", "realtime": True}
    r = client.post("/route/plan", json=body)
    assert r.status_code == 200, r.text
    legs = r.json()["routes"][0]["legs"]
    bus = [l for l in legs if l["kind"] == "bus"][0]
    assert bus["realtime"]["status"] == "success"
    assert bus["realtime"]["next_low_floor"]["predict_min"] == 9
    assert all("보장되지 않습니다" not in w for w in bus["warnings"])
    assert any("저상버스 51번이 약 9분" in w for w in bus["warnings"])
    # 스텝에도 승차 정류장·노선 참조가 실린다(프런트 폴링용)
    steps = r.json()["routes"][0]["steps"]
    board = [s for s in steps if s["maneuver"] == "bus_board"][0]
    assert board["leg_ref"] == {"kind": "bus", "route_id": "208000096", "route_name": "51",
                                "board_station_id": "208000069", "board_name": "출발정류장",
                                "alight_station_id": "BS-D"}


def test_plan_without_realtime_keeps_fixed_warning(client):
    body = {"origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "tour", "poi_id": "TBF-1"},
            "profile": "wheelchair_manual", "mode": "walk_bus"}
    r = client.post("/route/plan", json=body)
    bus = [l for l in r.json()["routes"][0]["legs"] if l["kind"] == "bus"][0]
    assert "realtime" not in bus
    assert any("보장되지 않습니다" in w for w in bus["warnings"])
    # 노선형상(정적, 지도선용)은 realtime 과 무관하게 조회한다 (v1.20.0) — 도착정보 호출만 없어야 한다
    assert [c for c in client.live.calls if "busarrivalservice" in c] == [], "realtime 미요청이면 도착정보 호출이 없어야 한다"


def test_plan_realtime_failure_keeps_fixed_warning(client):
    client.live._fetch = lambda url: (_ for _ in ()).throw(OSError("down"))
    client.live._cache.clear()
    body = {"origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "tour", "poi_id": "TBF-1"},
            "profile": "wheelchair_manual", "mode": "walk_bus", "realtime": True}
    r = client.post("/route/plan", json=body)
    assert r.status_code == 200
    bus = [l for l in r.json()["routes"][0]["legs"] if l["kind"] == "bus"][0]
    assert bus["realtime"]["status"] == "unavailable"
    assert any("보장되지 않습니다" in w for w in bus["warnings"])


def test_subway_leg_carries_station_facilities(client):
    body = {"origin": {"lat": 37.3901, "lng": 126.9501},
            "destination": {"type": "tour", "poi_id": "TBF-1"},
            "profile": "wheelchair_manual", "mode": "walk_bus_subway"}
    r = client.post("/route/plan", json=body)
    assert r.status_code == 200, r.text
    subs = [l for l in r.json()["routes"][0]["legs"] if l["kind"] == "subway"]
    assert subs, "지하철 leg 없음"
    b = subs[0]["board"]["facilities"]
    assert b["elevators"] and b["dis_toilet"] in ("yes", "no", "unknown")


# ── v1.20.0 노선형상 — 버스 구간 지도선 ──────────────────────────
LINE_OK = {"response": {"msgHeader": {"resultCode": 0},
                        "msgBody": {"busRouteLineList": [
                            {"lineSeq": 3, "x": "126.9532", "y": "37.3922"},
                            {"lineSeq": 1, "x": "126.9500", "y": "37.3900"},
                            {"lineSeq": 2, "x": "126.9515", "y": "37.3912"}]}}}


def test_route_line_sorted_by_seq_and_cached():
    live = _live({"getBusRouteLineListv2": LINE_OK})
    line = live.route_line("208000096")
    assert line == [[37.39, 126.95], [37.3912, 126.9515], [37.3922, 126.9532]]
    live.route_line("208000096")
    assert len(live.calls) == 1, "형상은 캐시돼야 한다"


def test_route_line_unavailable_is_empty():
    live = _live({"getBusRouteLineListv2": TimeoutError("t")})
    assert live.route_line("208000096") == []


def test_slice_line_between_stops_and_fallback_conditions():
    line = [[37.3900, 126.9500], [37.3912, 126.9515], [37.3922, 126.9532], [37.3930, 126.9545]]
    board = {"lat": 37.3901, "lng": 126.9501}
    alight = {"lat": 37.3921, "lng": 126.9531}
    seg = gbis_live.GbisLive.slice_line(line, board, alight)
    assert seg[0] == [37.3901, 126.9501] and seg[-1] == [37.3921, 126.9531]
    assert [37.3912, 126.9515] in seg
    assert gbis_live.GbisLive.slice_line(line, alight, board) == [], "역방향(편도 형상)은 폴백"
    assert gbis_live.GbisLive.slice_line(line, {"lat": 37.40, "lng": 126.99}, alight) == [], "형상에서 먼 정류장은 폴백"
    assert gbis_live.GbisLive.slice_line([], board, alight) == []
