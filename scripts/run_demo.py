"""Start the demonstration console on this machine and open it in the browser.

    python scripts/run_demo.py               # open http://127.0.0.1:8800/ and sign in (Google or the backup admin password)
    python scripts/run_demo.py --auto-login  # no authentication: /demo/login signs you in as a demo warehouse user

Runs the real service on a fixed clock (2026-08-04 09:35 IST, the snapshot's day) with an in-process cache and no real
messaging channel. The assistant is the rule-based stand-in unless a model is configured in .env, in which case a
"Live model" switch appears on the page. Only this machine can reach the demo routes. Use synthetic data only.
"""

# Lazy annotations.
from __future__ import annotations

# Command-line options.
import argparse

# Typing the backup password without showing it.
import getpass

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
    # Skip authentication for a quick demonstration.
    ap.add_argument("--auto-login", action="store_true", help="no authentication: /demo/login signs you in as a demo warehouse user")
    # Parse the command line.
    args = ap.parse_args()
    # Load configuration (provider, model id, keys) from .env.
    load_dotenv(ROOT / ".env")

    # The ASGI server, imported when needed.
    import uvicorn

    # The demo app builder.
    from setuhaul.demo_ui import build_demo_app, sign_in_from_env

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
        # Not usable: the chat stays switched off.
        except ModelUnavailable:
            live = None
    # The real sign-in settings, from .env or the environment.
    signing = sign_in_from_env(os.environ)
    # The backup password is typed here and kept only for this run (the demo database is rebuilt every start).
    if signing.local_admin and signing.admin_email:
        if sys.stdin.isatty():
            first = getpass.getpass(f"Backup password for {signing.admin_email} (12 characters or more, Enter to skip): ")
            if len(first) >= 12 and getpass.getpass("Type it again: ") == first:
                signing.admin_password = first
            elif first:
                print("  Passwords differ or are too short: the backup login has no password this run.")
        else:
            print("  No terminal to type the backup password in: the backup login has no password this run.")
    # Build the demo app (fixed clock, fake cache, the configured model; chat is off without one).
    app, _deps = build_demo_app(live_factory=live, policy=policy if live else None, fresh=args.fresh, sign_in=signing, auto_login=args.auto_login)
    base = f"http://127.0.0.1:{args.port}"
    url = f"{base}/demo/login" if args.auto_login else f"{base}/"
    # Tell the user where to go.
    print(f"SetuHaul demonstration: open {url}" + (" (no authentication: --auto-login)" if args.auto_login else f" to sign in, then {base}/demo"))
    if not args.auto_login:
        print(f"  Google sign-in: {'on, redirect URI ' + base + '/auth/callback' if signing.google else 'not configured'}")
        print(f"  Backup admin password: {'set for ' + signing.admin_email if signing.admin_password else 'not set'}")
        if not signing.admin_email:
            print("  Set SETUHAUL_BOOTSTRAP_ADMIN_EMAIL in .env, or nobody is allowed to sign in.")
    # Which assistant is in use.
    print(f"  assistant model: {policy.model_id}" if live else "  NO MODEL CONFIGURED: the driver chat is switched off. Set SETUHAUL_MODEL_PROVIDER, SETUHAUL_MODEL_ID and SETUHAUL_MODEL_API_KEY in .env and restart.")
    # Whether tracing is on and why.
    print(f"  LangSmith tracing: {tracing_status().reason}")
    # How to stop it.
    print("  The driver console is read-only: driver messages come from Telegram.")
    print(f"  To send messages as a demo driver, open {base}/demo/driver-login (add ?driver=DRV007 for another driver).")
    print("  Ctrl+C to stop. Data is synthetic and resets from the Reset button.")
    # Open the page after a short delay, so the server is up first.
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # Serve on 127.0.0.1 only: the demo is not reachable from other machines.
    # proxy_headers off and no trusted forwarders: X-Forwarded-For can never make a remote client look like loopback.
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", proxy_headers=False, forwarded_allow_ips="")
    # Clean exit.
    return 0


# Run main when executed as a script.
if __name__ == "__main__":
    raise SystemExit(main())
