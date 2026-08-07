#!/usr/bin/env bash
# Run Anneal's tests.
#
# Unit tests always. Acceptance tests as well, if a gateway is answering — they
# are pointless otherwise and are skipped with a reason rather than failing.
#
# Nothing is installed and nothing is pinned: this is stdlib unittest, run by
# the same interpreter the gateway itself runs under (/usr/bin/python3), so a
# passing suite says something about the Python that will actually serve.
#
#   tools/test.sh                 unit, plus acceptance if the gateway is up
#   tools/test.sh --unit          unit only
#   tools/test.sh --acceptance    acceptance only (fails if nothing answers)
#   tools/test.sh --heavy         also the tests that load models — minutes,
#                                 and they evict whatever is resident
#   tools/test.sh -v              verbose
#
# Everything after `--` goes to unittest, so a single test can be named:
#   tools/test.sh --unit -- tests.unit.test_outputs.TestDeleteContainment

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-/usr/bin/python3}"
BASE="${ANNEAL_TEST_BASE:-http://127.0.0.1:8001}"

want_unit=1
want_acceptance=auto
verbosity=()
passthrough=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --unit)        want_acceptance=no ;;
        --acceptance)  want_unit=0; want_acceptance=yes ;;
        --heavy)       export ANNEAL_TEST_HEAVY=1 ;;
        -v|--verbose)  verbosity=(-v) ;;
        --)            shift; passthrough=("$@"); break ;;
        -h|--help)     sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)             echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

cd "$HERE"

# Keep the sandbox off the internal disk when the external volume is there;
# it is only a few sqlite files, but the repo's rule is that bulk goes on
# Storage and this is one less thing to think about.
if [[ -z "${ANNEAL_TEST_ROOT:-}" && -d /Volumes/Storage/AIMusic ]]; then
    ANNEAL_TEST_ROOT="$(mktemp -d /Volumes/Storage/AIMusic/anneal-tests-XXXXXX)"
    export ANNEAL_TEST_ROOT
    trap 'rm -rf "$ANNEAL_TEST_ROOT"' EXIT
fi

failed=0

if [[ "$want_unit" == 1 ]]; then
    echo "== unit =="
    if [[ ${#passthrough[@]:-0} -gt 0 && "$want_acceptance" != yes ]]; then
        "$PYTHON" -m unittest ${verbosity[@]+"${verbosity[@]}"} ${passthrough[@]+"${passthrough[@]}"} || failed=1
    else
        "$PYTHON" -m unittest discover -s tests/unit -t . ${verbosity[@]+"${verbosity[@]}"} || failed=1
    fi
fi

gateway_up() {
    "$PYTHON" - "$BASE" <<'EOF'
import sys, urllib.parse, http.client
parts = urllib.parse.urlparse(sys.argv[1])
cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
try:
    conn = cls(parts.hostname, parts.port, timeout=3)
    conn.request("GET", "/health")
    sys.exit(0 if conn.getresponse().status == 200 else 1)
except Exception:
    sys.exit(1)
EOF
}

run_acceptance() {
    echo
    echo "== acceptance ($BASE) =="
    if [[ "${ANNEAL_TEST_HEAVY:-}" == 1 ]]; then
        echo "   ANNEAL_TEST_HEAVY=1 — this will load models and evict resident work."
    fi
    "$PYTHON" -m unittest discover -s tests/acceptance -t . ${verbosity[@]+"${verbosity[@]}"} || failed=1
}

case "$want_acceptance" in
    yes) run_acceptance ;;
    auto)
        if gateway_up; then
            run_acceptance
        else
            echo
            echo "== acceptance skipped: nothing answering /health at $BASE =="
            echo "   start it with ./start-api.sh, or set ANNEAL_TEST_BASE"
        fi
        ;;
esac

exit "$failed"
