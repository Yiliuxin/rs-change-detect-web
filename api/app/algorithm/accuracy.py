# -*- coding: utf-8 -*-
"""精度评价（HLD 模块4 功能一，REQ-09 / EX10）。

纯函数：3×3 混淆矩阵 → 总体精度 OA → Kappa（sklearn.cohen_kappa_score）。
"任一类"指 1 增强 + 2 退化 + 3 稳定共 **3 类**；参考样本中任一类
< MIN_SAMPLES_PER_CLASS（30，即 ≤29）→ ``SampleInsufficientError``（EX10），
禁用"计算精度"。Kappa < KAPPA_TEACHING_MIN 标注 ``教学级，非工程级``。
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence, Tuple

import numpy as np
from sklearn.metrics import cohen_kappa_score, confusion_matrix

from app.algorithm.change_detector import CHANGE_CODES
from app.config.constants import ERROR_MESSAGES, KAPPA_TEACHING_MIN, MIN_SAMPLES_PER_CLASS

DEFAULT_LABELS: Tuple[int, int, int] = CHANGE_CODES

__all__ = [
    "DEFAULT_LABELS",
    "SampleInsufficientError",
    "compute_confusion_matrix",
    "accuracy_report",
]


class SampleInsufficientError(Exception):
    """任一类参考样本数 < MIN_SAMPLES_PER_CLASS → EX10（禁用计算精度）。"""

    code = "EX10"


def _as_labels(y, name: str) -> np.ndarray:
    arr = np.asarray(y).ravel()
    if arr.size == 0:
        raise ValueError(f"{name} 为空，无法计算精度")
    return arr


def _validate_labels(y_true: np.ndarray, y_pred: np.ndarray,
                     labels: Sequence[int]) -> None:
    allowed = set(int(c) for c in labels)
    for name, arr in (("y_true", y_true), ("y_pred", y_pred)):
        unknown = set(int(v) for v in np.unique(arr)) - allowed
        if unknown:
            raise ValueError(f"{name} 含非三类编码 {sorted(unknown)}，仅允许 {sorted(allowed)}")


def compute_confusion_matrix(y_true, y_pred,
                             labels: Sequence[int] = DEFAULT_LABELS) -> np.ndarray:
    """3×3 混淆矩阵（行=参考 y_true，列=预测 y_pred），返回 int64 ndarray。"""
    yt = _as_labels(y_true, "y_true")
    yp = _as_labels(y_pred, "y_pred")
    if yt.shape != yp.shape:
        raise ValueError(f"y_true / y_pred 长度不一致：{yt.size} vs {yp.size}")
    label_tuple = tuple(int(c) for c in labels)
    _validate_labels(yt, yp, label_tuple)
    return confusion_matrix(yt, yp, labels=label_tuple).astype(np.int64)


def accuracy_report(y_true, y_pred,
                    labels: Sequence[int] = DEFAULT_LABELS,
                    run_id: str = "",
                    params_snapshot: Optional[dict] = None) -> Dict[str, object]:
    """返回精度报告 dict：混淆矩阵 / OA / Kappa / note / per_class_counts 等。"""
    yt = _as_labels(y_true, "y_true")
    yp = _as_labels(y_pred, "y_pred")
    if yt.shape != yp.shape:
        raise ValueError(f"y_true / y_pred 长度不一致：{yt.size} vs {yp.size}")
    label_tuple = tuple(int(c) for c in labels)
    _validate_labels(yt, yp, label_tuple)

    counts_true = {int(c): int((yt == c).sum()) for c in label_tuple}
    counts_pred = {int(c): int((yp == c).sum()) for c in label_tuple}
    insufficient = [c for c in label_tuple if counts_true[c] < MIN_SAMPLES_PER_CLASS]
    if insufficient:
        raise SampleInsufficientError(
            f"{ERROR_MESSAGES['EX10']}；类 {insufficient} 参考样本数={counts_true} "
            f"< {MIN_SAMPLES_PER_CLASS}"
        )

    cm = compute_confusion_matrix(yt, yp, labels=label_tuple)
    total = int(cm.sum())
    correct = int(np.trace(cm))
    oa = float(correct) / float(total) if total else 0.0

    try:
        kappa = float(cohen_kappa_score(yt, yp, labels=list(label_tuple)))
    except Exception:
        kappa = 0.0
    if not np.isfinite(kappa):
        kappa = 0.0

    teaching = bool(kappa < KAPPA_TEACHING_MIN)
    note = "教学级，非工程级" if teaching else ""

    # 生产者精度（每类参考样本中判对的比例），便于教学展示
    per_class_accuracy = {}
    for i, c in enumerate(label_tuple):
        denom = int(cm[i, :].sum())
        per_class_accuracy[int(c)] = float(cm[i, i]) / float(denom) if denom else 0.0

    return {
        "confusion_matrix": cm.tolist(),
        "labels": list(label_tuple),
        "overall_accuracy": oa,
        "kappa": kappa,
        "note": note,
        "teaching_level": teaching,
        "kappa_teaching_min": KAPPA_TEACHING_MIN,
        "min_samples_per_class": MIN_SAMPLES_PER_CLASS,
        "per_class_counts": counts_true,
        "per_class_predicted_counts": counts_pred,
        "per_class_accuracy": per_class_accuracy,
        "total": total,
        "correct": correct,
        "run_id": run_id,
        "params_snapshot": dict(params_snapshot or {}),
    }
