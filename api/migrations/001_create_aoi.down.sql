-- 回滚 001：删除 aoi 表
DROP INDEX IF EXISTS idx_aoi_geom;
DROP TABLE IF EXISTS aoi CASCADE;
