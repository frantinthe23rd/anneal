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
#   ./update.sh --models    re-fetch weights at the pinned revisions
#   ./update.sh --smoke     just run the smoke test against what's installed
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK="$HERE/models.lock.json"

export HF_HUB_DISABLE_XET=1 HF_HUB_ENABLE_HF_TRANSFER=0

MODE="${1:---check}"

py() { "$AIMUSIC_ROOT/gen-venv/bin/python" "$@"; }

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
models() {
    echo "Fetching weights at pinned revisions (Xet disabled — it writes sparse files here) ..."
    py - "$LOCK" "$ACESTEP_CHECKPOINTS_DIR" <<'PY'
import json, sys
from huggingface_hub import snapshot_download
lock, ckpt = json.load(open(sys.argv[1])), sys.argv[2]
for repo, spec in lock["models"].items():
    target = spec["target"]
    kwargs = {"repo_id": repo, "revision": spec["revision"], "max_workers": 4}
    if target == "checkpoints_dir":
        kwargs["local_dir"] = ckpt
    elif target.startswith("checkpoints_dir/"):
        kwargs["local_dir"] = "%s/%s" % (ckpt, target.split("/", 1)[1])
    print("  %s @ %s" % (repo, spec["revision"][:12]), flush=True)
    snapshot_download(**kwargs)
print("fetched")
PY
    "$HERE/verify-models.py"
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
    --models) models ;;
    --smoke)  smoke ;;
    *) echo "usage: $0 [--check|--deps|--models|--smoke]" >&2; exit 2 ;;
esac
