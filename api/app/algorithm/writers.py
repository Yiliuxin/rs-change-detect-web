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
from typing import Dict, Optional, Sequence

import numpy as np
import rasterio

from app.algorithm.change_detector import CHANGE_NODATA
from app.algorithm.mask_pipeline import judge_valid_ratio
from app.algorithm.renderer import render_change_png
from app.config.constants import (
    ATTRIBUTION_S2,
    ATTRIBUTION_S2_MODIFIED,
    ERROR_MESSAGES,
    EXPORT_FORMATS,
    VALID_RATIO_MIN,
    VALID_RATIO_WARN,
)

DEFAULT_FONT_PATH: str = r"C:\Windows\Fonts\msyh.ttc"
_CJK_FONT_NAME: str = "MSYH"

__all__ = [
    "ExportFormatError", "validate_export_formats", "valid_ratio_notice",
    "write_geotiff", "write_png", "write_pdf", "export_triplet",
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


def write_pdf(report: dict, output_path, font_path: str = DEFAULT_FONT_PATH) -> Dict[str, object]:
    """生成 PDF 报告：首页回显 valid_ratio 与阈值提示，含三类统计表 / Otsu / 署名。"""
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
    else:
        _line(L("精度评价：样本不足（EX10），禁用「计算精度」",
                "ACCURACY: SAMPLES INSUFFICIENT (EX10), ACCURACY DISABLED"),
              gap=18, color=(150, 30, 30))

    # —— 合规署名（REQ-15 / NF6 / NF7）——
    y -= 4
    _line(L("署名 / ATTRIBUTION:", "ATTRIBUTION:"), size=10, gap=16)
    _line(ATTRIBUTION_S2.format(year=year), size=9, gap=15, color=(70, 70, 70))
    _line(ATTRIBUTION_S2_MODIFIED.format(year=year), size=9, gap=20, color=(70, 70, 70))

    # —— 参数快照（REQ-14，确定性，不含时间戳）——
    snapshot = report.get("params_snapshot") or {}
    raw = json.dumps(snapshot, ensure_ascii=not cn_ok, sort_keys=True, default=str)
    _line(L("参数快照 / PARAMS SNAPSHOT:", "PARAMS SNAPSHOT:"), size=9, gap=15, color=(90, 90, 90))
    for i in range(0, len(raw), 95):
        _line(raw[i:i + 95], size=7.5, gap=11, color=(110, 110, 110))
        if y < 40:
            pdf.showPage()
            y = page_h - 48

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
        "attribution": [ATTRIBUTION_S2.format(year=year),
                        ATTRIBUTION_S2_MODIFIED.format(year=year)],
    }


def export_triplet(
    change,
    profile: dict,
    valid_ratio: float,
    report: dict,
    output_dir,
    formats: Sequence[str] = EXPORT_FORMATS,
    run_id: str = "",
    params_snapshot: Optional[dict] = None,
) -> Dict[str, object]:
    """一次性导出 GeoTIFF / PNG / PDF 三件套（文件名固定）。

    文件名：``change.tif`` / ``change.png`` / ``report.pdf``。
    """
    allowed = validate_export_formats(formats)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

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
        files["geotiff"] = write_geotiff(change, profile, out_dir / "change.tif")

    if "png" in allowed:
        png_meta = write_png(
            change, float(valid_ratio), out_dir / "change.png",
            run_id=run_id, params_snapshot=params_snapshot,
            otsu_threshold=enriched.get("otsu_threshold"),
            attribution_lines=enriched.get("attribution_lines"),
        )
        files["png"] = png_meta["path"]
        notes["png"] = png_meta

    if "pdf" in allowed:
        pdf_meta = write_pdf(enriched, out_dir / "report.pdf")
        files["pdf"] = pdf_meta["path"]
        notes["pdf"] = pdf_meta

    return result
