Implement loop no longer wastes the entire spawn budget on no-progress
re-attempts (b92d): a review re-spawn whose previous attempt committed
only changelog fragments while review threads remain open now escalates
to BLOCKED in preflight — before the agent loop, without consuming a
spawn — and the block note carries the reviewer's open gap list (not
the summary tail). Adds regression tests locking review-feedback
injection into the implement context after a review bounce.
