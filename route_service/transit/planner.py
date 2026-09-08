# -*- coding: utf-8 -*-
"""제약형 멀티모달 후보 탐색 (#36).

시각표 없는 정적 데이터(정류장·노선-정류장 순번·안양 관내 역)만으로
"도보+버스", "도보+버스+지하철" 조합의 뼈대를 찾는다.

제약 (3차년도 실증 범위):
  · 버스는 **직결 1회 승차만** — 버스↔버스 환승 없음
  · 지하철은 안양 관내 노선 내 이동만 — 노선 간 환승 없음
  · 소요시간은 정거장 수 기반 추정 — 대기 시간 미포함

여기서는 교통 수단 조합(어느 정류장에서 어떤 노선을 타고 어디서 내리는지)만
정하고, 도보 leg 의 실제 경로 계산은 호출자(api.main)가 보행 그래프로 수행한다.
순수 함수로만 구성해 파일 백엔드 픽스처로 단독 테스트한다.
"""
from __future__ import annotations

from ..engine.geo import haversine_m

# 추정 파라미터 — 실증 실측으로 보정한다
BUS_SEC_PER_STOP = 120          # 버스 한 정거장 평균
SUBWAY_SEC_PER_STATION = 150    # 지하철 한 역 평균
SUBWAY_ACCESS_SEC = 240         # 역 진입·승강 설비 이동 버퍼(승차·하차 합)
WALK_DETOUR = 1.35              # 도보 직선→실경로 근사 배율

STOP_RADIUS_M = 450             # 출발/도착 인근 정류장 탐색 반경
STOP_NEAR_STATION_M = 350       # 역 환승용 정류장 탐색 반경
STATION_RADIUS_M = 700          # 인근 역 탐색 반경
MAX_CANDIDATE_STOPS = 6

# 스코어 환산(도보 m 단위) — 낮을수록 좋다
STOP_PENALTY_M = 150            # 버스 한 정거장
STATION_PENALTY_M = 180         # 지하철 한 역
TRANSIT_LEG_PENALTY_M = 250     # 탑승 1회(대기·승하차 부담)

# 안양 관내 노선 위상(정적) — 역 이름은 poi_station_access_status.stn_name 기준
LINES = {
    "1호선": ["석수", "관악", "안양", "명학"],
    "4호선": ["인덕원", "평촌", "범계"],
}


def nearest(items, lat, lng, radius_m, k):
    """좌표 보유 항목을 실거리로 걸러 가까운 순 (dist, item) 목록."""
    out = []
    for it in items:
        if it.get("lat") is None:
            continue
        d = haversine_m(lat, lng, it["lat"], it["lng"])
        if d <= radius_m:
            out.append((d, it))
    out.sort(key=lambda x: x[0])
    return out[:k]


LOW_BUS_STALE_DAYS = 7          # 전일 기준 저상 노선 표가 이보다 오래되면 'N' 을 믿지 않는다


def low_bus_route_ok(route: dict, today=None) -> bool:
    """저상버스 우선 모드의 1차 필터 — 전일 기준 저상 운행 노선 표(01 low_bus_yn)로
    저상이 아예 없는 노선을 후보에서 뺀다. 값이 없거나(NULL) 표가 오래되면 통과시킨다
    (실시간 판정이 뒤에 있으므로 여기서 과하게 거르지 않는다)."""
    yn = (route or {}).get("low_bus_yn")
    if yn != "N":
        return True
    base = (route or {}).get("low_bus_base_dt")
    if base:
        try:
            import datetime as _dt
            d = _dt.date.fromisoformat(str(base)[:10])
            t = today or _dt.date.today()
            if (t - d).days > LOW_BUS_STALE_DAYS:
                return True
        except ValueError:
            pass
    return False


def _direct_bus_pairs(stops_a, stops_b, origin=None, target=None, route_ok=None):
    """직결 버스 조합 — 같은 노선이 올바른 순번 방향으로 두 정류장을 지나는 경우.

    회차 노선은 한 정류장에 순번이 여러 개다(예: [1, 42]) — 모든 (승차, 하차)
    순번 조합 중 승차 < 하차 이면서 정거장 수가 최소인 것을 쓴다.

    origin/target 이 주어지면 **진행성 가드**를 적용한다: 하차 정류장이 승차
    정류장보다 목적지에 가까워야 한다. 정거장 수 페널티만으로는 "목적지에서
    멀어지는 짧은 역주행 탑승"이 스코어에서 이길 수 있기 때문이다.
    """
    pairs = []
    for da, a in stops_a:
        routes_a = {r["route_id"]: r for r in (a.get("routes") or []) if r.get("route_id")}
        for db, b in stops_b:
            if a["poi_id"] == b["poi_id"]:
                continue
            if target is not None:
                to_t_board = haversine_m(a["lat"], a["lng"], target[0], target[1])
                to_t_alight = haversine_m(b["lat"], b["lng"], target[0], target[1])
                if to_t_alight >= to_t_board:
                    continue          # 타서 목적지에 가까워지지 않는 조합
            for r in (b.get("routes") or []):
                ra = routes_a.get(r.get("route_id"))
                if not ra:
                    continue
                if route_ok is not None and not route_ok(ra):
                    continue          # 저상 우선 모드 1차 필터 등
                best = None
                for sa in (ra.get("station_seq") or []):
                    for sb in (r.get("station_seq") or []):
                        if sa < sb and (best is None or sb - sa < best[2]):
                            best = (sa, sb, sb - sa)
                if best:
                    pairs.append({
                        "walk_a_m": da, "walk_b_m": db,
                        "board": a, "alight": b, "route": ra,
                        "seq_from": best[0], "seq_to": best[1],
                        "stop_cnt": best[2],
                    })
    return pairs


def _line_of(station_name: str):
    for line, seq in LINES.items():
        for i, nm in enumerate(seq):
            if station_name and station_name.startswith(nm):
                return line, i
    return None, None


def _subway_hop(s1, s2):
    """같은 노선 내 두 역 사이 이동. 성립하면 (line, station_cnt)."""
    l1, i1 = _line_of(s1.get("name") or "")
    l2, i2 = _line_of(s2.get("name") or "")
    if l1 is None or l1 != l2 or i1 == i2:
        return None
    return l1, abs(i1 - i2)


def _walk_est(*pts):
    """(lat,lng) 점들을 잇는 도보 근사(직선×배율)."""
    total = 0.0
    for (a, b) in zip(pts[:-1], pts[1:]):
        total += haversine_m(a[0], a[1], b[0], b[1])
    return total * WALK_DETOUR


def search(origin, target, mode, stops_near, stations, stop_radius_m=None,
           max_stops=None, route_ok=None):
    """조합 후보를 스코어 오름차순으로 반환.

    origin/target: (lat, lng)
    mode: walk_bus | walk_bus_subway
    stops_near(lat, lng, radius_m) -> 정류장 목록(routes 포함)
    stations: 안양 관내 역 목록(list_transit 의 정규화 형식 + line 판정은 이름 기반)
    stop_radius_m / max_stops: 출발·도착 인근 정류장 탐색 반경·개수(기본 450m·6개).
        저상버스 우선 모드는 450m 에 저상 후보가 없을 때만 800m 로 넓힌다(#64).
    route_ok(route) -> bool: 버스 노선 사전 필터(None 이면 전부 허용).
    """
    o, t = origin, target
    cands = []
    radius = float(stop_radius_m or STOP_RADIUS_M)
    k = int(max_stops or MAX_CANDIDATE_STOPS)

    stops_o = nearest(stops_near(o[0], o[1], radius), o[0], o[1], radius, k)
    stops_t = nearest(stops_near(t[0], t[1], radius), t[0], t[1], radius, k)

    # ── 직결 버스 ──
    for p in _direct_bus_pairs(stops_o, stops_t, origin=o, target=t, route_ok=route_ok):
        walk = (_walk_est(o, (p["board"]["lat"], p["board"]["lng"]))
                + _walk_est((p["alight"]["lat"], p["alight"]["lng"]), t))
        score = walk + p["stop_cnt"] * STOP_PENALTY_M + TRANSIT_LEG_PENALTY_M
        cands.append({
            "score": score,
            "parts": [
                {"kind": "walk", "frm": ("현재 위치", o), "to": ("%s 정류장" % p["board"]["name"], (p["board"]["lat"], p["board"]["lng"]))},
                {"kind": "bus", **{k: p[k] for k in ("board", "alight", "route", "seq_from", "seq_to", "stop_cnt")}},
                {"kind": "walk", "frm": ("%s 정류장" % p["alight"]["name"], (p["alight"]["lat"], p["alight"]["lng"])), "to": ("목적지", t)},
            ],
        })

    if mode == "walk_bus_subway":
        st_o = nearest(stations, o[0], o[1], STATION_RADIUS_M, 3)
        st_t = nearest(stations, t[0], t[1], STATION_RADIUS_M, 3)

        # ── 지하철만 (도보+지하철) ──
        for d1, s1 in st_o:
            for d2, s2 in st_t:
                hop = _subway_hop(s1, s2)
                if not hop:
                    continue
                line, n = hop
                walk = _walk_est(o, (s1["lat"], s1["lng"])) + _walk_est((s2["lat"], s2["lng"]), t)
                score = walk + n * STATION_PENALTY_M + TRANSIT_LEG_PENALTY_M
                cands.append({
                    "score": score,
                    "parts": [
                        {"kind": "walk", "frm": ("현재 위치", o), "to": ("%s역" % s1["name"], (s1["lat"], s1["lng"]))},
                        {"kind": "subway", "line": line, "board": s1, "alight": s2, "station_cnt": n},
                        {"kind": "walk", "frm": ("%s역" % s2["name"], (s2["lat"], s2["lng"])), "to": ("목적지", t)},
                    ],
                })

        # ── 버스 → 지하철 (직결 버스로 노선 내 어느 역까지 이동 후 승차) ──
        for _, s2 in st_t:
            line2, i2 = _line_of(s2.get("name") or "")
            if line2 is None:
                continue
            for s1 in stations:
                hop = _subway_hop(s1, s2)
                if not hop:
                    continue
                stops_s1 = nearest(stops_near(s1["lat"], s1["lng"], STOP_NEAR_STATION_M),
                                   s1["lat"], s1["lng"], STOP_NEAR_STATION_M, 4)
                for p in _direct_bus_pairs(stops_o, stops_s1, origin=o,
                                            target=(s1["lat"], s1["lng"]), route_ok=route_ok):
                    line, n = hop
                    walk = (_walk_est(o, (p["board"]["lat"], p["board"]["lng"]))
                            + _walk_est((p["alight"]["lat"], p["alight"]["lng"]), (s1["lat"], s1["lng"]))
                            + _walk_est((s2["lat"], s2["lng"]), t))
                    score = (walk + p["stop_cnt"] * STOP_PENALTY_M + n * STATION_PENALTY_M
                             + 2 * TRANSIT_LEG_PENALTY_M)
                    cands.append({
                        "score": score,
                        "parts": [
                            {"kind": "walk", "frm": ("현재 위치", o), "to": ("%s 정류장" % p["board"]["name"], (p["board"]["lat"], p["board"]["lng"]))},
                            {"kind": "bus", **{k: p[k] for k in ("board", "alight", "route", "seq_from", "seq_to", "stop_cnt")}},
                            {"kind": "walk", "frm": ("%s 정류장" % p["alight"]["name"], (p["alight"]["lat"], p["alight"]["lng"])), "to": ("%s역" % s1["name"], (s1["lat"], s1["lng"]))},
                            {"kind": "subway", "line": line, "board": s1, "alight": s2, "station_cnt": n},
                            {"kind": "walk", "frm": ("%s역" % s2["name"], (s2["lat"], s2["lng"])), "to": ("목적지", t)},
                        ],
                    })

    # 중복 제거(같은 노선·승하차 조합) 후 스코어 순
    seen, out = set(), []
    for c in sorted(cands, key=lambda x: x["score"]):
        key = tuple((p["kind"],
                     p.get("route", {}).get("route_id") if p["kind"] == "bus" else None,
                     p.get("board", {}).get("poi_id") if p["kind"] != "walk" else None,
                     p.get("alight", {}).get("poi_id") if p["kind"] != "walk" else None)
                    for p in c["parts"])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out
