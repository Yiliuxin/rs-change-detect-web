-- 回滚 004：删除 sample 表
DROP INDEX IF EXISTS idx_sample_geom;
DROP INDEX IF EXISTS idx_sample_run;
DROP TABLE IF EXISTS sample CASCADE;
