# Debugging and Observability Runbook

This runbook provides operators with a structured approach to diagnosing and recovering from common robotsix-mill failures. It consolidates observability guidance, debugging workflows, and recovery procedures in one place.

## Observability Stack Overview

The mill exposes observability across four layers:

### 1. Request Context (Board UI → Worker)

Every ticket and execution carries a **request ID** that flows through all subsequent agent calls:

- **Ticket ID** — stable identifier for the issue being worked (e.g., `TICKET-123`)
- **Run ID** — unique per ticket execution (e.g., `run_abc123def456`)
- **Agent Call ID** — unique per agent invocation within a run (e.g., `agent_xyz789`)

These IDs link board UI logs, worker logs, and traces so you can follow a single execution end-to-end.

### 2. Langfuse Tracing

[Langfuse](https://langfuse.com) is the authoritative trace store. Every agent call, tool invocation, and model interaction is recorded there.

- **Access:** `https://<langfuse-instance>/traces`
- **Filter by request ID:** Each trace is tagged with the ticket/run ID
- **What you see:** Prompt sent, model response, tool calls made, tokens consumed, latency, errors
- **When to use:** Model hallucination, unexpected tool invocation, token overage, latency investigation

### 3. Worker Logs

Structured JSON logs from the mill worker process. Entries include:

- **timestamp** — ISO 8601 (UTC)
- **level** — DEBUG, INFO, WARNING, ERROR, CRITICAL
- **message** — human-readable summary
- **request_id** / **run_id** — links to traces and tickets
- **module** — source component (`agents.base`, `forge.github`, `runtime.executor`, etc.)
- **details** — contextual fields (error type, retry count, agent name, etc.)

**Log location depends on deployment:**
- **Local dev:** stdout / `~/.mill/logs/`
- **Docker:** container stdout (accessible via `docker logs <container>`)
- **Kubernetes:** pod logs (accessible via `kubectl logs <pod>`)
- **Systemd:** `journalctl -u robotsix-mill` or log aggregation system

### 4. Workspace Artifacts

Each ticket's workspace persists:
- Source code and git history
- Agent-generated diffs and branches
- Test output and coverage reports
- Sandbox execution logs and container images

**Location:** Configurable (typically `~/.mill/workspaces/<ticket-id>/`)

---

## Step-by-Step Debugging Workflow

When a ticket fails or behaves unexpectedly, follow this workflow:

### Step 1: Check the Board UI

1. Go to the board (local: `http://localhost:8000`, prod: configured URL)
2. Find the ticket and open it
3. Look at the **ticket status** and **current stage** (e.g., `refine → implement → review`)
4. Read the **latest comment** — it often summarizes the failure
5. Note the **request ID** and **run ID** visible in the UI (usually in a `[run_...]` or `[TICKET-...]` label)

### Step 2: Check Langfuse Traces

1. Open Langfuse at your configured instance
2. Search traces by **request ID** (the run ID from Step 1)
3. Look for the **most recent trace** — expand it to see:
   - The agent's system prompt and user input
   - The model's response
   - Tool calls made (including any failures)
   - Final status (success, error, timeout)
4. Common findings:
   - **Tool error in trace:** The agent called a tool correctly, but the tool returned an error (e.g., git command failed)
   - **Hallucinated tool call:** The agent called a tool that doesn't exist or with invalid arguments
   - **Truncated response:** The model hit a token limit mid-response
   - **Timeout:** The model didn't respond within the deadline

### Step 3: Check Worker Logs

1. Fetch logs from your deployment:
   ```bash
   # Docker
   docker logs <container-id> | grep <request-id>
   
   # Kubernetes
   kubectl logs -n <namespace> <pod-name> | grep <request-id>
   
   # Local dev
   grep <request-id> ~/.mill/logs/*.log
   
   # Systemd
   journalctl -u robotsix-mill | grep <request-id>
   ```

2. Look for **ERROR** or **CRITICAL** entries with that request ID
3. Common log patterns:
   - `"message": "Agent failed", "error": "FileNotFoundError"` → file path issue
   - `"message": "Tool timeout"` → subprocess took too long
   - `"message": "Sandbox limit exceeded"` → resource exhaustion
   - `"message": "Git conflict"` → merge/rebase failed

### Step 4: Inspect Workspace

1. Locate the workspace for the ticket:
   ```bash
   ls ~/.mill/workspaces/  # or configured path
   cd ~/.mill/workspaces/<ticket-id>
   ```

2. Check git state:
   ```bash
   git status      # current branch, unstaged changes
   git log --oneline -10  # recent commits
   git branch -a   # branches created by agents
   git diff HEAD~1 # what changed in the last commit
   ```

3. Check for agent artifacts:
   ```bash
   ls -la  # look for .agent.log, test output, coverage reports
   cat .agent.log | tail -50  # recent agent stderr
   ```

4. Run any tests locally:
   ```bash
   uv run pytest tests/ -xvs  # reproduce test failure
   ```

#### Workspace Preservation

By default, workspaces are deleted when a ticket reaches a terminal state (closed, merged, or abandoned). In some cases — post-mortem investigation, compliance audit, or debugging a stale issue — you may want to preserve the workspace.

**Preserve a workspace:**
```bash
# Disable automatic cleanup for new tickets
export MILL_PRUNE_CLONE_ON_CLOSE=false

# Or in config.json:
# "prune_clone_on_close": false

# Restart the worker to pick up the change
systemctl restart robotsix-mill
```

**When to preserve:**
- Post-mortem investigation: analyze what failed after the fact
- Compliance/audit: retain evidence of changes and test coverage
- Stale debugging: reproduce intermittent failures days or weeks later

**Monitor disk space:**
```bash
# Check workspace directory size
du -sh ~/.mill/workspaces/

# List workspaces by age
ls -lht ~/.mill/workspaces/ | head -20

# Clean up specific old workspaces if disk is constrained
rm -rf ~/.mill/workspaces/<old-ticket-id>
```

**Auto-cleanup after investigation:**
```bash
# Re-enable cleanup for future tickets
unset MILL_PRUNE_CLONE_ON_CLOSE

# Or in config.json: "prune_clone_on_close": true

# Restart the worker
systemctl restart robotsix-mill
```

### Step 5: Validate Configuration

1. Check that the mill is correctly configured:
   ```bash
   robotsix-mill config show  # or cat config/config.example.json
   ```

2. Verify connectivity to external services:
   ```bash
   # GitHub API
   curl -H "Authorization: Bearer $GITHUB_TOKEN" \
     https://api.github.com/user
   
   # Langfuse
   curl https://<langfuse-host>/api/health
   
   # Model endpoint (if custom)
   curl https://<model-endpoint>/health
   ```

---

## Common Failure Modes

### Failure 1: Sandbox Execution Errors

**Symptoms:**
- `error": "Sandbox execution failed"` in logs
- Traces show tool timeout or "command not found"
- Workspace `.agent.log` is truncated or empty

**Diagnosis:**
1. Check logs for the exact error message:
   ```bash
   grep "Sandbox execution failed" ~/.mill/logs/*.log
   ```
2. Check if the tool exists in the sandbox:
   ```bash
   # Verify uv is available
   uv --version
   # Verify Python
   python3 --version
   ```
3. Look at the command that failed in Langfuse traces — verify it's syntactically correct

**Recovery:**
1. **If a tool is missing:** Install it (e.g., `uv sync --frozen`) or add it to the sandbox environment
2. **If a command timed out:** Increase the timeout in the agent definition or optimize the command
3. **If git fails:** Check for uncommitted changes or conflicts:
   ```bash
   cd ~/.mill/workspaces/<ticket-id>
   git status
   git reset --hard HEAD  # if needed to recover
   ```

---

### Failure 2: Out-of-Memory (OOM) Errors

**Symptoms:**
- `error": "OOM killer", "oom": true` in logs
- Agent stops mid-execution with no final message
- Trace ends abruptly with no tool response
- Worker process dies without logging a graceful shutdown

**Diagnosis:**
1. Check the heartbeat.json diagnostic file (persists OOM clues):
   ```bash
   # On startup after an abrupt process death, the mill logs OOM suspicion:
   grep "died abruptly\|suspected OOM kill" ~/.mill/logs/*.log
   
   # Read the heartbeat file in the data dir
   cat ~/.mill/heartbeat.json
   # Expected output on OOM: {"process_id": ..., "timestamp": "...", "was_abrupt": true}
   
   # Check for graceful shutdown logs (OOM typically skips these):
   grep -i "graceful\|shutdown\|signal" ~/.mill/logs/*.log | tail -5
   ```

2. Check cgroup OOM kill counter (if containerized):
   ```bash
   # Docker container — shows if OomKilled is true
   docker inspect <container-id> | grep -A 5 '"OomKilled"'
   
   # Kubernetes pod — look for OOMKilled state
   kubectl describe pod <pod-name> | grep -A 5 "Last State"
   kubectl get pod <pod-name> -o jsonpath='{.status.containerStatuses[*].lastState}'
   
   # Linux cgroup (direct access) — memory.oom_control counts OOM kills
   cat /sys/fs/cgroup/memory.oom_control
   cat /sys/fs/cgroup/memory/memory.oom_control  # cgroup v2
   cat /proc/<pid>/cgroup | grep memory
   ```

3. Check system memory and pressure at time of failure:
   ```bash
   # Current memory state
   free -h
   
   # Historical memory pressure (if available)
   cat /proc/pressure/memory  # Linux PSI metrics
   dmesg | grep -i "oom\|out of memory" | tail -10
   ```

4. Review what the agent was doing when it OOM'd:
   ```bash
   # Check Langfuse traces for the last tool call
   # Look for: large context window agent call, file read on multi-GB codebase, or subprocess memory spike
   ```

**Recovery:**
1. **Increase available memory** — resize the container or node
   ```bash
   # Docker: update memory limit
   docker update --memory 8g <container-id>
   
   # Kubernetes: edit pod spec
   kubectl set resources pod <pod-name> --limits=memory=8Gi
   ```
2. **Reduce scope** — split the ticket into smaller pieces (especially for implement passes with large context windows)
3. **Optimize the agent** — if a tool call uses excessive memory (e.g., loading entire repo into memory), optimize that tool
4. **Restart the worker** — OOM can leave the process in a bad state:
   ```bash
   # Docker
   docker restart <container-id>
   
   # Systemd
   systemctl restart robotsix-mill
   ```
5. **Clear stale workspaces** — if disk is full and blocking the process, clean up old workspace clones (see "Workspace Preservation" section below)

---

### Failure 3: Configuration Errors

**Symptoms:**
- `error": "NotConfiguredError"` or `"ConfigValidationError"` in logs
- Agent doesn't run or runs with wrong parameters
- Environment variable not found

**Diagnosis:**
1. Check the config file:
   ```bash
   cat config/config.example.json  # what was configured
   robotsix-mill config show        # what's actually loaded
   ```
2. Check environment variables:
   ```bash
   env | grep MILL_  # all mill-related vars
   ```
3. Look at the error message in logs — it should say which config key is missing or invalid

**Recovery:**
1. **Missing env var:** Add it:
   ```bash
   export MILL_<VAR>=<value>
   ```
2. **Invalid config value:** Fix the value and restart:
   ```bash
   # Edit config file or re-export env var
   systemctl restart robotsix-mill  # or docker restart
   ```
3. **Schema mismatch:** Check the config schema:
   ```bash
   cat config/config.schema.json
   ```

---

### Failure 4: Stuck Retries or Infinite Loops

**Symptoms:**
- A ticket has been in the same stage for hours
- Logs show the same error repeated many times
- Agent keeps retrying the same operation

**Diagnosis:**
1. Check how long the ticket has been stuck:
   ```bash
   # On the board UI: look at the timestamp of the latest comment
   ```
2. Check the retry count in logs:
   ```bash
   grep "retry_count" ~/.mill/logs/*.log | tail -20
   ```
3. Look at Langfuse traces — see if the model keeps making the same failing tool call

**Recovery:**
1. **If retrying forever:** Stop the worker and manually edit the ticket's status:
   ```bash
   # Stop worker
   systemctl stop robotsix-mill
   
   # Edit the ticket (API call or direct database)
   robotsix-mill ticket show <ticket-id>  # see current state
   ```
2. **If the model is stuck in a loop:** The underlying issue (e.g., test failure) must be fixed before the agent can proceed — see the test logs for the actual error
3. **Escalate:** If a retry loop is due to a model hallucination or incorrect tool behavior, file a separate issue and move the ticket to a blocked state

---

### Failure 5: Git Merge Conflicts

**Symptoms:**
- `error": "Git merge conflict"` in logs
- Workspace has conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`)
- Agent can't commit or push

**Diagnosis:**
1. Check git status:
   ```bash
   cd ~/.mill/workspaces/<ticket-id>
   git status
   ```
2. See which files have conflicts:
   ```bash
   git diff --name-only --diff-filter=U
   ```
3. Look at the actual conflicts:
   ```bash
   git diff  # shows conflict regions
   ```

**Recovery:**
1. **Automatic resolution (if safe):** Use a merge strategy:
   ```bash
   cd ~/.mill/workspaces/<ticket-id>
   git merge --abort  # if you want to start over
   # OR
   git checkout --theirs <file>  # take incoming changes
   git add <file>
   git commit -m "Resolve conflict"
   ```
2. **Manual resolution:** Edit the conflicted file, remove conflict markers, then:
   ```bash
   git add <file>
   git commit
   ```
3. **Rebase instead:** If the agent was rebasing when it failed:
   ```bash
   git rebase --abort  # to cancel
   # OR
   # Fix the conflict and continue
   git add <file>
   git rebase --continue
   ```

---

### Failure 6: Merge-Poll Failures

The mill's merge stage enters a polling loop to check whether a PR is ready to merge. Three ceilings prevent infinite polling when the PR is stuck in a merge-blocking state:

**Symptom 1: Green Unpromotable (CI passes but forge refuses merge)**
- `ticket → IMPLEMENT_COMPLETE → HUMAN_MR_APPROVAL → ... polling ...`
- Logs show: `"green_unpromotable_poll_count"` reaching default limit (10 polls)
- **Cause:** All CI checks report green, but GitHub branch protection requires a status context that no workflow produces
- **Diagnosis:**
  ```bash
  # Check branch protection rules
  gh api repos/<owner>/<repo>/branches/<branch>/protection
  
  # Look for required_status_checks that don't match any workflow
  grep -r "required_status_checks" .github/workflows/
  ```
- **Recovery:**
  1. Fix the branch protection rule to remove the missing context, or
  2. Add the missing workflow context check, or
  3. Increase `MILL_GREEN_UNPROMOTABLE_MAX_POLLS` (default 10) if temporary
  4. Manually approve and merge via GitHub UI if only human oversight is needed

**Symptom 2: Empty Rollup (CI succeeds but shows zero checks)**
- `ticket → HUMAN_MR_APPROVAL → ... polling ...`
- Logs show: `"empty_rollup_poll_count"` reaching default limit (3 polls)
- PR state: `mergeable_state=blocked`, zero check runs reported
- **Cause:** PR's `pull_request` event never fired in GitHub Actions, so no workflows ran
- **Diagnosis:**
  ```bash
  # Check PR event triggers in workflows
  grep -r "pull_request:" .github/workflows/ | head -5
  
  # Query the PR directly
  gh pr view <pr-number> --json mergeStateStatus,reviewDecision
  ```
- **Recovery:**
  1. **Mill auto-heals (default):** After 3 polls, the mill closes and reopens the PR to trigger the event
  2. **Manual trigger:** Close and reopen the PR yourself:
     ```bash
     gh pr close <pr-number>
     gh pr reopen <pr-number>
     ```
  3. Increase `MILL_EMPTY_ROLLUP_MAX_POLLS` (default 3) to allow more time before auto-heal

**Symptom 3: Merge PR Missing (PR not found after multiple polls)**
- `ticket → HUMAN_MR_APPROVAL → ... polling ...`
- Logs show: `"merge_pr_missing_poll_count"` reaching default limit (20 polls)
- **Cause:** The PR is not found in the expected repository (common in cross-repo / multi-repo delivery)
- **Diagnosis:**
  ```bash
  # Single-repo delivery: Check if PR exists
  gh pr view <pr-number> --repo <owner>/<repo>
  
  # Multi-repo delivery: Check pr_urls.json in the workspace
  cd ~/.mill/workspaces/<ticket-id>
  cat pr_urls.json | jq .
  ```
- **Recovery:**
  1. **Single-repo:** If the PR was deleted, the ticket must be manually recovered or marked BLOCKED
  2. **Multi-repo:** Verify that all expected repositories have the PR:
     ```bash
     while read url; do gh pr view --web "$url"; done < pr_urls.json
     ```
  3. If a repo is missing a PR, file a separate ticket to diagnose why
  4. Increase `MILL_MERGE_PR_MISSING_MAX_POLLS` (default 20) if delivery is slow

**Configuring merge-poll ceilings:**
```bash
# All three can be tuned via env vars or config.json
export MILL_GREEN_UNPROMOTABLE_MAX_POLLS=10      # default
export MILL_EMPTY_ROLLUP_MAX_POLLS=3             # default
export MILL_MERGE_PR_MISSING_MAX_POLLS=20        # default

# Or in config.json
# {"pipeline": {
#   "green_unpromotable_max_polls": 10,
#   "empty_rollup_max_polls": 3,
#   "merge_pr_missing_max_polls": 20
# }}
```

---

### Failure 7: Observability Infrastructure Failure

**Symptoms:**
- Langfuse traces are missing for recent runs
- Logs aren't appearing in the aggregation system
- `error": "Failed to send trace to Langfuse"` in logs

**Diagnosis:**
1. Check Langfuse availability:
   ```bash
   curl https://<langfuse-host>/api/health
   ```
2. Check network connectivity from worker:
   ```bash
   # From the worker container/pod
   curl https://<langfuse-host>/api/health
   ping -c 1 <langfuse-host>
   ```
3. Check logs for trace send errors:
   ```bash
   grep "trace.*failed\|langfuse.*error" ~/.mill/logs/*.log
   ```

**Recovery:**
1. **Langfuse is down:** Restart it (not the worker — the worker will queue traces and retry)
2. **Network issue:** Check firewall rules, DNS, and connectivity
3. **Credentials expired:** Regenerate API keys and update `MILL_LANGFUSE_*` env vars
4. **Worker continues without traces:** Traces are optional; the worker will log locally and continue. Once Langfuse is back, pending traces will be sent on retry

---

## Workspace Inspection Guide

### Git Commands for Investigation

```bash
cd ~/.mill/workspaces/<ticket-id>

# See current branch and status
git status

# See recent commits (who made them, when)
git log --oneline -20

# See what changed in the last commit
git show HEAD

# See branches created by agents (prefixed with agent/ or agent_)
git branch -a

# See the diff between current and main
git diff origin/main

# See all commits since main
git log --oneline origin/main..HEAD

# Check for uncommitted changes
git status --short

# View the full diff with context
git diff HEAD~1
```

### Workspace Preservation

By default, when a ticket reaches a terminal state (DONE, BLOCKED, ARCHIVED), its workspace clone is **deleted** to save disk space. For post-mortem investigation or long-term tracking, preserve clones on close:

```bash
# Set globally (all future closed tickets preserve workspaces)
export MILL_PRUNE_CLONE_ON_CLOSE=false

# Or in config file
# config.json: {"pipeline": {"prune_clone_on_close": false}}

# Verify the setting
robotsix-mill config show | grep prune_clone_on_close
```

**When to preserve:**
- Debugging catastrophic failures (OOM, merge conflicts, sandbox errors)
- Post-mortem root cause analysis
- Regulatory or auditing requirements to retain evidence

**Important:** Preserved workspaces accumulate on disk. Monitor space regularly:
```bash
du -sh ~/.mill/workspaces/  # total size
du -sh ~/.mill/workspaces/*/  | sort -h | tail -10  # top 10 largest
```

**Cleanup stale workspaces (manual):**
```bash
# Delete workspaces older than 30 days
find ~/.mill/workspaces/ -type d -mtime +30 -exec rm -rf {} \;

# Or delete specific workspace
rm -rf ~/.mill/workspaces/<ticket-id>/
```

### Inspecting Agent Artifacts

```bash
cd ~/.mill/workspaces/<ticket-id>

# Agent log (if present)
cat .agent.log 2>/dev/null | tail -100

# Test output
ls test-output.* coverage.*

# Agent-generated files (look for .agent.* or .mill.* prefixes)
find . -name ".agent.*" -o -name ".mill.*"
```

### Running Tests Locally

```bash
cd ~/.mill/workspaces/<ticket-id>

# Run the same tests the agent ran
uv run pytest tests/ -xvs

# Or run specific test file
uv run pytest tests/test_specific.py -xvs

# See coverage
uv run pytest --cov=src tests/
```

---

## Recovery Checklist

When a ticket is stuck or failed, use this checklist to systematically work through recovery:

- [ ] **Board UI:** Read the latest comment and note the run ID
- [ ] **Langfuse traces:** Search by run ID, review the last trace, identify where it failed
- [ ] **Worker logs:** Grep for the run ID, find ERROR or CRITICAL entries
- [ ] **Workspace:** Check git status, run tests locally if applicable
- [ ] **Configuration:** Verify env vars and config file are correct
- [ ] **External services:** Confirm Langfuse, GitHub, and model endpoint are reachable
- [ ] **Capacity:** Check available memory, disk, CPU
- [ ] **Documentation:** Consult relevant docs for additional context (see the runbook sections above for git conflicts)
- [ ] **Action:** Based on the failure mode, apply the recovery steps above
- [ ] **Verify:** Confirm the ticket can now progress (run tests, check logs again)

---

## Getting Help

If a ticket is stuck and the runbook doesn't help:

1. **Check the docs** referenced throughout this runbook:
   - [Agent Definitions](../agents/agent-yaml-schema.md) — how agents are configured
   - [Cycles and Merging](../cycles.md) — how the mill orchestrates work
   - [Deployment](../dev-tooling/deployment.md) — infrastructure setup
   - [Workspace Cleanup](../core/workspace-cleanup.md) — cleaning up stuck workspaces

2. **Collect diagnostics** to share:
   ```bash
   # Log snippet (last 50 lines for the run ID)
   grep <run-id> ~/.mill/logs/*.log | tail -50
   
   # Config (sanitized of credentials)
   robotsix-mill config show | head -30
   
   # Langfuse trace link (if available)
   https://<langfuse-host>/traces/<trace-id>
   ```

3. **File a draft ticket** on the board with:
   - Ticket ID that's stuck
   - How long it's been stuck
   - Latest log excerpt
   - What you've tried so far

4. **Escalate to the team** if it's a systematic issue (e.g., Langfuse down, infra failure)

---

## Additional Resources

- **Config reference:** [configuration.md](../config/configuration.md)
- **Module taxonomy:** [modules.yaml](../modules.yaml)
- **CI policy:** [ci-policy.md](../dev-tooling/ci-policy.md)
- **Agent references:** [agent_references/](../agent_references/)
