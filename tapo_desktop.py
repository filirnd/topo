#!/usr/bin/env python3
"""GTK/WebKit desktop shell for the cloud-only Tapo viewer."""

from __future__ import annotations

import argparse
import sys
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gio", "2.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gio, Gtk, WebKit2  # noqa: E402

from tapo_cloud_app import PRODUCT_NAME, CloudAppContext, Handler  # noqa: E402

ROOT = Path(__file__).resolve().parent
ICON_PATH = ROOT / "tapo-cloud-viewer.png"
if not ICON_PATH.exists():
    ICON_PATH = ROOT / "linux" / "tapo-cloud-viewer.png"


class TapoDesktop(Gtk.Application):
    def __init__(self, *, port: int, fresh_login: bool):
        super().__init__(
            application_id="io.github.filirnd.topo",
            flags=0,
        )
        self.requested_port = port
        self.fresh_login = fresh_login
        self.context: CloudAppContext | None = None
        self.server: ThreadingHTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self.window: Gtk.ApplicationWindow | None = None

    def do_activate(self) -> None:  # noqa: N802 - GTK virtual method
        if self.window is not None:
            self.window.present()
            return
        try:
            self.context = CloudAppContext(self.fresh_login)
            handler_class = type("DesktopAppHandler", (Handler,), {"context": self.context})
            self.server = ThreadingHTTPServer(("127.0.0.1", self.requested_port), handler_class)
            port = self.server.server_address[1]
            self.server_thread = threading.Thread(
                target=self.server.serve_forever,
                name="tapo-desktop-http",
                daemon=True,
            )
            self.server_thread.start()
        except (OSError, RuntimeError) as exc:
            print(f"Failed to start {PRODUCT_NAME}: {exc}", file=sys.stderr)
            self.quit()
            return

        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title(PRODUCT_NAME)
        if ICON_PATH.exists():
            self.window.set_icon_from_file(str(ICON_PATH))
        self.window.set_default_size(1280, 820)
        self.window.connect("destroy", lambda *_args: self.quit())

        webview = WebKit2.WebView()
        settings = webview.get_settings()
        settings.set_property("enable-developer-extras", False)
        webview.connect("decide-policy", self._handle_navigation)
        # The UI is served locally, but WebKit may otherwise reuse an older
        # cached index.html after an application update.
        webview.load_uri(f"http://127.0.0.1:{port}/?ui={uuid.uuid4().hex}")
        self.window.add(webview)
        self.window.show_all()

    @staticmethod
    def _handle_navigation(_webview, decision, decision_type):
        """Open external project links in the user's default browser."""
        if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            action = decision.get_navigation_action()
            request = action.get_request() if action is not None else None
            uri = request.get_uri() if request is not None else ""
            if uri.startswith(("http://", "https://")) and not uri.startswith("http://127.0.0.1:"):
                Gio.AppInfo.launch_default_for_uri(uri, None)
                decision.ignore()
                return True
        decision.use()
        return False

    def do_shutdown(self) -> None:  # noqa: N802 - GTK virtual method
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.context is not None:
            self.context.close()
            self.context = None
        Gtk.Application.do_shutdown(self)


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{PRODUCT_NAME} desktop app")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="local HTTP port; 0 selects a free port automatically",
    )
    parser.add_argument(
        "--fresh-login",
        action="store_true",
        help="force a new Tapo login and MFA flow",
    )
    args = parser.parse_args()
    application = TapoDesktop(
        port=args.port,
        fresh_login=args.fresh_login,
    )
    return application.run([])


if __name__ == "__main__":
    raise SystemExit(main())
