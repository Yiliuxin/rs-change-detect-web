// -*- coding: utf-8 -*-
// 全局错误码中文映射与统一展示（HLD 第 5 章 / REQ 全局错误处理）。
// 映射逐字照抄 api/app/config/constants.py 的 ERROR_MESSAGES，不臆造语义。

export const ERROR_MESSAGES = {
  EX1: 'AOI 跨度超过 0.25°×0.25°，请缩小框选范围',
  EX2: '无匹配影像对，请调整 AOI 或时相后重试',
  EX3: '影像全云覆盖（有效像元占比为 0），本轮检测终止',
  EX4: '双时相未配准，残差超容差，拒绝计算',
  EX5: '有效像元占比偏低（0.10–0.50），确认后可继续',
  EX6: '导出格式不支持，仅支持 GeoTIFF / PNG / PDF',
  EX7: '并发超限，MVP 并发上限为 1，请稍后重试',
  EX8: '辐射口径混用，双时相数据源不一致',
  EX9: '处理基线版次不可判定或未达标（S2 需 ≥04.00）',
  EX10: '样本数量不足，每类至少 30 个样本点',
  EX11: '有效像元不足（占比 < 0.10），本轮检测终止',
  'SYS-01': 'COG 读取失败',
  'SYS-02': '内存不足',
  'SYS-03': '专题图渲染失败',
  'SYS-04': '数据库连接失败',
  'SYS-05': '处理超时',
  // B 级扩展（在线取数）：文案逐字照抄 api/app/config/constants.py ERROR_MESSAGES
  'SYS-06': '在线数据源不可达或影像下载失败，请稍后重试',
  'SYS-07': '在线取数结果写入对象存储或数据库失败，请稍后重试',
};

// 后端真实业务错误：{code, message, run_id}。此类错误必须全局展示，不允许被演示降级吞掉。
export class ApiError extends Error {
  constructor(code, message, run_id) {
    super(message || code || '未知错误');
    this.name = 'ApiError';
    this.code = code || 'SYS';
    this.message = message || '';
    this.run_id = run_id || null;
  }
}

// 按 code 取内置中文映射；映射未命中时回退后端原文，绝不臆造语义。
export function errorMessage(payload) {
  const code = payload && payload.code;
  if (code && ERROR_MESSAGES[code]) return ERROR_MESSAGES[code];
  if (payload && payload.message) return payload.message;
  return '发生未知错误（后端未返回可识别的错误码）';
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// 轻量 toast（普通提示 / 错误提示均可复用）
export function showToast(text, type = 'info', ttl = 3600) {
  const box = document.getElementById('toasts');
  if (!box) return;
  const el = document.createElement('div');
  el.className = 'toast toast-' + type;
  el.innerHTML = '<span class="toast-dot"></span><span>' + esc(text) + '</span>';
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add('in'));
  window.setTimeout(() => {
    el.classList.remove('in');
    window.setTimeout(() => el.remove(), 260);
  }, ttl);
}

// 统一横幅：展示后端 {code, message, run_id}，中文提示按 code 映射
export function showError(payload) {
  const code = (payload && payload.code) || 'SYS';
  const zh = errorMessage(payload);
  const banner = document.getElementById('banner');
  if (banner) {
    let html = '';
    html += '<div class="banner-icon">!</div>';
    html += '<div class="banner-body">';
    html += '<div class="banner-title"><span class="banner-code">[' + esc(code) + ']</span> ' + esc(zh) + '</div>';
    const subs = [];
    if (payload && payload.message && payload.message !== zh) {
      subs.push('后端原文：' + esc(payload.message));
    }
    if (payload && payload.run_id) {
      subs.push('run_id：' + esc(payload.run_id));
    }
    if (!ERROR_MESSAGES[code]) {
      subs.push('（该错误码未纳入内置映射，已按后端原文展示）');
    }
    if (subs.length) {
      html += '<div class="banner-sub">' + subs.map(esc).join('　·　') + '</div>';
    }
    html += '</div>';
    html += '<button type="button" class="banner-close" aria-label="关闭">×</button>';
    banner.innerHTML = html;
    banner.hidden = false;
    banner.classList.add('banner-in');
    const close = banner.querySelector('.banner-close');
    if (close) close.onclick = () => { banner.hidden = true; banner.classList.remove('banner-in'); };
  }
  showToast('[' + code + '] ' + zh, 'error', 5200);
}

export function clearError() {
  const banner = document.getElementById('banner');
  if (banner) { banner.hidden = true; banner.classList.remove('banner-in'); }
}
