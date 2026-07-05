# Agent communication: research survey (Phase 1)

This document is **Phase 1 (Research)** of the epic to build a
distributed agent-to-agent (A2A) communication system. It surveys the
existing communication approaches the fleet could build on, scores each
against a fixed set of dimensions, and ends with a recommendation tuned
to this fleet's constraints. It is **research input only** — the formal,
binding choice belongs to **Phase 2 (Architecture)** and will be
recorded there as an Architecture Decision Record under
`docs/decisions/` (the planned ADR 0002). Nothing here is a committed
decision; the goal is to let Phase 2 choose deliberately rather than by
default.

The fleet constraints that bias the evaluation are:

- **Python-first.** The runtime is Python (`requires-python = ">=3.14"`
  in [`pyproject.toml`](../../pyproject.toml)); any candidate needs a mature,
  maintained Python client.
- **Stdlib-first / minimal-dependency.** The project's standing
  philosophy — captured in [`AGENT.md`](../../AGENT.md)
  ("Optimize for a small, sharp, honest codebase … no speculative
  'enterprise-grade' abstractions") and operationalised by the pin+bump
  dependency policy in [dependencies.md](../dependencies.md) — is to prefer
  the standard library and a small number of well-justified shared
  libraries over heavyweight infrastructure. This is the principle the
  prospective **ADR 0001 (programming language / dependency posture)**
  would formalise; until that ADR lands, `AGENT.md` and
  [dependencies.md](../dependencies.md) are the operative source.
- **Existing tooling.** The fleet already standardises model access,
  cost tracking, and OpenTelemetry tracing through the shared
  `robotsix-llmio` library (see [dependencies.md](../dependencies.md)). A
  transport that composes cleanly with async Python and OTel is
  preferred.
- **Deployment simplicity.** Services run as Docker Compose units
  ([docker-architecture.md](../docker-architecture.md)). A candidate that
  forces a new always-on broker or a service mesh carries real
  operational weight that must be justified.

## Evaluation criteria

Every candidate below is scored on the same five dimensions.

1. **Communication model** — synchronous vs. asynchronous;
   request–response, fire-and-forget, streaming, or publish–subscribe.
   Determines how naturally agents can interleave work while waiting.
2. **Network capability** — local-only (same host / loopback), LAN /
   distributed (trusted network), or internet-ready (must address NAT
   traversal, authentication, and TLS).
3. **Scalability & reliability** — throughput ceiling, delivery
   guarantees (at-most-once / at-least-once / exactly-once), backpressure
   handling, message durability, and the dominant failure modes.
4. **Extensibility for future agent groups** — does the model natively
   support multicast / group / broadcast delivery and *dynamic*
   membership? The epic explicitly anticipates "agent group
   functionality," so a one-to-many story that is bolted on later is a
   meaningful penalty.
5. **Fit with fleet constraints** — Python-ecosystem maturity,
   dependency and operational weight relative to the stdlib-first
   principle, and deployment simplicity.

A "✅ / ⚠️ / ❌" in the matrix means *good fit / workable with caveats /
poor fit* **for this fleet specifically** — not an absolute judgment of
the technology.

## Candidate evaluations

### MCP (Model Context Protocol)

MCP is an open, JSON-RPC 2.0–based protocol that standardises how an LLM
application (the *host/client*) connects to *servers* exposing tools,
resources, and prompts ([modelcontextprotocol.io](https://modelcontextprotocol.io/),
[specification](https://modelcontextprotocol.io/specification)). It has a
first-party Python SDK ([`modelcontextprotocol/python-sdk`](https://github.com/modelcontextprotocol/python-sdk))
and supports both a local **stdio** transport and a network-capable
**Streamable HTTP** transport (which superseded the original HTTP+SSE
transport in the 2025 spec revisions).

- **Communication model.** Client→server request–response over JSON-RPC,
  with server-initiated notifications and streaming over the HTTP
  transport. It is fundamentally a **client/server tool-access** model,
  not a symmetric peer-to-peer messaging fabric: one side consumes
  capabilities the other exposes.
- **Network capability.** stdio is local-only (subprocess pipes);
  Streamable HTTP is LAN/internet-capable and the spec defines an
  OAuth 2.x-based authorization framework for HTTP transports
  ([auth spec](https://modelcontextprotocol.io/specification)). TLS is
  whatever the HTTP layer provides.
- **Scalability & reliability.** Inherits the reliability of the
  underlying transport (TCP/HTTP). There is no built-in durable queue or
  delivery guarantee beyond the request lifecycle; a crashed peer loses
  in-flight context. Throughput is HTTP-request bound.
- **Extensibility for agent groups.** No native multicast or group
  semantics — MCP is point-to-point client↔server. Group behaviour would
  have to be layered above it.
- **Fit.** Excellent Python support and conceptual alignment with the
  fleet's existing tool-calling agents, and it is becoming the de-facto
  standard for *tool/context exchange*. But it answers "how does an agent
  call a capability," not "how do two peer agents exchange messages
  across a network," which is this epic's actual problem.

### Tool-based communication patterns

Here agents communicate by **invoking each other's exposed tools /
functions** — agent A's outbound message is literally a tool call routed
to agent B, often mediated by a shared registry or an orchestrator. This
is the pattern MCP, and most current agent frameworks, lean on.

- **Communication model.** Request–response, synchronous from the
  caller's perspective (call a tool, await a result). Asynchronous
  delivery requires modelling "send message" and "receive message" as
  separate tools plus an external mailbox.
- **Network capability.** Whatever transport carries the tool call —
  in-process (local), HTTP/MCP (LAN/internet). The pattern is
  transport-agnostic, which is both its strength and why it specifies
  nothing about NAT/auth/TLS itself.
- **Scalability & reliability.** No inherent durability or delivery
  guarantee; semantics are exactly those of the underlying call. Retries
  and idempotency are the caller's responsibility.
- **Extensibility for agent groups.** Naturally one-to-one. Broadcast
  means iterating a known recipient list; there is no dynamic-membership
  or fan-out primitive without an external bus.
- **Fit.** Low ceremony and an excellent match for the fleet's existing
  agent-as-tool-caller design — a peer agent can be exposed as "just
  another tool." It is the *cheapest* thing that works for tightly
  coupled, synchronous, request-shaped exchanges, but it pushes
  durability, fan-out, and async delivery onto whatever sits underneath.

### Message-queue / broker systems

Broker-centric systems decouple senders from receivers through a durable
intermediary, giving asynchronous pub-sub and work-queue semantics.

**RabbitMQ (AMQP 0-9-1).** A mature broker with flexible exchange types
(direct, topic, fanout, headers) enabling both work queues and pub-sub
routing ([rabbitmq.com](https://www.rabbitmq.com/),
[AMQP concepts](https://www.rabbitmq.com/tutorials/amqp-concepts)).

- *Communication model:* async pub-sub and work queues; RPC-over-queue is
  possible via reply-to/correlation-id.
- *Network capability:* LAN/internet-ready with TLS, SASL auth, and
  vhosts; requires an always-on broker process.
- *Scalability & reliability:* publisher confirms + consumer acks give
  at-least-once delivery; durable queues persist across restarts;
  per-consumer prefetch provides backpressure. Tens of thousands of
  msg/s per node; clustering/quorum queues for HA. Mature Python clients
  (`pika`, `aio-pika`).
- *Agent groups:* `fanout` and `topic` exchanges give native broadcast /
  selective multicast; bindings can be added/removed at runtime, which
  models dynamic group membership well.

**Apache Kafka.** A distributed, partitioned, **append-only log**
optimised for high-throughput durable event streaming
([kafka.apache.org](https://kafka.apache.org/documentation/)).

- *Communication model:* publish to topics; consumers pull at their own
  offset. Pub-sub with replay, not request–response.
- *Network capability:* distributed by design; TLS + SASL/ACLs; expects a
  cluster (brokers, plus KRaft/ZooKeeper for older versions).
- *Scalability & reliability:* the throughput leader (millions of
  msg/s); durable, replicated, time/size-retained logs; at-least-once by
  default with idempotent-producer + transactions for effectively
  exactly-once ([delivery semantics](https://kafka.apache.org/documentation/#semantics)).
  Backpressure via consumer-pull + partition lag.
- *Agent groups:* consumer **groups** load-balance a topic across
  members and rebalance on join/leave — strong dynamic-membership and
  fan-out story, at the cost of substantial operational weight.

**Lightweight option — NATS (with JetStream) / Redis Streams.**

- **NATS** is a single small Go binary offering subject-based pub-sub
  with wildcard subjects; the optional **JetStream** layer adds
  persistence and at-least-once delivery
  ([nats.io](https://nats.io/), [JetStream](https://docs.nats.io/nats-concepts/jetstream)).
  Core NATS is at-most-once and extremely low-overhead; subject wildcards
  and queue groups give native multicast and dynamic membership. Python
  client: `nats-py`.
- **Redis Streams** is an append-only log data type inside Redis, with
  consumer groups, acks, and pending-entry tracking
  ([redis.io streams](https://redis.io/docs/latest/develop/data-types/streams/)).
  At-least-once via `XREADGROUP`/`XACK`; durability bounded by Redis
  persistence (AOF/RDB). Attractive if Redis is already deployed; the
  `redis-py` client is mature. Fan-out across independent groups is
  native, though it is a single-node-centric store rather than a
  partitioned cluster.

Common broker trade-off for this fleet: brokers buy durability,
backpressure, and native fan-out, but every one of them adds an
**always-on stateful service** to operate, monitor, and secure —
directly in tension with the stdlib-first / deployment-simplicity
principle.

### Direct RPC / web transports

Point-to-point transports without a broker; the application owns routing,
delivery, and any fan-out.

**gRPC.** HTTP/2-based RPC with Protocol Buffers IDL and code generation;
supports unary and bidirectional streaming
([grpc.io](https://grpc.io/docs/what-is-grpc/introduction/)).

- *Model:* typed request–response **and** bidirectional streaming.
- *Network:* LAN/internet-ready; TLS and pluggable auth (mTLS, token).
  HTTP/2 needs end-to-end support (proxies/load-balancers must cooperate).
- *Scalability & reliability:* low-latency, high-throughput, efficient
  binary framing; delivery is at-most-once per RPC (app-level retries for
  more). No durability — a dropped connection drops in-flight calls.
- *Agent groups:* no native multicast; fan-out is client-side. Python
  support is mature (`grpcio`) but pulls in a sizeable native dependency
  and a codegen/`.proto` build step.

**REST / HTTP APIs.** Resource-oriented request–response over HTTP
([REST, Fielding](https://www.ics.uci.edu/~fielding/pubs/dissertation/rest_arch_style.htm)).

- *Model:* synchronous request–response; async needs polling or webhooks.
- *Network:* the most internet-ready option — universal TLS, auth, and
  proxy/NAT support; the fleet already runs FastAPI services
  ([runtime](../docker-architecture.md)).
- *Scalability & reliability:* scales horizontally behind a load
  balancer; at-most-once per request, idempotency via keys; no durability.
- *Agent groups:* none natively — fan-out is caller-driven. Lowest
  operational and dependency cost (stdlib `http.server` / existing
  FastAPI; clients via stdlib `urllib` or `httpx`).

**WebSocket.** A single long-lived, full-duplex TCP connection upgraded
from HTTP ([RFC 6455](https://datatracker.ietf.org/doc/html/rfc6455)).

- *Model:* bidirectional, asynchronous, push-capable streaming — well
  suited to live agent-to-agent exchange without polling.
- *Network:* internet-ready over `wss://` (TLS); rides HTTP ports so it
  traverses most firewalls/proxies; auth via the initial HTTP handshake.
- *Scalability & reliability:* low per-message overhead once open, but
  every connection is stateful and pinned to a server instance; no
  durability or redelivery — a dropped socket loses queued messages.
- *Agent groups:* no native multicast; the server maintains the
  connection set and fans out in application code (a small in-process
  "hub"). Python support is strong and lightweight (`websockets`, or
  FastAPI/Starlette's built-in WebSocket support over the existing stack).

### Other agent-to-agent frameworks

These are higher-level frameworks that combine a *transport* with
*orchestration* (planning, role assignment, shared state). The relevant
question is how cleanly the transport can be reused without adopting the
whole orchestration model.

- **Google's A2A (Agent2Agent) protocol.** An open protocol — donated to
  the Linux Foundation in 2025 — for interoperability *between*
  independent agents, distinct from MCP's agent↔tool framing. Agents
  publish a JSON **Agent Card** describing capabilities and communicate
  over HTTP using JSON-RPC / Server-Sent Events, with tasks, messages,
  and streaming updates ([a2a-protocol.org](https://a2a-protocol.org/),
  [google-a2a/A2A](https://github.com/google-a2a/A2A),
  [announcement](https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/)).
  It directly targets this epic's problem (peer agents, internet
  transport, auth) and has a Python SDK, but it is young and still
  stabilising. Communication is request/response + streaming; group
  semantics are not a first-class primitive; network story is HTTP-based
  and internet-oriented; reliability is transport-level (no durable
  queue).
- **AutoGen / AG2.** Microsoft's multi-agent conversation framework (the
  community fork is **AG2**) models agents that exchange messages in
  conversational patterns; v0.4+ introduced an actor-style, event-driven
  runtime with both in-process and (experimental) distributed messaging
  ([microsoft/autogen](https://github.com/microsoft/autogen),
  [AG2 docs](https://docs.ag2.ai/)). Strong async message model and a
  group-chat abstraction (native many-agent), but the distributed
  transport is heavier and less mature than its in-process mode, and
  adopting it means adopting its runtime.
- **LangGraph.** Models multi-agent systems as a **graph/state machine**
  with a shared, checkpointed state object rather than a message bus;
  "communication" is state hand-off between nodes
  ([LangGraph](https://langchain-ai.github.io/langgraph/)). Excellent for
  orchestrating one process's agents with durable checkpoints, but it is
  an orchestration framework, not a network transport for independently
  deployed agents.
- **CrewAI.** Role-based crews with task delegation; agents collaborate
  through the framework's delegation tools rather than an exposed wire
  protocol ([CrewAI docs](https://docs.crewai.com/)). Convenient for
  in-process role play, but it is opinionated orchestration with no
  standalone distributed transport to reuse.

The pattern across all four: they are strong on **orchestration** and
in-process messaging, but their **distributed-transport** stories are
either young (A2A), experimental (AutoGen distributed), or absent
(LangGraph, CrewAI as transports). A2A is the only one of the four whose
*protocol* is a plausible building block for cross-network peer agents.

## Comparison matrix

| Candidate | Communication model | Network capability | Scalability & reliability | Agent-group extensibility | Fleet fit |
|---|---|---|---|---|---|
| **MCP** | Client↔server JSON-RPC; streaming over HTTP | Local (stdio) → internet (Streamable HTTP + OAuth) | Transport-level only; no durable queue | ❌ point-to-point, no multicast | ✅ Python + tool-model alignment, but tool-access not peer-messaging |
| **Tool-based calls** | Sync request–response | Transport-agnostic (in-proc → HTTP) | Caller-owned; no guarantees | ❌ one-to-one; manual fan-out | ✅ matches existing agent design; cheapest for sync RPC |
| **RabbitMQ** | Async pub-sub + work queues | LAN/internet, TLS+SASL | At-least-once, durable, backpressure | ✅ fanout/topic + runtime bindings | ⚠️ native fan-out, but a stateful broker to operate |
| **Kafka** | Pub-sub log, replayable | Distributed cluster, TLS+ACL | Throughput leader; durable; ~exactly-once | ✅ consumer groups + rebalance | ❌ heavy ops/deps vs. stdlib-first |
| **NATS / Redis Streams** | Pub-sub (+ JetStream/streams durability) | LAN/internet | At-most/at-least-once; bounded durability | ✅ subjects/queue & consumer groups | ⚠️ lighter broker; still an always-on service |
| **gRPC** | Unary + bidi streaming | LAN/internet, mTLS | At-most-once; no durability | ❌ client-side fan-out | ⚠️ mature but native dep + `.proto` codegen |
| **REST / HTTP** | Sync request–response | Most internet-ready | At-most-once; idempotency keys | ❌ caller-driven fan-out | ✅ stdlib/FastAPI; lowest weight |
| **WebSocket** | Async full-duplex push | Internet-ready (`wss://`) | At-most-once; connection-pinned, no redelivery | ⚠️ app-level hub fan-out | ✅ lightweight on existing stack |
| **A2A protocol** | JSON-RPC + SSE tasks/streaming | HTTP, internet-oriented, auth | Transport-level; no durable queue | ⚠️ not first-class | ⚠️ on-target but young/stabilising |
| **AutoGen/AG2** | Async conversational/actor | In-proc → experimental distributed | Runtime-dependent | ✅ native group chat | ⚠️ adopt-the-runtime; distributed mode immature |
| **LangGraph** | Shared checkpointed state hand-off | In-process | Durable checkpoints (not a bus) | ⚠️ graph topology, not multicast | ⚠️ orchestration, not a transport |
| **CrewAI** | Framework delegation | In-process | Framework-owned | ⚠️ role crews | ❌ no standalone transport |

## Recommendation

**Recommended approach: a thin, layered design — a small message/envelope
protocol (JSON-RPC 2.0 shaped, the same family as MCP and A2A) carried
over an HTTP-family transport (HTTP request–response for fire-and-forget
and pull, WebSocket for live bidirectional exchange), with a deliberately
minimal in-process "hub" for group fan-out.** Concretely: model peer
agents as HTTP/WebSocket endpoints that exchange envelope-typed JSON
messages, and start *without* a broker.

Justification against the fleet constraints:

- **Stdlib-first / minimal dependency.** HTTP and WebSocket are realisable
  on the stack the fleet already runs (FastAPI/Starlette server,
  stdlib `urllib` / the existing `httpx` client, `websockets` if needed) —
  no new always-on stateful broker, consistent with the
  `AGENT.md` / [dependencies.md](../dependencies.md) posture and the future
  ADR 0001 it would formalise. A broker (Kafka especially) is rejected as
  the *default* precisely because it adds an operational service the epic
  does not yet need.
- **Python maturity.** Every layer has first-class, maintained Python
  support, and a JSON-RPC envelope keeps us protocol-compatible in spirit
  with MCP and A2A so we can interoperate later without re-plumbing.
- **Deployment simplicity.** Fits the existing Docker Compose model —
  agents are just more HTTP/WebSocket services; no broker cluster to
  provision, secure, or monitor.
- **Internet-readiness path.** The HTTP family gives the clearest
  TLS/auth/NAT story (reuse `wss://` + handshake auth), so the same design
  scales from local → LAN → internet without a transport rewrite.

**Implication for the future agent-group requirement.** This is the
recommendation's main *risk*: HTTP/WebSocket have **no native multicast**,
so group/broadcast delivery starts as an application-level hub
(maintain the membership set, iterate recipients). That is fine for small,
dynamic groups but does not give durable fan-out or backpressure. Phase 2
should therefore design the envelope and addressing so that a
**broker can be slotted under the group layer later** (see runner-ups)
without changing agent-facing APIs — i.e. keep "group address" an
abstraction, not a hard-coded recipient loop.

**Runner-up options and when they'd win:**

1. **Adopt/align with Google's A2A protocol** as the envelope instead of a
   bespoke one. Preferable if cross-vendor/cross-fleet interoperability
   becomes a goal, or if A2A stabilises and grows a strong Python SDK
   before Phase 4 — it solves the "don't reinvent the agent envelope"
   problem directly. Deferred now only because it is young and still
   moving.
2. **Introduce a lightweight broker (NATS+JetStream, or Redis Streams if
   Redis is already deployed)** under the group layer. Preferable as soon
   as the system genuinely needs **durable, backpressured, many-to-many**
   delivery — i.e. when agent groups grow beyond what an in-process hub can
   reliably fan out, or when at-least-once delivery across agent restarts
   becomes a hard requirement. NATS is the lightest broker that still
   offers native multicast and dynamic membership; Kafka only if extreme
   throughput/replay is later proven necessary.

### Open questions for Phase 2 (architecture)

- **Delivery guarantee target.** Is at-most-once (transport-level)
  acceptable for v1, or is at-least-once with redelivery a hard
  requirement? This single answer is what decides "broker or no broker."
- **Group semantics.** What exactly does an "agent group" need —
  best-effort broadcast, reliable fan-out, ordered delivery, or
  presence/membership tracking? Defines whether the in-process hub
  suffices or a broker is required from day one.
- **Envelope: bespoke vs. A2A.** Build a minimal JSON-RPC envelope now and
  optionally bridge to A2A later, or adopt A2A directly and accept its
  current churn?
- **Discovery & addressing.** How do agents find each other (static
  config, a registry service, MCP-style/Agent-Card descriptors) and how
  are addresses authenticated?
- **Security boundary.** Authn/authz model for internet exposure (mTLS,
  bearer tokens, signed envelopes) and how it reuses the fleet's existing
  secrets handling.
- **Backpressure & flow control.** With WebSocket/HTTP, how does a slow
  consumer signal a fast producer before a broker exists?
- **Tracing/cost integration.** How does the message layer propagate
  OpenTelemetry context and cost accounting through `robotsix-llmio` so
  cross-agent exchanges stay observable end-to-end?

These are inputs to the **Phase 2 architecture ADR (`docs/decisions/`,
prospective 0002)**, which will record the binding decision this survey
only informs.
