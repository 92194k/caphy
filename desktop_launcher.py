"""
desktop_launcher.py — CAPHY Desktop Application launcher.

Wraps the Flask app.py into a native window using pywebview. This is the
"double-click and it works" entry point for non-technical users.

The Flask backend runs in a background thread; pywebview opens a native
window pointing to http://127.0.0.1:5000.

On first run, device identity (device_id + device_secret) is generated and
persisted to %APPDATA%\\CAPHY\\device.json. This launcher can later be extended
to show a pairing screen with QR code, but for now it just boots the app.

Usage:
    python desktop_launcher.py       # launches the app window

PyInstaller packaging:
    pyinstaller --onefile --windowed --name CAPHY desktop_launcher.py
"""

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
import sys
import os

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
    started synchronously (before the webview window opens) is ready
    immediately, has no heavy imports, and needs no changes to the real
    Flask app or its auth guard."""
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


try:
    # The PyPI package is named "pywebview" but the importable module is
    # "webview" - installing pywebview and then writing `import pywebview`
    # is a common mixup that fails with ModuleNotFoundError even though
    # pip reports the install succeeded.
    import webview
except ImportError:
    print("ERROR: pywebview not installed. Run: pip install pywebview")
    sys.exit(1)

# Chdir into the same stable, per-user writable folder the packaged .exe
# uses (%LOCALAPPDATA%\CAPHY), BEFORE importing anything that reads
# config at import time (web.server imports config, which computes
# DB_PATH etc. as plain relative strings resolved against the CURRENT
# working directory). Doing this only inside "if __name__" would be too
# late - these imports already happen at module load time, above. Without
# this, running desktop_launcher.py from source (e.g. via VS Code) used
# the project folder itself as caphy.db's location, while the packaged
# .exe used %LOCALAPPDATA%\CAPHY - two completely different databases
# for what should be the exact same app.
from resource_path import use_writable_workdir
use_writable_workdir()

from identity import get_device_identity
from web.server import (app, start_workers, resolve_cameras,
                        start_auto_arm_scheduler, start_cloud_sync_retry,
                        start_lan_discovery_beacon)


def run_flask():
    """Run Flask in a background thread (non-blocking for the UI).

    start_workers() now also starts all the cloud services (registration,
    heartbeat, remote-command + pairing listener, WebRTC) internally - see
    web.server.start_cloud_services() - so this launcher no longer has to
    (and must not, to avoid double-starting) manage those threads itself.
    That centralization is deliberate: it's exactly what makes the laptop
    reachable over the internet even when CAPHY is started via app.py
    instead of this launcher."""
    start_workers(resolve_cameras())
    start_auto_arm_scheduler()
    start_cloud_sync_retry()
    start_lan_discovery_beacon()
    # host="0.0.0.0" (not "127.0.0.1") so the phone can reach this server
    # over LAN, matching app.py. This launcher used to bind 127.0.0.1
    # only, which silently defeated local/offline mode for anyone using
    # THIS launcher instead of app.py - the desktop window would work
    # fine (127.0.0.1 always reaches localhost), but the phone app would
    # get connection-refused reaching this laptop's LAN IP. Fixed
    # 2026-08-31 alongside the identical bug in caphy_desktop.py.
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False, use_reloader=False)


# NOTE: the cloud registration / heartbeat / remote-command / pairing /
# WebRTC threads used to live here. They now live in web.server
# (start_cloud_services, called from start_workers) so they run for EVERY
# entry point - app.py included - not just this launcher. Keeping them in
# one place also removes the risk of the two copies drifting (an earlier
# copy here posted the wrong arm-command body, {"armed": ...} instead of
# the {"on": ...} the /api/arm route actually expects).


def run_retention_cleanup():
    """
    Background loop: once a day, delete alerts (local files + Firebase
    copies) older than config.RETENTION_DAYS. Runs silently - no terminal
    window, no user action needed. Same tools/run_cleanup.py logic, just
    called as a function instead of a separate script.

    Runs one sweep shortly after startup (covers the case where the app
    wasn't open at all yesterday), then every 24 hours after that.
    """
    from tools.run_cleanup import cleanup_once

    # Small delay so this doesn't compete with camera/model startup.
    time.sleep(15)

    while True:
        try:
            cleanup_once()
        except Exception as e:
            # A cleanup failure should never take detection down.
            print(f"[CAPHY Desktop] Retention cleanup error (skipped): {e}")
        time.sleep(24 * 60 * 60)



class _WindowApi:
    """Exposed to the page's JS as `pywebview.api.*` so the custom in-page
    top bar (the window is frameless, so there is no native title bar /
    minimize / close button) can still minimize and close the app.
    window.minimize()/destroy() are stable pywebview calls present across
    3.x-5.x. Constructed with _window=None since the window object doesn't
    exist yet when create_window() needs the js_api instance; wired up
    with the real window right after create_window() returns, still
    before webview.start()."""
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


_WINDOW_OPENED_AT = [None]
_NORMAL_SESSION_SECS = 4.0


def on_window_close(window):
    """Shutdown when the window closes - but tell a real user close apart
    from a native WebView2 renderer crash (which Windows can trigger on an
    abrupt network/connectivity change and which no Python try/except can
    catch, since it kills the native process, not the Python one). A real
    user close is essentially never near-instant, so a session shorter than
    _NORMAL_SESSION_SECS is treated as a crash: relaunch instead of exiting,
    so "going offline" no longer makes the whole app disappear."""
    opened_at = _WINDOW_OPENED_AT[0]
    elapsed = (time.time() - opened_at) if opened_at else _NORMAL_SESSION_SECS
    if elapsed < _NORMAL_SESSION_SECS:
        print(f"[CAPHY] Window closed after only {elapsed:.1f}s - likely a "
              f"native crash (e.g. a network change), not a user close. "
              f"Relaunching...")
        try:
            import subprocess
            subprocess.Popen([sys.executable] + sys.argv)
        except Exception as e:
            print(f"[CAPHY] Auto-relaunch failed: {e}")
        os._exit(0)
    print("[CAPHY] Closing desktop app.")
    # In a real app, clean up workers/threads here if needed.
    sys.exit(0)


if __name__ == "__main__":
    # Load device identity on startup.
    device = get_device_identity()
    print(f"[CAPHY Desktop] Device ID: {device['device_id']}")
    print(f"[CAPHY Desktop] Hostname: {device['hostname']}")
    print("[CAPHY Desktop] Starting Flask...")

    # Start Flask in background.
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Start retention cleanup in background (silent, once a day - see
    # run_retention_cleanup() docstring). Daemon thread so it never blocks
    # the app from closing.
    cleanup_thread = threading.Thread(target=run_retention_cleanup, daemon=True)
    cleanup_thread.start()

    # Cloud registration, heartbeat, remote-command / pairing listener and
    # WebRTC are all started inside run_flask() -> start_workers() ->
    # start_cloud_services() now, so there's nothing extra to launch here.

    # Open the window immediately on a self-contained loading page,
    # served over a tiny local HTTP server on its own port (WebView2 does
    # not reliably navigate data: URLs - that showed up as a 404 instead
    # of the loading screen). The page polls the real Flask server and
    # auto-navigates once it responds. Replaces the old fixed time.sleep(2)
    # guess, which could open the window before Flask was actually ready,
    # showing a permanent "can't reach this page" error with no retry.
    print("[CAPHY Desktop] Opening window...")
    _start_loading_server()
    # frameless=True removes the native OS title bar (no OS "CAPHY Security
    # Console" bar - the custom top bar drawn in the HTML supplies the
    # logo, and minimize/maximize/close buttons call back into _WindowApi
    # below via js_api). resizable=True so the window can still be resized
    # and maximized/restored like a normal app - only drag-to-move by the
    # bar itself is intentionally left out, since that reliably needs
    # backend-specific JS that varies across pywebview versions, and a
    # broken drag right before the thesis defense is worse than a window
    # that opens centered and can't be dragged (it can still be resized
    # from its edges/corners, and maximized, normally).
    api = _WindowApi()
    window = webview.create_window(
        title="CAPHY Security Console",
        url="http://127.0.0.1:%d/" % _LOADING_PORT,
        width=1200,
        height=800,
        resizable=True,
        frameless=True,
        js_api=api,
    )
    api._window = window
    window.events.closed += on_window_close
    _WINDOW_OPENED_AT[0] = time.time()

    # debug=True opens DevTools inside the native window itself (right-click
    # -> Inspect, or it may open automatically depending on the platform's
    # WebView backend) - this is how to see real JS errors happening
    # inside the desktop app window, since pywebview otherwise swallows
    # them silently. Set back to False once everything works, so end
    # users never see a DevTools option.
    # private_mode=False is the actual fix for "signs me out every time I
    # close the app": pywebview 6.x defaults private_mode to True, which
    # explicitly means "cookies and local storage are not preserved" - a
    # fresh incognito-style browser profile every single launch, thrown
    # away on close. No server-side session setting (even a permanent
    # 30-day Flask session cookie) can survive that, because the cookie
    # storage itself never persists to disk in the first place.
    # storage_path pins WHERE that persistent profile lives.
    webview.start(debug=False, private_mode=False, storage_path=_webview_storage_path())
