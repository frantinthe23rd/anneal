#!/usr/bin/env bash
# Deliberately move Anneal to newer models or dependencies, and prove it still
# works before declaring success.
#
# Nothing here runs automatically. Updates are a choice, because the failure
# mode of a silent one is discovering at 2am that a model you depend on changed
# under you.
#
#   ./update.sh --check     what would change (default, read-only)
#   ./update.sh --deps      rebuild gen-venv from gen-venv.lock.txt
#   ./update.sh --smoke     just run the smoke test against what's installed
#   ./update.sh --smoke-deep  also generate on the patched high tier and
#                             check the result is music, not noise
#
#   ./update.sh --models              required weights for every service
#   ./update.sh --models all          everything, including the optional ones
#   ./update.sh --models music,speech only those services
#   ./update.sh --models list         print the plan and the sizes, fetch nothing
#
# Naming a service takes *all* of its models, optional ones included: asking for
# speech and getting only half of the voices would be the more surprising rule.
# `list` accepts the same argument, so any of these can be priced first.
#
# Selection exists because several models are large and genuinely optional --
# the high music tier, directed speech, the Kontext sprite path -- and roughly
# 40 GB of weights were deleted from this machine after they turned out not to
# earn their disk. Someone trying Anneal should not have to fetch those to find
# out. Sizes are printed before anything is downloaded, for the same reason.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK="$HERE/models.lock.json"

export HF_HUB_DISABLE_XET=1 HF_HUB_ENABLE_HF_TRANSFER=0
# Downloads are the one place that legitimately needs the network.
export HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0

MODE="${1:---check}"

# Everything here runs under gen-venv, the only environment with
# huggingface_hub in it. --models therefore depends on --deps having run, and
# that ordering used to fail as "no such file or directory" naming a path
# nobody recognised. Say what is actually wrong instead.
GEN_PYTHON="$AIMUSIC_ROOT/gen-venv/bin/python"
py() {
    if [[ ! -x "$GEN_PYTHON" ]]; then
        echo "ERROR: $GEN_PYTHON does not exist." >&2
        echo "  The model environment has not been built yet. Run:" >&2
        echo "    ./update.sh --deps      (or ./setup.sh, which does both in order)" >&2
        exit 1
    fi
    "$GEN_PYTHON" "$@"
}

# --------------------------------------------------------------- check
check() {
    echo "Pinned vs current upstream:"
    py - "$LOCK" <<'PY'
import json, sys
from huggingface_hub import HfApi
lock = json.load(open(sys.argv[1]))
api = HfApi()
drift = 0
for repo, spec in lock["models"].items():
    pinned = spec["revision"]
    try:
        current = api.model_info(repo).sha
    except Exception as exc:
        print("  %-45s unreachable (%s)" % (repo, type(exc).__name__)); continue
    if current == pinned:
        print("  %-45s up to date" % repo)
    else:
        drift += 1
        print("  %-45s DRIFTED\n      pinned  %s\n      current %s" % (repo, pinned, current))
print()
print("%d model(s) have moved upstream." % drift)
if drift:
    print("Nothing was changed. To take them, edit models.lock.json then run:")
    print("  ./update.sh --models && ./update.sh --smoke")
PY

    echo
    echo "Upstream ACE-Step checkout:"
    local pinned current
    pinned="$(py -c "import json;print(json.load(open('$LOCK'))['upstream']['ACE-Step/ACE-Step-1.5']['commit'])")"
    current="$(git -C "$ACESTEP_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
    if [[ "$pinned" == "$current" ]]; then
        echo "  at pinned commit ${pinned:0:12}"
    else
        echo "  DRIFTED — pinned ${pinned:0:12}, checkout ${current:0:12}"
    fi
    echo
    echo "Reminder: checkpoints/ must stay a symlink to \$ACESTEP_CHECKPOINTS_DIR."
    if [[ -L "$ACESTEP_DIR/checkpoints" ]]; then echo "  symlink intact"; else echo "  MISSING — start-api.sh will recreate it"; fi
}

# --------------------------------------------------------------- deps
deps() {
    echo "Rebuilding gen-venv from gen-venv.lock.txt ..."
    "$UV_BIN" venv --python 3.12 "$AIMUSIC_ROOT/gen-venv"
    VIRTUAL_ENV="$AIMUSIC_ROOT/gen-venv" "$UV_BIN" pip install -r "$HERE/gen-venv.lock.txt"
    echo "Done."
}

# --------------------------------------------------------------- models
# `selection` is one of: empty/"required", "all", "list", or a comma-separated
# list of service names taken from models.lock.json. An unknown name is an
# error that lists the real ones, rather than a run that downloads nothing and
# reports success.
models() {
    local selection="${1:-required}"
    local dry=0
    # `list` is a leading token, so a selection can be previewed as well as
    # applied: `--models list` shows everything, `--models list music` shows
    # what asking for music would fetch.
    if [[ "$selection" == "list" ]]; then dry=1; selection="${2:-all}"; fi

    py - "$LOCK" "$ACESTEP_CHECKPOINTS_DIR" "$selection" "$dry" <<'PYFETCH'
import json, os, sys

lock_path, ckpt, selection = sys.argv[1], sys.argv[2], sys.argv[3]
dry = sys.argv[4] == "1"
lock = json.load(open(lock_path))
models = lock["models"]

# Services come from the lockfile, never from a list written out here. A copied
# list is how the forge strip silently omitted video and how three tests went
# stale after the change they were meant to catch had already shipped.
services = []
for spec in models.values():
    if spec.get("service") and spec["service"] not in services:
        services.append(spec["service"])

only_required = False
if selection in ("required", "all"):
    wanted = list(services)
    only_required = selection == "required"
else:
    wanted = [s.strip() for s in selection.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in services]
    if unknown:
        sys.exit("unknown service(s): %s\nknown: %s"
                 % (", ".join(unknown), ", ".join(services)))


def local_dir(spec):
    target = spec["target"]
    if target == "checkpoints_dir":
        return ckpt
    if target.startswith("checkpoints_dir/"):
        return os.path.join(ckpt, target.split("/", 1)[1])
    return None            # hf_cache: the hub cache decides the layout, not us


def size(spec):
    try:
        return float(spec.get("size_gb") or 0)
    except (TypeError, ValueError):
        return 0.0


chosen, skipped = [], []
for repo, spec in models.items():
    take = spec.get("service") in wanted and (spec.get("required", True) or not only_required)
    (chosen if take else skipped).append((repo, spec))

# Size before bytes. Someone deciding whether to do this at all needs the
# number first, not after 9 GB has already moved.
print("Plan - %d model(s), about %.1f GB:" % (len(chosen), sum(size(s) for _, s in chosen)))
for repo, spec in chosen:
    print("  %-52s %5.1f GB  %-8s %s"
          % (repo, size(spec), spec.get("service", "?"),
             "" if spec.get("required", True) else "optional"))
    if "Non-Commercial" in (spec.get("licence") or ""):
        print("      licence: %s" % spec["licence"])
if skipped:
    print()
    print("Not fetching (%.1f GB): %s"
          % (sum(size(s) for _, s in skipped), ", ".join(r for r, _ in skipped)))
    print("  ./update.sh --models all         everything")
    print("  ./update.sh --models <service>   one of: %s" % ", ".join(services))
print()

if dry:
    raise SystemExit(0)

from huggingface_hub import snapshot_download

for repo, spec in chosen:
    kwargs = {"repo_id": repo, "revision": spec["revision"], "max_workers": 4}
    dest = local_dir(spec)
    if dest:
        kwargs["local_dir"] = dest
    print("  %s @ %s" % (repo, spec["revision"][:12]), flush=True)
    # Resumable by construction: snapshot_download resumes from partial blobs
    # in the cache, so an interrupted fetch is re-run, not restarted.
    snapshot_download(**kwargs)
print("fetched")
PYFETCH
    (( dry )) && return 0
    # Under gen-venv, not the system Python: safetensors lives there, and
    # without it only half the check runs — the sparseness half — while the
    # output still reads like a pass. The header check is the one that catches
    # the failure this exists for.
    py "$HERE/verify-models.py"
}

# --------------------------------------------------------------- smoke
smoke() {
    echo "Smoke test — exercising all three services through the gateway."
    echo "Music is the slow one; allow ~10 minutes from cold."
    bash "$HERE/stop-api.sh" >/dev/null 2>&1 || true
    bash "$HERE/start-api.sh" >/dev/null
    local fails=0

    echo -n "  speech ... "
    if curl -fsS -m 300 -X POST "http://127.0.0.1:$SUPERVISOR_PORT/v1/audio/speech" \
        -H "Authorization: Bearer $ACESTEP_API_KEY" -H 'Content-Type: application/json' \
        -d '{"input":"Smoke test.","voice":"af_heart","response_format":"mp3"}' \
        -o /tmp/anneal-smoke.mp3 && [[ -s /tmp/anneal-smoke.mp3 ]]; then echo "ok"; else echo "FAILED"; fails=$((fails+1)); fi

    echo -n "  image  ... "
    if curl -fsS -m 900 -X POST "http://127.0.0.1:$SUPERVISOR_PORT/v1/images/generations" \
        -H "Authorization: Bearer $ACESTEP_API_KEY" -H 'Content-Type: application/json' \
        -d '{"prompt":"a steel anvil","size":"512x512","steps":1,"response_format":"path"}' \
        -o /tmp/anneal-smoke.json && grep -q '"path"' /tmp/anneal-smoke.json; then echo "ok"; else echo "FAILED"; fails=$((fails+1)); fi

    echo -n "  music  ... "
    if "$HERE/generate.py" "smoke test tone" --instrumental --duration 20 \
        --out /tmp >/dev/null 2>&1; then echo "ok"; else echo "FAILED"; fails=$((fails+1)); fi

    # The patched non-turbo path is the one that has shipped garbled audio
    # twice, and nothing else here would notice: the patches only assert that
    # their anchors still match, not that the result is still music. This
    # generates on the high tier and looks at the waveform. Opt-in, because it
    # costs ~3 minutes on top of an already slow smoke run.
    if [[ "${DEEP:-}" == "yes" ]]; then
        echo -n "  music (high tier, patched path) ... "
        rm -rf /tmp/anneal-deep && mkdir -p /tmp/anneal-deep
        if "$HERE/generate.py" "solo piano, sparse, with pauses" --instrumental \
            --duration 20 --quality high --format flac \
            --out /tmp/anneal-deep >/dev/null 2>&1; then
            deep_file=$(ls -t /tmp/anneal-deep/*.flac 2>/dev/null | head -1)
            if [[ -z "$deep_file" ]]; then
                echo "FAILED (no output)"; fails=$((fails+1))
            elif "$HERE/tools/check-audio.py" "$deep_file" >/tmp/anneal-deep.txt 2>&1; then
                echo "ok"; sed -n '1p' /tmp/anneal-deep.txt
            else
                echo "FAILED — the patched path produced something that is not music"
                cat /tmp/anneal-deep.txt; fails=$((fails+1))
            fi
        else
            echo "FAILED (generation)"; fails=$((fails+1))
        fi
    fi

    echo
    if [[ $fails -eq 0 ]]; then
        echo "All three services generated successfully."
    else
        echo "$fails service(s) FAILED — do not treat this update as good." >&2
        echo "Logs: $AIMUSIC_ROOT/supervisor.log, api-server.log, image-server.log" >&2
        return 1
    fi
}

case "$MODE" in
    --check)  check ;;
    --deps)   deps ;;
    --models) models "${2:-required}" "${3:-}" ;;
    --smoke)  smoke ;;
    --smoke-deep) DEEP=yes smoke ;;
    *) echo "usage: $0 [--check|--deps|--models [all|list|<service>,...]|--smoke|--smoke-deep]" >&2
       exit 2 ;;
esac
