-- 迁移 006：用户表 + task 归属用户（注册/登录，REQ-AUTH 扩展）
-- 前置：PostgreSQL 14；密码仅存 PBKDF2-HMAC-SHA256 哈希（绝不存明文）。
CREATE TABLE IF NOT EXISTS users (
    user_id      UUID          NOT NULL,
    username     VARCHAR(32)   NOT NULL,
    password_hash TEXT         NOT NULL,
    display_name VARCHAR(64),
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT pk_users PRIMARY KEY (user_id),
    CONSTRAINT uq_users_username UNIQUE (username),
    -- 用户名：3–20 位字母/数字/下划线（服务层同步校验，双层防线）
    CONSTRAINT ck_users_username_format
        CHECK (username ~ '^[A-Za-z0-9_]{3,20}$')
);

CREATE INDEX IF NOT EXISTS idx_users_created ON users (created_at);

-- task 增加归属用户（可空：历史遗留 run 由首个注册用户认领）
ALTER TABLE task
    ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users (user_id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_task_user ON task (user_id);

COMMENT ON TABLE users          IS '平台用户（注册/登录，PBKDF2 口令哈希）';
COMMENT ON COLUMN task.user_id  IS '检测任务归属用户（NULL=历史遗留，首个注册者认领）';
