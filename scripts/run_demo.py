"""Start the demonstration console on this machine and open it in the browser.

    python scripts/run_demo.py            # http://127.0.0.1:8800/demo

Runs the real service on a fixed clock (2026-08-04 09:35 IST, the snapshot's day) with an in-process cache and no real
messaging channel. The assistant is the rule-based stand-in unless a model is configured in .env, in which case a
"Live model" switch appears on the page. Only this machine can reach the demo routes. Use synthetic data only.
"""

# Lazy annotations.
from __future__ import annotations

# Command-line options.
import argparse

# Environment variables.
import os

# sys.path setup.
import sys

# A timer opens the browser shortly after the server starts.
import threading

# Opens the default browser.
import webbrowser

# Path arithmetic.
from pathlib import Path

# The repository root.
ROOT = Path(__file__).resolve().parents[1]
# Make src/ importable when run directly.
sys.path.insert(0, str(ROOT / "src"))


# Read KEY=VALUE lines from the git-ignored .env, without overriding real environment variables.
def load_dotenv(path: Path) -> None:
    # Skip if reading .env is switched off (tests do this) or there is no file.
    if os.environ.get("SETUHAUL_DOTENV") == "off" or not path.exists():
        return
    # Each line.
    for line in path.read_text(encoding="utf-8").splitlines():
        # Trim whitespace.
        line = line.strip()
        # Ignore blank lines and comments.
        if line and not line.startswith("#") and "=" in line:
            # Split into key and value.
            k, v = line.split("=", 1)
            # Set it only if not already set; remove surrounding quotes. Values are never printed.
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# Start the demo server.
def main() -> int:
    # Command-line options: --port and --no-browser.
    ap = argparse.ArgumentParser()
    # Port to listen on (default 8800).
    ap.add_argument("--port", type=int, default=8800)
    # Do not open the browser automatically.
    ap.add_argument("--no-browser", action="store_true")
    # Start with an empty dock board (no existing appointments) so every slot can be allocated from scratch.
    ap.add_argument("--fresh", action="store_true", help="start with an empty dock board")
    # Parse the command line.
    args = ap.parse_args()
    # Load configuration (provider, model id, keys) from .env.
    load_dotenv(ROOT / ".env")

    # The ASGI server, imported when needed.
    import uvicorn

    # The demo app builder.
    from setuhaul.demo_ui import build_demo_app

    # Reports whether LangSmith tracing is on.
    from setuhaul.langsmith_trace import tracing_status

    # Model configuration helpers.
    from setuhaul.model_gateway import ModelUnavailable, build_gateway, load_model_policy

    # Provider and model id from the environment.
    policy = load_model_policy()
    # No live model unless fully configured.
    live = None
    # All three settings present: a live model can be offered.
    if policy.provider and policy.model_id and os.environ.get("SETUHAUL_MODEL_API_KEY"):
        # Build the gateway.
        try:
            # Create the provider adapter.
            gateway = build_gateway(policy)
            # A factory returning it.
            live = lambda: gateway
        # Not usable: stay on the rule-based assistant.
        except ModelUnavailable:
            live = None
    # Build the demo app (fixed clock, fake cache, stub or live model).
    app, _deps = build_demo_app(live_factory=live, policy=policy if live else None, fresh=args.fresh)
    # Where the page will be.
    url = f"http://127.0.0.1:{args.port}/demo"
    # Tell the user where to go.
    print(f"SetuHaul demonstration console: {url}")
    # Which assistant is in use.
    print(f"  assistant: rule-based{' (live model available: ' + policy.model_id + ')' if live else ' (no live model configured)'}")
    # Whether tracing is on and why.
    print(f"  LangSmith tracing: {tracing_status().reason}")
    # How to stop it.
    print("  Ctrl+C to stop. Data is synthetic and resets from the Reset button.")
    # Open the page after a short delay, so the server is up first.
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # Serve on 127.0.0.1 only: the demo is not reachable from other machines.
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    # Clean exit.
    return 0


# Run main when executed as a script.
if __name__ == "__main__":
    raise SystemExit(main())
