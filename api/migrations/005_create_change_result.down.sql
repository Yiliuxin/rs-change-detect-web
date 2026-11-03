-- 回滚 005：删除 change_result 表
DROP INDEX IF EXISTS idx_change_result_run;
DROP TABLE IF EXISTS change_result CASCADE;
