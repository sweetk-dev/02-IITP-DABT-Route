# -*- coding: utf-8 -*-
"""DEM 기반 링크 종단경사 산출.

국토지리정보원 공개DEM(.img, EPSG:5179) 을 그대로 읽는다.

설계 결정 — '경사도 래스터'가 아니라 '링크 종단경사(grade)'를 쓴다:
  기존 build_network.py 는 DEM 에서 지형 경사도(terrain slope)를 뽑아 링크에 붙였다.
  그러나 휠체어 통행 가부를 좌우하는 것은 주변 지형이 얼마나 가파른가가 아니라
  **그 길을 따라 실제로 얼마나 오르내리는가**이다. 경사도 래스터를 쓰면 옆이 절벽인
  평지 보도가 급경사로 오판된다. 그래서 링크 양 끝점의 표고차 / 링크 연장으로 계산한다.

격자가 성길수록(90m) 짧은 링크의 표고차가 0 으로 뭉개지므로 **이중선형 보간**으로 표고를 읽는다.
"""
from __future__ import annotations

import math


class DemSampler:
    """DEM 래스터에서 위경도로 표고(m)를 조회한다(이중선형 보간)."""

    def __init__(self, dem_path: str):
        import rasterio
        from pyproj import Transformer

        self.src = rasterio.open(dem_path)
        self.nodata = self.src.nodata
        self.to_dem = Transformer.from_crs("EPSG:4326", self.src.crs, always_xy=True)
        self.band = self.src.read(1)
        self.h, self.w = self.band.shape

    @property
    def meta(self) -> dict:
        b = self.src.bounds
        return {
            "crs": str(self.src.crs),
            "res_m": float(self.src.res[0]),
            "size": [self.src.width, self.src.height],
            "bounds": [b.left, b.bottom, b.right, b.top],
        }

    def _valid(self, v) -> bool:
        if v is None:
            return False
        if self.nodata is not None and abs(float(v) - float(self.nodata)) < 1e-6:
            return False
        return not math.isnan(float(v))

    def elevation(self, lat: float, lon: float):
        x, y = self.to_dem.transform(lon, lat)
        col, row = ~self.src.transform * (x, y)
        c0, r0 = int(math.floor(col - 0.5)), int(math.floor(row - 0.5))
        fx, fy = (col - 0.5) - c0, (row - 0.5) - r0

        acc, wsum = 0.0, 0.0
        for dr in (0, 1):
            for dc in (0, 1):
                rr, cc = r0 + dr, c0 + dc
                if not (0 <= rr < self.h and 0 <= cc < self.w):
                    continue
                v = self.band[rr, cc]
                if not self._valid(v):
                    continue
                wgt = (fx if dc else 1 - fx) * (fy if dr else 1 - fy)
                if wgt <= 0:
                    continue
                acc += float(v) * wgt
                wsum += wgt
        if wsum == 0:
            return None
        return acc / wsum

    def close(self):
        try:
            self.src.close()
        except Exception:
            pass


def apply_slope_from_dem(G, dem_path: str, **_ignored) -> dict:
    """그래프의 각 링크에 종단경사(도)를 채운다. 반환: 통계."""
    from .geo import haversine_m

    sampler = DemSampler(dem_path)
    cache = {}

    def elev_of(node):
        if node not in cache:
            d = G.nodes[node]
            cache[node] = sampler.elevation(d["lat"], d["lon"])
        return cache[node]

    filled = 0
    missing = 0
    steepest = 0.0
    hist = {"0-2": 0, "2-4": 0, "4-6": 0, "6-8": 0, "8+": 0}

    for u, v, data in G.edges(data=True):
        e1, e2 = elev_of(u), elev_of(v)
        if e1 is None or e2 is None:
            missing += 1
            continue
        length = float(data.get("length") or 0.0) or haversine_m(
            G.nodes[u]["lat"], G.nodes[u]["lon"], G.nodes[v]["lat"], G.nodes[v]["lon"]
        )
        if length < 1.0:
            missing += 1
            continue

        deg = math.degrees(math.atan(abs(e2 - e1) / length))
        data["slope"] = round(deg, 3)
        data["elev_start"] = round(e1, 1)
        data["elev_end"] = round(e2, 1)
        filled += 1
        steepest = max(steepest, deg)
        if deg < 2:
            hist["0-2"] += 1
        elif deg < 4:
            hist["2-4"] += 1
        elif deg < 6:
            hist["4-6"] += 1
        elif deg < 8:
            hist["6-8"] += 1
        else:
            hist["8+"] += 1

    sampler.close()
    total = G.number_of_edges()
    return {
        "edges": total,
        "slope_filled": filled,
        "missing": missing,
        "coverage": round(filled / total, 4) if total else 0.0,
        "max_slope_deg": round(steepest, 2),
        "slope_hist_deg": hist,
        "dem": sampler.meta,
    }
