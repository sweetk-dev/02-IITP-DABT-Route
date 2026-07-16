# -*- coding: utf-8 -*-
"""1:1,000 수치지형도 -> 보행망 node/link 산출.

수치지형도(EPSG:5186)의 인도 면형·선형을 중심선화하고 위상을 구축한 뒤,
OSM 횡단보도를 병합해 engine.sources.tabular 규격의 node/link 를 만든다.

사용 예:
  # 1) OSM 횡단보도 먼저 확보 (인터넷 필요 — 로컬 venv 에서 실행)
  python scripts/fetch_osm_crossings.py --place "Anyang-si, Gyeonggi-do, South Korea" \
      --out data/osm_crossings.geojson

  # 2) 보행망 산출
  python scripts/build_pednet_from_topomap.py \
      --src "../91-조사설계_이동편의/경기도 안양시 지도/안양시 수치지형도/1_1000_2026-07-16" \
      --crossings data/osm_crossings.geojson \
      --out-node data/pednet_node.csv --out-link data/pednet_link.csv

  # 3) 그래프 빌드
  python scripts/build_network.py --source tabular \
      --node data/pednet_node.csv --link data/pednet_link.csv \
      --dem data/dem/anyang_5m.tif --out data/network_anyang_topo.gpickle \
      --version anyang-topo1k-2026Q3
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.topomap import extract_sheet, build_topology, grid_centerlines  # noqa: E402
from route_service.topomap.crossings import load_crossings, crossing_features  # noqa: E402

SRC_EPSG = 5186


def _transformers():
    from pyproj import Transformer
    fwd = Transformer.from_crs(f"EPSG:{SRC_EPSG}", "EPSG:4326", always_xy=True)
    inv = Transformer.from_crs("EPSG:4326", f"EPSG:{SRC_EPSG}", always_xy=True)
    return fwd.transform, inv.transform


def _sheets(src: str) -> list[str]:
    out = sorted(glob.glob(os.path.join(src, "**", "*.zip"), recursive=True))
    out += sorted(glob.glob(os.path.join(src, "**", "*.ngi"), recursive=True))
    # 공유 도엽(만안·동안 양쪽 배치)은 도엽번호 기준 1회만
    seen, uniq = set(), []
    import re
    for p in out:
        m = re.search(r"_(\d{9})_", os.path.basename(p))
        k = m.group(1) if m else p
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    return uniq


def _write_csv(path, rows, cols):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="도엽 폴더 (zip/ngi 재귀 탐색)")
    ap.add_argument("--crossings", help="OSM 횡단보도 GeoJSON (fetch_osm_crossings.py 산출)")
    ap.add_argument("--out-node", default="data/pednet_node.csv")
    ap.add_argument("--out-link", default="data/pednet_link.csv")
    ap.add_argument("--snap-tol", type=float, default=1.5, help="끝점 스냅 허용오차(m)")
    ap.add_argument("--min-component", type=float, default=30.0, help="고립 연결요소 최소 총연장(m)")
    ap.add_argument("--limit", type=int, help="도엽 수 제한 (디버그)")
    args = ap.parse_args()

    to_wgs84, to_5186 = _transformers()
    sheets = _sheets(args.src)
    if args.limit:
        sheets = sheets[:args.limit]
    if not sheets:
        sys.exit(f"도엽을 찾지 못했습니다: {args.src}")

    t0 = time.time()
    raw = []
    for i, p in enumerate(sheets, 1):
        raw.extend(extract_sheet(p))
        if i % 25 == 0 or i == len(sheets):
            print(f"  추출 {i}/{len(sheets)} 도엽, 피처 {len(raw)}", flush=True)
    print(f"[1/4] 추출 완료: {len(raw)} 피처 ({time.time()-t0:.1f}s)")

    polys = [f for f in raw if f.get("kind") == "sidewalk_poly"]
    feats = [f for f in raw if f.get("kind") != "sidewalk_poly"]

    # 보도 면형은 도엽 경계를 넘어 전역 dissolve 후 스트립 단위로 중심선화한다.
    # (개별 폴리곤 스켈레톤화는 인접 보도끼리 이어지지 않아 그래프가 파편화된다)
    t1 = time.time()
    feats.extend(grid_centerlines(polys))
    print(f"[2/4] 보도 dissolve·중심선화: 폴리곤 {len(polys)} -> "
          f"중심선 {sum(1 for f in feats if f['kind']=='sidewalk')} ({time.time()-t1:.1f}s)")

    kinds = {}
    for f in feats:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    print(f"      종류별: {kinds}")

    # 횡단보도는 위상 구축 **이전에** 붙인다. 보도 중심선 위로 투영해 T자 접합을
    # 만들어두면 build_topology 의 분할·스냅이 그대로 이어준다.
    if args.crossings and os.path.exists(args.crossings):
        xs = load_crossings(args.crossings, to_5186)
        cf, stat = crossing_features(feats, xs)
        feats.extend(cf)
        print(f"[3/4] 횡단보도 병합: OSM {len(xs)}건 -> {len(cf)} 링크 "
              f"(선형 {stat['linear']} / 점형 {stat['point']} / 미접합 {stat['skipped']})")
    else:
        print("[3/4] 횡단보도 GeoJSON 미지정 — 병합 건너뜀 "
              "(⚠️ 보도가 블록마다 끊긴 상태로 산출됩니다)")

    nodes, links = build_topology(feats, to_wgs84, tol=args.snap_tol,
                                  min_component_len=args.min_component)
    print(f"[4/4] 위상 구축: 노드 {len(nodes)} / 링크 {len(links)}")

    _write_csv(args.out_node, nodes, ["NODE_ID", "X", "Y", "NODE_TYPE"])
    _write_csv(args.out_link, links,
               ["LINK_ID", "F_NODE", "T_NODE", "LENGTH", "LINK_TYPE", "WIDTH",
                "SURFACE", "LINK_NAME", "ACCESSIBLE", "SHEET", "SOURCE"])
    tot = sum(l["LENGTH"] for l in links)
    print(f"\n산출: {args.out_node} / {args.out_link}")
    print(f"  노드 {len(nodes)} / 링크 {len(links)} / 총연장 {tot/1000:.1f} km "
          f"({time.time()-t0:.1f}s)")


def _coords_5186(nodes, to_5186):
    return {n["NODE_ID"]: to_5186(n["X"], n["Y"]) for n in nodes}


if __name__ == "__main__":
    main()
