# -*- coding: utf-8 -*-
"""수동 확인 링크 적용 (#94).

    python scripts/apply_manual_links.py --graph data/network_anyang_hybrid_r3.gpickle \\
        --links data/manual_links/anyang.json --out data/network_anyang_hybrid_r4.gpickle

--out 을 주지 않으면 적용 결과만 출력한다(그래프 저장 안 함).
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.topomap import manual_links as ml  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--links", default="data/manual_links/anyang.json")
    ap.add_argument("--out")
    a = ap.parse_args()
    with open(a.graph, "rb") as f:
        G = pickle.load(f)
    n0, e0 = G.number_of_nodes(), G.number_of_edges()
    report = ml.apply_manual_links(G, ml.load_links(a.links))
    for r in report:
        print("%-14s %-12s %s — %s %s" % (r["id"], r["status"], r.get("u"), r.get("v"),
                                         ("%.1fm" % r["length"]) if r.get("length") else ""))
    added = sum(1 for r in report if r["status"] == "added")
    print("적용 %d / 건너뜀 %d (노드 %d→%d, 링크 %d→%d)" % (
        added, len(report) - added, n0, G.number_of_nodes(), e0, G.number_of_edges()))
    if a.out:
        with open(a.out, "wb") as f:
            pickle.dump(G, f)
        print("→ %s" % a.out)


if __name__ == "__main__":
    main()
