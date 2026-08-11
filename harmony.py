#!/usr/bin/env python3
"""Translate GPT-OSS's harmony channels into the OpenAI shape.

GPT-OSS does not answer in plain text. It emits channels:

    <|channel|>analysis<|message|>working it out<|end|>
    <|start|>assistant<|channel|>final<|message|>the answer

and a tool call is a channel too:

    <|start|>assistant<|channel|>commentary to=functions.read_file
    <|constrain|>json<|message|>{"path": "/tmp/notes.txt"}

`mlx_lm.server` passes that through verbatim, so measured against the running
gateway: `tool_calls` was null, `finish_reason` was "stop", and the whole thing
arrived as `content`. A client written to the OpenAI contract sees a model that
ignored its tools and replied with markup. Gemma returns a parsed `tool_calls`
for the same request, so the difference is the model's format and not the
client's expectations.

The gateway already rewrites the request on its way in — substituting the model
path, choosing which model answers. The response is the same boundary, and this
is the smallest thing that makes the fastest model that fits usable by an agent.

Deliberately narrow: anything without channel markers is returned untouched, so
no other model's output can be altered by this.
"""

from __future__ import annotations

import json
import re
import uuid

# The markers only ever appear in harmony output. A reply that merely discusses
# them is not affected, because parsing needs a channel *and* a message.
CHANNEL_RE = re.compile(
    r"<\|channel\|>(?P<name>[^<|]*?)(?:\s+to=(?P<recipient>[^\s<|]+))?\s*"
    r"(?:<\|constrain\|>[^<|]*)?<\|message\|>(?P<body>.*?)"
    r"(?=<\|end\|>|<\|start\|>|<\|return\|>|<\|channel\|>|$)",
    re.S)


# `<|im_end|>`, `<|eot_id|>`, `<|end|>` — every model has one and they differ,
# so this matches the shape rather than a list that goes stale the next time a
# model is added. Deliberately narrow: the pipes are required, so "a < b" and
# "x | y" are untouched.
CONTROL_TOKEN_RE = re.compile(r"<\|[^<>|]{0,40}\|>")


def strip_control_tokens(text):
    """Remove stop and role tokens a template leaked into the reply.

    mlx_lm passes them through, so a Qwen reply arrived as "Hi there! How can I
    assist you today?<|im_end|>" — the stop token rendered as text.
    """
    if not text or "<|" not in text:
        return text
    return CONTROL_TOKEN_RE.sub("", text).strip()


def looks_like_harmony(text):
    return bool(text) and "<|channel|>" in text and "<|message|>" in text


def _clean(text):
    """Strip any stray control markers a truncated reply left behind."""
    return re.sub(r"<\|[^|>]*\|>", "", text or "").strip()


def parse(text):
    """Split harmony output into content, reasoning and tool calls."""
    out = {"content": "", "reasoning": "", "tool_calls": None}
    if not looks_like_harmony(text):
        out["content"] = text or ""
        return out

    finals, analysis, calls = [], [], []
    for m in CHANNEL_RE.finditer(text):
        name = (m.group("name") or "").strip()
        body = m.group("body") or ""
        recipient = m.group("recipient")
        if recipient:
            # A tool call. Arguments that will not parse are dropped rather
            # than forwarded: handing a client junk to pass to a real function
            # is worse than reporting no call at all.
            try:
                args = json.loads(body.strip())
            except ValueError:
                continue
            calls.append({
                "id": "call_" + uuid.uuid4().hex[:24],
                "type": "function",
                "function": {"name": recipient.split(".")[-1],
                             "arguments": json.dumps(args)},
            })
        elif name == "analysis":
            analysis.append(_clean(body))
        elif name in ("final", ""):
            finals.append(_clean(body))
        else:
            # An unknown channel is not content. Keeping it would put the
            # model's private notes in front of a user.
            continue

    out["reasoning"] = "\n".join(a for a in analysis if a)
    # A call and a final answer do not co-occur, and if they did the client is
    # expected to act on the call — so content stays empty when one is present.
    out["content"] = "" if calls else "\n".join(f for f in finals if f)
    out["tool_calls"] = calls or None
    return out


# Qwen answers a tool turn as markup, not as `tool_calls`. Two shapes seen:
# `<function name="x" arguments='{...}'/>` (often inside a ```xml fence) and the
# `<tool_call>{"name": ..., "arguments": {...}}</tool_call>` its card documents.
# mlx_lm parses neither, so the reply came back with `tool_calls: None` and
# `finish_reason: stop` — a model that appears to have described the work and
# stopped, which is exactly what it looked like from the outside.
FUNCTION_TAG_RE = re.compile(
    r"""<function\s+name=["'](?P<name>[^"']+)["']\s+arguments=["'](?P<args>.*?)["']\s*/?>""",
    re.S)
TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(?P<body>\{.*?\})\s*</tool_call>", re.S)


# And a third shape, which is what Qwen actually produced here: a fenced JSON
# object with `name` and `arguments`, one fence per call. Both keys are required
# and `arguments` must be an object, so a fence that merely contains JSON with a
# `name` field — a package.json, a manifest — is not mistaken for a call.
JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_fenced_calls(text):
    if not text or "```" not in text:
        return []
    out = []
    for m in JSON_FENCE_RE.finditer(text):
        try:
            payload = json.loads(m.group(1))
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        name, args = payload.get("name"), payload.get("arguments")
        if isinstance(name, str) and isinstance(args, dict):
            out.append(_call(name, args))
    return out


def parse_markup_calls(text):
    """Tool calls a model wrote as markup, in OpenAI shape. [] if there are none."""
    if not text or "<" not in text:
        return []
    out = []
    for m in FUNCTION_TAG_RE.finditer(text):
        try:
            args = json.loads(m.group("args"))
        except ValueError:
            continue
        out.append(_call(m.group("name"), args))
    for m in TOOL_CALL_TAG_RE.finditer(text):
        try:
            payload = json.loads(m.group("body"))
        except ValueError:
            continue
        name = payload.get("name")
        args = payload.get("arguments")
        if isinstance(name, str) and isinstance(args, dict):
            out.append(_call(name, args))
    return out


def _call(name, args):
    return {"id": "call_" + uuid.uuid4().hex[:24], "type": "function",
            "function": {"name": str(name).split(".")[-1],
                         "arguments": json.dumps(args)}}


def rewrite(body):
    """Rewrite a chat-completions response in place, if it is harmony.

    Anything that is not a chat response, or not harmony, is returned exactly as
    it came — this must never be able to change another model's output.
    """
    if not isinstance(body, dict):
        return body
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return body
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if not looks_like_harmony(content):
            cleaned = strip_control_tokens(content)
            # A call written as markup is still a call. Without this the loop
            # sees a reply with no tool_calls and stops, having done nothing.
            markup = parse_markup_calls(cleaned) or parse_fenced_calls(cleaned)
            if markup:
                message["tool_calls"] = markup
                message["content"] = ""
                choice["finish_reason"] = "tool_calls"
            elif cleaned != content:
                message["content"] = cleaned
            continue
        parsed = parse(content)
        message["content"] = parsed["content"]
        if parsed["reasoning"]:
            message["reasoning"] = parsed["reasoning"]
        if parsed["tool_calls"]:
            message["tool_calls"] = parsed["tool_calls"]
            choice["finish_reason"] = "tool_calls"
    return body
