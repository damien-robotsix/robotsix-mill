"""SSRF guard tests for ``sandbox.fetch`` (ssrf-hardened-fetchers.md)."""

from __future__ import annotations

import socket
import subprocess
from types import SimpleNamespace

import pytest

from robotsix_mill.sandbox import _fetch


@pytest.fixture
def fake_settings():
    return SimpleNamespace(
        sandbox_pids_limit=64,
        sandbox_memory="256m",
        fetch_image="curl:test",
        web_fetch_timeout=10,
        web_fetch_max_bytes=1000,
    )


def _resolver(table):
    def getaddrinfo(host, port, *a, **kw):
        if host not in table:
            raise socket.gaierror("no such host")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
            for ip in table[host]
        ]

    return getaddrinfo


@pytest.fixture
def no_docker(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        raise AssertionError("docker must not run for a refused URL")

    monkeypatch.setattr(_fetch.subprocess, "run", run)
    return calls


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost:8080/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://0.0.0.0/",
        "http://2130706433/",  # decimal 127.0.0.1 — caught via resolution
    ],
)
def test_loopback_refused(url, fake_settings, no_docker, monkeypatch):
    monkeypatch.setattr(
        _fetch.socket, "getaddrinfo", _resolver({"2130706433": ["127.0.0.1"]})
    )
    rc, body = _fetch.fetch(url, settings=fake_settings)
    assert rc == 1
    assert body.startswith("refused:")
    assert no_docker == []


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://metadata/",
        "http://evil.example/",  # resolves to the metadata address
    ],
)
def test_metadata_refused(url, fake_settings, no_docker, monkeypatch):
    monkeypatch.setattr(
        _fetch.socket,
        "getaddrinfo",
        _resolver({"evil.example": ["93.184.216.34", "169.254.169.254"]}),
    )
    rc, body = _fetch.fetch(url, settings=fake_settings)
    assert rc == 1
    assert body.startswith("refused:")
    assert no_docker == []


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.1",
        "100.64.0.1",
        "224.0.0.1",
        "240.0.0.1",
        "fe80::1",
        "fc00::1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::",
    ],
)
def test_private_ranges_blocked(ip):
    import ipaddress

    assert _fetch._is_blocked(ipaddress.ip_address(ip))


def test_public_address_allowed():
    import ipaddress

    assert not _fetch._is_blocked(ipaddress.ip_address("93.184.216.34"))


def _completed(stdout=b"", location=""):
    stderr = f"\n{_fetch._REDIRECT_MARKER}{location}\n".encode()
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr=stderr)


def test_public_fetch_is_pinned_and_does_not_follow(fake_settings, monkeypatch):
    monkeypatch.setattr(
        _fetch.socket, "getaddrinfo", _resolver({"example.com": ["93.184.216.34"]})
    )
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return _completed(b"hello")

    monkeypatch.setattr(_fetch.subprocess, "run", run)
    rc, body = _fetch.fetch("https://example.com/x", settings=fake_settings)
    assert (rc, body) == (0, "hello")
    argv = calls[0]
    assert argv[argv.index("--resolve") + 1] == "example.com:443:93.184.216.34"
    assert argv[argv.index("--max-redirs") + 1] == "0"
    assert "-L" not in argv and "-sSL" not in argv


def test_redirect_to_internal_address_refused(fake_settings, monkeypatch):
    monkeypatch.setattr(
        _fetch.socket, "getaddrinfo", _resolver({"example.com": ["93.184.216.34"]})
    )
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return _completed(location="http://169.254.169.254/latest/meta-data/")

    monkeypatch.setattr(_fetch.subprocess, "run", run)
    rc, body = _fetch.fetch("https://example.com/", settings=fake_settings)
    assert rc == 1
    assert body.startswith("refused:")
    assert len(calls) == 1


def test_redirect_to_public_address_followed(fake_settings, monkeypatch):
    monkeypatch.setattr(
        _fetch.socket,
        "getaddrinfo",
        _resolver({"a.example": ["93.184.216.34"], "b.example": ["93.184.216.35"]}),
    )
    responses = [_completed(location="https://b.example/final"), _completed(b"done")]
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return responses.pop(0)

    monkeypatch.setattr(_fetch.subprocess, "run", run)
    rc, body = _fetch.fetch("https://a.example/", settings=fake_settings)
    assert (rc, body) == (0, "done")
    assert calls[1][-1] == "https://b.example/final"
    assert "b.example:443:93.184.216.35" in calls[1]


def test_redirect_loop_capped(fake_settings, monkeypatch):
    monkeypatch.setattr(
        _fetch.socket, "getaddrinfo", _resolver({"a.example": ["93.184.216.34"]})
    )
    monkeypatch.setattr(
        _fetch.subprocess,
        "run",
        lambda argv, **kw: _completed(location="https://a.example/again"),
    )
    rc, body = _fetch.fetch("https://a.example/", settings=fake_settings)
    assert rc == 1
    assert "redirects" in body


def test_unresolvable_host_refused(fake_settings, no_docker, monkeypatch):
    monkeypatch.setattr(_fetch.socket, "getaddrinfo", _resolver({}))
    rc, body = _fetch.fetch("https://nope.invalid/", settings=fake_settings)
    assert rc == 1
    assert body.startswith("refused:")
