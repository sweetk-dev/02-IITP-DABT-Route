# -*- coding: utf-8 -*-
"""v1.46.0 — 도보 목적지가 역이면 휠체어로 들어갈 수 있는 출구까지 안내하고, 맞이방으로 가는 방법을 말한다.

지금까지는 역 대표점(역사 건물 쪽)에서 경로가 끝나, 명학역은 1번 출구 승강기에서 약 56m 떨어진 횡단보도 위에서
"목적지에 도착했습니다"로 끝났다. 역 안 출발(#79, test_station_start)의 짝이다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from route_service.transit import exits as ex  # noqa: E402
from test_exits_category import client  # noqa: E402,F401  (fixture 재사용)

MH_EXIT1_ELEV = "(1F) 1번 출입구 및 성결대학교 셔틀버스 정류장 앞(2F) 1번 출입구 및 맞이방 연결통로"
ORIGIN = {"lat": 37.3909, "lng": 126.9511}          # 합성 보행망 — 명학 1번 출구 쪽
DEST = {"type": "building", "lat": 37.3908, "lng": 126.9510}   # 역 대표점


# ── 순수 함수 ─────────────────────────────────────────────
def test_enter_guide_myeonghak_exit1_goes_up():
    g = ex.station_enter_guide("명학역", {"exit_no": "1", "elevator": MH_EXIT1_ELEV, "has_elevator": True})
    assert g["via"] == "elevator" and g["exit_no"] == "1" and g["station"] == "명학"
    assert g["inside"] == ["1번 출구 승강기로 맞이방에 올라갑니다 — 성결대학교 셔틀버스 정류장 앞"]
    assert g["arrive"] == ("명학역 1번 출구 승강기 앞에 도착했습니다 (성결대학교 셔틀버스 정류장 앞). "
                           "승강기로 맞이방에 올라가세요")


def test_enter_guide_underground_goes_down_and_unknown_floor():
    g = ex.station_enter_guide("평촌", {"exit_no": "1", "elevator": "(B1) 대합실 개찰구 앞(1F) 1번 출입구 광장 분수대 옆",
                                       "has_elevator": True})
    assert g["arrive"].endswith("승강기로 맞이방에 내려가세요")
    g2 = ex.station_enter_guide("테스트", {"exit_no": "3", "elevator": "3번 출입구 옆", "has_elevator": True})
    assert g2["arrive"].endswith("승강기로 맞이방에 들어가세요")        # 층을 모르면 방향을 단정하지 않는다


def test_enter_guide_lift_and_no_data():
    lf = ex.station_enter_guide("석수", {"exit_no": "2", "has_elevator": False, "lift": "(1F) 2번 출구 계단 옆"})
    assert lf["via"] == "lift" and "휠체어리프트로 맞이방에" in lf["arrive"]
    plain = ex.station_enter_guide("명학", {"exit_no": "2", "has_elevator": None})
    assert plain["via"] is None and plain["arrive"] == "명학역 2번 출구에 도착했습니다"
    none = ex.station_enter_guide("명학", None)
    assert none["exit_no"] is None and none["arrive"] == "명학역에 도착했습니다" and none["inside"] == []


# ── API ───────────────────────────────────────────────────
def test_station_destination_ends_at_nearest_elevator_exit(client):
    r = client.post("/route/plan", json={"origin": ORIGIN, "destination": DEST,
                                          "destination_station": {"name": "명학역"}})
    assert r.status_code == 200, r.text
    body = r.json()
    end = body["station_end"]
    assert end["station"] == "명학" and end["exit"]["exit_no"] == "1"
    assert (round(body["destination"]["lat"], 4), round(body["destination"]["lng"], 4)) == (37.3905, 126.9511)
    assert body["destination"]["label"] == "명학역 1번 출구"
    last = body["routes"][0]["steps"][-1]
    assert last["maneuver"] == "arrive"
    assert last["instruction"].startswith("명학역 1번 출구 승강기 앞에 도착했습니다")
    assert last["instruction"].endswith("맞이방에 들어가세요") and last["enter"]["via"] == "elevator"
    # 역 대표점으로 가는 종전 요청은 그대로 — 도착 문장도 바뀌지 않는다
    plain = client.post("/route/plan", json={"origin": ORIGIN, "destination": DEST}).json()
    assert "station_end" not in plain
    assert plain["routes"][0]["steps"][-1]["instruction"] == "목적지에 도착했습니다."


def test_blocked_nearest_exit_falls_back(client, monkeypatch):
    import route_service.api.main as m
    from fastapi import HTTPException
    real = m._plan_core

    def fake(lat, lng, dest, *a, **k):
        if dest.type == "coord" and (round(dest.lat, 4), round(dest.lng, 4)) == (37.3905, 126.9511):
            raise HTTPException(status_code=422, detail="막힘")
        return real(lat, lng, dest, *a, **k)
    monkeypatch.setattr(m, "_plan_core", fake)
    body = client.post("/route/plan", json={"origin": ORIGIN, "destination": DEST,
                                             "destination_station": {"name": "명학"}}).json()
    assert body["station_end"]["exit"]["exit_no"] == "2"
    assert body["destination"]["label"] == "명학역 2번 출구"


def test_station_without_exit_data_keeps_destination(client, monkeypatch):
    monkeypatch.setattr(ex, "_DATA", {})
    body = client.post("/route/plan", json={"origin": ORIGIN, "destination": DEST,
                                             "destination_station": {"name": "명학"}}).json()
    assert body["station_end"]["exit"] is None and body["destination"]["label"] == "명학역"
    assert body["routes"][0]["steps"][-1]["instruction"] == "명학역에 도착했습니다"


def test_unknown_station_404(client):
    r = client.post("/route/plan", json={"origin": ORIGIN, "destination": DEST,
                                          "destination_station": {"name": "없는역"}})
    assert r.status_code == 404


def test_reroute_keeps_station_arrival_text(client):
    body = client.post("/route/plan", json={"origin": ORIGIN, "destination": DEST,
                                             "destination_station": {"name": "명학"}}).json()
    d = body["destination"]
    rr = client.post("/route/reroute", json={
        "current": ORIGIN, "route_id": body["route_id"],
        "destination": {"type": "coord", "lat": d["lat"], "lng": d["lng"]}}).json()
    assert rr["station_end"]["exit"]["exit_no"] == "1"
    assert rr["routes"][0]["steps"][-1]["instruction"] == body["routes"][0]["steps"][-1]["instruction"]
    # 다른 곳으로 재탐색하면 역 도착 문장을 붙이지 않는다
    other = client.post("/route/reroute", json={
        "current": ORIGIN, "route_id": body["route_id"],
        "destination": {"type": "tour", "poi_id": "TBF-1"}}).json()
    assert "station_end" not in other


# ── v1.46.1 — 화면에서 고른 역(transit_station)도 역 안까지 ───────────────────
def test_transit_station_poi_destination_gets_station_end(client):
    body = client.post("/route/plan", json={"origin": ORIGIN,
                                             "destination": {"type": "transit_station", "poi_id": "ST-M"}}).json()
    assert body["station_end"]["station"] == "명학" and body["station_end"]["exit"]["exit_no"] == "1"
    assert body["routes"][0]["steps"][-1]["instruction"].startswith("명학역 1번 출구 승강기 앞에 도착했습니다")


def test_transit_station_without_exit_data_unchanged(client, monkeypatch):
    monkeypatch.setattr(ex, "_DATA", {})
    body = client.post("/route/plan", json={"origin": ORIGIN,
                                             "destination": {"type": "transit_station", "poi_id": "ST-M"}}).json()
    assert "station_end" not in body
    assert body["routes"][0]["steps"][-1]["instruction"] == "목적지에 도착했습니다."
