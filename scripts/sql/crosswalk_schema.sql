-- 안양시 횡단보도 원천 (02-IITP-DABT-Route v1.13.0)
-- 원천: data.go.kr 15042415 "경기도 안양시_공간정보시스템_횡단보도 현황" 좌표 포함본(2026-08-26 배포)
--       안양시 스마트도시정보과 공간정보시스템 발췌. 2,728건.
-- 명명: mv_ (mobility) 접두 규약 — mv_poi / mv_pednet_node / mv_pednet_link 와 동일 계열
--
-- ⚠️ 접근성 컬럼 결측 주의
--   보도턱낮춤여부·점자블록유무는 2,728건 중 175건(6.4%)만 기재돼 있고,
--   그중 56건은 폭·길이가 0인 미조사 더미행("기타")이다. 실질 유효는 119건(4.4%).
--   따라서 curb_cut / tactile_paving 의 NULL 은 "없음"이 아니라 **미상**이다.
--   라우팅 엔진(planner.edge_passable)은 curb_cut IS FALSE 일 때만 차단하고
--   NULL 은 통과시키되 안내에 "턱낮춤 미상"으로 노출한다. 이 계약을 바꾸지 말 것.

CREATE TABLE IF NOT EXISTS mv_crosswalk (
    src_version      VARCHAR(20)  NOT NULL,          -- 원천 배포일 (예: 20260826)
    mgmt_no          VARCHAR(20)  NOT NULL,          -- 관리번호 (안양시 자연키, 2024xxxxxx/2025xxxxxx)
    lat              DOUBLE PRECISION NOT NULL,
    lon              DOUBLE PRECISION NOT NULL,
    width_m          NUMERIC(6,2),                   -- 횡단보도폭 = 도색 띠 폭(보행 유효폭). 0 -> NULL
    length_m         NUMERIC(6,2),                   -- 횡단보도길이 = 횡단 거리(차도 폭). 0 -> NULL
    dong_name        VARCHAR(40),                    -- 관할지역(행정동). '경기도 안양시' = 동 미기재
    curb_cut         BOOLEAN,                        -- 보도턱낮춤: 있음=TRUE / 없음=FALSE / 기타·공란=NULL(미상)
    tactile_paving   BOOLEAN,                        -- 점자블록: 동일 규칙
    survey_flag      VARCHAR(10)  NOT NULL DEFAULT 'ok',  -- ok | dummy(폭·길이 0 = 원천 미조사행)
    src_name         VARCHAR(80)  NOT NULL DEFAULT '안양시 공간정보시스템 횡단보도 현황',
    match_link_type  VARCHAR(12),                    -- exist(기존 crossing 매칭) | new(신규 링크 생성) | point(지점 부착) | orphan
    match_node_id    VARCHAR(20),                    -- 부착된 보행망 노드 (point/new 인 경우)
    match_dist_m     NUMERIC(6,2),                   -- 부착 거리
    network_version  VARCHAR(40),                    -- 매칭 대상 보행망 버전
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (src_version, mgmt_no)
);

CREATE INDEX IF NOT EXISTS idx_mv_crosswalk_latlon  ON mv_crosswalk (lat, lon);
CREATE INDEX IF NOT EXISTS idx_mv_crosswalk_dong    ON mv_crosswalk (dong_name);
CREATE INDEX IF NOT EXISTS idx_mv_crosswalk_match   ON mv_crosswalk (network_version, match_node_id);

COMMENT ON TABLE  mv_crosswalk IS '안양시 횡단보도 원천 (data.go.kr 15042415, 좌표 포함본) — curb_cut/tactile_paving 의 NULL 은 미상';
COMMENT ON COLUMN mv_crosswalk.curb_cut       IS '보도턱낮춤 여부. NULL = 미상(원천 기재율 4.4%)';
COMMENT ON COLUMN mv_crosswalk.tactile_paving IS '점자블록 유무. NULL = 미상';
COMMENT ON COLUMN mv_crosswalk.survey_flag    IS 'dummy = 원천에서 폭·길이 0 으로 들어온 미조사행(56건)';


-- ################################################
-- ## mv_pednet_link 확장 — 횡단보도 원천 연계
-- ################################################
ALTER TABLE mv_pednet_link ADD COLUMN IF NOT EXISTS tactile_paving BOOLEAN;
ALTER TABLE mv_pednet_link ADD COLUMN IF NOT EXISTS cw_mgmt_no     VARCHAR(20);   -- mv_crosswalk.mgmt_no
ALTER TABLE mv_pednet_link ADD COLUMN IF NOT EXISTS attr_source    VARCHAR(20);   -- city_cw2026 | topo1k | topo5k | osm

COMMENT ON COLUMN mv_pednet_link.tactile_paving IS '점자블록 유무. NULL = 미상';
COMMENT ON COLUMN mv_pednet_link.cw_mgmt_no     IS '매칭된 안양시 횡단보도 관리번호 (mv_crosswalk)';
COMMENT ON COLUMN mv_pednet_link.attr_source    IS 'width_m/curb_cut 등 속성의 출처';


-- ################################################
-- ## mv_pednet_node 확장 — 횡단보도 지점 부착
-- ################################################
ALTER TABLE mv_pednet_node ADD COLUMN IF NOT EXISTS crosswalk_cnt  SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE mv_pednet_node ADD COLUMN IF NOT EXISTS cw_mgmt_nos    TEXT;          -- 쉼표구분 관리번호 목록

COMMENT ON COLUMN mv_pednet_node.crosswalk_cnt IS '이 노드에 부착된 안양시 횡단보도 개수 (안내 문구 생성용)';
