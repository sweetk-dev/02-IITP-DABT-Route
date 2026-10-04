# -*- coding: utf-8 -*-
"""수치지형도 계단 면형과 겹치는 보행망 링크를 steps 로 재분류한다 (v1.21.0).

배경 (실측 2026-09-04)
  보행망 그래프의 `link_type='steps'` 는 47개뿐이고 대부분 OSM 유래다. 수치지형도
  1:1,000 의 계단 면형(C0390000)은 안양에만 1,715개 있는데 추출 파이프라인이
  읽지 않았다. 다만 계단 면형은 인도 면형과 사실상 겹치지 않아(실측 0~2.2%)
  대부분은 애초에 보행망이 지나지 않는 곳(건물 진입·공원 계단)이다.
  실제로 위험한 것은 **링크가 계단을 3m 이상 관통하면서 휠체어 통행 가능으로
  판정되던 경우**이고, 실측 19개다. 이 스크립트가 그 19개를 잡는다.

  휠체어 프로필은 이미 avoid=("steps","overpass","underpass") 로 계단을 회피한다.
  즉 로직은 멀쩡했고 라벨이 없었을 뿐이다.

사용
  # 감사만 (그래프 수정 없음)
  python scripts/mark_stairs_from_topomap.py --obstacles data/obstacles_anyang.geojson \
      --links data/db_export/mv_pednet_link.csv --nodes data/db_export/mv_pednet_node.csv

  # 그래프에 반영
  python scripts/mark_stairs_from_topomap.py --obstacles data/obstacles_anyang.geojson \
      --graph data/network_anyang_hybrid.gpickle --out data/network_anyang_stairs.gpickle
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.engine.geo import haversine_m  # noqa: E402
from route_service.topomap.obstacles import (OVERLAP_RATIO_MIN,  # noqa: E402
                                             STAIRS_CROSS_MIN_M,
                                             STRAIGHTNESS_MAX, ObstacleIndex)

# 이미 통행 불가로 취급되는 링크 종류 — 재분류 대상이 아니다.
ALREADY_AVOIDED = ("steps", "overpass", "underpass", "elevator")


def _line(a, b):
    from shapely.geometry import LineString
    return LineString([(a[1], a[0]), (b[1], b[0])])   # (lon, lat)


def _passes_guards(overlap_m: float, straight_m: float, declared_m: float) -> bool:
    if straight_m <= 0:
        return False
    if declared_m and declared_m / straight_m > STRAIGHTNESS_MAX:
        return False
    return overlap_m / straight_m >= OVERLAP_RATIO_MIN


def should_reclassify(overlap_m: float, base_m: float, declared_m: float, min_m: float) -> bool:
    """링크를 steps 로 재분류할지 — 감사(audit_csv)와 반영(apply_graph)이 함께 쓰는 단일 판정.

    조건은 셋 다 만족해야 한다.
      1) 계단 면형 관통 길이 overlap_m 이 min_m(기본 STAIRS_CROSS_MIN_M = 3m) 이상
      2) 직선성 가드: declared_m / base_m ≤ STRAIGHTNESS_MAX — 기재 길이가 판정에 쓴 선보다
         훨씬 길면 실제 선형은 곡선이라 "직선이 계단을 지난다"는 판정을 믿을 수 없다
      3) 관통비 가드: overlap_m / base_m ≥ OVERLAP_RATIO_MIN — 관통이 링크의 일부에 그치면
         계단 모서리를 스친 것일 수 있다

    길이 조건(1)만 보면 200m 보도가 계단 면형 모서리를 3m 스친 것만으로 링크 전체가 steps 가
    되어 휠체어 프로필에서 통째로 빠진다. 감사 출력의 "재분류 대상" 건수와 실제 반영 건수가
    달라지는 것도 이 때문이므로 두 경로가 이 함수 하나를 쓴다.

    인자
      overlap_m  : 판정선이 계단 면형과 겹치는 길이(m)
      base_m     : 판정선의 길이(m) — 관통 길이를 잰 바로 그 선의 길이
      declared_m : 링크에 기재된 길이(m). 0/None 이면 직선성 가드는 건너뛴다
      min_m      : 관통 길이 하한(m)
    반환
      재분류 대상이면 True. base_m 이 0 이하면 False.
    """
    if overlap_m < min_m:
        return False
    return _passes_guards(overlap_m, base_m, declared_m)


def edge_stairs_verdict(idx: ObstacleIndex, G, u, v, min_m: float):
    """그래프 링크 하나의 재분류 판정. 반환 (재분류 여부, 관통 길이 m).

    판정선은 engine.graph.edge_coords 가 주는 좌표열이다.
      · geometry 가 없는 링크(대부분) — 두 노드를 잇는 직선. base_m 은 두 노드의 직선거리이고
        감사(audit_csv)의 계산과 완전히 같다.
      · geometry 가 있는 링크 — 실제 선형으로 관통 길이를 재므로 base_m 도 그 선형의 길이로 잡는다.
        (직선거리를 쓰면 굽은 링크에서 관통비가 부풀고, 선형을 이미 반영했는데도 직선성 가드에 걸린다.)
    좌표가 2개 미만이면 (False, 0.0).
    """
    from route_service.engine.graph import edge_coords
    from shapely.geometry import LineString

    coords = edge_coords(G, u, v)
    if len(coords) < 2:
        return False, 0.0
    m = idx.stairs_overlap_m(LineString([(lo, la) for la, lo in coords]))
    if m < min_m:
        return False, m
    base = sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(coords[:-1], coords[1:]))
    declared = float(G[u][v].get("length") or 0)
    return should_reclassify(m, base, declared, min_m), m


def audit_csv(idx: ObstacleIndex, links_csv: str, nodes_csv: str, min_m: float):
    N = {r["node_id"]: (float(r["lat"]), float(r["lon"]))
         for r in csv.DictReader(open(nodes_csv, encoding="utf-8"))}
    rows = []
    for L in csv.DictReader(open(links_csv, encoding="utf-8")):
        a, b = N.get(L["f_node"]), N.get(L["t_node"])
        if not a or not b:
            continue
        m = idx.stairs_overlap_m(_line(a, b))
        if m < min_m:
            continue
        straight = haversine_m(a[0], a[1], b[0], b[1])
        ok = should_reclassify(m, straight, float(L.get("length_m") or 0), min_m)
        rows.append((L, a, m, straight, ok))
    return rows


def reclassify_graph(idx: ObstacleIndex, G, min_m: float) -> list:
    """G 의 링크 중 재분류 대상을 steps 로 바꾼다(G 를 직접 고친다). 반환 [(u, v, 원래 타입, 관통 m)].

    판정은 edge_stairs_verdict — 관통 길이뿐 아니라 감사와 같은 직선성·관통비 가드를 통과해야 한다.
    이미 회피되는 타입(ALREADY_AVOIDED)은 건드리지 않는다.
    """
    changed = []
    for u, v, d in G.edges(data=True):
        if d.get("link_type") in ALREADY_AVOIDED:
            continue
        ok, m = edge_stairs_verdict(idx, G, u, v, min_m)
        if ok:
            d["_orig_link_type"] = d.get("link_type")
            d["link_type"] = "steps"
            d["stairs_overlap_m"] = round(m, 1)
            d["attr_source"] = "topo1k:C0390000"
            changed.append((u, v, d["_orig_link_type"], round(m, 1)))
    return changed


def apply_graph(idx: ObstacleIndex, graph_path: str, out_path: str, min_m: float,
                overwrite_input: bool = False):
    """그래프 파일을 읽어 재분류하고 out_path 에 저장한다. 반환은 reclassify_graph 와 같다.

    out_path 가 graph_path 와 같으면 graphio.GraphIOError(overwrite_input=True 면 .bak 을 남기고 진행).
    """
    import pickle

    from route_service.topomap import graphio

    graphio.check_output_path(out_path, graph_path, overwrite_input)
    with open(graph_path, "rb") as fp:
        G = pickle.load(fp)
    changed = reclassify_graph(idx, G, min_m)
    # 임시 파일에 쓴 뒤 교체한다 — 쓰는 도중 중단돼도 잘린 그래프 파일이 남지 않는다.
    graphio.save_graph(G, out_path, input_path=graph_path, overwrite_input=overwrite_input)
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--obstacles", required=True, help="장애물 geojson (topomap.obstacles 산출)")
    ap.add_argument("--graph")
    ap.add_argument("--out")
    ap.add_argument("--links")
    ap.add_argument("--nodes")
    ap.add_argument("--min-overlap-m", type=float, default=STAIRS_CROSS_MIN_M)
    ap.add_argument("--overwrite-input", action="store_true",
                    help="--graph 와 같은 경로에 저장하는 것을 허용한다(원본은 <경로>.bak 으로 남긴다)")
    a = ap.parse_args()
    # 출력이 입력과 같은 경로면 장애물 색인을 만들기 전에 멈춘다.
    if a.graph and a.out:
        from route_service.topomap import graphio
        try:
            graphio.check_output_path(a.out, a.graph, a.overwrite_input)
        except graphio.GraphIOError as e:
            ap.error(str(e))

    idx = ObstacleIndex.from_geojson(a.obstacles)
    print("장애물 색인:", idx.counts())

    if a.graph:
        if not a.out:
            ap.error("--graph 를 쓰면 --out 이 필요합니다")
        changed = apply_graph(idx, a.graph, a.out, a.min_overlap_m, overwrite_input=a.overwrite_input)
        print("steps 로 재분류한 링크: %d개 -> %s" % (len(changed), a.out))
        for u, v, old, m in changed[:30]:
            print("   %s-%s  %s -> steps  (계단 관통 %.1fm)" % (u, v, old, m))
        return

    if not (a.links and a.nodes):
        ap.error("--graph 또는 --links/--nodes 중 하나가 필요합니다")
    rows = audit_csv(idx, a.links, a.nodes, a.min_overlap_m)
    risky = [r for r in rows if r[0]["link_type"] not in ALREADY_AVOIDED]
    marked = [r for r in risky if r[4]]
    print("계단을 %.1fm 이상 관통: 링크 %d개 | 휠체어 통행 가능 %d개 | 가드 통과(재분류 대상) %d개"
          % (a.min_overlap_m, len(rows), len(risky), len(marked)))
    for L, xy, m, st, _ok in sorted(marked, key=lambda x: -x[2]):
        print("   %-9s %-7s len=%-8s 직선%5.0fm 관통%5.1fm(%3.0f%%)  %.6f,%.6f  %s"
              % (L["link_type"], L.get("topo_source") or "OSM", L["length_m"], st, m,
                 100 * m / st, xy[0], xy[1], L.get("link_name") or ""))


if __name__ == "__main__":
    main()
