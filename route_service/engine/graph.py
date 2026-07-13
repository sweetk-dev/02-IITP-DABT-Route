# -*- coding: utf-8 -*-
"""보행 네트워크 그래프 로드·보관.

그래프 스키마(소스가 OSM 이든 융기원 node/link 든 동일해야 한다):

node attrs:
    lat, lon            : WGS84
    node_type           : intersection | crossing | entrance | stop | station | unknown

edge attrs:
    length      (float) : 링크 연장(m)
    slope       (float) : 평균 종단경사(도). DEM 미적용 시 0.0
    link_type   (str)   : sidewalk | road | crossing | steps | overpass | underpass | ramp | elevator | unknown
    width       (float|None) : 유효 보도폭(m)
    curb_cut    (bool|None)  : 턱낮춤 여부(횡단보도 접속부)
    surface     (str|None)
    link_name   (str|None)
    geometry    (list[(lat, lon)]|None) : 실제 선형. 없으면 노드 직선으로 대체
"""
from __future__ import annotations

import os
import pickle
import threading

import networkx as nx

from .geo import haversine_m

LINK_TYPES = (
    "sidewalk", "road", "crossing", "steps", "overpass",
    "underpass", "ramp", "elevator", "unknown",
)

EDGE_DEFAULTS = {
    "length": 0.0,
    "slope": 0.0,
    "link_type": "unknown",
    "width": None,
    "curb_cut": None,
    "surface": None,
    "link_name": None,
    "geometry": None,
}


def normalize_graph(G: nx.Graph) -> nx.Graph:
    """소스별 편차를 흡수해 표준 스키마로 맞춘다. 기존 인천 gpickle 도 그대로 수용."""
    for _n, data in G.nodes(data=True):
        data.setdefault("node_type", "unknown")
    for _u, _v, data in G.edges(data=True):
        for k, default in EDGE_DEFAULTS.items():
            data.setdefault(k, default)
        if data["link_type"] not in LINK_TYPES:
            data["link_type"] = "unknown"
        try:
            data["length"] = float(data["length"])
        except (TypeError, ValueError):
            data["length"] = 0.0
        try:
            data["slope"] = abs(float(data["slope"]))
        except (TypeError, ValueError):
            data["slope"] = 0.0
        # build_network.py 가 남긴 shapely geometry(투영좌표)는 라우팅에 쓰지 않는다.
        geom = data.get("geometry")
        if geom is not None and not isinstance(geom, (list, tuple)):
            data["geometry"] = None
    return G


def edge_coords(G: nx.Graph, u, v) -> list:
    """링크의 좌표열(lat, lon). geometry 가 없으면 두 노드를 잇는 직선."""
    data = G[u][v]
    geom = data.get("geometry")
    if geom:
        coords = [(float(a), float(b)) for a, b in geom]
        head = (G.nodes[u]["lat"], G.nodes[u]["lon"])
        if haversine_m(coords[0][0], coords[0][1], head[0], head[1]) > haversine_m(
            coords[-1][0], coords[-1][1], head[0], head[1]
        ):
            coords = list(reversed(coords))
        return coords
    return [
        (G.nodes[u]["lat"], G.nodes[u]["lon"]),
        (G.nodes[v]["lat"], G.nodes[v]["lon"]),
    ]


class NetworkStore:
    """그래프를 프로세스에 상주시키는 컨테이너(무중단 교체 지원)."""

    def __init__(self):
        self._lock = threading.RLock()
        self._G = None
        self._meta = {}
        self._node_ids = []
        self._node_coords = []
        self._components = {}     # (profile_id, max_slope) -> 최대 연결요소 노드 집합

    # ---- 로드 ----
    def load(self, path: str, version: str = "unknown", region: str = "") -> dict:
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        with open(path, "rb") as f:
            G = pickle.load(f)
        if not isinstance(G, nx.Graph):
            raise TypeError("network pickle 이 networkx.Graph 가 아닙니다")
        G = normalize_graph(G)
        with self._lock:
            self._G = G
            self._node_ids = list(G.nodes())
            self._node_coords = [
                (G.nodes[n]["lat"], G.nodes[n]["lon"]) for n in self._node_ids
            ]
            self._meta = self._build_meta(G, path, version, region)
            self._components = {}
        return self._meta

    def load_graph_object(self, G: nx.Graph, version="memory", region="") -> dict:
        """테스트·인메모리 구축용."""
        G = normalize_graph(G)
        with self._lock:
            self._G = G
            self._node_ids = list(G.nodes())
            self._node_coords = [
                (G.nodes[n]["lat"], G.nodes[n]["lon"]) for n in self._node_ids
            ]
            self._meta = self._build_meta(G, "", version, region)
            self._components = {}
        return self._meta

    @staticmethod
    def _build_meta(G, path, version, region) -> dict:
        lats = [d["lat"] for _, d in G.nodes(data=True)]
        lons = [d["lon"] for _, d in G.nodes(data=True)]
        types = {}
        slope_known = 0
        width_known = 0
        for _u, _v, d in G.edges(data=True):
            types[d["link_type"]] = types.get(d["link_type"], 0) + 1
            if d["slope"] > 0:
                slope_known += 1
            if d["width"] is not None:
                width_known += 1
        edge_cnt = G.number_of_edges()
        return {
            "network_version": version,
            "region": region,
            "source_path": os.path.basename(path) if path else "",
            "node_cnt": G.number_of_nodes(),
            "edge_cnt": edge_cnt,
            "bbox": {
                "min_lat": min(lats) if lats else None,
                "min_lng": min(lons) if lons else None,
                "max_lat": max(lats) if lats else None,
                "max_lng": max(lons) if lons else None,
            },
            "link_type_counts": types,
            # 데이터 품질 고지 — 계단·경사 속성이 없으면 회피 판정이 무의미해진다.
            "link_type_available": bool(set(types) - {"unknown"}),
            "slope_coverage": round(slope_known / edge_cnt, 4) if edge_cnt else 0.0,
            "width_coverage": round(width_known / edge_cnt, 4) if edge_cnt else 0.0,
        }

    # ---- 조회 ----
    @property
    def loaded(self) -> bool:
        return self._G is not None

    @property
    def graph(self) -> nx.Graph:
        if self._G is None:
            raise RuntimeError("네트워크가 로드되지 않았습니다")
        return self._G

    @property
    def meta(self) -> dict:
        return dict(self._meta)

    @property
    def node_index(self):
        """(node_ids, coords) — 스냅용."""
        return self._node_ids, self._node_coords

    def reachable_nodes(self, profile, max_slope_deg: float) -> set:
        """프로필 제약을 적용했을 때 **서로 오갈 수 있는 최대 덩어리**의 노드 집합.

        계단·급경사를 걷어내면 보행망은 수백 개 조각으로 쪼개진다(안양 실측: 수동 휠체어
        4도 기준 522개 컴포넌트). 가장 가까운 통행 가능 노드에 스냅하면 그 노드가 고립된
        조각에 속해 "경로 없음" 이 나온다 — 실제로는 갈 수 있는 길이 있는데도.
        그래서 스냅 후보를 이 집합으로 제한한다.
        """
        import networkx as nx

        key = (profile.id, round(float(max_slope_deg), 2))
        with self._lock:
            if key in self._components:
                return self._components[key]

            from .planner import edge_passable

            H = nx.Graph()
            H.add_nodes_from(self._G.nodes())
            for u, v, d in self._G.edges(data=True):
                if edge_passable(d, profile, max_slope_deg):
                    H.add_edge(u, v)
            comps = list(nx.connected_components(H))
            main = max(comps, key=len) if comps else set()
            self._components[key] = main
            return main


STORE = NetworkStore()
