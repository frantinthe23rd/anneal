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
        load = self.src[self.src.index("function loadChat()"):]
        self.assertIn("!m.pending", load[:load.index("\n}")])

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

    def test_discarding_asks_first(self):
        """It was free to discard when a reload destroyed it anyway."""
        handler = self.src[self.src.index('$("cClear").onclick'):]
        handler = handler[:handler.index("};")]
        self.assertIn("confirm(", handler)
        self.assertIn("clearChat()", handler)

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
