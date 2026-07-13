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
import sys

import networkx as nx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.engine.graph import NetworkStore  # noqa: E402


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
