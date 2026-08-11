#!/usr/bin/env python3
"""GPT-OSS speaks harmony; the OpenAI shape is what clients read (#55).

The model works and is the fastest that fits — 13 tok/s against 10 for the
default, 11.18 GB peak against about 11.8 GB usable. What it does not do is
answer in the shape an agent expects. `mlx_lm.server` passes its output through
verbatim, so a tool call arrives as text:

    <|channel|>analysis<|message|>The user wants to read a file...<|end|>
    <|start|>assistant<|channel|>commentary to=functions.read_file
    <|constrain|>json<|message|>{"path":"/tmp/notes.txt"}

`tool_calls` was null and `finish_reason` was "stop", so a client written to the
OpenAI contract sees a model that ignored its tools and replied with markup.
Measured, not assumed — Gemma returns a parsed `tool_calls` for the same
request.

Translating is the gateway's job. It already rewrites the request; the response
is the same boundary.
"""

import json
import unittest

from tests.context import REPO_ROOT  # noqa: F401

import harmony


ANALYSIS_ONLY = ("<|channel|>analysis<|message|>Thinking about it.<|end|>"
                 "<|start|>assistant<|channel|>final<|message|>Here is the answer.")
TOOL_CALL = ('<|channel|>analysis<|message|>We need the file.<|end|>'
             '<|start|>assistant<|channel|>commentary to=functions.read_file '
             '<|constrain|>json<|message|>{"path":"/tmp/notes.txt"}')
PLAIN = "Just an ordinary reply with no channels at all."


class TestDetecting(unittest.TestCase):
    def test_plain_text_is_left_alone(self):
        """Every other model's output must pass through untouched."""
        self.assertFalse(harmony.looks_like_harmony(PLAIN))

    def test_channels_are_recognised(self):
        self.assertTrue(harmony.looks_like_harmony(ANALYSIS_ONLY))
        self.assertTrue(harmony.looks_like_harmony(TOOL_CALL))


class TestSplitting(unittest.TestCase):
    def test_the_final_channel_becomes_the_content(self):
        out = harmony.parse(ANALYSIS_ONLY)
        self.assertEqual(out["content"], "Here is the answer.")

    def test_the_analysis_becomes_reasoning(self):
        """It is worth keeping — the page already shows Gemma's."""
        self.assertEqual(harmony.parse(ANALYSIS_ONLY)["reasoning"], "Thinking about it.")

    def test_no_channel_markers_survive(self):
        for field in harmony.parse(ANALYSIS_ONLY).values():
            if isinstance(field, str):
                self.assertNotIn("<|", field)


class TestToolCalls(unittest.TestCase):
    def test_a_commentary_call_becomes_a_tool_call(self):
        out = harmony.parse(TOOL_CALL)
        calls = out["tool_calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]),
                         {"path": "/tmp/notes.txt"})

    def test_it_carries_an_id_and_a_type(self):
        """A client echoes both back on the tool result turn."""
        call = harmony.parse(TOOL_CALL)["tool_calls"][0]
        self.assertEqual(call["type"], "function")
        self.assertTrue(call["id"])

    def test_the_namespace_prefix_is_stripped(self):
        # The model says functions.read_file; the tool is named read_file.
        self.assertNotIn("functions.",
                         harmony.parse(TOOL_CALL)["tool_calls"][0]["function"]["name"])

    def test_content_is_empty_when_it_called_a_tool(self):
        """A client that renders content would otherwise print the reasoning."""
        self.assertEqual(harmony.parse(TOOL_CALL)["content"], "")

    def test_no_tool_calls_when_there_are_none(self):
        self.assertIsNone(harmony.parse(ANALYSIS_ONLY)["tool_calls"])

    def test_arguments_that_are_not_json_are_dropped_rather_than_forwarded(self):
        """A malformed call is worse than none: a client would pass junk to a
        real function."""
        bad = ('<|start|>assistant<|channel|>commentary to=functions.read_file '
               '<|constrain|>json<|message|>{not json')
        self.assertIsNone(harmony.parse(bad)["tool_calls"])


class TestTheEnvelope(unittest.TestCase):
    def test_a_response_is_rewritten_in_place(self):
        body = {"choices": [{"message": {"role": "assistant", "content": TOOL_CALL},
                             "finish_reason": "stop"}]}
        out = harmony.rewrite(body)
        msg = out["choices"][0]["message"]
        self.assertEqual(out["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "read_file")

    def test_a_plain_response_is_untouched(self):
        body = {"choices": [{"message": {"role": "assistant", "content": PLAIN},
                             "finish_reason": "stop"}]}
        self.assertEqual(harmony.rewrite(body), body)

    def test_something_that_is_not_a_chat_response_is_returned_as_is(self):
        for junk in ({}, {"choices": []}, {"choices": [{}]}, {"error": "x"}):
            self.assertEqual(harmony.rewrite(junk), junk)


if __name__ == "__main__":
    unittest.main()
