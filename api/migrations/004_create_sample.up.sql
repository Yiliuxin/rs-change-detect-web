-- 迁移 004：创建 sample 表（HLD 3.2 表 3-3，REQ-09）
-- 依赖：003_create_task（run_id 外键）
CREATE TABLE IF NOT EXISTS sample (
    sample_id  UUID                     NOT NULL,
    run_id     UUID                     NOT NULL,
    geom       GEOMETRY(Point, 4326)    NOT NULL,
    px_x       INTEGER                  NOT NULL,
    px_y       INTEGER                  NOT NULL,
    ref_class  VARCHAR(16)              NOT NULL,
    label      TEXT                     NULL,
    created_at TIMESTAMPTZ              NOT NULL DEFAULT now(),
    CONSTRAINT pk_sample PRIMARY KEY (sample_id),
    CONSTRAINT fk_sample_task FOREIGN KEY (run_id) REFERENCES task (run_id) ON DELETE CASCADE,
    -- 用户标注的参考类别（中文枚举，与 change_result.change_class 1/2/3 对应）
    CONSTRAINT ck_sample_ref_class CHECK (ref_class IN ('增强', '退化', '稳定')),
    CONSTRAINT ck_sample_px_nonneg CHECK (px_x >= 0 AND px_y >= 0)
);

CREATE INDEX IF NOT EXISTS idx_sample_run  ON sample (run_id);
CREATE INDEX IF NOT EXISTS idx_sample_geom ON sample USING GIST (geom);

COMMENT ON TABLE  sample            IS '验证样本点（REQ-09）';
COMMENT ON COLUMN sample.ref_class  IS '用户标注的参考类别：增强 / 退化 / 稳定';
COMMENT ON COLUMN sample.px_x       IS '像元列号';
COMMENT ON COLUMN sample.px_y       IS '像元行号';
