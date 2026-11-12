"""SlackPage reads and drives the Slack web app (tests/e2e/standalone_ui/page.py).

Real headless Chromium, synthetic fixture HTML (tests/fork/e2e_standalone_ui/fixtures/), no network: app.slack.com
URLs are answered by page.route().
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

# Needs Playwright and its Chromium (`uv pip install playwright && python -m playwright install chromium`, see
# tests/e2e/standalone_ui/README.md); skipped in a plain `uv sync` environment.
sync_api = pytest.importorskip("playwright.sync_api")
sync_playwright = sync_api.sync_playwright

from tests.e2e.standalone_ui import page as page_mod  # noqa: E402
from tests.e2e.standalone_ui.page import Msg, PageError, SignedOut, SlackPage

FIX = Path(__file__).parent / "fixtures"
DM_HTML = (FIX / "dm.html").read_text(encoding="utf-8")
SIGNED_OUT_HTML = (FIX / "signed_out.html").read_text(encoding="utf-8")
NO_COMPOSER_HTML = (FIX / "no_composer.html").read_text(encoding="utf-8")
SIDEBAR_HTML = (FIX / "sidebar.html").read_text(encoding="utf-8")

MY_Q1 = "1700000100.000100"
BOT_A1 = "1700000110.000200"
BOT_A1B = "1700000111.000300"
SCHED = "1700000200.000400"
MY_Q2 = "1700000300.000500"


@dataclass(frozen=True)
class FakeCfg:
    """The Config fields SlackPage reads (duck-typed; the real one is tests/e2e/standalone_ui/config.py)."""

    slack_url: str = "https://example.slack.com"
    team_id: str = "T0TEST0001"
    bot_name: str = "Cost Bot"
    bot_user: str | None = "U0TESTBOT01"
    bot_dm: str | None = None
    me: str | None = None


CFG = FakeCfg()


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture
def dm(page):
    page.set_content(DM_HTML)
    return page


def _sp(page, cfg=CFG, **kw):
    kw.setdefault("poll_s", 0.05)
    kw.setdefault("ui_timeout_s", 3.0)
    return SlackPage(page, cfg, **kw)


def _scripted_sleep(clock, steps):
    def sleep(s):
        clock.t += s
        if steps:
            steps.pop(0)()

    return sleep


# ---------------------------------------------------------------- messages()


def test_messages_skip_day_dividers_and_thread_pane_oldest_first(dm):
    msgs = _sp(dm).messages()
    assert [m.ts for m in msgs] == [MY_Q1, BOT_A1, BOT_A1B, SCHED, MY_Q2]


def test_messages_parse_my_message_and_the_bot_reply(dm):
    by_ts = {m.ts: m for m in _sp(dm).messages()}
    assert by_ts[MY_Q1] == Msg(
        ts=MY_Q1,
        sender="Test User",
        sender_id=None,
        is_bot=False,
        text="What did we spend last month?",
        reactions=("white_check_mark",),
        task_id=None,
    )
    assert by_ts[BOT_A1] == Msg(
        ts=BOT_A1,
        sender="Cost Bot",
        sender_id="U0TESTBOT01",
        is_bot=True,
        text="You spent $1,234.56 last month.",
        reactions=(),
        task_id=None,
    )


def test_follow_on_message_inherits_the_previous_sender(dm):
    m = {m.ts: m for m in _sp(dm).messages()}[BOT_A1B]
    assert (m.sender, m.sender_id, m.is_bot, m.text) == ("Cost Bot", "U0TESTBOT01", True, "Top service: Compute.")


def test_reaction_names(dm):
    sp = _sp(dm)
    assert {m.ts: m for m in sp.messages()}[MY_Q2].reactions == ("hourglass_flowing_sand",)
    dm.evaluate(f"fx.react('{MY_Q2}', ['x', 'white_check_mark'])")
    assert {m.ts: m for m in sp.messages()}[MY_Q2].reactions == ("x", "white_check_mark")


@pytest.mark.parametrize(
    "label,name",
    [
        ("1 reaction, react with white check mark emoji", "white_check_mark"),
        ("2 reactions, react with hourglass flowing sand emoji", "hourglass_flowing_sand"),
        ("1 reaction, react with x emoji", "x"),
        ("white check mark emoji", "white_check_mark"),
        ("", None),
    ],
)
def test_reaction_name_from_label(label, name):
    assert page_mod.reaction_name(label) == name


def test_task_id_from_the_scheduled_post_footer(dm):
    by_ts = {m.ts: m for m in _sp(dm).messages()}
    assert by_ts[SCHED].task_id == "t7"
    assert "Daily cost: $12.00" in by_ts[SCHED].text
    assert all(by_ts[t].task_id is None for t in (MY_Q1, BOT_A1, BOT_A1B, MY_Q2))


def test_messages_on_the_sign_in_page_raise_signed_out(page):
    page.set_content(SIGNED_OUT_HTML)
    with pytest.raises(SignedOut):
        _sp(page).messages()


def test_signed_out_is_a_page_error():
    assert issubclass(SignedOut, PageError)


# ---------------------------------------------------------------- wait_done()


def test_wait_done_follows_working_then_done(dm):
    clock = FakeClock()
    steps = [
        lambda: dm.evaluate("fx.add({ts: '1700000310.000100', name: 'Cost Bot', bot: true, text: 'Tasks: t7'})"),
        lambda: dm.evaluate(f"fx.react('{MY_Q2}', ['white_check_mark'])"),
    ]
    sp = _sp(dm, poll_s=2.0, sleep=_scripted_sleep(clock, steps), clock=clock)
    assert sp.wait_done(MY_Q2, 60) == "done"
    assert steps == []  # it needed the reaction, not just the reply


def test_wait_done_error_mark(dm):
    clock = FakeClock()
    steps = [lambda: dm.evaluate(f"fx.react('{MY_Q2}', ['x'])")]
    sp = _sp(dm, poll_s=2.0, sleep=_scripted_sleep(clock, steps), clock=clock)
    assert sp.wait_done(MY_Q2, 60) == "error"


def test_wait_done_already_done(dm):
    slept = []
    sp = _sp(dm, sleep=slept.append, clock=FakeClock())
    assert sp.wait_done(MY_Q1, 60) == "done"
    assert slept == []


def test_wait_done_times_out_without_oversleeping(dm):
    clock = FakeClock()
    slept = []

    def sleep(s):
        slept.append(s)
        clock.t += s

    sp = _sp(dm, poll_s=2.0, sleep=sleep, clock=clock)
    assert sp.wait_done(MY_Q2, 5) == "timeout"
    assert slept == [2.0, 2.0, 1.0]


def test_wait_done_replied_ignores_scheduled_posts(dm):
    clock = FakeClock()
    steps = [
        lambda: dm.evaluate(
            "fx.add({ts: '1700000305.000100', name: 'Cost Bot', bot: true,"
            " text: \"Daily: $1\\nSet up by @Test User · task t9 · say 'stop task t9' to stop it\"})"
        ),
        lambda: dm.evaluate("fx.add({ts: '1700000306.000100', name: 'Cost Bot', bot: true, text: 'An answer'})"),
    ]
    sp = _sp(dm, poll_s=2.0, sleep=_scripted_sleep(clock, steps), clock=clock)
    assert sp.wait_done(MY_Q2, 60, want_reaction=False) == "replied"
    assert steps == []


# ---------------------------------------------------------------- replies_after() / posts_after()


def test_replies_after_leaves_out_scheduled_posts_and_my_messages(dm):
    assert [m.ts for m in _sp(dm).replies_after(MY_Q1)] == [BOT_A1, BOT_A1B]


def test_replies_after_until_and_exclude(dm):
    sp = _sp(dm)
    assert [m.ts for m in sp.replies_after(MY_Q1, until_ts=BOT_A1B)] == [BOT_A1]
    assert [m.ts for m in sp.replies_after(MY_Q1, exclude=frozenset({BOT_A1}))] == [BOT_A1B]


def test_posts_after_includes_scheduled_posts(dm):
    assert [m.ts for m in _sp(dm).posts_after(BOT_A1)] == [BOT_A1B, SCHED]


# ---------------------------------------------------------------- send()


def test_send_returns_my_new_message_ts(dm):
    sp = _sp(dm)
    ts = sp.send("How much did storage cost?")
    assert ts == "1700001000.000900"
    last = sp.messages()[-1]
    assert (last.ts, last.text, last.is_bot) == (ts, "How much did storage cost?", False)


def test_send_ignores_an_older_message_of_mine_drawn_again_after_enter(dm):
    # Live bug 2026-11-11: Slack opens a DM at the last-read point; after Enter it jumps to the bottom and draws
    # older messages of mine that were not on screen before. Only my NEW message (newer than everything seen,
    # same text) counts.
    dm.evaluate(f"""() => {{
        const old = document.querySelector('#list [data-item-key="{MY_Q1}"]');
        window.__old = old; old.remove();
        document.addEventListener('keydown', (e) => {{
            if (e.key !== 'Enter') return;
            e.preventDefault(); e.stopImmediatePropagation();
            const text = e.target.innerText.trim(); e.target.innerHTML = '';
            document.getElementById('list').prepend(window.__old);   // drawn again at once
            setTimeout(() => window.fx.add({{ts: '1700001000.000900', name: 'Test User', text}}), 400);  // mine, later
        }}, true);
    }}""")
    sp = _sp(dm)
    ts = sp.send("How much did storage cost?")
    assert ts == "1700001000.000900"


def test_send_ignores_my_newer_message_with_other_text(dm):
    # a message of mine typed elsewhere (another tab) is not the one this send made
    dm.evaluate("""() => document.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter') return;
        e.preventDefault(); e.stopImmediatePropagation();
        const text = e.target.innerText.trim(); e.target.innerHTML = '';
        window.fx.add({ts: '1700000999.000001', name: 'Test User', text: 'something else'});
        setTimeout(() => window.fx.add({ts: '1700001000.000900', name: 'Test User', text}), 400);
    }, true)""")
    assert _sp(dm).send("How much did storage cost?") == "1700001000.000900"


def test_a_mention_link_in_my_text_is_not_my_sender_id(dm):
    # Live bug 2026-11-11 (#cost-reports): my avatar is a button, not a /team/U link; the only /team/U link in my
    # channel message was the @mention of the bot inside the text, so the message was read as sent by the bot's id.
    dm.evaluate("""() => {
        const it = window.fx.item({ts: '1700000400.000700', name: 'Test User', text: ''});
        const t = it.querySelector('[data-qa="message-text"]');
        const a = document.createElement('a'); a.setAttribute('href', '/team/UBOT0001'); a.textContent = '@Cost Bot';
        t.appendChild(a); t.appendChild(document.createTextNode(' What did storage cost?'));
        document.getElementById('list').appendChild(it);
    }""")
    m = {x.ts: x for x in _sp(dm).messages()}["1700000400.000700"]
    assert m.sender_id is None and m.is_bot is False and m.text == "@Cost Bot What did storage cost?"


def test_send_works_with_a_tip_covering_the_composer(dm):
    # Live 2026-11-11: Slack's "New · View and save draft messages" tip covered the composer; clicking it timed out.
    dm.evaluate("""() => {
        const tip = document.createElement('div');
        tip.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.01)';
        tip.textContent = 'View and save draft messages'; document.body.appendChild(tip);
    }""")
    assert _sp(dm).send("How much did storage cost?") == "1700001000.000900"


def test_send_presses_enter_again_when_an_autocomplete_takes_the_first(dm):
    # Live 2026-11-11: typing "#cost-reports" opened the channel autocomplete; the first Enter picked the channel and
    # the message stayed in the composer.
    dm.evaluate("""() => {
        let first = true;
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && first) { first = false; e.preventDefault(); e.stopImmediatePropagation(); }
        }, true);
    }""")
    assert _sp(dm).send("Post a report in #cost-bot-dev") == "1700001000.000900"


def test_send_on_the_sign_in_page_raises_signed_out(page):
    page.set_content(SIGNED_OUT_HTML)
    with pytest.raises(SignedOut):
        _sp(page).send("hi")


def test_send_without_a_composer_names_the_selector(page):
    page.set_content(NO_COMPOSER_HTML)
    with pytest.raises(PageError) as exc:
        _sp(page).send("hi")
    assert page_mod.COMPOSER in str(exc.value)
    assert not isinstance(exc.value, SignedOut)


# ---------------------------------------------------------------- open() / open_bot_dm()


def _serve(page, routes):
    """routes: path -> html; records every requested app.slack.com URL."""
    seen = []

    def handler(route):
        url = route.request.url
        seen.append(url)
        path = url.split("app.slack.com", 1)[1]
        body = routes.get(path)
        if body is None:
            route.fulfill(status=404, body="not found")
        else:
            route.fulfill(status=200, content_type="text/html", body=body)

    page.route("https://app.slack.com/**", handler)
    return seen


def test_open_navigates_to_the_client_url(page):
    seen = _serve(page, {"/client/T0TEST0001/D0TESTDM001": DM_HTML})
    sp = _sp(page)
    sp.open("D0TESTDM001")
    assert seen[0] == "https://app.slack.com/client/T0TEST0001/D0TESTDM001"
    assert len(sp.messages()) == 5


def test_open_on_the_sign_in_page_raises_signed_out(page):
    _serve(page, {"/client/T0TEST0001/D0TESTDM001": SIGNED_OUT_HTML})
    with pytest.raises(SignedOut):
        _sp(page).open("D0TESTDM001")


def test_open_without_a_composer_raises_page_error(page):
    _serve(page, {"/client/T0TEST0001/D0TESTDM001": NO_COMPOSER_HTML})
    with pytest.raises(PageError) as exc:
        _sp(page, ui_timeout_s=0.5).open("D0TESTDM001")
    assert page_mod.COMPOSER in str(exc.value)


def test_open_rejects_a_bad_channel_id(page):
    with pytest.raises(PageError):
        _sp(page).open("../evil")


def test_open_bot_dm_uses_the_configured_dm(page):
    seen = _serve(page, {"/client/T0TEST0001/D0TESTDM001": DM_HTML})
    assert _sp(page, cfg=FakeCfg(bot_dm="D0TESTDM001")).open_bot_dm() == "D0TESTDM001"
    assert seen == ["https://app.slack.com/client/T0TEST0001/D0TESTDM001"]


def test_open_bot_dm_finds_the_sidebar_item_by_exact_name(page):
    seen = _serve(page, {"/client/T0TEST0001": SIDEBAR_HTML, "/client/T0TEST0001/D0TESTDM001": DM_HTML})
    assert _sp(page).open_bot_dm() == "D0TESTDM001"
    assert seen[-1] == "https://app.slack.com/client/T0TEST0001/D0TESTDM001"


def test_channel_names_come_from_the_sidebar(page):
    # the catalog types "#name" like a person does (2026-11-11: "<#C…>" tripped the agent's shell guard)
    _serve(page, {"/client/T0TEST0001": SIDEBAR_HTML})
    sp = _sp(page)
    page.goto("https://app.slack.com/client/T0TEST0001")
    assert sp.channel_names()["C0TESTCH001"] == "cost-bot-dev"


def test_open_bot_dm_missing_from_sidebar(page):
    _serve(page, {"/client/T0TEST0001": SIDEBAR_HTML})
    with pytest.raises(PageError) as exc:
        _sp(page, cfg=FakeCfg(bot_name="No Such Bot")).open_bot_dm()
    assert "No Such Bot" in str(exc.value)


# ---------------------------------------------------------------- threads


def test_thread_open_send_and_replies(dm):
    sp = _sp(dm)
    sp.open_thread(MY_Q1)
    assert dm.locator(page_mod.THREAD_PANE).is_visible()
    assert [m.ts for m in sp.thread_replies_after(MY_Q1)] == ["1700000150.000700"]
    ts = sp.send_in_thread("And the month before?")
    assert ts == "1700001000.000900"
    # my thread message is in the pane, not in the channel list
    assert ts not in [m.ts for m in sp.messages()]
    dm.evaluate(f"fx.add({{ts: '1700001001.000100', name: 'Cost Bot', bot: true, text: 'Before: $9'}}, true)")
    replies = sp.thread_replies_after(ts)
    assert [(m.ts, m.text) for m in replies] == [("1700001001.000100", "Before: $9")]
    assert sp.thread_replies_after(ts, exclude=frozenset({"1700001001.000100"})) == []


def test_open_thread_waits_until_the_pane_shows_its_messages_and_composer(dm):
    # Live 2026-11-11 (#cost-reports): the pane opens first; its messages and the reply composer render a moment later.
    dm.evaluate("""() => {
        const pane = document.getElementById('thread');
        const kids = [...pane.children]; kids.forEach(k => k.remove());
        const head = document.createElement('div'); head.setAttribute('data-qa', 'flexpane_header');
        head.textContent = 'Thread'; pane.appendChild(head);   // like Slack: the pane is visible before its list
        document.querySelector('[data-qa="reply_bar_count"]').onclick = () => {
            pane.hidden = false;
            setTimeout(() => kids.forEach(k => pane.appendChild(k)), 400);
        };
    }""")
    sp = _sp(dm)
    sp.open_thread(MY_Q1)
    assert [m.ts for m in sp.thread_replies_after(MY_Q1)] == ["1700000150.000700"]


def test_wait_done_for_a_message_in_the_thread_reads_the_pane(dm):
    # Live 2026-11-11: my follow-up's done mark is on its copy in the thread pane, not in the channel list.
    sp = _sp(dm)
    sp.open_thread(MY_Q1)
    ts = sp.send_in_thread("And the month before?")
    dm.evaluate(f"""() => {{
        const c = document.querySelector('#thread-list [data-item-key="{ts}"] [data-qa="message_content"]');
        const b = document.createElement('button'); b.setAttribute('data-qa', 'reactji');
        b.setAttribute('aria-label', '1 reaction, react with white check mark emoji'); c.appendChild(b);
    }}""")
    assert sp.wait_done(ts, 1.0, in_thread=True) == "done"


def test_open_thread_unknown_message(dm):
    with pytest.raises(PageError) as exc:
        _sp(dm).open_thread("1600000000.000001")
    assert "1600000000.000001" in str(exc.value)


def test_send_in_thread_without_a_pane(page):
    page.set_content(NO_COMPOSER_HTML)
    with pytest.raises(PageError) as exc:
        _sp(page).send_in_thread("hi")
    assert page_mod.THREAD_PANE in str(exc.value)


# ---------------------------------------------------------------- screenshot / selectors


def test_screenshot_writes_a_png(dm, tmp_path):
    out = tmp_path / "shot.png"
    _sp(dm).screenshot(str(out))
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_selectors_are_named_constants():
    for name in ("MESSAGE_ITEM", "MESSAGE_CONTAINER", "SENDER_NAME", "APP_BADGE", "TS_LINK", "PROFILE_LINK",
                 "MESSAGE_TEXT", "REACTJI", "COMPOSER", "THREAD_PANE", "THREAD_COMPOSER", "SIGNIN_FORM",
                 "MESSAGE_PANE", "SIDEBAR_ITEM", "REPLY_BAR", "START_THREAD"):
        assert isinstance(getattr(page_mod, name), str) and getattr(page_mod, name)
