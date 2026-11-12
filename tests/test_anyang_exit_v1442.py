# -*- coding: utf-8 -*-
"""v1.44.2 — 안양역 휠체어 출구 정정.

역 설비 자료는 출구 승강기를 '2번 출입구 — (2F) 1번출구 맞이방 서쪽'으로 적었고, 현장(2026-10-01)에서
휠체어로 나가는 출구는 철로 서쪽 1번 출구 승강기였다. 하차 뒤 2번 출구(철로 동쪽)로 안내하던 문제.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.transit import exits as ex  # noqa: E402

ANYANG_FAC = {"elevators": [
    {"exit_no": "2", "detail_loc": "(2F) 1번출구 맞이방 서쪽"},
    {"exit_no": "내부", "detail_loc": "(1F) 관악역 방향 승강장 4-3 출입문앞"},
    {"exit_no": "내부", "detail_loc": "(1F) 명학역 방향 승강장 7-2 출입문앞"}]}


def test_db_exit2_elevator_is_exit1_for_wheelchair():
    opts = ex.exit_options("안양", ANYANG_FAC, wheelchair=True)
    assert [e["exit_no"] for e in opts] == ["1"]                       # 휠체어 후보는 1번 출구만
    e1 = opts[0]
    assert e1["elevator"] == "(2F) 1번출구 맞이방 서쪽" and e1["has_elevator"] is True
    assert (e1["lat"], e1["lng"]) == (37.40123, 126.9225) and e1["at"] == "elevator"
    assert (e1["stairs_lat"], e1["stairs_lng"]) == (37.4013261, 126.9225719)


def test_egress_text_names_exit1():
    e1 = ex.exit_options("안양", ANYANG_FAC, wheelchair=True)[0]
    g = ex.egress_guide("안양", "관악", ANYANG_FAC, e1)
    assert g["exit"]["exit_no"] == "1"
    assert g["inside"][0] == "내린 승강장의 승강기로 이동합니다 — (1F) 명학역 방향 승강장 7-2 출입문앞"
    assert g["inside"][1] == "1번 출구 승강기로 나갑니다 — (2F) 1번출구 맞이방 서쪽"
    assert g["outside"].startswith("안양역 1번 출구 앞에서")


def test_without_correction_field_other_stations_unchanged():
    # 정정 필드가 없는 역은 자료의 출구 번호를 그대로 쓴다(명학 1·2번)
    fac = {"elevators": [{"exit_no": "1", "detail_loc": "a"}, {"exit_no": "2", "detail_loc": "b"}]}
    assert sorted(e["exit_no"] for e in ex.exit_options("명학", fac, wheelchair=True)) == ["1", "2"]
    # 자료에 1번 승강기가 이미 있으면 옮기지 않는다
    fac2 = {"elevators": [{"exit_no": "1", "detail_loc": "x"}, {"exit_no": "2", "detail_loc": "y"}]}
    o = {e["exit_no"]: e for e in ex.exit_options("안양", fac2, wheelchair=True)}
    assert o["1"]["elevator"] == "x" and o["2"]["elevator"] == "y"
