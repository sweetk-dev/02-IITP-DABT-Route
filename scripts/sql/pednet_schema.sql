-- 보행망 node/link (02-IITP-DABT-Route v1.8.0)
-- 원천: OSM 보행망 골격 + 수치지형도(1:1,000/1:5,000) 실측 폭·재질 보강
-- 명명: 기존 mv_poi 와 동일한 mv_ (mobility) 접두 규약

CREATE TABLE IF NOT EXISTS mv_pednet_node (
    node_id         VARCHAR(20)  NOT NULL,
    lat             DOUBLE PRECISION NOT NULL,
    lon             DOUBLE PRECISION NOT NULL,
    node_type       VARCHAR(20)  NOT NULL DEFAULT 'unknown',
    network_version VARCHAR(40)  NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (network_version, node_id)
);

CREATE TABLE IF NOT EXISTS mv_pednet_link (
    link_seq        BIGSERIAL PRIMARY KEY,
    f_node          VARCHAR(20)  NOT NULL,
    t_node          VARCHAR(20)  NOT NULL,
    length_m        NUMERIC(8,2) NOT NULL,
    link_type       VARCHAR(20)  NOT NULL DEFAULT 'unknown',  -- sidewalk/road/crossing/steps/overpass/underpass/ramp/elevator
    width_m         NUMERIC(5,2),           -- 수치지형도 실측 폭 (topo_source 있을 때)
    slope_pct       NUMERIC(6,3),           -- DEM 경사
    surface         VARCHAR(30),            -- 재질 (블록/아스콘 등)
    curb_cut        BOOLEAN,                -- 턱낮춤 (현재 원천 없음 — 안양시청 횡단보도 데이터 확보 시 채움)
    link_name       VARCHAR(100),
    topo_source     VARCHAR(10),            -- topo1k / topo5k / NULL(OSM 원본)
    network_version VARCHAR(40)  NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pednet_link_ver_type ON mv_pednet_link (network_version, link_type);
CREATE INDEX IF NOT EXISTS idx_pednet_link_fnode    ON mv_pednet_link (network_version, f_node);
CREATE INDEX IF NOT EXISTS idx_pednet_node_ver      ON mv_pednet_node (network_version);

COMMENT ON TABLE mv_pednet_node IS '안양 보행망 노드 (02-Route, OSM+수치지형도 하이브리드)';
COMMENT ON TABLE mv_pednet_link IS '안양 보행망 링크 — width_m/surface 는 수치지형도 실측(topo_source 참조)';
