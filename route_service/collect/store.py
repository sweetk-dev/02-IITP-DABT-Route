# -*- coding: utf-8 -*-
"""트랙·제보·오버라이드 저장소.

backend
  db     : POI_DB_DSN(iitp_db) 재사용. 스키마는 scripts/sql/collect_schema.sql
  memory : DSN 미설정·테스트용 인메모리 (프로세스 생존 동안만 유지)

개인정보: 참여자 식별자는 받지도 저장하지도 않는다. route_id(익명 난수) 단위.

품질 개선 자동화 수위 (사용자 확정 2026-08-27)
  1) 경고는 자동   — 제보 접수 즉시 warning 오버라이드 생성 ('이용자 제보(미확인)')
  2) 속성은 승인제 — 관리자 confirm/apply 를 거쳐야 curb_cut 등 실제 속성 오버라이드 생성
  3) 좌표 앵커     — 오버라이드는 노드/링크 ID 가 아닌 좌표+반경. 그래프 재생성에도 생존
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import re
import threading
from datetime import datetime, timezone

REASONS = {
    "curb": "턱 있음",
    "no_sidewalk": "보도 없음·끊김",
    "no_crossing": "횡단보도 없음",
    "steep": "경사 심함",
    "blocked": "통행 불가(공사 등)",
    "etc": "기타",
}
REPORT_STATUSES = ("new", "confirmed", "rejected", "applied")
OVERRIDE_ATTRS = ("warning", "curb_cut", "tactile_paving", "width", "passable")
# 승인제 속성 — 관리자 액션(apply)으로만 생성 가능
APPROVAL_ONLY_ATTRS = ("curb_cut", "tactile_paving", "width", "passable")
MAX_POINTS_PER_CALL = 5000
MAX_PHOTO_BYTES = 2 * 1024 * 1024
# DB 접속 타임아웃(초). 드라이버 기본값은 무제한이라, DB 호스트가 응답하지 않으면 운영체제 TCP 타임아웃
# (1~2분)까지 요청 처리 스레드가 묶인다. 같은 망 안의 DB 는 접속에 0.1초도 걸리지 않으므로 5초면
# 일시 지연은 넘기고 장애는 빨리 드러난다. 호출자(API)는 예외를 받아 503 으로 응답한다.
DB_CONNECT_TIMEOUT_S = 5

# 브라우저 FileReader.readAsDataURL 결과 앞머리: "data:image/jpeg;base64,"
_DATA_URL_PREFIX = re.compile(r"^data:[^;,]*(;[^;,]*)*;base64,", re.IGNORECASE)


def decode_photo(photo_b64: str) -> bytes:
    """제보 사진 base64 문자열을 바이트로 바꾼다. 잘못된 입력은 ValueError.

    · "data:image/...;base64," 접두(data URL)가 있으면 떼어 낸다. 접두를 둔 채 관대한 방식으로
      디코드하면 알파벳 밖 글자(':', ';', ',')가 조용히 버려지고 남은 글자가 본문과 이어 붙어
      깨진 바이트가 사진으로 저장된다.
    · 줄바꿈·공백은 제거한다(76자마다 줄을 나눈 base64 도 받는다).
    · validate=True 로 디코드한다 — base64 알파벳이 아닌 글자가 섞이면 버리지 않고 오류로 본다.
    · 크기는 MAX_PHOTO_BYTES 이하.
    호출자(API)는 ValueError 를 422 로 돌려준다.
    """
    s = _DATA_URL_PREFIX.sub("", photo_b64.strip(), count=1)
    s = "".join(s.split())
    try:
        photo = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("사진 데이터가 올바른 base64 가 아닙니다")
    if len(photo) > MAX_PHOTO_BYTES:
        raise ValueError("사진이 너무 큽니다 (2MB 이하)")
    return photo


def _connect_args(dsn: str) -> dict:
    """create_engine 에 넘길 connect_args. psycopg2(libpq) 드라이버일 때만 접속 타임아웃을 준다.

    connect_timeout 은 libpq 의 접속 파라미터다. DSN 이 "postgresql+psycopg2://" 이거나 드라이버를
    생략한 "postgresql://"(SQLAlchemy 기본 드라이버가 psycopg2)일 때만 넣는다 — 다른 드라이버에
    모르는 인자를 넘기면 접속 자체가 TypeError 로 실패한다.
    """
    scheme = (dsn or "").split("://", 1)[0].lower()
    if scheme in ("postgresql", "postgres", "postgresql+psycopg2"):
        return {"connect_timeout": DB_CONNECT_TIMEOUT_S}
    return {}


def warning_text(reason: str, confirmed: bool = False) -> str:
    label = REASONS.get(reason, REASONS["etc"])
    return "이용자 제보: %s%s" % (label, "" if confirmed else " (미확인)")


class CollectStore:
    def __init__(self, dsn: str = ""):
        self.dsn = dsn
        self.backend = "db" if dsn else "memory"
        self._engine = None
        self._lock = threading.Lock()
        # memory backend
        self._track_meta = {}
        self._track_points = {}       # route_id -> {seq: point}
        self._reports = {}            # report_id -> dict
        self._overrides = {}          # override_id -> dict
        self._next_report = 1
        self._next_override = 1

    # ---------- db helpers ----------
    def _db(self):
        if self._engine is None:
            from sqlalchemy import create_engine
            self._engine = create_engine(self.dsn, pool_pre_ping=True, future=True,
                                         connect_args=_connect_args(self.dsn))
        return self._engine

    @contextlib.contextmanager
    def _tx(self):
        """트랜잭션 하나를 연다. 블록이 정상 종료하면 커밋, 예외가 나면 롤백한다.

        한 연산이 여러 SQL 로 이뤄질 때(제보 + 경고 오버라이드, 오버라이드 철회 + 추가 + 상태 변경 등)
        이 블록 안에서 같은 conn 으로 실행해 전부 반영되거나 전부 취소되게 한다. SQL 마다 따로
        커밋하면 중간에 실패했을 때 '경고는 철회됐는데 제보 상태는 그대로' 같은 반쪽 상태가 남는다.
        """
        with self._db().begin() as conn:
            yield conn

    @staticmethod
    def _run(conn, sql: str, params=None, fetch: bool = False):
        """열려 있는 트랜잭션(conn)에서 SQL 하나를 실행한다.

        params 가 dict 의 리스트면 executemany 로 한 번에 실행된다(이때 fetch 는 쓰지 않는다).
        """
        from sqlalchemy import text
        res = conn.execute(text(sql), params if params is not None else {})
        if fetch:
            return [dict(r) for r in res.mappings().all()]
        return None

    def _exec(self, sql: str, params=None, fetch: bool = False, conn=None):
        """SQL 하나를 실행한다. conn 을 주면 그 트랜잭션 안에서, 없으면 단독 트랜잭션으로."""
        if conn is not None:
            return self._run(conn, sql, params, fetch)
        with self._tx() as c:
            return self._run(c, sql, params, fetch)

    # ---------- 트랙 ----------
    def log_track(self, route_id: str, points: list, meta: dict | None) -> int:
        points = points[:MAX_POINTS_PER_CALL]
        meta = meta or {}
        if self.backend == "memory":
            with self._lock:
                m = self._track_meta.setdefault(route_id, {"route_id": route_id, "point_cnt": 0})
                for k in ("profile", "network_version", "planned_dist_m", "geometry",
                          "outcome", "started_at", "finished_at"):
                    if meta.get(k) is not None:
                        m[k] = meta[k]
                bucket = self._track_points.setdefault(route_id, {})
                for p in points:
                    bucket.setdefault(int(p["seq"]), p)
                m["point_cnt"] = len(bucket)
                return len(bucket)

        # 메타 갱신 → 점 일괄 삽입 → 점 수 갱신을 한 트랜잭션으로 묶는다. 점마다 INSERT+커밋하면
        # 한 번 호출에 최대 MAX_POINTS_PER_CALL(5,000)번 커밋하게 되고, 중간에 실패하면 점 일부만
        # 들어간 채 point_cnt 가 갱신되지 않는다.
        with self._tx() as conn:
            return self._log_track_db(conn, route_id, points, meta)

    def _log_track_db(self, conn, route_id: str, points: list, meta: dict) -> int:
        """log_track 의 DB 경로 — conn 트랜잭션 안에서 실행한다. 반환은 저장된 총 점 수."""
        self._exec(
            """
            INSERT INTO mv_route_track_meta
                (route_id, profile, network_version, planned_dist_m, geometry,
                 started_at, finished_at, outcome)
            VALUES (:rid, :profile, :nv, :dist, CAST(:geom AS jsonb), :st, :ft, :outcome)
            ON CONFLICT (route_id) DO UPDATE SET
                profile         = COALESCE(EXCLUDED.profile, mv_route_track_meta.profile),
                network_version = COALESCE(EXCLUDED.network_version, mv_route_track_meta.network_version),
                planned_dist_m  = COALESCE(EXCLUDED.planned_dist_m, mv_route_track_meta.planned_dist_m),
                geometry        = COALESCE(EXCLUDED.geometry, mv_route_track_meta.geometry),
                started_at      = COALESCE(EXCLUDED.started_at, mv_route_track_meta.started_at),
                finished_at     = COALESCE(EXCLUDED.finished_at, mv_route_track_meta.finished_at),
                outcome         = COALESCE(EXCLUDED.outcome, mv_route_track_meta.outcome)
            """,
            {"rid": route_id, "profile": meta.get("profile"),
             "nv": meta.get("network_version"), "dist": meta.get("planned_dist_m"),
             "geom": __import__("json").dumps(meta["geometry"]) if meta.get("geometry") else None,
             "st": meta.get("started_at"), "ft": meta.get("finished_at"),
             "outcome": meta.get("outcome")},
            conn=conn,
        )
        if points:
            # 파라미터를 리스트로 넘기면 executemany — 문장 하나로 전 점을 넣는다(빈 리스트는 넘기지 않는다).
            self._exec(
                """
                INSERT INTO mv_route_track (route_id, seq, lat, lon, ts, accuracy_m)
                VALUES (:rid, :seq, :lat, :lon, :ts, :acc)
                ON CONFLICT (route_id, seq) DO NOTHING
                """,
                [{"rid": route_id, "seq": int(p["seq"]), "lat": float(p["lat"]),
                  "lon": float(p["lng"]), "ts": p.get("ts"), "acc": p.get("acc")} for p in points],
                conn=conn,
            )
        rows = self._exec(
            """
            UPDATE mv_route_track_meta m
               SET point_cnt = (SELECT count(*) FROM mv_route_track t WHERE t.route_id = :rid)
             WHERE m.route_id = :rid
            RETURNING m.point_cnt
            """,
            {"rid": route_id}, fetch=True, conn=conn)
        return int(rows[0]["point_cnt"]) if rows else 0

    # ---------- 제보 ----------
    def add_report(self, lat: float, lon: float, reason: str, detail: str | None,
                   route_id: str | None, photo_b64: str | None, photo_mime: str | None) -> dict:
        if reason not in REASONS:
            reason = "etc"
        photo = None
        if photo_b64:
            photo = decode_photo(photo_b64)      # data URL 접두 제거 + 엄격 디코드 + 크기 검사

        if self.backend == "memory":
            with self._lock:
                rid = self._next_report
                self._next_report += 1
                self._reports[rid] = {
                    "report_id": rid, "lat": lat, "lon": lon, "reason": reason,
                    "detail": detail, "route_id": route_id, "photo": photo,
                    "photo_mime": photo_mime, "status": "new", "review_note": None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
        else:
            # 제보 저장과 경고 오버라이드 생성을 한 트랜잭션으로 묶는다 — 따로 커밋하면 오버라이드
            # 생성이 실패했을 때 경고 없는 제보만 남는다(호출자에게는 실패로 응답했는데 기록은 남는다).
            with self._tx() as conn:
                rows = self._exec(
                    """
                    INSERT INTO mv_access_report (lat, lon, reason, detail, route_id, photo, photo_mime)
                    VALUES (:lat, :lon, :reason, :detail, :rid, :photo, :mime)
                    RETURNING report_id
                    """,
                    {"lat": lat, "lon": lon, "reason": reason, "detail": detail,
                     "rid": route_id, "photo": photo, "mime": photo_mime}, fetch=True, conn=conn)
                rid = int(rows[0]["report_id"])
                # 자동화 수위 1: 경고 오버라이드는 접수 즉시 생성 (미확인 표기)
                ov_id = self.add_override(lat, lon, attr="warning",
                                          value=warning_text(reason), source="report",
                                          report_id=rid, note="제보 접수 자동 생성", conn=conn)
            return {"report_id": rid, "override_id": ov_id}

        # 자동화 수위 1: 경고 오버라이드는 접수 즉시 생성 (미확인 표기)
        ov_id = self.add_override(lat, lon, attr="warning",
                                  value=warning_text(reason), source="report",
                                  report_id=rid, note="제보 접수 자동 생성")
        return {"report_id": rid, "override_id": ov_id}

    def list_reports(self, status: str | None = None, limit: int = 50, offset: int = 0) -> list:
        if self.backend == "memory":
            rows = sorted(self._reports.values(), key=lambda r: -r["report_id"])
            if status:
                rows = [r for r in rows if r["status"] == status]
            out = []
            for r in rows[offset:offset + limit]:
                d = {k: v for k, v in r.items() if k != "photo"}
                d["has_photo"] = r.get("photo") is not None
                out.append(d)
            return out
        return self._exec(
            """
            SELECT report_id, lat, lon, reason, detail, route_id, photo_mime,
                   (photo IS NOT NULL) AS has_photo, status, review_note,
                   reviewed_at, created_at
              FROM mv_access_report
             WHERE (:status IS NULL OR status = :status)
             ORDER BY report_id DESC
             LIMIT :limit OFFSET :offset
            """,
            {"status": status, "limit": limit, "offset": offset}, fetch=True)

    def get_report_photo(self, report_id: int):
        if self.backend == "memory":
            r = self._reports.get(report_id)
            return (r.get("photo"), r.get("photo_mime")) if r else (None, None)
        rows = self._exec(
            "SELECT photo, photo_mime FROM mv_access_report WHERE report_id = :id",
            {"id": report_id}, fetch=True)
        if not rows:
            return None, None
        return rows[0]["photo"], rows[0]["photo_mime"]

    def review_report(self, report_id: int, action: str, attr: str | None = None,
                      value: str | None = None, note: str | None = None,
                      radius_m: float = 20.0) -> dict:
        """관리자 검토 — confirm / reject / apply.

        confirm : 사실 확인. 경고 오버라이드를 확정 문구로 갱신(미확인 꼬리표 제거)
        reject  : 기각. 연결된 오버라이드 전부 철회
        apply   : 속성 반영(승인제). attr/value 로 속성 오버라이드 생성 + 경고는 철회
        """
        rep = self._get_report(report_id)
        if rep is None:
            raise KeyError("제보 %s 없음" % report_id)

        # 입력 검사는 DB 를 건드리기 전에 끝낸다(검사 순서·예외는 종전과 같다).
        if action not in ("confirm", "reject", "apply"):
            raise ValueError("action 은 confirm | reject | apply")
        if action == "apply":
            if attr not in APPROVAL_ONLY_ATTRS:
                raise ValueError("apply 가능한 속성: %s" % ", ".join(APPROVAL_ONLY_ATTRS))
            if value is None:
                raise ValueError("value 필요")

        if self.backend == "memory":
            self._review_steps(rep, report_id, action, attr, value, note, radius_m, None)
        else:
            # 오버라이드 철회 → 새 오버라이드 → 제보 상태 변경을 한 트랜잭션으로 묶는다. 따로 커밋하면
            # 중간 실패 시 '경고는 철회됐는데 확정 경고는 없고 상태는 new' 같은 반쪽 상태가 남는다.
            with self._tx() as conn:
                self._review_steps(rep, report_id, action, attr, value, note, radius_m, conn)
        return {"report_id": report_id, "action": action}

    def _review_steps(self, rep, report_id, action, attr, value, note, radius_m, conn):
        """review_report 의 실제 변경 단계. conn 이 있으면 그 트랜잭션 안에서 실행한다."""
        if action == "confirm":
            self._retire_report_overrides(report_id, conn=conn)
            self.add_override(rep["lat"], rep["lon"], attr="warning",
                              value=warning_text(rep["reason"], confirmed=True),
                              source="report", report_id=report_id, note=note or "관리자 확인",
                              conn=conn)
            self._set_report_status(report_id, "confirmed", note, conn=conn)
        elif action == "reject":
            self._retire_report_overrides(report_id, conn=conn)
            self._set_report_status(report_id, "rejected", note, conn=conn)
        elif action == "apply":
            self._retire_report_overrides(report_id, conn=conn)
            self.add_override(rep["lat"], rep["lon"], attr=attr, value=str(value),
                              source="report", report_id=report_id,
                              note=note or "관리자 속성 반영", radius_m=radius_m, conn=conn)
            self._set_report_status(report_id, "applied", note, conn=conn)

    def delete_report(self, report_id: int) -> dict:
        """제보 완전 삭제 (v1.17.0) — 오검·중복·시험 제보를 콘솔에서 지우기 위한 것.

        검토(reject)와 다르다. reject 는 '봤고 사실이 아니다'라는 기록을 남기지만,
        삭제는 기록 자체를 없앤다. 제보에서 파생된 오버라이드도 함께 지운다 —
        남겨 두면 출처 없는 경고가 그래프에 떠도는 데다, FK 때문에 삭제도 되지 않는다.
        """
        rep = self._get_report(report_id)
        if rep is None:
            raise KeyError("제보 %s 없음" % report_id)

        if self.backend == "memory":
            with self._lock:
                removed = [oid for oid, ov in self._overrides.items()
                           if ov.get("report_id") == report_id]
                for oid in removed:
                    del self._overrides[oid]
                self._reports.pop(report_id, None)
            return {"report_id": report_id, "deleted": True,
                    "deleted_overrides": len(removed)}

        # 파생 오버라이드 삭제와 제보 삭제를 한 트랜잭션으로 묶는다 — 따로 커밋하면 제보 삭제가
        # 실패했을 때 오버라이드만 사라진 제보가 남는다.
        with self._tx() as conn:
            rows = self._exec(
                "DELETE FROM mv_access_override WHERE report_id = :id RETURNING override_id",
                {"id": report_id}, fetch=True, conn=conn)
            self._exec("DELETE FROM mv_access_report WHERE report_id = :id", {"id": report_id},
                       conn=conn)
        return {"report_id": report_id, "deleted": True,
                "deleted_overrides": len(rows or [])}

    def _get_report(self, report_id: int):
        if self.backend == "memory":
            return self._reports.get(report_id)
        rows = self._exec(
            "SELECT report_id, lat, lon, reason, status FROM mv_access_report WHERE report_id = :id",
            {"id": report_id}, fetch=True)
        return rows[0] if rows else None

    def _set_report_status(self, report_id: int, status: str, note: str | None, conn=None):
        if self.backend == "memory":
            r = self._reports[report_id]
            r["status"] = status
            r["review_note"] = note
            r["reviewed_at"] = datetime.now(timezone.utc).isoformat()
            return
        self._exec(
            """
            UPDATE mv_access_report
               SET status = :status, review_note = :note, reviewed_at = now()
             WHERE report_id = :id
            """,
            {"status": status, "note": note, "id": report_id}, conn=conn)

    # ---------- 오버라이드 ----------
    def add_override(self, lat: float, lon: float, attr: str, value: str,
                     source: str = "report", report_id: int | None = None,
                     note: str | None = None, radius_m: float = 20.0,
                     target: str = "link", conn=None) -> int:
        """오버라이드 하나를 만든다. conn 을 주면 그 트랜잭션 안에서 실행한다(db 백엔드 전용 인자)."""
        if attr not in OVERRIDE_ATTRS:
            raise ValueError("attr 은 %s 중 하나" % ", ".join(OVERRIDE_ATTRS))
        if self.backend == "memory":
            with self._lock:
                oid = self._next_override
                self._next_override += 1
                self._overrides[oid] = {
                    "override_id": oid, "lat": lat, "lon": lon, "radius_m": radius_m,
                    "target": target, "attr": attr, "value": value, "source": source,
                    "report_id": report_id, "status": "active", "note": note,
                }
            return oid
        rows = self._exec(
            """
            INSERT INTO mv_access_override
                (lat, lon, radius_m, target, attr, value, source, report_id, note)
            VALUES (:lat, :lon, :radius, :target, :attr, :value, :source, :rid, :note)
            RETURNING override_id
            """,
            {"lat": lat, "lon": lon, "radius": radius_m, "target": target,
             "attr": attr, "value": value, "source": source, "rid": report_id,
             "note": note}, fetch=True, conn=conn)
        return int(rows[0]["override_id"])

    def _retire_report_overrides(self, report_id: int, conn=None):
        if self.backend == "memory":
            for ov in self._overrides.values():
                if ov.get("report_id") == report_id:
                    ov["status"] = "retired"
            return
        self._exec(
            "UPDATE mv_access_override SET status = 'retired' WHERE report_id = :id",
            {"id": report_id}, conn=conn)

    def active_overrides(self) -> list:
        if self.backend == "memory":
            return [dict(o) for o in self._overrides.values() if o["status"] == "active"]
        return self._exec(
            """
            SELECT override_id, lat, lon, radius_m, target, attr, value, source, report_id
              FROM mv_access_override WHERE status = 'active' ORDER BY override_id
            """, fetch=True)


STORE = CollectStore()


def configure(settings):
    global STORE
    STORE = CollectStore(dsn=settings.poi_db_dsn or "")
    return STORE
