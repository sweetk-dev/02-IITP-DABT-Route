# -*- coding: utf-8 -*-
"""도엽(zip/ngi) -> 보행망 원천 피처 추출.

수치지도 Ver2.0 지형지물 코드 (2026-07-16 안양 211매 실측 기준):
  A0033320  인도(면형, SHP) / 인도 중심선(선형, NGI 2017) — 폭·재질 보유
  A0033330  자전거보행자겸용도로(선형) — 폭·재질·구분
  A0063321  육교 — 형태=장애인이용여부(가능/불가)
  A0020000  도로중심선 — 횡단보도 추정·하이브리드 연결의 기준선

통행 장애물 레이어 (v1.21.0 추가 — 안양 205매 실측 개수):
  C0390000  계단(면형) 1,317개 — 명칭·구조·폭 보유. 휠체어 통행 불가 판정의 원천
  F0040000  옹벽(선형) 7,225개 — 종류·상하구분·연장·높이·재질
  B0020000  담장·철책(선형) 20,476개
  C0530000  가드펜스(선형) 2,497개
  D0030000  수목(점형) 17,551개 — 유효 보도폭을 잠식

계단 면형은 인도 면형(A0033320)과 사실상 겹치지 않는다(실측 겹침 0~2.2%). 즉
표준대로 별개 지물로 그려져 있어 중심선화 산출물이 서로 중복되지 않는다.
"""
from __future__ import annotations

import os
import zipfile
import tempfile
from shapely.geometry import LineString

SIDEWALK_AREA = "A0033320"
SIDEWALK_LINE = "A0033330"
OVERPASS = "A0063321"
ROAD_CENTER = "A0020000"

STAIRS_AREA = "C0390000"
RETAINING_WALL = "F0040000"
FENCE = "B0020000"
GUARD_FENCE = "C0530000"
TREE_POINT = "D0030000"

WANTED = (SIDEWALK_AREA, SIDEWALK_LINE, OVERPASS, ROAD_CENTER)

# 장애물 레이어 -> 장애물 분류. 링크가 되지 않고 통행 판정·게이트 지오메트리로만 쓰인다.
OBSTACLE_CODES = {
    STAIRS_AREA: "stairs",
    RETAINING_WALL: "wall",
    FENCE: "fence",
    GUARD_FENCE: "fence",
    TREE_POINT: "street_furniture",
}

# C0390000 은 계단 전용 코드가 아니라 '계단·스탠드' 지물이다 (안양 실측: 계단 1,533 / 스탠드 182).
# 스탠드(경기장 관람석)를 걸러내지 않으면 안양종합운동장 스탠드 한 장이 88m x 76m 폴리곤으로
# 들어와, 그 위를 지나는 평촌대로 일대 보도 12개가 통째로 '계단 관통' 으로 잡힌다.
# 휠체어 이용자에게 멀쩡한 보도를 막는 반대 방향의 사고가 되므로 반드시 제외한다 (2026-09-04 실측).
STAIRS_EXCLUDED_STRUCTURES = ("스탠드",)


def _f(v, default=None):
    try:
        f = float(str(v).strip())
        return f if f > 0 else default
    except (TypeError, ValueError):
        return default


def _sheet_id(path: str) -> str:
    import re
    m = re.search(r"_(\d{9})_", os.path.basename(path))
    return m.group(1) if m else os.path.basename(path)[:20]


def _from_shp(zip_path: str) -> list[dict]:
    """SHP 도엽 zip -> 피처 목록.

    zip 내부 파일 명명은 두 규칙이 혼재한다 (2026-07-16 안양 186매 실측):
      N1형   : N1A_A0033320.shp / N1L_A0020000.shp  (N1A=면, N1L=선) — 138매
      평문형 : A0033320.shp / A0010000-L.shp          (-L=선, 무접미=shapeType 으로 판별) — 48매(전부 2022)
    평문형을 놓치면 평촌 등 동안구 중심부가 통째로 빠진다 (실제 사고: OSM 횡단보도
    623건 중 400건이 보도에서 200m 이탈 → 원인이 이 누락이었음).
    """
    import shapefile
    from .centerline import poly_to_centerlines

    out = []
    sid = _sheet_id(zip_path)
    with tempfile.TemporaryDirectory() as td:
        try:
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(td)
        except zipfile.BadZipFile:
            return out
        for code in WANTED:
            for path, want_geom in _layer_files(td, code):
                try:
                    r = shapefile.Reader(path, encoding="cp949", encodingErrors="replace")
                except Exception:
                    continue
                # Windows 에서 Reader 가 파일을 물고 있으면 TemporaryDirectory 정리가
                # PermissionError 로 죽는다 -> 메모리로 다 읽고 즉시 닫는다.
                is_area = (want_geom == "area") if want_geom else (r.shapeType == 5)
                fl = [x[0] for x in r.fields[1:]]
                idx = {n: fl.index(n) for n in fl}
                shapes_recs = list(zip(r.shapes(), r.records()))
                r.close()
                for sh, rec in shapes_recs:
                    a = {n: rec[i] for n, i in idx.items()}
                    if is_area and code == SIDEWALK_AREA:
                        out.append({"sheet": sid, "code": code, "kind": "sidewalk_poly",
                                    "points": sh.points, "parts": sh.parts, "attrs": a})
                    elif is_area and code == OVERPASS:
                        for l in poly_to_centerlines(sh.points, sh.parts):
                            out.append(_mk(sid, code, l, a, "overpass"))
                    elif not is_area:
                        if len(sh.points) < 2:
                            continue
                        kind = {SIDEWALK_LINE: "sidewalk", ROAD_CENTER: "road"}.get(code)
                        if not kind:
                            continue
                        out.append(_mk(sid, code, LineString(sh.points), a, kind))
    return out


def _layer_files(td: str, code: str):
    """압축 해제 폴더에서 code 에 해당하는 shp 파일들을 (경로, 지오메트리 힌트) 로 나열."""
    cands = [
        (os.path.join(td, f"N1A_{code}.shp"), "area"),
        (os.path.join(td, f"N1L_{code}.shp"), "line"),
        (os.path.join(td, f"{code}.shp"), None),          # 평문형 — shapeType 으로 판별
        (os.path.join(td, f"{code}-L.shp"), "line"),      # 평문형 선형
    ]
    return [(p, g) for p, g in cands if os.path.exists(p)]


def _from_ngi(ngi_path: str) -> list[dict]:
    from .ngi import read_ngi

    out = []
    sid = _sheet_id(ngi_path)
    try:
        layers = read_ngi(ngi_path)
    except Exception:
        return out
    for code in WANTED:
        L = layers.get(code)
        if not L:
            continue
        kind = {SIDEWALK_AREA: "sidewalk", SIDEWALK_LINE: "sidewalk",
                OVERPASS: "overpass", ROAD_CENTER: "road"}[code]
        for feat in L["features"]:
            g = feat["geom"]
            for pts in g["parts"]:
                if len(pts) < 2:
                    continue
                # NGI 2017 의 인도는 이미 중심선(LINESTRING) 이라 스켈레톤화 불필요
                out.append(_mk(sid, code, LineString(pts), feat["attrs"], kind))
    return out


def _mk(sid: str, code: str, line: LineString, attrs: dict, kind: str) -> dict:
    width = _f(attrs.get("폭") or attrs.get("도로폭"))
    return {
        "sheet": sid,
        "code": code,
        "kind": kind,
        "geom": line,
        "width": width,
        "surface": (attrs.get("재질") or "").strip() or None,
        "name": (attrs.get("명칭") or attrs.get("도로명") or "").strip() or None,
        # 육교 A0063321 의 '형태' = 장애인이용여부(가능/불가)
        "accessible": _access(attrs.get("형태")),
        "ufid": (attrs.get("UFID") or "").strip() or None,
    }


def _access(v):
    if not v:
        return None
    s = str(v)
    if "가능" in s:
        return True
    if "불가" in s:
        return False
    return None


def extract_obstacles(zip_path: str) -> list[dict]:
    """도엽 zip -> 통행 장애물 피처 (계단 면형 · 옹벽 · 담장 · 가드펜스 · 수목).

    보행망 링크를 만들지 않는다. 반환 지오메트리는
      · 기존 링크가 계단을 관통하는지 검사 (scripts/mark_stairs_from_topomap.py)
      · 우회 삼각형 직결 링크 신설의 하드 게이트 (topomap.refine)
    두 곳에서 쓰인다. 좌표계는 원본 그대로(EPSG:5186) — 변환은 호출측에서 한다.
    """
    import shapefile
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.geometry import shape as shp_shape

    out = []
    sid = _sheet_id(zip_path)
    with tempfile.TemporaryDirectory() as td:
        try:
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(td)
        except zipfile.BadZipFile:
            return out
        for code, klass in OBSTACLE_CODES.items():
            for pref in ("N1A_", "N1L_", "N1P_", "N3A_", "N3L_", "N3P_", ""):
                p = os.path.join(td, f"{pref}{code}.shp")
                if not os.path.exists(p):
                    continue
                try:
                    r = shapefile.Reader(p, encoding="cp949", encodingErrors="replace")
                except Exception:
                    continue
                fl = [x[0] for x in r.fields[1:]]
                idx = {n: fl.index(n) for n in fl}
                pairs = list(zip(r.shapes(), r.records()))
                r.close()
                for sh, rec in pairs:
                    a = {n: rec[i] for n, i in idx.items()}
                    struct = str(a.get("구조") or "").strip()
                    if code == STAIRS_AREA and struct in STAIRS_EXCLUDED_STRUCTURES:
                        continue
                    try:
                        g = shp_shape(sh.__geo_interface__)
                    except Exception:
                        continue
                    if isinstance(g, (Polygon, MultiPolygon)) and not g.is_valid:
                        g = g.buffer(0)
                    if g.is_empty:
                        continue
                    out.append({
                        "sheet": sid, "code": code, "kind": "obstacle", "obstacle": klass,
                        "geom": g,
                        "name": (a.get("명칭") or a.get("종류") or "").strip() or None,
                        "structure": struct or None,
                        "height_m": _f(a.get("높이")),
                        "width": _f(a.get("폭")),
                        "ufid": (a.get("UFID") or "").strip() or None,
                    })
                break   # 같은 코드는 한 접두만 채택 (중복 방지)
    return out


def extract_sheet(path: str) -> list[dict]:
    """도엽 파일(.zip=SHP / .ngi) -> 피처 목록."""
    if path.lower().endswith(".zip"):
        return _from_shp(path)
    if path.lower().endswith(".ngi"):
        return _from_ngi(path)
    return []
