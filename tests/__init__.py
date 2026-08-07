"""Tests for Anneal.

Two suites, deliberately separated by what they need:

    tests/unit/        stdlib only, no network, no models, no running gateway.
                       Safe on CI hardware that has neither Apple silicon nor
                       the external volume.
    tests/acceptance/  talks to a live gateway on http://127.0.0.1:8001 and is
                       skipped wholesale when nothing answers there.

Everything is `unittest`. The gateway is deliberately stdlib-only and pins
nothing, and a test suite that needed a dependency the thing under test does
not have would be the first crack in that.
"""
