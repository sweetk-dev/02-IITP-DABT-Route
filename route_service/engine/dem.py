# -*- coding: utf-8 -*-
"""DEM(.img) 기반 링크 경사 부여.

기존 build_network.py 의 경사 산출 로직을 재사용 가능한 함수로 분리했다.
xdem/pyproj 는 선택 의존성이며, DEM 이 없으면 경사는 0 으로 두고
meta.slope_coverage 로 품질을 고지한다(경로는 여전히 생성된다).
"""
from __future__ import annotations

import math


def apply_slope_from_dem(G, dem_path: str, sample_step_m: float = 5.0, bbox=None) -> dict:
    """그래프의 각 링크에 평균 경사(도)를 채운다. 반환: 통계."""
    import numpy as np
    import xdem
    from pyproj import Transformer

    dem = xdem.DEM(dem_path)
    to_dem = Transformer.from_crs("EPSG:4326", dem.crs, always_xy=True)

    if bbox:
        min_lat, min_lng, max_lat, max_lng = bbox
        x_min, y_min = to_dem.transform(min_lng, min_lat)
        x_max, y_max = to_dem.transform(max_lng, max_lat)
        dem = dem.crop([x_min, y_min, x_max, y_max])

    slope_raster = xdem.terrain.slope(dem)
    arr = slope_raster.data.filled(np.nan)
    transform = dem.transform

    def slope_at(lat, lon):
        x, y = to_dem.transform(lon, lat)
        col, row = ~transform * (x, y)
        try:
            v = arr[int(row), int(col)]
        except IndexError:
            return float("nan")
        return float(v)

    from .graph import edge_coords
    from .geo import haversine_m

    filled = 0
    for u, v, data in G.edges(data=True):
        coords = edge_coords(G, u, v)
        seg_len = float(data.get("length") or 0.0) or haversine_m(
            coords[0][0], coords[0][1], coords[-1][0], coords[-1][1]
        )
        n = max(int(seg_len // sample_step_m), 1)
        samples = []
        for i in range(n + 1):
            t = i / n
            lat = coords[0][0] + (coords[-1][0] - coords[0][0]) * t
            lon = coords[0][1] + (coords[-1][1] - coords[0][1]) * t
            s = slope_at(lat, lon)
            if not math.isnan(s):
                samples.append(s)
        if samples:
            data["slope"] = float(sum(samples) / len(samples))
            filled += 1
    total = G.number_of_edges()
    return {"edges": total, "slope_filled": filled,
            "coverage": round(filled / total, 4) if total else 0.0}
