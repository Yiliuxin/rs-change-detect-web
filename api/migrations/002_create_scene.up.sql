-- 迁移 002：创建 scene 表（HLD 3.2 表 3-2，REQ-02/04）
-- 依赖：001_create_aoi（aoi_id 外键）
CREATE TABLE IF NOT EXISTS scene (
    scene_id            UUID           NOT NULL,
    aoi_id              UUID           NOT NULL,
    sensor              VARCHAR(8)     NOT NULL,
    acq_date            DATE           NOT NULL,
    cloud_cover         NUMERIC(5, 2)  NULL,
    cog_path            TEXT           NOT NULL,
    proc_baseline       VARCHAR(16)    NULL,
    radiometric_formula TEXT           NULL,
    -- 补充字段（HLD 3.2 表 3-2 未列，为支撑 ST_Intersects 空间相交检索与景号登记而加）
    tile                VARCHAR(16)    NULL,
    footprint           GEOMETRY(Polygon, 4326) NULL,
    created_at          TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT pk_scene PRIMARY KEY (scene_id),
    CONSTRAINT fk_scene_aoi FOREIGN KEY (aoi_id) REFERENCES aoi (aoi_id) ON DELETE CASCADE,
    -- 传感器枚举：Sentinel-2 / Landsat 8 / Landsat 9
    CONSTRAINT ck_scene_sensor CHECK (sensor IN ('S2', 'L8', 'L9'))
);

CREATE INDEX IF NOT EXISTS idx_scene_aoi   ON scene (aoi_id);
CREATE INDEX IF NOT EXISTS idx_scene_acq   ON scene (acq_date);
CREATE INDEX IF NOT EXISTS idx_scene_footprint ON scene USING GIST (footprint);

COMMENT ON TABLE  scene                IS '预置影像场景清单（REQ-02/04）';
COMMENT ON COLUMN scene.sensor         IS 'S2 / L8 / L9';
COMMENT ON COLUMN scene.cog_path       IS 'COG 对象存储路径（MinIO），形如 minio://scenes/pair_<name>/<tag>/';
COMMENT ON COLUMN scene.proc_baseline  IS '处理基线版次（S2 ≥04.00 / Landsat C2 L2），缺失或 <04.00 → EX9';
COMMENT ON COLUMN scene.radiometric_formula IS '按 sensor 锁定的辐射定标公式（NF5）';
COMMENT ON COLUMN scene.tile           IS 'MGRS 景号（补充字段），如 50RMT / 50RMS';
COMMENT ON COLUMN scene.footprint      IS '影像覆盖范围（补充字段），用于 ST_Intersects 检索';
