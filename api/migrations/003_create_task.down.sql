-- 回滚 003：删除 task 表
DROP INDEX IF EXISTS idx_task_status;
DROP INDEX IF EXISTS idx_task_aoi;
DROP TABLE IF EXISTS task CASCADE;
