# -*- coding: utf-8 -*-
"""报告产物落盘（HLD 模块4 功能二，REQ-12 / REQ-15）。

供后端 ``report_service`` 调用的 writer 层：GeoTIFF（rasterio，uint8 单波段
三类编码 + CRS/transform/nodata=0）、PNG（委托 renderer）、PDF（reportlab）。
除文件 IO 外不依赖数据库/路由；导出格式白名单 ``EXPORT_FORMATS`` 取自常量源，
非白名单抛 ``ExportFormatError``（EX6）。PDF 首页回显 valid_ratio 与四段阈值
提示；中文用 ``C:\\Windows\\Fonts\\msyh.ttc`` 注册 TTFont，失败退英文并说明。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import rasterio

from app.algorithm.change_detector import (
    CHANGE_CODES, CHANGE_LABELS_EN, CHANGE_NODATA,
)
from app.algorithm.mask_pipeline import judge_valid_ratio
from app.algorithm.renderer import render_change_png
from app.config.constants import (
    ATTRIBUTION_LS,
    ATTRIBUTION_S2,
    ATTRIBUTION_S2_MODIFIED,
    ERROR_MESSAGES,
    EXPORT_FORMATS,
    INDEX_RASTER_BAND1,
    INDEX_RASTER_BAND2,
    INDEX_RASTER_BANDS,
    INDEX_RASTER_COMPRESS,
    INDEX_RASTER_DTYPE,
    INDEX_RASTER_NODATA,
    INTENSITY_CODES,
    INTENSITY_K,
    INTENSITY_LABELS_EN,
    INTENSITY_NOTE,
    INTENSITY_NOTE_EN,
    LANDCOVER_LABELS_EN,
    LANDCOVER_NOTE,
    LANDCOVER_NOTE_EN,
    PATCH_PDF_TOP_N,
    VALID_RATIO_MIN,
    VALID_RATIO_WARN,
)

DEFAULT_FONT_PATH: str = r"C:\Windows\Fonts\msyh.ttc"
_CJK_FONT_NAME: str = "MSYH"

__all__ = [
    "ExportFormatError", "validate_export_formats", "valid_ratio_notice",
    "render_area_chart", "render_index_histogram", "render_intensity_chart",
    "attribution_lines", "write_geotiff", "write_geojson", "write_png",
    "write_pdf", "export_triplet", "write_index_raster",
]


class ExportFormatError(Exception):
    """导出格式不在 EXPORT_FORMATS 白名单 → EX6（REQ-12）。"""

    code = "EX6"


def validate_export_formats(formats: Sequence[str]) -> tuple[str, ...]:
    """校验并规范化导出格式；非白名单抛 EX6。"""
    normalized = tuple(str(f).strip().lower() for f in formats)
    bad = [f for f in normalized if f not in EXPORT_FORMATS]
    if bad:
        raise ExportFormatError(
            f"{ERROR_MESSAGES['EX6']}；不支持的格式：{bad}，白名单={list(EXPORT_FORMATS)}"
        )
    return normalized


def valid_ratio_notice(valid_ratio: float) -> Dict[str, object]:
    """四段阈值提示（唯一口径来自 mask_pipeline.judge_valid_ratio）。"""
    verdict = judge_valid_ratio(float(valid_ratio))
    zh = {
        "EX3": "EX3 影像全云覆盖（有效像元占比为 0），本轮检测终止",
        "EX11": "EX11 有效像元不足（占比 < 0.10），本轮检测终止",
        "EX5": "EX5 有效数据偏低，结论仅供参考（0.10–0.50，告警可继续）",
        "OK": "有效数据占比合格（≥ 0.50），结论可用",
    }
    en = {
        "EX3": "EX3: ALL CLOUD (VALID RATIO = 0), DETECTION TERMINATED",
        "EX11": "EX11: INSUFFICIENT VALID PIXELS (< 0.10), DETECTION TERMINATED",
        "EX5": "EX5: LOW VALID DATA - FOR REFERENCE ONLY (0.10-0.50, MAY CONTINUE)",
        "OK": "VALID DATA RATIO PASSED (>= 0.50), RESULT USABLE",
    }
    return {
        "valid_ratio": float(valid_ratio),
        "code": verdict.code,
        "terminate": verdict.terminate,
        "warn": verdict.warn,
        "message_zh": zh.get(verdict.code, verdict.message),
        "message_en": en.get(verdict.code, "VALID RATIO STATUS: " + verdict.code),
    }


def write_geotiff(change, profile: dict, output_path,
                  nodata: int = CHANGE_NODATA, compress: str = "deflate") -> str:
    """三类编码写为单波段 uint8 GeoTIFF，带 CRS / transform / nodata=0。"""
    arr = np.asarray(change, dtype=np.uint8)
    if arr.ndim != 2:
        raise ValueError(f"变化图需为二维数组，实际 {arr.shape}")
    src = dict(profile or {})
    out_profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "count": 1,
        "width": int(arr.shape[1]),
        "height": int(arr.shape[0]),
        "crs": src.get("crs"),
        "transform": src.get("transform"),
        "nodata": float(nodata),
        "compress": compress,
    }
    if out_profile["transform"] is None:
        raise ValueError("profile 缺少 transform，无法写出带地理参考的 GeoTIFF")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(str(path), "w", **out_profile) as dst:
        dst.write(arr, 1)
    return str(path.resolve())


def write_index_raster(primary, auxiliary, profile: dict, output_path) -> str:
    """B2 探针前置：双波段 float32 GeoTIFF（nodata=NaN，deflate）。

    band 1 = 主检测指数（NDVI 或 NDWI，与本轮 ``index`` 一致）；
    band 2 = 辅助指数（另一指数），供 A3 地物联合判据在只读 1 个像元时
    同时取得 NDVI 与 NDWI。无效像元恒为 NaN，与现有掩膜/定标口径一致。
    """
    p = np.asarray(primary, dtype=INDEX_RASTER_DTYPE)
    a = np.asarray(auxiliary, dtype=INDEX_RASTER_DTYPE)
    if p.ndim != 2 or a.shape != p.shape:
        raise ValueError(f"双时相指数栅格需同形二维数组：{p.shape} / {a.shape}")
    src = dict(profile or {})
    out_profile = {
        "driver": "GTiff",
        "dtype": INDEX_RASTER_DTYPE,
        "count": int(INDEX_RASTER_BANDS),
        "width": int(p.shape[1]),
        "height": int(p.shape[0]),
        "crs": src.get("crs"),
        "transform": src.get("transform"),
        "nodata": INDEX_RASTER_NODATA,
        "compress": INDEX_RASTER_COMPRESS,
    }
    if out_profile["transform"] is None:
        raise ValueError("profile 缺少 transform，无法写出带地理参考的指数栅格")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(str(path), "w", **out_profile) as dst:
        dst.write(p, 1)
        dst.write(a, 2)
        dst.set_band_description(1, INDEX_RASTER_BAND1)
        dst.set_band_description(2, INDEX_RASTER_BAND2)
    return str(path.resolve())


def write_png(change, valid_ratio: float, output_path, *,
              run_id: str = "", params_snapshot: Optional[dict] = None,
              otsu_threshold: Optional[float] = None,
              attribution_lines: Optional[Sequence[str]] = None) -> Dict[str, object]:
    """PNG 落盘（委托 renderer），返回渲染元数据（含文字退化说明）。"""
    return render_change_png(
        change, valid_ratio, output_path,
        run_id=run_id, params_snapshot=params_snapshot,
        otsu_threshold=otsu_threshold, attribution_lines=attribution_lines,
    )


def _resolve_year(report: dict) -> str:
    year = report.get("year")
    if year is None:
        date_str = str(report.get("t1_date") or report.get("date") or "")
        year = date_str[:4] if len(date_str) >= 4 else None
    if not year:
        raise ValueError("report 缺少 year / t1_date，无法确定合规署名年份（REQ-15）")
    return str(year)


def _register_cjk(font_path: str = DEFAULT_FONT_PATH):
    """注册微软雅黑；失败返回 Helvetica 与 False（英文退化）。"""
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        if _CJK_FONT_NAME in pdfmetrics.getRegisteredFontNames():
            return _CJK_FONT_NAME, True
        pdfmetrics.registerFont(TTFont(_CJK_FONT_NAME, font_path, subfontIndex=0))
        return _CJK_FONT_NAME, True
    except Exception:
        return "Helvetica", False


def attribution_lines(report: dict) -> List[str]:
    """REQ-15 / NF6 / NF7 合规署名，**逐字复用常量源文案**（不重写、不翻译）。

    Copernicus 两条带年份占位符，USGS 一条为固定串；PDF 署名页、PNG 与
    ``/report/export`` 响应共用此处唯一来源，避免三处措辞漂移。
    """
    year = _resolve_year(report)
    return [
        ATTRIBUTION_S2.format(year=year),
        ATTRIBUTION_S2_MODIFIED.format(year=year),
        ATTRIBUTION_LS,
    ]


def render_area_chart(stats, output_path) -> str:
    """REQ-12：matplotlib(Agg) 生成三类面积柱状图 PNG（无随机、无交互后端）。

    三类编码与顺序取自 ``change_detector.CHANGE_CODES``；``stats`` 缺类记 0，
    不伪造数值。返回的 PNG 既嵌入 PDF，也作为独立产物 ``area_chart.png`` 供前端复用。
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    by_code = {int(s.get("change_class", 0)): float(s.get("area_ha", 0.0))
               for s in (stats or [])}
    values = [by_code.get(int(code), 0.0) for code in CHANGE_CODES]
    labels = ["ENHANCED", "DEGRADED", "STABLE"]   # ASCII 标签，规避中文字体缺字警告
    colors = ["#2EA043", "#D63031", "#788CA0"]

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(6.4, 3.0), dpi=150)
    try:
        bars = ax.bar(labels, values, color=colors, width=0.55)
        ax.set_ylabel("Area (ha)")
        ax.set_title("Change class area (REQ-08 / REQ-11)")
        ax.grid(axis="y", linestyle=":", linewidth=0.6, alpha=0.6)
        ax.set_axisbelow(True)
        top = max(values) if values else 0.0
        ax.set_ylim(0.0, top * 1.18 if top > 0 else 1.0)
        for bar, value in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2.0, bar.get_height(),
                    f"{value:.2f}", ha="center", va="bottom", fontsize=8)
        fig.tight_layout()
        fig.savefig(str(path), format="png")
    finally:
        plt.close(fig)
    return str(path.resolve())


def render_index_histogram(hist: dict, output_path) -> str:
    """REQ-12 扩展：指数差值 / 回归残差分布直方图 PNG，红色虚线标检测阈值。

    ``hist`` 结构见 change_service._index_histogram：{name, counts, edges,
    threshold, valid_count}；缺键或缺数据时返回空串（调用方跳过，不伪造）。
    """
    counts = [int(c) for c in (hist.get("counts") or [])]
    edges = [float(e) for e in (hist.get("edges") or [])]
    if len(counts) < 2 or len(edges) != len(counts) + 1:
        return ""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    centers = [(edges[i] + edges[i + 1]) / 2.0 for i in range(len(counts))]
    width = (edges[-1] - edges[0]) / max(len(counts), 1) * 0.92
    thr = hist.get("threshold")
    name = str(hist.get("name") or "INDEX DELTA")

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(6.4, 3.0), dpi=150)
    try:
        ax.bar(centers, counts, width=width, color="#378ADD", alpha=0.85)
        if thr is not None:
            t = abs(float(thr))
            for v in (-t, t):
                ax.axvline(v, color="#D63031", linestyle="--", linewidth=1.2)
                ax.text(v, ax.get_ylim()[1] * 0.95, f" T={v:+.3f}",
                        color="#D63031", fontsize=8,
                        ha="left" if v >= 0 else "right", va="top")
        ax.set_xlabel(f"{name} value")
        ax.set_ylabel("Pixel count")
        ax.set_title(f"{name} distribution vs threshold")
        ax.grid(axis="y", linestyle=":", linewidth=0.6, alpha=0.6)
        ax.set_axisbelow(True)
        fig.tight_layout()
        fig.savefig(str(path), format="png")
    finally:
        plt.close(fig)
    return str(path.resolve())


def render_intensity_chart(intensity: dict, output_path) -> str:
    """A1：五档变化强度横向条形图 PNG（图内标签一律 ASCII）。

    ``intensity`` 结构见 ``algorithm.intensity.intensity_stats``；全 0 / 缺键
    时返回空串（调用方跳过，不伪造）。
    """
    counts = [int((intensity.get("counts") or {}).get(str(code), 0))
              for code in INTENSITY_CODES]
    if not any(counts):
        return ""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = [INTENSITY_LABELS_EN[int(code)] for code in INTENSITY_CODES]
    colors = ["#8E2C25", "#E4A23C", "#788CA0", "#67B86B", "#1E8A45"]
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(6.4, 3.0), dpi=150)
    try:
        pos = np.arange(len(labels))
        bars = ax.barh(pos, counts, color=colors, height=0.62)
        ax.set_yticks(pos)
        ax.set_yticklabels(labels, fontsize=7.5)
        ax.invert_yaxis()
        ax.set_xlabel("Pixel count")
        ax.set_title("Change intensity grades (K*T boundary)")
        ax.grid(axis="x", linestyle=":", linewidth=0.6, alpha=0.6)
        ax.set_axisbelow(True)
        top = max(counts) if counts else 0
        ax.set_xlim(0.0, top * 1.18 if top > 0 else 1.0)
        for bar, value in zip(bars, counts):
            ax.text(bar.get_width(), bar.get_y() + bar.get_height() / 2.0,
                    f" {value}", ha="left", va="center", fontsize=7.5)
        fig.tight_layout()
        fig.savefig(str(path), format="png")
    finally:
        plt.close(fig)
    return str(path.resolve())


def write_pdf(report: dict, output_path, font_path: str = DEFAULT_FONT_PATH) -> Dict[str, object]:
    """生成 PDF 报告（REQ-12 / REQ-15）。

    首页：valid_ratio 与四段阈值提示、Otsu、三类统计表 + 统计数字 ASCII 行、
    三类面积柱状图；method=regression 时追加差值法 / 回归法阈值与面积对比表；
    末页为独立**数据署名页**，逐字复用 NF6/NF7 常量中的 Copernicus / USGS 文案。
    页内容流不压缩，ASCII 行可直接字节检索，便于真链路抽验。
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as pdf_canvas
    from reportlab.platypus import Table, TableStyle

    valid_ratio = float(report.get("valid_ratio", 0.0))
    notice = valid_ratio_notice(valid_ratio)
    year = _resolve_year(report)
    font_name, cn_ok = _register_cjk(font_path)
    L = lambda zh, en: str(zh) if cn_ok else str(en)  # noqa: E731

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    page_w, page_h = A4
    # pageCompression=0：页内容流不压缩，便于验收方直接检索首页 VALID_RATIO /
    # 阈值提示 / 署名等文本；invariant=1：去除时间戳/随机文档 ID，保证同输入同字节
    pdf = pdf_canvas.Canvas(str(path), pagesize=A4,
                            pageCompression=0, invariant=1)
    pdf.setTitle(L("变化检测报告", "CHANGE DETECTION REPORT"))

    left = 40
    y = page_h - 48

    def _line(text, size=10.5, gap=17, color=(20, 20, 20)):
        nonlocal y
        pdf.setFillColorRGB(*(v / 255.0 for v in color))
        pdf.setFont(font_name, size)
        pdf.drawString(left, y, str(text))
        y -= gap

    def _ascii_line(text, size=8.5, gap=14, color=(90, 90, 90)):
        """强制 Helvetica 写纯 ASCII，保证 PDF 字节流里可直接检索数值 / 署名。"""
        nonlocal y
        pdf.setFillColorRGB(*(v / 255.0 for v in color))
        pdf.setFont("Helvetica", size)
        pdf.drawString(left, y, str(text))
        y -= gap

    def _new_page():
        nonlocal y
        pdf.showPage()
        y = page_h - 48

    def _ensure(space):
        if y - space < 40:
            _new_page()

    _line(L("变化检测专题报告 / CHANGE DETECTION REPORT", "CHANGE DETECTION REPORT"),
          size=15, gap=26)
    _line(f"RUN_ID: {report.get('run_id', '')}", gap=18)
    # —— 首页必回显：valid_ratio + 四段阈值提示（REQ-05 / EX3/EX5/EX11）——
    _line(f"VALID_RATIO = {valid_ratio:.4f}", size=12, gap=20, color=(0, 60, 130))
    notice_text = notice["message_zh"] if cn_ok else notice["message_en"]
    # 纯 ASCII 状态行：中文字体下混合串会被编码为字形十六进制，故另给一行
    # 可被直接检索的 EX3/EX5/EX11/OK 错误码与阈值提示（中文行供人阅读）
    _line(f"VALID_RATIO STATUS [{notice['code']}]: {notice['message_en']}",
          size=8.5, gap=15, color=(120, 30, 30))
    _line(L("阈值提示：", "THRESHOLD NOTICE: ") + notice_text,
          size=10.5, gap=18, color=(150, 30, 30))
    _line(L(
        f"阈值口径：=0 → EX3 终止；< {VALID_RATIO_MIN:.2f} → EX11 终止；"
        f"{VALID_RATIO_MIN:.2f}–{VALID_RATIO_WARN:.2f} → EX5 告警可继续；"
        f"≥ {VALID_RATIO_WARN:.2f} → 合格",
        f"SEGMENTS: =0 -> EX3; < {VALID_RATIO_MIN:.2f} -> EX11; "
        f"{VALID_RATIO_MIN:.2f}-{VALID_RATIO_WARN:.2f} -> EX5 WARN; "
        f">= {VALID_RATIO_WARN:.2f} -> PASS",
    ), size=9.5, gap=20, color=(90, 90, 90))

    otsu_t = report.get("otsu_threshold")
    otsu_fb = bool(report.get("otsu_fallback", False))
    otsu_reason = str(report.get("otsu_reason", ""))
    _line(L("Otsu 阈值：", "OTSU THRESHOLD: ") +
          (f"T = {float(otsu_t):.4f}" if otsu_t is not None else "N/A") +
          ("（回退固定阈值）" if cn_ok and otsu_fb else
           (" (FALLBACK)" if otsu_fb else ("（自适应）" if cn_ok else " (AUTO)"))))
    # 纯 ASCII 阈值行（同上，便于验收检索数值与是否回退）
    _line(f"OTSU_THRESHOLD T = {float(otsu_t):.4f}  FALLBACK = {otsu_fb}"
          if otsu_t is not None else "OTSU_THRESHOLD T = N/A",
          size=8.5, gap=15, color=(90, 90, 90))
    _line(L("Otsu 说明：", "OTSU REASON: ") + otsu_reason, size=9, gap=18, color=(90, 90, 90))
    _line(L("变化像元占比：", "CHANGE PIXEL RATIO: ") +
          f"{float(report.get('change_pixel_ratio', 0.0)):.4f}", gap=20)

    # —— 三类统计表 ——
    stats = report.get("stats") or []
    table_data = [
        [L("类别", "CLASS"), L("编码", "CODE"), L("像元数", "PIXEL COUNT"), L("面积(ha)", "AREA (ha)")],
    ]
    zh_names = {1: "增强", 2: "退化", 3: "稳定"}
    en_names = {1: "ENHANCED", 2: "DEGRADED", 3: "STABLE"}
    for item in stats:
        code = int(item.get("change_class", 0))
        table_data.append([
            L(zh_names.get(code, str(code)), en_names.get(code, str(code))),
            str(code),
            str(int(item.get("pixel_count", 0))),
            f"{float(item.get('area_ha', 0.0)):.4f}",
        ])
    table = Table(table_data, colWidths=[130, 70, 130, 130])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE6F1")),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    _, table_h = table.wrap(page_w - 2 * left, page_h)
    table.drawOn(pdf, left, y - table_h)
    y -= table_h + 22

    # 纯 ASCII 统计数字行：TTFont 表格单元格会被编码成字形十六进制，
    # 另给一行可直接字节检索的真实统计值（供真链路抽验，不伪造数值）
    st_map = {int(s.get("change_class", 0)): s for s in stats}
    _ascii_line("STATS " + " ".join(
        f"CLASS{code}:PIXELS={int(st_map.get(code, {}).get('pixel_count', 0))}"
        f":AREA_HA={float(st_map.get(code, {}).get('area_ha', 0.0)):.4f}"
        for code in CHANGE_CODES
    ))

    # —— 精度（EX10 时如实标注禁用）——
    acc = report.get("accuracy")
    if isinstance(acc, dict):
        _line(L("精度评价：", "ACCURACY: ") +
              f"OA={float(acc.get('overall_accuracy', 0.0)):.4f}  "
              f"KAPPA={float(acc.get('kappa', 0.0)):.4f}  "
              + (str(acc.get("note", "")) if cn_ok else
                 ("TEACHING LEVEL, NOT ENGINEERING GRADE" if acc.get("teaching_level") else "")),
              gap=18)
        _line(L("混淆矩阵(行=参考,列=预测)：", "CONFUSION MATRIX (ROWS=REF, COLS=PRED):"),
              size=9, gap=15, color=(90, 90, 90))
        for row in acc.get("confusion_matrix", []):
            _line("    " + "  ".join(f"{int(v):6d}" for v in row), size=9, gap=14,
                  color=(70, 70, 70))
        # 逐类生产者精度：键兼容中文类别名 / 数字编码两种来源
        pca = acc.get("per_class_accuracy") or {}
        if pca:
            zh_names_acc = {1: "增强", 2: "退化", 3: "稳定"}
            parts = []
            for code, zh in zh_names_acc.items():
                v = pca.get(zh, pca.get(str(code), pca.get(code)))
                if v is not None:
                    parts.append(f"{zh}={float(v) * 100:.1f}%")
            if parts:
                _line(L("逐类精度（生产者）：", "PER-CLASS ACCURACY (PRODUCER): ")
                      + "  ".join(parts), size=9, gap=15, color=(70, 70, 70))
    else:
        _line(L("精度评价：样本不足（EX10），禁用「计算精度」",
                "ACCURACY: SAMPLES INSUFFICIENT (EX10), ACCURACY DISABLED"),
              gap=18, color=(150, 30, 30))

    # —— 三类面积柱状图（matplotlib PNG → reportlab 内嵌，REQ-12）——
    y -= 4
    _ensure(215)
    _line(L("三类面积柱状图 / AREA CHART", "AREA CHART"), size=10, gap=16)
    _ascii_line("AREA CHART: ENHANCED / DEGRADED / STABLE (ha)", gap=14)
    chart_path = render_area_chart(stats, path.parent / "area_chart.png")
    chart_w, chart_h = 400.0, 187.5
    pdf.drawImage(chart_path, left, y - chart_h, width=chart_w, height=chart_h)
    y -= chart_h + 20

    # —— 指数差值 / 回归残差分布直方图（REQ-12 扩展，佐证阈值合理性）——
    hist = report.get("index_histogram")
    hist_path = ""
    if isinstance(hist, dict) and hist.get("counts"):
        _ensure(215)
        _line(L("指数分布直方图 / INDEX HISTOGRAM", "INDEX HISTOGRAM"),
              size=10, gap=16)
        _ascii_line(f"INDEX HISTOGRAM: {hist.get('name', 'INDEX DELTA')} "
                    f"vs THRESHOLD (dashed red)", gap=14)
        hist_path = render_index_histogram(hist, path.parent / "index_histogram.png")
        if hist_path:
            pdf.drawImage(hist_path, left, y - chart_h, width=chart_w, height=chart_h)
            y -= chart_h + 20

    # —— method=regression：差值法 / 回归法 阈值与面积对比表（REQ-11）——
    comparison = report.get("comparison")
    comparison_used = False
    if isinstance(comparison, dict) and {"difference", "regression"} <= set(comparison):
        comparison_used = True
        _ensure(190)
        _line(L("差值法 / 回归法 阈值与面积对比", "DIFFERENCE vs REGRESSION COMPARISON"),
              size=10, gap=16)
        cmp_rows = [["METHOD", "THRESHOLD T", "SOURCE",
                     "ENHANCED ha", "DEGRADED ha", "STABLE ha"]]
        for key, label in (("difference", "DIFF_OTSU"), ("regression", "REGRESSION")):
            item = comparison[key]
            areas = {int(s.get("change_class", 0)): float(s.get("area_ha", 0.0))
                     for s in (item.get("stats") or [])}
            cmp_rows.append([
                label,
                f"{float(item.get('threshold', 0.0)):.4f}",
                str(item.get("threshold_source", "")),
                *[f"{areas.get(int(code), 0.0):.4f}" for code in CHANGE_CODES],
            ])
        cmp_table = Table(cmp_rows, colWidths=[95, 85, 65, 85, 85, 85])
        cmp_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE6F1")),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        _, cmp_h = cmp_table.wrap(page_w - 2 * left, page_h)
        cmp_table.drawOn(pdf, left, y - cmp_h)
        y -= cmp_h + 16
        diff_item, reg_item = comparison["difference"], comparison["regression"]
        diff_area = sum(float(s.get("area_ha", 0.0)) for s in (diff_item.get("stats") or []))
        reg_area = sum(float(s.get("area_ha", 0.0)) for s in (reg_item.get("stats") or []))
        _ascii_line(
            f"METHOD COMPARISON DIFF_T={float(diff_item.get('threshold', 0.0)):.4f} "
            f"REG_T={float(reg_item.get('threshold', 0.0)):.4f} "
            f"DIFF_TOTAL_AREA_HA={diff_area:.4f} REG_TOTAL_AREA_HA={reg_area:.4f}"
        )

    # —— A1 变化强度分级（表 + 横向条形图，图内标签 ASCII；经验阈值标注）——
    intensity = report.get("intensity")
    intensity_chart_path = ""
    intensity_table_used = False
    if isinstance(intensity, dict) and intensity.get("levels"):
        intensity_table_used = True
        _ensure(200)
        _line(L("变化强度分级 / CHANGE INTENSITY GRADES", "CHANGE INTENSITY GRADES"),
              size=10, gap=16)
        _ascii_line(
            f"INTENSITY K={float(intensity.get('k', INTENSITY_K)):.2f} "
            f"T={float(intensity.get('threshold', 0.0)):.4f} "
            f"BOUNDARY_K_T={float(intensity.get('boundary', 0.0)):.4f}", gap=14)
        int_rows = [[L("档位", "GRADE"), L("编码", "CODE"),
                     L("像元数", "PIXEL COUNT"), L("占比", "RATIO")]]
        for lv in intensity["levels"]:
            int_rows.append([
                L(str(lv.get("label", "")), str(lv.get("label_en", ""))),
                str(int(lv.get("code", 0))),
                str(int(lv.get("pixel_count", 0))),
                f"{float(lv.get('ratio', 0.0)):.4f}",
            ])
        int_table = Table(int_rows, colWidths=[150, 60, 140, 110])
        int_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE6F1")),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        _, int_h = int_table.wrap(page_w - 2 * left, page_h)
        int_table.drawOn(pdf, left, y - int_h)
        y -= int_h + 14
        counts_map = intensity.get("counts") or {}
        _ascii_line("INTENSITY " + " ".join(
            f"G{code}:PIXELS={int(counts_map.get(str(code), 0))}"
            for code in INTENSITY_CODES), gap=14)
        # 硬约束 4：经验阈值在 PDF 逐字标注（中文行 + 可检索 ASCII 行）
        _line(L(INTENSITY_NOTE, INTENSITY_NOTE_EN), size=9, gap=14,
              color=(150, 30, 30))
        _ascii_line(INTENSITY_NOTE_EN, gap=14)
        intensity_chart_path = render_intensity_chart(
            intensity, path.parent / "intensity_chart.png")
        if intensity_chart_path:
            _ensure(215)
            _ascii_line("INTENSITY BAR CHART: STRONG_DEGRADE / MILD_DEGRADE / "
                        "STABLE / MILD_IMPROVE / STRONG_IMPROVE", gap=14)
            pdf.drawImage(intensity_chart_path, left, y - chart_h,
                          width=chart_w, height=chart_h)
            y -= chart_h + 20

    # —— A2 变化图斑统计（指标表 + 面积 Top5 表）——
    patch = report.get("patch_stats")
    patch_table_used = False
    if isinstance(patch, dict):
        patch_table_used = True
        _ensure(200)
        _line(L("变化图斑统计 / CHANGE PATCH STATISTICS", "CHANGE PATCH STATISTICS"),
              size=10, gap=16)
        if bool(patch.get("skipped")):
            _line(L("已跳过：", "SKIPPED: ") + str(patch.get("reason", "")),
                  size=9, gap=16, color=(150, 30, 30))
            _ascii_line("PATCH STATS SKIPPED: " + str(patch.get("reason", "")), gap=14)
        else:
            metric_rows = [
                [L("指标", "METRIC"), L("数值", "VALUE")],
                [L("斑块数", "TOTAL PATCHES"), str(int(patch.get("total_patches", 0)))],
                [L("总面积 (ha)", "TOTAL AREA (ha)"),
                 f"{float(patch.get('total_area_ha', 0.0)):.4f}"],
                [L("平均斑块面积 (ha)", "MEAN AREA (ha)"),
                 f"{float(patch.get('mean_area_ha', 0.0)):.4f}"],
                [L("中位斑块面积 (ha)", "MEDIAN AREA (ha)"),
                 f"{float(patch.get('median_area_ha', 0.0)):.4f}"],
                [L("最大斑块面积 (ha)", "MAX AREA (ha)"),
                 f"{float(patch.get('max_area_ha', 0.0)):.4f}"],
                [L("斑块密度 (个/km²)", "PATCH DENSITY (/km2)"),
                 f"{float(patch.get('patch_density_per_km2', 0.0)):.4f}"],
            ]
            metric_table = Table(metric_rows, colWidths=[220, 160])
            metric_table.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), font_name),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE6F1")),
                ("ALIGN", (1, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            _, metric_h = metric_table.wrap(page_w - 2 * left, page_h)
            metric_table.drawOn(pdf, left, y - metric_h)
            y -= metric_h + 12
            _ascii_line(
                f"PATCH STATS TOTAL={int(patch.get('total_patches', 0))} "
                f"AREA_HA={float(patch.get('total_area_ha', 0.0)):.4f} "
                f"MAX_HA={float(patch.get('max_area_ha', 0.0)):.4f}", gap=14)
            top_rows = [["#", "CLASS", "AREA (ha)", "CENTROID LNG", "CENTROID LAT"]]
            for item in (patch.get("top") or [])[:int(PATCH_PDF_TOP_N)]:
                lng, lat = item.get("centroid_lng"), item.get("centroid_lat")
                top_rows.append([
                    str(int(item.get("rank", 0))),
                    CHANGE_LABELS_EN.get(int(item.get("change_class", 0)), "NODATA"),
                    f"{float(item.get('area_ha', 0.0)):.4f}",
                    "—" if lng is None else f"{float(lng):.6f}",
                    "—" if lat is None else f"{float(lat):.6f}",
                ])
            if len(top_rows) > 1:
                _ensure(120)
                _line(L(f"面积 Top{int(PATCH_PDF_TOP_N)} 图斑（含质心经纬度）",
                        f"TOP {int(PATCH_PDF_TOP_N)} PATCHES BY AREA"),
                      size=9, gap=14)
                top_table = Table(top_rows, colWidths=[35, 95, 95, 115, 115])
                top_table.setStyle(TableStyle([
                    ("FONTNAME", (0, 0), (-1, -1), font_name),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE6F1")),
                    ("ALIGN", (1, 0), (-1, -1), "CENTER"),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]))
                _, top_h = top_table.wrap(page_w - 2 * left, page_h)
                top_table.drawOn(pdf, left, y - top_h)
                y -= top_h + 16

    # —— A3 地物转移矩阵（4×4 表 + 经验阈值说明行）——
    trans = report.get("transition_matrix")
    transition_table_used = False
    if isinstance(trans, dict) and trans.get("pixel_matrix"):
        transition_table_used = True
        _ensure(210)
        _line(L("地物转移矩阵 / LANDCOVER TRANSITION MATRIX",
                "LANDCOVER TRANSITION MATRIX"), size=10, gap=16)
        labels_en = trans.get("labels_en") or LANDCOVER_LABELS_EN
        codes = [int(c) for c in (trans.get("codes") or [1, 2, 3, 4])]
        short = lambda c: str(labels_en.get(str(c), c)).split("/")[0][:6]
        matrix = trans["pixel_matrix"]
        tr_rows = [[L("t1 / t2", "t1 / t2")] + [short(c) for c in codes]]
        for i, c in enumerate(codes):
            row = (matrix[i] if i < len(matrix) else [0] * len(codes))
            tr_rows.append([short(c)] + [str(int(x)) for x in row])
        tr_table = Table(tr_rows, colWidths=[96, 82, 82, 82, 82])
        tr_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE6F1")),
            ("BACKGROUND", (1, 1), (1, 1), colors.HexColor("#E9F3E9")),
            ("BACKGROUND", (2, 2), (2, 2), colors.HexColor("#E9F3E9")),
            ("BACKGROUND", (3, 3), (3, 3), colors.HexColor("#E9F3E9")),
            ("BACKGROUND", (4, 4), (4, 4), colors.HexColor("#E9F3E9")),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        _, tr_h = tr_table.wrap(page_w - 2 * left, page_h)
        tr_table.drawOn(pdf, left, y - tr_h)
        y -= tr_h + 12
        _ascii_line("TRANSITION MATRIX " + " ".join(
            "ROW" + str(i + 1) + ":" + ",".join(str(int(x)) for x in
                                                (matrix[i] if i < len(matrix)
                                                 else [0] * len(codes)))
            for i in range(len(codes))), gap=14)
        _ascii_line(
            f"UNCHANGED RATIO={float(trans.get('unchanged_ratio', 0.0)):.6f} "
            f"VALID={int(trans.get('valid_count', 0))} "
            f"ROW_TOTALS={','.join(str(int(x)) for x in (trans.get('row_totals') or []))} "
            f"COL_TOTALS={','.join(str(int(x)) for x in (trans.get('col_totals') or []))}",
            gap=14)
        # 硬约束 4：经验阈值在 PDF 逐字标注（中文行 + 可检索 ASCII 行）
        _line(L(LANDCOVER_NOTE, LANDCOVER_NOTE_EN), size=9, gap=14,
              color=(150, 30, 30))
        _ascii_line(LANDCOVER_NOTE_EN, gap=14)

    # —— 独立数据署名页（REQ-15 / NF6 / NF7）：逐字复用常量源文案 ——
    _new_page()
    _line(L("数据署名 / DATA ATTRIBUTION", "DATA ATTRIBUTION"), size=15, gap=26)
    _ascii_line("DATA ATTRIBUTION / NF6 NF7 VERBATIM", gap=18)
    lines = attribution_lines(report)
    for text in lines:
        _ascii_line(text, size=10, gap=18, color=(40, 40, 40))
    _ascii_line("SOURCE: Copernicus Sentinel / USGS NASA Landsat Collection 2 Level-2",
                size=8.5, gap=20, color=(90, 90, 90))
    _line(L("以上署名逐字取自系统常量（NF6 / NF7），未经改写。",
            "Attribution texts are verbatim from NF6 / NF7 constants."),
          size=9, gap=22, color=(90, 90, 90))

    # —— 参数快照（REQ-14，确定性，不含时间戳）——
    snapshot = report.get("params_snapshot") or {}
    raw = json.dumps(snapshot, ensure_ascii=not cn_ok, sort_keys=True, default=str)
    _ensure(40)
    _line(L("参数快照 / PARAMS SNAPSHOT:", "PARAMS SNAPSHOT:"),
          size=9, gap=15, color=(90, 90, 90))
    for i in range(0, len(raw), 95):
        if y < 40:
            _new_page()
        _line(raw[i:i + 95], size=7.5, gap=11, color=(110, 110, 110))

    pdf.showPage()
    pdf.save()
    return {
        "path": str(path.resolve()),
        "valid_ratio": valid_ratio,
        "code": notice["code"],
        "font": font_name,
        "font_mode": "ttf-msyh" if cn_ok else "helvetica",
        "degraded": not cn_ok,
        "reason": "" if cn_ok else "reportlab 中文字体注册失败，PDF 退化为纯英文并如实说明",
        "attribution": lines,
        "area_chart": chart_path,
        "index_histogram": hist_path,
        "comparison_table": comparison_used,
        "intensity_chart": intensity_chart_path,
        "intensity_table": intensity_table_used,
        "patch_table": patch_table_used,
        "transition_table": transition_table_used,
    }


def write_geojson(change, profile: dict, output_path) -> Dict[str, object]:
    """变化图斑导出 GeoJSON（RFC 7946 / WGS84）。

    仅导出 1 增强 / 2 退化两类图斑（稳定为背景、面积占绝对主导，导出只会
    撑爆文件且无分析价值）。像元几何经 ``rasterio.features.shapes`` 合并成
    多边形（transform 直接落在栅格 CRS），坐标再统一重投影 EPSG:4326。
    返回 ``{path, feature_count}``；无变化图斑时写空 FeatureCollection。
    """
    from rasterio.features import shapes as feature_shapes
    from rasterio.warp import transform as warp_transform

    arr = np.asarray(change)
    mask = np.isin(arr, (1, 2))
    src_crs = profile.get("crs") or "EPSG:32649"
    features: List[dict] = []
    for geom, value in feature_shapes(arr, mask=mask,
                                      transform=profile.get("transform")):
        code = int(value)
        rings: List[list] = []
        for ring in geom["coordinates"]:
            xs = [float(p[0]) for p in ring]
            ys = [float(p[1]) for p in ring]
            txs, tys = warp_transform(src_crs, "EPSG:4326", xs, ys)
            rings.append([[float(x), float(y)] for x, y in zip(txs, tys)])
        features.append({
            "type": "Feature",
            "properties": {
                "change_class": code,
                "class_zh": "增强" if code == 1 else "退化",
            },
            "geometry": {"type": "Polygon", "coordinates": rings},
        })
    fc = {"type": "FeatureCollection", "features": features}
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    return {"path": str(path.resolve()), "feature_count": len(features)}


def export_triplet(
    change,
    profile: dict,
    valid_ratio: float,
    report: dict,
    output_dir,
    formats: Sequence[str] = EXPORT_FORMATS,
    run_id: str = "",
    params_snapshot: Optional[dict] = None,
    names: Optional[Dict[str, str]] = None,
) -> Dict[str, object]:
    """一次性导出 GeoTIFF / PNG / PDF / GeoJSON 四件套。

    缺省文件名 ``change.tif`` / ``change.png`` / ``report.pdf`` /
    ``change.geojson``；``names`` 可按 ``{geotiff, png, pdf, geojson}`` 覆盖
    文件名（REQ-11 回归法用 ``_reg`` 后缀隔离，缺省调用行为完全不变）。
    """
    allowed = validate_export_formats(formats)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    chosen = dict(names or {})
    file_names = {
        "geotiff": chosen.get("geotiff", "change.tif"),
        "png": chosen.get("png", "change.png"),
        "pdf": chosen.get("pdf", "report.pdf"),
        "geojson": chosen.get("geojson", "change.geojson"),
    }

    enriched = dict(report)
    enriched.setdefault("valid_ratio", float(valid_ratio))
    enriched.setdefault("run_id", run_id)
    if params_snapshot is not None:
        enriched.setdefault("params_snapshot", dict(params_snapshot))

    result: Dict[str, object] = {
        "run_id": run_id,
        "formats": list(allowed),
        "files": {},
        "notes": {},
    }
    files = result["files"]
    notes = result["notes"]

    if "geotiff" in allowed:
        files["geotiff"] = write_geotiff(change, profile, out_dir / file_names["geotiff"])

    if "png" in allowed:
        png_meta = write_png(
            change, float(valid_ratio), out_dir / file_names["png"],
            run_id=run_id, params_snapshot=params_snapshot,
            otsu_threshold=enriched.get("otsu_threshold"),
            attribution_lines=enriched.get("attribution_lines"),
        )
        files["png"] = png_meta["path"]
        notes["png"] = png_meta

    if "pdf" in allowed:
        pdf_meta = write_pdf(enriched, out_dir / file_names["pdf"])
        files["pdf"] = pdf_meta["path"]
        notes["pdf"] = pdf_meta

    if "geojson" in allowed:
        gj_meta = write_geojson(change, profile, out_dir / file_names["geojson"])
        files["geojson"] = gj_meta["path"]
        notes["geojson"] = gj_meta

    return result
