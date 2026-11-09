#!/usr/bin/env python3
"""
Glass Dashboard — the project's own web dashboard, run as a separate app.

    python3 glass_server.py          (or double-click run_glass_dashboard.command)
    → http://127.0.0.1:8780/

It does not open or need the Tk launcher window. It loads the strategy engine from
Options-angel_live-ok.py (as web_server.py does), logs in to Angel One, and runs the launcher's
control logic without a window (headless_tk.py), so the scanner, reversal rows, filters,
next-day plan and order engine behave exactly as in the launcher. Nothing is copied or changed.

Run it instead of the launcher, not next to it: both would stream the same feed and write the
same sheet and order files. While the launcher is running, START and AUTO-TRADE here are refused.

Settings: GLASS_DASHBOARD_PORT (default 8780), GLASS_NO_BROWSER=1 (don't open the browser).
"""

import importlib.util
import os
import sys
import threading
import time
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

import headless_tk  # noqa: E402

tk = headless_tk.install()          # before the engine imports tkinter


def load_engine():
    spec = importlib.util.spec_from_file_location("pivot_engine", os.path.join(BASE_DIR, "Options-angel_live-ok.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["pivot_engine"] = m
    spec.loader.exec_module(m)
    return m


def angel_login(m):
    """Log in like the launcher does; on failure keep the dashboard up and retry every minute."""
    try:
        m.Session.login()
        print("   ✅ Angel One login OK")
        return True
    except Exception as e:
        print(f"   ⚠️  Angel One login failed: {e} (retrying every minute)")

    def retry():
        while m.Session.auth_token is None:
            time.sleep(60)
            try:
                m.Session.login()
                print("   ✅ Angel One login OK")
            except Exception as e:
                print(f"   ⚠️  Angel One login retry failed: {e}")
    threading.Thread(target=retry, daemon=True).start()
    return False


def load_scrip(m):
    try:
        m.Session.load_scrip()
        return m.Session.scrip_df is not None
    except Exception as e:
        print(f"   ⚠️  Instrument list not loaded: {e} (retrying every 30 s)")
        return False


def main():
    print("Loading the strategy engine...")
    m = load_engine()
    print("Logging in to Angel One...")
    angel_login(m)
    print("Loading instrument list (cached once per day)...")
    scrip_ok = load_scrip(m)

    import glass_dashboard
    root = tk.Tk()
    app = m.ControlPanel(root)        # the launcher's controls and logic, no window
    if not scrip_ok:
        def retry_scrip():
            while not load_scrip(m):
                time.sleep(30)
            app.root.after(0, app._on_exchange_change)     # fill symbols and expiries once it is in
        threading.Thread(target=retry_scrip, daemon=True).start()
    srv = glass_dashboard.start(app, m)
    if srv is None:
        print("Glass dashboard could not start (see above).")
        sys.exit(1)
    headless_tk.SINK.append(lambda kind, title, msg: srv.dash.note(f"{title}: {msg}" if title else msg,
                                                                   "error" if kind in ("error", "warning") else "info"))
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    if os.getenv("GLASS_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"\n✨ Glass dashboard ready: {url}   (Ctrl+C to stop)\n")
    seconds = os.getenv("GLASS_RUN_SECONDS")       # used by the demo/test only
    if seconds:
        root.after(int(seconds) * 1000, root.quit)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        try:
            m.feed.running = False
            m.feed.stop_ws()
        except Exception:
            pass
        srv.shutdown()


if __name__ == "__main__":
    main()
