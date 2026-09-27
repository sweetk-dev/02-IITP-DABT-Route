# -*- coding: utf-8 -*-
"""길안내 중 주변 랜드마크 (#88).

긴 직진 구간에서 안내가 끊기면 이용자는 제대로 가고 있는지 확인할 방법이 없다. 경로 옆에 있는
**이름으로 알아볼 수 있는 곳**을 골라 "오른쪽에 ○○ 정류장이 있습니다"처럼 알려 준다.

원천 (01 통합DB — 이미 보유, 재배포 제한 없는 공공데이터만)
  · 버스 정류장 ``tran_bus_station_info`` — 이름에 주변 시설명이 들어 있고 보도 바로 옆에 있다
  · 편의시설 실태조사 ``poi_facility_accessibility`` — 청사·학교·병원·우체국·도서관 등 이름 있는 건물
  · 공중·개방화장실 ``poi_public_toilet_info`` · 전동보장구 충전기·수리 ``poi_emergency_support``
  카카오 장소 정보는 저장·재배포가 약관상 금지라 쓰지 않는다(2026-09-04 검토).

규칙 (설계 2026-09-04 · 실측 2026-09-27)
  · 경로에서 MAX_OFFSET_M 안 · 출발 HEAD_M 이후 · 도착 TAIL_M 이전
  · 이름이 업종명뿐인 항목("일반음식점"·"소매점"), 주소형 이름("안양동 491-2 …"), 동 번호("10동"),
    다세대·연립주택·공장·숙박·주차장은 제외 — 길에서 알아볼 수 없다
  · 같은 이름은 경로에 가장 가까운 한 곳만(길 양쪽 같은 이름 정류장 등)
  · 간격 SPACING_M: 앞 랜드마크에서 SPACING_M 이 지난 뒤, WINDOW_M 안의 후보 중 등급·거리가 가장 좋은 것
  · 좌/우는 진행방향 기준. 경로 선에서 SIDE_MIN_M 미만이면(선 위) 방향을 말하지 않는다
"""
from __future__ import annotations

import logging
import math
import re

logger = logging.getLogger(__name__)

MAX_OFFSET_M = 35.0
HEAD_M = 25.0
TAIL_M = 30.0
SPACING_M = 150.0
WINDOW_M = 60.0
SIDE_MIN_M = 2.0
MAX_ITEMS = 40
BBOX_MARGIN_M = 60.0

# 편의시설 유형 등급 — 1: 누구나 아는 공공·대형 시설, 2: 이름이 보이는 건물. 목록 밖은 제외
TIER1_TYPES = (
    "국가 또는 지자체 청사", "지역자치센터", "보건소", "우체국", "파출소, 지구대", "도서관", "공공도서관",
    "초등학교", "중학교", "고등학교", "대학교", "전문대학", "특수학교", "종합병원",
    "병원·치과병원·한방병원·정신병원·요양병원", "전시장", "체육관", "이외 사회복지시설",
    "장애인복지시설", "생활권수련시설", "도매시정·소매시장", "국민연금공단 및 지사",
)
TIER2_TYPES = (
    "아파트", "금융업소 등 일반업무시설", "종교집회장", "경로당", "노인복지시설", "어린이집", "유치원",
    "관광숙박시설", "의원·치과의원·한의원·조산소·산후조리원", "교육원(연수원등)·직업훈련소·학원(자동차학원, 무도학원 제외) 등",
    "집회장", "아파트 부대복리시설",
)
GENERIC_NAMES = {"일반음식점", "음식점", "소매점", "공장", "주택", "연립주택", "다세대주택", "공동주택",
                 "공중화장실", "화장실", "빌딩", "상가", "근린생활시설"}
_ADDR_RE = re.compile(r"\d+-\d+")
_DONG_RE = re.compile(r"^[A-Za-z0-9가-힣]?\d*동$")

_KY = 110_540.0


def _kx(lat):
    return 111_320.0 * math.cos(math.radians(lat))


def clean_name(name) -> str:
    s = re.sub(r"\s+", " ", str(name or "")).strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
    return s


def usable_name(name: str, facl_type: str = "") -> bool:
    n = clean_name(name)
    if len(n) < 2 or n in GENERIC_NAMES or n == (facl_type or "").strip():
        return False
    if "·" in n or _ADDR_RE.search(n) or _DONG_RE.match(n) or n.endswith("주택"):
        return False
    return True


def _josa_i_ga(word: str) -> str:
    """받침이 있으면 '이', 없으면 '가'."""
    w = re.sub(r"[\s\)\]]+$", "", word or "")
    if not w:
        return "이"
    ch = w[-1]
    code = ord(ch)
    if 0xAC00 <= code <= 0xD7A3:
        return "이" if (code - 0xAC00) % 28 else "가"
    if ch.isdigit():
        return "이" if ch in "013678" else "가"
    if ch.isalpha():
        return "이" if ch.lower() in "lmn" else "가"
    return "이"


def speech(item: dict) -> str:
    """안내 문장. 방향이 없으면 '근처' 로 말한다."""
    kind, name, side = item["kind"], item["name"], item.get("side")
    where = {"left": "왼쪽에 ", "right": "오른쪽에 "}.get(side, "")
    spoken = name.replace("·", ", ")
    if kind == "bus_stop":
        return (where + spoken + " 정류장이 있습니다") if where else (spoken + " 정류장 근처입니다")
    if kind == "charger":
        subj = spoken + " 전동휠체어 충전기"
    elif kind == "repair":
        subj = spoken + " 보장구 수리점"
    elif kind == "toilet":
        subj = spoken if spoken.endswith("화장실") else spoken + " 화장실"
    else:
        subj = spoken
    if where:
        return where + subj + _josa_i_ga(subj) + " 있습니다"
    return subj + " 근처를 지납니다"


# ───────────── 후보 조회 ─────────────
def _bbox_of(geometries) -> dict:
    lats = [p[0] for g in geometries for p in g]
    lngs = [p[1] for g in geometries for p in g]
    if not lats:
        return {}
    dlat = BBOX_MARGIN_M / _KY
    dlng = BBOX_MARGIN_M / _kx(sum(lats) / len(lats))
    return {"min_lat": min(lats) - dlat, "max_lat": max(lats) + dlat,
            "min_lng": min(lngs) - dlng, "max_lng": max(lngs) + dlng}


def fetch_candidates(store, bbox: dict) -> list:
    """bbox 안 후보 [{kind, name, lat, lng, tier}]. db 백엔드만 — 그 밖은 빈 목록."""
    if not bbox or getattr(store, "backend", "none") != "db":
        return []
    W = ("COALESCE(del_yn, 'N') = 'N' AND latitude BETWEEN :min_lat AND :max_lat "
         "AND longitude BETWEEN :min_lng AND :max_lng")
    out = []

    def q(sql):
        try:
            return store._query(sql, bbox)
        except Exception as e:                       # 표 하나가 없어도 나머지는 쓴다
            logger.warning("랜드마크 후보 조회 실패(%s)", e)
            return []

    for r in q("SELECT station_name AS name, latitude, longitude FROM tran_bus_station_info WHERE " + W):
        n = clean_name(r.get("name")).replace(".", "·")
        if len(n) >= 2:
            out.append({"kind": "bus_stop", "name": n, "lat": r["latitude"], "lng": r["longitude"], "tier": 1})
    for r in q("SELECT facl_name AS name, facl_type, latitude, longitude FROM poi_facility_accessibility WHERE " + W):
        t = (r.get("facl_type") or "").strip()
        tier = 1 if t in TIER1_TYPES else 2 if t in TIER2_TYPES else 0
        if tier and usable_name(r.get("name"), t):
            out.append({"kind": "facility", "name": clean_name(r["name"]), "lat": r["latitude"],
                        "lng": r["longitude"], "tier": tier})
    for r in q("SELECT toilet_name AS name, latitude, longitude FROM poi_public_toilet_info WHERE " + W):
        n = clean_name(r.get("name"))
        if len(n) >= 2 and n not in ("공중화장실", "화장실"):
            out.append({"kind": "toilet", "name": n, "lat": r["latitude"], "lng": r["longitude"], "tier": 1})
    for r in q("SELECT name, support_type, latitude, longitude FROM poi_emergency_support "
               "WHERE support_type IN ('charge', 'repair') AND " + W):
        n = clean_name(r.get("name"))
        if len(n) >= 2:
            out.append({"kind": "charger" if r.get("support_type") == "charge" else "repair",
                        "name": n, "lat": r["latitude"], "lng": r["longitude"], "tier": 1})
    for c in out:
        c["lat"], c["lng"] = float(c["lat"]), float(c["lng"])
    return out


# ───────────── 경로 대조 ─────────────
def _project(geometry, lat, lng):
    """경로 위 최근접점 → (offset_m, along_m, side). side: 'left'|'right'|None"""
    if len(geometry) < 2:
        return None
    kx = _kx(geometry[0][0])
    pts = [(p[1] * kx, p[0] * _KY) for p in geometry]
    px, py = lng * kx, lat * _KY
    best, cum = None, 0.0
    for (ax, ay), (bx, by) in zip(pts[:-1], pts[1:]):
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        qx, qy = ax + t * dx, ay + t * dy
        d = math.hypot(px - qx, py - qy)
        if best is None or d < best[0]:
            cross = dx * (py - ay) - dy * (px - ax)       # 양수 = 진행방향 왼쪽
            best = (d, cum + t * math.sqrt(L2), cross)
        cum += math.sqrt(L2)
    d, along, cross = best
    side = None if d < SIDE_MIN_M else ("left" if cross > 0 else "right")
    return d, along, side, cum


def along_route(geometry, cands, walk_geometries=None, max_offset=MAX_OFFSET_M) -> list:
    """경로 옆 후보에 along_m·offset_m·side 를 붙인다. walk_geometries 가 있으면 그 구간 옆만."""
    out = []
    total = 0.0
    for c in cands:
        if walk_geometries:
            near = [_project(g, c["lat"], c["lng"]) for g in walk_geometries if len(g) >= 2]
            if not any(p and p[0] <= max_offset for p in near):
                continue
        p = _project(geometry, c["lat"], c["lng"])
        if not p:
            continue
        d, along, side, total = p
        if d > max_offset:
            continue
        it = dict(c)
        it.update({"offset_m": round(d, 1), "along_m": round(along, 1), "side": side})
        out.append(it)
    for it in out:
        it["_total"] = total
    return out


def select(items: list, total_m: float, spacing=SPACING_M, window=WINDOW_M,
           head=HEAD_M, tail=TAIL_M, limit=MAX_ITEMS) -> list:
    # 같은 이름은 경로에 가장 가까운 곳 하나
    best = {}
    for it in items:
        k = (it["kind"] if it["kind"] == "bus_stop" else "place", re.sub(r"\s+", "", it["name"]))
        if k not in best or it["offset_m"] < best[k]["offset_m"]:
            best[k] = it
    pool = sorted((it for it in best.values() if head <= it["along_m"] <= total_m - tail),
                  key=lambda x: x["along_m"])
    picked, cursor, i = [], -1e9, 0
    while i < len(pool) and len(picked) < limit:
        it = pool[i]
        if it["along_m"] < cursor + spacing:
            i += 1
            continue
        group = [x for x in pool[i:] if x["along_m"] <= it["along_m"] + window]
        choice = min(group, key=lambda x: (x["tier"], x["offset_m"]))
        picked.append(choice)
        cursor = choice["along_m"]
        i = pool.index(choice) + 1
    out = []
    for it in picked:
        out.append({"kind": it["kind"], "name": it["name"], "lat": round(it["lat"], 7),
                    "lng": round(it["lng"], 7), "along_m": round(it["along_m"]),
                    "offset_m": round(it["offset_m"]), "side": it["side"], "speech": speech(it)})
    return out


def for_route(store, geometry, walk_geometries=None) -> list:
    """경로 1건의 랜드마크 목록. 조회 실패는 빈 목록 — 경로 계획을 깨지 않는다."""
    try:
        geoms = walk_geometries or [geometry]
        cands = fetch_candidates(store, _bbox_of(geoms))
        if not cands:
            return []
        items = along_route(geometry, cands, walk_geometries)
        if not items:
            return []
        return select(items, items[0]["_total"])
    except Exception as e:
        logger.warning("랜드마크 계산 실패(%s) — 없이 응답", e)
        return []
