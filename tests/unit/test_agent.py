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
import time
import unittest
import socket

from tests.context import REPO_ROOT

import agent
import supervisor


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
        """After the one nudge. A first turn with no call is usually a plan, so
        it is pushed once; saying the same thing again ends the run."""
        out = agent.run("hello", self.root, self.scripted([
            {"content": "hi", "tool_calls": None},
            {"content": "hi", "tool_calls": None}]))
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


class TestSeeingWhatItMade(unittest.TestCase):
    """The run reported files and there was no way to look at them: the working
    folder is outside `outputs/`, so the library cannot show it, and the tab
    captured the list and rendered nothing. A finished job you can only inspect
    over ssh is not finished."""

    def setUp(self):
        import supervisor
        self.supervisor = supervisor

    def test_the_route_is_owned_by_the_gateway(self):
        import services
        self.assertIn("/v1/agent/file", services.GATEWAY_ROUTES)
        self.assertIsNone(services.resolve("/v1/agent/file"))

    def test_it_is_containment_checked_like_everything_else(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "supervisor.py"),
            encoding="utf-8").read()
        block = src[src.index('if route == "/v1/agent/file"'):]
        block = block[:block.index("\n        if route ==", 10)]
        self.assertIn("safe_path", block)
        self.assertIn("_authorized", block)


class TestInliningASite(SandboxCase):
    """A static site is the obvious thing to ask an agent for, and it is the
    one output a blob URL cannot show: `<link href="style.css">` does not
    resolve inside a blob. Same-folder CSS and JS are inlined so the page can
    be looked at without inventing a way to serve it unauthenticated."""

    def test_a_stylesheet_is_inlined(self):
        html = '<link rel="stylesheet" href="style.css"><h1>x</h1>'
        out = agent.inline_site(html, {"style.css": "body{color:red}"})
        self.assertIn("<style>body{color:red}</style>", out)
        self.assertNotIn("<link", out)

    def test_a_script_is_inlined(self):
        out = agent.inline_site('<script src="app.js"></script>', {"app.js": "var x=1"})
        self.assertIn("var x=1", out)
        self.assertNotIn('src="app.js"', out)

    def test_a_missing_asset_is_left_alone_rather_than_blanked(self):
        html = '<link rel="stylesheet" href="missing.css">'
        self.assertEqual(agent.inline_site(html, {}), html)

    def test_a_remote_asset_is_untouched(self):
        """Nothing here fetches from the network, and the page must not either."""
        html = '<link rel="stylesheet" href="https://cdn.example/x.css">'
        self.assertEqual(agent.inline_site(html, {}), html)


class TestItIsNudgedOffThePlan(SandboxCase):
    """A smaller model often answers the first turn with a plan — "I will
    create index.html, then a stylesheet" — and stops, because describing the
    work reads like doing it. Reported: qwen outlined the steps and ended."""

    def test_a_plan_with_no_call_is_pushed_once(self):
        turns = [{"content": "I will create index.html and style.css.", "tool_calls": None},
                 {"content": "", "tool_calls": [{"id": "1", "type": "function",
                  "function": {"name": "write_file",
                               "arguments": json.dumps({"path": "a.txt", "content": "x"})}}]},
                 {"content": "done", "tool_calls": None}]
        out = agent.run("build it", self.root, lambda m, t: turns.pop(0))
        self.assertEqual(out["steps"], 1)
        self.assertEqual(out["reply"], "done")

    def test_it_is_nudged_only_once(self):
        """If it says the same thing again it means it, and asking repeatedly
        would be a loop of its own."""
        seen = []
        def chat(messages, tools):
            seen.append(len(messages))
            return {"content": "I will do it later.", "tool_calls": None}
        out = agent.run("build it", self.root, chat)
        self.assertEqual(len(seen), 2)
        self.assertEqual(out["steps"], 0)

    def test_a_reply_after_real_work_is_not_nudged(self):
        """Once something has been made, a reply with no call is the summary —
        which is how a run is meant to end."""
        turns = [{"content": "", "tool_calls": [{"id": "1", "type": "function",
                  "function": {"name": "write_file",
                               "arguments": json.dumps({"path": "a.txt", "content": "x"})}}]},
                 {"content": "Made a.txt.", "tool_calls": None}]
        calls = []
        def chat(messages, tools):
            calls.append(1)
            return turns.pop(0)
        out = agent.run("go", self.root, chat)
        self.assertEqual(len(calls), 2)
        self.assertEqual(out["reply"], "Made a.txt.")


class TestThePinIsOnlyAgainstTheReaper(unittest.TestCase):
    """Pinning the text model started out blocking eviction too, which is wrong
    in the other direction: an agent asking for an image would be refused, when
    calling the other tools is the entire point of agent mode. Only one heavy
    model fits, so a media step has to take the slot and the next chat call pays
    a reload."""

    def setUp(self):
        self.src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "supervisor.py"),
            encoding="utf-8").read()

    def test_the_reaper_respects_it(self):
        reaper = self.src[self.src.index("def reaper():"):]
        reaper = reaper[:reaper.index("\ndef ")]
        self.assertIn("is_pinned(", reaper)

    def test_eviction_does_not(self):
        block = self.src[self.src.index("def free_heavy_slot"):]
        block = block[:block.index("\ndef ")]
        self.assertNotIn("is_pinned(", block)

    def test_starting_a_heavy_service_does_not_either(self):
        block = self.src[self.src.index("def start_service(name):"):]
        block = block[:block.index("\ndef ")]
        self.assertNotIn("is_pinned(", block)


class RunStoreCase(unittest.TestCase):
    """A run is only as durable as the tab, and the tab is the least durable
    thing in the system (#67). The record is what outlives it, so these are
    about what survives a second reader, a restart and a cancel — not about the
    loop, which is tested above and did not change shape."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "agent-runs.db")
        self.store = agent.RunStore(self.path)

    def start(self, job="demo", prompt="make a page", model="qwen-coder", steps=12):
        return self.store.create(job=job, prompt=prompt, model=model, max_steps=steps)

    def test_a_new_run_records_what_it_was_asked_for(self):
        rid = self.start()
        record = self.store.get(rid)
        self.assertEqual(record["id"], rid)
        self.assertEqual(record["job"], "demo")
        self.assertEqual(record["prompt"], "make a page")
        self.assertEqual(record["model"], "qwen-coder")
        self.assertEqual(record["state"], agent.RUNNING)
        self.assertEqual(record["steps"], 0)
        self.assertEqual(record["trace"], [])
        self.assertEqual(record["max_steps"], 12)

    def test_an_unknown_id_is_none_rather_than_an_exception(self):
        self.assertIsNone(self.store.get("nope"))

    def test_steps_are_visible_to_another_reader_while_the_run_is_going(self):
        """The whole point: the page that reconnects is not the process that
        started the run, and the run has not finished."""
        rid = self.start()
        self.store.append_step(rid, {"step": 1, "tool": "write_file",
                                     "args": {"path": "a.txt"}, "ok": True,
                                     "result": "wrote a.txt (1 bytes)"})
        other = agent.RunStore(self.path)
        record = other.get(rid)
        self.assertEqual(record["state"], agent.RUNNING)
        self.assertEqual(record["steps"], 1)
        self.assertEqual(record["trace"][0]["tool"], "write_file")
        self.assertEqual(record["trace"][0]["args"]["path"], "a.txt")

    def test_a_long_result_is_trimmed_on_write(self):
        """A twelve-step run with file contents in the results is not small,
        and the record is read on every poll."""
        rid = self.start()
        self.store.append_step(rid, {"step": 1, "tool": "read_file", "args": {},
                                     "ok": True, "result": "x" * 50000})
        kept = self.store.get(rid)["trace"][0]["result"]
        self.assertLessEqual(len(kept), agent.TRACE_RESULT_CHARS + 40)
        self.assertIn("truncated", kept)

    def test_finishing_records_the_reply_and_the_files(self):
        rid = self.start()
        self.store.finish(rid, "done", reply="Made a.txt.", stopped=None,
                          seconds=12.5, files=[{"path": "a.txt", "bytes": 1}])
        record = self.store.get(rid)
        self.assertEqual(record["state"], "done")
        self.assertEqual(record["reply"], "Made a.txt.")
        self.assertEqual(record["files"], [{"path": "a.txt", "bytes": 1}])
        self.assertEqual(record["seconds"], 12.5)

    def test_a_folder_has_at_most_one_run_in_flight(self):
        """Two runs writing into one folder is two models editing the same
        files with no idea about each other."""
        rid = self.start(job="demo")
        self.assertEqual(self.store.active_for("demo"), rid)
        self.assertIsNone(self.store.active_for("other"))
        self.store.finish(rid, "done")
        self.assertIsNone(self.store.active_for("demo"))

    def test_the_latest_run_for_a_folder_is_findable_after_it_ends(self):
        """A page that reconnects knows its folder; it may not know the id."""
        first = self.start(job="demo")
        self.store.finish(first, "done")
        second = self.start(job="demo")
        self.assertEqual(self.store.latest_for("demo")["id"], second)

    def test_recent_is_newest_first(self):
        one = self.start(job="a")
        self.store.finish(one, "done")
        two = self.start(job="b")
        self.assertEqual([r["id"] for r in self.store.recent(10)], [two, one])

    def test_a_run_the_gateway_left_behind_is_marked_interrupted(self):
        """A run lives in sqlite and runs in a thread. A restart keeps the
        record and loses the worker; anything still 'running' at startup is by
        definition orphaned, and must not go on claiming to work."""
        rid = self.start()
        self.store.append_step(rid, {"step": 1, "tool": "write_file", "args": {},
                                     "ok": True, "result": "wrote a.txt"})
        finished = self.start(job="other")
        self.store.finish(finished, "done")

        self.assertEqual(self.store.sweep_interrupted(), 1)
        record = self.store.get(rid)
        self.assertEqual(record["state"], "interrupted")
        self.assertIn("1", record["stage"])
        self.assertEqual(self.store.get(finished)["state"], "done")
        # And it is idempotent: nothing is left to sweep the second time.
        self.assertEqual(self.store.sweep_interrupted(), 0)

    def test_cancelling_asks_the_run_to_stop(self):
        rid = self.start()
        self.assertFalse(self.store.cancelled(rid))
        self.assertTrue(self.store.cancel(rid))
        self.assertTrue(self.store.cancelled(rid))
        # A run that already finished cannot be cancelled.
        done = self.start(job="other")
        self.store.finish(done, "done")
        self.assertFalse(self.store.cancel(done))

    def test_pruning_keeps_what_is_running(self):
        old = self.start(job="old")
        self.store.finish(old, "done")
        live = self.start(job="live")
        self.store._exec("UPDATE runs SET updated_at = ? WHERE id = ?",
                         (time.time() - 40 * 86400, old))
        self.store.prune(7 * 86400)
        self.assertIsNone(self.store.get(old))
        self.assertIsNotNone(self.store.get(live))


class TestTheLoopCanBeStopped(SandboxCase):
    """Blocking a second run against a folder means a wedged run holds the
    folder until the wall-clock cap, thirty minutes away. The button that
    already said "Stop" has to actually stop it."""

    def test_it_stops_between_steps_when_asked(self):
        calls = []

        def chat(messages, tools):
            calls.append(1)
            return {"content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "list_files", "arguments": "{}"}}]}

        out = agent.run("go", self.root, chat, should_stop=lambda: len(calls) >= 2)
        self.assertEqual(out["stopped"], "cancelled")
        self.assertLessEqual(out["steps"], 2)

    def test_it_stops_partway_through_a_batch_of_tool_calls(self):
        """A cancel must not wait for the rest of the turn.

        Measured against the running gateway before this was checked inside the
        batch: cancelled at 4s with nothing done, and the run still took 19
        steps — writing nine files after being asked to stop — because a turn
        carrying nineteen tool calls was one indivisible unit.
        """
        stop = {"now": False}
        turns = []

        def chat(messages, tools):
            turns.append(1)
            return {"content": "", "tool_calls": [
                {"id": "c%d" % i, "type": "function",
                 "function": {"name": "write_file",
                              "arguments": json.dumps({"path": "p%d.txt" % i,
                                                       "content": "x"})}}
                for i in range(10)]}

        def should_stop():
            # Asked once per call: let the first land, then cancel.
            if stop["now"]:
                return True
            stop["now"] = len(os.listdir(self.root)) >= 1
            return False

        out = agent.run("go", self.root, chat, should_stop=should_stop)
        self.assertEqual(out["stopped"], "cancelled")
        # The whole batch was ten. Stopping inside it is the point.
        self.assertLess(out["steps"], 10)
        self.assertEqual(len(turns), 1)
        self.assertLess(len(os.listdir(self.root)), 10)

    def test_a_run_nobody_cancelled_is_unaffected(self):
        out = agent.run("go", self.root, lambda m, t: {"content": "hi", "tool_calls": None},
                        should_stop=lambda: False)
        self.assertIsNone(out["stopped"])


class TestTheStreamIsAViewOfTheRecord(unittest.TestCase):
    """The stream used to be the run: the loop lived in the request handler, so
    closing the tab left a run nothing could re-attach to. It is now a reader of
    the record, which is what makes reconnecting and streaming the same thing."""

    def setUp(self):
        import supervisor
        self.supervisor = supervisor
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = agent.RunStore(os.path.join(self.tmp, "runs.db"))

    def test_a_finished_run_replays_its_whole_trace_then_done(self):
        rid = self.store.create(job="j", prompt="p", model="m", max_steps=12)
        for n in (1, 2):
            self.store.append_step(rid, {"step": n, "tool": "write_file",
                                         "args": {"path": "%d.txt" % n},
                                         "ok": True, "result": "wrote"})
        self.store.finish(rid, "done", reply="Made two files.", seconds=3.0,
                          files=[{"path": "1.txt", "bytes": 1}])
        seen = list(self.supervisor.agent_events(self.store, rid, sleep=lambda s: None))
        self.assertEqual([kind for kind, _ in seen], ["step", "step", "done"])
        self.assertEqual(seen[1][1]["args"]["path"], "2.txt")
        self.assertEqual(seen[-1][1]["reply"], "Made two files.")
        self.assertEqual(seen[-1][1]["state"], "done")

    def test_steps_written_while_it_watches_are_picked_up(self):
        rid = self.store.create(job="j", prompt="p", model="m", max_steps=12)
        ticks = []

        def sleep(_seconds):
            ticks.append(1)
            if len(ticks) == 1:
                self.store.append_step(rid, {"step": 1, "tool": "list_files",
                                             "args": {}, "ok": True, "result": "empty"})
            else:
                self.store.finish(rid, "done", reply="done", seconds=1.0, files=[])

        seen = list(self.supervisor.agent_events(self.store, rid, sleep=sleep))
        self.assertEqual([kind for kind, _ in seen], ["step", "done"])

    def test_a_silent_run_still_writes_something_now_and_then(self):
        """A step can be minutes apart. Nothing is written in between, so a
        client that hung up is never noticed and the thread waits for ever."""
        rid = self.store.create(job="j", prompt="p", model="m", max_steps=12)
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds
            if clock[0] > 60:
                self.store.finish(rid, "done", reply="", seconds=60.0, files=[])

        seen = list(self.supervisor.agent_events(
            self.store, rid, sleep=sleep, now=lambda: clock[0], idle_ping=10))
        self.assertIn("ping", [kind for kind, _ in seen])
        self.assertEqual(seen[-1][0], "done")

    def test_a_run_that_vanished_ends_the_stream(self):
        """A pruned or deleted record must not spin."""
        seen = list(self.supervisor.agent_events(self.store, "nosuchrun",
                                                 sleep=lambda s: None))
        self.assertEqual([kind for kind, _ in seen], ["error"])


class TestTheGatewayOwnsTheRunsRatherThanTheRequest(unittest.TestCase):
    """Structural, because the alternative is a real run: the properties below
    are the ones whose absence brings back exactly the bug in #67."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8") as fh:
            cls.src = fh.read()

    def post_handler(self):
        """The POST branch specifically — GET /v1/agent reads the same route
        name and comes first in the file."""
        post = self.src[self.src.index("    def do_POST(self):"):]
        block = post[post.index('if route == "/v1/agent":'):]
        return block[:block.index("\n        if route ==", 10)]

    def test_the_loop_is_not_run_inside_the_request_handler(self):
        handler = self.post_handler()
        self.assertNotIn("agent.run(", handler,
                         "the loop must be on a worker, or the run is only as "
                         "durable as the connection")
        self.assertIn("start_agent_run(", handler)

    def test_the_worker_records_every_step_as_it_happens(self):
        worker = self.src[self.src.index("def agent_worker("):]
        worker = worker[:worker.index("\ndef ")]
        self.assertIn("append_step", worker)
        self.assertIn("AGENT_RUNS.finish", worker)
        self.assertIn("unpin_service", worker)

    def test_an_interrupted_run_is_swept_at_startup(self):
        main = self.src[self.src.index("def main():"):]
        self.assertIn("AGENT_RUNS.sweep_interrupted()", main)

    def test_the_records_are_pruned_on_the_same_timer_as_the_jobs(self):
        """JobStore.prune() existed and nothing called it for a year; a trace
        of tool results grows faster than that did."""
        reaper = self.src[self.src.index("def reaper():"):]
        reaper = reaper[:reaper.index("\ndef ")]
        self.assertIn("AGENT_RUNS.prune(", reaper)


class TestTheRunEndpointsAreDocumented(unittest.TestCase):
    """Docs change with the code. Three endpoints have shipped here documented
    nowhere; the spec is the thing other people build against."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "openapi.json"), encoding="utf-8") as fh:
            cls.spec = json.load(fh)
        with open(os.path.join(REPO_ROOT, "INTEGRATION.md"), encoding="utf-8") as fh:
            cls.guide = fh.read()

    def test_reading_a_run_back_is_in_the_spec(self):
        self.assertIn("get", self.spec["paths"]["/v1/agent"])
        params = [p["name"] for p in self.spec["paths"]["/v1/agent"]["get"]["parameters"]]
        self.assertIn("id", params)
        self.assertIn("job", params)

    def test_cancelling_is_in_the_spec(self):
        self.assertIn("post", self.spec["paths"]["/v1/agent/cancel"])

    def test_the_conflict_status_is_documented(self):
        self.assertIn("409", self.spec["paths"]["/v1/agent"]["post"]["responses"])

    def test_the_run_id_is_in_the_start_event(self):
        example = (self.spec["paths"]["/v1/agent"]["post"]["responses"]["200"]
                   ["content"]["text/event-stream"]["example"])
        self.assertIn("run_id", example)

    def test_the_guide_says_a_run_survives_the_connection(self):
        self.assertIn("/v1/agent?id=", self.guide)
        self.assertIn("/v1/agent/cancel", self.guide)

    def test_the_route_is_owned_by_the_gateway(self):
        import services
        self.assertIn("/v1/agent/cancel", services.GATEWAY_ROUTES)
        self.assertIsNone(services.resolve("/v1/agent/cancel"))


class TestPriorRunsBecomeHistory(RunStoreCase):
    """A follow-up used to arrive as a brand-new task (#70).

    `run()` built its messages from the system prompt and the prompt in front
    of it, so "now wire the image in" reached the model with no idea what "the
    image" was, what the files were called, or that a previous run had
    happened. The working folder persisted; the conversation did not.

    The record from #67 is the source, rather than a copy held in the page:
    it is server-side, it survives a reconnect, and it is already the thing the
    page polls.
    """

    def settled(self, job="demo", prompt="make a page", reply="I made index.html",
                trace=(), files=(), state="done"):
        rid = self.store.create(job=job, prompt=prompt, model="qwen-coder", max_steps=12)
        for entry in trace:
            self.store.append_step(rid, entry)
        self.store.finish(rid, state, reply=reply, files=list(files))
        return rid

    # -- which runs count ---------------------------------------------------
    def test_a_folder_with_no_history_gives_nothing(self):
        self.assertEqual(self.store.history_for("empty"), [])

    def test_history_is_oldest_first(self):
        self.settled(prompt="first")
        self.settled(prompt="second")
        self.settled(prompt="third")
        self.assertEqual([r["prompt"] for r in self.store.history_for("demo")],
                         ["first", "second", "third"])

    def test_only_this_folder(self):
        self.settled(job="mine", prompt="mine")
        self.settled(job="theirs", prompt="theirs")
        self.assertEqual([r["prompt"] for r in self.store.history_for("mine")], ["mine"])

    def test_the_run_in_flight_is_not_its_own_history(self):
        """Called after `create()`, so the new run is already a row."""
        self.settled(prompt="earlier")
        current = self.start()
        got = self.store.history_for("demo", exclude=current)
        self.assertEqual([r["prompt"] for r in got], ["earlier"])

    def test_only_the_last_few_are_kept(self):
        for i in range(agent.HISTORY_RUNS + 3):
            self.settled(prompt="run %d" % i)
        got = self.store.history_for("demo")
        self.assertEqual(len(got), agent.HISTORY_RUNS)
        self.assertEqual(got[-1]["prompt"], "run %d" % (agent.HISTORY_RUNS + 2))

    def test_an_interrupted_run_is_still_history(self):
        """Its files are in the folder, so pretending it did not happen is the
        one thing that would be wrong."""
        self.settled(prompt="cut short", state="interrupted")
        self.assertEqual(len(self.store.history_for("demo")), 1)

    # -- what a run turns into ---------------------------------------------
    def test_a_run_becomes_a_user_turn_and_an_assistant_turn(self):
        record = self.store.get(self.settled(prompt="make a page",
                                             reply="I made index.html"))
        turns = agent.conversation([record])
        self.assertEqual([t["role"] for t in turns], ["user", "assistant"])
        self.assertEqual(turns[0]["content"], "make a page")
        self.assertIn("I made index.html", turns[1]["content"])

    def test_the_assistant_turn_is_the_models_own_words(self):
        """Measured: putting the tool trace in the assistant's mouth — "I
        called: write_file index.html." — made qwen-coder repeat that sentence
        back and call nothing. A small model imitates the last assistant turn,
        so it must only ever contain what the model actually said."""
        rid = self.settled(
            reply="I made index.html.",
            trace=[{"step": 1, "tool": "write_file",
                    "args": {"path": "index.html", "content": "<h1>hi</h1>"},
                    "ok": True, "result": "wrote index.html"}],
            files=[{"path": "index.html", "bytes": 11}])
        said = agent.conversation([self.store.get(rid)])[1]["content"]
        self.assertEqual(said, "I made index.html.")
        self.assertNotIn("I called", said)
        self.assertNotIn("write_file", said)

    def test_the_folder_is_described_by_the_environment_instead(self):
        """The names still have to reach the model — just not as something it
        supposedly said. `folder_note` goes on the end of the system prompt."""
        note = agent.folder_note([{"path": "index.html", "bytes": 11},
                                  {"path": "art/logo.png", "bytes": 4096}])
        self.assertIn("index.html", note)
        self.assertIn("art/logo.png", note)

    def test_an_empty_folder_says_nothing(self):
        self.assertEqual(agent.folder_note([]), "")
        self.assertEqual(agent.folder_note(None), "")

    def test_a_tool_call_the_model_typed_out_is_not_replayed(self):
        """#73: qwen-coder sometimes prints its calls instead of making them,
        and the run does nothing. History is where that compounds — replayed,
        the block is the example the next turn copies. Measured before this
        guard: turn three reproduced turn two's JSON word for word."""
        printed = ('```json\n[\n  {\n    "name": "write_file",\n'
                   '    "arguments": {"path": "index.html", "content": "<h1>hi</h1>"}\n'
                   '  }\n]\n```')
        self.assertTrue(agent.looks_like_a_printed_tool_call(printed))
        rid = self.settled(reply=printed)
        said = agent.conversation([self.store.get(rid)])[1]["content"]
        self.assertNotIn("write_file", said)
        self.assertTrue(said.strip())

    def test_a_truncated_printed_call_is_caught_too(self):
        """The reply is cut at the store's limit, so the common case does not
        parse as JSON at all."""
        self.assertTrue(agent.looks_like_a_printed_tool_call(
            '{"name": "write_file", "arguments": {"path": "notes.md", "content": "## Gui'))

    def test_an_ordinary_reply_is_left_alone(self):
        for good in ("I made index.html.",
                     "I wrote notes.md with three bullets, then read it back.",
                     "Here is what I did: {nothing structured}"):
            self.assertFalse(agent.looks_like_a_printed_tool_call(good), good)

    def test_a_files_worth_of_content_is_not_replayed(self):
        """The whole point of trimming: `args.content` carries the file, and a
        history that repeats every byte the run wrote is the run again."""
        rid = self.settled(trace=[{"step": 1, "tool": "write_file",
                                   "args": {"path": "big.txt", "content": "x" * 50000},
                                   "ok": True, "result": "wrote big.txt"}])
        said = agent.conversation([self.store.get(rid)])[1]["content"]
        self.assertLess(len(said), 4000)
        self.assertNotIn("x" * 2000, said)

    def test_a_run_that_stopped_early_says_so(self):
        rid = self.settled(prompt="ten pages", reply="", state="cancelled")
        said = agent.conversation([self.store.get(rid)])[1]["content"]
        self.assertIn("cancelled", said)

    def test_an_assistant_turn_is_never_empty(self):
        """An empty assistant message is a turn some backends reject outright."""
        rid = self.settled(reply="", state="done")
        said = agent.conversation([self.store.get(rid)])[1]["content"]
        self.assertTrue(said.strip())

    # -- what the loop does with it ----------------------------------------
    def test_the_loop_puts_history_between_the_system_prompt_and_the_ask(self):
        seen = []

        def chat(messages, tools):
            seen.append([dict(m) for m in messages])
            return {"content": "done", "tool_calls": None}

        history = [{"role": "user", "content": "make a page"},
                   {"role": "assistant", "content": "I made index.html"}]
        agent.run("now wire the image in", self.tmp, chat, history=history)
        roles = [m["role"] for m in seen[0]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertEqual(seen[0][1]["content"], "make a page")
        self.assertEqual(seen[0][-1]["content"], "now wire the image in")

    def test_without_history_the_loop_is_what_it_was(self):
        seen = []

        def chat(messages, tools):
            seen.append([dict(m) for m in messages])
            return {"content": "done", "tool_calls": None}

        agent.run("make a page", self.tmp, chat)
        self.assertEqual([m["role"] for m in seen[0]], ["system", "user"])


class TestTheSystemPromptSaysWhatAFollowUpMustDo(unittest.TestCase):
    """The unwired image was the same fault one level down (#70): the model
    generated `art/logo.png`, was told "saved art/logo.png", and nothing said
    an asset it makes has to be referenced from the page it wrote."""

    def test_it_is_told_to_look_before_assuming(self):
        self.assertIn("list_files", agent.SYSTEM)

    def test_it_is_told_an_unreferenced_asset_is_not_delivered(self):
        low = agent.SYSTEM.lower()
        self.assertIn("reference", low)
        self.assertTrue("generate" in low or "asset" in low)


class TestACallTypedOutIsStillACall(SandboxCase):
    """#73: asked for a page *and* an image in one prompt, qwen-coder stops
    calling tools and writes the calls out as a fenced JSON block instead. The
    run settles `done` having done nothing, which from outside is exactly the
    "it one-shots and ignores feedback" that #70 was reported as.

    The blocks are complete and well formed — checked against four stored
    records, all ending in a closed `]` and a closing fence — so the model did
    the work and used the wrong channel. Reading it is the same move #64 made
    for the shapes Qwen emits natively.
    """

    FENCED = ('```json\n[\n  {\n    "name": "write_file",\n'
              '    "arguments": {"path": "index.html", "content": "<h1>hi</h1>"}\n'
              '  },\n  {\n    "name": "generate_image",\n'
              '    "arguments": {"prompt": "a logo", "path": "logo.png"}\n'
              '  }\n]\n```')

    def test_a_fenced_block_of_calls_is_read(self):
        calls = agent.printed_tool_calls(self.FENCED)
        self.assertEqual([c["function"]["name"] for c in calls],
                         ["write_file", "generate_image"])
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(args["path"], "index.html")

    def test_every_call_gets_its_own_id(self):
        calls = agent.printed_tool_calls(self.FENCED)
        ids = [c["id"] for c in calls]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(all(c["type"] == "function" for c in calls))

    def test_a_bare_object_counts(self):
        calls = agent.printed_tool_calls(
            '{"name": "list_files", "arguments": {}}')
        self.assertEqual([c["function"]["name"] for c in calls], ["list_files"])

    def test_an_unfenced_list_counts(self):
        calls = agent.printed_tool_calls(
            '[{"name": "read_file", "arguments": {"path": "a.md"}}]')
        self.assertEqual(len(calls), 1)

    def test_arguments_given_as_a_string_are_accepted(self):
        calls = agent.printed_tool_calls(
            '{"name": "read_file", "arguments": "{\\"path\\": \\"a.md\\"}"}')
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["path"], "a.md")

    # -- what must not be read as a call ----------------------------------
    def test_ordinary_prose_is_not_a_call(self):
        for text in ("I made index.html.", "", "Here is some JSON: {\"a\": 1}",
                     "```json\n{\"title\": \"notes\"}\n```"):
            self.assertEqual(agent.printed_tool_calls(text), [], text)

    def test_a_name_that_is_not_a_tool_is_not_a_call(self):
        self.assertEqual(agent.printed_tool_calls(
            '{"name": "rm_rf", "arguments": {"path": "/"}}'), [])

    def test_a_truncated_block_is_refused(self):
        """Half a write_file is a broken file. Better to do nothing than to
        execute a call whose arguments were cut off."""
        self.assertEqual(agent.printed_tool_calls(
            '```json\n[{"name": "write_file", "arguments": {"path": "a.html", "cont'), [])

    def test_one_bad_item_refuses_the_whole_batch(self):
        self.assertEqual(agent.printed_tool_calls(
            '[{"name": "list_files", "arguments": {}}, {"name": "nope", "arguments": {}}]'),
            [])

    # -- and the loop acts on them ----------------------------------------
    def test_the_loop_runs_a_printed_call(self):
        turns = []

        def chat(messages, tools):
            turns.append(1)
            if len(turns) == 1:
                return {"content": ('```json\n[{"name": "write_file", "arguments": '
                                    '{"path": "typed.md", "content": "hi"}}]\n```'),
                        "tool_calls": None}
            return {"content": "I wrote typed.md.", "tool_calls": None}

        out = agent.run("make typed.md", self.root, chat)
        self.assertEqual(out["steps"], 1)
        self.assertEqual(out["trace"][0]["tool"], "write_file")
        self.assertTrue(os.path.exists(os.path.join(self.root, "typed.md")))
        self.assertEqual(out["reply"], "I wrote typed.md.")

    def test_the_block_does_not_become_the_reply(self):
        """It was never an answer, and it must not be shown as one — nor go
        into the next run's history as something to imitate."""
        def chat(messages, tools):
            return {"content": ('[{"name": "list_files", "arguments": {}}]'),
                    "tool_calls": None}

        out = agent.run("look", self.root, chat, max_steps=2)
        self.assertNotIn("list_files", out["reply"])


class TestAnAssetNothingPointsAtIsNotDone(SandboxCase):
    """The reported half of #70: "It generated an image for the website but did
    not wire it in." The model writes the page, then generates the image, and
    never goes back — asking it to in the system prompt did not move it across
    three measured runs. So it is asked directly, once, the same way the plan
    nudge works, and only when the folder actually shows the fault.
    """

    def image_then_stop(self, extra=None):
        """A model that writes a page, makes a logo, and stops."""
        turns = []

        def chat(messages, tools):
            turns.append(messages[-1].get("content") or "")
            if len(turns) == 1:
                return {"content": "", "tool_calls": [
                    {"id": "a", "type": "function", "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "index.html",
                                                 "content": "<h1>Tuning</h1>"})}},
                    {"id": "b", "type": "function", "function": {
                        "name": "generate_image",
                        "arguments": json.dumps({"prompt": "a logo",
                                                 "path": "logo.png"})}}]}
            if extra and len(turns) == 2:
                return extra(messages)
            return {"content": "I made the page and a logo.", "tool_calls": None}

        def media(name, args, dest):
            with open(dest, "wb") as fh:
                fh.write(b"\x89PNG fake")
            return True, "saved %s" % os.path.relpath(dest, self.root)

        return chat, media, turns

    def test_it_is_told_when_the_asset_is_not_referenced(self):
        chat, media, turns = self.image_then_stop()
        agent.run("make a site with a logo", self.root, chat, media=media)
        self.assertGreaterEqual(len(turns), 3)
        self.assertIn("logo.png", turns[2])

    def test_the_nudge_names_the_page_and_asks_for_one_call(self):
        """Asked to read then write, qwen-coder answered with fabricated
        <tool_response> blocks for both — it simulated the chain instead of
        running it. One named call is what it can actually do."""
        chat, media, turns = self.image_then_stop()
        agent.run("make a site with a logo", self.root, chat, media=media)
        self.assertIn("index.html", turns[2])
        self.assertIn("One tool call", turns[2])
        self.assertNotIn("read_file", turns[2])

    def test_a_fabricated_transcript_is_not_shown_as_the_summary(self):
        def chat(messages, tools):
            return {"content": "<tool_response>\nwrote index.html\n</tool_response>\nAll done.",
                    "tool_calls": None}

        out = agent.run("go", self.root, chat)
        self.assertNotIn("tool_response", out["reply"])
        self.assertIn("All done.", out["reply"])

    def test_it_gets_the_chance_to_fix_it(self):
        def wire(messages):
            return {"content": "", "tool_calls": [
                {"id": "c", "type": "function", "function": {
                    "name": "write_file",
                    "arguments": json.dumps({
                        "path": "index.html",
                        "content": '<h1>Tuning</h1><img src="logo.png">'})}}]}

        chat, media, _ = self.image_then_stop(extra=wire)
        out = agent.run("make a site with a logo", self.root, chat, media=media)
        page = open(os.path.join(self.root, "index.html")).read()
        self.assertIn('src="logo.png"', page)
        self.assertIsNone(out["stopped"])

    def test_it_is_asked_only_once(self):
        """A model that will not do it is not going to do it on the fourth
        asking, and a loop of its own is worse than the fault."""
        chat, media, turns = self.image_then_stop()
        agent.run("make a site with a logo", self.root, chat, media=media)
        self.assertEqual(sum(1 for t in turns if "logo.png" in t and "reference" in t), 1)

    def test_an_asset_already_referenced_is_left_alone(self):
        def chat(messages, tools):
            if not os.path.exists(os.path.join(self.root, "logo.png")):
                return {"content": "", "tool_calls": [
                    {"id": "a", "type": "function", "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "index.html",
                                                 "content": '<img src="logo.png">'})}},
                    {"id": "b", "type": "function", "function": {
                        "name": "generate_image",
                        "arguments": json.dumps({"prompt": "a logo",
                                                 "path": "logo.png"})}}]}
            return {"content": "Done.", "tool_calls": None}

        def media(name, args, dest):
            with open(dest, "wb") as fh:
                fh.write(b"png")
            return True, "saved %s" % os.path.relpath(dest, self.root)

        out = agent.run("make it", self.root, chat, media=media)
        self.assertEqual(out["reply"], "Done.")
        self.assertEqual(out["steps"], 2)

    def test_an_asset_with_no_page_to_go_in_is_left_alone(self):
        """Asked only for a sound effect, there is nothing to wire it into."""
        def chat(messages, tools):
            if not os.path.exists(os.path.join(self.root, "beep.wav")):
                return {"content": "", "tool_calls": [
                    {"id": "a", "type": "function", "function": {
                        "name": "generate_sfx",
                        "arguments": json.dumps({"description": "a beep",
                                                 "path": "beep.wav"})}}]}
            return {"content": "Here is the beep.", "tool_calls": None}

        def media(name, args, dest):
            with open(dest, "wb") as fh:
                fh.write(b"wav")
            return True, "saved %s" % os.path.relpath(dest, self.root)

        out = agent.run("make a beep", self.root, chat, media=media)
        self.assertEqual(out["reply"], "Here is the beep.")

    def test_the_check_itself(self):
        open(os.path.join(self.root, "index.html"), "w").write("<h1>no image</h1>")
        open(os.path.join(self.root, "logo.png"), "wb").write(b"png")
        trace = [{"tool": "generate_image", "ok": True, "result": "saved logo.png"}]
        self.assertEqual(agent.unreferenced_asset(self.root, trace), "logo.png")

        open(os.path.join(self.root, "index.html"), "w").write('<img src="logo.png">')
        self.assertIsNone(agent.unreferenced_asset(self.root, trace))


class TestAMediaCallIsBounded(SandboxCase):
    """A crashed image backend cost a run half an hour (#75, #76).

    The media calls inherited `_local_json`'s default of PROXY_TIMEOUT — the
    *music* timeout, and music is not one of the agent's tools. The worker sat
    on a dead socket, the record claimed `running`, and a cancel issued
    fourteen minutes in could not be looked at until the socket gave up.
    """

    def test_every_generator_has_a_timeout(self):
        """Asserted against the tool list rather than a copy of it: a fourth
        generator must not quietly inherit the half-hour."""
        self.assertEqual(set(supervisor.MEDIA_TIMEOUTS), set(agent.ASSET_TOOLS))

    def test_none_of_them_is_the_proxy_default(self):
        for tool, seconds in supervisor.MEDIA_TIMEOUTS.items():
            self.assertLess(seconds, supervisor.PROXY_TIMEOUT, tool)

    def test_an_image_is_allowed_longer_than_the_slowest_one_measured(self):
        """89 generations in this machine's log: max 164.1 s. The bound has to
        clear that with room for an eviction and a cold load, or a legitimate
        picture starts failing."""
        self.assertGreater(supervisor.MEDIA_TIMEOUTS["generate_image"], 164.1 * 2)

    def test_a_timeout_is_a_failed_step_not_a_failed_run(self):
        """The loop has to get control back — that is the whole point. A run
        that dies here cannot see the cancel it was already asked for."""
        def media(name, args, dest):
            raise socket.timeout("timed out")

        seen = []

        def chat(messages, tools):
            if not seen:
                seen.append(1)
                return {"content": "", "tool_calls": [
                    {"id": "a", "type": "function", "function": {
                        "name": "generate_image",
                        "arguments": json.dumps({"prompt": "a logo",
                                                 "path": "logo.png"})}}]}
            return {"content": "The image did not work, so here is the page.",
                    "tool_calls": None}

        out = agent.run("make a logo", self.root, chat, media=media)
        self.assertEqual(out["steps"], 1)
        self.assertFalse(out["trace"][0]["ok"])
        self.assertIsNone(out["stopped"])
        self.assertIn("The image did not work", out["reply"])

    def test_a_cancel_asked_for_during_the_call_is_seen_when_it_returns(self):
        """The case actually hit: stop pressed while the image call was wedged.
        It cannot interrupt the call, but it must act the moment it is back."""
        stopping = {"asked": False}

        def media(name, args, dest):
            stopping["asked"] = True          # the user presses Stop mid-call
            raise socket.timeout("timed out")

        def chat(messages, tools):
            return {"content": "", "tool_calls": [
                {"id": "a", "type": "function", "function": {
                    "name": "generate_image",
                    "arguments": json.dumps({"prompt": "x", "path": "a.png"})}},
                {"id": "b", "type": "function", "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "after.md", "content": "no"})}}]}

        out = agent.run("go", self.root, chat, media=media,
                        should_stop=lambda: stopping["asked"])
        self.assertEqual(out["stopped"], "cancelled")
        self.assertFalse(os.path.exists(os.path.join(self.root, "after.md")))


class TestARunSaysWhatItIsDoingNow(RunStoreCase):
    """"Is it stuck or actually doing something?" had no answer in the record.

    `stage` was written only when a step *completed*, so a run sitting in a
    five-minute image generation named the last thing that finished — which is
    indistinguishable from a run whose worker had died (#75).
    """

    def test_the_stage_names_the_call_in_flight(self):
        rid = self.start()
        self.store.working(rid, 5, "generate_image")
        record = self.store.get(rid)
        self.assertIn("generate_image", record["stage"])
        self.assertIn("running", record["stage"])
        # The trace is still four steps: this one has not come back.
        self.assertEqual(record["steps"], 0)

    def test_a_run_being_stopped_keeps_saying_so(self):
        """Otherwise the next call overwrites "stopping" and the person who
        pressed Stop watches it claim to start more work."""
        rid = self.start()
        self.store.cancel(rid)
        self.store.working(rid, 2, "write_file")
        self.assertEqual(self.store.get(rid)["stage"], "stopping")

    def test_the_loop_reports_before_it_runs_the_tool(self):
        order = []

        def chat(messages, tools):
            if not order:
                order.append("ask")
                return {"content": "", "tool_calls": [
                    {"id": "a", "type": "function", "function": {
                        "name": "list_files", "arguments": "{}"}}]}
            return {"content": "done", "tool_calls": None}

        agent.run("go", self.tmp, chat,
                  on_call=lambda n, tool: order.append("call:%d:%s" % (n, tool)),
                  on_step=lambda e: order.append("step:%d" % e["step"]))
        self.assertEqual(order, ["ask", "call:1:list_files", "step:1"])
