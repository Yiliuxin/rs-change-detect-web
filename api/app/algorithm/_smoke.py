# -*- coding: utf-8 -*-
"""算法模块1+2 headless 自测：读取 → 辐射定标 → 掩膜 → 输出 valid_ratio。

用法（两种均可，不依赖数据库与接口）：
    cd C:\\Test\\Web\\api         && python -m app.algorithm._smoke
    cd C:\\Test\\Web\\api\\app     && python algorithm\\_smoke.py

数据来源：C:\\Test\\Web\\data\\pair_* 三组真实 Sentinel-2 双时相影像对。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 支持以脚本方式直接运行：把 api/ 目录加入模块搜索路径
_HERE = Path(__file__).resolve()
_API_ROOT = _HERE.parents[2]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

import numpy as np  # noqa: E402

from app.algorithm.cog_reader import read_aoi  # noqa: E402
from app.algorithm.geotransform import BBox, validate_aoi_span  # noqa: E402
from app.algorithm.mask_pipeline import run_mask_pipeline  # noqa: E402
from app.algorithm.radiometric import to_reflectance  # noqa: E402
from app.config.constants import (  # noqa: E402
    AOI_MAX_SPAN_DEG,
    SENSOR_S2,
    VALID_RATIO_MIN,
    VALID_RATIO_WARN,
)

DATA_ROOT = Path(__file__).resolve().parents[3] / "data"


def _pick(scene_files, band: str) -> str:
    """在 local_files 中挑选指定波段文件（B02/B03/B04/B08/SCL）。"""
    hits = [f for f in scene_files if f.upper().endswith(f"_{band}.TIF")]
    if not hits:
        raise FileNotFoundError(f"未找到 {band} 波段：{scene_files}")
    return hits[0]


def run_group(pair_dir: Path) -> None:
    manifest = json.loads((pair_dir / "manifest.json").read_text(encoding="utf-8"))
    bbox = BBox.from_sequence(manifest["aoi_wgs84"])
    validate_aoi_span(bbox)

    print("=" * 74)
    print(f"组：{pair_dir.name}")
    print(f"AOI(WGS84)={bbox.as_tuple()}  跨度={bbox.span_x:.4f}°×{bbox.span_y:.4f}°"
          f"（上限 {AOI_MAX_SPAN_DEG}°）")
    print("-" * 74)

    for key, scene in manifest["scenes"].items():
        b04 = pair_dir / _pick(scene["local_files"], "B04")
        scl = pair_dir / _pick(scene["local_files"], "SCL")
        baseline = scene.get("processing_baseline")

        dn, profile, window = read_aoi(b04, bbox)
        nodata_value = profile.get("nodata")
        dn_nodata_mask = (dn == nodata_value) if nodata_value is not None else None

        rho = to_reflectance(dn, SENSOR_S2, proc_baseline=baseline)
        masks, result = run_mask_pipeline(scl, profile, dn_nodata_mask=dn_nodata_mask)

        valid = masks.valid
        rho_valid = rho[valid] if valid.any() else np.array([np.nan], dtype=np.float32)

        print(f"[{key}] 基线={baseline}  sensor={SENSOR_S2}")
        print(f"    读取  : {b04.name}  window={window}  数组={dn.shape} dtype={dn.dtype}")
        print(f"    定标  : rho=(DN-1000)/10000  有效区 ρ min={np.nanmin(rho_valid):.4f} "
              f"max={np.nanmax(rho_valid):.4f} mean={np.nanmean(rho_valid):.4f}")
        counts = masks.counts()
        print(f"    掩膜  : 总像元={counts['total']} 有效={counts['valid']} "
              f"云={counts['cloud']} 云影={counts['cloud_shadow']} "
              f"雪={counts['snow']} 无效={counts['invalid']}")
        print(f"    valid_ratio = {result.valid_ratio:.4f}  →  {result.code} "
              f"（terminate={result.terminate}, warn={result.warn}）{result.message}")
    print()


def main() -> int:
    pairs = sorted(p for p in DATA_ROOT.glob("pair_*") if (p / "manifest.json").exists())
    if not pairs:
        print(f"[error] 未找到影像对目录：{DATA_ROOT}")
        return 1

    print(f"阈值口径：VALID_RATIO_MIN={VALID_RATIO_MIN}（终止档）"
          f"  VALID_RATIO_WARN={VALID_RATIO_WARN}（告警可继续档）")
    for pair_dir in pairs:
        run_group(pair_dir)
    print("=" * 74)
    print("smoke 完成：读取 → 辐射定标 → 掩膜 → valid_ratio 全链路通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
