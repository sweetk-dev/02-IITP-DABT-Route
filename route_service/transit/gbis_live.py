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
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ..engine.geo import haversine_m

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
# REQUEST_BUDGET_SEC: 계획 한 건이 실시간 조회(실제 네트워크 호출)에 쓸 수 있는 시간. 호출 측 제한
#   6초에서 보행 경로 계산·DB 조회·응답 전송 몫으로 약 3초를 남기고 3초를 준다.
#   2초일 때는 외부 API 가 정상이지만 느린 구간(건당 0.3~0.5초)에서 후보 정류장을 다 확인하기 전에
#   예산이 끝나, 저상버스가 오는 정류장이 "판정 불가"로 밀리고 일반버스 정류장이 선택됐다.
#   캐시에 있는 응답을 쓰는 것은 시간이 들지 않으므로 예산과 무관하게 허용한다(GbisLive.*_cached).
# CONSECUTIVE_FAIL_LIMIT: 연속 실패가 이 횟수에 이르면 그 요청의 남은 조회를 생략한다.
#   1회는 특정 정류장·노선만의 일시 오류일 수 있어 한 번 더 확인하고, 서로 다른 대상에서
#   2회 연속이면 서비스 쪽 장애로 본다(즉시 거절·한도 초과처럼 빨리 실패하는 경우에도
#   남은 대상마다 외부 API 를 두드리지 않는다). 동시 조회(call_many)는 한 묶음을 다 보낸 뒤에
#   세므로, 장애 때 실제로 나가는 조회는 한 묶음(PARALLEL_CALLS)까지다.
# MIN_CALL_TIMEOUT_SEC: 예산이 거의 남지 않았을 때 조회 한 번에 주는 최소 타임아웃. 이보다 짧으면
#   정상 응답도 끊긴다. 따라서 실시간 조회 누적 시간의 상한은 예산 + 이 값이다.
# PARALLEL_CALLS: 후보 정류장 도착정보·노선 위치정보를 한 번에 몇 건씩 동시에 조회할지.
#   후보 정류장은 6~10곳이라 4건씩이면 2~3묶음, 즉 조회 2~3회 분량의 시간에 끝난다. 더 키우면
#   외부 API 에 순간 요청이 몰리므로(개발계정 한도·초당 제한) 4로 둔다.
REQUEST_BUDGET_SEC = 3.0
CONSECUTIVE_FAIL_LIMIT = 2
MIN_CALL_TIMEOUT_SEC = 0.5
PARALLEL_CALLS = 4

# LiveBudget.call() 이 실행 중인 조회에 적용할 타임아웃 상한(초). None 이면 상한 없음.
# 조회 메서드의 시그니처를 바꾸지 않고 `_http_get` 까지 전달하기 위해 컨텍스트 변수로 둔다.
_TIMEOUT_CAP: contextvars.ContextVar = contextvars.ContextVar("gbis_timeout_cap", default=None)


class LiveBudget:
    """경로 계획 한 건(요청 하나)의 실시간 조회 예산 — 누적 시간과 연속 실패를 센다.

    조회하는 쪽은 `allow()` 로 조회해도 되는지 묻고, `call()` 로 조회를 실행한 뒤
    `note()` 로 성공·실패를 알린다. 예산을 넘겼거나 연속 실패가 임계에 이르면 `allow()` 가
    False 가 되고, 호출 측은 남은 대상을 조회하지 않고 "실시간 불가"로 처리한다.
    실시간이 정상이면 예산에 닿지 않으므로 결과는 예산이 없을 때와 같다.

    예산은 실제 네트워크 호출에만 든다. 캐시에 남아 있는 응답은 예산이 끝난 뒤에도 쓸 수 있고
    (GbisLive.arrivals_cached 등 — 호출 측이 `allow()` 가 False 일 때 따로 확인한다),
    그렇게 쓴 건수는 `cache_hit_cnt` 로 센다.

    요청마다 새로 만든다(요청 사이에 공유하지 않는다). 집계 값(spent_sec·fail_streak 등)은
    요청을 처리하는 스레드 하나에서만 고친다 — `call_many` 의 작업 스레드는 조회를 실행하고
    걸린 시간을 돌려줄 뿐 집계에 손대지 않으므로 잠금을 두지 않는다.

    clock: 단조 시계 함수(초). 테스트에서 가짜 시계를 넣는다. `call_many` 는 조회를 실행한
    스레드에서 시작·끝 시각을 읽으므로, 가짜 시계는 스레드별로 따로 흘러도 된다.
    """

    def __init__(self, budget_sec: float = None, fail_limit: int = None, clock=time.monotonic):
        self.budget_sec = float(REQUEST_BUDGET_SEC if budget_sec is None else budget_sec)
        self.fail_limit = int(CONSECUTIVE_FAIL_LIMIT if fail_limit is None else fail_limit)
        self._clock = clock
        self.spent_sec = 0.0          # 실시간 조회에 쓴 누적 시간
        self.fail_streak = 0          # 연속 실패 횟수(성공하면 0 으로)
        self.ok_cnt = 0               # 성공한 조회 수
        self.skipped_cnt = 0          # 예산·연속 실패로 생략한 조회 수
        self.cache_hit_cnt = 0        # 조회 없이 캐시의 성공 응답을 쓴 건수(예산과 무관)

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
        cap = self.timeout_cap()
        token = _TIMEOUT_CAP.set(cap)
        t0 = self._clock()
        try:
            return fn(*args, **kwargs)
        finally:
            self.spent_sec += max(self._clock() - t0, 0.0)
            _TIMEOUT_CAP.reset(token)

    def timeout_cap(self) -> float:
        """지금 시작하는 조회 한 번에 줄 타임아웃 상한(초) — 남은 예산, 최소 MIN_CALL_TIMEOUT_SEC."""
        return max(self.budget_sec - self.spent_sec, MIN_CALL_TIMEOUT_SEC)

    def call_many(self, pool, thunks: list) -> list:
        """조회 여러 건을 스레드풀에서 동시에 실행한다(한 묶음).

        인자
          pool   : concurrent.futures.ThreadPoolExecutor — 묶음 크기 이상의 작업 스레드를 가진 풀.
                   요청 단위로 만든 풀을 넘긴다(여러 요청이 풀을 나눠 쓰면 대기 시간이 섞인다).
          thunks : 인자 없는 조회 함수 목록(각각 dict 를 돌려준다).
        반환: thunks 와 같은 순서의 결과 목록. 조회 함수가 예외를 올리면 그 자리에 예외 객체가 들어간다
              (호출 측이 "실시간 불가"로 바꾼다 — 한 건의 예외가 묶음 전체를 버리게 하지 않는다).

        시간 집계: 동시에 실행하므로 묶음이 쓰는 시간은 건별 합이 아니라 가장 오래 걸린 한 건이다.
        각 작업 스레드가 자기 조회의 시작·끝을 재서 돌려주고, 그 최댓값을 예산에 더한다.
        타임아웃 상한은 묶음 시작 시점의 남은 예산으로 모든 건에 똑같이 준다. 컨텍스트 변수는
        작업 스레드로 전달되지 않으므로 작업 스레드 안에서 직접 건다.
        성공·실패 기록(`note`)은 하지 않는다 — 호출 측이 결과 순서대로 한다.
        """
        if not thunks:
            return []
        cap = self.timeout_cap()
        clock = self._clock

        def run(fn):
            token = _TIMEOUT_CAP.set(cap)
            t0 = clock()
            try:
                out = fn()
            except Exception as e:            # 조회 한 건의 예외는 결과로 돌려준다
                out = e
            finally:
                dur = max(clock() - t0, 0.0)
                _TIMEOUT_CAP.reset(token)
            return out, dur

        results = list(pool.map(run, thunks))
        self.spent_sec += max(d for _o, d in results)
        return [o for o, _d in results]

    def note(self, ok: bool) -> None:
        """조회 결과를 기록한다 — 성공이면 연속 실패를 0 으로, 실패면 1 늘린다."""
        if ok:
            self.ok_cnt += 1
            self.fail_streak = 0
        else:
            self.fail_streak += 1

    def skipped(self) -> None:
        self.skipped_cnt += 1

    def cache_hit(self) -> None:
        """조회 없이 캐시의 성공 응답을 썼음을 기록한다(시간·연속 실패 집계는 건드리지 않는다)."""
        self.cache_hit_cnt += 1


def _is_timeout(e: BaseException) -> bool:
    """예외가 타임아웃인가 — urlopen 은 연결 단계 타임아웃을 URLError(reason=timeout)로 감싼다."""
    if isinstance(e, (TimeoutError, socket.timeout)):
        return True
    return isinstance(e, urllib.error.URLError) and isinstance(e.reason, (TimeoutError, socket.timeout))


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
        # 요청 단위 예산이 이 조회의 타임아웃을 정상값보다 짧게 줄였는가(아래 캐시 저장 판단용)
        cap = _TIMEOUT_CAP.get()
        shortened = cap is not None and float(cap) < self.timeout_sec
        timed_out = False
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
            timed_out = _is_timeout(e)
        if err:
            logger.warning("GBIS 호출 실패 %s %s — %s", path, params, err)
        if err and timed_out and shortened:
            # 예산 때문에 줄어든 타임아웃(예: 0.5초)으로 난 타임아웃은 "이 요청이 더 기다리지 못했다"는
            # 뜻이지 외부 API 가 응답하지 않는다는 뜻이 아니다. 전역 캐시에 실패로 남기면 직후의 단독
            # 조회(정류장 도착정보 API 등, 정상 타임아웃 3초)가 조회도 해 보지 않고 10초 동안
            # "실시간 불가"를 받는다 — 저장하지 않는다. 정상 타임아웃으로 난 실패는 종전대로 저장한다.
            return body, err
        with self._lock:
            # 실패도 짧게 캐시한다 — 장애 중 폴링이 외부 API 를 두드리지 않게
            ttl = ttl_ok if not err else min(self.cache_ttl_sec, 10.0)
            self._cache[key] = (now + ttl, body, err)
            if len(self._cache) > 500:
                self._cache.clear()
        return body, err

    def _peek(self, key: tuple):
        """캐시에 유효한 **성공** 응답이 있으면 (True, msg_body), 없으면 (False, None). 조회는 하지 않는다.

        실패로 캐시된 항목·만료된 항목은 없는 것으로 본다. 성공 응답의 본문은 빈 dict 일 수 있어
        (결과 없음) 본문만으로는 적중 여부를 가릴 수 없으므로 적중 여부를 따로 돌려준다.
        """
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now and not hit[2]:
                return True, hit[1]
        return False, None

    # ---------- 노선형상 ----------
    def route_line(self, route_id) -> list:
        """노선이 지나는 도로 좌표열 [[lat, lng], ...] (lineSeq 오름차순). 실패·미설정이면 [].

        정적 DB 에는 경유 정류장 좌표만 있어 버스 구간 지도선이 정류장끼리 직선으로 이어져
        건물을 뚫고 지나갔다(실증 2026-09-03). 형상은 하루 캐시한다."""
        body, err = self._get(("line", str(route_id)), ROUTE_LINE_PATH, ttl_sec=ROUTE_LINE_TTL_SEC,
                              routeId=route_id)
        if err or not body:
            return []
        return self._parse_route_line(body)

    def route_line_cached(self, route_id) -> list:
        """캐시에 있는 노선형상만 돌려준다(조회하지 않는다). 캐시에 없거나 형상이 비었으면 [].

        경로 후보를 비교하는 단계에서 쓴다 — 후보마다 형상을 조회하면 후보 수만큼 외부 호출이
        나가는데, 지도선은 최종 선택된 경로에만 필요하다.
        """
        hit, body = self._peek(("line", str(route_id)))
        if not hit or not body:
            return []
        return self._parse_route_line(body)

    @staticmethod
    def _parse_route_line(body: dict) -> list:
        """노선형상 응답 본문 → [[lat, lng], ...] (lineSeq 오름차순)."""
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
                d = haversine_m(lat, lng, a, b)
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
        return self._build_arrivals(body, err, station_id, route_id, route_meta)

    def arrivals_cached(self, station_id, route_id=None, route_meta: dict = None):
        """캐시에 있는 도착정보만으로 `arrivals()` 와 같은 모양의 성공 응답을 만든다(조회하지 않는다).

        캐시에 유효한 성공 응답이 없으면 None. 요청 단위 예산이 끝난 뒤에도 이미 받아 둔 응답은
        쓸 수 있게 하려는 것이다 — 저상 판정에 쓴 응답과 버스 구간에 붙이는 실시간 정보가 같은
        응답에서 나와야 서로 어긋나지 않는다.
        """
        hit, body = self._peek(("arr", str(station_id)))
        if not hit:
            return None
        return self._build_arrivals(body, None, station_id, route_id, route_meta)

    def _build_arrivals(self, body, err, station_id, route_id=None, route_meta: dict = None) -> dict:
        """도착정보 응답 본문(또는 오류) → `arrivals()` 반환 dict."""
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
        return self._build_locations(body, err, route_id, stop_index)

    def locations_cached(self, route_id, stop_index: dict = None):
        """캐시에 있는 위치정보만으로 `locations()` 와 같은 모양의 성공 응답을 만든다. 없으면 None."""
        hit, body = self._peek(("loc", str(route_id)))
        if not hit:
            return None
        return self._build_locations(body, None, route_id, stop_index)

    def _build_locations(self, body, err, route_id, stop_index: dict = None) -> dict:
        """위치정보 응답 본문(또는 오류) → `locations()` 반환 dict."""
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
