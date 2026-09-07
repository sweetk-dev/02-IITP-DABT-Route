# -*- coding: utf-8 -*-
"""우회 삼각형 직결 링크 정제 실행 (#67, v1.23.0).

    python scripts/refine_detour_links.py --graph data/network_anyang_hybrid.gpickle \\
        --layers data/topomap_layers --dem data/dem/anyang_5m.tif \\
        --out data/network_anyang_hybrid_r1.gpickle --report data/refine_report.csv --version anyang-hybrid-2026Q3r1

--layers 는 scripts/build_topomap_layers.py 산출(sidewalk_polys.geojson, obstacles.geojson).
--out 을 주지 않으면 보고서만 낸다. 활성 하한은 런타임 프로필이 판단하므로 채택분은 전부 넣는다(--min-confidence 로 제한 가능).
"""
from __future__ import annotations

import argparse
import csv
import os
import pickle
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.topomap import refine as rf  # noqa: E402
from route_service.topomap.obstacles import ObstacleIndex  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--layers", required=True)
    ap.add_argument("--dem")
    ap.add_argument("--out")
    ap.add_argument("--report", default="data/refine_report.csv")
    ap.add_argument("--version")
    ap.add_argument("--min-confidence", type=float, default=0.0)
    a = ap.parse_args()
    t0 = time.time()
    with open(a.graph, "rb") as f:
        G = pickle.load(f)
    sw = rf.SidewalkIndex.from_geojson(os.path.join(a.layers, "sidewalk_polys.geojson"))
    ob = ObstacleIndex.from_geojson(os.path.join(a.layers, "obstacles.geojson"))
    dem = None
    if a.dem:
        from route_service.engine.dem import DemSampler
        dem = DemSampler(a.dem)
    print("graph %d/%d, sidewalk polys %d, obstacles %s (%.0fs)" % (
        G.number_of_nodes(), G.number_of_edges(), len(sw.geoms), ob.counts(), time.time() - t0), flush=True)
    res = rf.refine(G, sw, ob, dem)
    print("candidates %d → %s (%.0fs)" % (len(res["candidates"]), dict(res["reasons"]), time.time() - t0), flush=True)
    os.makedirs(os.path.dirname(os.path.abspath(a.report)), exist_ok=True)
    with open(a.report, "w", newline="", encoding="utf-8") as fp:
        w = csv.writer(fp)
        w.writerow(["a", "m", "b", "lat_a", "lon_a", "lat_b", "lon_b", "straight_m", "via_m", "ratio",
                    "type_am", "type_mb", "gate", "confidence", "penalties", "slope_deg"])
        for c in res["candidates"]:
            w.writerow([c["a"], c["m"], c["b"], "%.7f" % c["pa"][0], "%.7f" % c["pa"][1], "%.7f" % c["pb"][0], "%.7f" % c["pb"][1],
                        "%.1f" % c["straight_m"], "%.1f" % c["via_m"], "%.2f" % c["ratio"], c["type_am"], c["type_mb"],
                        c.get("gate") or "", c.get("confidence", ""), "|".join(c.get("penalties", [])), c.get("slope_deg", "")])
    ad = res["adopted"]
    if ad:
        import statistics
        cs = [c["confidence"] for c in ad]
        print("adopted %d: confidence min %.2f median %.2f, ≥0.60 %d / ≥0.55 %d / ≥0.40 %d" % (
            len(ad), min(cs), statistics.median(cs), sum(c >= 0.6 for c in cs), sum(c >= 0.55 for c in cs), sum(c >= 0.4 for c in cs)))
    if a.out:
        n = rf.apply(G, ad, a.min_confidence)
        if a.version:
            G.graph["network_version"] = a.version
        with open(a.out, "wb") as f:
            pickle.dump(G, f)
        print("applied %d derived links → %s (%d/%d)" % (n, a.out, G.number_of_nodes(), G.number_of_edges()))


if __name__ == "__main__":
    main()
