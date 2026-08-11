#!/usr/bin/env python3
# Native-window launcher for the Crypto DCA Tracker.
# Runs the backend (server.py) in a background thread and shows the UI in a
# real macOS window via pywebview. Closing the window (or Cmd+Q) shuts the
# server down in-process, so there is nothing left running afterward.
import os
import threading

import webview

import server

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def set_dock_icon():
    try:
        from AppKit import NSApplication, NSImage

        icon_path = os.path.join(SCRIPT_DIR, "icon.svg")
        image = NSImage.alloc().initWithContentsOfFile_(icon_path)
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        pass


def main():
    set_dock_icon()
    try:
        httpd = server.build_server()
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]
    except OSError:
        # Already running (e.g. a second launch) - reuse the existing server
        # and skip owning its lifecycle, so closing this window won't stop it.
        httpd = None
        port = server.PORT

    window = webview.create_window(
        "Crypto DCA Tracker",
        f"http://127.0.0.1:{port}/index.html",
        width=1400,
        height=900,
        min_size=(900, 600),
    )

    if httpd is not None:
        window.events.closed += httpd.shutdown

    webview.start()

    if httpd is not None:
        httpd.server_close()


if __name__ == "__main__":
    main()
