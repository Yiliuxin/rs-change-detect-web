# -*- coding: utf-8 -*-
"""基础数据配置常量（唯一取值源）。

对应 HLD 3.7 表 3-6 / REQ-01~REQ-15。
规则：任何算法模块、路由模块、服务模块均须 ``from app.config.constants import ...`` 引用，
     禁止就地写死阈值或公式系数（REQ-04/NF5、REQ-05、REQ-14）。
"""
from __future__ import annotations

from typing import Callable, Dict, Final, Tuple

# ---------------------------------------------------------------------------
# REQ-01 AOI 校验
# ---------------------------------------------------------------------------
# AOI 最大跨度（度）；跨度 > 0.25°×0.25° 时前端拦截（EX1）。来源：REQ-01 / SRS 6.2 / HLD 3.7
AOI_MAX_SPAN_DEG: Final[float] = 0.25

# AOI 面积单位换算：1 平方度在赤道附近约 (111.32 km)^2，用于面积展示。来源：REQ-01 / REQ-11
DEG_TO_KM: Final[float] = 111.32

# ---------------------------------------------------------------------------
# REQ-05 有效像元占比（云/云影/雪/无效值掩膜后）
# ---------------------------------------------------------------------------
# 名实说明：名 MIN 但语义为**终止档**——0 < valid_ratio < 0.10 时触发 EX11，终止本轮检测、不产出结果。
# 来源：REQ-05 / EX11 / HLD 3.7
VALID_RATIO_MIN: Final[float] = 0.10

# 名实说明：名 WARN 但语义为**可继续档**——0.10 ≤ valid_ratio < 0.50 时触发 EX5 告警，
# 用户确认后允许继续，不终止。来源：REQ-05 / EX5 / HLD 3.7
VALID_RATIO_WARN: Final[float] = 0.50

# ---------------------------------------------------------------------------
# REQ-07 变化检测阈值
# ---------------------------------------------------------------------------
# Otsu 直方图退化（单峰/空直方图）时的回退固定阈值 ΔNDVI。来源：REQ-07 / HLD 3.7
OTSU_FALLBACK_DNDVI: Final[float] = 0.1

# ---------------------------------------------------------------------------
# REQ-03 / REQ-06 分块读写
# ---------------------------------------------------------------------------
# 默认分块尺寸（像素），控制峰值内存 < 4GB。来源：REQ-03 / REQ-06 / NF3 / HLD 3.7
BLOCK_SIZE: Final[int] = 256
# 允许的离散分块尺寸（HLD 3.7 记为 256/512）。来源：REQ-03 / NF3
BLOCK_SIZE_OPTIONS: Final[Tuple[int, ...]] = (256, 512)

# 单 AOI 最大波段数（红/绿/蓝/近红外）。来源：REQ-06 / SRS 6.2 / HLD 3.7
MAX_BANDS: Final[int] = 4
# 单 AOI 像元量上限（约 1.1 万² 像素）。来源：REQ-03 / SRS 6.2 / HLD 3.7
MAX_PIXELS: Final[int] = 11000 * 11000

# ---------------------------------------------------------------------------
# REQ-09 精度评价
# ---------------------------------------------------------------------------
# 精度评价每类最少样本点数，任一类不足 → EX10。来源：REQ-09 / HLD 3.7
MIN_SAMPLES_PER_CLASS: Final[int] = 30
# Kappa 教学级下限，低于则标注"教学级，非工程级"。来源：REQ-09 / HLD 3.7
KAPPA_TEACHING_MIN: Final[float] = 0.60

# ---------------------------------------------------------------------------
# REQ-13 并发与时效
# ---------------------------------------------------------------------------
# MVP 并发上限：同步阻塞不排队，超限 → EX7。来源：REQ-13 / EX7 / HLD 3.7
MAX_CONCURRENCY: Final[int] = 1
# 单 AOI 处理时长验收区间（秒）。来源：REQ-03 / NF1 / HLD 3.7
TIME_BUDGET_SEC: Final[Tuple[int, int]] = (30, 90)
# 非计算类接口响应上限（秒）。来源：REQ-01/02/09/12 / NF2 / HLD 3.7
API_TIMEOUT_SEC: Final[int] = 3

# ---------------------------------------------------------------------------
# REQ-04 辐射口径（S2 / Landsat 分公式，禁止互套 → EX8；基线不达标 → EX9）
# ---------------------------------------------------------------------------
# 传感器标识枚举（与 scene.sensor CHECK 一致）。来源：REQ-02 / REQ-04 / HLD 3.2 表 3-2
SENSOR_S2: Final[str] = "S2"
SENSOR_L8: Final[str] = "L8"
SENSOR_L9: Final[str] = "L9"
VALID_SENSORS: Final[Tuple[str, ...]] = (SENSOR_S2, SENSOR_L8, SENSOR_L9)

# Sentinel-2 处理基线最低版次（BOA_ADD_OFFSET = −1000 起）。来源：REQ-04 / SRS 6.1 / HLD 3.7
S2_BASELINE_MIN: Final[str] = "04.00"

# Sentinel-2 L2A 反射率公式。来源：REQ-04 / NF5 / HLD 3.7
#
# 规范式（ESA 原始 SAFE/JP2，DN 未应用 BOA_ADD_OFFSET）：ρ = (DN − 1000) / 10000
# 本项目 COG 数据源（AWS sentinel-s2-l2a-cogs）已在生产环节把 BOA_ADD_OFFSET 应用进 DN，
# 故实际取 ρ = DN / 10000。判定依据（实测，见 README「辐射口径」）：
#   1) 按规范式再减 1000，农田 t1 的 NIR/R 比值变负（1836 / −477），植被不可能；
#   2) 鄱阳湖枯水期 NDVI 中位数被压到 −0.92、25% 像元饱和到 ±1，与实况矛盾；
#   3) 改用 ρ = DN/10000 后两组 NDVI 零饱和，且季相/水情方向全部吻合。
# 若日后改用 ESA 原始 JP2（DN 含 offset），把 S2_DN_OFFSET_APPLIED 置 False 即可，其余代码无需改动。
S2_DN_OFFSET_APPLIED: Final[bool] = True
S2_FORMULA_ESA: Final[str] = "rho=(DN-1000)/10000"
S2_FORMULA: Final[str] = "rho=DN/10000" if S2_DN_OFFSET_APPLIED else S2_FORMULA_ESA
S2_DN_OFFSET: Final[float] = 1000.0
S2_DN_SCALE: Final[float] = 10000.0

# Landsat Collection 2 Level-2 反射率公式：ρ = 2.75e-5 * DN − 0.2。来源：REQ-04 / NF5 / HLD 3.7
LS_FORMULA: Final[str] = "rho=2.75e-5*DN-0.2"
LS_SCALE: Final[float] = 2.75e-5
LS_OFFSET: Final[float] = -0.2
LS_COLLECTION: Final[str] = "C2L2"


def s2_reflectance(dn):
    """Sentinel-2 L2A 反射率。来源：REQ-04 / NF5。

    DN 已应用 BOA_ADD_OFFSET 时 ρ = DN / 10000；否则回落到规范式 ρ = (DN − 1000) / 10000。
    由 ``S2_DN_OFFSET_APPLIED`` 统一开关，调用方无需感知数据源差异。
    """
    if S2_DN_OFFSET_APPLIED:
        return dn / S2_DN_SCALE
    return (dn - S2_DN_OFFSET) / S2_DN_SCALE


def landsat_c2_l2_reflectance(dn):
    """Landsat C2 L2：ρ = 2.75e-5 * DN − 0.2。来源：REQ-04 / NF5。"""
    return LS_SCALE * dn + LS_OFFSET


# 按 sensor 锁定的辐射定标公式表（NF5）：拿错 sensor 的公式即视为口径混用（EX8）。
# 来源：REQ-04 / NF5 / HLD 3.7
RADIOMETRIC_FORMULAS: Final[Dict[str, Dict[str, object]]] = {
    SENSOR_S2: {
        "formula_id": "S2_L2A_BOA",
        "text": S2_FORMULA,
        "func": s2_reflectance,  # type: ignore[dict-item]
        "baseline_min": S2_BASELINE_MIN,
    },
    SENSOR_L8: {
        "formula_id": "LS_C2L2_SR",
        "text": LS_FORMULA,
        "func": landsat_c2_l2_reflectance,  # type: ignore[dict-item]
        "collection": LS_COLLECTION,
    },
    SENSOR_L9: {
        "formula_id": "LS_C2L2_SR",
        "text": LS_FORMULA,
        "func": landsat_c2_l2_reflectance,  # type: ignore[dict-item]
        "collection": LS_COLLECTION,
    },
}


def radiometric_formula(sensor: str) -> Dict[str, object]:
    """按 sensor 取锁定公式；未知 sensor 视为口径不可判定（EX8/EX9 由调用方映射）。"""
    key = (sensor or "").strip().upper()
    if key not in RADIOMETRIC_FORMULAS:
        raise KeyError(f"未知 sensor：{sensor!r}，无法锁定辐射定标公式")
    return RADIOMETRIC_FORMULAS[key]


def radiometric_func(sensor: str) -> Callable:
    """按 sensor 取辐射定标函数（S2 与 Landsat 公式禁止互套）。"""
    return radiometric_formula(sensor)["func"]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# REQ-12 / REQ-15 导出与合规署名
# ---------------------------------------------------------------------------
# 导出格式白名单，其他格式 → EX6 拒绝。来源：REQ-12 / HLD 3.7
EXPORT_FORMATS: Final[Tuple[str, ...]] = ("geotiff", "png", "pdf")
# Copernicus / USGS 合规署名。来源：REQ-15 / NF6 / NF7 / HLD 3.7
ATTRIBUTION_S2: Final[str] = "Copernicus Sentinel data [{year}]"
ATTRIBUTION_S2_MODIFIED: Final[str] = "Contains modified Copernicus Sentinel data [{year}]"
ATTRIBUTION_LS: Final[str] = "Courtesy of USGS/NASA"
# 预置 COG 对象存储桶（本地 MinIO）。来源：REQ-02 / HLD 3.7
COG_BUCKET: Final[str] = "scenes"
# 结果产物桶（本地 MinIO）。来源：REQ-12 / HLD 3.2
RESULT_BUCKET: Final[str] = "results"
# 报告产物落盘目录。来源：REQ-12 / REQ-14 / HLD 3.7
REPORT_DIR: Final[str] = "./output/{run_id}/"

# ---------------------------------------------------------------------------
# 错误码（第 5 章 EX*/SYS-*）中文提示，供路由统一封装 {code, message, run_id}
# ---------------------------------------------------------------------------
# 来源：HLD 第 5 章出错处理设计
ERROR_MESSAGES: Final[Dict[str, str]] = {
    "EX1": "AOI 跨度超过 0.25°×0.25°，请缩小框选范围",
    "EX2": "无匹配影像对，请调整 AOI 或时相后重试",
    "EX3": "影像全云覆盖（有效像元占比为 0），本轮检测终止",
    "EX4": "双时相未配准，残差超容差，拒绝计算",
    "EX5": "有效像元占比偏低（0.10–0.50），确认后可继续",
    "EX6": "导出格式不支持，仅支持 GeoTIFF / PNG / PDF",
    "EX7": "并发超限，MVP 并发上限为 1，请稍后重试",
    "EX8": "辐射口径混用，双时相数据源不一致",
    "EX9": "处理基线版次不可判定或未达标（S2 需 ≥04.00）",
    "EX10": "样本数量不足，每类至少 30 个样本点",
    "EX11": "有效像元不足（占比 < 0.10），本轮检测终止",
    "SYS-01": "COG 读取失败",
    "SYS-02": "内存不足",
    "SYS-03": "专题图渲染失败",
    "SYS-04": "数据库连接失败",
    "SYS-05": "处理超时",
    # B 级扩展（在线取数）：STAC 检索不可达 / COG 逐波段下载重试后仍失败（REQ-02 在线分支）
    # 注：HTTP 映射由调用方显式传 http_status=500（errors.CODE_HTTP 未改动，5 接口契约不变）
    "SYS-06": "在线数据源不可达或影像下载失败，请稍后重试",
}
