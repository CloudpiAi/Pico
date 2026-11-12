"""Run the Slack end-to-end test of the standalone Pico FinOps Agent in the Slack web app (see README.md).

    python -m tests.e2e.standalone_ui.run --list
    python -m tests.e2e.standalone_ui.run [--env FILE] [--only GROUP]... [--case ID]... [--headed]

Settings come from --env (default: tests/e2e/standalone_ui/.env when present, else ~/.pico-e2e/e2e.env). The
browser reuses the saved session (E2E_STATE_FILE, made once with `python -m tests.e2e.standalone_ui.login`).
Each run writes run.log (live), report.json, report.html and failure screenshots to E2E_REPORT_DIR/<time>/.

Exit code: 0 nothing failed, 1 a check failed, 2 the run could not start (settings, session, data copy).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import os
import subprocess
import sys
import tempfile
import time
from typing import Callable, Optional

from tests.e2e.standalone_ui import catalog
from tests.e2e.standalone_ui.catalog import CatalogError
from tests.e2e.standalone_ui.checks import Check
from tests.e2e.standalone_ui.config import ConfigError, load_config
from tests.e2e.standalone_ui.ground_truth import Truth, snapshot
from tests.e2e.standalone_ui.page import SignedOut
from tests.e2e.standalone_ui.report import RunLog
from tests.e2e.standalone_ui.runner import Runner

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CATALOG = os.path.join(HERE, "catalog.yaml")
LOGIN_HINT = "sign in again: python -m tests.e2e.standalone_ui.login"
_NOT_IN_REPORT = ("state_file",)  # a path to the cookies; the report never names or copies it


def default_env(here: str = HERE, home: Optional[str] = None) -> Optional[str]:
    """tests/e2e/standalone_ui/.env when present, else ~/.pico-e2e/e2e.env when present, else None."""
    local = os.path.join(here, ".env")
    if os.path.isfile(local):
        return local
    user = os.path.join(home or os.path.expanduser("~"), ".pico-e2e", "e2e.env")
    return user if os.path.isfile(user) else None


@contextlib.contextmanager
def launch_browser(cfg, *, headless: bool):
    """Chromium with the saved session; yields a SlackPage."""
    from playwright.sync_api import sync_playwright

    from tests.e2e.standalone_ui.page import SlackPage

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            context = browser.new_context(storage_state=cfg.state_file, viewport={"width": 1400, "height": 900})
            yield SlackPage(context.new_page(), cfg)
        finally:
            browser.close()


def agent_image(container: Optional[str], docker: Callable = subprocess.run) -> str:
    if not container:
        return "unknown (no E2E_AGENT_CONTAINER)"
    try:
        proc = docker(["docker", "inspect", "--format", "{{.Config.Image}} {{.Image}}", container],
                      capture_output=True, text=True, timeout=30)
    except Exception as e:  # no docker on this machine: the run goes on
        return f"unknown ({type(e).__name__}: {e})"
    if proc.returncode != 0:
        return f"unknown ({(proc.stderr or '').strip()[:200]})"
    return (proc.stdout or "").strip() or "unknown"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tests.e2e.standalone_ui.run",
                                description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--env", help="settings file (default: tests/e2e/standalone_ui/.env, else ~/.pico-e2e/e2e.env)")
    p.add_argument("--only", action="append", default=[], choices=catalog.GROUPS, help="run one group (repeatable)")
    p.add_argument("--case", action="append", default=[], help="run one case id (repeatable)")
    p.add_argument("--list", action="store_true", help="print the planned cases; needs no browser or session")
    p.add_argument("--headed", action="store_true", help="show the browser")
    return p


def _config_ids(cfg) -> dict:
    data = dataclasses.asdict(cfg) if dataclasses.is_dataclass(cfg) else dict(vars(cfg))
    return {k: v for k, v in data.items() if k not in _NOT_IN_REPORT}


def main(argv=None, *, launch: Optional[Callable] = None, truth_factory: Callable = Truth,
         snapshot_fn: Callable = snapshot, docker: Callable = subprocess.run,
         catalog_path: str = DEFAULT_CATALOG) -> int:
    a = _parser().parse_args(argv)
    try:
        cases = catalog.select(catalog.load(catalog_path), a.only, a.case)
    except (CatalogError, OSError) as e:
        print(f"catalog: {e}")
        return 2
    if a.case and not cases:
        print(f"no case with id {', '.join(a.case)} (see --list)")
        return 2
    if a.list:
        for c in cases:
            print(f"{c.group:9} {c.id:28} {c.where:8} {' '.join((c.text or c.action).splitlines())}")
        print(f"\n{len(cases)} cases; channel cases repeat for each of E2E_TEST_CHANNELS")
        return 0

    env_file = a.env or default_env()
    try:
        cfg = load_config(env_file)
    except ConfigError as e:
        print(f"settings ({env_file or 'environment'}): {e}")
        return 2
    if not os.path.isfile(cfg.state_file):
        print(f"no saved Slack session at {cfg.state_file}: {LOGIN_HINT}")
        return 2

    truth = None
    try:
        path = cfg.data_copy
        if not path:
            if not cfg.agent_container:
                print("set E2E_AGENT_CONTAINER (copied with docker cp) or E2E_DATA_COPY")
                return 2
            path = snapshot_fn(cfg.agent_container, os.path.join(tempfile.mkdtemp(prefix="pico-e2e-"),
                                                                 "focus.duckdb"))
        truth = truth_factory(path)
        vars_ = dict(truth.vars())
    except Exception as e:  # SnapshotError, a bad copy, ...
        print(f"data copy failed: {type(e).__name__}: {e}")
        if truth:
            truth.close()
        return 2
    vars_.update({k: v for k, v in (("me", cfg.me), ("outside_channel", cfg.outside_channel)) if v})

    run_dir = os.path.join(cfg.report_dir, time.strftime("%Y%m%d-%H%M%S"))
    log = RunLog(run_dir, echo=print)
    log.set_meta(vars=vars_, config=_config_ids(cfg), agent_image=agent_image(cfg.agent_container, docker),
                 env_file=env_file, selected={"only": a.only, "case": a.case})
    log.note(f"agent image: {log.meta['agent_image']}; channels: {', '.join(cfg.test_channels) or 'none'}")
    setup_failed = False
    try:
        with (launch or launch_browser)(cfg, headless=cfg.headless and not a.headed) as page:
            Runner(page, cfg, truth, vars_, log).run(cases)
    except KeyboardInterrupt:
        log.add(Check("FAIL", "run", "stopped with Ctrl-C (see the clean-up lines)"))
    except SignedOut as e:
        setup_failed = True
        log.note(f"signed out: {e}: {LOGIN_HINT}")
        print(f"the Slack session has expired: {LOGIN_HINT}")
    except Exception as e:  # the browser didn't start, ...
        setup_failed = True
        log.add(Check("FAIL", "run", f"{type(e).__name__}: {e}"))
    finally:
        truth.close()
        _, html_path = log.write()
    print(log.summary_line())
    print(f"report: {html_path}")
    return log.exit_code(setup_failed=setup_failed)


if __name__ == "__main__":
    sys.exit(main())
