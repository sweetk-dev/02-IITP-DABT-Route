# -*- coding: utf-8 -*-
"""수치지형도 등고선 -> DEM(GeoTIFF) 생성.

배경: 국토정보플랫폼에서 즉시 받을 수 있는 공개DEM 은 90m 격자다. 90m 는 골목 단위
급경사를 평활화해 버려서, 휠체어가 실제로 못 오르는 언덕이 평지로 보인다.
5m DEM 은 오프라인 신청 대상이라 즉시 확보가 어렵다.

해법: 5m DEM 의 원자료인 **1:5,000 수치지형도 등고선**(주곡선 5m)을 직접 보간해
동급 해상도의 DEM 을 만든다.

입력: 수치지도 zip 묶음 (레이어 N3L_F0010000=등고선, N3P_F0020000=표고점, EPSG:5186)
출력: GeoTIFF (기본 5m 격자, 입력과 동일 좌표계)

메모리: 등고선 정점이 수백만 개가 되므로 격자를 블록으로 나눠 블록별로 지역 삼각망을
구성한다(전역 Delaunay 를 한 번에 만들지 않는다).

사용 예)
  python scripts/dem_from_contours.py \
      --src "../91-조사설계_이동편의/경기도 안양시 지도/안양시 수치지형도" \
      --out data/dem/anyang_5m.tif --res 5
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # pragma: no cover
    pass

CONTOUR_LAYER = "N3L_F0010000"   # 등고선
SPOT_LAYER = "N3P_F0020000"      # 표고점
NODATA = -9999.0


def _elev_column(gdf):
    """표고 컬럼 자동 탐지 (등고수치 / 수치 / 수치값 …). UFID·구분 등은 제외."""
    import pandas as pd

    for c in gdf.columns:
        if c == "geometry" or "UFID" in str(c).upper():
            continue
        s = pd.to_numeric(gdf[c], errors="coerce")
        if s.notna().mean() > 0.9 and s.max() > 0 and s.max() < 3000:
            return c
    return None


def _densify(line, spacing: float):
    """선을 spacing(m) 간격 점열로. 등고선 정점만 쓰면 긴 직선 구간이 비어 보간이 튄다."""
    length = line.length
    if length <= 0:
        return []
    n = max(int(length // spacing), 1)
    return [line.interpolate(i / n, normalized=True) for i in range(n + 1)]


def collect_points(src_dir: str, spacing: float):
    """zip 들에서 등고선·표고점을 읽어 (x, y, z) 배열로."""
    import geopandas as gpd
    import numpy as np

    zips = sorted(glob.glob(os.path.join(src_dir, "**", "*.zip"), recursive=True))
    if not zips:
        raise SystemExit("수치지도 zip 을 찾지 못했습니다: %s" % src_dir)
    print("[dem] 도엽 %d개" % len(zips))

    xs, ys, zs = [], [], []
    crs = None
    tmp = tempfile.mkdtemp(prefix="ct_")
    try:
        for i, zp in enumerate(zips, 1):
            sheet_dir = os.path.join(tmp, "s")
            shutil.rmtree(sheet_dir, ignore_errors=True)
            os.makedirs(sheet_dir, exist_ok=True)
            try:
                with zipfile.ZipFile(zp) as z:
                    for n in z.namelist():
                        if n.startswith((CONTOUR_LAYER, SPOT_LAYER)):
                            z.extract(n, sheet_dir)
            except zipfile.BadZipFile:
                print("  ! 손상된 zip 건너뜀: %s" % os.path.basename(zp))
                continue

            cp = os.path.join(sheet_dir, CONTOUR_LAYER + ".shp")
            if os.path.exists(cp):
                g = gpd.read_file(cp, encoding="cp949")
                crs = crs or g.crs
                col = _elev_column(g)
                if col is not None:
                    for geom, z_ in zip(g.geometry, g[col]):
                        if geom is None or z_ is None:
                            continue
                        parts = geom.geoms if geom.geom_type.startswith("Multi") else [geom]
                        for part in parts:
                            for pt in _densify(part, spacing):
                                xs.append(pt.x); ys.append(pt.y); zs.append(float(z_))

            sp = os.path.join(sheet_dir, SPOT_LAYER + ".shp")
            if os.path.exists(sp):
                g = gpd.read_file(sp, encoding="cp949")
                crs = crs or g.crs
                col = _elev_column(g)
                if col is not None:
                    for geom, z_ in zip(g.geometry, g[col]):
                        if geom is None or z_ is None:
                            continue
                        xs.append(geom.x); ys.append(geom.y); zs.append(float(z_))

            if i % 25 == 0:
                print("  ... %d/%d 도엽, 표고점 %d개" % (i, len(zips), len(zs)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    pts = np.column_stack([np.asarray(xs), np.asarray(ys)])
    vals = np.asarray(zs, dtype="float64")
    print("[dem] 표고 표본 %d개 / 표고 범위 %.1f~%.1fm" % (len(vals), vals.min(), vals.max()))
    return pts, vals, crs


def interpolate(pts, vals, res: float, block: int, buffer_m: float):
    """블록 단위 선형 보간(지역 삼각망). 전역 Delaunay 는 메모리를 감당하지 못한다."""
    import numpy as np
    from scipy.interpolate import LinearNDInterpolator

    x_min, y_min = pts[:, 0].min(), pts[:, 1].min()
    x_max, y_max = pts[:, 0].max(), pts[:, 1].max()
    w = int((x_max - x_min) / res) + 1
    h = int((y_max - y_min) / res) + 1
    print("[dem] 격자 %d x %d (%.0fm)" % (w, h, res))

    grid = np.full((h, w), NODATA, dtype="float32")
    for r0 in range(0, h, block):
        for c0 in range(0, w, block):
            r1, c1 = min(r0 + block, h), min(c0 + block, w)
            bx0 = x_min + c0 * res
            bx1 = x_min + c1 * res
            by1 = y_max - r0 * res          # 위쪽
            by0 = y_max - r1 * res          # 아래쪽

            m = (
                (pts[:, 0] >= bx0 - buffer_m) & (pts[:, 0] <= bx1 + buffer_m)
                & (pts[:, 1] >= by0 - buffer_m) & (pts[:, 1] <= by1 + buffer_m)
            )
            if m.sum() < 3:
                continue
            interp = LinearNDInterpolator(pts[m], vals[m])

            gx, gy = np.meshgrid(
                x_min + (np.arange(c0, c1) + 0.5) * res,
                y_max - (np.arange(r0, r1) + 0.5) * res,
            )
            out = interp(gx, gy)
            out = np.where(np.isnan(out), NODATA, out)
            grid[r0:r1, c0:c1] = out.astype("float32")
        print("  ... 행 %d/%d" % (min(r0 + block, h), h))

    filled = float((grid != NODATA).mean())
    print("[dem] 보간 완료 — 유효 셀 %.1f%%" % (filled * 100))
    return grid, (x_min, y_max)


def main():
    ap = argparse.ArgumentParser(description="수치지형도 등고선 -> DEM(GeoTIFF)")
    ap.add_argument("--src", required=True, help="수치지도 zip 이 있는 폴더(하위 폴더 포함)")
    ap.add_argument("--out", default="data/dem/anyang_5m.tif")
    ap.add_argument("--res", type=float, default=5.0, help="격자 간격(m)")
    ap.add_argument("--spacing", type=float, default=5.0, help="등고선 점열화 간격(m)")
    ap.add_argument("--block", type=int, default=1000, help="보간 블록 크기(셀)")
    ap.add_argument("--buffer", type=float, default=300.0, help="블록 경계 버퍼(m)")
    args = ap.parse_args()

    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    pts, vals, crs = collect_points(args.src, args.spacing)
    grid, (x_min, y_max) = interpolate(pts, vals, args.res, args.block, args.buffer)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    transform = from_origin(x_min, y_max, args.res, args.res)
    with rasterio.open(
        args.out, "w", driver="GTiff",
        height=grid.shape[0], width=grid.shape[1], count=1,
        dtype="float32", crs=crs, transform=transform,
        nodata=NODATA, compress="deflate",
    ) as dst:
        dst.write(grid, 1)
    print("[dem] 저장 완료: %s (%s, %.0fm)" % (args.out, crs, args.res))


if __name__ == "__main__":
    main()
