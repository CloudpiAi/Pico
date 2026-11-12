"""One-time sign-in: open a visible browser, let the person sign in to Slack, save the session.

    python -m tests.e2e.standalone_ui.login [--env FILE] [--timeout-s 600]

Waits until the client page (app.slack.com/client/) loads, then saves Playwright's storage state to
cfg.state_file with mode 600 (it holds the Slack session cookie: treat it like a password).
"""

from __future__ import annotations

import argparse
import os
import sys

CLIENT_URL_GLOB = "**://app.slack.com/client/**"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m tests.e2e.standalone_ui.login", description=__doc__.splitlines()[0])
    p.add_argument("--env", default=None, help="E2E_* settings file (KEY=VALUE)")
    p.add_argument("--timeout-s", type=float, default=600.0, help="how long to wait for the sign-in (seconds)")
    return p.parse_args(argv)


def save_state(context, path: str) -> str:
    """Save context.storage_state to path, readable by the owner only (created 600 before anything is written)."""
    path = os.path.expanduser(path)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    context.storage_state(path=path)
    os.chmod(path, 0o600)
    return path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from playwright.sync_api import sync_playwright

    from tests.e2e.standalone_ui.config import load_config

    cfg = load_config(args.env)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(cfg.slack_url)
            print(f"Sign in to Slack in the browser window (waiting up to {args.timeout_s:.0f} s) ...")
            page.wait_for_url(CLIENT_URL_GLOB, timeout=args.timeout_s * 1000)
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(3000)  # let the client finish setting its cookies
            out = save_state(context, cfg.state_file)
            print(f"Saved the Slack session to {out} (mode 600).")
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
