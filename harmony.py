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
        if not isinstance(content, str) or not looks_like_harmony(content):
            continue
        parsed = parse(content)
        message["content"] = parsed["content"]
        if parsed["reasoning"]:
            message["reasoning"] = parsed["reasoning"]
        if parsed["tool_calls"]:
            message["tool_calls"] = parsed["tool_calls"]
            choice["finish_reason"] = "tool_calls"
    return body
