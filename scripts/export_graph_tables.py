# -*- coding: utf-8 -*-
"""보행 그래프(gpickle) -> node/link 테이블(CSV) 산출.

통합DB(iitp_db)의 mv_pednet_node / mv_pednet_link 적재용.
스키마는 scripts/sql/pednet_schema.sql 과 1:1 대응한다.

  python scripts/export_graph_tables.py \
      --graph data/network_anyang_enriched.gpickle \
      --out-dir data/db_export --version anyang-topo-enrich-2026Q3
"""
from __future__ import annotations

import argparse
import csv
import os
import pickle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--out-dir", default="data/db_export")
    ap.add_argument("--version", required=True, help="네트워크 버전 태그 (테이블에 기록)")
    args = ap.parse_args()

    with open(args.graph, "rb") as f:
        G = pickle.load(f)
    os.makedirs(args.out_dir, exist_ok=True)

    node_path = os.path.join(args.out_dir, "mv_pednet_node.csv")
    link_path = os.path.join(args.out_dir, "mv_pednet_link.csv")

    with open(node_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["node_id", "lat", "lon", "node_type",
                    "crosswalk_cnt", "cw_mgmt_nos", "network_version"])
        for n, d in G.nodes(data=True):
            w.writerow([n, d.get("lat"), d.get("lon"),
                        d.get("node_type") or "unknown",
                        int(d.get("crosswalk_cnt") or 0),
                        ",".join(d.get("cw_mgmt_nos") or []),
                        args.version])

    def _b(v):
        return {True: "true", False: "false"}.get(v, "")

    with open(link_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["f_node", "t_node", "length_m", "link_type", "width_m",
                    "slope_pct", "surface", "curb_cut", "tactile_paving",
                    "link_name", "topo_source", "cw_mgmt_no", "attr_source",
                    "network_version"])
        for u, v, d in G.edges(data=True):
            w.writerow([
                u, v, round(float(d.get("length") or 0), 2),
                d.get("link_type") or "unknown",
                d.get("width") or "",
                d.get("slope") if d.get("slope") is not None else "",
                d.get("surface") or "",
                _b(d.get("curb_cut")),
                _b(d.get("tactile_paving")),
                d.get("link_name") or "",
                d.get("topo_source") or "",
                d.get("cw_mgmt_no") or "",
                d.get("attr_source") or "",
                args.version,
            ])

    print(f"노드 {G.number_of_nodes()} -> {node_path}")
    print(f"링크 {G.number_of_edges()} -> {link_path}")


if __name__ == "__main__":
    main()
