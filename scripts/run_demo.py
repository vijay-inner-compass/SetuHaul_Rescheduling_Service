"""Start the demonstration console on this machine and open it in the browser.

    python scripts/run_demo.py            # http://127.0.0.1:8800/demo

Runs the real service on a fixed clock (2026-08-04 09:35 IST, the snapshot's day) with an in-process cache and no real
messaging channel. The assistant is the rule-based stand-in unless a model is configured in .env, in which case a
"Live model" switch appears on the page. Only this machine can reach the demo routes. Use synthetic data only.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def load_dotenv(path: Path) -> None:
    if os.environ.get("SETUHAUL_DOTENV") == "off" or not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")

    import uvicorn

    from setuhaul.demo_ui import build_demo_app
    from setuhaul.langsmith_trace import tracing_status
    from setuhaul.model_gateway import ModelUnavailable, build_gateway, load_model_policy

    policy = load_model_policy()
    live = None
    if policy.provider and policy.model_id and os.environ.get("SETUHAUL_MODEL_API_KEY"):
        try:
            gateway = build_gateway(policy)
            live = lambda: gateway
        except ModelUnavailable:
            live = None
    app, _deps = build_demo_app(live_factory=live, policy=policy if live else None)
    url = f"http://127.0.0.1:{args.port}/demo"
    print(f"SetuHaul demonstration console: {url}")
    print(f"  assistant: rule-based{' (live model available: ' + policy.model_id + ')' if live else ' (no live model configured)'}")
    print(f"  LangSmith tracing: {tracing_status().reason}")
    print("  Ctrl+C to stop. Data is synthetic and resets from the Reset button.")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
