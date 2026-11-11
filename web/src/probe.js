// -*- coding: utf-8 -*-
// B2 鼠标拾取探针：mousemove 节流 180ms + AbortController 取消旧请求。
// 与 constants.py PROBE_THROTTLE_MS 同值；失败/越界一律显示「—」，
// 不弹窗、不抛未捕获异常、不拦截地图事件（打点模式完全不受影响）。
import { AUTH } from './api.js';

const PROBE_URL = '/api/v1/result/change/probe';
const THROTTLE_MS = 180;
const DASH = '—';

function fmt(v, digits = 4) {
  const n = Number(v);
  return (v == null || !Number.isFinite(n)) ? DASH : n.toFixed(digits);
}

function text(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

export function initProbe({ map, getRunId }) {
  if (!map || typeof getRunId !== 'function') return { dispose() {} };
  const hud = document.getElementById('probe-hud');
  if (!hud) return { dispose() {} };

  let controller = null;
  let timer = null;
  let lastAt = 0;
  let lastLngLat = null;
  let seq = 0;

  function showDash() {
    text('probe-coord', DASH);
    text('probe-index', DASH);
    text('probe-delta', DASH);
    text('probe-class', DASH);
  }

  function render(d) {
    if (!d || !d.in_bounds) { showDash(); return; }
    text('probe-coord', fmt(d.lng, 5) + ', ' + fmt(d.lat, 5));
    text('probe-index', fmt(d.t1_index) + ' / ' + fmt(d.t2_index));
    const cls = d.change_label || DASH;
    text('probe-delta', 'Δ ' + fmt(d.delta) + ' · ' + cls);
    const grade = d.intensity_label || DASH;
    const lc = (d.landcover_t1_label || DASH) + '→' + (d.landcover_t2_label || DASH);
    text('probe-class', grade + ' · ' + lc);
  }

  async function request(lng, lat) {
    const runId = getRunId();
    if (!runId) { showDash(); return; }
    // 取消旧请求：鼠标连续移动时只保留最后一次，避免乱序覆盖 / 卡顿
    if (controller) controller.abort();
    const mine = new AbortController();
    controller = mine;
    const mySeq = ++seq;
    const qs = new URLSearchParams({
      run_id: runId, lng: String(lng), lat: String(lat),
    });
    const headers = { Accept: 'application/json' };
    const token = AUTH.token;
    if (token) headers.Authorization = 'Bearer ' + token;
    try {
      const resp = await fetch(PROBE_URL + '?' + qs.toString(),
        { method: 'GET', headers, signal: mine.signal });
      if (!resp.ok) { if (mySeq === seq) showDash(); return; }
      const d = await resp.json();
      if (mySeq === seq && !mine.signal.aborted) render(d);
    } catch (_) {
      // 被新请求 abort / 网络抖动：静默显示 —，绝不弹窗、绝不打断打点
      if (mySeq === seq) showDash();
    } finally {
      if (controller === mine) controller = null;
    }
  }

  function onMove(e) {
    const ll = e && e.lngLat;
    if (!ll || !Number.isFinite(Number(ll.lng)) || !Number.isFinite(Number(ll.lat))) return;
    lastLngLat = { lng: Number(ll.lng), lat: Number(ll.lat) };
    if (timer) return; // 已有一次排队请求，节流窗口内不重复排队
    const wait = Math.max(0, THROTTLE_MS - (Date.now() - lastAt));
    timer = window.setTimeout(() => {
      timer = null;
      lastAt = Date.now();
      if (lastLngLat) request(lastLngLat.lng, lastLngLat.lat);
    }, wait);
  }

  const onLeave = () => { lastLngLat = null; showDash(); };
  map.on('mousemove', onMove);
  map.on('mouseout', onLeave);
  showDash();

  return {
    refresh() { if (lastLngLat) request(lastLngLat.lng, lastLngLat.lat); },
    dispose() {
      if (timer) window.clearTimeout(timer);
      if (controller) controller.abort();
      map.off('mousemove', onMove);
      map.off('mouseout', onLeave);
      showDash();
    },
  };
}
