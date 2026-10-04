# -*- coding: utf-8 -*-
"""이미 만들어진 그래프의 횡단보도 부착 노드에 위치·건너는 거리(cw_points)를 채운다.

apply_city_crosswalks.py 의 [3] 부착 단계는 노드에 관리번호(cw_mgmt_nos)만 남겼다.
직진으로 지나는 교차로에서 "가는 길을 가로막는 횡단보도"만 골라 알리려면 각 횡단보도의
좌표와 건너는 거리가 필요하다. 그래프를 처음부터 다시 만들지 않고 그 값만 덧붙인다.
위상(노드·링크·길이)은 건드리지 않는다.

사용:
  python scripts/attach_crosswalk_points.py \\
      --graph data/network_in.gpickle \\
      --crosswalks data/crosswalks_anyang_city.geojson \\
      --out data/network_out.gpickle
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.topomap import graphio  # noqa: E402


def attach(G, features) -> dict:
    by_id = {}
    for feat in features:
        g = feat.get("geometry") or {}
        p = feat.get("properties") or {}
        if g.get("type") != "Point" or not p.get("mgmt_no"):
            continue
        by_id[p["mgmt_no"]] = (g["coordinates"][1], g["coordinates"][0], p.get("cw_length_m"))
    stat = {"nodes": 0, "points": 0, "missing": 0, "no_length": 0}
    for _n, a in G.nodes(data=True):
        ids = a.get("cw_mgmt_nos") or []
        if not ids:
            continue
        pts = []
        for mn in ids:
            rec = by_id.get(mn)
            if rec is None:
                stat["missing"] += 1
                continue
            lat, lon, length = rec
            if not length:
                stat["no_length"] += 1
            pts.append({"id": mn, "lat": round(float(lat), 7), "lon": round(float(lon), 7),
                        "length_m": (float(length) if length else None)})
        a["cw_points"] = pts
        stat["nodes"] += 1
        stat["points"] += len(pts)
    return stat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--crosswalks", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--overwrite-input", action="store_true",
                    help="--graph 와 같은 경로에 저장하는 것을 허용한다(원본은 <경로>.bak 으로 남긴다)")
    args = ap.parse_args()
    # 출력이 입력과 같은 경로면 계산 전에 멈춘다 — 입력 그래프를 그 자리에서 덮어쓰면 되돌릴 수 없다.
    try:
        graphio.check_output_path(args.out, args.graph, args.overwrite_input)
    except graphio.GraphIOError as e:
        ap.error(str(e))
    with open(args.graph, "rb") as f:
        G = pickle.load(f)
    with open(args.crosswalks, "r", encoding="utf-8") as f:
        feats = json.load(f).get("features", [])
    before = (G.number_of_nodes(), G.number_of_edges())
    stat = attach(G, feats)
    assert before == (G.number_of_nodes(), G.number_of_edges())
    # 임시 파일에 쓴 뒤 교체한다 — 쓰는 도중 중단돼도 잘린 그래프 파일이 남지 않는다.
    graphio.save_graph(G, args.out, input_path=args.graph, overwrite_input=args.overwrite_input)
    print("노드 %d곳에 횡단보도 %d건 기록 (원천에 없는 번호 %d / 길이 미기재 %d)"
          % (stat["nodes"], stat["points"], stat["missing"], stat["no_length"]))
    print("저장: %s  노드 %d / 링크 %d" % ((args.out,) + before))


if __name__ == "__main__":
    main()
