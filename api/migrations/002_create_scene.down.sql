-- 回滚 002：删除 scene 表
DROP INDEX IF EXISTS idx_scene_footprint;
DROP INDEX IF EXISTS idx_scene_acq;
DROP INDEX IF EXISTS idx_scene_aoi;
DROP TABLE IF EXISTS scene CASCADE;
