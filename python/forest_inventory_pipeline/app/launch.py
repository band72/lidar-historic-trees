"""
Start the web app in-process.

Used by ``fip app`` and by the installed desktop program. Running Streamlit in-process
(instead of ``python -m streamlit run``) also works inside a frozen PyInstaller bundle,
where there is no separate Python interpreter to call.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

APP_SCRIPT = Path(__file__).with_name("streamlit_app.py")


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(preferred: int, attempts: int = 20) -> int:
    """``preferred`` if free, else the next free port above it."""
    for port in range(preferred, preferred + attempts):
        if _port_free(port):
            return port
    raise RuntimeError(f"no free port in {preferred}-{preferred + attempts - 1}")


def run_app(
    port: int = 8501,
    open_browser: bool = True,
    plugin_dirs: list[Path] | None = None,
    address: str = "127.0.0.1",
) -> None:
    """Block running the web app until interrupted (Ctrl+C or closing the window)."""
    from streamlit.web import bootstrap

    from ..plugins.registry import ENV_VAR
    from ..utils import logger

    if not APP_SCRIPT.exists():
        raise FileNotFoundError(f"app script missing: {APP_SCRIPT} (broken installation?)")
    if plugin_dirs:
        existing = os.environ.get(ENV_VAR)
        dirs = [str(Path(p).resolve()) for p in plugin_dirs] + ([existing] if existing else [])
        os.environ[ENV_VAR] = os.pathsep.join(dirs)

    port = pick_port(port)
    flags = {
        "server.port": port,
        "server.address": address,
        "server.headless": not open_browser,
        "browser.gatherUsageStats": False,
        "global.developmentMode": False,
        "server.fileWatcherType": "none",
        "server.maxUploadSize": 4096,
    }
    logger.info(f"LiDAR Tree Inventory running at http://{address}:{port}  (Ctrl+C to stop)")
    bootstrap.load_config_options(flag_options=flags)
    bootstrap.run(str(APP_SCRIPT), False, [], flags)


def main() -> None:
    """Entry point of the installed program: no arguments starts the web app, otherwise CLI."""
    import multiprocessing

    multiprocessing.freeze_support()
    if len(sys.argv) > 1:
        from ..fip import cli

        cli()
    else:
        run_app()
