#!/usr/bin/env python3
"""Structural checks on the chat transcript persistence in ui.html.

**These are not behavioural tests and should not be mistaken for them.** There
is no JavaScript runtime on this machine (`node`, `deno` and `bun` are all
absent), so the trimming and quota logic cannot be executed here. What these
assert is that the invariants which make that logic safe are still present in
the file — the ones whose absence turns a bug into a silent data loss or an
exception that takes out whatever ran next.

The behaviour that genuinely needs a browser is listed in the commit and needs
checking against the running gateway:

  - a reload restores the transcript
  - "New conversation" asks before discarding, and discarding empties storage
  - the copy button copies the reply and not the reasoning
  - the fallback selection path on http://127.0.0.1:8001, where
    navigator.clipboard is unavailable outside a secure context
"""

import os
import re
import unittest

from tests.context import REPO_ROOT

UI = os.path.join(REPO_ROOT, "ui.html")


class ChatPersistenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_storage_key_is_namespaced_like_the_others(self):
        self.assertIn('const CHAT_KEY = "anneal.chat"', self.src)
        for key in ('"anneal.key"', '"anneal.prefs"'):
            self.assertIn(key, self.src)

    def test_load_save_and_clear_all_exist(self):
        for fn in ("function loadChat()", "function saveChat()", "function clearChat()"):
            self.assertIn(fn, self.src)

    def test_transcript_is_loaded_at_boot(self):
        boot = self.src[self.src.index("async function boot()"):]
        self.assertIn("loadChat()", boot.split("\n}")[0])

    def test_setitem_is_guarded(self):
        """localStorage.setItem throws on quota; unguarded, it takes out the
        rest of whatever called it."""
        for m in re.finditer(r"localStorage\.setItem\(CHAT_KEY", self.src):
            window = self.src[max(0, m.start() - 200):m.start()]
            self.assertIn("try {", window,
                          "setItem(CHAT_KEY) must be inside a try block")

    def test_a_size_bound_exists(self):
        self.assertIn("CHAT_MAX_BYTES", self.src)
        self.assertIn("CHAT_MAX_MESSAGES", self.src)
        # And it must be well under the ~5 MB quota, not at it.
        size = re.search(r"CHAT_MAX_BYTES = ([\d\s*]+);", self.src).group(1)
        self.assertLess(eval(size), 2 * 1024 * 1024)               # noqa: S307

    def test_trimming_drops_from_the_front(self):
        """Oldest first: the recent turns are the ones still in play."""
        save = self.src[self.src.index("function saveChat()"):]
        save = save[:save.index("\n}")]
        self.assertIn("keep.slice(2)", save)
        self.assertIn("keep.slice(-CHAT_MAX_MESSAGES)", save)

    def test_pending_placeholder_is_never_persisted(self):
        """Otherwise a reload shows a reply stuck at '…' for ever."""
        save = self.src[self.src.index("function saveChat()"):]
        save = save[:save.index("\n}")]
        self.assertIn("!m.pending", save)
        # Reading a conversation moved into openChat() when there became more
        # than one of them; the filter is what matters, not where it lives.
        opened = self.src[self.src.index("function openChat("):]
        self.assertIn("!m.pending", opened[:opened.index("\n}")])

    def test_save_is_called_when_a_reply_completes(self):
        run = self.src[self.src.index("async function runChat()"):]
        run = run[:run.index("\n/* ")]
        self.assertGreaterEqual(run.count("saveChat()"), 2,
                                "save after success and after failure")

    def test_save_is_not_called_per_streamed_chunk(self):
        """Serialising the whole transcript on every token is not free."""
        run = self.src[self.src.index("async function runChat()"):]
        stream = run[run.index("for (const line of lines)"):run.index("reply.pending = false;\n    saveChat();")]
        self.assertNotIn("saveChat", stream)

    def test_clearing_leaves_the_rebuild_signal_intact(self):
        """renderChat tears the transcript down when it holds more nodes than
        messages. clearChat zeroed both, so the signal vanished and the old
        messages stayed on screen after New conversation."""
        fn = self.src[self.src.index("function clearChat()"):]
        fn = fn[:fn.index("\n}")]
        self.assertNotIn("chatNodes = []", fn,
                         "zeroing chatNodes here hides the reset from renderChat")
        self.assertIn("renderChat()", fn)

    def test_starting_a_new_conversation_keeps_the_old_one(self):
        """It used to ask before discarding, because starting a new
        conversation destroyed the only one there was. Conversations are kept
        in a list now, so there is nothing to discard and nothing to confirm —
        asking would be a prompt about a loss that no longer happens."""
        fn = self.src[self.src.index("function startNewChat()"):]
        fn = fn[:fn.index("\n}")]
        self.assertNotIn("confirm(", fn)
        # The previous conversation is already filed; only the live state resets.
        self.assertIn("chatLog = []", fn)
        self.assertIn("renderChatList()", fn)

    def test_deleting_one_still_asks(self):
        """That is a real loss, and the only one left."""
        fn = self.src[self.src.index("function renderChatList()"):]
        fn = fn[:fn.index("\nfunction ")]
        self.assertIn("confirm(", fn)
        self.assertIn("deleteChat(", fn)

    def test_forget_everything_takes_the_transcript_too(self):
        """It is now the most personal thing in localStorage."""
        wipe = self.src[self.src.index('"Forget key'):]
        wipe = wipe[:wipe.index("location.reload()")]
        self.assertIn("removeItem(CHAT_KEY)", wipe)

    def test_clearing_empties_storage_not_just_memory(self):
        clear = self.src[self.src.index("function clearChat()"):]
        self.assertIn("removeItem(CHAT_KEY)", clear[:clear.index("\n}")])


class CopyReplyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as fh:
            cls.src = fh.read()

    def paint(self):
        """The renderer is incremental: renderChat() reuses message elements and
        paintMsg() decides their contents, so that is where the button lives."""
        fn = self.src[self.src.index("function paintMsg("):]
        return fn[:fn.index("\n}")]

    def test_copy_button_exists_for_assistant_replies_only(self):
        render = self.paint()
        self.assertIn('m.role === "assistant"', render)
        self.assertIn("copyText(m.content", render)

    def test_copies_content_not_reasoning(self):
        """Reasoning is working out, not answer, and is not what you paste."""
        self.assertNotIn("copyText(m.reasoning", self.paint())

    def test_has_a_fallback_outside_a_secure_context(self):
        """navigator.clipboard is undefined on http://127.0.0.1:8001, which is
        exactly how the host itself reaches Anneal."""
        fn = self.src[self.src.index("async function copyText("):]
        fn = fn[:fn.index("\n}")]
        self.assertIn("navigator.clipboard", fn)
        self.assertIn("catch", fn)
        self.assertIn("getSelection", fn)

    def test_button_is_a_type_button(self):
        """An unspecified <button> inside a form submits it."""
        self.assertIn('copy.type = "button"', self.paint())

    def test_the_button_is_not_rebuilt_on_every_repaint(self):
        """paintMsg runs once a frame while a reply streams. Re-creating the
        button each time would drop it out from under a click."""
        self.assertIn(".copy", self.paint())


class NoExternalRequestsTest(unittest.TestCase):
    """A standing property of this page, restated in the README: it makes no
    external requests at all. Easy to break by pasting in a library."""

    def test_no_external_urls(self):
        with open(UI, encoding="utf-8") as fh:
            src = fh.read()
        offenders = re.findall(r"""["'(](https?:)?//[^"')\s]+""", src)
        self.assertEqual([o for o in offenders if "w3.org" not in o], [])


if __name__ == "__main__":
    unittest.main()


class ChatHistoryTest(unittest.TestCase):
    """One conversation, and "New conversation" discarded it — so the only way
    to keep a thread was never to start another. Every other chat interface
    keeps a list; this one now does too."""

    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_conversations_are_stored_separately_from_the_index(self):
        """Loading the list must not deserialise every message of every chat,
        and trimming one must not rewrite the others."""
        self.assertIn("CHAT_INDEX_KEY", self.src)
        self.assertIn("function chatKeyFor", self.src)

    def test_the_old_single_conversation_is_adopted(self):
        """Upgrading should not look like losing your history."""
        fn = self.src[self.src.index("function loadChat()"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("CHAT_KEY", fn)
        self.assertIn("removeItem", fn)

    def test_dropping_an_old_chat_takes_its_body(self):
        """Or the index shrinks while the storage it pointed at is orphaned."""
        fn = self.src[self.src.index("function saveChat()"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("CHAT_MAX_KEPT", fn)
        self.assertIn("removeItem(chatKeyFor(", fn)

    def test_the_title_is_the_first_thing_said(self):
        self.assertIn("function titleFor", self.src)

    def test_the_list_can_be_collapsed_and_the_choice_remembered(self):
        self.assertIn("CHAT_ASIDE_KEY", self.src)
        self.assertIn("function setChatAside", self.src)

    def test_it_starts_collapsed_where_it_would_cover_the_conversation(self):
        fn = self.src[self.src.index("function restoreChatAside()"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn("max-width: 760px", fn)


class ChatFitsTheWindowTest(unittest.TestCase):
    """The box you type into must be on screen at any window height. It was
    not: #chatitems was `clamp(300px, 100vh - 400px, 900px)`, so on a short
    window the clamp floored at 300px and the transcript plus the composer were
    taller than the viewport."""

    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_the_transcript_no_longer_has_a_fixed_height(self):
        self.assertNotIn("calc(100vh - 400px)", self.src)

    def test_the_view_is_sized_from_its_own_position(self):
        """Nothing above it has a height CSS could subtract — the header, the
        page tabs and the mode strip all size to their content, and one of them
        wraps on a narrow window."""
        fn = self.src[self.src.index("function sizeChat()"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn("getBoundingClientRect", fn)
        self.assertIn("innerHeight", fn)

    def test_the_floor_is_what_must_fit_rather_than_a_round_number(self):
        """300 was a round number, and at 900x540 it was 49px taller than the
        room available — which put the composer back below the fold."""
        fn = self.src[self.src.index("function sizeChat()"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn(".composer", fn)
        self.assertIn(".loghead", fn)

    def test_it_is_resized_when_the_window_is(self):
        self.assertIn('addEventListener("resize", sizeChat)', self.src)


class ChatSitsInTheSameBoxTest(unittest.TestCase):
    """#chatview was a sibling of .wrap rather than a child. That cost two
    things with one cause: it spanned the whole window — measured at 1600px, the
    tab strip ran 200-1400 and the chat ran 0-1600 — and .wrap's 64px bottom
    padding fell between the tab strip and the chat, so the gap above it was
    visibly larger than in every other view.

    Asserted as nesting rather than as a matching width, because a copied
    max-width is exactly the kind of second copy that drifts."""

    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_the_chat_view_is_inside_the_wrap(self):
        import re
        start = self.src.index('<div class="wrap">')
        tag = re.compile(r"<(/?)div\b[^>]*>", re.I)
        i, depth = start, 0
        while True:
            m = tag.search(self.src, i)
            depth += -1 if m.group(1) else 1
            i = m.end()
            if depth == 0:
                break
        self.assertLess(self.src.index('id="chatview"'), i,
                        "#chatview is outside .wrap, so it spans the window and "
                        "inherits .wrap's bottom padding as a gap above it")

    def test_it_does_not_carry_its_own_width(self):
        """Constraining it separately worked and left two numbers to keep in
        step. Being inside the wrap is the fix that cannot drift."""
        import re
        m = re.search(r"\n\.chatview \{([^}]*)\}", self.src)
        self.assertNotIn("max-width", m.group(1))


class TabsAreGroupedTest(unittest.TestCase):
    """Grouped by what comes out — audio, then visual, then text — so related
    things sit together rather than in the order they were built."""

    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as fh:
            cls.src = fh.read()

    def order(self):
        import re
        strip = self.src[self.src.index('<div class="tabs" role="tablist">'):]
        strip = strip[:strip.index("</div>")]
        return re.findall(r'data-mode="(\w+)"', strip)

    def test_the_audio_tabs_are_contiguous(self):
        order = self.order()
        audio = [order.index(m) for m in ("music", "press", "speech", "sfx")]
        self.assertEqual(sorted(audio), list(range(min(audio), min(audio) + 4)))

    def test_the_visual_tabs_are_contiguous(self):
        order = self.order()
        visual = [order.index(m) for m in ("image", "sprites")]
        self.assertEqual(sorted(visual), list(range(min(visual), min(visual) + 2)))

    def test_audio_comes_before_visual_and_chat_is_last(self):
        order = self.order()
        self.assertLess(order.index("sfx"), order.index("image"))
        self.assertEqual(order[-1], "chat")


class AgentComposerTest(unittest.TestCase):
    """The agent branch returned early and never reached the reset, so the
    prompt stayed in the box while its own reply streamed underneath — and the
    next message had to be typed around it."""

    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_the_box_is_cleared_before_the_handoff(self):
        fn = self.src[self.src.index("async function runChat()"):]
        branch = fn[:fn.index("return runAgent(text);")]
        self.assertIn('$("cInput").value = ""', branch)


class AgentRunSurvivesTheTabTest(unittest.TestCase):
    """A run was only as durable as the tab (#67): the loop lived in the request
    handler, the transcript kept whatever arrived before the drop, and nothing
    said the work had carried on. The record is now the source of truth and the
    page attaches to it — on a fresh load exactly as on the run it started.

    Structural, like the rest of this file: there is no JavaScript runtime here.
    The behaviour to check in a browser is in the commit — navigate away
    mid-run, come back, and see the steps you missed."""

    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as fh:
            cls.src = fh.read()

    def fn(self, name):
        body = self.src[self.src.index(name):]
        return body[:body.index("\n}")]

    def test_the_run_id_is_stored_under_a_namespaced_key(self):
        self.assertIn('const AGENT_RUN_KEY = "anneal.agentruns"', self.src)

    def test_the_run_is_remembered_before_it_is_watched(self):
        """A reload one second in must still find it, so the id is written the
        moment the gateway hands it over — not when the run finishes."""
        run = self.fn("async function runAgent(")
        self.assertIn("rememberAgentRun(", run)
        self.assertLess(run.index("rememberAgentRun("), run.index("watchAgentRun("))

    def test_the_page_asks_for_the_run_rather_than_holding_a_stream(self):
        """The stream is a convenience for API callers now. The page polls the
        record, which is the same path a reconnecting page takes — so the way
        back is exercised on every run rather than only after a drop."""
        watch = self.fn("async function watchAgentRun(")
        self.assertIn("/v1/agent?id=", watch)

    def test_a_run_in_flight_is_picked_up_on_load(self):
        boot = self.src[self.src.index("async function boot()"):]
        self.assertIn("resumeAgentRun()", boot.split("\n}")[0])

    def test_opening_a_conversation_picks_up_its_run(self):
        """Each conversation has its own working folder, so each can have its
        own run in flight."""
        self.assertIn("resumeAgentRun()", self.fn("function openChat("))

    def test_a_settled_run_is_rendered_and_then_forgotten(self):
        """Otherwise every reload appends the same finished run again."""
        watch = self.fn("async function watchAgentRun(")
        self.assertIn("forgetAgentRun(", watch)

    def test_a_busy_folder_attaches_to_the_run_that_holds_it(self):
        """409 means someone is already working in that folder — which is a
        thing to show, not an error to report."""
        run = self.fn("async function runAgent(")
        self.assertIn("409", run)
        self.assertIn("run_id", run)

    def test_stopping_stops_the_run_and_not_just_the_watching(self):
        """The button said Stop and detached; the loop kept the folder for as
        long as it liked."""
        self.assertIn("/v1/agent/cancel", self.src)

    def test_an_interrupted_run_is_shown_as_interrupted(self):
        """A gateway restart leaves the record; it must not read as finished."""
        self.assertIn("interrupted", self.fn("function agentRunLines("))


class AgentModeFollowsTheConversationTest(unittest.TestCase):
    """The Agent box was left clear when a conversation with agent work in it
    was reopened, so the follow-up went to plain chat with no tools and no
    working folder (#72). Structural, like the rest of this file — the browser
    check is in the commit."""

    def setUp(self):
        with open(UI, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_the_box_is_set_from_the_transcript(self):
        self.assertIn("function reflectAgentMode()", self.src)
        block = self.src[self.src.index("function reflectAgentMode()"):]
        block = block[:block.index("\nasync function")]
        self.assertIn("agentJob", block)
        self.assertIn('$("cAgent")', block)

    def test_it_runs_even_when_nothing_is_rendered(self):
        """`loadChat` opens the conversation with `render: false`, which is the
        path a reload takes. Inside the render branch the call never ran there,
        and the box was still clear after coming back — which is the bug."""
        block = self.src[self.src.index("function openChat("):]
        block = block[:block.index("\nfunction deleteChat(")]
        self.assertIn("reflectAgentMode()", block)
        before, _, after = block.partition("if (render) {")
        self.assertIn("reflectAgentMode()", before)

    def test_a_run_picked_up_on_load_sets_it_too(self):
        block = self.src[self.src.index("async function resumeAgentRun()"):]
        block = block[:block.index("\nasync function runAgent(")]
        self.assertIn("reflectAgentMode()", block)

    def test_it_never_clears_a_box_the_person_set(self):
        block = self.src[self.src.index("function reflectAgentMode()"):]
        block = block[:block.index("\nasync function")]
        self.assertNotIn("= false", block)


class ARunShowsWhatItIsDoingTest(unittest.TestCase):
    """"Is it stuck or actually doing something?" — the transcript showed the
    last completed step and nothing else, so a five-minute image generation was
    indistinguishable from a dead worker (#75), and pressing Stop showed
    nothing at all for as long as fourteen minutes (#76)."""

    def setUp(self):
        with open(UI, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_the_call_in_flight_is_rendered(self):
        block = self.src[self.src.index("function agentRunLines("):]
        block = block[:block.index("\nfunction waitingOn(")]
        self.assertIn("waitingOn(run)", block)
        self.assertIn("working", block)

    def test_a_stop_says_what_it_is_waiting_for(self):
        block = self.src[self.src.index("function agentRunLines("):]
        block = block[:block.index("\nfunction waitingOn(")]
        self.assertIn("stopping", block)
        self.assertIn("as soon as the step it is in returns", block)

    def test_the_in_flight_step_is_read_from_the_stage(self):
        """`stage` is the only thing that knows: the trace holds what came
        back, so a call still running is not in it."""
        block = self.src[self.src.index("function waitingOn(run)"):]
        block = block[:block.index("\n}") + 2]
        self.assertIn("run.stage", block)
        self.assertIn("run.trace", block)
