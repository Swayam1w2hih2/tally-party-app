"""
Tally Party App - Windows launcher
----------------------------------
This is what the installed program runs (TallyPartyApp.exe).

    TallyPartyApp.exe               start (if needed) and open the app window
    TallyPartyApp.exe --background  start quietly without a window (used at Windows startup)

The app opens in its own window using Microsoft Edge's app mode, which is part of
Windows 10 and 11. If Edge is not found, it opens in the default browser.
Settings and data are kept in %APPDATA%\\TallyPartyApp.
"""
import os
import socket
import subprocess
import sys
import time
import webbrowser


def _quiet_output():
    """A windowed .exe has no console, so print() would fail. Send output to a log file."""
    if sys.stdout is None or sys.stderr is None or getattr(sys, "frozen", False):
        folder = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "TallyPartyApp", "data")
        os.makedirs(folder, exist_ok=True)
        f = open(os.path.join(folder, "app-log.txt"), "a", encoding="utf-8", buffering=1)
        sys.stdout = f
        sys.stderr = f


_quiet_output()

import app  # noqa: E402  (after output is set up)


def is_running(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
        if os.path.exists(p):
            return p
    return None


def open_window(url):
    edge = find_edge()
    if edge:
        profile = os.path.join(app.USER_DIR, "window")
        try:
            subprocess.Popen([edge, "--app=" + url, "--window-size=1280,860",
                              "--user-data-dir=" + profile, "--no-first-run",
                              "--disable-features=Translate"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return
        except OSError:
            pass
    webbrowser.open(url)


def main():
    background = "--background" in sys.argv
    port = int(app.load_config().get("app_port", 8050))
    url = "http://localhost:%d" % port

    if is_running(port):
        if not background:
            open_window(url)
        return

    server, url = app.start_server()
    if server is None:                      # someone else grabbed the port
        if not background:
            open_window(url)
        return
    app.log("Tally Party App started%s." % (" in the background" if background else ""))
    if not background:
        # give the server a moment, then open the window
        def later():
            time.sleep(0.8)
            open_window(url)
        import threading
        threading.Thread(target=later, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
