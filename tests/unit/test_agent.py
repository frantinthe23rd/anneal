#!/usr/bin/env python3
"""Agent mode: a working folder and the tools already here (#61).

Chat can call tools — measured against the running gateway — but nothing was on
the other end of a call. This puts Anneal's own generators there, plus a folder
to write into, and runs the loop until the model stops asking or a cap is hit.

The safety story is one sentence: **everything happens inside one directory and
nothing executes.** There is no shell tool, so the worst a confused 20B model
can do is fill a folder with bad files. That only holds if the folder actually
contains it, which is what most of this file is about.
"""

import json
import os
import shutil
import tempfile
import unittest

from tests.context import REPO_ROOT  # noqa: F401

import agent


class SandboxCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # realpath: on macOS /var is a symlink to /private/var, and safe_path
        # resolves before comparing — as it must, or a symlink out slips past.
        self.root = os.path.realpath(os.path.join(self.tmp, "job-1"))
        os.makedirs(self.root)


class TestTheFolderContains(SandboxCase):
    def test_a_plain_name_resolves_inside(self):
        self.assertEqual(agent.safe_path(self.root, "index.html"),
                         os.path.join(self.root, "index.html"))

    def test_a_subdirectory_is_allowed(self):
        self.assertTrue(agent.safe_path(self.root, "src/main.py").startswith(self.root))

    def test_climbing_out_is_refused(self):
        for escape in ("../secrets", "../../etc/passwd", "a/../../b",
                       "./../../x", "sub/../../../y"):
            with self.assertRaises(ValueError, msg=escape):
                agent.safe_path(self.root, escape)

    def test_an_absolute_path_is_refused(self):
        for escape in ("/etc/passwd", "/tmp/x", os.path.join(self.tmp, "outside")):
            with self.assertRaises(ValueError, msg=escape):
                agent.safe_path(self.root, escape)

    def test_a_symlink_out_is_refused(self):
        """The check has to be on the resolved path, not the joined one."""
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside, exist_ok=True)
        os.symlink(outside, os.path.join(self.root, "link"))
        with self.assertRaises(ValueError):
            agent.safe_path(self.root, "link/file.txt")

    def test_a_name_that_merely_starts_the_same_is_refused(self):
        """`/root/job-1-evil` starts with `/root/job-1` as a string and is a
        different directory. Prefix comparison without a separator is the
        classic way this check is got wrong."""
        sibling = self.root + "-evil"
        os.makedirs(sibling, exist_ok=True)
        with self.assertRaises(ValueError):
            agent.safe_path(self.root, "../job-1-evil/file")

    def test_empty_and_dotty_names_are_refused(self):
        for bad in ("", "   ", ".", ".."):
            with self.assertRaises(ValueError, msg=repr(bad)):
                agent.safe_path(self.root, bad)


class TestTheTools(unittest.TestCase):
    def test_every_tool_has_an_openai_schema(self):
        for spec in agent.TOOLS:
            self.assertEqual(spec["type"], "function")
            fn = spec["function"]
            for key in ("name", "description", "parameters"):
                self.assertIn(key, fn)
            self.assertEqual(fn["parameters"]["type"], "object")

    def test_there_is_no_way_to_run_anything(self):
        """The one property that makes a working folder safe to hand over."""
        names = [t["function"]["name"] for t in agent.TOOLS]
        for forbidden in ("run", "shell", "exec", "command", "bash", "eval"):
            self.assertFalse([n for n in names if forbidden in n.lower()], names)

    def test_the_generators_are_offered(self):
        names = [t["function"]["name"] for t in agent.TOOLS]
        for wanted in ("write_file", "read_file", "list_files"):
            self.assertIn(wanted, names)
        # At least one Anneal generator, or the point is lost.
        self.assertTrue([n for n in names if n.startswith("generate_")], names)

    def test_names_are_unique(self):
        names = [t["function"]["name"] for t in agent.TOOLS]
        self.assertEqual(len(names), len(set(names)))


class TestTheLoop(SandboxCase):
    def scripted(self, replies):
        """A stand-in for the text model: hands back canned turns in order."""
        turns = list(replies)
        def chat(messages, tools):
            return turns.pop(0) if turns else {"content": "done", "tool_calls": None}
        return chat

    def call(self, name, args):
        return {"tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)}}],
                "content": ""}

    def test_a_reply_with_no_tool_call_ends_it(self):
        out = agent.run("hello", self.root, self.scripted([{"content": "hi", "tool_calls": None}]))
        self.assertEqual(out["steps"], 0)
        self.assertEqual(out["reply"], "hi")

    def test_a_tool_call_is_executed_and_fed_back(self):
        chat = self.scripted([
            self.call("write_file", {"path": "a.txt", "content": "hello"}),
            {"content": "written", "tool_calls": None}])
        out = agent.run("write a file", self.root, chat)
        self.assertEqual(out["steps"], 1)
        with open(os.path.join(self.root, "a.txt")) as fh:
            self.assertEqual(fh.read(), "hello")

    def test_it_stops_at_the_step_cap(self):
        """A model that keeps calling tools must not run for ever."""
        def chat(messages, tools):
            return self.call("list_files", {})
        out = agent.run("go", self.root, chat, max_steps=3)
        self.assertEqual(out["steps"], 3)
        self.assertTrue(out["stopped"])

    def test_a_failing_tool_is_reported_back_rather_than_raising(self):
        """The model should get the error and be able to try something else —
        that is the whole point of a loop."""
        chat = self.scripted([
            self.call("read_file", {"path": "../escape"}),
            {"content": "understood", "tool_calls": None}])
        out = agent.run("read it", self.root, chat)
        self.assertEqual(out["steps"], 1)
        self.assertFalse(out["trace"][0]["ok"])
        self.assertIn("outside", out["trace"][0]["result"].lower())

    def test_every_step_is_reported_as_it_happens(self):
        seen = []
        chat = self.scripted([
            self.call("write_file", {"path": "a.txt", "content": "x"}),
            {"content": "ok", "tool_calls": None}])
        agent.run("go", self.root, chat, on_step=lambda s: seen.append(s))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["tool"], "write_file")

    def test_an_unknown_tool_is_an_error_not_a_crash(self):
        chat = self.scripted([
            self.call("delete_everything", {}),
            {"content": "sorry", "tool_calls": None}])
        out = agent.run("go", self.root, chat)
        self.assertFalse(out["trace"][0]["ok"])

    def test_arguments_that_are_not_json_are_an_error_not_a_crash(self):
        def chat(messages, tools):
            return {"content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "write_file", "arguments": "{not json"}}]}
        out = agent.run("go", self.root, chat, max_steps=1)
        self.assertFalse(out["trace"][0]["ok"])


if __name__ == "__main__":
    unittest.main()
