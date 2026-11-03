-- 迁移 003：创建 task 表（HLD 3.2 表 3-5，REQ-13/14）
-- 依赖：001_create_aoi、002_create_scene
-- 说明：sample / change_result 均以 run_id 外键引用 task，故 task 先于二者创建。
CREATE TABLE IF NOT EXISTS task (
    run_id          UUID          NOT NULL,
    aoi_id          UUID          NOT NULL,
    scene_t1_id     UUID          NOT NULL,
    scene_t2_id     UUID          NOT NULL,
    params_snapshot JSONB         NOT NULL,
    commit_hash     VARCHAR(40)   NULL,
    status          VARCHAR(16)   NOT NULL DEFAULT 'PENDING',
    error_code      VARCHAR(16)   NULL,
    started_at      TIMESTAMPTZ   NULL,
    finished_at     TIMESTAMPTZ   NULL,
    CONSTRAINT pk_task PRIMARY KEY (run_id),
    CONSTRAINT fk_task_aoi   FOREIGN KEY (aoi_id)      REFERENCES aoi (aoi_id)     ON DELETE CASCADE,
    CONSTRAINT fk_task_t1    FOREIGN KEY (scene_t1_id) REFERENCES scene (scene_id) ON DELETE RESTRICT,
    CONSTRAINT fk_task_t2    FOREIGN KEY (scene_t2_id) REFERENCES scene (scene_id) ON DELETE RESTRICT,
    -- 状态流转由服务层显式写入（HLD 3.5 不使用触发器）
    CONSTRAINT ck_task_status CHECK (status IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED')),
    -- 时相须为两个不同场景
    CONSTRAINT ck_task_two_phases CHECK (scene_t1_id <> scene_t2_id)
);

CREATE INDEX IF NOT EXISTS idx_task_aoi    ON task (aoi_id);
CREATE INDEX IF NOT EXISTS idx_task_status ON task (status);

COMMENT ON TABLE  task                  IS '运行记录（REQ-13/14）';
COMMENT ON COLUMN task.run_id           IS '运行主键（可复现标识）';
COMMENT ON COLUMN task.params_snapshot  IS '参数快照：公式 / 版次 / 阈值 / 块大小';
COMMENT ON COLUMN task.commit_hash      IS '代码 commit（可复现三要素之一）';
COMMENT ON COLUMN task.error_code       IS '失败错误码（EX*/SYS-*，见 HLD 第 5 章）';
