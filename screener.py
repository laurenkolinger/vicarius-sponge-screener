"""Start Sponge Screener: serve the app on 127.0.0.1 and open it in Chrome.

Run ``python3 screener.py``. Options set the port, the data and cache
folders, and the bucket URL, and ``--no-browser`` leaves the browser closed.
The first line printed is the URL the server listens on. Ctrl+C stops the
server, the converter, and the relay in that order.
"""

import argparse
import signal
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from screener import config  # noqa: E402 - the path insert above must come first
from screener.convert import ConvertError  # noqa: E402
from screener.relay import RelayError  # noqa: E402
from screener.server import ScreenerApp, make_server  # noqa: E402
from screener.store import StoreError  # noqa: E402

CHROME_APP = Path("/Applications/Google Chrome.app")
CHROME_NAME = "Google Chrome"
OPEN_COMMAND = "open"
MAX_PORT = 65535
EXIT_OK = 0
EXIT_FAILED = 1
STARTUP_ERRORS = (OSError, ValueError, StoreError, RelayError, ConvertError)


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    """Read the command line.

    Args:
        argv: The arguments after the program name, or None for sys.argv.

    Returns:
        The options: port, no_browser, data_dir, cache_dir, bucket_url.

    Raises:
        SystemExit: With code 2 for an unknown option or a port outside 0 to 65535.
    """
    parser = argparse.ArgumentParser(
        prog="screener.py",
        description="Serve Sponge Screener on 127.0.0.1 and open it in Google Chrome.",
    )
    parser.add_argument("--port", type=int, default=config.PORT, help=f"port to listen on (default {config.PORT}; 0 picks a free port)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    parser.add_argument("--data-dir", type=Path, default=config.data_dir(), help=f"folder for sightings, images, and settings (default {config.data_dir()})")
    parser.add_argument("--cache-dir", type=Path, default=config.cache_dir(), help=f"folder for video chunks and converted files (default {config.cache_dir()})")
    parser.add_argument("--bucket-url", default=config.BUCKET_URL, help=f"base URL of the S3 bucket (default {config.BUCKET_URL})")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= MAX_PORT:
        parser.error(f"--port: expected a number from 0 to {MAX_PORT}, got {args.port}")
    return args


def open_browser(url: str) -> None:
    """Open the app in Google Chrome when it is installed, else in the default browser.

    A browser that cannot be started is reported on stderr and the server
    keeps running, because the URL is already printed.

    Args:
        url: The app URL.
    """
    try:
        if CHROME_APP.exists():
            subprocess.Popen([OPEN_COMMAND, "-a", CHROME_NAME, url], stdin=subprocess.DEVNULL)
        else:
            webbrowser.open(url)
    except OSError as error:
        sys.stderr.write(f"screener: could not open a browser ({error}); open {url} yourself\n")


def _request_stop(signal_number: int, frame: Any) -> None:
    """Turn SIGTERM (a plain ``kill``, or a closed terminal window) into the Ctrl+C path.

    Args:
        signal_number: The signal received.
        frame: The interrupted stack frame, unused.

    Raises:
        KeyboardInterrupt: Always, so serve_forever ends and the app closes.
    """
    raise KeyboardInterrupt


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Start the server, print its URL, open the browser, and serve until Ctrl+C or SIGTERM.

    Args:
        argv: The command line arguments after the program name, or None for sys.argv.

    Returns:
        0 after a clean stop, 1 when the app or the server could not start.

    Raises:
        SystemExit: From argument parsing.
    """
    args = parse_args(argv)
    signal.signal(signal.SIGTERM, _request_stop)
    try:
        app = ScreenerApp(args.data_dir, args.cache_dir, config.config_dir(), config.static_dir(), args.bucket_url, args.port)
    except STARTUP_ERRORS as error:
        sys.stderr.write(f"screener: could not start: {error}\n")
        return EXIT_FAILED
    try:
        server = make_server(app, config.HOST, args.port)
    except OSError as error:
        sys.stderr.write(
            f"screener: could not listen on {config.HOST}:{args.port}: {error}. "
            "Is another Sponge Screener running? Stop it, or pass --port with another number.\n"
        )
        app.close()
        return EXIT_FAILED
    url = f"http://{config.HOST}:{app.port}/"
    print(f"Sponge Screener listening on {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        open_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Sponge Screener.", flush=True)
    finally:
        server.server_close()
        app.close()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
