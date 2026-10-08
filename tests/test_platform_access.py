# -*- coding: utf-8 -*-
"""v1.35.0 — 리프트만 있는 승강장: 경로 단계 경고·판독용 표시·후보 감점, 역 안 출발 문장.

2026-10-08 운영 서버 실측: 안양 → 김중업건축박물관 walk_subway 는 안양에서 1호선(북행)을 타고 관악역에서
내리게 안내했다. 관악역 상행(석수 방향) 승강장에는 승강기가 없고 휠체어리프트(폭 800mm·길이 1,100mm)만
있는데, 경로 응답에는 아무 경고가 없었다. 하차 뒤 역 안 안내에서야 "리프트만 있다"고 알려 주었다.

여기서 확인하는 것
  1) 하차 승강장이 리프트뿐이면 스텝·요약 경고 + platform_access 표시, 그리고 감점으로 다른 하차역 후보가 이긴다
  2) 모든 후보가 리프트뿐이면 가장 나은 후보를 경고와 함께 남긴다
  3) 승차 승강장이 리프트뿐인 경우도 같은 규칙
  4) 시각장애 프로필은 경고·감점 없이 종전 선택 그대로
  5) 역 안 출발(station_start) 문장에 리프트 위치·크기·할 일이 들어간다
  6) 저상버스 우선 모드(walk_bus_subway, 휠체어 기본 on)의 시간 키에도 감점이 들어간다

합성 보행망(conftest.make_graph)은 N1(37.3900,126.9500) ~ N3(37.3909,126.9511) 의 작은 망이다.
역 좌표도 합성이다 — 노선 위상(LINES)의 역 이름과 방향만 실제와 같게 둔다.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from route_service.transit import exits as ex  # noqa: E402

ORIGIN = {"lat": 37.3900, "lng": 126.9500}
DEST = {"type": "tour", "poi_id": "TBF-1"}          # N3(37.3909, 126.9511)

# 관악역 실제 설비 문구(운영 DB) — 상행(석수 방향) 승강장은 리프트뿐, 승강기는 하행(안양 방향) 승강장과 출구에만
GWANAK_ROW = {
    "stn_cd": "ST-G", "stn_name": "관악", "elevator_cnt": 3, "wheelchair_lift_cnt": 1,
    "elevators": [
        {"exit_no": "내부", "detail_loc": "(1F) 안양역 방향 승강장 진행방향 앞쪽 끝"},
        {"exit_no": "2", "detail_loc": "(1F) 2번 출입구 옆"},
    ],
    "lifts": [{"mng_no": "1", "exit_no": "내부", "detail_loc": "1F(석수역 방향 상행승강장 계단 옆)",
               "width_mm": 800, "length_mm": 1100, "start_floor": "1F", "end_floor": "1F"}],
}
MYEONGHAK_ROW = {
    "stn_cd": "ST-M", "stn_name": "명학", "elevator_cnt": 4,
    "elevators": [{"exit_no": "내부", "detail_loc": "(1F) 안양역 방향 승강장 1-1"},
                  {"exit_no": "내부", "detail_loc": "(1F) 금정역 방향 승강장 10-4"}],
}
ANYANG_ROW = {
    "stn_cd": "ST-A", "stn_name": "안양", "elevator_cnt": 4,
    "elevators": [{"exit_no": "내부", "detail_loc": "(1F) 관악역 방향 승강장"},
                  {"exit_no": "내부", "detail_loc": "(1F) 명학역 방향 승강장"}],
}
SEOKSU_ROW = {
    "stn_cd": "ST-S", "stn_name": "석수", "elevator_cnt": 2,
    "elevators": [{"exit_no": "내부", "detail_loc": "(1F) 금천구청역 방향 승강장"},
                  {"exit_no": "내부", "detail_loc": "(1F) 관악역 방향 승강장"}],
}

EXPECT_ALIGHT_WARN = ("관악역 하차 승강장(석수 방향)에는 대합실로 이어지는 승강기가 없고 휠체어리프트만 있습니다"
                      "(폭 800mm·길이 1,100mm). 승강장의 리프트 호출 버튼을 누르거나 역무실에 연락해 역무원을 불러 주세요.")
EXPECT_BOARD_WARN = ("관악역 승차 승강장(석수 방향)에는 대합실로 이어지는 승강기가 없고 휠체어리프트만 있습니다"
                     "(폭 800mm·길이 1,100mm). 승강장의 리프트 호출 버튼을 누르거나 역무실에 연락해 역무원을 불러 주세요.")
# 승차 스텝에서 내릴 역 리프트를 미리 알리는 짧은 문장 — 전체 문장은 하차 스텝·경로 요약에만
EXPECT_ALIGHT_SHORT = "내릴 관악역 승강장(석수 방향)에는 휠체어리프트만 있습니다."


def _make_client(tmp_path, monkeypatch, stations, facilities):
    """합성 보행망 + 파일 백엔드 POI 로 API 클라이언트를 만든다(test_exits_category 의 client 와 같은 방식).

    역 출구 자료는 비운다 — 출구가 없는 역은 역 중심을 도보 leg 끝점으로 쓴다(_walk_leg_via_exit).
    """
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
         "latitude": 37.3909, "longitude": 126.9511, "slope_yn": "Y",
         "entrance": {"lat": 37.3909, "lng": 126.9511}},
        {"poi_id": "TBF-0", "name": "출발점 옆 공원", "addr": "경기도 안양시 만안구 테스트로 0",
         "latitude": 37.3900, "longitude": 126.9500, "slope_yn": "Y",
         "entrance": {"lat": 37.3900, "lng": 126.9500}},
    ])
    W("stations.json", stations)
    W("transit_stops.json", [])
    W("station_facilities.json", facilities)
    log = tmp_path / "metrics" / "events.jsonl"
    monkeypatch.setenv("NETWORK_PATH", str(net))
    monkeypatch.setenv("NETWORK_VERSION", "test-1")
    monkeypatch.setenv("POI_BACKEND", "file")
    monkeypatch.setenv("POI_DATA_DIR", str(poi_dir))
    monkeypatch.setenv("ROUTE_API_TOKEN", "")
    monkeypatch.setenv("METRICS_LOG_PATH", str(log))
    monkeypatch.setattr(ex, "_PLATFORMS", {})
    monkeypatch.setattr(ex, "_DATA", {})

    import route_service.config as cfg
    importlib.reload(cfg)
    cfg._settings = None
    import route_service.api.main as m
    importlib.reload(m)
    m._FAC_CACHE.clear()
    # 역 탐색 반경을 60m 로 줄인다. 합성 망은 150m 남짓이라 기본 반경(3km·700m)이면 모든 역이 출발·도착
    # 양쪽 후보가 되어 "목적지 쪽 역에서 타고 출발지 쪽 역에서 내리는" 역방향 조합까지 같은 도보 거리로
    # 경쟁한다. 실제 지리에서는 역방향 조합의 도보가 길어 지는 일이 없으므로, 여기서는 반경으로 뺀다.
    monkeypatch.setattr(m.transit, "STATION_RADIUS_SUBWAY_ONLY_M", 60)
    monkeypatch.setattr(m.transit, "STATION_RADIUS_M", 60)
    return m, TestClient(m.app)


def _st(pid, name, lat, lng):
    return {"poi_id": pid, "name": name, "latitude": lat, "longitude": lng,
            "elevator_cnt": 2, "wheelchair_lift_cnt": 0}


# 안양(출발점 옆) → 관악(목적지 바로 위, 북행 = 상행 승강장 = 리프트뿐) 또는 명학(목적지에서 약 44m, 남행 = 승강기 있음)
ST_ANYANG = _st("ST-A", "안양", 37.3900, 126.9501)
ST_GWANAK_AT_DEST = _st("ST-G", "관악", 37.3909, 126.9511)
ST_MYEONGHAK_NEAR = _st("ST-M", "명학", 37.3905, 126.9511)


def _plan(c, profile="wheelchair_electric", mode="walk_subway", origin=ORIGIN, dest=DEST):
    r = c.post("/route/plan", json={"origin": origin, "destination": dest,
                                     "profile": profile, "mode": mode})
    assert r.status_code == 200, r.text
    return r.json()


def _subway(body):
    return [l for l in body["routes"][0]["legs"] if l["kind"] == "subway"][0]


def _step(body, maneuver):
    return [s for s in body["routes"][0]["steps"] if s["maneuver"] == maneuver][0]


# ── 순수 함수 ─────────────────────────────────────────────
def test_platform_access_judgement_follows_travel_direction():
    up = ex.platform_access("관악역", "north", GWANAK_ROW, "alight")
    assert up["access"] == "lift_only" and up["updown"] == "상행" and up["toward"] == "석수"
    assert up["lift"] == {"detail_loc": "1F(석수역 방향 상행승강장 계단 옆)", "width_mm": 800, "length_mm": 1100}
    down = ex.platform_access("관악", "south", GWANAK_ROW, "board")
    assert down["access"] == "elevator" and down["toward"] == "안양" and down["lift"] is None
    assert down["elevator"] == "(1F) 안양역 방향 승강장 진행방향 앞쪽 끝"
    assert ex.platform_access("관악", None, GWANAK_ROW) is None                 # 방향 모름 — 판정하지 않는다
    assert ex.platform_access("없는역", "north", GWANAK_ROW) is None
    assert ex.platform_access("관악", "north", {})["access"] == "unknown"       # 설비 자료 없음 — 단정하지 않는다
    assert ex.subway_travel("안양", "관악역") == ("1호선", "north")
    assert ex.subway_travel("관악", "안양") == ("1호선", "south")
    assert ex.subway_travel("평촌", "관악") is None
    assert ex.subway_travel("안양", "관악", line="4호선") is None       # 그 노선에 없는 역 — 판정하지 않는다


def test_lift_warning_text_and_size_from_data_only():
    pa = ex.platform_access("관악", "north", GWANAK_ROW, "alight")
    assert ex.lift_only_warning(pa) == EXPECT_ALIGHT_WARN
    assert ex.lift_only_warning(dict(pa, side="board")) == EXPECT_BOARD_WARN
    # 크기 자료가 없으면 숫자를 지어내지 않는다
    no_size = dict(pa, lift={"detail_loc": "x", "width_mm": None, "length_mm": None})
    assert "mm" not in ex.lift_only_warning(no_size)
    assert ex.lift_size_text({"width_mm": 900}) == "폭 900mm"
    assert ex.lift_size_text(None) == ""
    assert ex.lift_only_short(pa) == EXPECT_ALIGHT_SHORT
    # 하차 스텝의 "2번 출구(승강기)로 나갑니다"와 모순처럼 들리지 않게 — 없는 것은 승강장↔대합실 승강기다
    assert "대합실로 이어지는 승강기가 없고" in ex.lift_only_warning(pa)


def test_toward_name_unknown_line_or_index_is_none():
    assert ex._toward_name("없는선", 0, "north") is None
    assert ex._toward_name("1호선", 99, "south") is None
    assert ex._toward_name("1호선", 0, "north") == "금천구청"       # 노선표 끝 — 바깥 첫 이름


@pytest.fixture()
def interchange_lines():
    """관악·안양이 두 노선에 함께 있는 가상 노선표 — '가상선'이 1호선보다 **먼저** 오고 순서가 반대다.

    _line_of(역 이름)는 처음 맞는 노선(가상선)을 고르므로, 노선을 넘기지 않으면 안양 → 관악(1호선 북행)을
    가상선 기준 남행으로 뒤집어 판정한다. LINES 는 exits·planner 가 같은 dict 를 쓰므로 제자리에서 바꾸고 되돌린다.
    """
    from route_service.transit import planner as tp
    saved = dict(tp.LINES)
    tp.LINES.clear()
    tp.LINES.update({"가상선": ["안양", "관악"]})
    tp.LINES.update(saved)
    yield
    tp.LINES.clear()
    tp.LINES.update(saved)


def test_interchange_station_uses_leg_line(interchange_lines):
    # 노선을 넘기지 않으면 가상선 기준으로 판정해 방향이 뒤집힌다(이 픽스처가 실제로 오판을 만드는지 확인)
    assert ex.subway_travel("안양", "관악") == ("가상선", "south")
    # leg 의 노선(1호선)을 넘기면 1호선 순서로 북행 — 상행 승강장의 리프트를 찾는다
    assert ex.subway_travel("안양", "관악", line="1호선") == ("1호선", "north")
    pa = ex.platform_access("관악", "north", GWANAK_ROW, "alight", line="1호선")
    assert pa["line"] == "1호선" and pa["access"] == "lift_only" and pa["toward"] == "석수"
    eg = ex.egress_guide("관악", "안양", GWANAK_ROW, None, line="1호선")
    assert eg["platform_access"]["access"] == "lift_only"
    assert eg["inside"][0].startswith("내린 승강장 쪽에는 휠체어리프트만 있습니다")


def test_station_start_lift_text_has_location_size_and_action():
    exit2 = {"exit_no": "2", "lat": 37.4189, "lng": 126.9092, "has_elevator": True,
             "elevator": "(1F) 2번 출입구 옆", "lift": None}
    up = ex.station_start_guide("관악", "north", GWANAK_ROW, exit2)
    assert up["inside"][0] == ("내린 승강장 쪽에는 휠체어리프트만 있습니다 — 1F(석수역 방향 상행승강장 계단 옆)"
                               "(폭 800mm·길이 1,100mm). 승강장의 리프트 호출 버튼을 누르거나 역무실에 연락해 "
                               "역무원을 불러 주세요")
    assert up["platform_access"]["access"] == "lift_only"
    # 승강기 쪽 문장은 종전 그대로
    down = ex.station_start_guide("관악", "south", GWANAK_ROW, exit2)
    assert down["inside"][0] == "내린 승강장의 승강기로 이동합니다 — (1F) 안양역 방향 승강장 진행방향 앞쪽 끝"
    assert down["platform_access"]["access"] == "elevator"


# ── API: 하차 승강장 리프트뿐 ───────────────────────────────
def test_alight_lift_only_loses_to_alternative_with_elevator(tmp_path, monkeypatch):
    m, c = _make_client(tmp_path, monkeypatch, [ST_ANYANG, ST_GWANAK_AT_DEST, ST_MYEONGHAK_NEAR],
                        [GWANAK_ROW, MYEONGHAK_ROW, ANYANG_ROW])
    with c:
        body = _plan(c)
        sub = _subway(body)
        assert sub["alight"]["name"] == "명학", "리프트뿐인 관악 하차가 감점되어 명학 하차가 이겨야 한다"
        assert sub["platform_access"]["alight"]["access"] == "elevator"
        summ = body["routes"][0]["summary"]
        assert summ["platform_access"] == [] and summ["platform_access_penalty_m"] == 0
        assert not any("휠체어리프트만" in w for w in summ["warnings"])
        # 이 시나리오가 실제로 감점을 가려내는지 — 감점을 끄면 도보가 짧은 관악 하차가 이긴다
        monkeypatch.setattr(m, "LIFT_ONLY_PLATFORM_PENALTY_M", 0)
        m._FAC_CACHE.clear()
        assert _subway(_plan(c))["alight"]["name"] == "관악"


def test_all_candidates_lift_only_keeps_best_with_warning(tmp_path, monkeypatch):
    m, c = _make_client(tmp_path, monkeypatch, [ST_ANYANG, ST_GWANAK_AT_DEST],
                        [GWANAK_ROW, ANYANG_ROW])
    with c:
        body = _plan(c)
    sub = _subway(body)
    assert sub["board"]["name"] == "안양" and sub["alight"]["name"] == "관악"
    pa = sub["platform_access"]["alight"]
    assert pa["access"] == "lift_only" and pa["side"] == "alight" and pa["station"] == "관악"
    assert pa["lift"]["width_mm"] == 800 and pa["lift"]["length_mm"] == 1100
    assert pa["warning"] == EXPECT_ALIGHT_WARN
    assert sub["platform_access"]["board"]["access"] == "elevator"
    summ = body["routes"][0]["summary"]
    assert summ["warnings"][0] == EXPECT_ALIGHT_WARN, "출발 전에 가장 먼저 보여야 한다"
    assert [p["station"] for p in summ["platform_access"]] == ["관악"]
    assert summ["platform_access_penalty_m"] == m.LIFT_ONLY_PLATFORM_PENALTY_M
    alight = _step(body, "subway_alight")
    assert alight["warnings"] == [EXPECT_ALIGHT_WARN]
    assert alight["platform_access"][0]["access"] == "lift_only"
    assert alight["instruction"].startswith("관악역에서 하차합니다. 주의: ") and EXPECT_ALIGHT_WARN in alight["instruction"]
    # 승차 스텝에서도 미리 알린다 — 승차 직전이 경로를 바꿀 수 있는 마지막 시점이다
    assert alight["instruction"].count("주의:") == 1
    # 승차 스텝은 내릴 역 리프트를 짧게만 알린다 — 전체 문장은 하차 스텝·요약에 있다
    board = _step(body, "subway_board")
    assert board["instruction"].endswith(". 주의: " + EXPECT_ALIGHT_SHORT), board["instruction"]
    assert EXPECT_ALIGHT_WARN not in board["instruction"] and EXPECT_ALIGHT_WARN not in board["warnings"]
    assert EXPECT_ALIGHT_SHORT in board["warnings"]
    assert [p["side"] for p in board["platform_access"]] == ["alight"]
    # 후보 part 의 내부 메모 키는 응답으로 나가지 않는다
    assert "_platform_access_memo" not in json.dumps(body, ensure_ascii=False)
    # 하차 뒤 역 안 문장도 같은 판정(리프트 위치·크기·할 일)
    assert alight["egress"]["inside"][0].startswith("내린 승강장 쪽에는 휠체어리프트만 있습니다 — 1F(석수역")
    assert "역무원을 불러 주세요" in alight["egress"]["inside"][0]


def test_board_lift_only_is_flagged(tmp_path, monkeypatch):
    # 관악(출발점 옆)에서 북행(석수 방향)을 탄다 — 상행 승강장은 리프트뿐
    m, c = _make_client(tmp_path, monkeypatch,
                        [_st("ST-G", "관악", 37.3900, 126.9501), _st("ST-S", "석수", 37.3909, 126.9511)],
                        [GWANAK_ROW, SEOKSU_ROW])
    with c:
        body = _plan(c)
    sub = _subway(body)
    assert (sub["board"]["name"], sub["alight"]["name"]) == ("관악", "석수")
    assert sub["platform_access"]["board"]["warning"] == EXPECT_BOARD_WARN
    assert sub["platform_access"]["alight"]["access"] == "elevator"
    board = _step(body, "subway_board")
    assert EXPECT_BOARD_WARN in board["instruction"] and [p["side"] for p in board["platform_access"]] == ["board"]
    alight = _step(body, "subway_alight")
    assert alight["warnings"] == [] and "platform_access" not in alight
    assert body["routes"][0]["summary"]["warnings"][0] == EXPECT_BOARD_WARN


def test_visual_profile_is_not_penalised_or_warned(tmp_path, monkeypatch):
    m, c = _make_client(tmp_path, monkeypatch, [ST_ANYANG, ST_GWANAK_AT_DEST, ST_MYEONGHAK_NEAR],
                        [GWANAK_ROW, MYEONGHAK_ROW, ANYANG_ROW])
    with c:
        body = _plan(c, profile="visual")
    sub = _subway(body)
    assert sub["alight"]["name"] == "관악", "시각장애 프로필은 종전 선택(도보가 짧은 쪽) 그대로"
    assert "warning" not in (sub["platform_access"]["alight"] or {})
    summ = body["routes"][0]["summary"]
    assert summ["platform_access"] == [] and summ["platform_access_penalty_m"] == 0
    assert not any("휠체어리프트만" in w for w in summ["warnings"])
    for s in body["routes"][0]["steps"]:
        assert "platform_access" not in s and "주의:" not in s["instruction"]


def test_low_floor_mode_time_key_also_penalised(tmp_path, monkeypatch):
    # walk_bus_subway + 휠체어 = 저상버스 우선 모드 기본 on(버스 후보는 없다) — 시간 키에도 감점이 들어가야 한다
    m, c = _make_client(tmp_path, monkeypatch, [ST_ANYANG, ST_GWANAK_AT_DEST, ST_MYEONGHAK_NEAR],
                        [GWANAK_ROW, MYEONGHAK_ROW, ANYANG_ROW])
    with c:
        body = _plan(c, mode="walk_bus_subway")
        assert body["low_floor"]["mode"] is True
        assert _subway(body)["alight"]["name"] == "명학"
        monkeypatch.setattr(m, "LIFT_ONLY_PLATFORM_PENALTY_M", 0)
        m._FAC_CACHE.clear()
        assert _subway(_plan(c, mode="walk_bus_subway"))["alight"]["name"] == "관악"


# ── API: 역 안 출발 ─────────────────────────────────────────
def test_station_start_lift_only_warns_on_step_and_summary(tmp_path, monkeypatch):
    m, c = _make_client(tmp_path, monkeypatch, [_st("ST-G", "관악", 37.3900, 126.9501)], [GWANAK_ROW])
    with c:
        up = c.post("/route/plan", json={"origin": ORIGIN, "destination": DEST,
                                          "origin_station": {"name": "관악", "travel": "north"}}).json()
        down = c.post("/route/plan", json={"origin": ORIGIN, "destination": DEST,
                                            "origin_station": {"name": "관악", "travel": "south"}}).json()
        vis = c.post("/route/plan", json={"origin": ORIGIN, "destination": DEST, "profile": "visual",
                                           "origin_station": {"name": "관악", "travel": "north"}}).json()
    st = up["routes"][0]["steps"][0]
    assert st["maneuver"] == "station_start"
    assert st["warnings"] == [EXPECT_ALIGHT_WARN] and st["platform_access"][0]["access"] == "lift_only"
    assert "(폭 800mm·길이 1,100mm). 승강장의 리프트 호출 버튼" in st["egress"]["inside"][0]
    assert up["routes"][0]["summary"]["warnings"][0] == EXPECT_ALIGHT_WARN
    st_down = down["routes"][0]["steps"][0]
    assert st_down["warnings"] == [] and "platform_access" not in st_down
    assert EXPECT_ALIGHT_WARN not in down["routes"][0]["summary"]["warnings"]
    # 시각장애 프로필: 역 안 문장(설비 사실)은 같지만 휠체어 경고는 붙이지 않는다
    st_vis = vis["routes"][0]["steps"][0]
    assert st_vis["warnings"] == [] and "platform_access" not in st_vis


# ── 판정 메모 · leg 노선 전달 ─────────────────────────────────
def test_platform_access_evaluated_once_per_candidate_side(tmp_path, monkeypatch):
    """근사 정렬과 실계산이 같은 후보를 두 번 판정하지 않는다 — part 에 메모한 결과를 다시 쓴다."""
    m, c = _make_client(tmp_path, monkeypatch, [ST_ANYANG, ST_GWANAK_AT_DEST, ST_MYEONGHAK_NEAR],
                        [GWANAK_ROW, MYEONGHAK_ROW, ANYANG_ROW])
    calls = []
    real = ex.platform_access

    def spy(name, travel, fac, side="alight", line=None):
        calls.append((name, side, line))
        return real(name, travel, fac, side, line=line)
    monkeypatch.setattr(m.station_exits, "platform_access", spy)
    with c:
        body = _plan(c)
    assert _subway(body)["alight"]["name"] == "명학"
    # 후보 2개(안양→관악, 안양→명학) × 승차·하차 = 4번. 메모가 없으면 근사·실계산에서 8번이 된다
    assert len(calls) == 4, calls
    assert all(line == "1호선" for _, _, line in calls)
    assert "_platform_access_memo" not in json.dumps(body, ensure_ascii=False)


def test_subway_platform_access_uses_part_line(tmp_path, monkeypatch, interchange_lines):
    m, c = _make_client(tmp_path, monkeypatch, [ST_ANYANG, ST_GWANAK_AT_DEST], [GWANAK_ROW, ANYANG_ROW])
    with c:
        part = {"kind": "subway", "line": "1호선", "station_cnt": 1,
                "board": {"poi_id": "ST-A", "name": "안양"}, "alight": {"poi_id": "ST-G", "name": "관악"}}
        pam = m._subway_platform_access(part, True)
    assert pam["alight"]["line"] == "1호선" and pam["alight"]["access"] == "lift_only"
    assert pam["alight"]["warning"] == EXPECT_ALIGHT_WARN
    assert pam["board"]["line"] == "1호선" and pam["board"]["toward"] == "관악"
