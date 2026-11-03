-- 迁移 001：创建 aoi 表（HLD 3.2 表 3-1，REQ-01）
-- 前置：PostgreSQL 14 + PostGIS 3.x
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS aoi (
    aoi_id     UUID                        NOT NULL,
    aoi_geom   GEOMETRY(Polygon, 4326)     NOT NULL,
    area_deg2  NUMERIC(10, 6)              NOT NULL,
    created_at TIMESTAMPTZ                 NOT NULL DEFAULT now(),
    CONSTRAINT pk_aoi PRIMARY KEY (aoi_id),
    -- AOI 多边形必须合法闭合（WGS84）；跨度 ≤ 0.25°×0.25° 由服务层按 AOI_MAX_SPAN_DEG 判定（EX1）
    CONSTRAINT ck_aoi_geom_valid CHECK (ST_IsValid(aoi_geom)),
    CONSTRAINT ck_aoi_area_positive CHECK (area_deg2 > 0)
);

-- GIST 空间索引：支撑 ST_Intersects 检索预置影像对（REQ-02）
CREATE INDEX IF NOT EXISTS idx_aoi_geom ON aoi USING GIST (aoi_geom);

COMMENT ON TABLE  aoi            IS 'AOI 感兴趣区（REQ-01）';
COMMENT ON COLUMN aoi.aoi_geom   IS 'AOI 多边形（WGS84），跨度 ≤ 0.25°×0.25°';
COMMENT ON COLUMN aoi.area_deg2  IS 'AOI 面积（平方度），用于 EX1 校验';
