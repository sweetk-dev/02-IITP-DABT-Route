# -*- coding: utf-8 -*-
"""저상버스 우선 모드 — 조회 시점 실시간 저상 판정 (#64).

휠체어 이용자에게 일반버스는 탑승이 불가능하다. 그래서 저상 여부는 스코어의 가중치가
아니라 **탑승 가능 여부**로 다루고, 후보를 계층(tier)으로 나눈 뒤 같은 계층 안에서만
시간으로 정렬한다.

  tier 1  승차 정류장 도착정보(GBIS)에서 저상 차량의 도착 예정이 확인됨
  tier 2  도착정보의 2대는 일반 차량이지만, 노선 위치정보에서 상류에 운행 중인 저상 차량이 있음
  tier 0  실시간 정보를 받지 못함(장애·미설정) — 판정 불가, 정적 순서 유지
  tier 3  저상 차량이 없음(도착·위치정보 모두 확인)

정렬 우선순위는 1 → 2 → 0 → 3 이다. 판정 불가(0)는 저상이 있을 수도 있으므로 '없음'(3)보다 앞선다.

놓치는 차량: 승차 정류장 도착 전에 지나가는 차량은 뺀다 — 도착 예정 ≥ 도보 소요 + MISS_BUFFER_SEC.
도착정보(getBusArrivalListv2)는 노선당 2대만 주므로 "2대 모두 일반" ≠ "저상 없음" 이다.
그 경우에만 노선 위치정보(getBusLocationListv2)로 승차 순번 상류의 저상 차량을 찾아 정거장 수로
대기를 추정한다(순환 노선은 (승차순번 - 차량순번 + N) mod N).

도착정보 flag 는 운행 중 'PASS' 가 실측값이다(2026-09-07). 운행 종료·회차 대기 값은 제외한다.
"""
from __future__ import annotations

import time

from . import planner as transit

MISS_BUFFER_SEC = 180           # 승차 정류장 도착 여유(횡단보도 1회·승강기 대기 흡수)
BOARDING_OVERHEAD_SEC = 90      # 경사판 전개·고정 등 승하차 오버헤드
UNKNOWN_WAIT_SEC = 600          # 판정 불가 후보의 대기 추정(정렬용, 안내에는 쓰지 않는다)
STOP_RADIUS_EXPANDED_M = 800    # 450m 에 저상 후보가 없을 때만 넓히는 반경
EXPANDED_MAX_STOPS = 10
VALID_FOR_SEC = 120             # 실시간 기반 결과의 유효 시간 — 앱은 이후 재탐색을 권한다

TIER_RANK = {1: 0, 2: 1, 0: 2, 3: 3}
EXCLUDED_FLAGS = ("STOP", "WAIT", "END")


def is_wheelchair(profile_id: str) -> bool:
    return str(profile_id or "").startswith("wheelchair")


def resolve_mode(requested, profile_id: str) -> bool:
    """요청값이 없으면 휠체어 프로필에서 기본 on (#64 — 결정)."""
    if requested is None:
        return is_wheelchair(profile_id)
    return bool(requested)


def _predict_sec(v: dict):
    ps = v.get("predict_sec")
    if ps is not None:
        return int(ps)
    pm = v.get("predict_min")
    return None if pm is None else int(pm) * 60


def _upstream_stops(vehicle_seq, board_seq, n_total):
    """차량 현재 순번 → 승차 순번까지 남은 정거장 수(양수). 상류가 아니면 None."""
    if vehicle_seq is None or board_seq is None:
        return None
    d = int(board_seq) - int(vehicle_seq)
    if d > 0:
        return d
    if n_total and n_total > 0 and d < 0:
        return d + int(n_total)      # 순환 노선 — 종점 직전 차량이 기점 직후 정류장으로 온다
    return None


class LowFloorJudge:
    """후보의 버스 part 에 대해 저상 판정을 내린다. 도착정보는 정류장 단위로 캐시된다(gbis_live)."""

    def __init__(self, live, store, now=None):
        self.live = live
        self.store = store
        self.now = now or time.time()
        self._arr = {}
        self._loc = {}
        self._route_len = {}

    # ---------- 실시간 조회(정류장·노선 단위 1회) ----------
    def arrivals(self, station_id):
        key = str(station_id)
        if key not in self._arr:
            if not getattr(self.live, "enabled", False):
                self._arr[key] = {"status": "unavailable", "reason": "realtime disabled", "items": []}
            else:
                try:
                    meta = self.store.stop_route_meta(key)
                except Exception:
                    meta = {}
                self._arr[key] = self.live.arrivals(key, route_meta=meta)
        return self._arr[key]

    def locations(self, route_id):
        key = str(route_id)
        if key not in self._loc:
            if not getattr(self.live, "enabled", False):
                self._loc[key] = {"status": "unavailable", "vehicles": []}
            else:
                self._loc[key] = self.live.locations(key)
        return self._loc[key]

    def route_len(self, route_id):
        key = str(route_id)
        if key not in self._route_len:
            try:
                stops = self.store.route_stops(key)
                self._route_len[key] = max((int(s.get("station_seq") or 0) for s in stops), default=0)
            except Exception:
                self._route_len[key] = 0
        return self._route_len[key]

    # ---------- 판정 ----------
    def judge(self, part: dict, walk_to_board_sec: float) -> dict:
        """bus part(board/route/seq_from) + 승차 정류장까지 도보 초 → 판정 dict.

        반환: {tier, wait_sec, predict_min, stops_away, plate_no, route_name, source, reason}
        """
        route_id = str(part["route"]["route_id"])
        route_name = part["route"].get("name")
        board = part["board"]
        need = float(walk_to_board_sec) + MISS_BUFFER_SEC
        arr = self.arrivals(board["poi_id"])
        base = {"route_id": route_id, "route_name": route_name, "board_station_id": str(board["poi_id"]),
                "queried_at": int(self.now)}

        if arr.get("status") != "success":
            return dict(base, tier=0, wait_sec=UNKNOWN_WAIT_SEC, source="none",
                        reason=arr.get("reason") or "realtime unavailable")

        item = next((it for it in arr.get("items") or [] if str(it.get("route_id")) == route_id), None)
        vehicles = []
        if item is not None:
            flag = (item.get("flag") or "").upper()
            if flag not in EXCLUDED_FLAGS:
                vehicles = item.get("vehicles") or []
        # 1) 도착정보에서 저상 차량 — 도착 전에 지나가는 차량은 제외
        best = None
        for v in vehicles:
            if not v.get("low_floor"):
                continue
            ps = _predict_sec(v)
            if ps is None or ps < need:
                continue
            if best is None or ps < best[0]:
                best = (ps, v)
        if best is not None:
            ps, v = best
            return dict(base, tier=1, wait_sec=max(ps - float(walk_to_board_sec), 0.0),
                        predict_min=v.get("predict_min"), stops_away=v.get("stops_away"),
                        plate_no=v.get("plate_no"), source="arrivals", reason=None)

        # 2) 도착정보 2대에 (탈 수 있는) 저상이 없다 → 노선 위치정보로 상류 저상 차량 탐색
        loc = self.locations(route_id)
        if loc.get("status") == "success":
            n_total = self.route_len(route_id)
            cand = None
            for v in loc.get("vehicles") or []:
                if not v.get("low_floor"):
                    continue
                away = _upstream_stops(v.get("station_seq"), part.get("seq_from"), n_total)
                if away is None:
                    continue
                est = away * transit.BUS_SEC_PER_STOP
                if est < need:
                    continue
                if cand is None or away < cand[0]:
                    cand = (away, v)
            if cand is not None:
                away, v = cand
                return dict(base, tier=2, wait_sec=max(away * transit.BUS_SEC_PER_STOP - float(walk_to_board_sec), 0.0),
                            predict_min=None, stops_away=away, plate_no=v.get("plate_no"),
                            source="locations", reason=None)
            return dict(base, tier=3, wait_sec=None, source="locations",
                        reason="no low-floor vehicle in service")
        # 위치정보까지 못 받으면: 도착정보에 차량이 있었지만 저상이 아니었을 뿐 — 판정 불가
        return dict(base, tier=0, wait_sec=UNKNOWN_WAIT_SEC, source="arrivals",
                    reason=loc.get("reason") or "locations unavailable")


def rank_key(tier: int, seconds: float):
    return (TIER_RANK.get(int(tier), 2), float(seconds))


def leg_warning(j: dict) -> str:
    """판정 결과 → 이용자 경고 문구(버스 leg 최상단)."""
    name = j.get("route_name") or j.get("route_id")
    t = j.get("tier")
    if t == 1:
        away = j.get("stops_away")
        return ("저상버스 %s번이 약 %d분 뒤 도착 예정입니다(%s 정거장 전) — 실시간 정보라 변동될 수 있습니다"
                % (name, int(j.get("predict_min") or 0), away if away is not None else "?"))
    if t == 2:
        return ("저상버스 %s번이 운행 중입니다(약 %s 정거장 전) — 도착 시각은 정류장 도착정보에서 다시 확인하세요"
                % (name, j.get("stops_away")))
    if t == 3:
        return "현재 운행 중인 저상버스가 없습니다 — 이 구간은 일반 버스 기준 안내입니다"
    return "실시간 저상버스 정보를 확인하지 못했습니다 — 정류장 도착정보에서 저상 차량을 확인하세요"


NO_LOW_FLOOR_WARNING = "현재 운행 중인 저상버스가 없습니다 — 일반 버스 경로로 안내합니다. 정류장에서 저상 차량을 다시 확인하세요"
UNKNOWN_LOW_FLOOR_WARNING = "실시간 저상버스 정보를 확인하지 못했습니다 — 정류장에서 저상 차량을 확인하세요"
