"""
caphy_desktop.py — packaged desktop entry point for CAPHY (the .exe).

What it does, in order:
  1. Picks a stable, writable folder for runtime files (caphy.db, logs, prefs)
     so the app works no matter where the .exe is installed - including
     read-only locations like Program Files.
  2. Starts the full CAPHY backend (cameras, detection, cloud services, Flask).
  3. Opens the console: it TRIES a native desktop window (pywebview); if that
     backend isn't available on this machine, it falls back to opening the
     default web browser. Either way the user gets a working app.

Build into an .exe with:  pyinstaller CAPHY.spec   (see BUILD_EXE.md)
"""

import os
import sys
import threading
import time


def _webview_storage_path():
    """Persistent folder for the desktop window's browser profile (cookies,
    local storage) - lives inside the same stable, writable, per-user
    folder as caphy.db/logs/settings (see resource_path.use_writable_workdir()),
    so it survives every relaunch instead of pywebview's private_mode=True
    default silently throwing the whole profile away on close."""
    try:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "CAPHY", "webview_profile")
        os.makedirs(path, exist_ok=True)
        return path
    except Exception as e:
        print(f"[CAPHY] Could not set up webview storage path: {e}")
        return None

_LOADING_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<title>CAPHY</title>
<style>
  html,body{height:100%;margin:0;background:#0b1220;color:#e6edf7;
    font-family:Segoe UI,Arial,sans-serif;display:flex;align-items:center;
    justify-content:center;flex-direction:column}
  .spinner{width:48px;height:48px;border:4px solid #1e293b;
    border-top-color:#3b82f6;border-radius:50%;
    animation:spin 0.9s linear infinite;margin-bottom:20px}
  @keyframes spin{to{transform:rotate(360deg)}}
  h1{font-size:18px;font-weight:600;margin:0 0 6px 0;letter-spacing:0.3px}
  p{font-size:13px;color:#8b98ab;margin:0}
</style></head>
<body>
<div id="appTitlebar" style="display:none;position:fixed;top:0;left:0;right:0;z-index:1000;
    height:36px;align-items:center;justify-content:space-between;
    background:#0b1220;border-bottom:1px solid #1e293b;padding:0 10px 0 14px;
    box-sizing:border-box;user-select:none">
    <div style="display:flex;align-items:center;gap:9px">
      <svg viewBox="0 0 96 96" width="18" height="18" xmlns="http://www.w3.org/2000/svg">
        <defs><linearGradient id="atbg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="#5b7bff"></stop><stop offset="1" stop-color="#a78bfa"></stop>
        </linearGradient></defs>
        <path d="M48 6 L82 18 V44 C82 66 66 82 48 90 C30 82 14 66 14 44 V18 Z" fill="url(#atbg)"></path>
      </svg>
      <span style="display:block;color:#8b98ab;font-size:12px;font-weight:700;
        letter-spacing:1.2px;text-transform:uppercase">CAPHY Console</span>
    </div>
    <div id="atbBtns" style="display:none;align-items:center;gap:2px">
      <button id="atbMin" title="Minimize" type="button"
        style="width:32px;height:28px;display:flex;align-items:center;justify-content:center;
          border-radius:5px;cursor:pointer;color:#8b98ab;background:transparent;border:none">
        <svg viewBox="0 0 11 11" width="11" height="11"><rect x="0" y="5" width="11" height="1.4" fill="currentColor"></rect></svg>
      </button>
      <button id="atbMax" title="Maximize" type="button"
        style="width:32px;height:28px;display:flex;align-items:center;justify-content:center;
          border-radius:5px;cursor:pointer;color:#8b98ab;background:transparent;border:none">
        <svg id="atbMaxIcon" viewBox="0 0 11 11" width="11" height="11"><rect x="0.75" y="0.75" width="9.5" height="9.5" rx="1" fill="none" stroke="currentColor" stroke-width="1.3"></rect></svg>
      </button>
      <button id="atbClose" title="Close" type="button"
        style="width:32px;height:28px;display:flex;align-items:center;justify-content:center;
          border-radius:5px;cursor:pointer;color:#8b98ab;background:transparent;border:none">
        <svg viewBox="0 0 11 11" width="11" height="11"><path d="M0.5 0.5l10 10M10.5 0.5l-10 10" stroke="currentColor" stroke-width="1.3"></path></svg>
      </button>
    </div>
  </div>
  <script>
  (function(){
    var ICON_MAXIMIZE = '<rect x="0.75" y="0.75" width="9.5" height="9.5" rx="1" fill="none" stroke="currentColor" stroke-width="1.3"></rect>';
    var ICON_RESTORE = '<rect x="2.5" y="0.75" width="7.75" height="7.75" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"></rect>'
      + '<path d="M0.75 3.25V9.5a0.75 0.75 0 0 0 0.75 0.75H7.75" fill="none" stroke="currentColor" stroke-width="1.2"></path>';

    function wireAppTitlebar(){
      if(!window.pywebview || !window.pywebview.api) return;
      var bar = document.getElementById('appTitlebar');
      if(bar) bar.style.display = 'flex';
      var btns = document.getElementById('atbBtns');
      if(btns) btns.style.display = 'flex';
      var min = document.getElementById('atbMin');
      var max = document.getElementById('atbMax');
      var maxIcon = document.getElementById('atbMaxIcon');
      var close = document.getElementById('atbClose');
      var isMax = false;
      function setMaxIcon(){
        if(maxIcon) maxIcon.innerHTML = isMax ? ICON_RESTORE : ICON_MAXIMIZE;
        if(max) max.title = isMax ? 'Restore' : 'Maximize';
      }
      if(min) min.onclick = function(){ window.pywebview.api.minimize_window(); };
      if(max) max.onclick = function(){
        window.pywebview.api.toggle_maximize_window();
        isMax = !isMax;
        setMaxIcon();
      };
      if(close) close.onclick = function(){ window.pywebview.api.close_window(); };
      if(bar) bar.ondblclick = function(e){
        if(e.target.closest('button')) return;
        window.pywebview.api.toggle_maximize_window();
        isMax = !isMax;
        setMaxIcon();
      };
    }
    window.addEventListener('pywebviewready', wireAppTitlebar);
    if(window.pywebview) wireAppTitlebar();
  })();
  </script>
  <div class="spinner"></div>
  <h1>Starting CAPHY&hellip;</h1>
  <p id="msg">Waking up cameras and services</p>
<script>
  var tries = 0;
  var navigated = false;
  function poll() {
    if (navigated) return;  // stop polling once we've triggered navigation
    tries++;
    // no-cors HEAD-style probe: we only care whether the server answers
    // AT ALL, not what it answers. A normal fetch() to "/" was used here
    // before, but fetch() follows redirects internally and resolves
    // "successfully" even for the 302 that /login redirect produces -
    // so the old code kept sending the page back to "/", which got
    // redirected again, forever (visible as endless reloading). A plain
    // top-level navigation lets the browser itself follow the redirect
    // chain the normal way, landing on /login or the dashboard as
    // appropriate - so once we know the server is alive at all, we
    // navigate exactly once and let it handle the rest.
    fetch("http://127.0.0.1:5000/", {cache: "no-store", mode: "no-cors"})
      .then(function(){
        navigated = true;
        window.location.replace("http://127.0.0.1:5000/");
      })
      .catch(function(){
        if (tries > 60) {
          document.getElementById("msg").textContent =
            "Still starting... this is taking longer than usual.";
        }
        setTimeout(poll, 500);
      });
  }
  setTimeout(poll, 300);
</script>
</body></html>"""

_LOADING_PORT = 5099

def _start_loading_server():
    """Serve the loading page over real HTTP on a small side port instead
    of a data: URL - pywebview's Windows backend (WebView2) does not
    reliably navigate data: URLs, which showed up as a 404 error inside
    the window instead of the loading screen. A tiny stdlib HTTP server
    started synchronously (before the window opens) is ready immediately
    and needs no changes to the real Flask app."""
    import http.server

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = _LOADING_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass  # keep the console quiet

    srv = http.server.HTTPServer(("127.0.0.1", _LOADING_PORT), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def _loading_url():
    _start_loading_server()
    return "http://127.0.0.1:%d/" % _LOADING_PORT



class _WindowApi:
    """Exposed to the page's JS as `pywebview.api.*` (via js_api=) so the
    custom in-page top bar - added because the window is frameless and has
    no native title bar / minimize / close buttons - can still minimize
    and close the app. window.minimize()/destroy() are stable pywebview
    calls present across 3.x-5.x, so this doesn't depend on pinning a
    specific pywebview version.

    The window object doesn't exist yet when create_window() needs the
    js_api instance, so this is constructed with _window=None and wired up
    with the real window right after create_window() returns (see below) -
    still before webview.start(), so the bridge is fully ready by the time
    the page can call into it."""
    def __init__(self):
        self._window = None
        self._maximized = False
        self._restore_geom = (1200, 800, 0, 0)

    def minimize_window(self):
        if self._window:
            self._window.minimize()

    def close_window(self):
        if self._window:
            self._window.destroy()

    def toggle_maximize_window(self):
        # .maximize() isn't a consistently available method across every
        # pywebview version, but .resize()/.move() have been stable for a
        # long time - so "maximize" here is implemented manually: resize
        # to the screen's usable area and move to (0,0), remembering the
        # previous size/position so the same button can restore it.
        if not self._window:
            return
        try:
            if getattr(self, '_maximized', False):
                w, h, x, y = self._restore_geom
                self._window.resize(w, h)
                self._window.move(x, y)
                self._maximized = False
            else:
                import ctypes
                user32 = ctypes.windll.user32
                screen_w = user32.GetSystemMetrics(0)
                screen_h = user32.GetSystemMetrics(1)
                self._restore_geom = (1200, 800,
                                      max(0, (screen_w - 1200)//2),
                                      max(0, (screen_h - 800)//2))
                self._window.resize(screen_w, screen_h)
                self._window.move(0, 0)
                self._maximized = True
        except Exception as e:
            print(f'[CAPHY] maximize/restore failed: {e}')


def _open_console_window(url):
    """Try a native window; fall back to the default browser.

    Includes a crash watchdog: on Windows, pywebview's WebView2 backend can
    have its underlying native renderer process crash outright during an
    abrupt network/connectivity change (a DNS flap, adapter reset, etc.) -
    this is a native process crash, not a Python exception, so no
    try/except inside this app can catch it. When it happens, webview.start()
    returns almost immediately (well under NORMAL_SESSION_SECS), as if the
    user had closed the window instantly. We tell the two apart by timing
    the session: a real user close is essentially never that fast, so a
    too-short session re-opens the window automatically (up to a few
    times) instead of silently letting the whole app vanish - which is
    exactly the "goes offline -> whole app disappears" bug this fixes."""
    NORMAL_SESSION_SECS = 4.0
    MAX_AUTO_RESTARTS = 5
    attempt = 0
    while attempt <= MAX_AUTO_RESTARTS:
        attempt += 1
        try:
            import webview  # pywebview
            # frameless=True removes the native OS title bar (per the branding
            # request: no OS "CAPHY Security Console" bar, just our own UI).
            # No drag/resize wiring is added on purpose - dragging a frameless
            # pywebview window reliably needs backend-specific JS that varies
            # across pywebview versions, and a broken drag right before the
            # thesis defense is a worse outcome than a fixed-position window.
            # The window opens centered and stays put; the custom top bar
            # (added in the HTML) supplies its own minimize/close buttons via
            # js_api, since the OS ones are gone.
            api = _WindowApi()
            window = webview.create_window("CAPHY Security Console", url,
                                  width=1200, height=800, resizable=True,
                                  frameless=True, js_api=api)
            api._window = window
            _start_ts = time.time()
            # private_mode=False is the actual fix for "signs me out every
            # time I close the app": pywebview 6.x defaults private_mode to
            # True, which explicitly means "cookies and local storage are
            # not preserved" - a fresh incognito-style browser profile every
            # single launch, thrown away on close. No server-side session
            # setting (even a permanent 30-day Flask session cookie) can
            # survive that, because the cookie storage itself never
            # persists to disk in the first place. storage_path pins WHERE
            # that persistent profile lives (this app's own writable
            # folder, not some pywebview-managed default elsewhere) so it
            # survives across launches AND isn't scattered outside the
            # app's own data folder.
            webview.start(private_mode=False, storage_path=_webview_storage_path())
            elapsed = time.time() - _start_ts
            if elapsed >= NORMAL_SESSION_SECS:
                return  # window closed normally by the user -> exit
            print(f"[CAPHY] Window closed after only {elapsed:.1f}s - likely a "
                  f"native crash (e.g. a network change), not a user close. "
                  f"Reopening ({attempt}/{MAX_AUTO_RESTARTS})...")
            continue
        except Exception as e:
            print(f"[CAPHY] Native window unavailable ({e}); opening in browser.")
            break

    # 2) browser fallback (always works).
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception as e:
        print(f"[CAPHY] Could not open a browser automatically: {e}")
        print(f"[CAPHY] Open this address manually:  {url}")

    # Keep the process (and the background server threads) alive.
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main():
    # Called unconditionally (not just when frozen) so this .exe and the
    # dev-mode desktop_launcher.py always agree on where caphy.db, logs,
    # and settings live - see resource_path.use_writable_workdir()'s
    # docstring for why that matters.
    from resource_path import use_writable_workdir
    use_writable_workdir()

    # Import AFTER chdir so any import-time paths resolve against the workdir.
    from web.server import (app, start_workers, resolve_cameras,
                            start_auto_arm_scheduler, start_lan_discovery_beacon)

    from identity import get_device_identity
    dev = get_device_identity()
    print(f"[CAPHY] Device ID: {dev['device_id']}")

    def run_server():
        start_workers(resolve_cameras())      # also starts cloud services
        start_auto_arm_scheduler()
        start_lan_discovery_beacon()
        # host="0.0.0.0" (not "127.0.0.1") so the phone can reach this
        # server over LAN, same as app.py does - this used to be
        # "127.0.0.1" here, which silently broke local/offline mode for
        # anyone using the packaged/frozen (.exe) build specifically: the
        # server would still run, the desktop window/browser on THIS
        # machine would still work (since 127.0.0.1 always reaches
        # localhost), but the phone app would get a connection refused
        # trying to reach this laptop's LAN IP, with no obvious error
        # pointing at why. Fixed 2026-08-31 ahead of packaging the .exe
        # for the thesis defense. The desktop window/browser below still
        # correctly points at 127.0.0.1 for itself - only the SERVER's
        # bind address needed to change, not where the local UI looks.
        # threaded so the UI (window/browser) and the server run together;
        # use_reloader off so it doesn't try to spawn a second frozen process.
        app.run(host="0.0.0.0", port=5000, threaded=True,
                debug=False, use_reloader=False)

    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    # Open the window immediately on a self-contained loading page (a
    # data: URL, so it needs no server) that polls the real server and
    # auto-navigates once it responds. This replaces the old fixed
    # time.sleep(2.0) guess, which could open the real window before
    # Flask (and camera/model/cloud-service startup) was actually ready,
    # showing a permanent "can't reach this page" error with no retry.
    _open_console_window(_loading_url())


if __name__ == "__main__":
    main()
