# Component Agent

The component agent is a generic **monitor / config-get / config-set**
responder that registers on the agent-comm broker, making robotsix-mill
a discoverable, monitorable, and live-configurable component agent.

It is additive to the existing board-agent and board-manager integrations
— those are untouched and continue to operate independently.

## Agent-id deviation

The epic's provisional id ``board-manager-robotsix-mill`` is already
claimed by the existing `BoardManager` conversational agent (registered
in ``_start_board_manager`` as ``"board-manager-{repo_id}"``). Registering
a second `BrokeredAgent` under that id would collide on the broker.

**The component-agent responder is registered under the distinct id
``component-robotsix-mill``** (configurable via
``component_agent_agent_id``). This avoids the collision while keeping
the agent discoverable under a searchable, template-standard name.

## Enabling

The component agent is **off by default**. Set these environment variables
(or their YAML equivalents) to enable it:

```bash
export MILL_COMPONENT_AGENT_ENABLED=true
export MILL_COMPONENT_AGENT_BROKER_HOST=ai-broker.robotsix.net
export MILL_COMPONENT_AGENT_BROKER_TOKEN=your-bearer-token
# Optional overrides:
# export MILL_COMPONENT_AGENT_AGENT_ID=component-robotsix-mill
# export MILL_COMPONENT_AGENT_BROKER_PORT=443
# export MILL_COMPONENT_AGENT_BROKER_SCHEME=https
```

| Field | Env var | Default | Description |
|---|---|---|---|
| `component_agent_enabled` | `MILL_COMPONENT_AGENT_ENABLED` | `false` | Master kill-switch. |
| `component_agent_agent_id` | `MILL_COMPONENT_AGENT_AGENT_ID` | `component-robotsix-mill` | Agent id on the broker. |
| `component_agent_broker_host` | `MILL_COMPONENT_AGENT_BROKER_HOST` | `""` | Broker hostname (required). |
| `component_agent_broker_port` | `MILL_COMPONENT_AGENT_BROKER_PORT` | `443` | Broker port. |
| `component_agent_broker_scheme` | `MILL_COMPONENT_AGENT_BROKER_SCHEME` | `https` | Broker scheme. |
| `component_agent_broker_token` | `MILL_COMPONENT_AGENT_BROKER_TOKEN` | `""` | Bearer token (required). |

All fields are settable via YAML under the ``component_agent`` key:

```yaml
component_agent:
  enabled: true
  agent_id: component-robotsix-mill
  broker_host: ai-broker.robotsix.net
  broker_port: 443
  broker_scheme: https
  broker_token: your-bearer-token
```

## Contract

The responder handles three request kinds on ``request.body["kind"]``:

### `monitor`

Returns live telemetry from the running mill process:

- **Process uptime** — elapsed seconds since process start.
- **Worker snapshot** — running state, active periodic loops, consumer
  task count, queue depth, in-flight periodic passes.
- **Recent run activity** — last 10 runs per board from the run registry.
- **Ticket counts** — total tickets and per-state breakdown.

Payload is a plain dict wrapped in ``Response.to(request, body=...)``.

### `config-get`

Returns a flat dotted-path config snapshot with all secret-named fields
(keys containing ``token``, ``api_key``, ``password``, or ``secret``)
redacted to ``"***"``.  Also includes a ``meta`` block describing which
keys are ``SETTABLE_KEYS`` and their types.

### `config-set`

Accepts a ``payload.updates`` map of dotted-key → new value. The responder:

1. **Validates** every key is in ``SETTABLE_KEYS`` — startup-only fields
   (forge wiring, data-dir layout, broker connection, sandbox provisioning)
   are rejected with ``unknown_keys`` error.
2. **Rebuilds** a full ``Settings`` candidate to run pydantic cross-field
   invariants (e.g. ``component_agent_enabled=True`` requires
   ``component_agent_broker_host``, etc.).
3. **Applies** only on validation success — mutates ``app.state.settings``
   in-place so all consumers (worker, periodic passes, routes) pick up the
   new values on their next access.
4. **Audit-logs** every change with ``{key: (old, new)}`` via the
   ``robotsix_mill`` logger.

## Architecture

```text
┌─────────────────────────────┐
│  Other agents (agent-comm)  │
└──────────────┬──────────────┘
               │ Request/Response
               ▼
┌─────────────────────────────┐
│   ComponentAgentResponder   │
│  (robotsix-mill)            │
│  registered as              │
│  component-robotsix-mill    │
└──────────────┬──────────────┘
               │ reads/writes
               ▼
┌─────────────────────────────┐
│      app.state              │
│  (settings, worker,         │
│   service, run_registries)  │
└─────────────────────────────┘
```

## Lifecycle

The component agent starts after the worker is started and unfinished
tickets are requeued — alongside the board agent and board manager.
It stops in the lifespan's ``finally`` block before the worker is
stopped, matching the board-agent stop ordering.

When ``component_agent_enabled`` is ``false`` (the default), zero
imports from ``robotsix_agent_comm`` occur — the guard short-circuits
before the deferred import, so deployments that keep the agent off
pay no import overhead and don't need the package installed.
