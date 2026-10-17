"""Self-hosted connection page; no analytics, third-party scripts or credential URLs."""

CONNECT_HTML = """<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>اتصال شخصی اینستاگرام</title><link rel="stylesheet" href="/static/connect.css">
</head><body><main><h1>اتصال شخصی اینستاگرام</h1>
<p>این صفحه متعلق به ربات دانلودر است، نه سایت اینستاگرام. اطلاعات ورود فقط برای اتصال
به اینستاگرام استفاده می‌شود؛ رمز عبور و کد تأیید ذخیره نمی‌شوند. فقط نشست شما رمزنگاری می‌شود.</p>
<p>اتصال حساب، اشتراک VIP ایجاد نمی‌کند و اجازهٔ دسترسی فراتر از حساب شما نمی‌دهد.</p>
<form id="connect-form" method="post" action="/instagram/connect/complete">
<label for="username">نام کاربری اینستاگرام</label>
<input id="username" autocomplete="off" maxlength="256" required>
<label for="password" id="password-label">رمز عبور</label>
<input id="password" type="password" autocomplete="off" maxlength="4096" required>
<label id="code-label" for="code" hidden>کد تأیید دومرحله‌ای</label>
<input id="code" type="text" autocomplete="off" maxlength="32" inputmode="numeric" hidden disabled>
<label class="consent"><input id="consent" type="checkbox" required>
با استفاده از نشست شخصی برای محتوایی که حسابم مجاز به دیدن آن است موافقم.</label>
<button id="submit" type="submit" disabled>اتصال حساب</button></form>
<p id="status" role="status" aria-live="polite">در حال بررسی لینک امن اتصال...</p>
<p>رمز عبور یا کد تأیید را در تلگرام ارسال نکنید.</p></main>
<script src="/static/connect.js" defer></script></body></html>"""

CONNECT_CSS = """
body{font-family:Tahoma,sans-serif;background:#f3f5f8;color:#17212b;margin:0;padding:24px}
main{max-width:440px;margin:auto;background:white;border-radius:14px;padding:24px;line-height:1.9}
h1{font-size:1.4rem}label{display:block;margin-top:12px}input:not([type=checkbox]){
width:100%;padding:10px;box-sizing:border-box;border:1px solid #a9b5c2;border-radius:6px;direction:ltr}
button{width:100%;padding:12px;margin-top:18px;border:0;border-radius:7px;background:#1769e0;
color:white;font:inherit}button:disabled{opacity:.55}.consent{font-size:.9rem}#status{white-space:pre-wrap}
"""

CONNECT_JS = r"""'use strict';
(() => {
  const form = document.getElementById('connect-form');
  const submit = document.getElementById('submit');
  const statusEl = document.getElementById('status');
  const username = document.getElementById('username');
  const password = document.getElementById('password');
  const code = document.getElementById('code');
  const codeLabel = document.getElementById('code-label');
  const consent = document.getElementById('consent');
  let csrf = null, stage = 'need_credentials', busy = false;
  let token = new URLSearchParams(location.hash.slice(1)).get('handoff');
  history.replaceState(null, '', location.pathname);
  const exchange = (async () => {
    if (location.protocol !== 'https:' || !token) {
      statusEl.textContent = 'لینک امن معتبر نیست؛ از داخل ربات لینک تازه بگیرید.';
      return false;
    }
    try {
      const request = fetch('/instagram/connect/exchange', {
        method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({token})
      });
      token = null;
      const response = await request;
      const data = await response.json();
      if (!response.ok || typeof data.csrf_token !== 'string') throw new Error('exchange');
      csrf = data.csrf_token;
      submit.disabled = false;
      statusEl.textContent = 'نام کاربری و رمز عبور خود را وارد کنید.';
      return true;
    } catch (_) {
      statusEl.textContent = 'لینک منقضی یا ارتباط قطع شده است؛ از ربات لینک تازه بگیرید.';
      return false;
    }
  })();
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !consent.checked) return;
    if (!(await exchange) || busy) return;
    busy = true; submit.disabled = true;
    let input = stage === 'need_2fa' ? {code:code.value} : {
      username:username.value, password:password.value, consent:'yes'
    };
    password.value = ''; code.value = '';
    statusEl.textContent = 'در حال بررسی با اینستاگرام...';
    try {
      const request = fetch('/instagram/connect/complete', {
        method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-TMB-CSRF':csrf},
        body:JSON.stringify({input})
      });
      input = null;
      const response = await request;
      const data = await response.json();
      stage = data.stage || 'unavailable';
      if (stage === 'connected') {
        csrf = null; username.value = ''; form.hidden = true;
        statusEl.textContent = 'حساب با موفقیت متصل شد. به ربات برگردید و وضعیت اتصال را بررسی کنید.';
      } else if (stage === 'need_2fa') {
        username.disabled = true; password.disabled = true;
        code.hidden = false; code.disabled = false; code.required = true; codeLabel.hidden = false;
        statusEl.textContent = 'کد تأیید دومرحله‌ای را وارد کنید.';
      } else if (response.status === 401 || response.status === 408) {
        csrf = null;
        statusEl.textContent = 'نشست منقضی شد؛ از ربات لینک تازه بگیرید.';
      } else {
        stage = 'need_credentials'; username.disabled = false; password.disabled = false;
        code.hidden = true; code.disabled = true; codeLabel.hidden = true;
        statusEl.textContent = 'ورود تأیید نشد. اطلاعات را بررسی کنید؛ ممکن است تأیید داخل برنامهٔ اینستاگرام لازم باشد.';
      }
    } catch (_) {
      csrf = null;
      statusEl.textContent = 'ارتباط قطع شد. ابتدا وضعیت اتصال را در ربات بررسی کنید؛ سپس لینک تازه بگیرید.';
    } finally {
      input = null; busy = false; submit.disabled = csrf === null;
    }
  });
})();
"""
