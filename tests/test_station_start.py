# -*- coding: utf-8 -*-
"""#79 (v1.28.0) — 역에서 출발하는 도보 경로: 역 근처 판정 → 역 안/밖 → 타고 온 방향 → 출구 안내.

서비스 밖(서울 방면)에서 전철로 와 역에서 도보 경로를 시작하면 경로에 지하철 구간이 없어
하차 안내가 붙지 않았다. 출발점이 역 가까이면 station_nearby 를 주고, 역 안이라고 답하면
origin_station 으로 다시 요청해 출구에서 계획하고 station_start 스텝을 맨 앞에 붙인다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from route_service.transit import exits as ex  # noqa: E402
from test_exits_category import GWANAK_FAC, client  # noqa: E402,F401  (fixture 재사용)

DEST = {"type": "tour", "poi_id": "TBF-1"}
NEAR_MYEONGHAK = {"lat": 37.3907, "lng": 126.9509}


# ── 순수 함수 ─────────────────────────────────────────────
def test_arrival_choices_name_both_sides():
    ch = {c["travel"]: c for c in ex.arrival_choices("관악역")}
    assert ch["south"]["label"] == "석수·서울 쪽에서 타고 왔어요" and ch["south"]["updown"] == "하행"
    assert ch["north"]["label"] == "안양·수원 쪽에서 타고 왔어요" and ch["north"]["updown"] == "상행"
    # 노선 끝 역은 노선표 바깥 이름을 쓴다
    sk = {c["travel"]: c["label"] for c in ex.arrival_choices("석수")}
    assert sk["south"] == "금천구청·서울 쪽에서 타고 왔어요"
    assert ex.arrival_choices("없는역") == []


def test_station_start_guide_uses_answered_direction():
    exit2 = {"exit_no": "2", "lat": 37.4189, "lng": 126.9092, "has_elevator": True,
             "elevator": "(1F) 2번 출구 옆", "lift": None}
    down = ex.station_start_guide("관악", "south", GWANAK_FAC, exit2)   # 서울 쪽에서 옴 → 하행 승강장
    assert down["inside"][0] == "내린 승강장의 승강기로 이동합니다 — (1F) 안양역 방향 승강장 진행방향 앞쪽 끝"
    assert down["inside"][-1] == "2번 출구 승강기로 나갑니다 — (1F) 2번 출구 옆"
    assert down["question"] is None and down["travel"] == "south"
    up = ex.station_start_guide("관악", "north", GWANAK_FAC, exit2)     # 안양 쪽에서 옴 → 상행(리프트뿐)
    assert up["inside"][0].startswith("내린 승강장 쪽에는 휠체어리프트만 있습니다")
    unk = ex.station_start_guide("관악", None, GWANAK_FAC, exit2)        # 방향 모름 — 단정하지 않는다
    assert unk["travel"] is None and unk["inside"][0].startswith("역 안 승강기 위치 — ")
    assert all(p["side"] == "unknown" for p in unk["platform"])


# ── API ───────────────────────────────────────────────────
def test_walk_near_station_offers_question(client):
    r = client.post("/route/plan", json={"origin": NEAR_MYEONGHAK, "destination": DEST})
    assert r.status_code == 200, r.text
    hint = r.json()["station_nearby"]
    assert hint["station"] == "명학" and hint["distance_m"] <= 150
    assert "역 안" in hint["question"] and hint["travel_question"]
    assert {c["travel"] for c in hint["choices"]} == {"north", "south"}
    # 역 근처 판정만으로 경로를 바꾸지 않는다 — 스텝은 종전과 같다
    assert r.json()["routes"][0]["steps"][0]["maneuver"] != "station_start"


def test_walk_far_from_station_has_no_hint(client, monkeypatch):
    import route_service.api.main as m
    monkeypatch.setattr(m, "STATION_NEAR_M", 5.0)
    monkeypatch.setattr(m, "STATION_FOOTPRINT_NEAR_M", 5.0)
    r = client.post("/route/plan", json={"origin": NEAR_MYEONGHAK, "destination": DEST})
    assert r.status_code == 200 and "station_nearby" not in r.json()


def test_inside_station_plans_from_exit_with_start_step(client):
    r = client.post("/route/plan", json={"origin": NEAR_MYEONGHAK, "destination": DEST,
                                          "origin_station": {"name": "명학역", "travel": "south"}})
    assert r.status_code == 200, r.text
    body = r.json()
    ss = body["station_start"]
    assert ss["station"] == "명학" and ss["travel"] == "south" and ss["exit"]["exit_no"] == "1"
    step = body["routes"][0]["steps"][0]
    assert step["maneuver"] == "station_start"
    assert step["instruction"] == "명학역 안에서 출발합니다. 1번 출구(승강기)로 나간 뒤 걸어서 이동합니다"
    assert step["coord"] == [37.3905, 126.9511]
    assert any("금정역 방향" in t for t in step["egress"]["inside"])          # 안양 쪽에서 왔다 = 하행
    assert body["origin"]["label"] == "명학역 1번 출구"
    assert "station_nearby" not in body
    north = client.post("/route/plan", json={"origin": NEAR_MYEONGHAK, "destination": DEST,
                                              "origin_station": {"name": "명학", "travel": "north"}}).json()
    assert any("안양역 방향" in t for t in north["station_start"]["egress"]["inside"])


def test_inside_station_bad_input(client):
    bad = client.post("/route/plan", json={"origin": NEAR_MYEONGHAK, "destination": DEST,
                                            "origin_station": {"name": "명학", "travel": "east"}})
    assert bad.status_code == 400
    nf = client.post("/route/plan", json={"origin": NEAR_MYEONGHAK, "destination": DEST,
                                           "origin_station": {"name": "없는역"}})
    assert nf.status_code == 404
    unk = client.post("/route/plan", json={"origin": NEAR_MYEONGHAK, "destination": DEST,
                                            "origin_station": {"name": "명학"}})
    assert unk.status_code == 200 and unk.json()["station_start"]["travel"] is None


def test_arrival_choices_line4():
    ch = {c["travel"]: c["label"] for c in ex.arrival_choices("평촌")}
    assert ch == {"south": "인덕원·사당 쪽에서 타고 왔어요", "north": "범계·오이도 쪽에서 타고 왔어요"}
    first = {c["travel"]: c["label"] for c in ex.arrival_choices("인덕원")}
    assert first["south"] == "과천·사당 쪽에서 타고 왔어요"


def test_start_step_reindexes_and_normalizes_travel(client):
    body = client.post("/route/plan", json={"origin": NEAR_MYEONGHAK, "destination": DEST,
                                             "origin_station": {"name": "명학", "travel": " SOUTH "}}).json()
    steps = body["routes"][0]["steps"]
    assert [s["idx"] for s in steps] == list(range(len(steps)))
    assert body["station_start"]["travel"] == "south"


def test_blocked_nearest_exit_falls_back_to_next(client, monkeypatch):
    import route_service.api.main as m
    from fastapi import HTTPException
    real = m._plan_core

    def fake(lat, lng, *a, **k):
        if (round(lat, 4), round(lng, 4)) == (37.3905, 126.9511):      # 1번 출구는 막혔다
            raise HTTPException(status_code=422, detail="막힘")
        return real(lat, lng, *a, **k)
    monkeypatch.setattr(m, "_plan_core", fake)
    body = client.post("/route/plan", json={"origin": NEAR_MYEONGHAK, "destination": DEST,
                                             "origin_station": {"name": "명학", "travel": "south"}}).json()
    assert body["station_start"]["exit"]["exit_no"] == "2"
    assert body["origin"]["label"] == "명학역 2번 출구"


def test_station_without_exit_data_still_gets_inside_step(client, monkeypatch):
    monkeypatch.setattr(ex, "_DATA", {})
    body = client.post("/route/plan", json={"origin": NEAR_MYEONGHAK, "destination": DEST,
                                             "origin_station": {"name": "명학", "travel": "south"}}).json()
    st = body["routes"][0]["steps"][0]
    assert st["maneuver"] == "station_start" and body["station_start"]["exit"] is None
    assert st["instruction"] == "명학역 안에서 출발합니다. 출구로 나간 뒤 걸어서 이동합니다"
    assert any("금정역 방향" in t for t in st["egress"]["inside"])
    # 출구 자료가 없으면 역 근처라도 묻지 않는다(안내할 출구가 없다)
    plain = client.post("/route/plan", json={"origin": NEAR_MYEONGHAK, "destination": DEST}).json()
    assert "station_nearby" not in plain


# ── 역 근처 판정 기준: 승강장 윤곽·출구에서 50m (v1.29.0) ──────────────────
# 역 중심 좌표는 역사 쪽에 찍혀 있어 명학·관악은 승강장 전체가 중심에서 60~140m 밖이다.
# 중심점 반경을 50m 로 줄이면 승강장 위에서도 묻지 않게 되므로, 승강장 윤곽과 출구를 기준으로 잰다.
GWANAK_CENTER = (37.419578, 126.908476)     # 운영 DB 역 좌표
GWANAK_PLATFORM_N_END = (37.42058, 126.90753)
GWANAK_PLATFORM_MID = (37.419745, 126.908215)
MYEONGHAK_PLATFORM_MID = (37.38429, 126.93577)


def test_footprint_uses_real_platform_outline():
    from route_service.engine.geo import haversine_m
    # 승강장 위 — 거리 0 에 가깝다. 역 중심에서는 100m 이상 떨어진 지점이다
    assert ex.footprint_distance_m("관악역", *GWANAK_PLATFORM_N_END) < 5
    assert haversine_m(*GWANAK_CENTER, *GWANAK_PLATFORM_N_END) > 130
    assert ex.footprint_distance_m("관악", *GWANAK_PLATFORM_MID) < 5
    assert ex.footprint_distance_m("명학", *MYEONGHAK_PLATFORM_MID) < 5
    # 출구 앞도 50m 안
    e1 = [e for e in ex.exits_for("명학") if e["exit_no"] == "1"][0]
    assert ex.footprint_distance_m("명학", e1["lat"], e1["lng"]) < 1
    # 승강장에서 동쪽으로 약 26m → 안 / 약 88m → 밖
    near = ex.footprint_distance_m("명학", MYEONGHAK_PLATFORM_MID[0], MYEONGHAK_PLATFORM_MID[1] + 0.0003)
    far = ex.footprint_distance_m("명학", MYEONGHAK_PLATFORM_MID[0], MYEONGHAK_PLATFORM_MID[1] + 0.0010)
    assert near <= 50 < far
    # 승강장 윤곽이 없는 역(4호선)은 None — 역 중심 기준으로 판정한다
    assert ex.footprint_distance_m("범계", 37.389793, 126.950876) is None


def test_walk_hint_uses_platform_outline_when_available(client, monkeypatch):
    lat, lng = NEAR_MYEONGHAK["lat"], NEAR_MYEONGHAK["lng"]
    d = 0.0001
    ring = [[lat - d, lng - d], [lat - d, lng + d], [lat + d, lng + d], [lat + d, lng - d], [lat - d, lng - d]]
    monkeypatch.setattr(ex, "_PLATFORMS", {"명학": [{"ring": ring}]})
    r = client.post("/route/plan", json={"origin": NEAR_MYEONGHAK, "destination": DEST})
    hint = r.json()["station_nearby"]
    assert hint["station"] == "명학" and hint["basis"] == "platform" and hint["distance_m"] == 0
    # 역 중심(합성 역)에서는 150m 안이지만 승강장·출구에서 50m 밖 → 묻지 않는다
    import route_service.api.main as m
    from route_service.engine.geo import haversine_m
    off = (lat, lng + 0.0014)                  # 승강장에서 약 120m 동쪽 (합성 보행망 밖이라 판정 함수로 확인)
    assert haversine_m(37.3908, 126.9510, *off) < m.STATION_NEAR_M
    assert m._station_nearby_hint(off[0], off[1], "wheelchair_electric") is None
    assert m._station_nearby_hint(lat, lng, "wheelchair_electric")["basis"] == "platform"


def test_walk_hint_falls_back_to_center_without_outline(client):
    import route_service.api.main as m
    r = client.post("/route/plan", json={"origin": NEAR_MYEONGHAK, "destination": DEST})
    assert r.json()["station_nearby"]["basis"] == "exit"            # 윤곽 없음 — 출구 약 26m
    h = m._station_nearby_hint(37.3918, 126.9510, "wheelchair_electric")   # 출구 100m 밖·중심 111m
    assert h and h["basis"] == "center"


def test_egress_carries_platform_area():
    exit2 = {"exit_no": "2", "lat": 37.4189, "lng": 126.9092, "has_elevator": True,
             "elevator": "(1F) 2번 출구 옆", "lift": None}
    g = ex.station_start_guide("관악", "south", GWANAK_FAC, exit2)
    assert len(g["area"]) == 4 and all(len(r) >= 4 for r in g["area"])       # 실제 승강장 윤곽
    assert ex.egress_guide("범계", "", {}, None)["area"] == []                  # 윤곽 없는 역


def test_footprint_handles_unclosed_ring(monkeypatch):
    # 첫 점을 끝에 다시 두지 않은 고리 — 닫는 변(마지막 점 → 첫 점)도 거리에 넣는다
    ring = [[37.0000, 127.0000], [37.0000, 127.0010], [37.0010, 127.0010], [37.0010, 127.0000]]
    monkeypatch.setattr(ex, "_PLATFORMS", {"가상": [{"ring": ring}]})
    monkeypatch.setattr(ex, "_DATA", {"가상": []})
    d = ex.footprint_distance_m("가상", 37.0005, 126.9998)       # 서쪽 변(닫는 변) 바로 옆 약 18m
    assert d is not None and d < 25


def test_no_outline_station_asks_near_exit(client, monkeypatch):
    import route_service.api.main as m
    # 합성 역: 윤곽 없음. 출구 1 (37.3905,126.9511) 에서 약 30m, 역 중심에서는 150m 밖이 되도록 중심 기준을 줄여 본다
    monkeypatch.setattr(m, "STATION_NEAR_M", 5.0)
    h = m._station_nearby_hint(37.3907, 126.9513, "wheelchair_electric")
    assert h and h["station"] == "명학" and h["basis"] == "exit" and h["distance_m"] <= 50
    assert m._station_nearby_hint(37.3930, 126.9540, "wheelchair_electric") is None
