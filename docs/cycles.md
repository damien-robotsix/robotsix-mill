# Dependency Cycles — Breaking Deadlocked Tickets

When multiple BLOCKED tickets form a **circular dependency**, they create a deadlock that the blocked-auto-resume runner cannot break. This page explains what cycles are, how to identify them, and how to resolve them.

## What is a dependency cycle?

A cycle occurs when a set of BLOCKED tickets have a chain of dependencies that loops back on itself. For example:

- **Ticket A** is BLOCKED, waiting for **Ticket B** to complete
- **Ticket B** is BLOCKED, waiting for **Ticket C** to complete
- **Ticket C** is BLOCKED, waiting for **Ticket A** to complete

In this `A → B → C → A` cycle, none of the tickets can resume because each is blocked waiting for another ticket in the same cycle to finish first.

## How the runner detects cycles

The blocked-auto-resume runner scans all BLOCKED tickets on each pass and builds a dependency graph using the `depends_on` and `unblocks` fields. It applies Kosaraju's algorithm to detect strongly-connected components (SCCs) — groups of tickets with circular dependencies.

When a cycle is found:
1. **Every member of the cycle receives a `[cycle-detected]` comment** with details of the cycle path
2. **The members are marked for escalation** — they are NOT auto-resumed, because auto-resume cannot break a circular deadlock
3. **Repeated passes are silent** — the idempotency key is the `[cycle-detected]` comment itself; if a member already has one, the runner skips re-reporting the same cycle

## How to resolve a cycle

To break a cycle, you must **sever one edge** of the circular dependency or **close a member outright**.

### Option 1: Break one edge

Edit one of the BLOCKED tickets and remove one dependency edge:

- **If ticket A depends on ticket B**, edit ticket A and clear (or update) the `depends_on` field to remove the link to B.
- **If ticket B unblocks ticket A**, edit ticket B and clear (or update) the `unblocks` field to remove the link to A.

Once one edge is removed, the cycle is broken. The runner will detect the change on the next pass and stop reporting the cycle. The now-unblocked tickets can resume.

### Option 2: Close a member

If one ticket in the cycle is no longer needed or can be closed:

- **Close the ticket.** A closed ticket is no longer part of the BLOCKED set, so it cannot participate in a cycle.
- The remaining tickets may then be eligible for auto-resume or manual intervention.

## Example: two-ticket mutual block

The simplest cycle is a **two-ticket mutual block**:

- **Ticket A** says `depends_on: ["B"]`
- **Ticket B** says `depends_on: ["A"]`

To resolve:
1. Edit ticket A and remove B from `depends_on` (or edit ticket B and remove A), OR
2. Close one of the tickets if it's no longer needed.

## Debugging cycle detection

If you believe a cycle report is incorrect:

1. **Check the comment.** The `[cycle-detected]` comment lists the member IDs and the cycle path (e.g. `A → B → C`).
2. **Review the `depends_on` and `unblocks` fields** on each member ticket to confirm the circular structure.
3. **Re-run the auto-resume pass** after making any edit — the runner clears the cycle comment once the dependency is resolved.

If a cycle persists after you believe you've broken it, the dependency graph may not have been updated yet. Check that the edit was actually persisted (refresh the ticket detail view), or manually trigger a new pass.
