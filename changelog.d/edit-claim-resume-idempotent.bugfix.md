The edit-claim contradiction guard no longer blocks a resuming run whose edits
were an idempotent re-application. When every file the run claimed to edit is
already changed on the branch, the empty diff means a prior pass committed the
work — not that the work was lost — and the ticket proceeds to deliver. An edit
to a file the branch never touched still trips the guard.
