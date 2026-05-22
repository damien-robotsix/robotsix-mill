"""GitLab forge adapter. STUB — to be implemented."""

from __future__ import annotations

from .base import Forge


class GitLabForge(Forge):
    def open_merge_request(
        self, *, source_branch: str, title: str, body: str
    ) -> str:
        raise NotImplementedError("GitLab forge adapter not implemented yet")

    def pr_status(self, *, source_branch: str) -> dict | None:
        raise NotImplementedError("GitLab forge adapter not implemented yet")

    def check_status(self, *, source_branch: str) -> dict | None:
        raise NotImplementedError("GitLab forge adapter not implemented yet")

    def merge_pr(self, *, source_branch: str) -> dict:
        return {"merged": False, "reason": "GitLab forge adapter not implemented yet"}

    def list_workflow_runs(
        self, *, branch: str | None = None, head_sha: str | None = None
    ) -> list[dict]:
        raise NotImplementedError("GitLab forge adapter not implemented yet")

    def fetch_workflow_job_logs(self, *, run_id: int) -> str:
        raise NotImplementedError("GitLab forge adapter not implemented yet")
