// -*- coding: utf-8 -*-
// 登录/注册页逻辑：tab 切换 + 调 /auth/login|register + token 入 localStorage + 跳转工作台。
import '../src/style.css';

const $ = (id) => document.getElementById(id);

// 已登录则直接进工作台
if (localStorage.getItem('rst_token')) location.replace('/');

// —— tab 切换 ——
document.querySelectorAll('.login-tab').forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll('.login-tab').forEach((t) => t.classList.toggle('active', t === tab));
    const isLogin = tab.dataset.tab === 'login';
    $('login-form').hidden = !isLogin;
    $('register-form').hidden = isLogin;
  };
});

function showErr(id, msg) {
  $(id).textContent = msg || '';
}

async function submitAuth(path, body, errId, btnId, btnText) {
  showErr(errId, '');
  const btn = $(btnId);
  btn.disabled = true;
  btn.textContent = '请稍候…';
  try {
    const resp = await fetch('/api/v1' + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      showErr(errId, (j.message || ('请求失败（HTTP ' + resp.status + '）'))
        + (j.code ? ' [' + j.code + ']' : ''));
      return;
    }
    localStorage.setItem('rst_token', j.token);
    localStorage.setItem('rst_username', j.display_name || j.username);
    location.replace('/');
  } catch (_) {
    showErr(errId, '网络异常或后端不可达，请确认后端已启动');
  } finally {
    btn.disabled = false;
    btn.textContent = btnText;
  }
}

$('login-form').onsubmit = (e) => {
  e.preventDefault();
  const username = $('lg-username').value.trim();
  const password = $('lg-password').value;
  if (!username || !password) { showErr('login-err', '请填写用户名与密码'); return; }
  submitAuth('/auth/login', { username, password }, 'login-err', 'login-submit', '登 录');
};

$('register-form').onsubmit = (e) => {
  e.preventDefault();
  const username = $('rg-username').value.trim();
  const password = $('rg-password').value;
  const display_name = $('rg-display').value.trim();
  if (!/^[A-Za-z0-9_]{3,20}$/.test(username)) {
    showErr('register-err', '用户名需为 3–20 位字母/数字/下划线'); return;
  }
  if (password.length < 6) { showErr('register-err', '密码至少 6 位'); return; }
  submitAuth('/auth/register', { username, password, display_name },
    'register-err', 'register-submit', '注 册');
};
