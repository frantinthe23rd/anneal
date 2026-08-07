"""Video generation as a pluggable service (#20).

#20 was filed as *not currently possible* with the criteria for reopening it
written down: a 4-bit MLX port of a competent model with a peak footprint under
~10 GB. Those criteria are now met by Wan 2.1 T2V-1.3B through mlx-video, so the
issue gets a service rather than another comment.

The design constraint that shapes everything here is that the model must be
swappable. Wan 1.3B is what fits *this* machine; Wan 14B or LTX-2 fit a 32 GB
one, and the licence question — which is what made MiniMax H3 unattractive —
becomes a per-model property rather than a decision baked into the code. So the
backend is a table, and these tests are mostly about the table staying honest.

They exercise argv construction and validation, which is pure. Nothing here
loads a model or needs mlx-video installed; that is what the acceptance suite
and a live run are for.
"""
import os
import unittest

from tests.context import REPO_ROOT  # noqa: F401

import video_server


class TestBackendTable(unittest.TestCase):
    """Two families with genuinely different command lines is the whole reason
    this is a table. Wan takes a pre-converted `--model-dir`; LTX-2 takes a
    `--model-repo` it resolves itself. A single flag mapping cannot cover both,
    and pretending otherwise is how a third model becomes a rewrite."""

    def test_the_default_backend_exists_in_the_table(self):
        self.assertIn(video_server.DEFAULT_BACKEND, video_server.BACKENDS)

    def test_every_backend_declares_what_it_needs(self):
        for name, spec in video_server.BACKENDS.items():
            self.assertTrue(spec.get("module"), name)
            self.assertTrue(callable(spec.get("argv")), name)
            self.assertIn("licence", spec, "%s must state its licence" % name)

    def test_the_default_is_the_permissively_licensed_one(self):
        """The point of the research that reopened this: H3 is use-restricted
        and Wan 2.1 is Apache-2.0. A restricted model may be plugged in, but it
        has to be a deliberate act, not the out-of-the-box behaviour."""
        self.assertIn("apache", video_server.BACKENDS[
            video_server.DEFAULT_BACKEND]["licence"].lower())


class TestArgv(unittest.TestCase):
    def argv(self, backend="wan", **kw):
        req = {"prompt": "a lighthouse in a storm"}
        req.update(kw)
        return video_server.build_argv(backend, req, "/tmp/out.mp4")

    def test_the_prompt_and_output_reach_the_command(self):
        argv = self.argv()
        self.assertIn("a lighthouse in a storm", argv)
        self.assertIn("/tmp/out.mp4", argv)

    def test_wan_is_pointed_at_a_converted_directory(self):
        self.assertIn("--model-dir", self.argv("wan"))

    def test_ltx_resolves_its_own_repo_instead(self):
        argv = self.argv("ltx")
        self.assertIn("--model-repo", argv)
        self.assertNotIn("--model-dir", argv)

    def test_an_unknown_backend_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(ValueError):
            video_server.build_argv("nope", {"prompt": "x"}, "/tmp/o.mp4")

    def test_nothing_is_run_through_a_shell(self):
        """argv is a list precisely so a prompt cannot become a command."""
        argv = self.argv(prompt="a cat; rm -rf /")
        self.assertIsInstance(argv, list)
        self.assertIn("a cat; rm -rf /", argv)


class TestFrameCount(unittest.TestCase):
    """Wan's own CLI says num-frames must be 4n+1. Passing 16 fails several
    minutes in, after the weights have loaded — so it is snapped up front."""

    def test_a_valid_count_is_left_alone(self):
        for n in (5, 9, 17, 33):
            self.assertEqual(video_server.snap_frames(n), n)

    def test_an_invalid_count_is_snapped_up_to_the_next_valid_one(self):
        self.assertEqual(video_server.snap_frames(16), 17)
        self.assertEqual(video_server.snap_frames(6), 9)

    def test_it_never_snaps_below_the_minimum(self):
        self.assertEqual(video_server.snap_frames(1), 5)
        self.assertEqual(video_server.snap_frames(0), 5)

    def test_the_snapped_count_is_what_reaches_the_command(self):
        argv = video_server.build_argv("wan", {"prompt": "x", "frames": 16}, "/tmp/o.mp4")
        self.assertIn("17", argv)
        self.assertNotIn("16", argv)


class TestValidation(unittest.TestCase):
    def test_a_prompt_is_required(self):
        self.assertIsNotNone(video_server.limits({}))

    def test_a_reasonable_request_passes(self):
        self.assertIsNone(video_server.limits({"prompt": "a lighthouse"}))

    def test_length_is_capped(self):
        """Nine frames took about ten minutes on the reference hardware. An
        unbounded request is an hours-long one that looks like a hang."""
        self.assertIsNotNone(video_server.limits(
            {"prompt": "x", "frames": video_server.MAX_FRAMES + 4}))

    def test_resolution_is_capped(self):
        self.assertIsNotNone(video_server.limits({"prompt": "x", "width": 4096, "height": 4096}))


class TestServiceRegistration(unittest.TestCase):
    def test_video_is_a_service_with_its_own_routes(self):
        import services
        self.assertIn("video", services.SERVICES)
        self.assertEqual(services.resolve("/v1/videos/generations"), "video")

    def test_it_is_heavy(self):
        """It is the largest thing here after music. Letting it coexist with
        another heavy model is how the machine falls over."""
        import services
        self.assertTrue(services.SERVICES["video"]["heavy"])

    def test_it_does_not_run_in_the_pinned_model_environment(self):
        """gen-venv serves music, speech and images and is version-pinned.
        mlx-video drags librosa and numba behind it; the two must not share."""
        import services
        cmd = " ".join(services.SERVICES["video"]["cmd"])
        self.assertNotIn("gen-venv", cmd)
        self.assertIn("video-venv", cmd)

    def test_video_is_a_library_kind(self):
        import outputs
        self.assertIn("video", outputs.KINDS)

    def test_a_cold_start_estimate_exists(self):
        """The UI warns before a slow request, and this is the slowest thing
        here by a wide margin — silence would read as a hang."""
        import services
        self.assertIn("video", services.COLD_START_SECONDS)


class TestModelIsNotAssumedPresent(unittest.TestCase):
    """The weights are a separate ~17 GB download and a conversion step. A host
    without them should say so, not fail somewhere inside MLX."""

    def test_a_missing_model_directory_is_reported_as_such(self):
        problem = video_server.model_problem("wan", "/definitely/not/here")
        self.assertIsNotNone(problem)
        self.assertIn("convert", problem.lower())

    def test_a_present_directory_passes(self):
        self.assertIsNone(video_server.model_problem("wan", os.path.dirname(__file__)))

    def test_ltx_needs_no_local_directory(self):
        self.assertIsNone(video_server.model_problem("ltx", "/definitely/not/here"))


if __name__ == "__main__":
    unittest.main()
