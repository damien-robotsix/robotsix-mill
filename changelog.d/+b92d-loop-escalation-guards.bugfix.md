Implement loop no longer wastes the entire spawn budget on no-progress
re-attempts (b92d). Review feedback is now injected into the implement
prompt on every spawn — including blocked-resume re-spawns, which
previously never saw the reviewer's corrective comments and reproduced
the same rejected diff until the spawn cap tripped. And a review
re-spawn whose previous attempt committed only changelog fragments while
review threads remain open now escalates to BLOCKED in preflight —
before the agent loop, without consuming a spawn — with the reviewer's
open gap list (not the summary tail) written into the block note.
