# -*- coding: utf-8 -*-
"""v1.41.0 — 관악역 승차 현장 동선 (현장 점검 2026-10-20).

서비스는 "역에 들어가 개찰구를 지나 안양 방향 승강장으로 가세요 … 10번째 칸(맨 뒤)"이라고만 안내했다.
실제로는 2번 출구 승강기로 맞이방에 올라가, 일반 개찰구가 아니라 맞이방 옆 안양역 방면 승강기 전용 개찰구로
들어가 승강기를 타야 하고, 승강기에서 내리면 1-1 앞이다. 명학역 하차 승강기는 10-4 앞이라 승강장 끝까지 이동해 탄다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.transit import exits as ex  # noqa: E402
from tests.test_station_access_v1390 import GWANAK_FAC, MYEONGHAK_FAC  # noqa: E402


def _bx2():
    return ex.exit_brief({e["exit_no"]: e for e in ex.exit_options("관악", GWANAK_FAC, True)}["2"])


def test_gwanak_exit2_toward_anyang_uses_field_route():
    g = ex.ingress_guide("관악", _bx2(), ex.platform_access("관악", "south", GWANAK_FAC, "board"),
                         ex.platform_access("명학", "south", MYEONGHAK_FAC, "alight"), "명학")
    assert g["speech"] == ("관악역 2번 출구 승강기로 맞이방에 올라가, 옆의 안양역 방면 승강기 전용 개찰구로 들어가세요. "
                           "승강기에서 내리면 1-1 앞입니다")
    assert g["inside"] == [
        "2번 출구 승강기로 2층 맞이방에 올라갑니다",
        "일반 개찰구가 아니라 맞이방 옆 안양역 방면 승강기 전용 개찰구로 들어갑니다 — 개찰구 바로 뒤가 승강기",
        "승강기로 1층 안양 방향 승강장에 내려가면 1-1 출입문 앞입니다",
        "승강장 끝 10-4까지 이동해 타면 명학역에서 내리자마자 승강기입니다",
    ]
    assert g["landing"] == "1-1" and (g["car"]["car"], g["car"]["door"]) == (10, 4)
    assert g["gate"] == {"lat": 37.418702, "lng": 126.90924}


def test_landing_same_as_target_car_keeps_car_hint():
    ov = {"speech": "s", "inside": ["a"], "landing": "10-4"}
    car = {"car": 10, "door": 4, "where": "맨 뒤", "text": "10번째 칸(맨 뒤)에 타면 명학역 승강기가 가깝습니다"}
    g = ex._ingress_from_override("관악", ov, car, "명학")
    assert g["inside"] == ["a", "10번째 칸(맨 뒤)에 타면 명학역 승강기가 가깝습니다"]


def test_other_direction_or_no_exit_falls_back():
    # 석수 방향(상행)은 현장 동선 자료가 없다 — 설비 자료 문장
    pa_n = ex.platform_access("관악", "north", GWANAK_FAC, "board")
    g = ex.ingress_guide("관악", _bx2(), pa_n, None, "석수")
    assert "전용 개찰구" not in g["speech"]
    assert g["speech"].startswith("관악역 2번 출구 승강기입니다")
    g2 = ex.ingress_guide("관악", None, ex.platform_access("관악", "south", GWANAK_FAC, "board"), None, "명학")
    assert "전용 개찰구" not in g2["speech"]
