"""The sandbox egress allowlist must keep ``uv lock``-style resolution possible.

The implement/ci_fix sandbox is network-isolated by design: its only egress
is the tinyproxy destination allowlist in ``sandbox/proxy/filter``
(FilterDefaultDeny). A locked dependency bump (e.g. an audit/CVE fix such as
the anyio advisory) is regenerated in-sandbox with
``uv lock --upgrade-package <pkg>``, which must reach PyPI (index +
artifact host) and GitHub (existing ``git+https`` pins are reused, so only
public repos matter) — and nothing else. These tests regress-guard that
allowlist so the gate-drain (CI red on a new advisory, ci_fix unable to
regenerate uv.lock) cannot silently re-appear.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FILTER = REPO_ROOT / "sandbox" / "proxy" / "filter"
TINYPROXY_CONF = REPO_ROOT / "sandbox" / "proxy" / "tinyproxy.conf"

# Hosts `uv lock --upgrade-package <pkg>` must reach to regenerate a
# lockfile for a locked-dependency bump. Mirrors the patterns in
# sandbox/proxy/filter.
UV_LOCK_HOSTS = {
    r"(^|\.)pypi\.org$",
    r"(^|\.)pythonhosted\.org$",
    r"(^|\.)github\.com$",
    r"(^|\.)githubusercontent\.com$",
}

# Hosts that must stay DENIED: the sandbox must resolve dependency bumps
# without opening general network access. api.osv.dev is the advisory-DB
# host `uv audit` queries from CI — it is intentionally NOT reachable from
# the sandbox (audit runs network-enabled in CI; the sandbox only regenerates
# the lockfile).
DENIED_HOSTS = ("api.osv.dev", "example.com")


def _allowlist_patterns() -> list[str]:
    lines = FILTER.read_text(encoding="utf-8").splitlines()
    patterns = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return patterns


def test_allowlist_covers_every_host_uv_lock_needs():
    """PyPI + GitHub must be permitted for in-sandbox lock regeneration."""
    patterns = _allowlist_patterns()
    assert patterns, "sandbox/proxy/filter must contain allowlist patterns"
    for host in UV_LOCK_HOSTS:
        assert host in patterns, (
            f"egress allowlist is missing {host!r} — `uv lock --upgrade-package` "
            "cannot regenerate uv.lock for an audit/CVE bump, re-opening the "
            "gate-drain blocker"
        )


def test_allowlist_denies_non_registry_hosts():
    """Deny-by-default: hosts outside the allowlist are refused."""
    patterns = _allowlist_patterns()
    for host in DENIED_HOSTS:
        assert not any(re.search(pattern, host) for pattern in patterns), (
            f"egress allowlist must deny {host!r}"
        )


def test_tinyproxy_filter_is_a_deny_by_default_allowlist():
    """FilterDefaultDeny Yes + host matching turn the filter into an allowlist."""
    conf = TINYPROXY_CONF.read_text(encoding="utf-8")
    assert "FilterDefaultDeny Yes" in conf
    assert "FilterURLs Off" in conf
    assert 'Filter "/etc/tinyproxy/filter"' in conf
