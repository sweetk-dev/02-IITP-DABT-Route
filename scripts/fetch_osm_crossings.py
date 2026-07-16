# -*- coding: utf-8 -*-
"""OSM 횡단보도 추출 -> GeoJSON.

수치지형도에 횡단보도 레이어가 없어 OSM 기존 데이터로 보완한다.
인터넷 접근이 필요하므로 로컬 venv 에서 실행한다 (샌드박스는 overpass 차단).

  python scripts/fetch_osm_crossings.py \
      --place "Anyang-si, Gyeonggi-do, South Korea" --out data/osm_crossings.geojson
"""
from __future__ import annotations

import argparse
import json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--place", default="Anyang-si, Gyeonggi-do, South Korea")
    ap.add_argument("--bbox", help="min_lat,min_lng,max_lat,max_lng (place 대신)")
    ap.add_argument("--out", default="data/osm_crossings.geojson")
    args = ap.parse_args()

    import osmnx as ox

    tags = {"highway": "crossing", "footway": "crossing", "crossing": True}
    if args.bbox:
        s, w, n, e = [float(x) for x in args.bbox.split(",")]
        gdf = ox.features_from_bbox((w, s, e, n), tags)
    else:
        gdf = ox.features_from_place(args.place, tags)

    keep = gdf[gdf.geometry.type.isin(["LineString", "Point"])]
    feats = []
    for _, row in keep.iterrows():
        g = row.geometry
        feats.append({
            "type": "Feature",
            "geometry": json.loads(json.dumps(g.__geo_interface__)),
            "properties": {
                "crossing": str(row.get("crossing") or ""),
                "tactile_paving": str(row.get("tactile_paving") or ""),
                "kerb": str(row.get("kerb") or ""),
            },
        })
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, ensure_ascii=False)
    n_line = sum(1 for x in feats if x["geometry"]["type"] == "LineString")
    print(f"횡단보도 {len(feats)}건 (선형 {n_line} / 점형 {len(feats)-n_line}) -> {args.out}")


if __name__ == "__main__":
    main()
