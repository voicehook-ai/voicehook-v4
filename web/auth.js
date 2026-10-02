/* Gemeinsame Anmelde-Aufrufe für /login und den Speichern-Dialog in voice.html
   (Oliver 02.10.: dieselben Endpunkte, kein Duplikat).
   - providers(): GET /api/auth/providers -> {google, github, email} (Fehler -> {})
   - start(p, next): POST /api/auth/<p>/start {next} MIT X-Wallet-Token (falls da)
     -> Nonce in localStorage 'vh_login_nonce', dann zur authorize_url.
     Rückgabe {ok:true} (Weiterleitung läuft) oder {ok:false, status}.
   - sendLink(email): POST /api/login {email} -> Nonce merken; {ok, status}. */
(function () {
  'use strict';
  var TOKEN_KEY = 'vh-wallet-v1', NONCE_KEY = 'vh_login_nonce';
  function token() { try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; } }
  function setNonce(n) { try { if (n) localStorage.setItem(NONCE_KEY, n); else localStorage.removeItem(NONCE_KEY); } catch (e) {} }
  function headers() { var h = { 'Content-Type': 'application/json' }; var t = token(); if (t) h['X-Wallet-Token'] = t; return h; }
  async function providers() {
    try { var r = await fetch('/api/auth/providers', { cache: 'no-store' }); if (r.ok) { var j = await r.json(); if (j && typeof j === 'object') return j; } } catch (e) {}
    return {};
  }
  async function start(p, next) {
    var r, d;
    try {
      r = await fetch('/api/auth/' + encodeURIComponent(p) + '/start', { method: 'POST', headers: headers(), body: JSON.stringify({ next: next }) });
      d = await r.json().catch(function () { return {}; });
    } catch (e) { return { ok: false, status: 0 }; }
    if (r.ok && d && typeof d.authorize_url === 'string' && /^https:\/\//.test(d.authorize_url)) {
      if (typeof d.login_nonce === 'string' && d.login_nonce) setNonce(d.login_nonce);
      location.href = d.authorize_url;
      return { ok: true };
    }
    return { ok: false, status: r.status };
  }
  async function sendLink(email) {
    var r;
    try { r = await fetch('/api/login', { method: 'POST', headers: headers(), body: JSON.stringify({ email: email }) }); }
    catch (e) { return { ok: false, status: 0 }; }
    if (r.ok) {
      var d = await r.json().catch(function () { return {}; });
      if (d && typeof d.login_nonce === 'string' && d.login_nonce) setNonce(d.login_nonce);
    }
    return { ok: r.ok, status: r.status };
  }
  window.vhAuth = { providers: providers, start: start, sendLink: sendLink, TOKEN_KEY: TOKEN_KEY, NONCE_KEY: NONCE_KEY };
})();
