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

**Diagnosis:**
1. Check system memory:
   ```bash
   free -h
   ```
2. Check sandbox resource limits (if containerized):
   ```bash
   # Docker
   docker stats <container-id>
   
   # Kubernetes
   kubectl top pod <pod-name>
   ```
3. Review what the agent was doing when it OOM'd — check Langfuse traces for the last tool call

**Recovery:**
1. **Increase available memory** — resize the container or node
2. **Reduce scope** — split the ticket into smaller pieces
3. **Optimize the agent** — if a tool call uses excessive memory (e.g., loading entire repo into memory), optimize that tool
4. **Restart the worker** — OOM can leave the process in a bad state:
   ```bash
   # Docker
   docker restart <container-id>
   
   # Systemd
   systemctl restart robotsix-mill
   ```

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

### Failure 6: Observability Infrastructure Failure

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
- [ ] **Documentation:** Consult relevant docs (e.g., for git conflicts, see [Git Workflow](../git-workflow.md))
- [ ] **Action:** Based on the failure mode, apply the recovery steps above
- [ ] **Verify:** Confirm the ticket can now progress (run tests, check logs again)

---

## Getting Help

If a ticket is stuck and the runbook doesn't help:

1. **Check the docs** referenced throughout this runbook:
   - [Agent Definitions](../agents/agent-yaml-schema.md) — how agents are configured
   - [Cycles and Merging](../deployment/cycles.md) — how the mill orchestrates work
   - [Deployment](../deployment/deployment.md) — infrastructure setup
   - [Workspace Cleanup](../dev-tooling/workspace-cleanup.md) — cleaning up stuck workspaces

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
- **Module taxonomy:** [modules.yaml](../../docs/modules.yaml)
- **CI policy:** [ci-policy.md](../dev-tooling/ci-policy.md)
- **Agent references:** [agent_references/](../agent_references/)
