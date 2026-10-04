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

실시간 조회는 요청 단위 예산(`gbis_live.LiveBudget`) 안에서만 한다. 조회가 연속으로 실패하거나
누적 시간이 예산을 넘으면 그 요청의 남은 정류장·노선은 조회하지 않고 tier 0(판정 불가)으로 둔다 —
외부 API 장애가 "정류장 수 × 타임아웃"만큼 경로 계획을 붙잡지 않게 한다.
단, 캐시에 남아 있는 응답은 예산이 끝난 뒤에도 쓴다(조회가 아니므로 시간이 들지 않는다).

조회 순서(`LowFloorJudge.prefetch`): 후보를 하나씩 "도착정보 → 위치정보" 순으로 판정하면 가까운
후보의 위치정보 조회가 먼 후보의 도착정보 조회보다 먼저 예산을 쓴다. 외부 API 가 느릴 때는 저상
차량이 오는 정류장의 도착정보를 받기 전에 예산이 끝나 그 후보가 tier 0 으로 밀린다. 그래서
  1) 전 후보 정류장의 도착정보를 먼저(가까운 순, PARALLEL_CALLS 건씩 동시에) 받고
  2) 도착정보만으로 판정이 끝나지 않는 후보의 노선에 대해서만 위치정보를 받는다.
tier 1(도착정보에서 저상 확인)은 위치정보가 필요 없으므로 1) 만으로 확정된다. 판정 규칙 자체는
`judge()` 그대로이고, 예산에 닿지 않으면 결과는 후보별 순차 조회와 같다.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from . import planner as transit
from .gbis_live import PARALLEL_CALLS, LiveBudget

log = logging.getLogger(__name__)

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
    """후보의 버스 part 에 대해 저상 판정을 내린다. 도착정보는 정류장 단위로 캐시된다(gbis_live).

    요청 하나에 판정 객체 하나를 쓴다. budget 은 그 요청의 실시간 조회 예산이며, 같은 요청의
    다른 실시간 조회(노선형상 등)와 나눠 쓰도록 호출 측이 넘길 수 있다(없으면 새로 만든다).
    """

    def __init__(self, live, store, now=None, budget: LiveBudget = None):
        self.live = live
        self.store = store
        self.now = now or time.time()
        self.budget = budget if budget is not None else LiveBudget()
        self._arr = {}
        self._loc = {}
        self._route_len = {}

    @property
    def realtime_unavailable(self) -> bool:
        """이 요청에서 실시간 응답을 한 건도 받지 못했는가(미설정·장애·예산 소진).

        True 면 tier 0 은 "저상버스가 없다"가 아니라 "확인하지 못했다"는 뜻이다 — 탐색 반경을
        넓혀도 판정이 달라지지 않으므로 호출 측은 확장 탐색을 하지 않는다.
        캐시에서 꺼내 쓴 성공 응답도 받은 응답으로 센다.
        """
        return self.budget.ok_cnt == 0 and self.budget.cache_hit_cnt == 0

    def _cached(self, kind: str, key: str, **kwargs):
        """캐시에 있는 성공 응답(dict) 또는 None — 조회하지 않는다.

        kind 는 "arrivals" 또는 "locations". 실시간 클라이언트에 `<kind>_cached` 가 없거나(캐시가
        없는 구현), 캐시에 없거나, 꺼내는 중 예외가 나면 None 이다.
        """
        fn = getattr(self.live, kind + "_cached", None)
        if not callable(fn):
            return None
        try:
            out = fn(key, **kwargs)
        except Exception:
            return None
        if not isinstance(out, dict) or out.get("status") != "success":
            return None
        self.budget.cache_hit()
        return out

    @staticmethod
    def _as_result(out) -> dict:
        """조회 함수의 반환값(또는 예외 객체)을 결과 dict 로 — 예외·형식 오류는 실시간 불가로 바꾼다."""
        if isinstance(out, BaseException):
            return {"status": "unavailable", "reason": "%s: %s" % (type(out).__name__, out)}
        if not isinstance(out, dict):
            return {"status": "unavailable", "reason": "unexpected realtime response"}
        return out

    def _query(self, kind: str, key: str, **kwargs) -> dict:
        """실시간 조회 한 번 — 예산 확인 → 실행 → 성공·실패 기록.

        kind: "arrivals"(정류장 도착정보) 또는 "locations"(노선 위치정보), key: 정류장·노선 id.
        반환: 조회 결과 dict. 예산을 넘겼거나 연속 실패 임계에 이르렀으면 조회하지 않는다 —
        캐시에 성공 응답이 있으면 그것을 돌려주고(예산은 네트워크 호출에만 든다), 없으면
        {"status": "unavailable", "reason": 생략 사유, "skipped": True} 를 돌려준다.
        조회 함수가 예외를 올려도 실시간 불가로 바꿔 돌려준다(경로 계획을 멈추지 않는다).
        """
        reason = self.budget.skip_reason()
        if reason is not None:
            hit = self._cached(kind, key, **kwargs)
            if hit is not None:
                return hit
            self.budget.skipped()
            return {"status": "unavailable", "reason": reason, "skipped": True}
        try:
            out = self.budget.call(getattr(self.live, kind), key, **kwargs)
        except Exception as e:
            out = e
        out = self._as_result(out)
        self.budget.note(out.get("status") == "success")
        return out

    def _route_meta(self, station_key: str) -> dict:
        try:
            return self.store.stop_route_meta(station_key)
        except Exception:
            return {}

    # ---------- 미리 받기(동시 조회) ----------
    def _fetch_many(self, kind: str, keys: list, memo: dict, list_field: str, pool_box: list) -> None:
        """keys(정류장 또는 노선 id, 우선순위 순)를 받아 memo 에 채운다.

        1) 캐시에 성공 응답이 있는 대상은 조회 없이 채운다(예산과 무관).
        2) 나머지는 PARALLEL_CALLS 건씩 묶어 동시에 조회한다. 묶음을 보내기 전마다 예산을 확인하고,
           예산이 끝났거나 연속 실패 임계에 이르렀으면 남은 대상은 채우지 않는다 — 판정 때
           `arrivals()`·`locations()` 가 생략(tier 0)으로 처리한다.
        성공·실패는 묶음 단위로 기록한다(묶음 전체가 실패하면 다음 묶음은 나가지 않는다).

        pool_box: 스레드풀을 담아 두는 1칸 목록. 처음 필요할 때 만들고 `prefetch` 가 닫는다 —
        조회할 것이 없거나 한 건뿐이면 풀을 만들지 않는다.
        저장소 조회(노선 메타)는 작업 스레드가 아니라 이 스레드에서 미리 한다(저장소 구현이
        스레드 간 공유에 안전하다는 보장이 없다).
        """
        todo = []
        for k in keys:
            if k in memo or k in todo:
                continue
            kw = {"route_meta": self._route_meta(k)} if kind == "arrivals" else {}
            hit = self._cached(kind, k, **kw)
            if hit is not None:
                hit.setdefault(list_field, [])
                memo[k] = hit
            else:
                todo.append(k)
        fn = getattr(self.live, kind)
        for i in range(0, len(todo), PARALLEL_CALLS):
            chunk = todo[i:i + PARALLEL_CALLS]
            if self.budget.skip_reason() is not None:
                return
            if len(chunk) == 1:
                kw = {"route_meta": self._route_meta(chunk[0])} if kind == "arrivals" else {}
                out = self._query(kind, chunk[0], **kw)
                out.setdefault(list_field, [])
                memo[chunk[0]] = out
                continue
            if not pool_box:
                pool_box.append(ThreadPoolExecutor(max_workers=PARALLEL_CALLS,
                                                   thread_name_prefix="gbis-live"))
            thunks = []
            for k in chunk:
                kw = {"route_meta": self._route_meta(k)} if kind == "arrivals" else {}
                thunks.append(lambda k=k, kw=kw: fn(k, **kw))
            outs = [self._as_result(o) for o in self.budget.call_many(pool_box[0], thunks)]
            for k, out in zip(chunk, outs):
                out.setdefault(list_field, [])
                memo[k] = out
            # 연속 실패 집계는 묶음 단위로 한다. 동시에 보낸 조회는 순서가 없으므로 건별로 차례로
            # 기록하면 "성공, 성공, 실패, 실패" 와 "실패, 실패, 성공, 성공" 이 다르게 집계되어,
            # 정류장 두 곳만 오류인 부분 장애에서 남은 조회가 전부 생략될 수 있다.
            # 한 건이라도 성공했으면 서비스는 살아 있다고 보고 연속 실패를 0 으로 되돌리고,
            # 묶음 전체가 실패했을 때만 실패 건수만큼 올린다(임계 도달 → 다음 묶음 생략).
            oks = [o.get("status") == "success" for o in outs]
            if any(oks):
                self.budget.note(True)
            else:
                for _ in oks:
                    self.budget.note(False)

    def prefetch(self, items: list) -> None:
        """후보들의 판정에 필요한 실시간 응답을 미리 받아 둔다(이후 `judge()` 는 받아 둔 값을 쓴다).

        인자 items: [(bus part, 승차 정류장까지 도보 초)] — 후보 전체. 도보가 짧은(가까운) 순으로 조회한다.
        1) 전 후보 승차 정류장의 도착정보  2) 도착정보로 판정이 끝나지 않은 후보의 노선 위치정보.
        실시간이 꺼져 있으면 아무것도 하지 않는다. 예외를 올리지 않는다 — 받지 못한 대상은
        판정 때 tier 0(판정 불가)이 된다.
        """
        if not items or not getattr(self.live, "enabled", False):
            return
        items = sorted(items, key=lambda x: float(x[1]))
        pool_box = []
        try:
            self._fetch_many("arrivals", [str(p["board"]["poi_id"]) for p, _w in items],
                             self._arr, "items", pool_box)
            routes = [str(p["route"]["route_id"]) for p, w in items if self._needs_locations(p, w)]
            self._fetch_many("locations", routes, self._loc, "vehicles", pool_box)
        except Exception as e:
            # 미리 받기는 판정을 빠르게 하려는 준비 단계다. 스레드를 만들지 못하는 등 여기서 난
            # 예외가 경로 계획 전체를 실패시키면 안 된다 — 받지 못한 대상은 judge() 가
            # 한 건씩 조회하거나(예산이 남았을 때) 판정 불가(tier 0)로 처리한다.
            log.warning("실시간 미리 받기 실패 — 판정 단계에서 개별 조회로 대신합니다: %s", type(e).__name__)
        finally:
            if pool_box:
                # 작업은 call_many 안에서 이미 끝났다 — 스레드만 정리한다
                pool_box[0].shutdown(wait=True)

    def _needs_locations(self, part: dict, walk_to_board_sec: float) -> bool:
        """이 후보의 판정에 노선 위치정보가 필요한가 — `judge()` 가 위치정보를 읽는 조건과 같다.

        도착정보를 받았고(성공) 그 안에 탈 수 있는 저상 차량이 없을 때만 필요하다. 도착정보를 받지
        못했으면 위치정보와 무관하게 tier 0 이고, 저상 차량이 확인됐으면 tier 1 로 끝난다.
        """
        arr = self._arr.get(str(part["board"]["poi_id"]))
        if not arr or arr.get("status") != "success":
            return False
        need = float(walk_to_board_sec) + MISS_BUFFER_SEC
        return self._catchable_low_floor(arr, str(part["route"]["route_id"]), need) is None

    @staticmethod
    def _catchable_low_floor(arr: dict, route_id: str, need: float):
        """도착정보에서 해당 노선의 탈 수 있는 저상 차량 중 가장 빨리 오는 것 — (도착 예정 초, 차량) 또는 None.

        운행 종료·회차 대기 flag 의 차량과, 승차 정류장 도착 전에 지나가는 차량(도착 예정 < need)은 뺀다.
        """
        item = next((it for it in arr.get("items") or [] if str(it.get("route_id")) == route_id), None)
        vehicles = []
        if item is not None:
            flag = (item.get("flag") or "").upper()
            if flag not in EXCLUDED_FLAGS:
                vehicles = item.get("vehicles") or []
        best = None
        for v in vehicles:
            if not v.get("low_floor"):
                continue
            ps = _predict_sec(v)
            if ps is None or ps < need:
                continue
            if best is None or ps < best[0]:
                best = (ps, v)
        return best

    # ---------- 실시간 조회(정류장·노선 단위 1회) ----------
    def arrivals(self, station_id):
        key = str(station_id)
        if key not in self._arr:
            if not getattr(self.live, "enabled", False):
                self._arr[key] = {"status": "unavailable", "reason": "realtime disabled", "items": []}
            else:
                out = self._query("arrivals", key, route_meta=self._route_meta(key))
                out.setdefault("items", [])
                self._arr[key] = out
        return self._arr[key]

    def locations(self, route_id):
        key = str(route_id)
        if key not in self._loc:
            if not getattr(self.live, "enabled", False):
                self._loc[key] = {"status": "unavailable", "vehicles": []}
            else:
                out = self._query("locations", key)
                out.setdefault("vehicles", [])
                self._loc[key] = out
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

        # 1) 도착정보에서 저상 차량 — 도착 전에 지나가는 차량은 제외
        best = self._catchable_low_floor(arr, route_id, need)
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
