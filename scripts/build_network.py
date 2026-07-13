# -*- coding: utf-8 -*-
"""보행 네트워크 그래프 구축 (CLI).

기존 build_network.py 는 인천 node/link + 90m DEM 경로가 하드코딩되어 있었다.
소스·DEM·출력 경로를 인자로 받고, 소스 어댑터(osm / tabular)를 선택할 수 있게 했다.

사용 예)
  # OSM 기반 안양 보행망 (융기원 원본 수령 전 선구축)
  python scripts/build_network.py --source osm --place "Anyang-si, Gyeonggi-do, South Korea" \
      --out data/network_anyang.gpickle --version anyang-osm-2026Q3

  # 안양 DEM(.img) 으로 경사 부여
  python scripts/build_network.py --source osm --place "Anyang-si, ..." \
      --dem data/dem/anyang_5m.img --out data/network_anyang.gpickle

  # node/link 표 기반 (인천 검증본 · 융기원 제공 데이터)
  python scripts/build_network.py --source tabular --node data/node.xlsx --link data/link.xlsx \
      --dem data/dem/DEM_인천.img --out data/network_incheon.gpickle
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.engine.graph import normalize_graph  # noqa: E402
from route_service.engine.sources import build_from_osm, build_from_tabular  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="보행 네트워크 그래프 구축")
    ap.add_argument("--source", choices=("osm", "tabular"), default="osm")
    ap.add_argument("--place", default="Anyang-si, Gyeonggi-do, South Korea")
    ap.add_argument("--bbox", help="min_lat,min_lng,max_lat,max_lng")
    ap.add_argument("--node", help="tabular: node 파일(xlsx/csv)")
    ap.add_argument("--link", help="tabular: link 파일(xlsx/csv)")
    ap.add_argument("--dem", help="DEM(.img) 경로 — 없으면 경사 0 으로 두고 진행")
    ap.add_argument("--out", default="data/network.gpickle")
    ap.add_argument("--version", default="unknown")
    args = ap.parse_args()

    bbox = None
    if args.bbox:
        bbox = tuple(float(x) for x in args.bbox.split(","))

    if args.source == "osm":
        print("[build] OSM 보행망 수집: %s" % (args.bbox or args.place))
        G = build_from_osm(place=args.place, bbox=bbox)
    else:
        if not (args.node and args.link):
            ap.error("--source tabular 은 --node, --link 가 필요합니다")
        print("[build] node/link 표 로드: %s / %s" % (args.node, args.link))
        G = build_from_tabular(args.node, args.link)

    G = normalize_graph(G)
    print("[build] 그래프: nodes=%d edges=%d" % (G.number_of_nodes(), G.number_of_edges()))

    if args.dem:
        print("[build] DEM 경사 부여: %s" % args.dem)
        from route_service.engine.dem import apply_slope_from_dem

        stat = apply_slope_from_dem(G, args.dem, bbox=bbox)
        print("[build] 경사 적용: %s" % json.dumps(stat, ensure_ascii=False))
    else:
        print("[build] DEM 미지정 — 경사 0 (경사 회피가 동작하지 않습니다)")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "wb") as f:
        pickle.dump(G, f, protocol=pickle.HIGHEST_PROTOCOL)
    print("[build] 저장 완료: %s (version=%s)" % (args.out, args.version))


if __name__ == "__main__":
    main()
