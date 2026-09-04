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
        ok = _passes_guards(m, straight, float(L.get("length_m") or 0))
        rows.append((L, a, m, straight, ok))
    return rows


def apply_graph(idx: ObstacleIndex, graph_path: str, out_path: str, min_m: float):
    import pickle

    from route_service.engine.graph import edge_coords
    from shapely.geometry import LineString

    with open(graph_path, "rb") as fp:
        G = pickle.load(fp)
    changed = []
    for u, v, d in G.edges(data=True):
        if d.get("link_type") in ALREADY_AVOIDED:
            continue
        coords = edge_coords(G, u, v)
        if len(coords) < 2:
            continue
        m = idx.stairs_overlap_m(LineString([(lo, la) for la, lo in coords]))
        if m >= min_m:
            d["_orig_link_type"] = d.get("link_type")
            d["link_type"] = "steps"
            d["stairs_overlap_m"] = round(m, 1)
            d["attr_source"] = "topo1k:C0390000"
            changed.append((u, v, d["_orig_link_type"], round(m, 1)))
    with open(out_path, "wb") as fp:
        pickle.dump(G, fp)
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--obstacles", required=True, help="장애물 geojson (topomap.obstacles 산출)")
    ap.add_argument("--graph")
    ap.add_argument("--out")
    ap.add_argument("--links")
    ap.add_argument("--nodes")
    ap.add_argument("--min-overlap-m", type=float, default=STAIRS_CROSS_MIN_M)
    a = ap.parse_args()

    idx = ObstacleIndex.from_geojson(a.obstacles)
    print("장애물 색인:", idx.counts())

    if a.graph:
        if not a.out:
            ap.error("--graph 를 쓰면 --out 이 필요합니다")
        changed = apply_graph(idx, a.graph, a.out, a.min_overlap_m)
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
