# -*- coding: utf-8 -*-
"""테스트 픽스처 — 합성 보행망.

    N1 --sidewalk(100m, 1도)-- N2 --sidewalk(100m, 1도)-- N3(목적지)
     |                                                     |
     +--------- steps(60m) ---- N4 ---- sidewalk(60m, 9도)-+

  · 완만한 우회로(N1-N2-N3, 200m) vs 급경사·계단 지름길(N1-N4-N3, 120m)
  · 수동 휠체어 프로필은 계단(steps)을 회피해야 하므로 우회로를 선택해야 한다.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import networkx as nx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 계측 로그(JSONL)는 테스트 실행 전용 임시 폴더로 보낸다.
# 설정(route_service.config)은 METRICS_LOG_PATH 환경변수를 읽고, 없으면 저장소의
# data/metrics/events.jsonl 에 append 한다 — 테스트를 돌릴 때마다 저장소 안에 시험 요청의
# 계측 행이 쌓인다. 설정 모듈이 import 되기 전에(=conftest 로드 시점) 환경변수를 정해 둔다.
# 개별 테스트가 monkeypatch.setenv 로 자기 경로를 지정하면 그쪽이 우선한다.
_METRICS_TMP_DIR = tempfile.mkdtemp(prefix="route-test-metrics-")
os.environ["METRICS_LOG_PATH"] = os.path.join(_METRICS_TMP_DIR, "events.jsonl")


def pytest_sessionfinish(session, exitstatus):
    """테스트 실행이 끝나면 임시 계측 로그 폴더를 지운다."""
    shutil.rmtree(_METRICS_TMP_DIR, ignore_errors=True)

from route_service.engine.graph import NetworkStore  # noqa: E402

# 인증(v1.44.0) — 토큰이 설정되지 않은 서버는 보호 엔드포인트를 503 으로 거부한다.
# API 테스트는 시험용 토큰을 환경변수로 설정하고, TestClient 기본 헤더로 그 토큰을 보낸다.
TEST_API_TOKEN = "test-route-api-token"
TEST_ADMIN_TOKEN = "test-route-admin-token"
API_AUTH = {"Authorization": "Bearer %s" % TEST_API_TOKEN}
ADMIN_AUTH = {"Authorization": "Bearer %s" % TEST_ADMIN_TOKEN}


def make_graph() -> nx.Graph:
    G = nx.Graph()
    G.add_node("N1", lat=37.3900, lon=126.9500, node_type="intersection")
    G.add_node("N2", lat=37.3900, lon=126.9511, node_type="intersection")
    G.add_node("N3", lat=37.3909, lon=126.9511, node_type="entrance")
    G.add_node("N4", lat=37.3905, lon=126.9500, node_type="intersection")

    G.add_edge("N1", "N2", length=100.0, slope=1.0, link_type="sidewalk",
               width=2.0, curb_cut=True, surface="asphalt", link_name="예시로", geometry=None)
    G.add_edge("N2", "N3", length=100.0, slope=1.5, link_type="sidewalk",
               width=2.0, curb_cut=True, surface="asphalt", link_name="예시2로", geometry=None)
    G.add_edge("N1", "N4", length=60.0, slope=2.0, link_type="steps",
               width=1.2, curb_cut=None, surface=None, link_name="지름길계단", geometry=None)
    G.add_edge("N4", "N3", length=60.0, slope=9.0, link_type="sidewalk",
               width=1.2, curb_cut=None, surface=None, link_name="언덕길", geometry=None)
    return G


@pytest.fixture()
def store() -> NetworkStore:
    s = NetworkStore()
    s.load_graph_object(make_graph(), version="test-1", region="테스트")
    return s
