# -*- coding: utf-8 -*-
"""보도망·도로망 접합부 보강 실행 (#87).

    python scripts/restitch_boundary.py --graph data/network_anyang_hybrid_r2.gpickle \\
        --out data/network_anyang_hybrid_r3.gpickle --report data/restitch_report.csv

--out 을 주지 않으면 보고서만 낸다.
"""
from __future__ import annotations

import argparse
import csv
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.topomap import restitch as rs  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--out")
    ap.add_argument("--report", default="data/restitch_report.csv")
    ap.add_argument("--margin", type=float, default=rs.MARGIN_M)
    a = ap.parse_args()
    with open(a.graph, "rb") as f:
        G = pickle.load(f)
    items = rs.find_restitch(G, margin_m=a.margin)
    os.makedirs(os.path.dirname(os.path.abspath(a.report)) or ".", exist_ok=True)
    with open(a.report, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["osm_node", "topo_node", "existing_topo_node", "length_m", "existing_m", "lat", "lon"])
        for it in items:
            nd = G.nodes[it["t"]]
            w.writerow([it["o"], it["t"], it["via"], it["length"], it["d0"], nd["lat"], nd["lon"]])
    stitched = sum(1 for _u, _v, e in G.edges(data=True) if e.get("stitched"))
    print("기존 스티칭 %d / 추가 후보 %d → %s" % (stitched, len(items), a.report))
    if a.out:
        n = rs.apply_restitch(G, items)
        joins = rs.connect_dead_ends(G)
        print("보도 끝 연결 %d (링크 분할 %d)" % (len(joins), sum(1 for j in joins if j["split"])))
        with open(a.out, "wb") as f:
            pickle.dump(G, f)
        print("추가 %d → %s (노드 %d / 링크 %d)" % (n, a.out, G.number_of_nodes(), G.number_of_edges()))


if __name__ == "__main__":
    main()
