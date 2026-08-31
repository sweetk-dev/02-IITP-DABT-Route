-- 수집 장치화 (02-IITP-DABT-Route v1.16.0)
-- 실증 자체를 수집 장치로: 주행 GPS 트랙 + 접근성 오류 제보 + 그래프 오버라이드.
--
-- 개인정보 원칙
--   · 참여자 식별자는 어떤 테이블에도 저장하지 않는다 — route_id(익명 난수) 단위로만 묶인다.
--   · 원좌표 트랙은 분석 배치 후 보존 기한(실증 종료 후 3개월)이 지나면 삭제하고
--     단순화본(분석 리포트)만 남긴다.
--
-- 품질 개선 자동화 수위 (2026-08-27 사용자 확정)
--   1) 경고는 자동  — 제보 접수 즉시 '이용자 제보(미확인)' 경고 오버라이드 생성 → 안내에 노출
--   2) 속성 변경은 승인제 — 관리자가 제보를 확인(confirm/apply)해야 curb_cut 등 실제 속성 오버라이드 생성
--   3) 오버라이드는 좌표 앵커 — 노드/링크 ID 가 아니라 좌표+반경으로 저장해 그래프 재생성에도 살아남는다

-- ################################################
-- ## 주행 GPS 트랙
-- ################################################
CREATE TABLE IF NOT EXISTS mv_route_track_meta (
    route_id        VARCHAR(20) PRIMARY KEY,          -- /route/plan 이 발급한 익명 id
    profile         VARCHAR(30),
    network_version VARCHAR(40),
    planned_dist_m  INTEGER,                          -- 안내 경로 거리
    geometry        JSONB,                            -- 안내 경로선 [[lat,lng],...] (클라이언트 제공)
    point_cnt       INTEGER      NOT NULL DEFAULT 0,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    outcome         VARCHAR(12),                      -- arrived | canceled | unknown
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mv_route_track (
    route_id   VARCHAR(20) NOT NULL,
    seq        INTEGER     NOT NULL,
    lat        DOUBLE PRECISION NOT NULL,
    lon        DOUBLE PRECISION NOT NULL,
    ts         TIMESTAMPTZ,
    accuracy_m NUMERIC(6,1),
    PRIMARY KEY (route_id, seq)
);

COMMENT ON TABLE mv_route_track_meta IS '안내 세션 메타 — 참여자 식별자 없음(route_id 익명)';
COMMENT ON TABLE mv_route_track      IS '주행 GPS 트랙 점 — 분석 배치 후 보존 기한 지나면 삭제';

-- ################################################
-- ## 접근성 오류 제보
-- ################################################
CREATE TABLE IF NOT EXISTS mv_access_report (
    report_id   BIGSERIAL PRIMARY KEY,
    lat         DOUBLE PRECISION NOT NULL,
    lon         DOUBLE PRECISION NOT NULL,
    reason      VARCHAR(20) NOT NULL,                 -- curb | no_sidewalk | no_crossing | steep | blocked | etc
    detail      TEXT,
    route_id    VARCHAR(20),                          -- 제보 시점의 안내 세션 (선택)
    photo       BYTEA,                                -- 사진 1장 (선택, ≤2MB)
    photo_mime  VARCHAR(40),
    status      VARCHAR(12) NOT NULL DEFAULT 'new',   -- new | confirmed | rejected | applied
    review_note TEXT,
    reviewed_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mv_access_report_status ON mv_access_report (status, created_at);

COMMENT ON TABLE  mv_access_report IS '접근성 오류 제보 (안내 화면 원터치 버튼)';
COMMENT ON COLUMN mv_access_report.status IS 'new=미검토 / confirmed=사실 확인(경고 유지) / rejected=기각(경고 철회) / applied=속성 오버라이드 반영됨';

-- ################################################
-- ## 그래프 오버라이드 (좌표 앵커 — 재생성에도 생존)
-- ################################################
CREATE TABLE IF NOT EXISTS mv_access_override (
    override_id BIGSERIAL PRIMARY KEY,
    lat         DOUBLE PRECISION NOT NULL,
    lon         DOUBLE PRECISION NOT NULL,
    radius_m    NUMERIC(5,1) NOT NULL DEFAULT 20,
    target      VARCHAR(6)   NOT NULL DEFAULT 'link', -- link | node
    attr        VARCHAR(20)  NOT NULL,                -- warning | curb_cut | tactile_paving | width | passable
    value       VARCHAR(200) NOT NULL,                -- 경고문 / 'true'·'false' / 숫자
    source      VARCHAR(10)  NOT NULL DEFAULT 'report',  -- report | track | survey
    report_id   BIGINT REFERENCES mv_access_report(report_id),
    status      VARCHAR(10)  NOT NULL DEFAULT 'active',  -- active | retired
    note        TEXT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mv_access_override_status ON mv_access_override (status);

COMMENT ON TABLE  mv_access_override IS '그래프 로드 시 좌표 최근접 링크/노드에 적용되는 속성·경고 오버라이드';
COMMENT ON COLUMN mv_access_override.attr IS 'warning(자동 허용) / curb_cut·tactile_paving·width·passable(승인제 — 관리자 apply 로만 생성)';
