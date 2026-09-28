// ServiceTap cookie capture -- the readable source of the bookmarklet.
//
// Do NOT paste this into the HTML by hand: run `python setup-wizard/bookmarklet/build.py`,
// which strips the comments, collapses it to one line, and stamps it into the
// `<a id="bookmarklet-link">` in docs/setup.html and docs/cookie-tool.html.
//
// Rules the builder relies on: every statement ends in ';' or '}', comments are ONLY
// whole-line '//' comments, and only single quotes are used (the result lives inside a
// double-quoted HTML attribute).
//
// What it does: on app.gomining.com, read the two login cookies a page CAN read
// (access_token, refresh_token), shape them the way the automation's browser expects, and
// copy them to the clipboard. It deliberately does NOT ask for cf_clearance: that cookie is
// Cloudflare's own (unreadable to any webpage) and the automation's browser gets its own
// automatically -- verified from a GitHub Actions runner with no login at all.
(function () {
  var HOST_OK = /(^|[.])gomining[.]com$/;

  function toast(msg, bad) {
    try {
      var d = document.createElement('div');
      d.textContent = msg;
      d.setAttribute('style', 'position:fixed;z-index:2147483647;top:16px;right:16px;max-width:380px;padding:12px 16px;border-radius:8px;font:14px/1.45 sans-serif;color:#fff;box-shadow:0 4px 16px rgba(0,0,0,.35);background:' + (bad ? '#b62324' : '#1a7f37'));
      document.body.appendChild(d);
      setTimeout(function () { if (d.parentNode) { d.parentNode.removeChild(d); } }, 10000);
    } catch (e) {
      alert(msg);
    }
  }

  if (!HOST_OK.test(location.hostname)) {
    toast('Open this on app.gomining.com while logged in, then click it again.', true);
    return;
  }

  function get(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? m[1] : null;
  }

  var access = get('access_token');
  var refresh = get('refresh_token');
  if (!access || !refresh) {
    toast('Could not find your login cookies. Make sure you are logged into GoMining in this tab, then click again.', true);
    return;
  }

  var expires = Math.floor(Date.now() / 1000) + 31536000;
  function cookie(name, value) {
    return { name: name, value: value, domain: '.gomining.com', path: '/', expires: expires, httpOnly: false, secure: true, sameSite: 'Lax' };
  }
  var json = JSON.stringify([cookie('access_token', access), cookie('refresh_token', refresh)]);

  function copiedOk() {
    toast('\u2713 Copied! Now go back to the ServiceTap setup page and paste it (Ctrl+V / Cmd+V) - or, if it is open, it may fill in by itself.', false);
  }

  function manual() {
    window.prompt('Could not copy automatically. Select all, copy (Ctrl/Cmd+C), then paste it into the ServiceTap setup page:', json);
  }

  function legacyCopy() {
    try {
      var t = document.createElement('textarea');
      t.value = json;
      t.setAttribute('style', 'position:fixed;top:0;left:0;opacity:0');
      document.body.appendChild(t);
      t.focus();
      t.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(t);
      return ok;
    } catch (e) {
      return false;
    }
  }

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(json).then(copiedOk, function () {
      if (legacyCopy()) { copiedOk(); } else { manual(); }
    });
  } else if (legacyCopy()) {
    copiedOk();
  } else {
    manual();
  }
})();
