# -*- coding: utf-8 -*-
"""경기버스정보(GBIS) 실시간 조회 — 정류장 도착정보 · 노선 차량 위치.

정적 데이터(01 `tran_bus_*`)에는 저상버스 정차 여부가 없다. 그래서 지금까지는
"실시간 도착정보로 확인하세요"라는 경고만 붙여 왔고, 확인은 이용자 몫이었다.
이 모듈이 그 확인을 서비스 안으로 가져온다.

- 도착정보 `getBusArrivalListv2?stationId=`  — 정류장 기준, 노선별 1·2번째 차량의
  도착 예정(분)·몇 정거장 전·**저상 여부(lowPlate)**·차량번호
- 위치정보 `getBusLocationListv2?routeId=`   — 노선 기준, 운행 중인 전 차량의
  현재 정류장 순번·저상 여부

인증키는 공공데이터포털 일반 인증키(`DATA_GO_KR_API_KEY`) 하나로 두 API 를 모두 부른다.
호출 실패는 예외로 올리지 않고 ``{"status": "unavailable", "reason": ...}`` 로 돌려준다 —
실시간을 못 붙였다고 경로 안내가 멈추면 안 된다.

응답 값의 빈 항목은 빈 문자열("")로 온다(실측 2026-09-02). 정수 변환은 전부 관대하게 한다.
"""
from __future__ import annotations

import contextvars
import http.client
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("route_api.gbis")

ARRIVAL_PATH = "/busarrivalservice/v2/getBusArrivalListv2"
LOCATION_PATH = "/buslocationservice/v2/getBusLocationListv2"
# 노선형상(노선이 지나는 도로 좌표열, lineSeq 순) — 버스 구간 지도선용 (v1.20.0)
ROUTE_LINE_PATH = "/busrouteservice/v2/getBusRouteLineListv2"
ROUTE_LINE_TTL_SEC = 24 * 3600.0   # 형상은 하루 단위로 바뀌지 않는다

# lowPlate: 0 일반 / 1 저상 / 2 2층 등 특수차량 — 휠체어 승차 기준으로는 1 만 '저상'이다
LOW_FLOOR_CODE = 1

# stateCd(위치정보): 0 교차로 통과 / 1 정류소 도착 / 2 정류소 출발
STATE_LABEL = {0: "이동 중", 1: "정류소 도착", 2: "정류소 출발"}

# ── 요청 단위 실시간 조회 예산 ──
# 경로 계획 한 건은 후보 정류장·노선마다 실시간을 조회한다. 조회 한 번의 타임아웃이 3초이므로
# 외부 API 가 느리거나 응답하지 않으면 "조회 수 × 3초"가 그대로 계획 응답시간에 쌓인다.
# 호출 측(경로 API 클라이언트)은 6초에 요청을 끊고, 연속으로 끊기면 한동안 호출 자체를 막는다 —
# 실시간을 못 붙인 것이 경로 안내 전체의 실패로 번진다.
#
# REQUEST_BUDGET_SEC: 계획 한 건이 실시간 조회에 쓸 수 있는 누적 시간. 호출 측 제한 6초에서
#   보행 경로 계산·DB 조회 몫을 넉넉히(약 4초) 남기도록 2초로 둔다. 정상 응답은 건당 0.1~0.3초라
#   후보 8개 안팎의 조회는 이 안에 끝난다.
# CONSECUTIVE_FAIL_LIMIT: 연속 실패가 이 횟수에 이르면 그 요청의 남은 조회를 생략한다.
#   1회는 특정 정류장·노선만의 일시 오류일 수 있어 한 번 더 확인하고, 서로 다른 대상에서
#   2회 연속이면 서비스 쪽 장애로 본다(즉시 거절·한도 초과처럼 빨리 실패하는 경우에도
#   남은 대상마다 외부 API 를 두드리지 않는다).
# MIN_CALL_TIMEOUT_SEC: 예산이 거의 남지 않았을 때 조회 한 번에 주는 최소 타임아웃. 이보다 짧으면
#   정상 응답도 끊긴다. 따라서 실시간 조회 누적 시간의 상한은 예산 + 이 값이다.
REQUEST_BUDGET_SEC = 2.0
CONSECUTIVE_FAIL_LIMIT = 2
MIN_CALL_TIMEOUT_SEC = 0.5

# LiveBudget.call() 이 실행 중인 조회에 적용할 타임아웃 상한(초). None 이면 상한 없음.
# 조회 메서드의 시그니처를 바꾸지 않고 `_http_get` 까지 전달하기 위해 컨텍스트 변수로 둔다.
_TIMEOUT_CAP: contextvars.ContextVar = contextvars.ContextVar("gbis_timeout_cap", default=None)


class LiveBudget:
    """경로 계획 한 건(요청 하나)의 실시간 조회 예산 — 누적 시간과 연속 실패를 센다.

    조회하는 쪽은 `allow()` 로 조회해도 되는지 묻고, `call()` 로 조회를 실행한 뒤
    `note()` 로 성공·실패를 알린다. 예산을 넘겼거나 연속 실패가 임계에 이르면 `allow()` 가
    False 가 되고, 호출 측은 남은 대상을 조회하지 않고 "실시간 불가"로 처리한다.
    실시간이 정상이면 예산에 닿지 않으므로 결과는 예산이 없을 때와 같다.

    요청마다 새로 만든다(요청 사이에 공유하지 않는다). 한 요청은 한 스레드에서 순차로
    조회하므로 잠금은 두지 않는다.
    """

    def __init__(self, budget_sec: float = None, fail_limit: int = None, clock=time.monotonic):
        self.budget_sec = float(REQUEST_BUDGET_SEC if budget_sec is None else budget_sec)
        self.fail_limit = int(CONSECUTIVE_FAIL_LIMIT if fail_limit is None else fail_limit)
        self._clock = clock
        self.spent_sec = 0.0          # 실시간 조회에 쓴 누적 시간
        self.fail_streak = 0          # 연속 실패 횟수(성공하면 0 으로)
        self.ok_cnt = 0               # 성공한 조회 수
        self.skipped_cnt = 0          # 예산·연속 실패로 생략한 조회 수

    def skip_reason(self):
        """조회를 생략해야 하는 이유(문자열). 조회해도 되면 None."""
        if self.fail_streak >= self.fail_limit:
            return "realtime skipped: %d consecutive failures" % self.fail_streak
        if self.spent_sec >= self.budget_sec:
            return "realtime skipped: time budget %.1fs used" % self.budget_sec
        return None

    def allow(self) -> bool:
        return self.skip_reason() is None

    def call(self, fn, *args, **kwargs):
        """조회 함수 fn 을 실행하고 걸린 시간을 누적한다. 반환값·예외는 fn 의 것을 그대로 넘긴다.

        실행 동안 조회 타임아웃을 남은 예산(최소 MIN_CALL_TIMEOUT_SEC)으로 낮춘다 — 예산이
        0.3초 남은 시점에 3초짜리 조회가 시작돼 예산을 크게 넘기는 일을 막는다.
        """
        cap = max(self.budget_sec - self.spent_sec, MIN_CALL_TIMEOUT_SEC)
        token = _TIMEOUT_CAP.set(cap)
        t0 = self._clock()
        try:
            return fn(*args, **kwargs)
        finally:
            self.spent_sec += max(self._clock() - t0, 0.0)
            _TIMEOUT_CAP.reset(token)

    def note(self, ok: bool) -> None:
        """조회 결과를 기록한다 — 성공이면 연속 실패를 0 으로, 실패면 1 늘린다."""
        if ok:
            self.ok_cnt += 1
            self.fail_streak = 0
        else:
            self.fail_streak += 1

    def skipped(self) -> None:
        self.skipped_cnt += 1


def _int(v, default=None):
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def _str(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _dist_m(lat1, lon1, lat2, lon2) -> float:
    import math
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


class GbisLive:
    """실시간 조회 클라이언트. 같은 정류장·노선을 짧은 간격으로 반복 조회하는 폴링을
    TTL 캐시로 흡수한다(개발계정 도착정보 한도 1,000회/일)."""

    def __init__(self, api_key: str = "", base_url: str = "https://apis.data.go.kr/6410000",
                 timeout_sec: float = 3.0, cache_ttl_sec: float = 20.0, fetch=None):
        self.api_key = api_key or ""
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_sec = float(timeout_sec)
        self.cache_ttl_sec = float(cache_ttl_sec)
        self._fetch = fetch or self._http_get
        self._cache = {}
        self._lock = threading.Lock()

    # ---------- 공통 ----------
    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _http_get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "iitp-dabt-route/1.0"})
        # 요청 단위 예산(LiveBudget.call)이 걸려 있으면 타임아웃을 남은 예산까지로 낮춘다
        cap = _TIMEOUT_CAP.get()
        timeout = self.timeout_sec if cap is None else min(self.timeout_sec, float(cap))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _url(self, path: str, **params) -> str:
        q = {"serviceKey": self.api_key, "format": "json"}
        q.update({k: v for k, v in params.items() if v not in (None, "")})
        return "%s%s?%s" % (self.base_url, path, urllib.parse.urlencode(q))

    def _get(self, key: tuple, path: str, ttl_sec: float = None, **params):
        """캐시 → 호출. 반환: (msg_body dict | None, error str | None)."""
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1], hit[2]
        ttl_ok = self.cache_ttl_sec if ttl_sec is None else float(ttl_sec)
        if not self.enabled:
            return None, "인증키가 설정되지 않았습니다(DATA_GO_KR_API_KEY)"
        body, err = None, None
        try:
            data = self._fetch(self._url(path, **params))
            # 응답 JSON 의 최상위·response·msgHeader·msgBody 가 dict 가 아닌 경우(게이트웨이가
            # 오류를 배열·문자열로 돌려주는 등)에는 .get 호출이 AttributeError 로 번진다 —
            # 형식 오류로 보고 "실시간 불가"로 돌려준다.
            resp = data.get("response") if isinstance(data, dict) else None
            head = resp.get("msgHeader") if isinstance(resp, dict) else None
            if not isinstance(resp, dict) or not isinstance(head, dict):
                err = "GBIS 응답 형식 오류(%s)" % type(data).__name__
            else:
                code = _int(head.get("resultCode"), -1)
                if code == 0:
                    body = resp.get("msgBody") or {}
                    if not isinstance(body, dict):
                        body, err = None, "GBIS 응답 형식 오류(msgBody=%s)" % type(body).__name__
                elif code == 4:
                    body = {}                    # 결과 없음 — 정상 응답의 한 형태
                else:
                    err = "GBIS resultCode=%s %s" % (code, head.get("resultMessage") or "")
        except urllib.error.HTTPError as e:
            err = "HTTP %s" % e.code
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                http.client.HTTPException) as e:
            # http.client.HTTPException: 본문을 읽는 도중 연결이 끊기면 IncompleteRead 가 난다.
            # OSError·URLError 의 하위가 아니어서 따로 잡지 않으면 호출자에게 예외로 올라간다.
            err = "%s: %s" % (type(e).__name__, e)
        if err:
            logger.warning("GBIS 호출 실패 %s %s — %s", path, params, err)
        with self._lock:
            # 실패도 짧게 캐시한다 — 장애 중 폴링이 외부 API 를 두드리지 않게
            ttl = ttl_ok if not err else min(self.cache_ttl_sec, 10.0)
            self._cache[key] = (now + ttl, body, err)
            if len(self._cache) > 500:
                self._cache.clear()
        return body, err

    # ---------- 노선형상 ----------
    def route_line(self, route_id) -> list:
        """노선이 지나는 도로 좌표열 [[lat, lng], ...] (lineSeq 오름차순). 실패·미설정이면 [].

        정적 DB 에는 경유 정류장 좌표만 있어 버스 구간 지도선이 정류장끼리 직선으로 이어져
        건물을 뚫고 지나갔다(실증 2026-09-03). 형상은 하루 캐시한다."""
        body, err = self._get(("line", str(route_id)), ROUTE_LINE_PATH, ttl_sec=ROUTE_LINE_TTL_SEC,
                              routeId=route_id)
        if err or not body:
            return []
        raw = body.get("busRouteLineList") or []
        if isinstance(raw, dict):
            raw = [raw]
        pts = []
        for it in (raw if isinstance(raw, list) else []):
            if not isinstance(it, dict):     # 항목이 dict 가 아니면 건너뛴다(형식 오류 방어)
                continue
            try:
                x = float(it.get("x")); y = float(it.get("y"))
            except (TypeError, ValueError):
                continue
            if not (124.0 <= x <= 132.0 and 33.0 <= y <= 39.0):   # x=경도, y=위도 (뒤바뀐 항목 방어)
                if 124.0 <= y <= 132.0 and 33.0 <= x <= 39.0:
                    x, y = y, x
                else:
                    continue
            pts.append((_int(it.get("lineSeq"), len(pts)), y, x))
        pts.sort(key=lambda p: p[0])
        return [[round(p[1], 7), round(p[2], 7)] for p in pts]

    @staticmethod
    def slice_line(line: list, board: dict, alight: dict, max_snap_m: float = 120.0) -> list:
        """형상에서 승차→하차 정류장 사이 구간만 잘라낸다. 정류장이 형상에서 max_snap_m 보다 멀거나
        순서가 뒤집히면(형상이 편도만 있는 경우 등) [] — 호출 쪽이 정류장 직선으로 폴백한다."""
        if not line or len(line) < 2:
            return []

        def near_idx(lat, lng):
            # 형상이 순환·왕복이면 같은 정류장 근처를 두 번 지난다 — 후보를 전부 모아 승차<하차 인 가장 짧은 쌍을 고른다
            out = []
            for i, (a, b) in enumerate(line):
                d = _dist_m(lat, lng, a, b)
                if d <= max_snap_m:
                    out.append((i, d))
            return out

        pair = None
        for i0, _d0 in near_idx(float(board["lat"]), float(board["lng"])):
            for i1, _d1 in near_idx(float(alight["lat"]), float(alight["lng"])):
                if i1 > i0 and (pair is None or (i1 - i0) < (pair[1] - pair[0])):
                    pair = (i0, i1)
        if pair is None:
            return []
        i0, i1 = pair
        seg = [[float(board["lat"]), float(board["lng"])]] + [list(p) for p in line[i0:i1 + 1]] \
            + [[float(alight["lat"]), float(alight["lng"])]]
        return seg

    # ---------- 도착정보 ----------
    @staticmethod
    def _vehicle(item: dict, n: int):
        plate = _str(item.get("plateNo%d" % n))
        pred = _int(item.get("predictTime%d" % n))
        if plate is None and pred is None:
            return None
        low = _int(item.get("lowPlate%d" % n))
        return {
            "order": n,
            "predict_min": pred,
            "predict_sec": _int(item.get("predictTimeSec%d" % n)),
            "stops_away": _int(item.get("locationNo%d" % n)),
            "current_stop": _str(item.get("stationNm%d" % n)),
            "low_floor": (low == LOW_FLOOR_CODE) if low is not None else None,
            "plate_no": plate,
            "remain_seat_cnt": _int(item.get("remainSeatCnt%d" % n)),
            "crowded": _int(item.get("crowded%d" % n)),
        }

    def arrivals(self, station_id, route_id=None, route_meta: dict = None) -> dict:
        """정류장 도착정보.

        route_meta: {route_id(str): {"name","type","end_station"}} — 정적 DB 의 노선명·유형을
        덧입힌다(도착 API 도 routeName 을 주지만 유형은 코드뿐이다).
        route_id 를 주면 그 노선만 남긴다(경로 안내 중 승차 노선 확인용).
        """
        body, err = self._get(("arr", str(station_id)), ARRIVAL_PATH, stationId=station_id)
        if err:
            return {"status": "unavailable", "reason": err, "station_id": str(station_id),
                    "items": [], "next_low_floor": None}
        raw = (body or {}).get("busArrivalList") or []
        if isinstance(raw, dict):
            raw = [raw]
        items = []
        for it in (raw if isinstance(raw, list) else []):
            if not isinstance(it, dict):     # 항목이 dict 가 아니면 건너뛴다(형식 오류 방어)
                continue
            rid = _str(it.get("routeId"))
            if route_id is not None and str(route_id) != rid:
                continue
            meta = (route_meta or {}).get(rid) or {}
            vehicles = [v for v in (self._vehicle(it, 1), self._vehicle(it, 2)) if v]
            items.append({
                "route_id": rid,
                "route_name": meta.get("name") or _str(it.get("routeName")),
                "route_type": meta.get("type"),
                "end_station": meta.get("end_station") or _str(it.get("routeDestName")),
                "station_seq": _int(it.get("staOrder")),
                "flag": _str(it.get("flag")),
                "vehicles": vehicles,
            })
        items.sort(key=lambda x: (x["vehicles"][0]["predict_min"] if x["vehicles"]
                                  and x["vehicles"][0]["predict_min"] is not None else 9999,
                                  x["route_name"] or ""))
        return {
            "status": "success",
            "station_id": str(station_id),
            "queried_at": int(time.time()),
            "items": items,
            "next_low_floor": self.next_low_floor(items),
        }

    @staticmethod
    def next_low_floor(items: list):
        """가장 빨리 오는 저상 차량 하나. 없으면 None."""
        best = None
        for it in items:
            for v in it.get("vehicles") or []:
                if not v.get("low_floor"):
                    continue
                pm = v.get("predict_min")
                if pm is None:
                    continue
                cand = {"route_id": it["route_id"], "route_name": it.get("route_name"),
                        "route_type": it.get("route_type"), "end_station": it.get("end_station"),
                        "predict_min": pm, "stops_away": v.get("stops_away"),
                        "plate_no": v.get("plate_no")}
                if best is None or pm < best["predict_min"]:
                    best = cand
        return best

    # ---------- 위치정보 ----------
    def locations(self, route_id, stop_index: dict = None) -> dict:
        """노선의 운행 차량 위치. stop_index: {station_id(str): {"name","lat","lng","station_seq"}}
        가 있으면 차량이 있는 정류장의 이름·좌표를 붙인다(지도 표시용)."""
        body, err = self._get(("loc", str(route_id)), LOCATION_PATH, routeId=route_id)
        if err:
            return {"status": "unavailable", "reason": err, "route_id": str(route_id),
                    "vehicles": [], "low_floor_cnt": 0}
        raw = (body or {}).get("busLocationList") or []
        if isinstance(raw, dict):
            raw = [raw]
        vehicles = []
        for it in (raw if isinstance(raw, list) else []):
            if not isinstance(it, dict):     # 항목이 dict 가 아니면 건너뛴다(형식 오류 방어)
                continue
            sid = _str(it.get("stationId"))
            low = _int(it.get("lowPlate"))
            st = _int(it.get("stateCd"))
            v = {
                "vehicle_id": _str(it.get("vehId")),
                "plate_no": _str(it.get("plateNo")),
                "low_floor": (low == LOW_FLOOR_CODE) if low is not None else None,
                "station_id": sid,
                "station_seq": _int(it.get("stationSeq")),
                "state": STATE_LABEL.get(st, None),
                "remain_seat_cnt": _int(it.get("remainSeatCnt")),
                "crowded": _int(it.get("crowded")),
            }
            info = (stop_index or {}).get(sid)
            if info:
                v["station_name"] = info.get("name")
                v["lat"] = info.get("lat")
                v["lng"] = info.get("lng")
            vehicles.append(v)
        vehicles.sort(key=lambda x: (x["station_seq"] if x["station_seq"] is not None else 9999))
        return {
            "status": "success",
            "route_id": str(route_id),
            "queried_at": int(time.time()),
            "vehicles": vehicles,
            "low_floor_cnt": sum(1 for v in vehicles if v["low_floor"]),
        }


def configure(settings) -> GbisLive:
    return GbisLive(api_key=settings.gbis_api_key, base_url=settings.gbis_base_url,
                    timeout_sec=settings.gbis_timeout_sec,
                    cache_ttl_sec=settings.gbis_cache_ttl_sec)


LIVE = GbisLive()
