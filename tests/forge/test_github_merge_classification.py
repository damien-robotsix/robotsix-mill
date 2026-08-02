"""``_merge_pr`` must keep GitHub's 405 message and mark it retryable.

GitHub answers 405 both for a permanent refusal ("Merge commits are not
allowed on this repository") and for a required check that has not
reported yet ("Required status check ... is expected"). Mill used to
collapse both into the guess "merge not allowed (branch protection?)"
and block the ticket, which discarded the only evidence that told the
two apart.
"""

from types import SimpleNamespace

from robotsix_mill.forge.github_pr import GitHubForgePRMixin, _api_message


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _merge(response):
    """Call the mixin method against a stub holding just ``_http``."""
    stub = SimpleNamespace(_http=SimpleNamespace(put=lambda *a, **k: response))
    return GitHubForgePRMixin._merge_pr(stub, owner="o", repo="r", pull_number=1)


def test_405_pending_required_check_is_retryable_and_keeps_the_message():
    response = _Response(
        405, {"message": 'Required status check "CodeQL" is expected.'}
    )

    result = _merge(response)

    assert result["merged"] is False
    assert result["retryable"] is True
    assert 'Required status check "CodeQL" is expected.' in result["reason"]


def test_405_permanent_refusal_also_surfaces_verbatim():
    """Still retryable-flagged: only the message distinguishes the two,
    and the merge stage's bounded retry converges on BLOCKED anyway."""
    response = _Response(
        405, {"message": "Merge commits are not allowed on this repository."}
    )

    result = _merge(response)

    assert "Merge commits are not allowed on this repository." in result["reason"]


def test_409_keeps_the_message_too():
    response = _Response(409, {"message": "Head branch was modified."})

    result = _merge(response)

    assert result["retryable"] is True
    assert "Head branch was modified." in result["reason"]


def test_success_is_unchanged():
    assert _merge(_Response(200)) == {"merged": True, "reason": "merged"}


def test_api_message_falls_back_to_raw_text():
    """A non-JSON error body must not swallow the diagnostic."""
    assert _api_message(_Response(405, None, "gateway said no")) == "gateway said no"
