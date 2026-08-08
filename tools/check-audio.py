#!/usr/bin/env python3
"""Is this file music, or is it noise?

Not a quality judgement — nothing here can tell a good take from a mediocre
one, and CLAUDE.md is emphatic that a metric moving the right way is not
evidence of improvement. This answers the narrower question that has actually
gone wrong twice: whether the model produced structured audio at all, or a
uniform wash that only sounds like a fault when you play it.

Two measures, both from the project's own notes on what discriminates:

  near-silent fraction   Music breathes. A garbled take had *zero* quiet
                         frames against 22.8% for a good one. This is the
                         sharpest single signal.
  spectral flatness      Noise measures several times higher. Flat spectra are
                         what a broken DiT produces.

Exit 0 if the file looks like music, 1 if it looks like noise, 2 if it could
not be read. Thresholds are deliberately loose: this is a catastrophe detector,
and a false alarm on a legitimately dense track would make it worthless.

    tools/check-audio.py take.flac
"""
import argparse
import math
import struct
import subprocess
import sys
import wave

# Calibrated against real output rather than picked. Four generated takes from
# this machine measured 8.0-18.7% quiet frames and 0.075-0.123 flatness; white
# and pink noise measured 0.0% quiet and 0.714-0.833 flatness. The thresholds
# below sit roughly 2.4x clear of both, which is the margin that makes this a
# catastrophe detector rather than a tripwire.
#
# An earlier version decimated the DFT by four and skipped the window. It
# reported real music at 0.395 and failed it. If you change the transform,
# recalibrate against actual takes before trusting the number.

# Below this fraction of full scale a frame counts as quiet.
SILENCE_FLOOR = 0.02
# A real take has some. Zero means it never stopped, which noise never does.
MIN_QUIET_FRACTION = 0.005
# Flatness above this is a wash rather than a signal. Music sits far below.
MAX_FLATNESS = 0.30


def decode(path):
    """PCM samples, mono, via ffmpeg — which the project already depends on."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", "16000",
         "-f", "wav", "-"],
        capture_output=True, check=True).stdout
    import io
    with wave.open(io.BytesIO(out)) as w:
        frames = w.readframes(w.getnframes())
        width = w.getsampwidth()
    if width != 2:
        raise ValueError("expected 16-bit PCM from ffmpeg, got %d bytes" % width)
    count = len(frames) // 2
    return struct.unpack("<%dh" % count, frames[:count * 2])


def quiet_fraction(samples, frame=1600):
    """Share of 100 ms frames whose peak sits under the silence floor."""
    if not samples:
        return 0.0
    quiet = total = 0
    for i in range(0, len(samples) - frame, frame):
        peak = max(abs(s) for s in samples[i:i + frame]) / 32768.0
        total += 1
        if peak < SILENCE_FLOOR:
            quiet += 1
    return quiet / total if total else 0.0


def flatness(samples, size=512):
    """Geometric over arithmetic mean of the spectrum, averaged across frames.

    A naive DFT is slow, so this samples a handful of frames rather than the
    whole file — enough to tell a wash from music, which is all it claims.
    """
    frames = []
    step = max(1, (len(samples) - size) // 12)
    for start in range(0, max(1, len(samples) - size), step):
        block = samples[start:start + size]
        if len(block) < size:
            break
        # A Hann window first: without it the rectangular edges leak energy
        # across the whole spectrum, which flattens it and made real music
        # measure as noise. Every sample is used — decimating in time aliased
        # high frequencies down and inflated the result further.
        win = [0.5 - 0.5 * math.cos(2 * math.pi * n / (size - 1)) for n in range(size)]
        mags = []
        for k in range(1, size // 2):
            re = im = 0.0
            for n in range(size):
                angle = -2 * math.pi * k * n / size
                re += block[n] * win[n] * math.cos(angle)
                im += block[n] * win[n] * math.sin(angle)
            mags.append(math.hypot(re, im) + 1e-9)
        geo = math.exp(sum(math.log(m) for m in mags) / len(mags))
        arith = sum(mags) / len(mags)
        frames.append(geo / arith)
        if len(frames) >= 6:
            break
    return sum(frames) / len(frames) if frames else 1.0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--quiet-min", type=float, default=MIN_QUIET_FRACTION)
    ap.add_argument("--flatness-max", type=float, default=MAX_FLATNESS)
    args = ap.parse_args(argv)

    try:
        samples = decode(args.path)
    except Exception as exc:
        print("could not read %s: %s" % (args.path, exc), file=sys.stderr)
        return 2
    if not samples:
        print("no audio in %s" % args.path, file=sys.stderr)
        return 2

    q = quiet_fraction(samples)
    f = flatness(samples)
    verdict = []
    if q < args.quiet_min:
        verdict.append("never goes quiet (%.1f%% of frames, expected > %.1f%%)"
                       % (q * 100, args.quiet_min * 100))
    if f > args.flatness_max:
        verdict.append("spectrally flat (%.3f, expected < %.3f)" % (f, args.flatness_max))

    print("  near-silent frames %.1f%%   flatness %.3f   %s"
          % (q * 100, f, "looks like music" if not verdict else "LOOKS LIKE NOISE"))
    for line in verdict:
        print("    " + line)
    print("  A spectrogram is still the real check: "
          "ffmpeg -i %s -lavfi showspectrumpic=s=900x360 out.png" % args.path)
    return 1 if verdict else 0


if __name__ == "__main__":
    sys.exit(main())
