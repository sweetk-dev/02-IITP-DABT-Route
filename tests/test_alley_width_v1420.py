# -*- coding: utf-8 -*-
"""v1.42.0 — 휠체어 큰길 우선(이면도로 가중)·전동휠체어 최소 폭 1.2m."""
import networkx as nx

from route_service.engine import planner
from route_service.engine.planner import edge_cost, edge_passable, is_alley
from route_service.engine.profiles import PROFILES

E = PROFILES["wheelchair_electric"]
M = PROFILES["wheelchair_manual"]
W = PROFILES["walk"]


def _e(link_type="road", name=None, length=100.0, width=None):
    return {"link_type": link_type, "link_name": name, "length": length, "slope": 0.0, "width": width}


def test_is_alley():
    assert is_alley(_e(name="경수대로1196번길"))
    assert is_alley(_e(name=None)) and is_alley(_e(name=" "))
    assert not is_alley(_e(name="예술공원로"))
    assert not is_alley(_e(name="경수대로"))
    assert not is_alley(_e("sidewalk", name="경수대로1196번길"))


def test_alley_cost_only_wheelchair():
    big, alley = _e(name="경수대로"), _e(name="경수대로1196번길")
    for p in (E, M):
        assert edge_cost(alley, p) == edge_cost(big, p) * 1.5
    assert edge_cost(alley, W) == edge_cost(big, W)


def test_electric_min_width_1_2():
    assert E.min_width_m == 1.2
    assert not edge_passable(_e("sidewalk", width=1.0), E, E.hard_slope())
    assert edge_passable(_e("sidewalk", width=1.0), M, M.hard_slope())
    assert edge_passable(_e("sidewalk", width=1.3), E, E.hard_slope())
    assert edge_passable(_e("sidewalk", width=None), E, E.hard_slope())   # 폭 미상은 막지 않음


def _graph():
    # A-B-C-D: 큰길 보도 우회 330m / A-X-D: 이면도로 지름길 240m (x1.2 road x1.5 alley = 432 > 330)
    G = nx.Graph()
    pts = {"A": (37.0, 127.0), "B": (37.001, 127.0), "C": (37.001, 127.002), "D": (37.0, 127.002), "X": (37.0005, 127.001)}
    for n, (la, lo) in pts.items():
        G.add_node(n, lat=la, lon=lo)
    for u, v, L in (("A", "B", 110), ("B", "C", 110), ("C", "D", 110)):
        G.add_edge(u, v, **_e("sidewalk", name="큰길", length=L))
    G.add_edge("A", "X", **_e(name="큰길10번길", length=120))
    G.add_edge("X", "D", **_e(name="큰길10번길", length=120))
    return G


def test_wheelchair_prefers_main_road_walk_takes_alley():
    G = _graph()
    assert planner._astar(G, "A", "D", E, E.hard_slope()) == ["A", "B", "C", "D"]
    assert planner._astar(G, "A", "D", M, M.hard_slope()) == ["A", "B", "C", "D"]
    assert planner._astar(G, "A", "D", W, W.hard_slope()) == ["A", "X", "D"]
