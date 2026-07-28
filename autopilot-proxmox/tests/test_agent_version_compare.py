"""Version comparison behind /api/agent/v1/update-check.

The bug this pins down: the agent version scheme changed from a hand-rolled
0.1.x to CalVer YYYY.M.SEQ, and the comparison was a plain integer-tuple
compare. That is meaningless across the boundary in both directions, and the
dangerous direction is silent -- a CalVer agent asked about an 0.1.x publish
was told "current", which the agent treats as "nothing to do" and returns
without logging anything at all.

A fleet sat on 0.1.2.0 for weeks showing "Upgrade available" in the UI while
every agent was being told it was fine, and nothing on either side said why.
So the rule now is: a comparison we cannot make is never reported as "current".
"""
from __future__ import annotations

import pytest

from web.agent_v1_endpoints import _newer_version, _version_scheme, compare_versions


@pytest.mark.parametrize(
    "version,scheme",
    [
        ("0.1.2.0", "semver"),
        ("0.1.4", "semver"),
        ("1.0.0.0", "semver"),
        ("2026.7.14", "calver"),
        ("2026.7.14.0", "calver"),
        ("", "unknown"),
        (None, "unknown"),
        ("garbage", "unknown"),
    ],
)
def test_scheme_detection(version, scheme):
    assert _version_scheme(version) == scheme


@pytest.mark.parametrize(
    "published,installed",
    [
        ("0.1.4", "0.1.2.0"),
        ("0.1.4", "0.1.3"),
        ("2026.7.14", "2026.7.13"),
        ("2026.8.0", "2026.7.14"),
    ],
)
def test_newer_within_the_same_scheme_offers_the_upgrade(published, installed):
    status, _ = compare_versions(published, installed)
    assert status == "upgrade_available"
    assert _newer_version(published, installed) is True


@pytest.mark.parametrize(
    "published,installed",
    [
        ("0.1.4", "0.1.4"),
        ("0.1.4", "0.1.4.0"),
        ("2026.7.14", "2026.7.14"),
        ("2026.7.14", "2026.7.14.0"),
    ],
)
def test_equal_versions_are_current(published, installed):
    status, reason = compare_versions(published, installed)
    assert status == "current"
    assert reason == "installed_version_matches_published"


def test_installed_ahead_of_published_is_current_not_an_upgrade():
    """A rollback on the server must not push an agent backwards."""
    status, reason = compare_versions("0.1.2", "0.1.4")
    assert status == "current"
    assert reason == "installed_version_newer_than_published"
    assert _newer_version("0.1.2", "0.1.4") is False


@pytest.mark.parametrize(
    "published,installed",
    [
        # The exact production shape: source moved to CalVer while the last
        # published MSI was still 0.1.4.
        ("0.1.4", "2026.7.14.0"),
        # And the reverse, which used to read as a valid "upgrade" to what is
        # really unrelated numbering.
        ("2026.7.14", "0.1.2.0"),
    ],
)
def test_scheme_mismatch_is_indeterminate_never_current(published, installed):
    status, reason = compare_versions(published, installed)
    assert status == "indeterminate"
    assert reason.startswith("version_scheme_mismatch:")
    # The whole point: it must not answer "current", because the agent treats
    # that as "nothing to do" and returns without a word.
    assert status != "current"
    assert _newer_version(published, installed) is False


def test_unparseable_versions_are_indeterminate():
    status, reason = compare_versions("0.1.4", "not-a-version")
    assert status == "indeterminate"
    assert reason == "unparseable_version"


def test_missing_published_version_is_indeterminate():
    status, reason = compare_versions(None, "0.1.2.0")
    assert status == "indeterminate"
    assert reason == "no_published_version"


def test_agent_that_cannot_report_its_version_is_offered_the_upgrade():
    """An agent too broken to say what it is running is worth reinstalling."""
    status, reason = compare_versions("0.1.4", None)
    assert status == "upgrade_available"
    assert reason == "installed_version_unknown"


def test_the_production_case_that_should_have_worked():
    """0.1.2.0 installed against 0.1.4 published: a plain in-scheme upgrade.

    Kept explicit because this is the pair that was checked by hand against
    production while chasing the fault, and it must never silently regress.
    """
    status, _ = compare_versions("0.1.4", "0.1.2.0")
    assert status == "upgrade_available"


def test_three_part_published_never_loops_against_four_part_installed():
    """The MSI publishes "0.1.4"; the agent reports AssemblyVersion "0.1.4.0".

    These are the same build. Before padding they compared unequal, and in the
    direction where the published string has more segments that read as an
    upgrade to the version already installed, so the agent would download and
    reinstall the identical MSI on every heartbeat, forever, at 30s intervals.
    """
    for published, installed in (("0.1.4", "0.1.4.0"), ("0.1.4.0", "0.1.4")):
        status, reason = compare_versions(published, installed)
        assert status == "current", f"{published} vs {installed} produced {status}"
        assert reason == "installed_version_matches_published"

    # And the same shape on the CalVer side of the boundary.
    for published, installed in (("2026.7.14", "2026.7.14.0"), ("2026.7.14.0", "2026.7.14")):
        assert compare_versions(published, installed)[0] == "current"


def test_padding_does_not_mask_a_genuine_upgrade():
    """Padding must not swallow real differences in the trailing segment."""
    assert compare_versions("0.1.4.1", "0.1.4")[0] == "upgrade_available"
    assert compare_versions("0.1.5", "0.1.4.9")[0] == "upgrade_available"
    assert compare_versions("0.1.4", "0.1.4.1")[0] == "current"
