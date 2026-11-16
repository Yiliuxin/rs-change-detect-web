-- 迁移 007：检测参数模板表（批次 D，NF9 参数快照可复现的模板化）
-- 前置：006_create_users（user_id 外键指向 users，写法与既有迁移一致）
-- 幂等：CREATE TABLE IF NOT EXISTS / IF NOT EXISTS，重跑不报错；不手写 ALTER。
CREATE TABLE IF NOT EXISTS detect_template (
    id         UUID        NOT NULL,
    user_id    UUID        NOT NULL,
    name       VARCHAR(64) NOT NULL,
    params     JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_detect_template PRIMARY KEY (id),
    -- 模板归属用户：用户注销时模板随之删除（REG/登录扩展同一套 FK 口径）
    CONSTRAINT fk_detect_template_user
        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE,
    -- 同一用户内模板名唯一（重名由服务层以 TMPL-01(409) 报告，此处为兜底防线）
    CONSTRAINT uq_detect_template_user_name UNIQUE (user_id, name),
    -- 模板名不允许空串（服务层同步校验，双层防线）
    CONSTRAINT ck_detect_template_name CHECK (name <> '')
);

CREATE INDEX IF NOT EXISTS idx_detect_template_user_created
    ON detect_template (user_id, created_at DESC);

COMMENT ON TABLE  detect_template        IS '检测参数模板（口径面板参数白名单键集合，批次 D）';
COMMENT ON COLUMN detect_template.id         IS '模板主键（UUID）';
COMMENT ON COLUMN detect_template.user_id    IS '归属用户（users.user_id，按人隔离）';
COMMENT ON COLUMN detect_template.name       IS '模板名（同用户内唯一）';
COMMENT ON COLUMN detect_template.params     IS '口径参数快照：index/method/block_size/threshold（白名单外键已丢弃）';
