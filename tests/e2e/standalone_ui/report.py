"""The run's log and reports: run.log (live, one timestamped line per event), report.json and report.html.

A case is FAIL when any of its checks failed, SKIP when all of its checks were skipped (or it has none), else PASS.
Checks added outside a case (Ctrl-C, a crash) go to the `_run` entry. The reports are rewritten after every case, so
a stopped run still leaves one. They hold the case texts, the bot's replies (truncated), the checks, the timings,
links to the failure screenshots and the meta data the caller gives (vars, config ids, agent image): never the
browser session.

Exit code: 0 no FAIL, 1 a FAIL, 2 the run could not start (config, session, data copy).
"""

from __future__ import annotations

import html
import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

STATUSES = ("PASS", "FAIL", "SKIP")
RUN_ENTRY = "_run"
REPLY_REPORT_CHARS = 2000
REPLY_LOG_CHARS = 300


@dataclass
class CaseRecord:
    id: str
    text: str = ""
    group: str = ""
    where: str = ""
    reply: str = ""
    checks: list = field(default_factory=list)
    seconds: Optional[float] = None
    screenshot: Optional[str] = None
    started: str = ""

    @property
    def status(self) -> str:
        states = {c.status for c in self.checks}
        if "FAIL" in states:
            return "FAIL"
        if not states or states == {"SKIP"}:
            return "SKIP"
        return "PASS"


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "..."


class RunLog:
    def __init__(self, dir: str, *, clock: Callable[[], float] = time.time,
                 echo: Optional[Callable[[str], None]] = None):
        self.dir = dir
        self.clock, self.echo = clock, echo
        self.cases: dict[str, CaseRecord] = {}
        self.meta: dict = {}
        self.current: Optional[str] = None
        self.started = self._stamp()
        os.makedirs(dir, exist_ok=True)
        self._log_path = os.path.join(dir, "run.log")
        open(self._log_path, "a", encoding="utf-8").close()

    # -- events --

    def _stamp(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.clock()))

    def _line(self, text: str) -> None:
        line = f"{self._stamp()} {text}"
        with open(self._log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
        if self.echo:
            self.echo(line)

    def _record(self, case_id: str) -> CaseRecord:
        if case_id not in self.cases:
            self.cases[case_id] = CaseRecord(case_id, started=self._stamp())
        return self.cases[case_id]

    def note(self, text: str) -> None:
        self._line(f"  .. {text}")

    def case_start(self, case_id: str, text: str, *, group: str = "", where: str = "") -> None:
        rec = self._record(case_id)
        rec.text, rec.group, rec.where = text, group, where
        self.current = case_id
        self._line(f"START {case_id} sent: {text!r}")

    def add(self, check) -> None:
        rec = self._record(self.current or RUN_ENTRY)
        rec.checks.append(check)
        self._line(f"  {check.status} {check.name}: {_one_line(check.detail, 400)}")

    def reply(self, case_id: str, texts) -> None:
        rec = self._record(case_id)
        joined = "\n".join(texts)
        rec.reply = (rec.reply + "\n" + joined).strip() if rec.reply else joined
        self._line(f"  reply {case_id}: {_one_line(joined, REPLY_LOG_CHARS) or '(nothing)'}")

    def screenshot(self, case_id: str, path: str) -> None:
        self._record(case_id).screenshot = path
        self._line(f"  screenshot {case_id}: {path}")

    def case_end(self, case_id: str, seconds: float) -> None:
        rec = self._record(case_id)
        rec.seconds = round(float(seconds), 1)
        self.current = None
        self._line(f"END {case_id} {rec.status} {rec.seconds:.1f}s")
        self.write()

    def set_meta(self, **kw) -> None:
        self.meta.update(kw)

    # -- results --

    def counts(self) -> dict:
        out = dict.fromkeys(STATUSES, 0)
        for rec in self.cases.values():
            out[rec.status] += 1
        return out

    def check_counts(self) -> dict:
        out = dict.fromkeys(STATUSES, 0)
        for rec in self.cases.values():
            for c in rec.checks:
                out[c.status] = out.get(c.status, 0) + 1
        return out

    def exit_code(self, *, setup_failed: bool = False) -> int:
        if setup_failed:
            return 2
        return 1 if self.counts()["FAIL"] else 0

    def summary_line(self) -> str:
        c = self.counts()
        return f"PASS {c['PASS']}  FAIL {c['FAIL']}  SKIP {c['SKIP']}  ({len(self.cases)} cases)"

    def _rel(self, path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        try:
            return os.path.relpath(path, self.dir).replace(os.sep, "/")
        except ValueError:  # another drive on Windows
            return path

    def as_dict(self) -> dict:
        return {
            "started": self.started,
            "written": self._stamp(),
            "summary": {"cases": self.counts(), "checks": self.check_counts()},
            "meta": self.meta,
            "cases": [{
                "id": r.id, "status": r.status, "group": r.group, "where": r.where, "started": r.started,
                "seconds": r.seconds, "sent": r.text, "reply": r.reply[:REPLY_REPORT_CHARS],
                "checks": [{"status": c.status, "name": c.name, "detail": c.detail} for c in r.checks],
                "screenshot": self._rel(r.screenshot),
            } for r in self.cases.values()],
        }

    def write(self) -> tuple[str, str]:
        data = self.as_dict()
        json_path = os.path.join(self.dir, "report.json")
        html_path = os.path.join(self.dir, "report.html")
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(render_html(data))
        return json_path, html_path


# -- the HTML page (self-contained: inline style and script, no external assets) --

_CSS = """
:root{--bg:#fff;--fg:#1d1c1d;--muted:#616061;--line:#ddd;--pass:#007a5a;--fail:#c0392b;--skip:#8a6d00}
@media (prefers-color-scheme:dark){:root{--bg:#1a1d21;--fg:#e8e8e8;--muted:#a0a0a0;--line:#383a3f;
--pass:#2bac76;--fail:#e8655a;--skip:#d4b106}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:16px;max-width:1200px}
h1{font-size:20px;margin:0 0 4px}.muted{color:var(--muted)}
.counts span{display:inline-block;margin-right:16px;font-weight:600}
.PASS{color:var(--pass)}.FAIL{color:var(--fail)}.SKIP{color:var(--skip)}
table{border-collapse:collapse;width:100%;margin-top:12px}td,th{border-top:1px solid var(--line);padding:6px;
vertical-align:top;text-align:left}pre{white-space:pre-wrap;word-break:break-word;margin:0;font-size:12px}
details summary{cursor:pointer}ul{margin:4px 0;padding-left:18px}
"""

_JS = """
function f(){var v=document.getElementById('status').value;
document.querySelectorAll('tr[data-status]').forEach(function(r){
r.style.display=(v==='ALL'||r.getAttribute('data-status')===v)?'':'none';});}
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_html(data: dict) -> str:
    counts = data["summary"]["cases"]
    meta = data.get("meta") or {}
    rows = []
    for c in data["cases"]:
        checks = "".join(f'<li class="{_e(k["status"])}">{_e(k["status"])} {_e(k["name"])}'
                         f'{": " + _e(k["detail"]) if k["detail"] else ""}</li>' for k in c["checks"])
        reply = c["reply"] or ""
        short = reply if len(reply) <= 300 else reply[:300] + "..."
        shot = f'<a href="{_e(c["screenshot"])}">screenshot</a>' if c["screenshot"] else ""
        seconds = "" if c["seconds"] is None else f'{c["seconds"]:.1f}s'
        rows.append(
            f'<tr data-status="{_e(c["status"])}"><td class="{_e(c["status"])}"><b>{_e(c["status"])}</b></td>'
            f'<td><b>{_e(c["id"])}</b><br><span class="muted">{_e(c["group"])} {_e(c["where"])}</span></td>'
            f'<td><pre>{_e(c["sent"])}</pre></td>'
            f'<td><details><summary><pre>{_e(short)}</pre></summary><pre>{_e(reply)}</pre></details></td>'
            f'<td><ul>{checks}</ul></td><td>{seconds}<br>{shot}</td></tr>')
    meta_rows = "".join(f"<li><b>{_e(k)}</b>: <pre>{_e(json.dumps(v, ensure_ascii=False, default=str))}</pre></li>"
                        for k, v in meta.items())
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>Pico FinOps Agent e2e</title><style>{_CSS}</style></head><body>"
        f"<h1>Pico FinOps Agent Slack e2e</h1><div class=\"muted\">started {_e(data['started'])}, "
        f"written {_e(data['written'])}</div>"
        f"<p class=\"counts\"><span class=\"PASS\">PASS {counts['PASS']}</span>"
        f"<span class=\"FAIL\">FAIL {counts['FAIL']}</span><span class=\"SKIP\">SKIP {counts['SKIP']}</span></p>"
        "<label>Show <select id=\"status\" onchange=\"f()\"><option>ALL</option><option>FAIL</option>"
        "<option>PASS</option><option>SKIP</option></select></label>"
        "<table><thead><tr><th>Status</th><th>Case</th><th>Sent</th><th>Reply</th><th>Checks</th><th>Time</th></tr>"
        f"</thead><tbody>{''.join(rows)}</tbody></table>"
        f"<details><summary>Run details</summary><ul>{meta_rows}</ul></details>"
        f"<script>{_JS}</script></body></html>")
