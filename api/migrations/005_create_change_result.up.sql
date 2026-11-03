-- 迁移 005：创建 change_result 表（HLD 3.2 表 3-4，REQ-08/10/12）
-- 依赖：003_create_task（run_id 外键）
CREATE TABLE IF NOT EXISTS change_result (
    result_id    UUID             NOT NULL,
    run_id       UUID             NOT NULL,
    change_class SMALLINT         NOT NULL,
    pixel_count  BIGINT           NOT NULL,
    area_ha      NUMERIC(14, 4)   NOT NULL,
    raster_path  TEXT             NULL,
    png_path     TEXT             NULL,
    created_at   TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CONSTRAINT pk_change_result PRIMARY KEY (result_id),
    CONSTRAINT fk_change_result_task FOREIGN KEY (run_id) REFERENCES task (run_id) ON DELETE CASCADE,
    -- 变化类别：1 增强 / 2 退化 / 3 稳定
    CONSTRAINT ck_change_class CHECK (change_class IN (1, 2, 3)),
    CONSTRAINT ck_change_pixels CHECK (pixel_count >= 0),
    -- 同一运行同一类别仅一条统计记录
    CONSTRAINT uq_change_result_run_class UNIQUE (run_id, change_class)
);

CREATE INDEX IF NOT EXISTS idx_change_result_run ON change_result (run_id);

COMMENT ON TABLE  change_result              IS '变化结果（REQ-08/10/12）';
COMMENT ON COLUMN change_result.change_class  IS '1 增强 / 2 退化 / 3 稳定';
COMMENT ON COLUMN change_result.area_ha       IS '该类别面积（公顷），由像元数换算';
COMMENT ON COLUMN change_result.raster_path   IS 'GeoTIFF 结果路径（MinIO / 本地）';
COMMENT ON COLUMN change_result.png_path      IS '静态专题图 PNG 路径';
