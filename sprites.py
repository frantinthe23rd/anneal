#!/usr/bin/env python3
"""Cut a generated sprite sheet into frames.

The reason sheets are generated whole rather than a frame at a time is
identity: four separate generations of "the same character" produce four
different characters — measured, twice, once via img2img and once via plain
prompting. One generation containing four poses cannot diverge, because the
frames were never separate.

What that leaves is a picture, not a grid. The model places poses at different
sizes with irregular spacing, so cutting on fixed cells slices characters in
half. Frames are found by content instead.

Two things about generated sheets specifically drive the implementation:

  * The background is never pure white. It is off-white with compression noise,
    so background detection is a tolerance rather than an equality.
  * Every sprite sits on a soft drop shadow. Left alone, each shadow is a large
    light-grey blob that gets cut as its own frame, and its bounding box merges
    with the character's. It is excluded by requiring a pixel to be some way
    clear of the background before it counts as content.

    ./sprites.py sheet.png --out frames/
"""
import argparse
import json
import os
import sys

# How far from the background colour a pixel must be to count as content. This
# is a sum across all three channels, so a #eeeeee drop shadow on white scores
# 3x17 = 51 — the threshold has to clear that, which 46 did not.
CONTENT_DISTANCE = 66
# Anything smaller is compression noise or an antialiasing crumb.
MIN_FRAME_PX = 12
# Frames on the same row rarely align exactly; this is how much vertical
# disagreement still counts as "the same row" for reading order.
ROW_TOLERANCE = 0.5
# Width of the alpha ramp, in the same summed-channel units. Wide enough to
# keep the model's antialiasing as partial transparency rather than throwing it
# away, which is what makes a cut-out look jagged.
SOFT_EDGE = 40

_REMBG_SESSION = None


def _load(path):
    from PIL import Image
    return Image.open(path).convert("RGB")


def _background(img):
    """The sheet's background colour, taken from its corners.

    Corners rather than the most common colour: a large flat character on a
    small sheet can out-vote the background, and every generated sheet so far
    has had all four corners empty.
    """
    w, h = img.size
    corners = [img.getpixel((0, 0)), img.getpixel((w - 1, 0)),
               img.getpixel((0, h - 1)), img.getpixel((w - 1, h - 1))]
    return tuple(sum(c[i] for c in corners) // len(corners) for i in range(3))


def _content_mask(img, bg, distance=CONTENT_DISTANCE):
    w, h = img.size
    px = img.load()
    mask = bytearray(w * h)
    for y in range(h):
        row = y * w
        for x in range(w):
            r, g, b = px[x, y]
            if abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) > distance:
                mask[row + x] = 1
    return mask, w, h


def _components(mask, w, h, min_px=MIN_FRAME_PX):
    """Connected regions of content, as bounding boxes.

    Iterative flood fill: a recursive one blows the stack on a sprite of any
    size, which is every sprite.
    """
    seen = bytearray(w * h)
    boxes = []
    for start in range(w * h):
        if not mask[start] or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        minx = maxx = start % w
        miny = maxy = start // w
        count = 0
        while stack:
            i = stack.pop()
            count += 1
            x, y = i % w, i // w
            if x < minx: minx = x
            if x > maxx: maxx = x
            if y < miny: miny = y
            if y > maxy: maxy = y
            if x > 0 and mask[i - 1] and not seen[i - 1]:
                seen[i - 1] = 1; stack.append(i - 1)
            if x < w - 1 and mask[i + 1] and not seen[i + 1]:
                seen[i + 1] = 1; stack.append(i + 1)
            if y > 0 and mask[i - w] and not seen[i - w]:
                seen[i - w] = 1; stack.append(i - w)
            if y < h - 1 and mask[i + w] and not seen[i + w]:
                seen[i + w] = 1; stack.append(i + w)
        if count >= min_px:
            boxes.append({"x": minx, "y": miny,
                          "width": maxx - minx + 1, "height": maxy - miny + 1,
                          "pixels": count})
    return boxes


def _merge_parts(boxes):
    """One sprite, one frame — even when it is several disconnected regions.

    Raising the content threshold high enough to drop drop-shadows also drops
    the pale interior of a pale sprite, leaving its outline, its eyes and its
    details as separate components. They are still obviously one character:
    their boxes overlap. Merge anything overlapping, repeatedly, until nothing
    does.
    """
    boxes = list(boxes)
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if (a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"] and
                        a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"]):
                    x0 = min(a["x"], b["x"]); y0 = min(a["y"], b["y"])
                    x1 = max(a["x"] + a["width"], b["x"] + b["width"])
                    y1 = max(a["y"] + a["height"], b["y"] + b["height"])
                    boxes[i] = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0,
                                "pixels": a["pixels"] + b["pixels"]}
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return boxes


def _reading_order(boxes):
    """Left to right, then top to bottom — how a sheet is read.

    Rows are grouped by vertical overlap rather than by exact y, because the
    model does not align them.
    """
    if not boxes:
        return []
    rows = []
    for box in sorted(boxes, key=lambda b: b["y"]):
        placed = False
        for row in rows:
            ref = row[0]
            if box["y"] < ref["y"] + ref["height"] * ROW_TOLERANCE:
                row.append(box)
                placed = True
                break
        if not placed:
            rows.append([box])
    out = []
    for row in rows:
        out.extend(sorted(row, key=lambda b: b["x"]))
    return out


def find_frames(path, distance=CONTENT_DISTANCE, min_px=MIN_FRAME_PX):
    """Bounding boxes of each sprite on the sheet, in reading order."""
    img = _load(path)
    bg = _background(img)
    mask, w, h = _content_mask(img, bg, distance)
    return _reading_order(_merge_parts(_components(mask, w, h, min_px)))


def matte(img, bg, soft=SOFT_EDGE, distance=CONTENT_DISTANCE):
    """Alpha from colour distance. Kept, but not the default — see `cut_alpha`.

    This was the first attempt and the reasoning behind it was wrong. It argued
    that a segmentation model is the wrong tool because a generated sprite sits
    on a flat field the model itself painted, so alpha is implied by colour and
    can be computed exactly rather than predicted.

    That holds only while the sprite differs from its background. It does not
    for a *white* robot on white, which is an ordinary thing to want: the body
    came out see-through, with the backdrop readable straight through its head.
    Not a halo — a hole.

    Generating on magenta to force contrast made it worse rather than better.
    The model painted a gradient rather than a flat field, so thresholding cut a
    slab of background as its own frame; it drew eight poses and a stray object
    instead of four; and magenta bled into the shadows. Eleven frames where four
    were wanted.

    It is still the right tool when the subject genuinely contrasts and no model
    is available, so it stays behind `--no-model`.
    """
    from PIL import Image
    out = Image.new("RGBA", img.size)
    src, dst = img.load(), out.load()
    w, h = img.size
    floor = max(1, distance - soft)
    for y in range(h):
        for x in range(w):
            r, g, b = src[x, y]
            d = abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2])
            if d <= floor:
                continue                     # background: fully transparent
            if d >= distance:
                dst[x, y] = (r, g, b, 255)
            else:
                dst[x, y] = (r, g, b, int(255 * (d - floor) / (distance - floor)))
    return out


def cut_alpha(img):
    """Alpha by semantic segmentation, which is what actually works here.

    rembg does not care what colour the subject is, which is the whole point: it
    cut the white robot cleanly off white where colour distance made it
    transparent. Roughly 0.3s a frame after a one-off session start, against a
    177 MB model held outside the repo.

    Imported lazily so that finding and cutting frames — the parts that need
    nothing but Pillow — keep working wherever rembg is not installed.
    """
    from rembg import remove
    global _REMBG_SESSION
    if _REMBG_SESSION is None:
        from rembg import new_session
        _REMBG_SESSION = new_session(os.environ.get("ANNEAL_MATTE_MODEL", "u2net"))
    return remove(img.convert("RGBA"), session=_REMBG_SESSION)


def atlas(path, **kw):
    """Frames plus what a game engine needs to place them."""
    img = _load(path)
    frames = find_frames(path, **kw)
    return {
        "source": os.path.basename(path),
        "source_size": [img.size[0], img.size[1]],
        "background": list(_background(img)),
        "frames": [{"index": i, "x": f["x"], "y": f["y"],
                    "width": f["width"], "height": f["height"]}
                   for i, f in enumerate(frames)],
    }


# Below this fraction of visible pixels a matted frame is empty rather than
# sparse. Measured: a real run produced a 107x10 strip at 0.00% alongside two
# good sprites at 44% and 59%, so there is a wide gap to sit in.
MIN_VISIBLE = 0.01


def is_blank(img):
    """Did matting remove everything?

    Finding a region and matting it are separate passes and each did its job:
    the content pass saw a faint smear on the sheet, and the segmentation model
    correctly decided none of it was subject. Nothing was checking the result,
    so a fully transparent PNG went into the library and rendered as an empty
    cell in the UI. A frame that mattes to nothing is not a frame.
    """
    if img.mode != "RGBA":
        return False                     # opaque output has nothing to judge
    alpha = img.getchannel("A")
    total = img.width * img.height
    if not total:
        return True
    # Count from the histogram rather than per pixel: this runs on every frame
    # of every sheet and the pixel loop showed up.
    hist = alpha.histogram()
    visible = sum(hist[41:])             # the same threshold matte() ramps to
    return visible / float(total) < MIN_VISIBLE


def _cut(path, out_dir, transparent=True, use_model=True, **kw):
    """Write the frames, and say which source frame each file came from.

    Callers need the pairing, not just the paths: a frame dropped for being
    blank must be dropped from the atlas too, and zipping two lists that no
    longer correspond would give every later frame the wrong box. Numbering
    counts what was *written*, so the files stay contiguous.
    """
    img = _load(path)
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    kept = []
    bg = _background(img)
    for f in find_frames(path, **kw):
        crop = img.crop((f["x"], f["y"], f["x"] + f["width"], f["y"] + f["height"]))
        if transparent:
            if use_model:
                crop = cut_alpha(crop)
            else:
                crop = matte(crop, bg, distance=kw.get("distance", CONTENT_DISTANCE))
            if is_blank(crop):
                continue
        target = os.path.join(out_dir, "%s-%02d.png" % (stem, len(kept)))
        crop.save(target)
        kept.append((f, target))
    return kept


def cut(path, out_dir, transparent=True, use_model=True, **kw):
    """Write each frame as its own PNG. Returns the paths written."""
    return [target for _f, target in
            _cut(path, out_dir, transparent, use_model, **kw)]


# A frame sequence is illegible as a list of PNGs. The one thing that makes it
# readable is seeing it move, so every set gets a preview it can be judged by.
DEFAULT_FPS = 8


def animate(frame_paths, out_path, fps=DEFAULT_FPS):
    """Write an animated, transparent GIF of the frames. Returns the path.

    Frames are *padded* onto a common canvas rather than resized. Cut frames are
    never the same size — the model spaces poses irregularly and at different
    scales — and scaling each to fit would make the character pulse between
    frames, which reads as a fault in the sprite rather than in the preview.
    Each frame is centred horizontally and sat on the bottom, so the character
    stands on a consistent floor instead of bobbing.
    """
    if not frame_paths:
        return None
    from PIL import Image

    frames = [Image.open(p).convert("RGBA") for p in frame_paths]
    width = max(f.width for f in frames)
    height = max(f.height for f in frames)

    canvas = []
    for f in frames:
        sheet = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        sheet.paste(f, ((width - f.width) // 2, height - f.height), f)
        # Quantise with one palette slot reserved for full transparency: a GIF
        # has no alpha channel, only a transparent colour index.
        flat = sheet.convert("RGB").quantize(colors=255, method=Image.MEDIANCUT)
        mask = sheet.getchannel("A").point(lambda a: 255 if a <= 128 else 0)
        flat.paste(255, mask)
        canvas.append(flat)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    canvas[0].save(out_path, save_all=True, append_images=canvas[1:],
                   duration=max(int(1000.0 / max(fps, 1)), 20), loop=0,
                   transparency=255, disposal=2, optimize=False)
    for f in frames:
        f.close()
    return out_path


def pipeline(sheet_path, out_dir, use_model=True, distance=CONTENT_DISTANCE, fps=DEFAULT_FPS):
    """Cut, matte and describe a sheet in one call. Returns the atlas.

    This is what the gateway invokes as a subprocess: it prints the atlas as
    JSON on stdout so nothing has to be imported across environments.
    """
    data = atlas(sheet_path, distance=distance)
    kept = _cut(sheet_path, out_dir, transparent=True, use_model=use_model,
                distance=distance)
    # Rebuild rather than zip: blank frames are gone from `kept` and must be
    # gone from the atlas too, with the surviving indices renumbered to match
    # the files on disk. Zipping the original list against a shorter one gives
    # every frame after a dropped one somebody else's box.
    frames = []
    for i, (found, path) in enumerate(kept):
        frame = dict(found)
        frame["index"] = i
        frame["file"] = path
        frames.append(frame)
    data["frames"] = frames
    data["frame_dir"] = out_dir
    preview = animate([f["file"] for f in frames],
                      os.path.join(out_dir, "preview.gif"), fps=fps)
    if preview:
        data["preview"] = preview
        data["fps"] = fps
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sheet")
    ap.add_argument("--out", help="directory for the cut frames; omit to only report")
    ap.add_argument("--opaque", action="store_true",
                    help="keep the background instead of cutting alpha")
    ap.add_argument("--json", action="store_true",
                    help="print the atlas as JSON on stdout and nothing else")
    ap.add_argument("--no-model", action="store_true",
                    help="matte by colour distance instead of segmentation. Faster and "
                         "dependency-free, but it makes a pale sprite on a pale "
                         "background transparent — measured, on a white robot")
    ap.add_argument("--fps", type=int, default=DEFAULT_FPS,
                    help="frame rate of the preview GIF")
    ap.add_argument("--distance", type=int, default=CONTENT_DISTANCE,
                    help="how far from the background a pixel must be to be content")
    args = ap.parse_args(argv)

    if args.json:
        if not args.out:
            print(json.dumps({"error": "--json needs --out"}))
            return 2
        try:
            print(json.dumps(pipeline(args.sheet, args.out,
                                      use_model=not args.no_model,
                                      distance=args.distance, fps=args.fps)))
            return 0
        except Exception as exc:
            print(json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)}))
            return 1

    data = atlas(args.sheet, distance=args.distance)
    print("  %s  %dx%d  background rgb%s" % (data["source"], data["source_size"][0],
                                             data["source_size"][1], tuple(data["background"])))
    for f in data["frames"]:
        print("    frame %d  %4dx%-4d at (%4d, %4d)" % (f["index"], f["width"], f["height"],
                                                        f["x"], f["y"]))
    if not data["frames"]:
        print("    no frames found — try a lower --distance")
        return 1
    if args.out:
        written = cut(args.sheet, args.out, transparent=not args.opaque,
                      use_model=not args.no_model, distance=args.distance)
        with open(os.path.join(args.out, "atlas.json"), "w") as fh:
            json.dump(data, fh, indent=2)
        print("  wrote %d frame(s) and atlas.json to %s" % (len(written), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
