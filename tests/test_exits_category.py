# -*- coding: utf-8 -*-
"""#77 (v1.27.0) — 역 출구 기준 도보 leg·하차 안내, 관광 분류 추천, 시설 내 화장실, 관리기관 번호, 요청 출처 태그.

  1) 출구 후보(승강기 있는 출구만)·하차 승강장 쪽 판정·역 안/밖 안내 문장
  2) walk_subway 응답: 하차 출구 선택·출구 통과 스텝·egress, 도보 leg 가 출구에서 시작
  3) 추천: category=tour 는 상점·식당 제외 + 상위 등급 먼저, category=all 은 종전 거리순
  4) 화장실: 시설 내 장애인화장실 합류(공중화장실과 겹치면 공중화장실만)
  5) 충전기 전화번호 = 관리기관 번호 표시
  6) X-Client-Tag → 계측 행 client, /meta/latency nearest-rank·오류 분리·client 필터
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from route_service import metrics as mt  # noqa: E402
from route_service.transit import exits as ex  # noqa: E402

GWANAK_FAC = {
    "elevators": [
        {"exit_no": "내부", "detail_loc": "(1F) 안양역 방향 승강장 진행방향 앞쪽 끝"},
        {"exit_no": "2", "detail_loc": "(1F) 2번 출구 옆"},
        {"exit_no": "1", "detail_loc": "(1F) 1번 출구 옆"},
    ],
    "lifts": [{"exit_no": "내부", "detail_loc": "1F(석수역 방향 상행승강장 계단 옆)"}],
}


# ── 1. 순수 함수 ─────────────────────────────────────────────
def test_exit_options_keep_only_elevator_exits_for_wheelchair(monkeypatch):
    monkeypatch.setattr(ex, "_DATA", {"테스트": [
        {"exit_no": "1", "lat": 37.0, "lng": 127.0},
        {"exit_no": "2", "lat": 37.001, "lng": 127.0},
        {"exit_no": "3", "lat": 37.002, "lng": 127.0},
    ]})
    fac = {"elevators": [{"exit_no": "2번", "detail_loc": "2번 출구 옆"}], "lifts": []}
    wheel = ex.exit_options("테스트역", fac, wheelchair=True)
    assert [e["exit_no"] for e in wheel] == ["2"]
    assert wheel[0]["has_elevator"] is True and wheel[0]["elevator"] == "2번 출구 옆"
    walk = ex.exit_options("테스트", fac, wheelchair=False)
    assert len(walk) == 3
    # 출구별 승강기 자료가 없는 역은 전 출구가 후보, 승강기 여부는 모름(None)
    none_fac = ex.exit_options("테스트", {}, wheelchair=True)
    assert len(none_fac) == 3 and all(e["has_elevator"] is None for e in none_fac)


def test_platform_side_follows_travel_direction():
    plat = ex.platform_facilities("안양", "관악", GWANAK_FAC)
    side = {p["detail_loc"]: p["side"] for p in plat}
    assert side["(1F) 안양역 방향 승강장 진행방향 앞쪽 끝"] == "opposite"
    assert side["1F(석수역 방향 상행승강장 계단 옆)"] == "arrival"
    assert all(p["detail_loc"] not in ("(1F) 2번 출구 옆", "(1F) 1번 출구 옆") for p in plat)
    # 반대 방향(석수 → 관악)으로 오면 판정이 뒤집힌다
    rev = {p["detail_loc"]: p["side"] for p in ex.platform_facilities("석수", "관악", GWANAK_FAC)}
    assert rev["(1F) 안양역 방향 승강장 진행방향 앞쪽 끝"] == "arrival"


def test_egress_guide_inside_and_outside_sentences():
    sel = {"exit_no": "2", "lat": 37.4189, "lng": 126.9092, "has_elevator": True,
           "elevator": "(1F) 2번 출구 옆", "lift": None}
    g = ex.egress_guide("관악역", "안양", GWANAK_FAC, sel)
    assert g["station"] == "관악" and g["exit"]["exit_no"] == "2"
    assert "휠체어리프트만" in g["inside"][0]           # 하차 승강장 쪽은 리프트뿐
    assert g["inside"][-1].startswith("2번 출구 승강기로 나갑니다")
    assert "2번 출구 앞에서" in g["outside"]
    assert "역 안" in g["question"] and "역 밖" in g["question"]


def test_platform_side_unknown_when_not_certain():
    fac = {"elevators": [
        {"exit_no": "내부", "detail_loc": "서울 방면 승강장 5-1"},        # 북쪽 종착 이름 → 안양→관악(북행) 하차 쪽
        {"exit_no": "내부", "detail_loc": "수원 방면 승강장 3-2"},        # 남쪽 → 반대편
        {"exit_no": "내부", "detail_loc": "하행 승강장 끝"},              # 하행 = 남쪽 → 반대편
        {"exit_no": "내부", "detail_loc": "대합실 중앙"},                 # 방향 없음 → 모름
        {"exit_no": "내부", "detail_loc": "서울·수원 방면 공용"}],         # 양쪽 → 모름
        "lifts": []}
    side = {p["detail_loc"]: p["side"] for p in ex.platform_facilities("안양", "관악", fac)}
    assert side["서울 방면 승강장 5-1"] == "arrival"
    assert side["수원 방면 승강장 3-2"] == "opposite"
    assert side["하행 승강장 끝"] == "opposite"
    assert side["대합실 중앙"] == "unknown"
    assert side["서울·수원 방면 공용"] == "unknown"
    # 노선 판정이 안 되면(다른 노선) 전부 모름
    assert all(p["side"] == "unknown" for p in ex.platform_facilities("평촌", "관악", GWANAK_FAC))


def test_lift_only_station_prefers_lift_exits(monkeypatch):
    monkeypatch.setattr(ex, "_DATA", {"리프트": [
        {"exit_no": "1", "lat": 37.0, "lng": 127.0}, {"exit_no": "2", "lat": 37.001, "lng": 127.0}]})
    fac = {"elevators": [], "lifts": [{"exit_no": "2", "detail_loc": "2번 출구 계단 옆"}]}
    got = ex.exit_options("리프트역", fac, wheelchair=True)
    assert [e["exit_no"] for e in got] == ["2"] and got[0]["lift"] == "2번 출구 계단 옆"


def test_manager_name_without_parenthesis_and_festival_category():
    from route_service.poi.support import _tel_owner
    from route_service.poi.store import tour_category
    assert _tel_owner("STD_WCHAIR_CHARGER", "관리기관: 밀알보장구수리센터 031-381-6103 | 동시사용 2대")["name"] == "밀알보장구수리센터"
    assert _tel_owner("STD_WCHAIR_CHARGER", "관리기관: 온누리 부흥센터")["name"] == "온누리 부흥센터"
    assert tour_category({"sf_tourist_spot": "문화관광축제,지역축제"})[0] == "event"
    assert tour_category({"sf_tourist_spot": "도시공원,주제공원"})[0] == "tour"
    assert tour_category({"sf_shopping": "시장,상설시장"})[0] == "shopping"


# ── 공통 픽스처 ─────────────────────────────────────────────
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
    W = lambda name, rows: (poi_dir / name).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    W("tour_bf.json", [
        {"poi_id": "TBF-1", "name": "테스트 무장애 공원", "addr": "경기도 안양시 만안구 테스트로 1",
         "latitude": 37.3909, "longitude": 126.9511,
         "toilet_yn": "Y", "elevator_yn": "Y", "slope_yn": "Y", "parking_yn": "Y",
         "entrance": {"lat": 37.3909, "lng": 126.9511}},
        {"poi_id": "FOOD-1", "name": "가까운 식당", "addr": "경기도 안양시 만안구 테스트로 2",
         "latitude": 37.3900, "longitude": 126.9501, "slope_yn": "Y",
         "sf_restaurant": "한식"},
        {"poi_id": "PARK-2", "name": "가까운 작은 공원", "addr": "경기도 안양시 만안구 테스트로 3",
         "latitude": 37.3901, "longitude": 126.9502, "slope_yn": "Y",
         "sf_tourist_spot": "도시공원,시민공원"},
        {"poi_id": "FEST-1", "name": "축제", "addr": "경기도 안양시 만안구 테스트로 4",
         "latitude": 37.3902, "longitude": 126.9502, "dis_toilet_yn": "Y", "elevator_yn": "Y",
         "slope_yn": "Y", "sf_tourist_spot": "축제,문화예술축제"},
    ])
    W("stations.json", [
        {"poi_id": "ST-A", "name": "안양", "latitude": 37.3901, "longitude": 126.9502,
         "elevator_cnt": 4, "wheelchair_lift_cnt": 0},
        {"poi_id": "ST-M", "name": "명학", "latitude": 37.3908, "longitude": 126.9510,
         "elevator_cnt": 2, "wheelchair_lift_cnt": 0},
    ])
    W("transit_stops.json", [])
    W("station_facilities.json", [
        {"stn_cd": "ST-M", "stn_name": "명학", "elevator_cnt": 2, "dis_toilet_yn": "Y",
         "elevators": [{"exit_no": "1", "detail_loc": "1번 출입구 옆"},
                       {"exit_no": "2", "detail_loc": "2번 출입구 옆"},
                       {"exit_no": "내부", "detail_loc": "(1F) 안양역 방향 승강장 1-1"},
                       {"exit_no": "내부", "detail_loc": "(1F) 금정역 방향 승강장 10-4"}]},
    ])
    W("public_toilets.json", [
        {"toilet_id": 1, "toilet_name": "테스트 무장애 공원", "toilet_type": "개방화장실",
         "m_dis_toilet_count": 1, "f_dis_toilet_count": 1, "latitude": 37.3909, "longitude": 126.9511},
    ])
    W("tour_bf_facility.json", [
        {"fclt_id": 42, "fclt_name": "테스트 박물관", "toilet_yn": "Y",
         "latitude": 37.3905, "longitude": 126.9506, "restroom": "장애인 전용 화장실 있음"},
        {"fclt_id": 43, "fclt_name": "테스트 무장애 공원", "toilet_yn": "Y",   # 공중화장실과 같은 곳
         "latitude": 37.3909, "longitude": 126.9511},
        {"fclt_id": 44, "fclt_name": "화장실 없는 곳", "toilet_yn": "N",
         "latitude": 37.3903, "longitude": 126.9503},
    ])
    W("emergency_support.json", [
        {"support_id": 1, "support_type": "charge", "name": "시청", "latitude": 37.3903, "longitude": 126.9504,
         "tel": "031-455-1313", "source": "STD_WCHAIR_CHARGER",
         "note": "관리기관: 온누리 부흥센터 031-455-1313 (전화번호는 관리기관 번호, 설치장소 직통 아님) | 동시사용 2대"},
        {"support_id": 2, "support_type": "repair", "name": "수리점", "latitude": 37.3904, "longitude": 126.9504,
         "tel": "031-111-1111", "source": "GG_ASSIST_REPAIR"},
    ])
    log = tmp_path / "metrics" / "events.jsonl"
    monkeypatch.setenv("NETWORK_PATH", str(net))
    monkeypatch.setenv("NETWORK_VERSION", "test-1")
    monkeypatch.setenv("POI_BACKEND", "file")
    monkeypatch.setenv("POI_DATA_DIR", str(poi_dir))
    monkeypatch.setenv("ROUTE_API_TOKEN", "")
    monkeypatch.setenv("METRICS_LOG_PATH", str(log))
    # 합성 보행망 위에 출구를 둔다 — 명학 1번(목적지 쪽), 2번(반대편), 안양 1번
    monkeypatch.setattr(ex, "_DATA", {
        "명학": [{"exit_no": "1", "lat": 37.3905, "lng": 126.9511},
                 {"exit_no": "2", "lat": 37.3900, "lng": 126.9500}],
        "안양": [{"exit_no": "1", "lat": 37.3900, "lng": 126.9503}],
    })

    import route_service.config as cfg
    importlib.reload(cfg)
    cfg._settings = None
    import route_service.api.main as m
    importlib.reload(m)
    m._FAC_CACHE.clear()
    with TestClient(m.app) as c:
        c.log_path = log
        yield c


def _rows(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


# ── 2. walk_subway 출구 ─────────────────────────────────────
def test_walk_subway_uses_exit_nearest_destination(client):
    r = client.post("/route/plan", json={
        "origin": {"lat": 37.3901, "lng": 126.9501},
        "destination": {"type": "tour", "poi_id": "TBF-1"},
        "profile": "wheelchair_electric", "mode": "walk_subway"})
    assert r.status_code == 200, r.text
    route = r.json()["routes"][0]
    sub = [l for l in route["legs"] if l["kind"] == "subway"][0]
    assert sub["alight"]["name"] == "명학"
    assert sub["alight_exit"]["exit_no"] == "1" and sub["alight_exit"]["has_elevator"] is True
    eg = sub["egress"]
    assert eg["exit"]["exit_no"] == "1"
    assert any("금정역 방향" in line for line in eg["inside"])     # 하차 승강장 쪽 승강기
    assert eg["inside"][-1].startswith("1번 출구 승강기로 나갑니다")
    mans = [s["maneuver"] for s in route["steps"]]
    i = mans.index("subway_alight")
    assert mans[i + 1] == "station_exit"
    alight, exit_step = route["steps"][i], route["steps"][i + 1]
    assert "1번 출구(승강기)로 나갑니다" in alight["instruction"]
    assert alight["egress"]["question"] and exit_step["egress"]["exit"]["exit_no"] == "1"
    assert exit_step["coord"] == [37.3905, 126.9511]
    last_walk = [l for l in route["legs"] if l["kind"] == "walk"][-1]
    assert last_walk["from_label"] == "명학역 1번 출구"


# ── 3. 추천 분류·순위 ─────────────────────────────────────────
def test_recommend_tour_category_excludes_food_and_ranks_tier_first(client):
    body = client.post("/tour/recommend", json={
        "disabilities": ["지체장애"], "topk": 10,
        "origin_lat": 37.3900, "origin_lng": 126.9500}).json()
    ids = [i["poi_id"] for i in body["items"]]
    assert "FOOD-1" not in ids and "FEST-1" not in ids
    assert ids == ["TBF-1", "PARK-2"], ids          # 상위 등급(먼 공원)이 가까운 저충족 공원보다 먼저
    assert body["items"][0]["tier"] == 1 and body["items"][1]["tier"] == 2
    assert body["items"][0]["category"] == "tour" and body["items"][0]["category_label"] == "관광지"
    legacy = client.post("/tour/recommend", json={
        "disabilities": ["지체장애"], "topk": 10, "category": "all",
        "origin_lat": 37.3900, "origin_lng": 126.9500}).json()
    assert [i["poi_id"] for i in legacy["items"]][0] == "FOOD-1"   # 종전: 순수 거리순
    ev = [x for x in _rows(client.log_path) if x["kind"] == "recommend"]
    assert ev[0]["category"] == "tour" and ev[1]["category"] == "all"


def test_recommend_unknown_category_400(client):
    r = client.post("/tour/recommend", json={"disabilities": ["지체장애"], "category": "zzz"})
    assert r.status_code == 400


# ── 4·5. 화장실·관리기관 번호 ─────────────────────────────────
def test_toilet_merges_facility_toilets_without_duplicates(client):
    body = client.get("/toilet/nearby", params={"lat": 37.3905, "lng": 126.9506, "radius_m": 800}).json()
    names = [i["name"] for i in body["items"]]
    assert "테스트 박물관 (시설 내 장애인화장실)" in names
    assert "테스트 무장애 공원 (시설 내 장애인화장실)" not in names   # 공중화장실과 같은 곳
    fac = [i for i in body["items"] if i.get("facility_toilet")][0]
    assert fac["source"] == "KTO_BF" and fac["open_time"] == "시설 운영시간 내"
    pub = [i for i in body["items"] if i["source"] == "PUBLIC_TOILET"]
    assert pub and pub[0]["dis_male_cnt"] == 1


def test_charger_phone_is_marked_as_manager(client):
    items = client.get("/support/nearby", params={"lat": 37.3903, "lng": 126.9504}).json()["items"]
    ch = [i for i in items if i["support_type"] == "charge"][0]
    rp = [i for i in items if i["support_type"] == "repair"][0]
    assert ch["tel_owner"] == "manager" and ch["tel_owner_name"] == "온누리 부흥센터"
    assert rp["tel_owner"] == "site" and rp["tel_owner_name"] is None


# ── 6. 요청 출처 태그·백분위 ───────────────────────────────────
def test_client_tag_recorded_and_sanitized(client):
    client.get("/health", headers={"X-Client-Tag": "trial_p1<script>"})
    client.get("/health")
    rows = [x for x in _rows(client.log_path) if x["kind"] == "request" and x["path"] == "/health"]
    assert rows[0]["client"] == "trial_p1script"
    assert "client" not in rows[1]
    only = client.get("/meta/latency", params={"client": "trial_p1script"}).json()
    assert only["paths"]["/health"]["count"] == 1


def test_nearest_rank_percentile_and_error_split():
    assert mt.nearest_rank([], 0.95) is None
    assert mt.nearest_rank([10.0, 200.0], 0.95) == 200.0      # 종전 floor 방식은 10.0
    assert mt.nearest_rank(sorted([float(i) for i in range(1, 21)]), 0.95) == 19.0
    m = mt.Metrics(enabled=False)
    m.recent.extend([
        {"kind": "request", "ts": 1e12, "path": "/x", "status": 200, "ms": 100.0},
        {"kind": "request", "ts": 1e12, "path": "/x", "status": 400, "ms": 0.5},
        {"kind": "request", "ts": 1e12, "path": "/x", "status": 200, "ms": 300.0},
    ])
    s = m.summary()["paths"]["/x"]
    assert s["count"] == 2 and s["error_cnt"] == 1 and s["p95_ms"] == 300.0 and s["p50_ms"] == 100.0
