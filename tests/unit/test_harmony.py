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


class TestControlTokensDoNotReachTheReader(unittest.TestCase):
    """Qwen ends a turn with `<|im_end|>` and mlx_lm passes it through, so a
    reply arrived as "Hi there! How can I assist you today?<|im_end|>" — the
    stop token rendered as text. Measured against the running gateway.

    Every model has one of these and they differ, so the fix is to strip the
    control-token *shape* rather than a list of tokens that goes stale the next
    time a model is added.
    """

    def test_a_trailing_stop_token_is_removed(self):
        body = {"choices": [{"message": {"role": "assistant",
                                         "content": "Hi there!<|im_end|>\n"}}]}
        self.assertEqual(harmony.rewrite(body)["choices"][0]["message"]["content"],
                         "Hi there!")

    def test_tokens_in_the_middle_go_too(self):
        body = {"choices": [{"message": {"content": "one<|im_end|>two"}}]}
        self.assertEqual(harmony.rewrite(body)["choices"][0]["message"]["content"],
                         "onetwo")

    def test_ordinary_text_is_untouched(self):
        for text in ("a < b and c > d", "if (x <| y) return", "<b>bold</b>",
                     "shell pipe a | b"):
            body = {"choices": [{"message": {"content": text}}]}
            self.assertEqual(harmony.rewrite(body)["choices"][0]["message"]["content"],
                             text)

    def test_harmony_still_wins_where_it_applies(self):
        body = {"choices": [{"message": {"content": ANALYSIS_ONLY}}]}
        out = harmony.rewrite(body)["choices"][0]["message"]
        self.assertEqual(out["content"], "Here is the answer.")
        self.assertEqual(out["reasoning"], "Thinking about it.")


class TestCallsWrittenAsMarkup(unittest.TestCase):
    """Qwen answers a tool turn as markup. mlx_lm does not parse it, so the
    reply arrived with `tool_calls: None` and `finish_reason: stop` — and an
    agent run ended after zero steps having described the work and done none of
    it. Measured against the running gateway."""

    FUNC = ("```xml\n<function name=\"write_file\" "
            "arguments='{\"path\": \"a.txt\", \"content\": \"hello\"}'/>\n```")
    TOOLCALL = '<tool_call>{"name": "list_files", "arguments": {}}</tool_call>'

    def rewrite(self, content):
        body = {"choices": [{"message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}]}
        out = harmony.rewrite(body)
        return out["choices"][0]

    def test_the_function_tag_becomes_a_tool_call(self):
        choice = self.rewrite(self.FUNC)
        call = choice["message"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "write_file")
        self.assertEqual(json.loads(call["function"]["arguments"]),
                         {"path": "a.txt", "content": "hello"})
        self.assertEqual(choice["finish_reason"], "tool_calls")

    def test_the_documented_tool_call_tag_works_too(self):
        call = self.rewrite(self.TOOLCALL)["message"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "list_files")

    def test_the_markup_does_not_also_remain_as_content(self):
        """A client that renders content would print the call as text."""
        self.assertEqual(self.rewrite(self.FUNC)["message"]["content"], "")

    def test_prose_that_merely_mentions_a_tag_is_untouched(self):
        text = "Use the <function> element when you want a call."
        self.assertIsNone(self.rewrite(text)["message"].get("tool_calls"))

    def test_unparseable_arguments_are_not_forwarded(self):
        bad = '<function name="write_file" arguments=\'{not json\'/>'
        self.assertIsNone(self.rewrite(bad)["message"].get("tool_calls"))


class TestCallsWrittenAsFencedJson(unittest.TestCase):
    """The shape Qwen actually produced: a ```json fence holding
    {"name": ..., "arguments": {...}}, one fence per call. Measured — an agent
    run ended at zero steps because nothing recognised it."""

    def rewrite(self, content):
        body = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        return harmony.rewrite(body)["choices"][0]

    def test_a_fenced_call_is_recognised(self):
        text = '```json\n{"name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}\n```'
        call = self.rewrite(text)["message"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "write_file")

    def test_two_fences_are_two_calls(self):
        text = ('```json\n{"name": "write_file", "arguments": {"path": "a"}}\n```\n\n'
                '```json\n{"name": "list_files", "arguments": {}}\n```')
        self.assertEqual(len(self.rewrite(text)["message"]["tool_calls"]), 2)

    def test_json_that_is_not_a_call_is_left_alone(self):
        """A fenced package.json has a name and is not a tool call."""
        text = '```json\n{"name": "my-app", "version": "1.0.0"}\n```'
        self.assertIsNone(self.rewrite(text)["message"].get("tool_calls"))
        self.assertIn("my-app", self.rewrite(text)["message"]["content"])
