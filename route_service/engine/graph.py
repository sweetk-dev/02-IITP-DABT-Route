# -*- coding: utf-8 -*-
"""보행 네트워크 그래프 로드·보관.

그래프 스키마(소스가 OSM 이든 융기원 node/link 든 동일해야 한다):

node attrs:
    lat, lon            : WGS84
    node_type           : intersection | crossing | entrance | stop | station | unknown
    -- 이하 안양시 횡단보도 지점 부착 (apply_city_crosswalks.py [3]단계, 없으면 미부착) --
    crosswalk_cnt       (int)        : 이 노드에 부착된 횡단보도 개수 (안내 전용 계층)
    cw_mgmt_nos         (list[str])  : 부착분 관리번호 목록 (mv_crosswalk.mgmt_no)
    cw_curb_cut         (bool|None)  : 부착분 턱낮춤 AND 집계. **None = 미상(없음 아님)**
    cw_tactile_paving   (bool|None)  : 부착분 점자블록 AND 집계. None = 미상

edge attrs:
    length      (float) : 링크 연장(m)
    slope       (float) : 평균 종단경사(도). DEM 미적용 시 0.0
    link_type   (str)   : sidewalk | road | crossing | steps | overpass | underpass | ramp | elevator | unknown
    width       (float|None) : 유효 보도폭(m)
    curb_cut    (bool|None)  : 턱낮춤 여부(횡단보도 접속부). **None = 미상 — False 일 때만 차단**
    tactile_paving (bool|None) : 점자블록 유무. None = 미상
    surface     (str|None)
    link_name   (str|None)
    geometry    (list[(lat, lon)]|None) : 실제 선형. 없으면 노드 직선으로 대체
    -- 이하 안양시 횡단보도 원천 전이 (apply_city_crosswalks.py [1]·[2]단계, 선택) --
    cw_length_m (float) : 횡단보도길이(횡단 거리) 원천값
    cw_mgmt_no  (str)   : 매칭된 관리번호 (mv_crosswalk.mgmt_no)
    attr_source (str)   : 속성 출처 (city_cw2026 | topo1k | ...)
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

# 연결요소 캐시(NetworkStore._components) 항목 수 상한.
# 키는 (프로필 id, 경사 상한, avoid, 최소 폭, 턱낮춤 요구, 정제 링크 하한) 조합이다. 기본 프로필 5종 ×
# 경사 단계(하드·완화) 2~3개에 요청 제약(avoid·경사 상한 지정) 변형 몇 개면 평소 10여 개다.
# 요청 제약의 경사 상한·최소 폭은 임의 실수라 조합 수에 끝이 없으므로 상한을 둔다. 항목 하나가
# 노드 id 집합(수만 개)이라 크게 잡지 않는다 — 넘치면 먼저 들어온 것부터 버리고, 버려진 조합은
# 다음 요청 때 다시 계산한다(결과는 같고 계산 시간만 든다).
COMPONENT_CACHE_MAX = 32

EDGE_DEFAULTS = {
    "length": 0.0,
    "slope": 0.0,
    "link_type": "unknown",
    "width": None,
    "curb_cut": None,
    "tactile_paving": None,
    "surface": None,
    "link_name": None,
    "geometry": None,
}

# 링크 이름 -> 보행 부적합 시설 재분류 (#27).
# 차량용 지하차도(예: "일번가지하차도")는 OSM 태그상 highway=road + tunnel 이라
# 기존 빌드에서 link_type="road" 로 들어와 휠체어 회피(avoid=underpass)를 그대로
# 통과했다. 이미 배포된 그래프(pickle·DB 산출본)를 재빌드 없이 바로잡기 위해
# 로드 시점에 이름으로 재분류한다. 재빌드 시점에는 sources/osm.py 의 태그 분류가
# 같은 결과를 낸다.
_NAME_RECLASS = (
    (("지하차도", "지하도", "지하보도"), "underpass"),
    (("육교", "고가교"), "overpass"),
)
# 이름 재분류를 허용하는 기존 타입 — 명시 조사된 타입(crossing·steps 등)은 건드리지 않는다.
_NAME_RECLASS_FROM = ("road", "sidewalk", "unknown")


def _reclass_by_name(link_type: str, link_name) -> str:
    if link_type not in _NAME_RECLASS_FROM or not link_name:
        return link_type
    name = str(link_name)
    for keywords, new_type in _NAME_RECLASS:
        if any(k in name for k in keywords):
            return new_type
    return link_type


def normalize_graph(G: nx.Graph) -> nx.Graph:
    """소스별 편차를 흡수해 표준 스키마로 맞춘다. 기존 인천 gpickle 도 그대로 수용."""
    for _n, data in G.nodes(data=True):
        data.setdefault("node_type", "unknown")
    for _u, _v, data in G.edges(data=True):
        for k, default in EDGE_DEFAULTS.items():
            data.setdefault(k, default)
        if data["link_type"] not in LINK_TYPES:
            data["link_type"] = "unknown"
        data["link_type"] = _reclass_by_name(data["link_type"], data.get("link_name"))
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
        self._components = {}     # _component_key(profile, max_slope) -> 최대 연결요소 노드 집합

    # ---- 로드 ----
    def load(self, path: str, version: str = "unknown", region: str = "") -> dict:
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        with open(path, "rb") as f:
            G = pickle.load(f)
        if not isinstance(G, nx.Graph):
            raise TypeError("network pickle 이 networkx.Graph 가 아닙니다")
        G = normalize_graph(G)
        # 색인·메타를 먼저 다 만든 뒤에 한꺼번에 교체한다. 교체 도중에 예외가 나면(좌표 없는 노드 등
        # 형식이 맞지 않는 파일) 새 그래프와 옛 색인이 섞인 상태로 남으므로, 실패 시에는 아무것도
        # 바꾸지 않고 종전 그래프를 그대로 둔다.
        node_ids = list(G.nodes())
        node_coords = [(G.nodes[n]["lat"], G.nodes[n]["lon"]) for n in node_ids]
        meta = self._build_meta(G, path, version, region)
        with self._lock:
            self._G = G
            self._node_ids = node_ids
            self._node_coords = node_coords
            self._meta = meta
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

    @staticmethod
    def _component_key(profile, max_slope_deg: float) -> tuple:
        """연결요소 캐시 키 — 통행 가능 판정(`planner.edge_passable`)이 읽는 프로필 값 전부.

        프로필 id 와 경사 상한만으로 키를 만들면, 요청 제약으로 `replace(profile, avoid=...)` 처럼
        id 는 같고 판정 값만 다른 프로필이 들어왔을 때 그 결과가 기본 프로필의 캐시 자리에 들어가
        (또는 기본 프로필 결과가 제약 요청에 쓰여) 스냅 후보 집합이 서로 뒤바뀐다.
        `edge_passable` 이 읽는 필드는 avoid · min_width_m · requires_curb_cut ·
        derived_min_confidence 와 인자 max_slope_deg 이다 — 그 함수가 읽는 필드가 늘면 여기도 늘린다.
        avoid 는 순서·중복이 판정에 영향을 주지 않으므로 정렬한 튜플로 넣는다.

        avoid 는 요청 제약(constraints.avoid)으로 임의 문자열이 들어올 수 있다. 그대로 키에 넣으면
        요청마다 다른 문자열로 캐시 항목이 끝없이 늘어난다. 그래프의 link_type 은 로드할 때
        LINK_TYPES 안의 값으로 맞춰지고(`normalize_graph`) 그 뒤에 얹는 링크도 같은 값만 쓰므로, 그 밖의 문자열은 어떤 링크와도 일치하지
        않아 판정에 영향이 없다 — 알려진 링크 유형과의 교집합만 키에 넣는다(판정 결과는 같다).
        """
        return (
            profile.id,
            round(float(max_slope_deg), 2),
            tuple(sorted({str(a) for a in (profile.avoid or ())} & set(LINK_TYPES))),
            float(getattr(profile, "min_width_m", 0.0) or 0.0),
            bool(getattr(profile, "requires_curb_cut", False)),
            float(getattr(profile, "derived_min_confidence", 0.0) or 0.0),
        )

    def invalidate_components(self) -> None:
        """연결요소 캐시를 비운다 — 링크 통행성(blocked·curb_cut·width 등)을 바꾼 뒤 부른다."""
        with self._lock:
            self._components = {}

    def update_graph(self, fn):
        """그래프 속성을 고치는 함수 `fn(G)` 를 저장소 락 아래에서 실행하고 연결요소 캐시를 비운다.

        오버라이드(통행 불가 승인·폭·턱낮춤 보정)의 적용·철회는 링크 통행성을 바꾼다. 캐시를
        그대로 두면 `reachable_nodes` 가 바뀌기 전 연결요소를 계속 돌려줘, 막힌 링크 너머의
        고립 조각에 스냅되거나 다시 열린 구간이 스냅 후보에서 빠진다.
        같은 락을 쓰는 `load` · `reachable_nodes` 와는 서로 겹치지 않는다(연결요소 계산 도중
        속성이 바뀌거나, 그래프 교체와 적용이 엇갈리는 것을 막는다). 경로 탐색 요청 전체를
        이 락으로 감싸지는 않는다 — 요청은 그래프 사본으로 탐색하며, 락은 수정 구간에만 건다.

        인자 fn: 그래프(nx.Graph)를 받아 제자리에서 고치는 함수. 반환값은 그대로 돌려준다.
        실패 시: fn 이 올린 예외는 그대로 전파하되, 일부만 적용됐을 수 있으므로 캐시는 비운다.
        그래프가 로드되지 않았으면 RuntimeError.
        """
        with self._lock:
            if self._G is None:
                raise RuntimeError("네트워크가 로드되지 않았습니다")
            try:
                return fn(self._G)
            finally:
                self._components = {}

    def reachable_nodes(self, profile, max_slope_deg: float) -> set:
        """프로필 제약을 적용했을 때 **서로 오갈 수 있는 최대 덩어리**의 노드 집합.

        계단·급경사를 걷어내면 보행망은 수백 개 조각으로 쪼개진다(안양 실측: 수동 휠체어
        4도 기준 522개 컴포넌트). 가장 가까운 통행 가능 노드에 스냅하면 그 노드가 고립된
        조각에 속해 "경로 없음" 이 나온다 — 실제로는 갈 수 있는 길이 있는데도.
        그래서 스냅 후보를 이 집합으로 제한한다.
        """
        import networkx as nx

        key = self._component_key(profile, max_slope_deg)
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
            # 항목 수 상한 — dict 는 넣은 순서를 지키므로 맨 앞이 가장 먼저 들어온 항목이다.
            while len(self._components) >= COMPONENT_CACHE_MAX:
                self._components.pop(next(iter(self._components)))
            self._components[key] = main
            return main


STORE = NetworkStore()
