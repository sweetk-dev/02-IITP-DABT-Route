# -*- coding: utf-8 -*-
"""v1.44.3 — 이용자 제보 경고를 한 줄로 묶고 '기타'는 뺀다.

같은 지점 제보가 "이용자 제보: 경사 심함 (미확인), 이용자 제보: 기타 (미확인)" 처럼 따로 읽혀 길고,
'기타'는 이용자가 무엇인지 알 수 없었다(사용자 요청 2026-11-12).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.engine.steps import merge_report_warnings  # noqa: E402
from route_service.collect.store import warning_text  # noqa: E402


def test_two_reports_join_and_etc_dropped():
    ws = [warning_text("steep"), warning_text("etc"), warning_text("blocked")]
    assert merge_report_warnings(ws) == ["이용자 제보: 경사 심함, 통행 불가(공사 등) (미확인)"]


def test_only_etc_disappears_other_warnings_kept_in_order():
    ws = ["짧은 구간 경사 추정 6.3도", warning_text("etc"), "턱낮춤 미상"]
    assert merge_report_warnings(ws) == ["짧은 구간 경사 추정 6.3도", "턱낮춤 미상"]


def test_report_placed_at_first_report_position_and_dedup():
    ws = ["턱낮춤 미상", warning_text("steep"), "보도 폭 1.0m (좁음)", warning_text("steep")]
    assert merge_report_warnings(ws) == ["턱낮춤 미상", "이용자 제보: 경사 심함 (미확인)", "보도 폭 1.0m (좁음)"]


def test_confirmed_only_has_no_pending_mark():
    ws = [warning_text("steep", confirmed=True), warning_text("curb", confirmed=True)]
    assert merge_report_warnings(ws) == ["이용자 제보: 경사 심함, 턱 있음"]
    assert merge_report_warnings([]) == [] and merge_report_warnings(None) == []
