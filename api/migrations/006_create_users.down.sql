-- 回滚 006：删除用户与 task 归属列
ALTER TABLE task DROP COLUMN IF EXISTS user_id;
DROP INDEX IF EXISTS idx_users_created;
DROP TABLE IF EXISTS users CASCADE;
