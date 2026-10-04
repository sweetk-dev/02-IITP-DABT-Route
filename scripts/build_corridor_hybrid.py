# -*- coding: utf-8 -*-
"""실증 회랑 구간만 수치지형도 보도망으로 위상 교체한 하이브리드 그래프를 만든다.

배경 (횡단보도_적재_시뮬레이션_결과_2026-08-27.md §5-2)
  안양 OSM 보행망은 도로 중심선 위주라 실증 6개 도보 구간의 crossing 링크가 0이다.
  수치지형도 보도 중심선 + 시 횡단보도 재빌드망은 좌우 보도가 구분되고 crossing 이
  실제로 생기지만, 전역 최대 연결요소가 66%(규칙 C)에 그쳐 통째 교체는 불가능하다.
  그래서 **실증 회랑(6개 leg 버퍼) 안에서만** 보도망으로 교체하고 경계를 기존
  OSM 망에 스티칭한다. 거리는 40~100% 인플레가 예상되며(도로 중심선 → 실제 보도),
  이는 오류가 아니라 정직한 증가일 수 있다 — 주행 GPS 트랙 대조로 최종 판정한다.

절차
  [1] 기준 그래프에서 6개 leg 를 라우팅해 회랑 폴리라인 확보 -> 버퍼(기본 80m)
  [2] 회랑 안 topo 보도망 부분망 추출 (pednet_node_cw/pednet_link_cw)
  [3] 갭 브리징 (규칙 C — 검증 완료된 규칙만):
      · 서로 다른 컴포넌트 노드쌍 거리 <= 15m
      · 브리지 선분이 OSM 차도(road/underpass)와 교차하면 '도로 횡단' —
        횡단보도 점(시+OSM 병합본) 12m 이내일 때만 crossing 링크로 생성
      · 교차하지 않으면 진출입로 단절 — sidewalk 링크로 연결
  [4] 기준 그래프에서 회랑 내부(양끝 모두 버퍼 안) walkable 링크 제거
      (steps·overpass·underpass·ramp·elevator 는 topo 에 없는 구조물이라 보존)
  [5] topo 부분망 삽입(노드 ID 'T' 접두) + 경계 스티칭
      (내부에 남은 OSM 노드 -> 최근접 topo 노드 <= 25m 연결)
  [6] 신규 링크 경사 = DEM 표고 샘플링 (rasterio, 없으면 0.0)

주의
  - 입력 그래프는 **횡단보도 미반영본(network_anyang_enriched)** 을 쓸 것.
    산출 후 apply_city_crosswalks.py 를 돌려야 부착 계층이 한 번만 깨끗하게 붙는다.
  - 회랑 밖 위상은 건드리지 않는다.

사용:
  python scripts/build_corridor_hybrid.py \
      --graph data/network_anyang_enriched.gpickle \
      --pednet-node data/pednet_node_cw.csv --pednet-link data/pednet_link_cw.csv \
      --crosswalks data/crosswalks_merged.geojson \
      --legs data/db_export/demo_legs.json \
      --dem data/dem/anyang_5m.tif \
      --out data/network_anyang_hybrid_raw.gpickle \
      --report data/db_export/corridor_hybrid_report.json
  python scripts/apply_city_crosswalks.py --graph data/network_anyang_hybrid_raw.gpickle \
      --crosswalks data/crosswalks_anyang_city.geojson \
      --out data/network_anyang_hybrid.gpickle
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BUFFER_M = 80.0        # 회랑 반폭
BRIDGE_MAX = 15.0      # 규칙 C: 브리지 최대 길이
CROSS_NEAR = 12.0      # 규칙 C: 도로 횡단 브리지는 횡단보도 점 12m 이내에서만
STITCH_MAX = 25.0      # 경계 스티칭 최대 거리
COVER_RADIUS = 30.0    # OSM 링크 제거 조건: topo 보도가 이 반경 안에 실재할 때만
REMOVE_TYPES = ("road", "sidewalk", "crossing", "unknown")   # 회랑 내부에서 교체되는 타입
# 신규 링크 중 DEM 에서 표고를 얻지 못해 경사가 0.0 으로 들어간 비율의 허용 상한.
# 회랑은 시 경계 안쪽이라 정상이면 결측은 0 에 가깝다(DEM 가장자리·nodata 셀 몇 개 정도).
# 경사 0.0 은 '평지'로 읽혀 휠체어 프로필의 경사 차단·비용 가중이 그 링크에서 꺼지므로, 결측이
# 눈에 띄는 비율이면 산출 그래프를 그대로 쓰면 안 된다. 5% 는 가장자리 결측은 허용하되
# DEM 파일 누락·좌표계 불일치(이 경우 결측은 사실상 100%)는 확실히 걸러내도록 잡은 운영값이다
# (실측으로 정한 값이 아니다 — 결측 수는 리포트의 dem 항목에 남으니 필요하면 조정한다).
DEM_MISSING_MAX_RATIO = 0.05
DEM_EXPECTED_EPSG = 5186       # _DemSampler.elev 가 가정하는 입력 좌표계 (중부원점 GRS80)
EXIT_DEM_MISSING = 3           # 결측 비율 초과로 저장하지 않고 끝낼 때의 종료 코드


def _tf():
    from pyproj import Transformer
    to_5186 = Transformer.from_crs("EPSG:4326", "EPSG:5186", always_xy=True).transform
    return to_5186


def _seg_intersect(p1, p2, p3, p4) -> bool:
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2 = ccw(p3, p4, p1), ccw(p3, p4, p2)
    d3, d4 = ccw(p1, p2, p3), ccw(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def dem_crs_warning(crs):
    """DEM 좌표계가 EPSG:5186 이 아니면 경고 문구, 맞거나 알 수 없으면 None.

    _DemSampler.elev 는 받은 좌표를 그대로 래스터 색인에 넣는다(재투영하지 않는다). 호출부는
    EPSG:5186 좌표를 넘기므로 DEM 이 다른 좌표계(예: 4326, 5179)면 모든 점이 범위 밖이 되어
    경사가 전부 0.0 이 된다. crs 는 rasterio 의 CRS 객체(to_epsg 메서드)를 기대하며,
    None 이거나 EPSG 코드를 알아낼 수 없으면 판단하지 않는다(None 반환).
    """
    if crs is None:
        return None
    try:
        epsg = crs.to_epsg()
    except Exception:
        return None
    if epsg is None or int(epsg) == DEM_EXPECTED_EPSG:
        return None
    return ("DEM 좌표계가 EPSG:%s 입니다 — 이 스크립트는 EPSG:%d 좌표로 표고를 읽습니다. "
            "좌표가 맞지 않으면 표고를 얻지 못해 경사가 0.0 으로 들어갑니다."
            % (epsg, DEM_EXPECTED_EPSG))


def dem_missing_summary(requested: bool, opened: bool, links: int, missing: int,
                        max_ratio: float = DEM_MISSING_MAX_RATIO) -> dict:
    """DEM 결측 집계를 리포트용 dict 로 만든다.

    인자
      requested : --dem 을 지정했는가
      opened    : DEM 파일을 실제로 열었는가(파일 없음·rasterio 미설치면 False)
      links     : 경사를 구하려 한 신규 링크 수
      missing   : 그중 표고를 얻지 못해 경사 0.0 이 된 링크 수
    반환 {"requested", "opened", "links", "missing_links", "missing_ratio", "max_ratio", "exceeded"}
      exceeded 는 --dem 을 지정했는데 결측 비율이 max_ratio 를 넘을 때만 True.
      --dem 을 주지 않은 실행은 경사 0.0 이 의도된 동작이므로 exceeded=False 다.
    """
    ratio = (missing / links) if links else 0.0
    return {"requested": bool(requested), "opened": bool(opened), "links": int(links),
            "missing_links": int(missing), "missing_ratio": round(ratio, 4),
            "max_ratio": max_ratio,
            "exceeded": bool(requested and links and ratio > max_ratio)}


class _DemSampler:
    def __init__(self, path):
        self.ds = None
        # 결측 집계 — 표고를 얻지 못해도 경사 0.0 으로 계속 진행하는 동작은 그대로 두고,
        # 몇 개 링크가 그렇게 됐는지만 센다(main 이 리포트에 적고 임계 초과 시 실패 처리).
        self.requested = bool(path)      # --dem 지정 여부
        self.links = 0                   # slope_deg 를 구하려 한 링크 수
        self.missing = 0                 # 그중 한쪽 끝이라도 표고가 없어 0.0 이 된 수
        if path and not os.path.exists(path):
            print(f"  경고: DEM 파일이 없습니다: {path} — 신규 링크 경사가 전부 0.0 이 됩니다")
        if path and os.path.exists(path):
            try:
                import rasterio
                self.ds = rasterio.open(path)
                self.band = self.ds.read(1)
                self.nodata = self.ds.nodata
                warn = dem_crs_warning(getattr(self.ds, "crs", None))
                if warn:
                    print("  경고: " + warn)
            except Exception as e:      # rasterio 미설치 등 — 경사 0 으로 진행
                print(f"  (DEM 미사용: {e})")

    def elev(self, x, y):
        """EPSG:5186 좌표의 표고. 실패 시 None."""
        if self.ds is None:
            return None
        try:
            r, c = self.ds.index(x, y)
            if 0 <= r < self.band.shape[0] and 0 <= c < self.band.shape[1]:
                v = float(self.band[r, c])
                if self.nodata is not None and v == self.nodata:
                    return None
                return v
        except Exception:
            return None
        return None

    def note_missing(self):
        """좌표를 알 수 없어 slope_deg 를 부르지 못한 링크 — 결측으로 센다."""
        self.links += 1
        self.missing += 1

    def slope_deg(self, xy1, xy2, length_m):
        e1, e2 = self.elev(*xy1), self.elev(*xy2)
        self.links += 1
        if e1 is None or e2 is None:
            self.missing += 1            # 범위 밖·nodata·예외·DEM 미사용 — 반환값(0.0)은 종전과 같다
        if e1 is None or e2 is None or length_m <= 0:
            return 0.0
        return round(abs(math.degrees(math.atan2(e2 - e1, length_m))), 2)


def _route_corridors(G, legs, to_5186):
    """기준 그래프에서 6개 leg 라우팅 -> 회랑 폴리라인(5186) 목록."""
    from route_service.engine.graph import NetworkStore
    from route_service.engine.planner import plan
    from route_service.engine.profiles import get_profile
    from route_service.engine.snap import snap

    store = NetworkStore()
    store.load_graph_object(G.copy(), version="corridor-base", region="")
    p = get_profile("wheelchair_manual")
    allowed = store.reachable_nodes(p, p.max_slope_deg + 4.0)
    lines = []
    for i, leg in enumerate(legs):
        # 스냅·라우팅이 실패하면 어느 leg 인지 밝힌다. leg 이름 없이 SnapError/KeyError/NoRouteError 만
        # 올라가면 여러 leg 중 어느 항목(좌표 누락·보행망 밖·경로 없음)이 문제인지 알 수 없다.
        # 성공할 때의 동작(스냅 결과·경로)은 종전과 같다.
        name = str(leg.get("name") or "#%d" % (i + 1)) if isinstance(leg, dict) else "#%d" % (i + 1)
        try:
            s = snap(store, leg["from"][0], leg["from"][1], p, 300, allowed=allowed)
            g = snap(store, leg["to"][0], leg["to"][1], p, 300, allowed=allowed)
        except Exception as e:
            raise RuntimeError("leg '%s' 의 출발/도착 좌표를 보행망에 붙이지 못했습니다 (%s: %s)"
                               % (name, type(e).__name__, e)) from e
        try:
            r = plan(store, s["node_id"], g["node_id"], p)["routes"][0]
        except Exception as e:
            raise RuntimeError("leg '%s' 의 경로를 찾지 못했습니다 (%s → %s, %s: %s)"
                               % (name, s["node_id"], g["node_id"], type(e).__name__, e)) from e
        lines.append([to_5186(lng, lat) for lat, lng in r["geometry"]])
    return lines


def _in_corridor_fn(lines, buffer_m):
    """회랑 판정 함수 — 폴리라인 샘플점 KDTree 로 근사(샘플 간격 << 버퍼라 오차 미미)."""
    import numpy as np
    from scipy.spatial import cKDTree
    pts = []
    step = min(10.0, buffer_m / 4.0)
    for line in lines:
        for (x1, y1), (x2, y2) in zip(line[:-1], line[1:]):
            n = max(2, int(math.hypot(x2 - x1, y2 - y1) / step) + 1)
            for t in np.linspace(0.0, 1.0, n):
                pts.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
    tree = cKDTree(np.array(pts))

    def inside(x, y):
        d, _ = tree.query((x, y))
        return d <= buffer_m
    return inside


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True, help="기준 그래프 (횡단보도 미반영본)")
    ap.add_argument("--pednet-node", required=True)
    ap.add_argument("--pednet-link", required=True)
    ap.add_argument("--crosswalks", required=True, help="병합 횡단보도 점 (시+OSM)")
    ap.add_argument("--legs", required=True)
    ap.add_argument("--exclude", default="",
                    help="교체에서 제외할 leg 이름 접두어 (쉼표 구분). "
                         "예: '3b' — 안양역 동서 단절(#30)은 topo 로도 못 잇는데 "
                         "우회로의 OSM 만 제거돼 인플레가 커진다(실측 +234%%)")
    ap.add_argument("--dem")
    ap.add_argument("--buffer", type=float, default=BUFFER_M)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report")
    ap.add_argument("--allow-missing-dem", action="store_true",
                    help="DEM 표고 결측 비율이 %d%%%% 를 넘어도 저장하고 정상 종료한다 "
                         "(기본: 저장하지 않고 종료 코드 %d)" % (DEM_MISSING_MAX_RATIO * 100, EXIT_DEM_MISSING))
    ap.add_argument("--overwrite-input", action="store_true",
                    help="--graph 와 같은 경로에 저장하는 것을 허용한다(원본은 <경로>.bak 으로 남긴다)")
    args = ap.parse_args()
    # 출력이 입력과 같은 경로면 계산 전에 멈춘다 — 입력 그래프를 그 자리에서 덮어쓰면 되돌릴 수 없다.
    from route_service.topomap import graphio
    try:
        graphio.check_output_path(args.out, args.graph, args.overwrite_input)
    except graphio.GraphIOError as e:
        ap.error(str(e))

    import networkx as nx
    import numpy as np
    from scipy.spatial import cKDTree

    to_5186 = _tf()
    stat = {"buffer_m": args.buffer}

    with open(args.graph, "rb") as f:
        G = pickle.load(f)
    from route_service.engine.graph import normalize_graph
    G = normalize_graph(G)
    with open(args.legs, "r", encoding="utf-8") as f:
        legs = json.load(f)

    # [1] 회랑
    excl = [e.strip() for e in args.exclude.split(",") if e.strip()]
    use_legs = [l for l in legs
                if not any(str(l.get("name", "")).startswith(e) for e in excl)]
    if len(use_legs) != len(legs):
        print(f"      제외 leg: {[l['name'] for l in legs if l not in use_legs]}")
    lines = _route_corridors(G, use_legs, to_5186)
    inside = _in_corridor_fn(lines, args.buffer)
    print(f"[1/6] 회랑 확보: leg {len(lines)}개, 버퍼 {args.buffer:.0f}m")

    XY = {n: to_5186(d["lon"], d["lat"]) for n, d in G.nodes(data=True)}
    osm_in = {n for n in G.nodes() if inside(*XY[n])}
    stat["osm_nodes_in_corridor"] = len(osm_in)

    # [2] topo 부분망
    tnodes = {}
    with open(args.pednet_node, "r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            xy = to_5186(float(r["X"]), float(r["Y"]))
            if inside(*xy):
                tnodes[r["NODE_ID"]] = {"lat": float(r["Y"]), "lon": float(r["X"]),
                                        "xy": xy, "node_type": r.get("NODE_TYPE") or "unknown"}
    tlinks = []
    with open(args.pednet_link, "r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["F_NODE"] in tnodes and r["T_NODE"] in tnodes:
                tlinks.append(r)
    print(f"[2/6] 회랑 내 topo 부분망: 노드 {len(tnodes)} / 링크 {len(tlinks)}")
    stat["topo_nodes"], stat["topo_links"] = len(tnodes), len(tlinks)

    # topo 부분망 그래프 (규칙 C 브리징용)
    T = nx.Graph()
    for nid, d in tnodes.items():
        T.add_node(nid)
    for r in tlinks:
        T.add_edge(r["F_NODE"], r["T_NODE"])

    # 도로 선분 (교차 판정용) — 기준 그래프의 road/underpass
    road_segs = [(XY[u], XY[v]) for u, v, d in G.edges(data=True)
                 if d.get("link_type") in ("road", "underpass")
                 and (u in osm_in or v in osm_in)]
    road_mid = np.array([((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in road_segs]) \
        if road_segs else np.empty((0, 2))
    road_tree = cKDTree(road_mid) if len(road_mid) else None
    road_halflen = [math.hypot(b[0] - a[0], b[1] - a[1]) / 2 for a, b in road_segs]
    max_half = max(road_halflen) if road_halflen else 0.0

    # 횡단보도 점 (규칙 C 게이트)
    with open(args.crosswalks, "r", encoding="utf-8") as f:
        cwgj = json.load(f)
    cw_pts = [to_5186(*f_["geometry"]["coordinates"][:2]) for f_ in cwgj["features"]
              if (f_.get("geometry") or {}).get("type") == "Point"]
    cw_tree = cKDTree(np.array(cw_pts)) if cw_pts else None

    def crosses_road(a, b):
        if road_tree is None:
            return False
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        r = math.hypot(b[0] - a[0], b[1] - a[1]) / 2 + max_half + 1.0
        for i in road_tree.query_ball_point(mid, r=r):
            if _seg_intersect(a, b, road_segs[i][0], road_segs[i][1]):
                return True
        return False

    def near_crosswalk(a, b):
        if cw_tree is None:
            return False
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        d, _ = cw_tree.query(mid)
        return d <= CROSS_NEAR

    # [3] 규칙 C 브리징 — 거리 오름차순 그리디, union-find 로 컴포넌트 관리
    ids = list(tnodes.keys())
    pos = np.array([tnodes[n]["xy"] for n in ids])
    ttree = cKDTree(pos)
    comp = {n: i for i, c in enumerate(nx.connected_components(T)) for n in c}

    pairs = []
    for i, j in ttree.query_pairs(r=BRIDGE_MAX):
        a, b = ids[i], ids[j]
        if comp[a] != comp[b]:
            pairs.append((math.hypot(*(pos[i] - pos[j])), a, b))
    pairs.sort()

    # union-find 를 컴포넌트 id 위에 얹는다
    cparent = {c: c for c in set(comp.values())}

    def cfind(c):
        while cparent[c] != c:
            cparent[c] = cparent[cparent[c]]
            c = cparent[c]
        return c

    bridges = []
    n_cross, n_side, n_blocked = 0, 0, 0
    for d, a, b in pairs:
        ca, cb = cfind(comp[a]), cfind(comp[b])
        if ca == cb:
            continue
        pa, pb = tnodes[a]["xy"], tnodes[b]["xy"]
        if crosses_road(pa, pb):
            if not near_crosswalk(pa, pb):
                n_blocked += 1
                continue
            lt = "crossing"
            n_cross += 1
        else:
            lt = "sidewalk"
            n_side += 1
        bridges.append((a, b, d, lt))
        cparent[ca] = cb
    print(f"[3/6] 규칙 C 브리징: {len(bridges)}건 (보도 {n_side} / 횡단 {n_cross} / "
          f"무단횡단 기각 {n_blocked})")
    stat.update(bridges=len(bridges), bridge_sidewalk=n_side,
                bridge_crossing=n_cross, bridge_blocked=n_blocked)

    # [4] 회랑 내부 OSM walkable 링크 제거 — **topo 보도가 실재하는 곳만**.
    # topo 미커버 구간(보도 면형 미기재 이면도로 등)까지 제거하면 사각지대가 생겨
    # 과잉 우회(실측: 2a 101m→1,418m)가 발생한다. 미커버 구간은 OSM 폴백을 유지해
    # 회랑 안에서도 topo(Silver)·OSM(Bronze) 이 혼재하는 하이브리드로 둔다.
    topo_pts = []
    for r in tlinks:
        (x1, y1), (x2, y2) = tnodes[r["F_NODE"]]["xy"], tnodes[r["T_NODE"]]["xy"]
        n = max(2, int(math.hypot(x2 - x1, y2 - y1) / 10.0) + 1)
        for t in np.linspace(0.0, 1.0, n):
            topo_pts.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
    topo_cover = cKDTree(np.array(topo_pts)) if topo_pts else None

    def covered(u, v):
        if topo_cover is None:
            return False
        (x1, y1), (x2, y2) = XY[u], XY[v]
        for t in (0.0, 0.5, 1.0):
            d_, _ = topo_cover.query((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
            if d_ > COVER_RADIUS:
                return False
        return True

    removed = []
    for u, v, d in list(G.edges(data=True)):
        if (u in osm_in and v in osm_in and d.get("link_type") in REMOVE_TYPES
                and covered(u, v)):
            removed.append((u, v))
            G.remove_edge(u, v)
    stat["osm_links_removed"] = len(removed)
    iso = [n for n in osm_in if G.degree(n) == 0]
    G.remove_nodes_from(iso)
    stat["osm_nodes_removed"] = len(iso)
    print(f"[4/6] OSM 링크 제거 {len(removed)} / 고립 노드 제거 {len(iso)}")

    # [5] topo 삽입 + 스티칭
    dem = _DemSampler(args.dem)
    for nid, d in tnodes.items():
        G.add_node("T" + nid, lat=d["lat"], lon=d["lon"], node_type=d["node_type"])

    def _add_edge(u, v, length, lt, width=None, surface=None, name=None, **extra):
        def _xy(n):
            if isinstance(n, str) and n.startswith("T") and n[1:] in tnodes:
                return tnodes[n[1:]]["xy"]
            return XY.get(n)
        xy_u, xy_v = _xy(u), _xy(v)
        if xy_u and xy_v:
            slope = dem.slope_deg(xy_u, xy_v, length)
        else:
            slope = 0.0
            dem.note_missing()
        G.add_edge(u, v, length=round(float(length), 2), slope=slope, link_type=lt,
                   width=width, curb_cut=None, tactile_paving=None, surface=surface,
                   link_name=name, geometry=None, topo_source="topo1k", **extra)

    for r in tlinks:
        w = None
        try:
            w = float(r["WIDTH"]) if (r.get("WIDTH") or "").strip() else None
        except ValueError:
            pass
        _add_edge("T" + r["F_NODE"], "T" + r["T_NODE"], float(r["LENGTH"]),
                  r.get("LINK_TYPE") or "sidewalk", width=w,
                  surface=(r.get("SURFACE") or None), name=(r.get("LINK_NAME") or None))
    for a, b, d, lt in bridges:
        _add_edge("T" + a, "T" + b, max(d, 0.5), lt, bridged=True)

    # 스티칭: 회랑 내부에 남은 OSM 노드(경계 스텁·보존 구조물) -> 최근접 topo 노드
    stitch = 0
    for n in osm_in:
        if n not in G or G.degree(n) == 0:
            continue
        d, i = ttree.query(XY[n])
        if d <= STITCH_MAX:
            _add_edge(n, "T" + ids[i], max(float(d), 0.5), "sidewalk", stitched=True)
            stitch += 1
    stat["stitch_links"] = stitch
    print(f"[5/6] topo 삽입 완료 + 스티칭 {stitch}건")

    # DEM 결측 집계 — 표고를 얻지 못한 링크는 경사 0.0('평지')으로 들어가 있다.
    dem_stat = dem_missing_summary(dem.requested, dem.ds is not None, dem.links, dem.missing)
    stat["dem"] = dem_stat
    if dem_stat["requested"]:
        print(f"      DEM 표고 결측: 신규 링크 {dem_stat['links']}개 중 {dem_stat['missing_links']}개 "
              f"({dem_stat['missing_ratio'] * 100:.1f}%) — 경사 0.0 처리")
    else:
        print("      DEM 미지정(--dem 없음) — 신규 링크 경사는 전부 0.0")
    dem_failed = dem_stat["exceeded"] and not args.allow_missing_dem
    if dem_stat["exceeded"]:
        bar = "!" * 78
        print(bar)
        print(f"!! 경고: DEM 표고 결측 {dem_stat['missing_ratio'] * 100:.1f}% "
              f"(허용 {DEM_MISSING_MAX_RATIO * 100:.0f}%) — 결측 링크는 경사 0.0 으로 들어가 "
              "경사 차단·비용 가중이 적용되지 않습니다.")
        print("!! DEM 파일 경로·범위·좌표계(EPSG:%d)를 확인하십시오." % DEM_EXPECTED_EPSG)
        if dem_failed:
            print(f"!! 그래프를 저장하지 않고 종료 코드 {EXIT_DEM_MISSING} 로 끝냅니다 "
                  "(그대로 진행하려면 --allow-missing-dem).")
        else:
            print("!! --allow-missing-dem 지정 — 그대로 저장합니다.")
        print(bar)

    # [6] 저장 — 임시 파일에 쓴 뒤 교체한다(쓰는 도중 중단돼도 잘린 그래프 파일이 남지 않는다).
    # DEM 결측 초과로 실패 처리할 때는 저장하지 않는다 — 뒤 단계가 경사 없는 그래프를 이어받지 않게 한다.
    if not dem_failed:
        graphio.save_graph(G, args.out, input_path=args.graph, overwrite_input=args.overwrite_input)
    stat["saved"] = not dem_failed
    stat["nodes"], stat["links"] = G.number_of_nodes(), G.number_of_edges()
    lt_cnt = {}
    for _u, _v, d in G.edges(data=True):
        lt_cnt[d["link_type"]] = lt_cnt.get(d["link_type"], 0) + 1
    stat["link_types"] = lt_cnt
    print(f"[6/6] {'저장' if not dem_failed else '저장 안 함'}: {args.out}  "
          f"노드 {stat['nodes']} / 링크 {stat['links']}")
    print(f"      링크타입: {lt_cnt}")

    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)) or ".", exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(stat, f, ensure_ascii=False, indent=1)
        print(f"      리포트: {args.report}")
    if dem_failed:
        sys.exit(EXIT_DEM_MISSING)


if __name__ == "__main__":
    main()
