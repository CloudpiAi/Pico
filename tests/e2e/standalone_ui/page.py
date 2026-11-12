"""Read and drive the Slack web app with Playwright (sync API). See CONTRACTS.md "page.py".

Every selector lives in the constants below: when Slack changes its markup, fix it here. A selector that finds
nothing raises PageError naming it; the sign-in page raises SignedOut.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

    from tests.e2e.standalone_ui.config import Config

# --- selectors (recorded from the live Slack web app 2026-11-11 unless marked "not recorded") --------------------
MESSAGE_ITEM = '[data-qa="virtual-list-item"][id^="message-list_"]'  # data-item-key = ts; day dividers too
MESSAGE_CONTAINER = '[data-qa="message_container"]'  # absent on day dividers
SENDER_NAME = '[data-qa="message_sender_name"]'  # absent on a follow-on message from the same sender
APP_BADGE = '[data-qa="message_sender_app_badge"]'
TS_LINK = "a[data-ts]"
PROFILE_LINK = 'a[href*="/team/U"]'
MESSAGE_TEXT = '[data-qa="message-text"]'
REACTJI = '[data-qa="reactji"]'
COMPOSER = '[data-qa="message_input"] [data-qa="texty_input"]'
MESSAGE_PANE = '[data-qa="message_pane"]'  # not recorded; screenshot falls back to the list (role=list)
MESSAGE_LIST = '[role="list"]'
THREAD_PANE = '[data-qa="threads_flexpane"]'  # not recorded
THREAD_COMPOSER = THREAD_PANE + ' [data-qa="texty_input"]'  # not recorded
REPLY_BAR = '[data-qa="reply_bar_count"]'  # not recorded: "N replies" under a message
START_THREAD = '[data-qa="start_thread"]'  # not recorded: hover action "Reply in thread"
SIDEBAR_ITEM = "[data-qa-channel-sidebar-channel-id]"  # not recorded
SIDEBAR_ID_ATTR = "data-qa-channel-sidebar-channel-id"
SIGNIN_FORM = (  # not recorded: any of the sign-in page's forms/inputs
    '#signin_form, [data-qa="signin_form"], [data-qa="login_email"], [data-qa="signin_email_input"]'
)
SIGNIN_URL = re.compile(r"^https://([a-z0-9-]+\.)*slack\.com/(signin|sign_in|workspace-signin|ssb/signin|check-login)")

CLIENT_URL = "https://app.slack.com/client/{team}/{channel}"
TS_RE = re.compile(r"^\d+\.\d+$")
_ID_RE = re.compile(r"^[A-Z0-9]{3,32}$")
_MEMBER_RE = re.compile(r"/team/(U[A-Z0-9]+)")
_REACT_RE = re.compile(r"react(?:ed)? with (.+?) emoji", re.I)
_EMOJI_RE = re.compile(r"^(.+?) emoji$", re.I)
_FOOTER_RE = re.compile(r"\btask\s+(t\d+)\s*·\s*say\s+['‘’\"]?stop task\s+\1\b", re.I)


@dataclass(frozen=True)
class Msg:
    ts: str
    sender: str
    sender_id: str | None
    is_bot: bool
    text: str
    reactions: tuple[str, ...]  # emoji names: "white_check_mark", "hourglass_flowing_sand", "x", ...
    task_id: str | None  # from the scheduled-post footer, else None


class PageError(Exception):
    """A selector or navigation problem; the message names the selector."""


class SignedOut(PageError):
    """The Slack session is gone (the sign-in page is shown)."""


def ts_key(ts: str) -> tuple[int, int]:
    sec, _, frac = ts.partition(".")
    return int(sec), int(frac.ljust(6, "0")[:6] or 0)


def reaction_name(label: str) -> str | None:
    """'1 reaction, react with white check mark emoji' -> 'white_check_mark'."""
    label = (label or "").strip()
    m = _REACT_RE.search(label) or _EMOJI_RE.match(label)
    if not m:
        return None
    return re.sub(r"\s+", "_", m.group(1).strip().lower())


def task_id_of(text: str) -> str | None:
    m = _FOOTER_RE.search(text or "")
    return m.group(1) if m else None


# Reads every message container in scope -> raw rows; Python turns them into Msg.
_READ_JS = """
([sel, scopeSel]) => {
  const scope = scopeSel ? document.querySelector(scopeSel) : document;
  if (!scope) return null;
  const tsOk = (v) => !!v && /^\\d+\\.\\d+$/.test(v);
  const rows = [];
  for (const c of scope.querySelectorAll(sel.container)) {
    if (c.parentElement && c.parentElement.closest(sel.container)) continue;  // nested (shared) message
    const item = c.closest(sel.item);
    if (!scopeSel) {
      if (!item || c.closest(sel.thread)) continue;  // main list only
    }
    const link = c.querySelector(sel.tsLink);
    const cands = [item && item.getAttribute('data-item-key'), c.getAttribute('data-msg-ts'),
                   link && link.getAttribute('data-ts')];
    const anyItem = c.closest('[data-item-key]');
    if (anyItem) cands.push(anyItem.getAttribute('data-item-key'));
    const ts = cands.find(tsOk);
    if (!ts) continue;
    const name = c.querySelector(sel.sender);
    // the sender's profile link, never an @mention link inside the message text (live 2026-11-11)
    const prof = Array.from(c.querySelectorAll(sel.profile)).find((a) => !a.closest(sel.text)) || null;
    const text = c.querySelector(sel.text);
    rows.push({
      ts,
      name: name ? name.innerText.trim() : null,
      badge: !!c.querySelector(sel.badge),
      profile: prof ? prof.getAttribute('href') : null,
      text: text ? text.innerText.trim() : '',
      reactions: Array.from(c.querySelectorAll(sel.reactji)).map((r) => {
        const img = r.querySelector('[aria-label]');
        return [r.getAttribute('aria-label') || '', img ? img.getAttribute('aria-label') || '' : ''];
      }),
    });
  }
  return rows;
}
"""

# Scrolls the scrollable ancestor of the message items to the bottom, then waits two frames.
_SCROLL_JS = """
([itemSel, scopeSel]) => new Promise((resolve) => {
  const scope = scopeSel ? document.querySelector(scopeSel) : document;
  const first = scope && scope.querySelector(itemSel);
  let el = first ? first.parentElement : null;
  while (el && el !== document.body) {
    const s = getComputedStyle(el);
    if (/(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight) { el.scrollTop = el.scrollHeight; break; }
    el = el.parentElement;
  }
  requestAnimationFrame(() => requestAnimationFrame(() => resolve(true)));
})
"""

_SIDEBAR_JS = """
([sel, attr, name]) => {
  for (const a of document.querySelectorAll(sel)) {
    const leaves = [a, ...a.querySelectorAll('*')];
    if (leaves.some((e) => (e.textContent || '').trim() === name && e.children.length === 0) ||
        (a.innerText || '').split('\\n')[0].trim() === name) {
      return a.getAttribute(attr);
    }
  }
  return null;
}
"""


MAX_ENTERS = 3  # Enter presses per send: an autocomplete may take one (live 2026-11-11, "#cost-reports")
ENTER_RETRY_S = 1.0  # wait this long before pressing Enter again while the text is still in the composer


def _norm(text: str) -> str:
    return " ".join((text or "").split())


class SlackPage:
    def __init__(
        self,
        page: "Page",
        cfg: "Config",
        *,
        poll_s: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        ui_timeout_s: float = 60.0,
    ):
        self.page = page
        self.cfg = cfg
        self.poll_s = poll_s
        self._sleep = sleep
        self._clock = clock
        self.ui_timeout_s = ui_timeout_s

    # ------------------------------------------------------------------ session / navigation

    def _check_signed_in(self) -> None:
        if SIGNIN_URL.match(self.page.url or "") or self.page.locator(SIGNIN_FORM).count() > 0:
            raise SignedOut(f"Slack shows the sign-in page ({self.page.url}); run login.py again")

    def _goto(self, url: str) -> None:
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.ui_timeout_s * 1000)
        except Exception as e:  # playwright Error / TimeoutError
            raise PageError(f"could not load {url}: {e}") from e

    def _wait_any(self, selector: str, what: str) -> None:
        try:
            self.page.wait_for_selector(f"{selector}, {SIGNIN_FORM}", timeout=self.ui_timeout_s * 1000)
        except Exception as e:
            self._check_signed_in()
            raise PageError(f"{what} not found: selector {selector!r} ({self.page.url})") from e
        self._check_signed_in()

    def open(self, channel_id: str) -> None:
        if not _ID_RE.match(channel_id or ""):
            raise PageError(f"not a Slack channel id: {channel_id!r}")
        self._goto(CLIENT_URL.format(team=self.cfg.team_id, channel=channel_id))
        self._wait_any(COMPOSER, "composer")
        try:  # an empty DM has no items: don't fail, just give the list a moment
            self.page.wait_for_selector(MESSAGE_ITEM, timeout=min(5.0, self.ui_timeout_s) * 1000)
        except Exception:
            pass

    def channel_names(self) -> dict[str, str]:
        """Channel id -> name from the sidebar of the open client page (a person types '#name', not an id)."""
        self._check_signed_in()
        rows = self.page.evaluate(
            "([sel, attr]) => [...document.querySelectorAll(sel)].map(e => "
            "[e.getAttribute(attr), (e.innerText || '').split('\\n')[0].trim()])",
            [SIDEBAR_ITEM, SIDEBAR_ID_ATTR])
        return {cid: name for cid, name in rows if cid and name and cid[:1] in ("C", "G")}

    def open_bot_dm(self) -> str:
        if self.cfg.bot_dm:
            self.open(self.cfg.bot_dm)
            return self.cfg.bot_dm
        self._goto(f"https://app.slack.com/client/{self.cfg.team_id}")
        self._wait_any(SIDEBAR_ITEM, "sidebar")
        dm_id = self.page.evaluate(_SIDEBAR_JS, [SIDEBAR_ITEM, SIDEBAR_ID_ATTR, self.cfg.bot_name])
        if not dm_id:
            raise PageError(f"no sidebar item named {self.cfg.bot_name!r}: selector {SIDEBAR_ITEM!r}")
        self.open(dm_id)
        return dm_id

    # ------------------------------------------------------------------ reading

    def _read(self, scope: str | None) -> list[Msg]:
        self._check_signed_in()
        item_sel = "[data-qa=\"virtual-list-item\"]" if scope else MESSAGE_ITEM
        self.page.evaluate(_SCROLL_JS, [item_sel, scope])
        sel = {
            "item": MESSAGE_ITEM, "container": MESSAGE_CONTAINER, "thread": THREAD_PANE, "tsLink": TS_LINK,
            "sender": SENDER_NAME, "profile": PROFILE_LINK, "text": MESSAGE_TEXT, "badge": APP_BADGE,
            "reactji": REACTJI,
        }
        rows = self.page.evaluate(_READ_JS, [sel, scope])
        if rows is None:
            raise PageError(f"not found: selector {scope!r}")
        out: list[Msg] = []
        prev: Msg | None = None
        for r in sorted(rows, key=lambda r: ts_key(r["ts"])):
            sender_id = None
            if r["profile"]:
                m = _MEMBER_RE.search(r["profile"])
                sender_id = m.group(1) if m else None
            if r["name"]:
                sender, is_bot = r["name"], bool(r["badge"])
            elif prev is not None:  # follow-on message: same sender as the one before
                sender, is_bot = prev.sender, prev.is_bot or bool(r["badge"])
                sender_id = sender_id or prev.sender_id
            else:
                sender, is_bot = "", bool(r["badge"])
            names = []
            for labels in r["reactions"]:
                name = next((n for n in map(reaction_name, labels) if n), None)
                if name and name not in names:
                    names.append(name)
            msg = Msg(
                ts=r["ts"], sender=sender, sender_id=sender_id, is_bot=is_bot, text=r["text"],
                reactions=tuple(names), task_id=task_id_of(r["text"]),
            )
            out.append(msg)
            prev = msg
        return out

    def messages(self) -> list[Msg]:
        return self._read(None)

    def _thread_messages(self) -> list[Msg]:
        if self.page.locator(THREAD_PANE).count() == 0:
            raise PageError(f"no thread pane open: selector {THREAD_PANE!r}")
        return self._read(THREAD_PANE)

    @staticmethod
    def _bot_after(msgs: Iterable[Msg], ts: str, until_ts: str | None, exclude, *, posts: bool) -> list[Msg]:
        lo = ts_key(ts)
        hi = ts_key(until_ts) if until_ts else None
        return [
            m for m in msgs
            if m.is_bot and ts_key(m.ts) > lo and (hi is None or ts_key(m.ts) < hi)
            and m.ts not in exclude and (posts or m.task_id is None)
        ]

    def replies_after(self, my_ts: str, *, until_ts: str | None = None, exclude=frozenset()) -> list[Msg]:
        return self._bot_after(self.messages(), my_ts, until_ts, exclude, posts=False)

    def posts_after(self, ts: str) -> list[Msg]:
        return self._bot_after(self.messages(), ts, None, frozenset(), posts=True)

    def thread_replies_after(self, my_ts: str, exclude=frozenset()) -> list[Msg]:
        return self._bot_after(self._thread_messages(), my_ts, None, exclude, posts=False)

    # ------------------------------------------------------------------ waiting

    def wait_done(self, my_ts: str, timeout_s: float, *, want_reaction: bool = True, in_thread: bool = False) -> str:
        """in_thread: my message is a reply in the open thread pane, so its marks are read there."""
        start = self._clock()
        while True:
            msgs = self._thread_messages() if in_thread else self.messages()
            mine = next((m for m in msgs if m.ts == my_ts), None)
            if mine is not None:
                if "x" in mine.reactions:
                    return "error"
                if "white_check_mark" in mine.reactions:
                    return "done"
            if not want_reaction and self._bot_after(msgs, my_ts, None, frozenset(), posts=False):
                return "replied"
            left = timeout_s - (self._clock() - start)
            if left <= 0:
                return "timeout"
            self._sleep(min(self.poll_s, left))

    # ------------------------------------------------------------------ sending

    def _main_composer(self):
        self._check_signed_in()
        loc = self.page.locator(COMPOSER)
        for i in range(loc.count()):
            el = loc.nth(i)
            if not el.evaluate("(e, s) => !!e.closest(s)", THREAD_PANE):
                return el
        raise PageError(f"composer not found: selector {COMPOSER!r}")

    def _send_with(self, composer, text: str, read: Callable[[], list[Msg]]) -> str:
        # Slack's list is virtual and a DM opens at the last-read point: after Enter it jumps to the bottom and draws
        # older messages that were not on screen. So "not seen before" is not enough: my message must be newer than
        # everything seen before sending, and carry the text I sent (whitespace-normalized; Slack trims and may
        # reformat, so after half the wait the newest of my newer messages is taken).
        seen = read()
        floor = max((ts_key(m.ts) for m in seen), default=(0, 0))
        want = _norm(text)
        # no click: a Slack tip ("View and save draft messages") can cover the composer, and fill() needs none
        composer.fill(text)
        composer.press("Enter")
        enters, last_enter = 1, self._clock()
        start = self._clock()
        while True:
            # an autocomplete (#channel, @mention) can take an Enter and leave the text in the composer: press again
            if enters < MAX_ENTERS and self._clock() - last_enter >= ENTER_RETRY_S and _norm(composer.inner_text()):
                composer.press("Enter")
                enters, last_enter = enters + 1, self._clock()
            mine = [m for m in read() if not m.is_bot and ts_key(m.ts) > floor
                    and not (self.cfg.me and m.sender_id and m.sender_id != self.cfg.me)]
            exact = [m for m in mine if _norm(m.text) == want]
            if exact:
                return exact[-1].ts
            if mine and self._clock() - start >= self.ui_timeout_s / 2:
                return mine[-1].ts
            left = self.ui_timeout_s - (self._clock() - start)
            if left <= 0:
                raise PageError(f"my message did not appear after Enter in {COMPOSER!r}: {text[:60]!r}")
            self._sleep(min(self.poll_s, left))

    def send(self, text: str) -> str:
        return self._send_with(self._main_composer(), text, self.messages)

    def open_thread(self, root_ts: str) -> None:
        self._check_signed_in()
        if not TS_RE.match(root_ts or ""):
            raise PageError(f"not a Slack ts: {root_ts!r}")
        item = self.page.locator(f'{MESSAGE_ITEM}[data-item-key="{root_ts}"]')
        if item.count() == 0:
            raise PageError(f"message {root_ts} not found: selector {MESSAGE_ITEM!r}")
        bar = item.locator(REPLY_BAR)
        try:
            if bar.count() > 0:
                bar.first.click()
            else:
                item.first.hover()
                start = self.page.locator(START_THREAD)
                if start.count() == 0:
                    raise PageError(f"no reply action on {root_ts}: selectors {REPLY_BAR!r} / {START_THREAD!r}")
                start.first.click()
            self.page.locator(THREAD_PANE).first.wait_for(state="visible", timeout=self.ui_timeout_s * 1000)
            # live 2026-11-11: the pane shows before its list; wait for the root message and the reply composer
            self.page.locator(f"{THREAD_PANE} {MESSAGE_CONTAINER}").first.wait_for(
                state="attached", timeout=self.ui_timeout_s * 1000)
            self.page.locator(THREAD_COMPOSER).first.wait_for(state="attached", timeout=self.ui_timeout_s * 1000)
        except PageError:
            raise
        except Exception as e:
            raise PageError(f"thread pane did not open for {root_ts}: selector {THREAD_PANE!r}") from e

    def send_in_thread(self, text: str) -> str:
        self._check_signed_in()
        if self.page.locator(THREAD_PANE).count() == 0:
            raise PageError(f"no thread pane open: selector {THREAD_PANE!r}")
        loc = self.page.locator(THREAD_COMPOSER)
        if loc.count() == 0:
            raise PageError(f"thread composer not found: selector {THREAD_COMPOSER!r}")
        return self._send_with(loc.first, text, self._thread_messages)

    # ------------------------------------------------------------------ evidence

    def screenshot(self, path: str) -> None:
        pane = self.page.locator(MESSAGE_PANE)
        if pane.count() == 0:
            pane = self.page.locator(MESSAGE_LIST).filter(has=self.page.locator(MESSAGE_ITEM))
        if pane.count() == 0:
            raise PageError(f"message pane not found: selectors {MESSAGE_PANE!r} / {MESSAGE_LIST!r}")
        pane.first.screenshot(path=path)
