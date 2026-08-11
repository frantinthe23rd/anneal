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

from tests.context import REPO_ROOT

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
