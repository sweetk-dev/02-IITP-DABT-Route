# -*- coding: utf-8 -*-
"""네트워크 품질 리포트 + 샘플 경로 검증 (CLI).

그래프를 새로 구축하거나 교체한 뒤 이 스크립트로 품질을 확인한다.
경사 커버리지가 0 이면 경사 회피가 동작하지 않는다는 뜻이므로 반드시 확인할 것.

사용 예)
  python scripts/inspect_network.py --network data/network_anyang.gpickle \
      --route 37.4025,126.9227,37.3856,126.9256
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # pragma: no cover
    pass

from route_service.engine.graph import NetworkStore  # noqa: E402
from route_service.engine.planner import NoRouteError, plan  # noqa: E402
from route_service.engine.profiles import get_profile  # noqa: E402
from route_service.engine.snap import snap  # noqa: E402
from route_service.engine.steps import build_steps  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="네트워크 품질 리포트")
    ap.add_argument("--network", default="data/network_anyang.gpickle")
    ap.add_argument("--route", action="append", default=[],
                    help="olat,olng,dlat,dlng — 프로필별 비교 경로(여러 번 지정 가능)")
    ap.add_argument("--steps", type=int, default=5, help="출력할 턴바이턴 스텝 수")
    args = ap.parse_args()

    store = NetworkStore()
    m = store.load(args.network)

    print("== 네트워크 ==")
    print("  노드 %d / 링크 %d" % (m["node_cnt"], m["edge_cnt"]))
    print("  경사 커버리지 %.1f%% / 보도폭 커버리지 %.1f%%"
          % (m["slope_coverage"] * 100, m["width_coverage"] * 100))
    if m["slope_coverage"] < 0.5:
        print("  ⚠ 경사 데이터가 없습니다 — 경사 회피가 동작하지 않습니다")
    b = m["bbox"]
    print("  범위 lat %.4f~%.4f / lng %.4f~%.4f"
          % (b["min_lat"], b["max_lat"], b["min_lng"], b["max_lng"]))

    print("== 링크 타입 ==")
    total = m["edge_cnt"] or 1
    for t, c in sorted(m["link_type_counts"].items(), key=lambda x: -x[1]):
        print("  %-10s %6d (%5.1f%%)" % (t, c, c / total * 100))

    G = store.graph
    steep = [d["slope"] for _u, _v, d in G.edges(data=True) if d["slope"] > 4.0]
    print("== 경사 ==")
    print("  4도 초과 링크 %d개 (%.1f%%) — 수동 휠체어 통행 불가 구간"
          % (len(steep), len(steep) / total * 100))

    for spec in args.route:
        try:
            olat, olng, dlat, dlng = [float(x) for x in spec.split(",")]
        except ValueError:
            print("경로 형식 오류: %s" % spec)
            continue
        print("\n===== 경로 %s =====" % spec)
        summaries = {}
        for pid in ("wheelchair_manual", "walk"):
            p = get_profile(pid)
            o = snap(store, olat, olng, p)
            d = snap(store, dlat, dlng, p)
            try:
                res = plan(store, o["node_id"], d["node_id"], p)
            except NoRouteError as e:
                print("  [%s] 경로 없음: %s" % (p.label, e))
                continue
            s = res["routes"][0]["summary"]
            summaries[pid] = s
            print("  [%-8s] %5dm / %3d분 / 계단 %d / 최대경사 %5.2f° / 평균 %4.2f° / 접근성 %.2f"
                  % (p.label, s["total_distance_m"], s["duration_sec"] // 60, s["stairs_cnt"],
                     s["max_slope_deg"], s["mean_slope_deg"], s["accessibility_score"]))
            if res["fallback"]["used"]:
                print("      ↳ %s" % res["fallback"]["reason"])
            for w in s["warnings"]:
                print("      ↳ 경고: %s" % w)
            if pid == "wheelchair_manual" and args.steps:
                steps = build_steps(G, res["routes"][0]["path"], p)
                print("      턴바이턴 %d스텝:" % len(steps))
                for st in steps[:args.steps]:
                    print("        %2d %s" % (st["idx"], st["instruction"]))

        if "wheelchair_manual" in summaries and "walk" in summaries:
            wc, wk = summaries["wheelchair_manual"], summaries["walk"]
            print("  → 휠체어 우회 %+dm / 최대경사 %.2f° → %.2f° / 계단 %d → %d"
                  % (wc["total_distance_m"] - wk["total_distance_m"],
                     wk["max_slope_deg"], wc["max_slope_deg"],
                     wk["stairs_cnt"], wc["stairs_cnt"]))


if __name__ == "__main__":
    main()
