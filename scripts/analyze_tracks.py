# -*- coding: utf-8 -*-
"""주행 GPS 트랙 vs 안내 경로 대조 분석.

실증의 부산물(트랙)로 세 가지를 자동 판정한다:
  1) 거리 정직성 — 실주행 거리 ≈ 안내 거리면 인플레는 정직한 증가,
     실주행 << 안내면 과잉 우회(위상·비용 문제) 후보
  2) 이탈 클러스터 — 안내 경로에서 threshold(기본 30m) 이상 벗어난 점들의 묶음
     = "실제로는 그 길로 안/못 간다" 신호. 위상 오류 보정 큐로 보낸다
  3) 요약 통계 — 완주 여부·소요·정확도 분포

입력: DB(--dsn, mv_route_track/meta) 또는 JSON 파일(--input, 아래 형식)
  [{"route_id":"r_x","geometry":[[lat,lng],...],"planned_dist_m":1234,
    "points":[{"seq":0,"lat":..,"lng":..,"ts":"..."}, ...]}, ...]

사용:
  python scripts/analyze_tracks.py --dsn "$POI_DB_DSN" --out data/db_export/track_analysis.json
  python scripts/analyze_tracks.py --input tracks.json --out track_analysis.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.engine.geo import haversine_m, point_segment_dist_m  # noqa: E402

OFF_ROUTE_M = 30.0      # 이탈 판정 거리
CLUSTER_GAP = 5         # 이탈 클러스터 분리 기준 (연속 seq 끊김 허용치)
MIN_CLUSTER = 3         # 이 미만 점수의 클러스터는 GPS 튐으로 무시


def _dist_to_line(lat, lng, geom) -> float:
    if not geom:
        return float("inf")
    if len(geom) == 1:
        return haversine_m(lat, lng, geom[0][0], geom[0][1])
    best = float("inf")
    for (a1, o1), (a2, o2) in zip(geom[:-1], geom[1:]):
        d = point_segment_dist_m(lat, lng, a1, o1, a2, o2)
        if d < best:
            best = d
    return best


def _track_len(points) -> float:
    total = 0.0
    for p, q in zip(points[:-1], points[1:]):
        total += haversine_m(p["lat"], p["lng"], q["lat"], q["lng"])
    return total


def analyze_route(route_id: str, geometry, planned_dist_m, points) -> dict:
    points = sorted(points, key=lambda p: int(p["seq"]))
    actual = _track_len(points)
    straight = (haversine_m(points[0]["lat"], points[0]["lng"],
                            points[-1]["lat"], points[-1]["lng"])
                if len(points) >= 2 else 0.0)

    # 이탈 클러스터
    clusters, cur = [], []
    for p in points:
        off = _dist_to_line(p["lat"], p["lng"], geometry or [])
        if off > OFF_ROUTE_M:
            if cur and int(p["seq"]) - int(cur[-1]["seq"]) > CLUSTER_GAP:
                clusters.append(cur)
                cur = []
            cur.append({**p, "off_m": round(off)})
        elif cur:
            clusters.append(cur)
            cur = []
    if cur:
        clusters.append(cur)
    clusters = [c for c in clusters if len(c) >= MIN_CLUSTER]

    honesty = None
    if planned_dist_m and actual:
        ratio = actual / float(planned_dist_m)
        # 실주행이 안내의 85% 미만 = 안내가 실제보다 길게 돌렸다(과잉 우회 후보)
        honesty = ("over_detour" if ratio < 0.85
                   else "honest" if ratio <= 1.25
                   else "off_route_heavy")

    return {
        "route_id": route_id,
        "point_cnt": len(points),
        "planned_dist_m": planned_dist_m,
        "actual_dist_m": round(actual),
        "straight_m": round(straight),
        "actual_vs_planned": round(actual / float(planned_dist_m), 2)
        if planned_dist_m else None,
        "verdict": honesty,
        "off_route_clusters": [
            {
                "point_cnt": len(c),
                "center": [round(sum(p["lat"] for p in c) / len(c), 6),
                           round(sum(p["lng"] for p in c) / len(c), 6)],
                "max_off_m": max(p["off_m"] for p in c),
                "seq_range": [int(c[0]["seq"]), int(c[-1]["seq"])],
            }
            for c in clusters
        ],
    }


def _load_db(dsn: str) -> list:
    from sqlalchemy import create_engine, text
    eng = create_engine(dsn, future=True)
    out = []
    with eng.connect() as conn:
        metas = conn.execute(text(
            "SELECT route_id, planned_dist_m, geometry FROM mv_route_track_meta"
        )).mappings().all()
        for m in metas:
            pts = conn.execute(text(
                "SELECT seq, lat, lon AS lng FROM mv_route_track "
                "WHERE route_id = :rid ORDER BY seq"), {"rid": m["route_id"]}
            ).mappings().all()
            if not pts:
                continue
            geom = m["geometry"]
            if isinstance(geom, str):
                geom = json.loads(geom)
            out.append({"route_id": m["route_id"], "geometry": geom,
                        "planned_dist_m": m["planned_dist_m"],
                        "points": [dict(p) for p in pts]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", help="postgresql+psycopg2://... (mv_route_track)")
    ap.add_argument("--input", help="JSON 파일 입력 (DB 대신)")
    ap.add_argument("--out", default="data/db_export/track_analysis.json")
    args = ap.parse_args()

    if args.input:
        with open(args.input, "r", encoding="utf-8") as f:
            routes = json.load(f)
    elif args.dsn:
        routes = _load_db(args.dsn)
    else:
        sys.exit("--dsn 또는 --input 필요")

    results = [analyze_route(r["route_id"], r.get("geometry"),
                             r.get("planned_dist_m"), r["points"]) for r in routes]
    summary = {
        "routes": len(results),
        "honest": sum(1 for r in results if r["verdict"] == "honest"),
        "over_detour": sum(1 for r in results if r["verdict"] == "over_detour"),
        "off_route_heavy": sum(1 for r in results if r["verdict"] == "off_route_heavy"),
        "off_route_clusters": sum(len(r["off_route_clusters"]) for r in results),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "results": results}, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))
    for r in results:
        print(f"  {r['route_id']}: 안내 {r['planned_dist_m']}m / 실주행 {r['actual_dist_m']}m "
              f"({r['actual_vs_planned']}) {r['verdict']} 이탈클러스터 {len(r['off_route_clusters'])}")
    print(f"산출: {args.out}")


if __name__ == "__main__":
    main()
