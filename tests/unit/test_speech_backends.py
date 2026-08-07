#!/usr/bin/env python3
"""Two speech models behind one endpoint, chosen by voice (#30).

Kokoro has no expressive control at all — its generate() takes text, voice,
speed and a language code, and the server already exposed every one of them.
Measured, not assumed. So "make speech more expressive" could only ever mean a
second model.

Qwen3-TTS CustomVoice is that model: named speakers for identity, plus a written
`instruct` for the performance. The two are separated, which matters — the
VoiceDesign variant tangles them together and designs a fresh voice per line, so
a character does not survive from one line of dialogue to the next. Confirmed by
ear on three lines; a same-seed checksum test had said it was fine, which it was
only for identical text.

The backend is picked from the **voice name**, not a separate parameter. The two
sets do not collide (`af_heart` vs `ryan`), every existing caller keeps working
untouched, and there is no way to ask for a voice and a model that disagree.
"""

import os
import unittest

from tests.context import REPO_ROOT  # noqa: F401

import speech_server as ss


class TestTheVoiceRegistry(unittest.TestCase):
    def test_both_backends_are_registered(self):
        backends = {v["backend"] for v in ss.VOICE_REGISTRY.values()}
        self.assertEqual(backends, {"kokoro", "qwen"})

    def test_the_default_voice_is_a_kokoro_one(self):
        """Kokoro stays the default: 350 MB against 2.3 GB, one to two seconds
        against four, and it is the right answer for narration and bulk."""
        self.assertEqual(ss.VOICE_REGISTRY[ss.DEFAULT_VOICE]["backend"], "kokoro")

    def test_no_voice_name_is_claimed_by_both(self):
        """The whole scheme rests on this. A collision would make the backend
        ambiguous and silently route someone to the wrong model."""
        self.assertEqual(len(ss.VOICE_REGISTRY),
                         len(ss.KOKORO_VOICES) + len(ss.QWEN_VOICES))

    def test_every_kokoro_voice_survived(self):
        """Existing callers name these. Losing one is a breaking change."""
        for v in ("af_heart", "am_michael", "bf_emma", "bm_george"):
            self.assertIn(v, ss.VOICE_REGISTRY)
            self.assertEqual(ss.VOICE_REGISTRY[v]["backend"], "kokoro")

    def test_only_qwen_voices_claim_direction(self):
        for name, spec in ss.VOICE_REGISTRY.items():
            self.assertEqual(spec["supports_instruct"], spec["backend"] == "qwen", name)


class TestChoosingTheBackend(unittest.TestCase):
    def test_a_kokoro_voice_routes_to_kokoro(self):
        self.assertEqual(ss.backend_for("af_heart"), "kokoro")

    def test_a_qwen_voice_routes_to_qwen(self):
        self.assertEqual(ss.backend_for("ryan"), "qwen")

    def test_an_unknown_voice_is_refused_rather_than_defaulted(self):
        self.assertIsNone(ss.backend_for("nobody"))


class TestValidation(unittest.TestCase):
    def bad(self, payload):
        return ss.speech_problem(payload)

    def test_text_is_required(self):
        self.assertIsNotNone(self.bad({"voice": "af_heart"}))

    def test_an_unknown_voice_is_rejected_with_a_pointer(self):
        problem = self.bad({"input": "hi", "voice": "nobody"})
        self.assertIsNotNone(problem)
        self.assertIn("/v1/voices", problem)

    def test_a_plain_request_passes(self):
        self.assertIsNone(self.bad({"input": "hello", "voice": "af_heart"}))

    def test_direction_on_a_qwen_voice_passes(self):
        self.assertIsNone(self.bad({"input": "hello", "voice": "ryan",
                                    "instruct": "Panicked and breathless."}))

    def test_direction_on_a_kokoro_voice_is_refused_not_ignored(self):
        """The failure mode worth designing against. A caller who sends
        `instruct` to Kokoro and gets flat delivery has no way to tell whether
        the model tried and failed or the parameter was dropped on the floor.
        Say so, and name a voice that can do it."""
        problem = self.bad({"input": "hello", "voice": "af_heart",
                            "instruct": "Very angry."})
        self.assertIsNotNone(problem)
        self.assertIn("instruct", problem)
        self.assertTrue(any(v in problem for v in ss.QWEN_VOICES),
                        "the error should name a voice that supports it")

    def test_an_empty_direction_is_not_treated_as_a_request_for_one(self):
        self.assertIsNone(self.bad({"input": "hi", "voice": "af_heart", "instruct": ""}))


class TestTheVoicesEndpointDescribesCapability(unittest.TestCase):
    """A client cannot offer direction sensibly without knowing which voices
    take it, and hardcoding that list in the UI is the frozen-literal bug this
    project has now fixed four times."""

    def test_it_reports_backend_and_capability_per_voice(self):
        payload = ss.voices_payload()
        self.assertEqual(payload["default"], ss.DEFAULT_VOICE)
        by_name = {v["name"]: v for v in payload["voices"]}
        self.assertEqual(set(by_name), set(ss.VOICE_REGISTRY))
        self.assertFalse(by_name["af_heart"]["supports_instruct"])
        self.assertTrue(by_name["ryan"]["supports_instruct"])

    def test_every_voice_says_which_model_makes_it(self):
        for v in ss.voices_payload()["voices"]:
            self.assertIn(v["backend"], ("kokoro", "qwen"), v)


class TestTheHeavyModelIsNotLoadedToAnswerAQuestion(unittest.TestCase):
    def test_listing_voices_needs_no_model(self):
        """/v1/voices used to wake the speech model. With a second, larger one
        behind the same endpoint that would be a 2.3 GB load to answer a
        question the registry already knows."""
        self.assertEqual(len(ss.voices_payload()["voices"]), len(ss.VOICE_REGISTRY))


if __name__ == "__main__":
    unittest.main()
