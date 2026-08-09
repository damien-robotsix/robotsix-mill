# Changelog

## [0.4.0](https://github.com/damien-robotsix/robotsix-mill/compare/v0.3.0...v0.4.0) (2026-08-09)


### Features

* Autonomous MR lifecycle: mill rebases conflicted PRs, merges mergeable PRs continuously, escalates only on validity doubts (20260808T075448Z-autonomous-mr-lifecycle-mill-rebases-con-958d) ([#2814](https://github.com/damien-robotsix/robotsix-mill/issues/2814)) ([610a594](https://github.com/damien-robotsix/robotsix-mill/commit/610a59411a43cb11a07cc6d4fd9d2d9cf15fbe76))


### Bug Fixes

* **release:** don't fail lock-sync when the release branch is gone ([#2812](https://github.com/damien-robotsix/robotsix-mill/issues/2812)) ([5db323b](https://github.com/damien-robotsix/robotsix-mill/commit/5db323b101b9fde919208bf2392bd702d0481726))

## [0.3.0](https://github.com/damien-robotsix/robotsix-mill/compare/v0.2.1...v0.3.0) (2026-08-09)


### Features

* **periodic:** cap the drafts one periodic pass run can create ([#2810](https://github.com/damien-robotsix/robotsix-mill/issues/2810)) ([e9e513c](https://github.com/damien-robotsix/robotsix-mill/commit/e9e513caeb28a4c840e2139cbcdda8ccb3b738f1))


### Bug Fixes

* **deliver:** keep the changelog kind so PR titles are not all chore ([#2806](https://github.com/damien-robotsix/robotsix-mill/issues/2806)) ([cae58bb](https://github.com/damien-robotsix/robotsix-mill/commit/cae58bb3b96c4b3c68ac579e3ced6debcad7c6fc))
* **implement:** unclaim a dropped fragment from modules.yaml ([#2809](https://github.com/damien-robotsix/robotsix-mill/issues/2809)) ([e0f996a](https://github.com/damien-robotsix/robotsix-mill/commit/e0f996a344d4bc42e6712817562ded9d4fbc8cf6))
* **periodic:** credit_balance was wired to a non-pass-shaped runner ([#2811](https://github.com/damien-robotsix/robotsix-mill/issues/2811)) ([40bfaca](https://github.com/damien-robotsix/robotsix-mill/commit/40bfaca7a6a0002f91093446e17f7980070126ae))

## [0.2.1](https://github.com/damien-robotsix/robotsix-mill/compare/v0.2.0...v0.2.1) (2026-08-09)


### Bug Fixes

* **deliver:** emit conventional-commit subjects so releases see mill's work ([#2802](https://github.com/damien-robotsix/robotsix-mill/issues/2802)) ([bdb2e33](https://github.com/damien-robotsix/robotsix-mill/commit/bdb2e33c4578fddb74996703e8e37c7483c80e8d))

## [0.2.0](https://github.com/damien-robotsix/robotsix-mill/compare/v0.1.0...v0.2.0) (2026-08-08)


### Features

* **config:** report pinned settings that shadow a changed default ([#2771](https://github.com/damien-robotsix/robotsix-mill/issues/2771)) ([74cb5e2](https://github.com/damien-robotsix/robotsix-mill/commit/74cb5e2a77b072a25b9c051075e1ac22fd81cffe))
* **merge:** let an operator send a reviewed PR back to implement ([#2652](https://github.com/damien-robotsix/robotsix-mill/issues/2652)) ([e3227eb](https://github.com/damien-robotsix/robotsix-mill/commit/e3227ebda1611d4e7658e9893ae9d2460d184dce))
* **refine:** standards gate — discard drafts whose goal violates robotsix-standards ([#2784](https://github.com/damien-robotsix/robotsix-mill/issues/2784)) ([6cbd55f](https://github.com/damien-robotsix/robotsix-mill/commit/6cbd55fb07ad8da9e9f5f9e87ec34e5d08b9b0af))
* **release:** static version and release-please ([#2793](https://github.com/damien-robotsix/robotsix-mill/issues/2793)) ([fc1fc89](https://github.com/damien-robotsix/robotsix-mill/commit/fc1fc895c2a76367d75dfbbb72f8a37b7e22f050))
* resume-blocked accepts an operator note to bypass the stale-spec guard ([#2301](https://github.com/damien-robotsix/robotsix-mill/issues/2301)) ([45ac4ea](https://github.com/damien-robotsix/robotsix-mill/commit/45ac4eaa573798c0ab5db71e5bdf529deb7b0bd7))
* **sandbox:** cap each sandbox's CPU with sandbox_cpus ([#2719](https://github.com/damien-robotsix/robotsix-mill/issues/2719)) ([e984f9b](https://github.com/damien-robotsix/robotsix-mill/commit/e984f9b7d494ed666a5142d1abdde968ad96116d))


### Bug Fixes

* **agents:** use llmio's task_budget clamp instead of mill's own workarounds ([#2728](https://github.com/damien-robotsix/robotsix-mill/issues/2728)) ([482ec2d](https://github.com/damien-robotsix/robotsix-mill/commit/482ec2deed2b030d29ab4c1408c74fb4aaa052eb))
* **agents:** write changelog fragments instead of editing CHANGELOG.md ([#2769](https://github.com/damien-robotsix/robotsix-mill/issues/2769)) ([0620a6f](https://github.com/damien-robotsix/robotsix-mill/commit/0620a6f33ae31d230f92d8dc5b6259d361c443b2))
* await async tools in implement progress wrapper ([#2394](https://github.com/damien-robotsix/robotsix-mill/issues/2394)) ([a173450](https://github.com/damien-robotsix/robotsix-mill/commit/a1734501b0e9e236c6c5e34cb085c7b4c8e89a49))
* **board:** remove the column-move control and its 500ing route ([#2724](https://github.com/damien-robotsix/robotsix-mill/issues/2724)) ([0d53526](https://github.com/damien-robotsix/robotsix-mill/commit/0d53526688afa5e4b4baf22587d74c555571f30d))
* **changelog:** map a kind onto the repo's own towncrier spelling ([#2776](https://github.com/damien-robotsix/robotsix-mill/issues/2776)) ([2c0743f](https://github.com/damien-robotsix/robotsix-mill/commit/2c0743f4b10a871a45786e967b24cf2d07b6e93b))
* **changelog:** skip validation on release-please repos ([#2791](https://github.com/damien-robotsix/robotsix-mill/issues/2791)) ([46a5613](https://github.com/damien-robotsix/robotsix-mill/commit/46a5613916b6608a5da3f62d5653ae99bd4bb5d4))
* **ci_fix:** bound the empty-commit CI refresh so real failures reach the fix agent ([#2723](https://github.com/damien-robotsix/robotsix-mill/issues/2723)) ([92de273](https://github.com/damien-robotsix/robotsix-mill/commit/92de27344fb52938b717404168bb8fb45817a3d1))
* **ci_fix:** stop the stage killing its own agent mid-verify-loop ([#2737](https://github.com/damien-robotsix/robotsix-mill/issues/2737)) ([379a0d4](https://github.com/damien-robotsix/robotsix-mill/commit/379a0d499896af2de318be48d665545ccc54b6cb))
* **config:** restore JSON config-file sourcing on Settings ([#2717](https://github.com/damien-robotsix/robotsix-mill/issues/2717)) ([a117a7a](https://github.com/damien-robotsix/robotsix-mill/commit/a117a7a8d19e17e44e453ca123d384001206e62d))
* **config:** restore secrets sourcing + surface missing creds on the board ([#2720](https://github.com/damien-robotsix/robotsix-mill/issues/2720)) ([fcae936](https://github.com/damien-robotsix/robotsix-mill/commit/fcae936b936646026e2d010aa0a670d0dea573dc))
* **deps:** bump cryptography to 50.0.0 to clear main's audit failure ([#2760](https://github.com/damien-robotsix/robotsix-mill/issues/2760)) ([c87e2ee](https://github.com/damien-robotsix/robotsix-mill/commit/c87e2ee1503ac35d88e4cf37467bddfcb9977796))
* **deps:** bump mcp 1.27.2 -&gt; 1.28.1 (CVE-2026-59950) ([#2396](https://github.com/damien-robotsix/robotsix-mill/issues/2396)) ([d60bc2a](https://github.com/damien-robotsix/robotsix-mill/commit/d60bc2abcf0e8c5298381f909ccb77306ad830b3))
* **implement:** changelog-only review re-spawn short-circuits to BLOCKED in preflight (b92d, v5 — supersedes [#2642](https://github.com/damien-robotsix/robotsix-mill/issues/2642)) ([#2645](https://github.com/damien-robotsix/robotsix-mill/issues/2645)) ([cf8cb95](https://github.com/damien-robotsix/robotsix-mill/commit/cf8cb95d25714b48b7376ee6fe652767f33670e3))
* **implement:** don't block a resume whose edits already landed ([#2766](https://github.com/damien-robotsix/robotsix-mill/issues/2766)) ([3fdf4fe](https://github.com/damien-robotsix/robotsix-mill/commit/3fdf4fe38f313c9ef031440743694ed533342316))
* **implement:** don't take an LLM-free bypass when a reviewer asked for changes ([#2664](https://github.com/damien-robotsix/robotsix-mill/issues/2664)) ([59abf50](https://github.com/damien-robotsix/robotsix-mill/commit/59abf5011ef3ac2fd96594c212ec623bbb6e01d9))
* **implement:** flood guard counts newly added files, not every file ([#2770](https://github.com/damien-robotsix/robotsix-mill/issues/2770)) ([e7844d4](https://github.com/damien-robotsix/robotsix-mill/commit/e7844d4954fe235d986ce2610a73adbbff3f311f))
* **implement:** preserve uncommitted work on resume + loosen large-file loop-guard ([#2596](https://github.com/damien-robotsix/robotsix-mill/issues/2596)) ([c92cf2a](https://github.com/damien-robotsix/robotsix-mill/commit/c92cf2a8eb949dc29d3328db3570f0d521364ee5))
* **implement:** refund the spawn budget when a ticket makes progress ([#2637](https://github.com/damien-robotsix/robotsix-mill/issues/2637)) ([8d1dab2](https://github.com/damien-robotsix/robotsix-mill/commit/8d1dab2f093422ca5156594821ab2ab1afbbc94d))
* **implement:** resume guard routes to DELIVERABLE so the PR actually opens ([#2599](https://github.com/damien-robotsix/robotsix-mill/issues/2599)) ([2a04e3a](https://github.com/damien-robotsix/robotsix-mill/commit/2a04e3ad216fdf1736efa1047fe95f563f4c3baf))
* **limits:** disable per-ticket budget caps and drop agent max_tokens ([#2764](https://github.com/damien-robotsix/robotsix-mill/issues/2764)) ([59b9ac4](https://github.com/damien-robotsix/robotsix-mill/commit/59b9ac493e50c7b634ecbae952f145351152eaab))
* loop guard refused every command once a file was grepped 3 times ([#2377](https://github.com/damien-robotsix/robotsix-mill/issues/2377)) ([2183b7b](https://github.com/damien-robotsix/robotsix-mill/commit/2183b7bd6c51f9423e58ea74270bb40e0b96ee76))
* **merge:** bound the CI refresh to one empty commit per branch head ([#2726](https://github.com/damien-robotsix/robotsix-mill/issues/2726)) ([11b599a](https://github.com/damien-robotsix/robotsix-mill/commit/11b599a7bcf33b18cc1c05152a113c380515c3f4))
* **merge:** don't restart CI on every poll while checks are still running ([#2700](https://github.com/damien-robotsix/robotsix-mill/issues/2700)) ([c175f45](https://github.com/damien-robotsix/robotsix-mill/commit/c175f45f1a4e4d26b262c0e907ad349728445e30))
* **merge:** retry a merge rejected by a not-yet-reported required check ([#2729](https://github.com/damien-robotsix/robotsix-mill/issues/2729)) ([796b2bc](https://github.com/damien-robotsix/robotsix-mill/commit/796b2bc42321f00a7a6c52d4ef41c366fc522549))
* **merge:** stop the rebase drop guard blocking on registry files ([#2765](https://github.com/damien-robotsix/robotsix-mill/issues/2765)) ([8758194](https://github.com/damien-robotsix/robotsix-mill/commit/87581949b506f3e446383e23598f432a4fe0b85b))
* **modules:** drop changelog.d reintroduced by in-flight PRs ([#2799](https://github.com/damien-robotsix/robotsix-mill/issues/2799)) ([0caf552](https://github.com/damien-robotsix/robotsix-mill/commit/0caf552fd1e798bd5c3c7b0154c7eb0156345dff))
* **pass_runner:** stop the rollup source set from shadowing the scanner set ([#2715](https://github.com/damien-robotsix/robotsix-mill/issues/2715)) ([479b221](https://github.com/damien-robotsix/robotsix-mill/commit/479b221fa7d6f91bd17a3bc09ec46d83ba8729c5))
* **refine:** fall back to the draft instead of hard-blocking on a degenerate spec ([#2661](https://github.com/damien-robotsix/robotsix-mill/issues/2661)) ([de25137](https://github.com/damien-robotsix/robotsix-mill/commit/de25137f9407330abe0ddf3dbf5296f46db8f315))
* **refine:** prior-SKIP replay must not carry the old routing verdict ([#2650](https://github.com/damien-robotsix/robotsix-mill/issues/2650)) ([4b26172](https://github.com/damien-robotsix/robotsix-mill/commit/4b26172b9004f66cd5ec41d3afade6af0aa9c93c))
* **refine:** raise max_tokens to the task_budget floor; don't swallow API 400s ([#2727](https://github.com/damien-robotsix/robotsix-mill/issues/2727)) ([0e7b036](https://github.com/damien-robotsix/robotsix-mill/commit/0e7b0368edfa68c81281da34725fff3b99c46b0d))
* **refine:** suspicion patterns must defer to the classifier, not reject ([#2647](https://github.com/damien-robotsix/robotsix-mill/issues/2647)) ([229df9b](https://github.com/damien-robotsix/robotsix-mill/commit/229df9bb3a3701c7d904941c26b2fd79056ed9b2))
* **release:** mint an App token so release PRs get CI ([#2795](https://github.com/damien-robotsix/robotsix-mill/issues/2795)) ([1be9e06](https://github.com/damien-robotsix/robotsix-mill/commit/1be9e0688086f7681437e3060abac361497c5e6c))
* **release:** regenerate uv.lock on the release branch ([#2798](https://github.com/damien-robotsix/robotsix-mill/issues/2798)) ([d5d2f2a](https://github.com/damien-robotsix/robotsix-mill/commit/d5d2f2a9885ea10fe167cb3b3ade48359f6af9d7))
* **retrospect:** reject a draft whose body is a placeholder ([#2677](https://github.com/damien-robotsix/robotsix-mill/issues/2677)) ([bd30be7](https://github.com/damien-robotsix/robotsix-mill/commit/bd30be72d3b449dd0b40aaac980fcc5b52ce3e74))
* route green-CI behind-target PRs to REBASING instead of polling forever ([#2440](https://github.com/damien-robotsix/robotsix-mill/issues/2440)) ([398ac49](https://github.com/damien-robotsix/robotsix-mill/commit/398ac499bb06ce6ad3976379d80d2814fd7d37ac))
* **sandbox:** admit sandbox slots by priority instead of arrival order ([#2725](https://github.com/damien-robotsix/robotsix-mill/issues/2725)) ([3bb0db3](https://github.com/damien-robotsix/robotsix-mill/commit/3bb0db3465bc706b8cef4a5175caa25ab64f653b))
* **sandbox:** keep the uv/pip cache out of the RAM-backed /tmp ([#2714](https://github.com/damien-robotsix/robotsix-mill/issues/2714)) ([fed03fc](https://github.com/damien-robotsix/robotsix-mill/commit/fed03fcdef55ab8d207503ddad90a77afbfaadf7))
* **sandbox:** make max_global_concurrency a hard ceiling on live sandboxes ([#2713](https://github.com/damien-robotsix/robotsix-mill/issues/2713)) ([e8fd94b](https://github.com/damien-robotsix/robotsix-mill/commit/e8fd94bf7d972a6c4a29a5ed718c02df43369e0e))
* **sandbox:** re-attach egress proxy before every sandbox spawn ([#2282](https://github.com/damien-robotsix/robotsix-mill/issues/2282)) ([956eb3e](https://github.com/damien-robotsix/robotsix-mill/commit/956eb3ec476290eaf7b453950343fd4f4225d2ce))
* **stage-cache:** never store or replay a BLOCKED outcome ([#2666](https://github.com/damien-robotsix/robotsix-mill/issues/2666)) ([c3a4c5b](https://github.com/damien-robotsix/robotsix-mill/commit/c3a4c5bd1250689b9a1a73c71fbf032351384966))
* stop _check_progress from blocking the event loop on every ticket ([#2299](https://github.com/damien-robotsix/robotsix-mill/issues/2299)) ([e8d51a9](https://github.com/damien-robotsix/robotsix-mill/commit/e8d51a99b5c159ff06a9b16c5f71923f5a94a042))
* stop /health and /chat-skill from starving behind the agent thread pool ([#2297](https://github.com/damien-robotsix/robotsix-mill/issues/2297)) ([56cab17](https://github.com/damien-robotsix/robotsix-mill/commit/56cab17291ffae4d841ec05300b9d307cc495046))
* stop a full data volume from mass-blocking the board ([#2759](https://github.com/damien-robotsix/robotsix-mill/issues/2759)) ([bd1a9be](https://github.com/damien-robotsix/robotsix-mill/commit/bd1a9be29d23156425d25105452368bf8bba143c))
* stop periodic supervisor loops from blocking the event loop ([#2298](https://github.com/damien-robotsix/robotsix-mill/issues/2298)) ([be31d3f](https://github.com/damien-robotsix/robotsix-mill/commit/be31d3fe7dd4bac60797f31d7a309e56d8df96c1))
* **tests:** drop unused unittest.mock.patch import ([#2722](https://github.com/damien-robotsix/robotsix-mill/issues/2722)) ([3e59f97](https://github.com/damien-robotsix/robotsix-mill/commit/3e59f97b02daa3eee4ffda577e44cfad42c4970c))
* **tests:** langfuse helpers stopped enabling tracing after the config cutover ([#2718](https://github.com/damien-robotsix/robotsix-mill/issues/2718)) ([3496711](https://github.com/damien-robotsix/robotsix-mill/commit/3496711dfc1f09db7c76747fe90f50747aa8bbaa))
* three faults that dead-end tickets — retired-state rows, unbounded git, false rebase-drop ([#2632](https://github.com/damien-robotsix/robotsix-mill/issues/2632)) ([ccc09fd](https://github.com/damien-robotsix/robotsix-mill/commit/ccc09fdf082b132d596ab56eb8a75bf27aec49ea))
* treat SQLite 'database is locked' as transient and raise the busy timeout ([#2782](https://github.com/damien-robotsix/robotsix-mill/issues/2782)) ([10f1790](https://github.com/damien-robotsix/robotsix-mill/commit/10f17904f35fd757843f79cbddf0faa9c3ef45f0))
* two ways a ticket parks forever with no path forward ([#2646](https://github.com/damien-robotsix/robotsix-mill/issues/2646)) ([144eb4a](https://github.com/damien-robotsix/robotsix-mill/commit/144eb4a022dc8123a4053b154cfb6d68f82aaa56))
* unblock the merge pipeline — wire auto_merge_enabled + break the ci_fix deadlock ([#2629](https://github.com/damien-robotsix/robotsix-mill/issues/2629)) ([05bb516](https://github.com/damien-robotsix/robotsix-mill/commit/05bb516342d18562d2cc460ee2c89a037e8d76e5))
* **vcs:** stop reporting mill's own bots as a foreign human push ([#2663](https://github.com/damien-robotsix/robotsix-mill/issues/2663)) ([fc57318](https://github.com/damien-robotsix/robotsix-mill/commit/fc5731887c4afdab907b67964566e82b048626a1))
* **worker:** check the sandbox overlay filesystem in the disk gate ([#2768](https://github.com/damien-robotsix/robotsix-mill/issues/2768)) ([38354c2](https://github.com/damien-robotsix/robotsix-mill/commit/38354c2eb9085647518efdffcf95c92abbb8146d))
* **worker:** rank the global concurrency slot so priority works fleet-wide ([#2721](https://github.com/damien-robotsix/robotsix-mill/issues/2721)) ([69fb305](https://github.com/damien-robotsix/robotsix-mill/commit/69fb305981e1533a6d441693ccee0ef8efd41974))

## 0.1.0 (2026-08-08)

### Features

- Added a daily config pin-drift check. `config/config.json` pins ~288 settings and
  a pin always beats the model default, so changing a `Field(default=…)` is a no-op
  in production until someone edits the pin too. That silently reverted the move to
  weekly periodics — twelve generators ran daily for weeks at roughly 7× the
  intended ticket volume — and a change disabling the per-ticket spend caps. Both
  were found by hand, long after. The pass reports only drift not listed in
  `config_pin_drift_baseline`, so deliberate operator choices stay quiet. (config-pin-drift-check)
- Add a refine-time standards gate: for repos that follow robotsix-standards (auto-detected from the `robotsix-` id prefix, overridable per repo via `follows_standards` in repos.yaml), a single cheap LLM call discards agent-spawned drafts whose goal violates an explicit standards prohibition (e.g. "publish to npm" vs distribution-packaging.md's no-registry rule) before any refine budget is spent. The fetched standards context now also includes distribution-packaging.md and free-tier-only.md so both the gate and the refine agent see the fleet's prohibition rules. (refine-standards-gate)
- New `sandbox_cpus` setting caps each sandbox container's CPU, in cores
  (`MILL_SANDBOX_CPUS`, `0` = unlimited, the previous behaviour). Sandboxes
  already capped memory and PIDs but nothing bounded CPU, so
  `max_global_concurrency` bounded the sandbox *count* while host load stayed
  unbounded — N sandboxes could take N cores. Setting a quota makes the two
  proportional, which is what allows raising the concurrency cap on a small host. (sandbox-cpu-quota)
- Add outbound event mechanism that fires HTTP POSTs to configured subscriber URLs on every ticket state transition. Events carry ``{ticket_id, board_id, old_state, new_state, timestamp}``. Delivery is best-effort and asynchronous — subscriber downtime does not block or slow down ticket transitions. Configured via ``subscriber_urls`` and ``subscriber_shared_secret`` settings. (20260807T133210Z-emit-outbound-ticket-state-change-events-b31b)
- Auto-rebase PRs that become conflicted due to sibling merges: when a PR in human_mr_approval or waiting_auto_merge is detected as conflicted, the merge stage now tries the server-side update-branch API first — resolving base-moved-forward conflicts cheaply without invoking the rebase agent. Genuine content conflicts still fall through to the rebase agent as before. (20260807T210552Z-auto-rebase-prs-that-fall-into-merge-con-2f27)
- Add ``module_size`` periodic agent: scans source and test files for
  excessive line counts and proposes concrete split tickets for the
  highest-priority offenders. (#0)
- Added `POST /tickets/{id}/request-implementation-changes`, letting an operator reviewing an open PR send the ticket back to the implement stage for rework. Previously the only send-back path was `request-changes`, which applies to `human_issue_approval` and re-refines the spec from `draft` — discarding the implementation — so an operator who simply wanted the code adjusted had no way to ask for it. The ticket returns to `ready` and implement re-runs against the same spec. The request body is required and is recorded as a comment, which is the channel the implement stage reads operator feedback from; the stale-spec fingerprint guard and the implement spawn counter are cleared so an operator request is never refused for an unchanged spec or an exhausted retry budget. Documented in the chat skill so robotsix-chat can offer it alongside `merge-now` as the two outcomes of a PR review.

### Bugfixes

- `ci_fix` no longer livelocks on a genuinely failing CI. When the rebase was a no-op the stage pushed an empty commit to force a fresh CI run — reasonable for a stale or flaky run — and then read the check status about two seconds later. The run it had just triggered was necessarily still `queued`, so the status came back `pending` and the stage returned to `implement_complete` without ever reaching the fix path below it. Every subsequent entry into `fixing_ci` did the same thing, so a real failure was refreshed forever and never diagnosed: on `robotsix-auto-mail` ticket `…-6a4e` this cycled for hours (each pass returning in under 10 seconds) while the fix agent ran zero times. The refresh is now bounded — one per failure, reset on genuine forward progress — and the stage returns immediately after pushing rather than reading a status it just invalidated. Once the budget is spent it falls through to the real status, so a failure that survives a fresh run reaches the fix agent. (bound-ci-fix-empty-commit-refresh)
- The merge stage's CI refresh now pushes at most one empty commit per branch head, and skips the empty commit entirely when the rebase it just did already pushed. Previously every entry into the `implement_complete` poll that found a concluded CI run pushed a fresh no-op commit, and a ticket bouncing `implement_complete → fixing_ci → implement_complete` re-entered that poll on each transition: observed on `robotsix-http` ticket `…-d320`, three empty commits in 22 seconds from two different call sites, each one abandoning the CI run the previous had triggered. The in-flight guard added earlier stops the refresh while checks are running, but not this case, where each run concludes before the next poll. A sentinel now records the head SHA produced by the last refresh, so the next poll is a no-op until something real moves the branch on — self-resetting, so a landed fix still gets its own refresh budget. (bound-merge-poll-ci-refresh)
- The implement agent now records changelog entries as towncrier fragments
  (`add_changelog_fragment`) instead of inserting bullets into `CHANGELOG.md`
  (`insert_changelog_entry`). Every ticket previously wrote to the same spot under
  `## 0.0.0 (unreleased)`, so any two open PRs conflicted pairwise — a
  combinatorial problem no `gh pr update-branch` could resolve. Fragments are one
  file per ticket, which is what the fleet standard requires and what makes
  parallel PRs conflict-free. The fragment directory and valid types are read from
  each repo's own `[tool.towncrier]`, since the fleet is not uniform. (changelog-fragment-tool)
- ci_fix: the stage no longer kills its own agent mid-verify-loop. The ci-fix agent owns a fix→push→``wait_for_ci``→verify loop budgeted at ``ci_fix_max_iterations × ci_fix_wait_timeout_s``, but the stage wrapper fell back to the generic ``stage_timeout_seconds`` (2400 s) because ``ci_fix`` had no override — with the values pinned in production that killed the agent at 26% of its sanctioned budget and discarded fixes it had already pushed. ``ci_fix`` accounted for 25 of the 31 stage timeouts ever recorded. The stage ceiling is now *derived* from the agent's budget so the two cannot drift apart, a ci_fix deadline is retried as a transient stall instead of hard-blocking the ticket (matching ``implement``), and the shipped loop defaults are resized to observed run times (3 × 900 s; sampled successful runs finished in 300-900 s on a single iteration). (ci-fix-stage-budget)
- Classify SQLite "database is locked" errors on the mill's own per-board DB as transient so lock contention gets a stage retry with backoff instead of escalating the ticket to BLOCKED, and raise the SQLite busy timeout from 5s to 30s so write bursts across the worker thread pool rarely hit the lock error at all. (db-locked-is-transient)
- The disk gate now checks the container root alongside `data_dir`, via the new
  `disk_check_extra_paths` setting. Sandbox containers write package installs to
  the Docker overlay, which lives on a different device from the workspace
  volume — so a rebase could fail three times with ENOSPC on every command while
  the gate, looking only at the data volume, saw 146 GB free and waved the ticket
  straight back in. Park notes now name the filesystem that is actually full. (disk-gate-checks-overlay-fs)
- Turn off the per-ticket runaway budgets (`max_spend_usd_per_ticket`,
  `max_traces_per_ticket`, `max_openrouter_marginal_usd_per_ticket`) and remove
  `max_tokens` from every agent definition.

  A per-ticket budget is the wrong unit for guarding against a model that
  consumes erratically: that is a property of the model, not of whichever ticket
  happened to be running, so the cap punishes the unlucky ticket while the real
  problem continues on the next one. Measured against real fleet behaviour these
  fired on ordinary long work rather than on runaways — on 2026-08-06 the trace
  cap alone had 20 tickets BLOCKED at $0.00 of recorded OpenRouter spend.

  Agent `max_tokens` could not be honoured at all on the Claude SDK transport:
  it was forwarded as an advisory `task_budget` that capped nothing and instead
  told the model it had a small allowance for the whole task. Also bumps the
  llmio pin to pick up the transport-side fix.

  The mechanism is retained, not deleted — set any cap non-zero to re-arm it.
  `max_turns` and the per-stage wall-clock timeout remain the real backstops. (drop-per-ticket-budget-caps)
- The edit-claim contradiction guard no longer blocks a resuming run whose edits
  were an idempotent re-application. When every file the run claimed to edit is
  already changed on the branch, the empty diff means a prior pass committed the
  work — not that the work was lost — and the ticket proceeds to deliver. An edit
  to a file the branch never touched still trips the guard. (edit-claim-resume-idempotent)
- Fixed stale `files:` trigger paths in the `validate-config-sync` and `check-builtin-kinds` pre-commit hooks so they fire locally on relevant file edits. (fix-stale-pre-commit-files-triggers)
- The scope-triage flood guard now counts only files the branch newly introduced,
  not every out-of-scope file. A build-artifact flood is thousands of new paths; a
  cross-cutting refactor is edits to files that already existed. Counting both
  alike blocked exactly the changes that are most tedious by hand — a
  default-account removal touching 79 files and a mypy-gate promotion touching 71,
  both correctly scoped and neither containing an artifact. The prompt-overflow
  protection the cap also provided moves to a separate, far higher
  `scope_triage_hard_max_files` ceiling. (flood-guard-newly-added)
- Data-dir GC now reclaims the ``.venv`` inside workspaces of PARKED
  tickets — ``BLOCKED``, ``HUMAN_MR_APPROVAL`` and ``HUMAN_ISSUE_APPROVAL``.
  Previously only terminal states (``CLOSED`` / ``EPIC_CLOSED`` /
  ``ANSWERED``) were reclaimable, so a blocked ticket pinned its whole
  dependency tree indefinitely. Measured on the deploy box 2026-08-06: 157
  parked workspaces holding 45 GB of ``.venv``, 34 GB of it under
  ``BLOCKED`` — on the very volume whose exhaustion had blocked 146 of
  those tickets, a loop that could not drain on its own. Only ``.venv`` is
  removed; the clone, its git history and any uncommitted work stay
  inspectable for the human the ticket is parked for, and ``uv sync``
  rebuilds the venv on resume from the shared package cache. Guarded by a
  1-hour park-age threshold and a live-sandbox check so a ticket resumed
  mid-pass never loses its venv under a running sync. Knobs:
  ``data_dir_gc_prune_parked_venvs`` (default on) and
  ``data_dir_gc_prune_parked_venvs_age_seconds``. (gc-parked-ticket-venvs)
- Restored the Langfuse test helpers' tracing setup. The config-standard cutover
  moved the Langfuse credentials onto `Settings` itself, but the helpers in
  `tests/langfuse/` still populated only the `Secrets` singleton — so
  `Settings.tracing_enabled` was False, every runner short-circuited, and 17
  tests asserted against empty results. They now set the `Settings` fields too. (langfuse-test-tracing-creds)
- A full data volume no longer mass-blocks the board. ENOSPC now
  classifies as transient, and — mirroring the existing network-outage
  parking — a stage that fails on a genuinely full volume PARKS without
  consuming a retry attempt, re-polling every
  ``disk_full_retry_seconds`` until space returns. Previously ENOSPC was
  fatal, so one full volume became one hand-resumable BLOCKED ticket per
  affected ticket: 146 of them on 2026-08-06, whose retained workspaces
  then pinned the disk they were waiting on. A new pre-dispatch admission
  gate (``disk_min_free_mb``, default 5 GB) parks tickets *before* a stage
  starts, so a stage can no longer die partway through and leave a
  half-written workspace consuming the space it just ran out of. Disk
  exhaustion arriving as subprocess stderr is detected too, so a
  ``git clone`` that died on a full disk is no longer reported as an
  opaque ``Fatal: CalledProcessError``. (park-tickets-on-full-disk)
- The global concurrency cap now admits waiting tickets by rank instead of arrival order, so the priority flag works fleet-wide rather than only within one board. Each repo's queue already sorted its own tickets `(priority, then closest-to-CLOSED, then FIFO)`, but every board then contended for the same `max_global_concurrency` slots through a plain `asyncio.Semaphore` — which hands slots out strictly FIFO and cannot see what its waiters are carrying. In production (26 consumer tasks across 21 boards, cap 3) a flagged ticket won its own queue instantly and then lost the global slot to whichever other repo's unflagged ticket happened to reach the semaphore first, so operators flagged a ticket and watched unflagged work start ahead of it. The new `PriorityGate` keeps the same cap but orders its waiters by the `(priority_rank, stage_rank)` tuple the per-board queues already use, breaking ties FIFO so nothing starves within a rank. (priority-aware-global-slot)
- The sandbox concurrency ceiling now admits waiting work by priority instead of arrival order, closing the last priority-blind gate on the path from "operator flags a ticket" to "that ticket runs". The ceiling deliberately lives where sandboxes are created, so it is shared between ticket stages — which carry the operator's flag — and the ~20 per-repo periodic passes (audit, test-gap, survey, …), which carry nothing. Because it was a plain `BoundedSemaphore`, a `test_gap_workspace` pass could take the last of the three slots ahead of a flagged ticket that had already won both its board queue and the global stage gate. The pool now orders its waiters by the same `(priority_rank, stage_rank)` tuple used elsewhere, breaking ties FIFO, with the same bounded wait and the same `SandboxError` on timeout. The board consumer publishes each ticket's rank through a context variable, which `asyncio.to_thread` carries into the worker thread, so no stage signature changes; passes that never set it keep the unflagged default. (priority-aware-sandbox-slots)
- Bumped `pypdf` to 6.15.0, closing GHSA-fp3f-mc75-235c and GHSA-fwg2-594c-jp42
  (both resource exhaustion). The release landed inside the rolling `exclude-newer`
  window, so it needed a per-package override — the same shape as the cryptography
  override above it. (pypdf-advisories)
- The post-rebase drop guard no longer blocks tickets over registry and
  boilerplate files. `docs/modules.yaml` and `site/modules.yaml` join the
  changelog paths already exempt, and the list is now the
  `rebase_drop_exempt_paths` setting rather than a hardcoded tuple. These files
  are a function of the whole repo and are re-derived by CI, so a rebase settles
  them on a version matching neither the branch nor the target — a case the
  blob-equality excuse cannot clear, which reported healthy reconciliations as
  silent drops. (rebase-drop-exempt-registry)
- Unblock the refine agent, which was failing every call, and stop a permanent API error from being swallowed as a successful empty run.

  On the Claude SDK transport an agent's `max_tokens` becomes `task_budget.total`, which the API rejects below 20,000 tokens. `refine.yaml` set `8192`, so with `refine_subscription_tier_routing_enabled` on, every refine call was rejected with `400 \`task_budget.total\` must be at least 20,000 tokens for this model`. Raised to `20000` — a hard floor, not a preference.

  The failure was invisible because the SDK collapses the 400 into its degenerate-`success` frame, which `_is_claude_sdk_degenerate_result` matched through the cause chain — so the refine runner logged "treating as successful run with no changes" and returned an empty result. An API `400` now takes precedence over that signature in both `_is_claude_sdk_degenerate_result` and `is_transient`, so it fails loudly instead of silently no-opping (and is never retried — the retry re-sends an identically invalid request). Matched on the message so the guard holds regardless of the installed `robotsix_llmio` version; scoped to 400, since 429 and 5xx stay retryable. (refine-task-budget-floor)
- `Settings()` reads `config/config.json` again. The clean-cutover to the
  robotsix config-standard (#2525) removed the model's JSON source and replaced
  it with a `load_settings()` helper that was never wired to a caller — and
  nothing else reads the file, so every one of the several hundred bare
  `Settings()` constructions across the codebase silently fell back to model
  defaults. The commit had not been deployed yet; the next deploy would have
  reverted mill's entire runtime configuration, `MILL_MAX_GLOBAL_CONCURRENCY`
  included. The file source is restored below `os.environ`, and
  `tests/config/test_config.py` now pins the precedence. (restore-json-config-sourcing)
- Sandbox `uv`/`pip` caches now live on a shared disk-backed volume subpath
  instead of the sandbox's `/tmp`. `HOME=/tmp` is a tmpfs — RAM charged to the
  container's own memory limit — so ever since the test gate began installing the
  project, each sandbox spent its memory budget caching the dependency tree
  (measured: 625 MB of `/tmp/.cache` in one live sandbox, another pinned at
  1022 MiB against a 1 GiB cap). The tmpfs is also size-bounded now, so an
  overflow fails with `ENOSPC` rather than an unexplained OOM kill, and the
  sandbox-reaper pass drops the shared cache once it exceeds its budget. (sandbox-cache-off-tmpfs)
- `max_global_concurrency` is now a hard ceiling on live `mill-sbx-*` sandbox
  containers, not just on board-consumer stages. It was applied only around
  `process_ticket`, so the ~20 per-repo periodic passes, the meta-agent, the
  diagnostic pass and refine's warnings collection all spawned sandboxes outside
  it — with the cap set to 1, three sandboxes ran at once. The limit now lives in
  `sandbox.run()` itself, where the containers are actually created. (sandbox-hard-ceiling)
- Scanner findings are grouped under a rollup epic again for all 19 scanner
  sources, not 5. #2672 (epic parents for scanner findings) and #2667 (collapse N
  findings into one ticket) landed as concurrent PRs and both named their source
  set `_SCANNER_SOURCES`, so the second definition silently shadowed the first —
  `AUDIT`, `TRACE_HEALTH` and 12 others stopped getting an epic parent. The
  narrower set is now `_ROLLUP_SOURCES`. This also clears the `no-redef` mypy
  violation that was failing CI on `main` and therefore on every open PR. (scanner-sources-name-collision)
- Delegate the Claude SDK `task_budget` floor and API-400 classification to `robotsix_llmio` instead of reimplementing them locally.

  `robotsix-llmio` is bumped to `c65df6b`, which adds `build_task_budget()` (clamps a below-floor `max_tokens` up to the API's 20,000-token `task_budget` minimum) and `ClaudeSDKPermanentAPIError` (an API 400, excluded from the transient set). With those in place:

  - `refine.yaml` returns to its intended `max_tokens: 8192`. The previous bump to `20000` was mill working around the floor itself, which also silently loosened the cap on the OpenRouter path, where `max_tokens` is a real per-response limit rather than an advisory budget. llmio now clamps only the Claude SDK value, warning once.
  - `agents/retry.py` drops its local `_is_permanent_api_error` message matcher and its `_chain_contains` helper in favour of llmio's `is_claude_sdk_permanent_api_error`. `is_transient` no longer needs an explicit guard either — `is_claude_sdk_transient` already excludes a 400 ahead of the degenerate-`success` signature. `_is_claude_sdk_degenerate_result` still defers to the library predicate, since the refine runner consults it directly when deciding whether to swallow a failure as an empty result.

  Adds a regression test asserting that **every** agent definition's `max_tokens` yields an API-valid `task_budget`, so a future below-floor value can't take a stage offline again. This also covers `retrospect` (16384) and `periodic/completeness_check` (8192), which were previously safe only because they don't route to the Claude SDK.

  (use-llmio-task-budget-clamp)
- Wire per-repo `auto_merge_enabled` from `repos.yaml`/`repos.json` through `load_repos_config()` to `RepoConfig`. Previously the field was declared and documented but never passed from the config data, so per-repo opt-in was silently ignored. (wire-auto-merge-enabled)
- `changelog_fragment` now maps a requested kind onto the repo's own towncrier spelling instead of rejecting it. robotsix-llmio names its types `feat`/`fix` where every other repo uses `feature`/`bugfix`, so an agent choosing the majority spelling hit a hard error and the ticket blocked. A kind with no configured equivalent still raises, because towncrier silently skips a fragment with an unconfigured extension. (20260808T040000Z-changelog-kind-aliases)
- Fix `GET /tickets?created_after=...` filter: naive ISO-8601 datetimes (e.g. `2020-01-01T00:00:00`) are now treated as UTC instead of raising a `TypeError` inside the ORM layer. The list endpoint also now skips individual tickets whose enrichment fails (e.g. a row with NULL `created_at`) instead of 500-ing the whole board — the bad row is logged and the remaining tickets are returned. (20260808T072512Z-get-tickets-list-endpoint-returns-an-err-916e)
- Fix queue.task_done() counter mismatch in the drain-at-gate-entry loop that would cause queue.join() to hang forever after N swaps. Also fix cap-deferred rank undoing (demoted tickets were re-enqueued at their real priority rank during drain swaps) and extract _peek() helper for PriorityQueue private-attribute access. (20260808T113243Z-global-gate-head-of-line-blocking-a-boar-2b91)
- Fix config drift: `auto_merge_enabled` in `config.example.json` now matches the model default (`true`) and the documented default. (20260801T125422Z-config-drift-auto-merge-enabled-default-5475)
- Retrospect stage now enforces ``retrospect_max_drafts_per_run`` — the per-run draft cap that was declared in settings but never consumed at runtime. Drafts, follow-ups, and AGENT.md proposal tickets all count against the same shared cap, and surplus proposals are silently skipped once the budget is exhausted. (20260802T160626Z-wire-retrospect-max-drafts-per-run-into-2a1e)
- Changelog validation now skips repos that have migrated to release-please. `_modules_yaml_check` does not merely report — it *inserts* a `changelog.d/*.md` glob into `docs/modules.yaml`, so on a release-please repo (which has no such directory) mill was actively breaking the repo's own `check-registration` job the next time it worked a ticket there. (20260808T195500Z-changelog-validate-skip-release-please)
- The CI_FAILURE diagnostic event emitter in the ci_fix stage now falls back to `ticket.board_id` when `ctx.repo_config` is `None` or has no `board_id`.  Previously a missing or unresolvable repo config silently skipped the event, starving the recurring-category → auto-fix-proposal pipeline of input.  Successful and skipped emissions are now logged at info / warning level for observability.  Three regression tests cover the happy path, the `repo_config=None` fallback, and the non-emission on pending CI. (20260807T195906Z-ci-failure-diagnostic-emitter-is-not-emi-e251)
- Fix test suite: disable disk-admission gate in test Settings so tests are not parked at DRAFT/READY due to insufficient free space in /tmp. (20260807T210552Z-auto-rebase-prs-that-fall-into-merge-con-2f27)
- Regenerate uv.lock after removing cryptography exclude-newer-package override in pyproject.toml, resolving the stale `cryptography = "2026-08-01T00:00:00Z"` entry that would cause `uv sync --locked` to fail in CI. (20260807T211754Z-bump-cryptography-to-50-0-0-to-remediate-a309)
- Allow `POST /tickets/ingest` to create tickets on the synthetic `meta` board by falling back to `repos.meta` when the `repo_id` is not in `repos.repos`. (20260807T212743Z-post-tickets-ingest-rejects-the-syntheti-8791)
- Bump `cryptography` to 50.0.0, clearing GHSA-m2h6-j472-rp4c and
  GHSA-g6cj-pr64-35w5 (a Bleichenbacher oracle in PKCS#7 EnvelopedData
  decryption). The vulnerability scan had been failing on `main` since
  2026-08-03, which tripped mill's own target-branch-debt guard and blocked
  13 tickets from merging. The fix version landed inside the rolling 7-day
  `exclude-newer` window, so it needed a per-package override to be
  resolvable — safe to remove once the window has passed. (bump-cryptography-50)
- Merge: a forge merge rejection that only means "a required status check has not reported yet" no longer strands the ticket. GitHub answers 405 both for a permanent refusal and for a still-settling required gate; mill collapsed both into the guess ``merge not allowed (branch protection?)``, discarded the response body, and transitioned straight to ``BLOCKED``. The rejection is now marked retryable, carries GitHub's own message, and is re-polled up to five passes before failing closed. (merge-405-retryable-not-blocked)
- A triage note matching `_TRIAGE_REJECTION_PATTERNS` no longer rejects the ticket by itself; it now only suppresses the deterministic auto-approve shortcut so the LLM classifier decides. These patterns are substrings of free-form LLM prose, and the identical phrase carries opposite meanings depending on its referent — "Grounding is confirmed: the source file exists, the test file does not exist" describes the work to do, while "'mail-ingester' does not exist anywhere in this repository" is a genuinely ungrounded premise. Treating a match as a verdict rejected the first kind too, parking healthy tickets in `human_issue_approval`, a state with no automated stage that nothing ever closes, so they accumulated indefinitely. Confirmed false positives included a test-coverage ticket whose target module exists, a devcontainer fix rejected over an unrelated footnote, and a `NO_CHANGE` audit describing its desired end state. The classifier reads the actual spec and can tell the cases apart; over-matching now costs one classifier call rather than stranding a ticket.
- Fix the deploy-time config-standard footprint gate
  (`validate_config_standard_footprint`) that globbed every `*.yaml`/`*.yml`
  in a repo and blocked any delivery whose tree carried ordinary yaml
  (`config/default.yaml`, a root `docker-compose.yml`, `.pre-commit-config.yaml`,
  `mkdocs.yml`, …) — which was virtually every repo, blocking tickets
  fleet-wide. The gate now only flags a genuine stray `_standards/` copy, the
  one artifact that is never legitimate.
- Git operations that talk to a remote (clone, fetch, push) now run under a 300s timeout with `GIT_TERMINAL_PROMPT=0`, and per-repo clone/fetch refreshes are capped at two concurrent operations. Every stage offloads its blocking work to Python's default thread pool, which holds only `min(32, cpu_count + 4)` threads — 8 on the deploy host — so an unbounded git call that stalled removed one worker from that pool permanently, and a bad or expired token could hang on `/dev/tty` rather than failing. With ~22 registered repos all refreshing simultaneously at start-up (the supervisor deliberately skips its initial delay), this was observed live as the entire pool wedged in the periodic repo refresh while merge stages sat "active" for 10+ minutes having done no work. `TimeoutExpired` is now credential-redacted like `CalledProcessError`, since its `cmd` carries the same tokenized URL.
- Implement loop no longer wastes the entire spawn budget on no-progress
  re-attempts (b92d): a review re-spawn whose previous attempt committed
  only changelog fragments while review threads remain open now escalates
  to BLOCKED in preflight — before the agent loop, without consuming a
  spawn — and the block note carries the reviewer's open gap list (not
  the summary tail). Adds regression tests locking review-feedback
  injection into the implement context after a review bounce.
- Implement no longer takes an LLM-free bypass when a reviewer has asked for changes. The trivial-config-only, rename-only and spec-exact levels apply whatever is already in the working tree and never read the feedback field, so on a review sendback they re-emitted the exact diff the reviewer had just rejected. Review sent it straight back, implement bypassed again, and after four identical passes the cycle ceiling blocked the ticket for human review — a loop that produced the largest blocked class on the board, with reviews naming a concrete edit and every pass summarised "trivial config-only addition". Those three levels are now skipped whenever feedback is present; the cheaper level-1 model stays available because it is a real LLM pass and can act on what the reviewer asked for.
- Implement resume guard now routes a green-CI-but-no-open-PR branch to DELIVERABLE (which opens the PR) instead of IMPLEMENT_COMPLETE (the post-deliver merge-poll state). Routing to IMPLEMENT_COMPLETE skipped the deliver stage entirely, so the PR was never created and the ticket churned back into implement until the spawn cap tripped — the deeper half of the "implement finishes green but never emits a PR" deadlock.
- Implement stage: preserve uncommitted edits on resume. When a cycle terminated on a non-finalizing exit path (transient `AgentRunError` re-raise, pause/interrupt, worker-scheduled retry) it left real edits uncommitted in the worktree; the next resume's `try_rebase_onto` (`git reset --hard` + `git clean -fd`) silently destroyed them, so the agent re-did the work every spawn until the spawn cap tripped and the ticket blocked with 0 commits. `_clone_and_branch` now WIP-commits a dirty worktree that has no commits beyond base before the rebase, so the work survives to the deliver stage.
- Per-repo `auto_merge_enabled` is now read from the repos config and defaults to enabled. The field was declared on `RepoConfig` but never populated in `load_repos_config()`, so it kept its opt-in `False` default for every repo regardless of configuration — making gate 3 of the auto-merge eligibility check unsatisfiable fleet-wide. Every green, review-approved PR bounced back to `HUMAN_MR_APPROVAL` instead of merging, and boards filled with tickets no automated stage could advance. Auto-merge remains gated by the global switch, the global kill-switch, the review gate, the trusted-bot-author check and the sensitive-path globs; set `auto_merge_enabled: false` on a repo entry to opt that repo out.
- Refine no longer hard-blocks a ticket when the refiner returns no usable spec. Both degenerate-spec paths resolved the next state from a literal empty string, which always tripped the "never auto-approve an empty spec" guard and returned `BLOCKED` — while the recorded note still claimed the original draft had been kept and the run had carried on. Every ticket whose refiner produced nothing has been silently blocked since that guard landed, including ones with substantial drafts, making it the single largest blocked class on the board. The next state is now resolved from the original draft: a substantive draft follows its normal approval route, and a draft that is *also* degenerate still blocks — correctly, and with a note that says so instead of claiming otherwise.
- Replaying a prior `triage SKIP` verdict now keeps only the triage's own reasoning. The stored history note also carries the routing verdict appended to it (`" | auto-approve: …"`), and the short-circuit replayed the whole string: it rewrote the stale verdict into the new history entry, where it reads as a fresh decision, and fed it back in as `triage_note` so the obsolete verdict re-derived itself on every pass. A ticket in that state could never be re-judged — live, 11 tickets kept replaying an `auto-approve: REJECTED` suffix produced by a gate that no longer exists, even after the gate was fixed.
- Retrospect no longer files a draft whose body is a placeholder. The spawn condition tested `draft_body` for truthiness only, and a body of literally `"..."` is truthy, so such a draft reached the board with a plausible title and then blocked in refine, which could not build a spec from it. Both the improvement-draft and the follow-up paths now reject a degenerate body. The predicate refine already used for this moved to `core.text_noop` so the body an agent may file and the spec refine will accept can never disagree; dropping an under-specified finding costs one report the agent can raise again, while filing it costs a refine pass and a permanent blocked ticket.
- The `NO_CHANGE`→implement route no longer forwards its own triage reason as a rejection signal. When triage concludes a ticket's work is already done, refine deliberately routes it to `READY` so the implement stage can verify the claim against the live tree. But it also passed the triage reason to the auto-approve gate, and a `NO_CHANGE` reason says by definition "this is already implemented / no change is needed" — verbatim what `_TRIAGE_REJECTION_PATTERNS` scans for. The gate therefore fired on the premise of the route itself, diverting the ticket to `HUMAN_ISSUE_APPROVAL`, where it sat indefinitely: nothing auto-closes that state and no operator approves a ticket whose own note says there is nothing to do. Live, such tickets accumulated for up to six days and became the board's largest and oldest bucket. The spec still faces the normal auto-approve triage; only the self-referential rejection match is skipped.
- The implement stage's spawn counter is now cleared when a ticket leaves `READY` for a later stage. The counter caps implement invocations so a ticket looping through `BLOCKED`→`READY`→`BLOCKED` cannot burn unbounded LLM quota, but nothing ever reset it on success — it was monotonic across the ticket's entire life. A ticket therefore got three implement passes *ever*, and a fourth (an entirely routine outcome after review feedback) dead-ended it with `implement spawn limit reached (3/3)` no matter how much genuine progress it had made. That became the largest blocked class on the live board, on tickets whose own summaries reported every gate passing. Loop protection is unaffected: the implement↔review ping-pong is separately bounded by `implement_cycles` against `max_implement_review_cycles`, and an unproductive ticket never reaches a progress state, so its budget still runs out. The block message no longer tells operators to delete a workspace file — `resume_blocked` has always cleared the counter itself.
- The merge stage no longer pushes a new head SHA while CI checks are still running. Each `IMPLEMENT_COMPLETE` poll refreshed the branch unconditionally — rebasing, or pushing an empty commit when the branch was already current — to un-stick a stale SHA pinning old failed check-runs. A new SHA makes the forge abandon the in-progress run and start a fresh one, and the poll interval is far shorter than a CI run, so every poll restarted the very checks it was waiting on and no run could ever conclude. Measured on the live board: 477 empty commits in one hour across 15 tickets, one of them looping for 20 hours restarting 18 checks every 2 minutes, with 22 tickets stacked in `IMPLEMENT_COMPLETE` because none could finish CI. A cheap probe now skips the refresh while anything is in flight; once a run has concluded the refresh still happens before the status the stage acts on is read, so a stale failing SHA is re-run rather than re-diagnosed as a genuine failure.
- The per-stage outcome cache no longer stores or replays a BLOCKED outcome. Its premise — same input, same result, so skip the expensive re-run — does not hold for BLOCKED, which means a human must intervene; resume-blocked, a code fix and a config change all exist to produce a *different* result next pass. Caching it made a blocked ticket unrecoverable unless its description changed: a ticket resumed after the fix written for it had been deployed logged `refine cache hit (hash=…) → blocked` and replayed the pre-fix outcome verbatim, note and all, without running the fixed code. Root-cause fixes could not reach the tickets they were written for. The read path also ignores pre-existing BLOCKED entries, so workspaces already holding one recover on their next pass rather than needing the cache file deleted by hand.
- The post-push check no longer reports mill's own automation as a foreign human push. It required every commit ahead of the target to have both author and committer equal to `mill@robotsix.local`, so a repo's own CI auto-format commit (`github-actions[bot]@users.noreply.github.com`) and mill's own GitHub App (`…+robotsix-mill[bot]@users.noreply.github.com`, committed by `noreply@github.com`) were classified as foreign. Affected tickets were blocked with "a human likely pushed to the PR branch. Manual reconciliation required" when no human had touched the branch. Commits from GitHub Apps and Actions are now recognised as automation, matched on the shared `[bot]@users.noreply.github.com` suffix so app installation ids need no enumeration. A genuine human commit still trips the check.
- The post-rebase integrity check no longer reports a file as silently dropped when the target branch already carries the branch's version of it. A file legitimately leaves the post-rebase diff when a sibling PR lands the same change first — the rebase correctly collapses the now-redundant delta to nothing — and blocking on that dead-ends a perfectly healthy ticket. Live, ten tickets blocked on the identical `Dockerfile` immediately after the canonical fix for it merged from another PR. Both cases leave `HEAD` agreeing with the target afterwards, so the rebase stage now snapshots each changed file's blob id before the agent runs and a file is excused only when the target's content is byte-for-byte what the branch was trying to deliver; a genuinely discarded change leaves the target on its original content and is still reported.
- Ticket deletion, redraft and the data-dir GC now remove history rows with bulk `DELETE` statements instead of loading each row as an ORM object. Materialising a `TicketEvent` decodes its `state` column through the strict SQLModel `Enum`, which raises `KeyError` for any value retired from `State` since the row was written — live, 200 legacy `MAINTENANCE` rows across five boards made every archived-ticket purge crash. Because the purge runs from `transition()`, each crash also aborted the worker's post-transition stage chaining, costing the ticket a poll cycle, and archived tickets accumulated without bound. Rows being deleted never need to be decoded, and this is now one statement per table rather than a `SELECT` plus N deletes.
- `POST /tickets/{id}/merge-now` is now documented in the `/chat-skill` document, with explicit guidance to call it rather than sending the operator to the forge UI. The endpoint was implemented and served but absent from the skill — the chat agent's only description of the mill API — so the agent could not know it existed and told operators to open the PR and merge by hand, which is precisely the manual step the endpoint exists to remove. A ticket parks in `human_mr_approval` when the merge stage deliberately declines to auto-merge (sensitive path such as `.github/workflows/**`, or a repo on the infra denylist); the PR is already green and mergeable and only a human decision is missing, so the agent should surface the blocking reason and then act on the answer itself. A new test enumerates state-changing ticket routes from the live router and fails when one is undocumented, against a recorded baseline of the seven operator-only routes that were already exempt.
- `ci_fix` dependency tickets are now exempt from the pre-existing target-branch CI-debt guard. The guard blocks a PR when every workflow failing on its head is also failing on the merge target — but a `ci_fix` ticket is spawned precisely to repair that debt, so blocking it deadlocked the board: the repair for a red `main` was refused because `main` was red, and only a human merging by hand could break the cycle. Observed live on `robotsix-chat`, where one red Dockerfile check wedged 22 tickets along with all three `ci_fix` tickets spawned to clear it. These tickets now fall through to the auto-fix loop, which remains bounded by the existing cross-stage cycle counter.
- `run_command` loop-guard no longer bricks legitimate large-file exploration. It now counts only *distinct* greps per file (byte-identical repeats are already refused separately) and raises the per-file threshold from 3 to 8, so a large source file that genuinely needs many different greps is not refused after the third one.

### Deprecations and Removals

- Board: removed the per-card column-move control and the `POST /board/move/{card_id}/{target_status}` route behind it. The route had been returning 500 (`AttributeError: 'str' object has no attribute 'value'` in `board_move`), and the control itself was already invisible on the mill board — `board-mill.css` hid it with `.board-card-move { display: none }` because all 22 mill columns are automated pipeline stages, so a manual move "is misleading and can drop a ticket into a state inconsistent with the pipeline". robotsix-board no longer renders the form, so both the hide rule and mill's `move_endpoint()` / `move_endpoint_template()` adapter methods are gone. A state change goes through `POST /tickets/{id}/transition`, the path the pipeline itself uses. Mill's own "Move to board…" button (`.move-btn`, `POST /tickets/{id}/migrate`) is unaffected — it migrates a ticket between repos, not between columns.

### Misc

- 20260801T130251Z-upgrade-ruff-from-0-15-15-to-0-16-0-and-cb29, #0

### Other changes

- Promote mypy from advisory to a gating CI check and clear 293 `[type-arg]` baseline errors — bare `dict`, `list`, `Task`, `Queue`, `PriorityQueue`, `tuple`, `set`, `Pattern`, `CompletedProcess`, `AbstractAsyncContextManager`, and `TypeDecorator` generics now carry explicit type arguments. The baseline shrinks from 697 to 412 lines (~41% reduction). Add `make mypy-baseline-shrink` target for snapshot regeneration.
- Remove `docker` pytest marker and two dead integration tests (`tests/sandbox/test_sandbox_integration.py`) that had no CI execution path. Sandbox integration behavior is already covered hermetic-ally by the monkeypatched tests in `tests/sandbox/test_sandbox.py`.
- Harden module-registration CI check against deleted-but-not-yet-git-rm'd files: filter out files that no longer exist on disk before flagging them as unclassified.  Update implement agent instructions to ``git rm`` deleted files before running ``check-registration``, preventing the loop where an agent keeps re-adding a ``docs/modules.yaml`` entry for a file it just deleted.
- Add docstring to `PrioritySlots.release` matching the sibling `acquire` method. (mill: Resolve dead docker-marked sandbox integration tests: give them a real CI home or mark local-only (20260806T184416Z-resolve-dead-docker-marked-sandbox-integ-c421))
- Add dedicated unit tests for `PollLoopsMixin` (`_initial_delay`, `_load_ci_state`, `_prune_ci_state`, `_find_canonical_ci_ticket`, `_fetch_run_logs_with_deferral`, `_dependabot_title`, `_dependabot_body`) and for `processing.py` helpers (`_post_trace_event`, `_block_ticket_and_notify`, `_handle_stage_error`, `_maybe_reevaluate_epic`, `_root_span_attributes`, `_root_input_summary`, `_root_output_summary`)
- Configure Renovate `platformAutomerge` (squash) for patch, minor, and digest dependency updates, replacing the dead `dependabot-auto-merge.yml` wrapper that gated only on `dependabot[bot]` (Renovate PRs were never auto-merged).
- Sandbox: clean stale build artifacts (`build/`, `src/*.egg-info/`) before every project install so a single transient setuptools failure doesn't poison the entire sandbox run. Audit agent prompt updated to treat repeated identical build errors as a terminal signal and fall back to pre-built-venv commands.
- Tighten the implement agent's prompt around external-dependency tickets:
  add a pre-flight existence check for the target manifest file (emit
  `no_change_needed` early when the file is absent), require the agent
  to declare and validate the before→after version against local git
  tags before editing, treat unverified web results as terminal, and
  enforce one-edit convergence so investigation can't consume the full
  run budget on a single-line change.) (mill: Resolve dead docker-marked sandbox integration tests: give them a real CI home or mark local-only (20260806T184416Z-resolve-dead-docker-marked-sandbox-integ-c421))
- Strengthen `run_command` tool docstring to explicitly forbid prefixing commands with ``cd`` to any absolute path (the sandbox already sets the working directory), preventing agents from hallucinating workspace-root ``cd`` prefixes that waste ~160 s on doomed calls.
- Implement stage: when `github_token()` fails permanently (e.g. missing
  credentials, App not installed), clone failures now skip the transient-retry
  loop and BLOCK immediately with a clear root-cause message ("no auth token
  available"), instead of burning through retry attempts on an unfixable error. (mill: tool_error — Multiple run_command observations (612ef4f942217042, 23d7e1f705253e2d and their parallel s (20260806T001800Z-tool-error-multiple-run-command-observat-e242))
- Refine stage memory persistence now degrades gracefully on disk-full errors instead of failing the ticket. The `persist_memory_db` retry loop now covers SELECT operations (not just commit/flush), and `_emergency_vacuum` runs a WAL checkpoint before VACUUM to improve recovery odds when `/tmp` is nearly full.
- Fix unbounded no-op CI refresh commits in `ci_fix_mixin`: pass a sentinel path to `_refresh_branch_for_ci` so that a ticket bouncing BLOCKED→resume pushes at most one empty commit per branch head, mirroring the bound already in `ci_poll`. Remove the dead `_CI_EMPTY_COMMIT_COUNTER` / `_MAX_CI_EMPTY_COMMIT_REFRESHES` constants.
- The fixing_ci/implement cycle ceiling no longer counts transient CI
  failures (runner crashes, network resets, Docker flakes, auth outages)
  against the auto-fix budget.  A green PR on a fast-moving base branch
  can no longer be hard-blocked by infrastructure churn masquerading as
  CI failures.
- Add `rerun_workflow` to `GitLabForgeCIMixin`, backed by GitLab's `POST /projects/:id/pipelines/:pipeline_id/retry` endpoint. Previously, CI-fix agents on GitLab repos could not automatically retry failed pipelines.
- ci_fix: include run URLs in `wait_for_ci` failure summaries so the agent can pass them to `fetch_ci_logs`; hardened `fetch_ci_logs` to reject placeholder ids with actionable guidance; added prompt instruction to never guess a run id.
- Add `board-read` skill to `agent_definitions/implement.yaml` (`skills: [board-report, board-read, ask_user_guardrails]`). The implement agent has `read_ticket: true` but was the only agent with that flag missing the `board-read` skill, which provides guidance on using the `read_ticket` tool in sandbox environments.
- Extract `_maybe_collapse_scanner_rollup` and `_create_one_draft` helpers from `run_agent_pass` (~160-line loop body), leaving a shallow coordinator that sequences phases and persists memory. No behavior change. (mill: Investigate refine-stage sqlite 'database or disk is full' persistence errors blocking observation writes (20260804T171734Z-investigate-refine-stage-sqlite-database-caa5))
- Decomposed `_poll_implement_complete` (334 lines, 7-level nesting) in `ci_poll.py` into a shallow state-machine coordinator plus three focused helpers: `_refresh_branch_for_ci_if_idle`, `_handle_ci_failure_route`, and `_merge_or_promote_when_green`.
  Decomposed `_handle_out_of_scope` (194 lines, 8-level nesting) in `ci_fix.py` into a thin coordinator plus four helpers: `_reject_in_scope_alerts`, `_refresh_stale_branch_once`, `_retry_transient_ci_failure`, and `_spawn_or_reuse_fix`.
  Pure extraction — no behavior change.
- Remove dead backward-compat alias `_PLACEHOLDER_SPEC_PHRASES` from `stages/refine/helpers.py` (zero consumers; everyone binds directly to the canonical `PLACEHOLDER_BODY_PHRASES`).
- Remove dead backward-compat artifacts from `Secrets`: `model_dump()` method and `_secret_field_names` class attribute.
- Fix ``_extract_check_names`` to parse the ``## ❌ FAILED: <name>`` format produced by ``_build_failing_summary``, so the ci-fix agent timeout diagnostic note correctly names the failing CI check(s) instead of returning ``(unknown)`` or ``**Summary:**``.
- ci_fix: added ``ci_fix_agent_timeout_seconds`` (default 1800 s). The ci-fix agent call is now wrapped with its own wall-clock timeout that fires *before* the worker's generic ``stage_timeout_seconds``. When the agent exceeds its budget the stage produces a diagnostic BLOCKED note (failing check name, elapsed time) instead of a bare "timed out after 2400 s" — the operator can see *what* CI check was being worked on.
- Add `audit-ignore` to the CI workflow call with the same three CVE ignores as `security-audit.yml`, so the `uv audit --frozen --preview` step introduced by the python-ci.yml reusable workflow bump (e94a9aad) no longer fails the ci / Tests job before tests run.  The three CVEs are all false positives: PYSEC-2025-183 (pyjwt, disputed), MAL-2026-4750 (fastapi, withdrawn), and GHSA-9xwg-3r6f-jcx2 (pymdown-extensions, already fixed).
- Add `audit-ignore` to `ci.yml` with the same CVE ignores as `security-audit.yml`, so `uv audit --frozen --preview` (introduced by the python-ci.yml reusable workflow bump to e94a9aad) does not fail the ci / Tests job before tests run.
- Bound Hypothesis `st.text()` strategies with `max_size` limits in `test_web_knowledge.py` and `test_core_dedup.py` to prevent resource exhaustion in CI from unconstrained string generation.
- Complete the config-standard cutover: remove all `MILL_CONFIG_FILE`, `MILL_SECRETS_FILE`, and `MILL_REPOS_FILE` env-var reads. `ROBOTSIX_CONFIG_FILE` is now the sole config-file locator. Delete `config/repos.example.yaml` (repos now live in the main config file). Update smoke test and docs accordingly. (mill: CI red on main: ci / Tests job failing (exit code 1) — fix the failing test(s) (20260802T103726Z-ci-red-on-main-ci-tests-job-failing-exit-07b1))
- Fix stale capability-levels table in `docs/config/configuration.md`: add missing level-4 row and move `epic_breakdown` from level 3 to level 4. Also correct the model-tier-rebalance CHANGELOG entry to reflect that `meta_triage` stayed at level 3 (not moved to level 2).
- **Model tier rebalance:** move `epic_breakdown` from level 3 (opus) to level 4 (fable-5) to reduce weekly Claude subscription token pressure — fable-5 handles structured JSON output reliably at lower cost. `pipeline/meta_triage` stays at level 3 (opus) due to the existing test `test_meta_triage_model_defaults_to_capable_tier` which asserts `level == 3` ("capable" tier). `refine` remains at level 3 (opus) as the only stage that genuinely needs it. Level 4 is now a valid agent-definition tier, documented in the schema and configuration reference.
- Adopt the canonical Langfuse projects block (robotsix-standards#189). Mill's own Langfuse credentials move from flat `secrets.langfuse_*` keys into a top-level `langfuse` block: `{"host": "https://...", "projects": {"robotsix-mill": {"public_key": "...", "secret_key": "...", "project_id": "..."}}}`. Per-repo `langfuse_*` fields on `RepoConfig` are unchanged. A `model_validator` strips old `secrets.langfuse_*` keys at startup so an unmigrated config starts cleanly (traces nothing) instead of crash-looping. (mill: 20260802T133619Z-adopt-the-canonical-langfuse-projects-cr-0660 resume-preserve WIP [WIP])
- ci_fix agent: add pre-commit hook verification guidance to the system prompt — fetch upstream `.pre-commit-hooks.yaml` before guessing hook config, verify Docker image tags exist before pushing, and confirm the pinned revision actually exposes the hook manifest.  Reduces the guess-and-push loop that burns CI verification iterations on misconfigured pre-commit entries.
- `scripts/emit_config_schema.py --check` now prints a unified diff showing
  exactly what differs when the committed schema drifts from the generated
  one, giving CI-fix agents (and humans) actionable signal instead of a
  bare "stale" message.
- Re-synced `mypy-baseline.txt`: removed 2 stale entries for resolved strict-mode violations, fixing CI's baseline-ratchet step on main.
- Implement agent: always run `robotsix-modules check-registration` as a pre-DONE gate, not just when new files are created. This catches glob drift from file moves, deletions, and directory restructuring before the post-merge auto-fix cycle.
- Rebase agent now receives the PR's implement-stage file list and any previously-dropped files as context, so it preserves them during conflict resolution instead of silently discarding them. Fixed a short-circuit bug in `check_rebase_diff_integrity` that missed drops when all PR files were removed.
- Wire `mypy_baseline` into the pass registry, CLI, and periodic config toggles, matching the sibling `llm_agent` passes so it can be triggered on-demand via `mill mypy-baseline` or the board pass-run endpoint. (mill: Implement agent must run `robotsix-modules check-registration` (or reconcile modules.yaml globs) before declare-DONE when moving files or editing module paths (20260731T180026Z-implement-agent-must-run-robotsix-module-3478))
- Refine block notes now include a diagnostic reason when a refined spec is rejected as degenerate — distinguishing empty specs from placeholder-phrase matches (e.g. "tbd", "see above") so operators can tell real spec gaps from false negatives.
- Add three guardrails to the meta-agent's periodic-workflow coverage prompt (§3) to prevent recurring re-add/remove churn: language-scope check (skip source-scanner agents for repos lacking the target language), deliberate-removal check (grep CHANGELOG before classifying absence as regression), and the guiding rule that "was merged before" does not mean "should exist now."
- AGENT.md: add "## Import hygiene" rule codifying that side-effect imports
  (e.g. ORM table registration) must be module-level with `# noqa: F401`,
  never function-local, to survive Ruff/vulture auto-fix passes.
- Wire `parallel_commands` into the audit periodic agent's toolset via a new `include_parallel_commands` flag on `make_agent_runner` / `run_periodic_agent` / `_build_periodic_tools`, so the agent that originally motivated the tool can actually use it.
- ci_fix: stop pushing empty no-op commits before diagnosing CI failures. The stage now reads job logs first, classifies the failure via `is_transient_ci_failure()`, and only re-triggers (via `rerun_workflow`, not empty commits) for transient infrastructure flakes. Deterministic failures proceed directly to the ci-fix agent for root-cause fixing. The agent prompt now explicitly forbids empty commits and requires failure classification before any code change. (mill: meta-planner: gap-detector treats deliberately-removed periodic agents as regressions; add config-scope + CHANGELOG guardrail (20260801T083252Z-meta-planner-gap-detector-treats-deliber-1cb5))
- **Breaking (sandbox):** `sandbox.run()` now defaults `install_project=True` so the workspace clone is the imported tree in ALL sandbox paths — not just the test gate. Callers that must skip the install (e.g. ad-hoc commands with no egress proxy) can pass `install_project=False` explicitly. This fixes the root cause behind the `implement.yaml` step-0 stopgap (PR #2679), where coordinating/chat agents wasted ~69 LLM rounds discovering the workspace clone wasn't on the import path.
- Document CLI shell-completion convention in AGENT.md: when adding a CLI subcommand, regenerate `contrib/completions/` and commit in the same change to avoid CI failures.
- Promote `from . import models` to module-level in `db.py`
  with `# noqa: F401`. A function-local noqa import was
  silently deleted by a ruff auto-fix pass, breaking
  SQLModel schema creation. The module-level pattern already
  exists in `alembic/env.py`; this aligns `db.py` with it.
- Document stage: prevent the doc agent from overwriting source/test files under `src/`, `tests/`, and `www/` by passing `write_blocked_prefixes` to `build_fs_tools`. The `write_file`, `edit_file`, and `delete_file` tools now refuse to mutate paths that start with any blocked prefix. (mill: AGENT.md: CLI — When adding a CLI subcommand (a `_RUNNERS` entry in `src/robotsix_mill/cli/__in… (20260801T012300Z-agent-md-cli-when-adding-a-cli-subcomman-3530))
- Decomposed `_poll_one_repo_ci` (301-line god-method) into four focused helpers (`_load_ci_state`, `_prune_ci_state`, `_latest_runs_by_workflow`, `_fetch_run_logs_with_deferral`) plus a lean coordinator that sequences high-level steps, mirroring the review.py `run` decomposition style.
- Fix Docker Hub non-library image digest resolution: `_registry_digest` now uses `registry-1.docker.io` for the OCI manifest URL while keeping `auth.docker.io` for token requests only. Previously the same host was used for both, causing manifest requests to fail against the auth server. Also properly splits `token_host`/`manifest_host` parameters so non-Docker-Hub registries (quay.io, etc.) get their own host for both calls instead of being hardcoded to Docker Hub. (mill: Fix Docker Hub non-library digest resolution: _registry_digest must use registry-1.docker.io (not auth.docker.io) as manifest host (20260801T033242Z-fix-docker-hub-non-library-digest-resolu-7c98))
- Raise `retries:` in `agent_definitions/review.yaml` and `agent_definitions/document.yaml` from 2 to 3 to reduce `UnexpectedModelBehavior: Exceeded maximum output retries` failures
- Refine stage-cache now keys on a hash of the refine module's Python source
  files in addition to the ticket description content, so pipeline-code
  changes (e.g. gate fixes) automatically invalidate the cache and force
  a fresh re-refine rather than replaying a stale pre-fix verdict.
- **Board discovery tests**: Added 7 tests covering `_collect_candidate_boards` (own-board inclusion, `default_repo_id` fallback, disk-scan-failure resilience, dedup), `resolve_by_suffix`/`list_children_across_boards` bound-board coverage, and `_board_for` default-repo discovery from board-less services — the scenario closest to the reported "ticket not found" error.
- **Board discovery**: `_collect_candidate_boards` now always includes the service's own `board_id` (not only when `prepend_self` was set), and falls back to `default_repo_id` when its on-disk DB exists but the repo is absent from `repos.yaml`. Fixes "ticket not found in any configured board" errors for the config_sync and bespoke agents whose tickets can land on boards outside the managed-repo registry.
- Remove the deprecated legacy `robotsix_mill/runners/` shim package. All CLI
  subcommands now import directly from `robotsix_mill.agents.runners.*`.
- Fix stale docstring in `run_answer_agent`: the answer agent now gets web access via the `web_knowledge` flag (injecting `ask_web_knowledge`), not a direct `web_research` tool.
- Fix ``init_db()`` missing ``from . import models`` import that was
  accidentally dropped during the ``_init_locks`` refactor, which could
  cause ``SQLModel.metadata.create_all()`` to produce no tables when
  ``init_db`` is the first code path to touch models.  Also add a
  defensive ``mkdir`` before Alembic commands and a ``timeout=5``
  connect arg in ``alembic/env.py`` to guard against transient
  "unable to open database file" failures under xdist.
- mill: Land stranded implement loop-escalation fixes (b92d): remove the `blocked_from` guard in `_load_implement_context` so review feedback is injected on every resume including review→READY bounces, and add `_NOT_DOC_ONLY_TERMS_RE` refine doc-only pre-check to prevent false doc-only classification.
- Add missing `PeriodicAgentResult` import and `TriageBoilerplateResult` alias in `triage_boilerplate.py` — the triage-boilerplate periodic agent would crash with `AttributeError` on `getattr(module, "PeriodicAgentResult")` before reaching its prompt. Updated YAML `output_type` to `TriageBoilerplateResult` for consistency.
- Add `scripts/resolve_docker_digest.py`: resolves `image:tag` → sha256 digest via the Docker Hub REST API and OCI registry manifest API, so "pin Docker base images" tickets can resolve digests programmatically without pausing for operator input. (mill: Autonomously resolve Docker base-image digests instead of asking the operator (20260731T123627Z-autonomously-resolve-docker-base-image-d-d662))
- `run_command` now consults `read_file`'s read-dedup state: shell commands that re-read already-served file content (sed, cat, awk, head, tail) are refused with a REFUSED message, closing a loophole where agents could bypass the read_file dedup guard by reading through a shell command.
- Add a pre-flight step to the implement agent's system prompt: before editing Python source files, confirm the installed package resolves to the workspace `src/` directory rather than `site-packages`, to prevent wasted rounds editing the wrong tree.
- Raise ``web_research_request_limit`` and ``web_knowledge_request_limit`` defaults from 12 to 16 model turns to prevent ``UsageLimitExceeded`` budget exhaustion on multi-page web research tasks. Fix hardcoded budget literals in ``ask_web_knowledge`` tool description so they stay in sync with the setting.
- Board UI: add **Re‑implement** button for tickets in `human_mr_approval` — posts a change-request comment via the existing `request-implementation-changes` API, transitions the ticket back to `READY` so the implement agent re-runs against the existing PR branch without requiring the PR to be closed first.
- Cap web_fetch calls per web_research sub-agent invocation at 4 (new `web_research_fetch_max_calls` setting), raise `web_research_request_limit` from 8 to 12, and return a distinct "budget exhausted" error when the sub-agent hits `UsageLimitExceeded` — prevents the sub-agent from exhausting its request budget on excessive fetches before synthesizing an answer.
- Fix Re‑implement button flicker on fresh drawer open by including it in `updateMergeButton()` (both render paths now build identical HTML for `ticket-merge-btn-area`). Also add `.reimplement-btn:disabled` to the in-flight-lock CSS list so the button dims during `lockWhile`.
- Audit agent: add `run_command` batching guidance to prevent serial tool-call storms (chain multiple one-liners with `&&` or delegate to `explore`/`parallel_explore`).
- Document board-hygiene settings (draft TTL auto-close, open-ticket cap, rollup epics) in the configuration reference
- `wait_for_ci` now includes the GitHub Actions `run_id` in the `CI_FAILING` output
  prefix (e.g. `[sha: 04cdd8f, run: 30399400000]`), so the ci-fix agent can pass it
  directly to `fetch_ci_logs` without blindly guessing run IDs.
- `ask_user` tool now emits an operator notification (via the existing ntfy channel) when it opens an `[ASK_USER]` thread, so the operator sees the question before the 3-day timeout fires.
- Auto-merge is now **opt-out** (default `True`) instead of opt-in. The global
  `auto_merge_enabled` setting defaults to `True` — merging green PRs is the
  mill's job, no toggle required. Set `auto_merge_enabled: false` to opt a
  specific repo out.
- Branch-protection rejections during auto-merge now transition the ticket to
  `blocked` with the forge error message recorded in history, instead of
  silently parking in `human_mr_approval`.
- **Scanner rollup**: periodic scanner passes (docstring_coverage, module_size,
  test_gap, health, completeness_check) now roll up multiple findings into a
  single ticket per run when `scanner_rollup` is enabled (default true),
  cutting ~80% of scanner ticket inflow. Configurable via `scanner_rollup`
  and `scanner_max_drafts_per_run`.
- **Ingest dedup hardening**: `POST /tickets/ingest` now applies a normalized-title
  fingerprint check before the LLM dedup, catching same-symptom reports that
  differ only in timestamps, file paths, or counters. Re-reporting a symptom
  whose normalized title matches an existing open ticket appends a history
  note instead of creating a duplicate.
- **Per-source rate caps**: added `scanner_max_drafts_per_run` (default 5) and
  `retrospect_max_drafts_per_run` (default 2) settings to cap high-volume
  feedback sources.
- Classify transient CI failures (ECONNRESET, buildkit boot timeouts, setup-uv fetch errors, runner shutdowns, etc.) before spawning a blocking `ci_fix_dependency` ticket. Transient failures now trigger automatic workflow re-runs (up to `ci_transient_max_retries`, default 3) instead of immediately spawning a fix ticket.
- `resume-blocked` for CI-failed tickets now refreshes the PR branch (rebase + empty-commit) **before** evaluating CI, so a transient flake that has since resolved un-sticks in one resume instead of re-reading the same stale failing run forever.
- fix(implement): advance to DELIVERABLE when the branch has committed-ahead work and the agent produces no new file edits (non-zero tool calls but zero edit calls), instead of looping until the spawn limit trips
- Added `tail` query parameter to `GET /tickets/{id}/history`, returning the last N events in chronological order without requiring the caller to know the total event count.
- `read_ticket` tool now fetches only the most recent 50 history events (``order="desc"``, ``limit=50``) so the final event is always visible even for tickets with very large histories. The chat skill doc now documents the history endpoint's ``limit``, ``offset``, and ``order`` query params.
- Emit a SPAWN_LIMIT_EXHAUSTED diagnostic event when the implement spawn
  counter hits its limit, with the last attempt's summary tail included in
  the event reason. Agents (including the periodic diagnostic agent) can
  now discover spawn-limit exhaustion programmatically via the shared
  diagnostic event store.
- Add reverse-check invariant (#4) to `scripts/check_config_docs_sync.py`: `_check_stale_no_doc_exceptions` flags every `_MODEL_FIELDS_NOT_IN_DOCS` entry whose env var now resolves in `docs/config/configuration.md`, so a field that gains documentation without removing its exception causes the script to exit non-zero.
- Added "Config & docs conventions" section to `AGENT.md`: when `config.example.json` intentionally diverges from a Settings model default, document the override in `docs/config/configuration.md` using the established `sandbox.image` pattern.
- Refine: short-circuit to human approval when the triage classifier fails instead of falling through to the expensive opus refine agent. Saves ~$1.84 per ticket when the cheap triage model is unavailable.
- Added `mypy_baseline` periodic agent: monitors `mypy-baseline.txt` and `mypy-baseline-test.txt` entry counts, detects growth across file boundaries, and files draft tickets categorized by error code (`[type-arg]`, `[no-untyped-def]`, etc.) — closes the gap where pre-commit (staged-files-only) and advisory CI let cross-file mypy regressions silently grow the baseline.
- Auto-answer: replying to an open `[ASK_USER]` thread on an `awaiting_user_reply` ticket now automatically closes the thread and resumes the ticket — no separate close-thread step required.
- Add `exclude-newer = "7 days"` to `[tool.uv]` in `pyproject.toml` — newly-published package versions (< 7 days old) are excluded from `uv lock --upgrade` resolution, providing a cooldown window for the community to detect malicious releases before they propagate via automated Renovate bumps. (mill: Add `exclude-newer` dependency cooldown to `[tool.uv]` for supply-chain defense-in-depth (20260729T174433Z-add-exclude-newer-dependency-cooldown-to-abc0))
- Auto-heal stuck auto-merge tickets whose PR is merely out-of-date with the target branch: mill now calls the forge's update-branch API (server-side) and retries auto-merge after CI re-runs, instead of bouncing to `human_mr_approval`. Genuine failures (conflict, red CI) still bounce as before.
- Cleaned up ~70 stale entries from `_MODEL_FIELDS_NOT_IN_DOCS` in `scripts/check_config_docs_sync.py`: removed fields already documented in `docs/config/configuration.md`, moved `deliver_max_identical_blocks` and `refine_web_fetch_*` to the config-file-only group, and removed two nonexistent fields (`trace_review_max_inspector_runs_per_pass`, `sandbox_push_token`). Added `review_diff_max_chars` to the default-mismatch exceptions (doc uses `200_000` for readability; model stores `200000`).
- Document the intentional `refine_trivial_model_level` override in `config.example.json` (`3` for flat-cost Claude subscription vs. code default `2` for pay-per-token DeepSeek Pro) in `docs/config/configuration.md`.
- Fix false "already satisfied" closure on resumed tickets: when the ticket branch carries ahead-of-target WIP commits (resume-preserve) but the work has not been merged into origin/main, the implement stage now proceeds to CODE_REVIEW → deliver instead of short-circuiting to DONE. This prevents stranding unmerged work and the gap from regrowing.
- `explore` sub-agent: stop retrying on non-transient errors (e.g. account-out-of-credits 402) instead of burning 3 retry attempts against a model that will never succeed.
- Wire `credit_balance` pass into `_PASS_REGISTRY` so it is triggerable
  via the board UI and API, not only on its hourly poll schedule.
- Refine stage: add output-length guidance to system prompt (target 2500–4000 words, prefer bullets, aim for ~4000 tokens). Lower `refine_trivial_model_level` default from 3 (Claude sonnet) to 2 (DeepSeek Pro) so straightforward gap-fill tickets use a cheaper model, saving ~$0.90 per trivial refine.
- **Stale-spec fingerprint guard now durably suppressed after `resume-blocked`.**  When an operator resumes a blocked ticket with a justification note, the guard's cleared state persists across subsequent spawns for the same spec fingerprint — the ticket only re-blocks when the spec actually changes.  Previously the guard re-triggered on the next implement attempt even after a manual resume, forcing repeated operator intervention.
- Wire per-repo `auto_merge_enabled` from `repos.yaml`/`repos.json` through `load_repos_config()` to `RepoConfig`. Previously the field was declared and documented but never passed from the config data, so per-repo opt-in was silently ignored.
- Document `MILL_DIAGNOSTIC_EVENTS_PATH` env var in the periodic-agents config table (`docs/config/configuration.md`).
- Document `gates.delta_context_retry_enabled` (`MILL_DELTA_CONTEXT_RETRY_ENABLED`, default `true`) in the configuration reference under section 11.3 Refine routing.
- config-sync: `max_refine_passes_per_ticket` — already documented in section 11.2 (Stages tuning) of `docs/config/configuration.md`; no docs change needed.
- Document `max_refine_passes_per_ticket` (`MILL_MAX_REFINE_PASSES_PER_TICKET`, default 3) in the configuration reference under "Stages tuning"
- Tighten implement agent tool-use discipline: forbid `run_command` for trivial
  file-content lookups (`grep`, `cat`, `ls`, `head`, `tail`, `wc` on known paths)
  and direct the model to use `read_file` or `explore` instead.
- Add documentation row for `implement_max_spawns_per_ticket` in `docs/config/configuration.md`.
- Document `implement_stall_threshold` in the configuration reference table (Section 3).
- mill: ci_fix: narrow success criterion to dispatched check(s) only — agent now reports DONE when its assigned check(s) pass even if unrelated pre-existing failures remain red, and compares new CI_FAILING summaries against the original dispatch summary to determine scope
- **Fix:** spawn counter increment moved to after all preflight guards pass, so blocks from late guards (stale respawn, stall detector, cycle cap, tool/skill/workspace integrity) no longer burn a spawn slot.  Previously the counter was incremented early in preflight and later checks could block the spawn without any LLM work, exhausting the 3/3 budget with near-zero spend and no implement-cycle traces.
- Added deterministic fast-path for trivial config-only changes (new presence/config files ≤40 lines). Bypasses the LLM coordinator entirely for tickets that only add fresh `.yaml`/`.toml`/`.md`/etc. files — handled via `_handle_trivial_config_change` mirroring the rename-only pattern.
- Eliminate duplicate `_CODQL_CHECK_NAMES` definition in `ci_fix_codeql.py`; import from canonical definition in `ci_fix_helpers` instead.
- Add `POST /tickets/{id}/answer` endpoint so the chat agent can deliver an operator-supplied answer to a ticket in `awaiting_user_reply`. Posts the answer to the open `[ASK_USER]` thread, closes it, and auto-resumes the ticket back to its originating state.
- Add missing `.robotsix-mill/periodic/roadmap_sync.yaml` presence file so the periodic scheduler discovers and runs the `roadmap_sync` workflow.
- Fix vulture "unused variable 'frame'" warning in `src/robotsix_mill/runtime/tracing.py` by prefixing the unused signal handler parameter with `_`.
- **config**: Moved `deploy_api_url` to its correct alphabetical position in `config.example.json` and removed it from the config-sync exception set in `scripts/check_config_sync.py`, permanently enforcing its presence (`deploy_api_url` is no longer an optional/exception field).
- Add `deploy_api_url` to `config/config.example.json` and document it in `docs/config/configuration.md` (section 6 Service).
- Wire `roadmap_sync` as a fully scheduled periodic pass: add `roadmap_sync_periodic`/`roadmap_sync_interval_seconds` settings fields, register it in `_BUILTIN_KINDS` as `schedule_only`, and add the runner to `_SCHEDULE_ONLY_RUNNERS` so the periodic supervisor can schedule it automatically.
- `_revert_standard_configs` no longer auto-reverts standard config files
  (`.pre-commit-config.yaml`, `docker-compose.yml`, `mkdocs.yml`) when the
  ticket's `file_map` declares them as relevant scope; they now pass through
  to the scope-triage LLM for normal evaluation.
- Remove stale agent references (`maintenance.yaml`, `periodic/cost_analyst.yaml`) from AGENT.md example lists; both files were deleted in earlier commits.
- Enabled expanded Ruff lint rules (SIM, C4, LOG, G, ERA, PGH, RUF, PT) in
  ``pyproject.toml``. Applied ~540 auto-fixes across 223 files (``ruff --fix``
  + ``--unsafe-fixes``) for safe transformations like nested-with flattening,
  contextlib.suppress, collapsible-ifs, needless-bool simplification,
  collection-literal conversions, and Yoda-condition fixes. Grandfathered
  remaining pre-existing violations via per-file-ignores: RUF001-003 and
  ERA001 blanket-suppressed for ``src/**`` and ``scripts/**``, RUF012
  per-file for 6 source modules, PT/SIM/RUF059/RUF012/ERA/E402/PGH004/B017/
  B018 noise blanket-suppressed for ``tests/**``, SIM103 for ``dev/**``.
  Recovered ~80 bandit/other violations exposed when RUF100 stripped
  inline ``# noqa:`` comments during the unsafe-fix pass — added per-file
  ignores for those too. All gates pass: ruff check, ruff format, mypy
  (no new errors), deptry, vulture.
- `_build_failing_summary` now includes an explicit pass/fail indicator (❌ FAILED: / ✅ PASSED:) per check instead of the ambiguous `Failing check #N` label. Check-run conclusions are now preserved through `_extract_annotations` (GitHub) and `_get_failed_jobs` (GitLab) so the summary can show the genuine status at a glance.
- Consolidate `runners` module into `agents.runners` sub-package to break the circular dependency between `agents` and `runners`.  All runner source files move to `src/robotsix_mill/agents/runners/` and test files to `tests/agents/runners/`.  A deprecation shim at `src/robotsix_mill/runners/__init__.py` preserves backward-compatible imports for one release cycle.
- Enable Ruff D (pydocstyle) rules with Google convention, suppressing D105/D107/D205/D415.  Auto-fixed ~260 mechanical violations; remaining D102/D417 gaps are per-file-ignored with FIXME markers for incremental cleanup.
- Enable pytest-xdist parallel test execution: add `-n auto` to CI pytest-args, `parallel = true` to coverage config, and restructure `make test` with `coverage combine` for accurate multi-worker coverage. Add `make test-fast` for no-coverage parallel runs.
- Fix `test_gap_interval_seconds` documented default in `docs/config/configuration.md`: `86400` → `604800` (7 days), matching the Pydantic model default in `_settings_periodic.py`.
- Fix stale code comment: survey interval default now correctly stated as 604800 (7 days) in `_settings_periodic.py`.
- Document `trace_review_min_confidence` in the `trace_review` config table (`docs/config/configuration.md`).
- Fix `survey_interval_seconds` documented default in `docs/config/configuration.md`: was `86400` (1 day), now matches the Pydantic model default of `604800` (7 days).
- Fix `docs/config/configuration.md` audit agent table: default for `MILL_AUDIT_INTERVAL_SECONDS` corrected from 86400 to 604800 (7 days), matching the Pydantic model field default.
- Fix `docs/config/configuration.md`: the `MILL_META_INTERVAL_SECONDS` default was documented as `86400` (1 day) but the model default is `604800` (7 days). Now documents the correct `604800` default.
- Fix stale `doc_request_limit` default in `agent_definitions/document.yaml`: both the budget description ("default 16" → "default 32") and tool-use discipline section ("budget (16)" → "budget (32)") now match the actual config default of 32.
- Fix `board_list_cache_ttl_seconds` default drift: changed Pydantic model default from `0.0` to `3.0` to match `config.example.json` and docs, making the board-list cache enabled by default as intended.
- Add `check-builtin-kinds` pre-commit hook to validate `_BUILTIN_KINDS` cross-sync across workflow portability, periodic passes, poll loops, and agent definitions.
- Updated all stale `config/repos.yaml` references in the Repos registry section of `docs/config/configuration.md` to reference `config/config.json`'s `"repos"` key, matching the actual loader behaviour.
- Add `check-builtin-kinds` pre-commit hook to validate `_BUILTIN_KINDS` cross-sync between `workflow_portability.py`, `_passes.py`, `poll_loops.py`, `.robotsix-mill/periodic/`, and `agent_definitions/periodic/`.
- Added `scripts/check_builtin_kinds.py` pre-commit hook that cross-validates `_BUILTIN_KINDS` against `.robotsix-mill/periodic/`, `agent_definitions/periodic/`, and `_passes.py` to prevent multi-site synchronization drift. Fixed the known inconsistency: added `"roadmap_sync": "schedule_only"` to `_BUILTIN_KINDS`. Registered the hook in `.pre-commit-config.yaml` and `.github/workflows/ci.yml`.
- Refine agent: add "Run pytest only once" guidance to combine all flags into a single invocation, avoiding wasted duplicate full-suite runs.
- Add `credit_balance` to `_BUILTIN_KINDS` as `"schedule_only"` so `kind_for("credit_balance")` returns the correct kind and `is_portable` returns `True` consistently with other schedule-only workflows.
- Reclassify `repo_description_sync` from `schedule_only` to `llm_agent` so that
  per-repo presence-file overrides (``prompt_overlay`` / ``system_prompt``) take
  effect at runtime. The custom runner now receives ``definition_override`` from
  the periodic-workflow dispatch path instead of loading the built-in YAML
  directly from disk.
- Fix stale `config/repos.yaml` references in the "Deployed log folder" section
  of `docs/config/configuration.md` — the repos config lives under the `"repos"`
  key of `config/config.json`, not in a standalone `config/repos.yaml`
- Post-rebase diff integrity check: after every rebase the pipeline now
  verifies that all implement-stage source files still appear in the
  branch diff vs merge-base.  If the rebase agent silently dropped a
  file (the PR's changes were discarded during conflict resolution),
  the ticket is BLOCKED with a diagnostic listing the dropped files.
  Excludes `CHANGELOG.md` and `changelog.d/` from the comparison.
- Stuck-loop detector in implement stage now considers committed branch-ahead-of-main work (not just working-tree changes), preventing false BLOCKED when a prior cycle already committed and pushed complete work
- Implement agent: add instruction to register new changelog fragment files in `docs/modules.yaml` under the `core` module's `paths` list.
- Extended `scripts/check_config_sync.py` with invariant 4: validates code-comment `Default N` values against the actual `Field(default=N)` in the Settings model. Scans `_settings_periodic.py` and `_settings_core.py` for comment-stated numeric defaults, resolves the associated field name, and reports mismatches. Fixed the stale `roadmap_sync_interval_seconds` comment (said 86400/daily, actual 604800).
- Fix zero-edit implement loop persisting after #2552: the resume-with-ahead-branch
  path in `_detect_no_change_contradiction` was gated on `_any_repo_has_changes`
  returning False, which bundles both working-tree cleanliness AND branch-not-ahead.
  On a resume the branch IS ahead (prior commits from earlier passes), so the
  guard never fired and the ticket fell through to CODE_REVIEW → re-implement →
  spawn-limit BLOCKED.  Add a new exit path that detects "resuming + clean working
  tree + branch ahead" and routes directly to DONE (already satisfied).
- Added 12 unit tests for the GitLab pagination helper `_paginated_get` (single/multi-page, empty, HTTP errors, item transforms, parameter forwarding).
- Implement stage scope guardrail: auto-revert standard config files
  (``.pre-commit-config.yaml``, ``docker-compose.yml``, ``mkdocs.yml``)
  when they appear out-of-scope after an agent pass.  These repo
  scaffolding files are occasionally regenerated by the agent (driven
  by AGENT.md conventions) even when the ticket scope is narrow — the
  guardrail now reverts them to ``origin/<target>`` instead of blocking
  or consuming LLM tokens on scope-triage.
- Decompose the 598-line `ReviewStage.run` method into an orchestrator (~95 lines) and six private helpers: `_resolve_diff_and_metadata`, `_clone_cross_repo_workflows`, `_resolve_review_level`, `_validate_action_shas`, `_handle_review_verdict`, and `_handle_request_changes`. Each helper encapsulates a distinct responsibility (diff computation, cross-repo cloning, model-level routing, action-ref validation, verdict routing, and REQUEST_CHANGES processing). Behaviour is unchanged — all 57 review-stage tests pass.
- Implement stage: zero-edit runs with all gates green now route to
  DONE (already satisfied) instead of re-spawning and eventually
  hitting the spawn-limit BLOCKED.  Two paths fixed: resuming with
  empty diff in `_detect_no_change_contradiction`, and resuming with
  zero tool calls in `_verify_repo_changes`.
- Pin `pymdown-extensions>=11.0.0` in the docs dependency group to resolve GHSA-9xwg-3r6f-jcx2 (path traversal in the b64 extension, fixed in 11.0.0).
- Extract ``preflight`` gate checks from ``PhaseCoordinatorMixin`` to a new
  ``phase_coordinator_preflight.py`` module (reduces ``phase_coordinator.py``
  from 1414 → 1123 lines).
- **fix:** newly created `ci_fix_dependency` tickets are now transitioned to `READY` immediately so the worker's poll loop picks them up, preventing the silent deadlock where a draft fix ticket was created but never enqueued.
- Add CI gate (`check_sourcekind_frontend_parity.py`) to prevent Python `SourceKind` / JS `SOURCE_CLASS` / CSS `.src-*` drift. Fix existing drift: add missing `user`, `repo_description_sync`, and `config_standard` JS entries; remove duplicate `docstring_coverage` key.
- Add `pytest-randomly>=4.1.0` to dev dependencies for randomized test-order detection
- Implement the config-ownership standard component config surface: `GET /config`, `PUT /config` (with validation and secret rejection), `GET /config/versions`, and `POST /config/rollback`. Secrets are always masked as `**********` on read and rejected on write — they remain env-injected by the deploy plane. A Settings panel in the board UI (⚙ Settings) lets operators view and edit component-owned config fields at runtime without a redeploy, with a version-history tab for rollback.
- Bump `pypdf` minimum constraint from `>=5` to `>=6.14.2` to pick up fixes for CVE-2026-59935, CVE-2026-59936, CVE-2026-59937, and CVE-2026-59938.
- Deliverable-stage config-standard footprint check now only flags files that the ticket branch actually touched (added, modified, or deleted). Pre-existing fleet-standard files like `.pre-commit-config.yaml` and `docker-compose.yml` no longer block delivery when they are not part of the branch's change set.
- Add missing `module_size` entry to `SOURCE_CLASS` map in `board-mill.js` and corresponding `.src-module_size` CSS rule in `board-mill.css`, so tickets with `SourceKind.MODULE_SIZE` render with the correct source badge instead of the fallback `.src-user` badge.
- Fix four stale Langfuse credential descriptions in `docs/config/configuration.md`: the Secrets reference table (5 rows), footnote ¹, `deployed_log_folder` prose, **and the Repos registry intro paragraph** all described the data flow backward. Now correctly describe the global secrets block → `Secrets` → `_apply_global_langfuse` → `RepoConfig` path, and that all repos share the same global Langfuse configuration.
- Add `docs/stages/implement.md` — a narrative design document covering the implement-stage lifecycle, fix iteration loop, resume path, submodule breakdown, config knobs, scope-triage sub-gate, and cross-spawn stall guard.
- Config-standard footprint validation at deliverable stage now only inspects files the ticket's branch actually changed (three-dot diff), ignoring pre-existing repo files that the ticket never touched. Previously, files like `.pre-commit-config.yaml` that exist in the repo but were not part of the ticket's diff would block deliverable.
- Document `sandbox_push_token` in the Secrets reference table (`docs/config/configuration.md`).
- `commit_all()` is now no-op-safe: after staging with `git add -A`, it checks `git status --porcelain` and returns silently if nothing is staged, avoiding a `CalledProcessError` from `git commit` on a clean tree (e.g. review-fix passes whose net diff is a pure deletion). The implement stage's `_finalize` also wraps `commit_all()` calls with stderr capture so that git failure diagnostics appear in ticket history instead of just the command line.
- Periodic agents (survey, audit, health, etc.) now catch the Claude SDK degenerate "error result: success" exception and degrade gracefully to a no-op pass with preserved memory, matching the refine agent's existing guard.
- Config-standard 4-file footprint enforcement: CI gate rejects PRs
  adding files outside the canonical footprint, deploy-time validation
  blocks out-of-footprint files before push, and the refine stage
  enumerates the approved footprint for config-standard tickets.
- Bring sandbox git-push bridge credential under the managed Secrets surface
  with a dedicated ``sandbox_push_token`` field, a push-access health probe
  in ``/health/ready``, and distinct ``PUSH_AUTH_ERROR`` classification so
  operators can distinguish a credential blind spot from a code defect.
- Fix malformed base URL in lifecycle wrapper: `deploy_api_url` now validates the `https?://` scheme at config time, and `check_deploy_freshness()` normalizes bare hostnames by prepending `https://` when a scheme is missing, so httpx never sees a URL it can't parse.
- Rewrite `docs/langfuse/observability.md` to describe the current global `secrets:` block mechanism instead of the removed per-repo `langfuse:` blocks. Removed all references to `langfuse_from` inheritance.
- Remove dead re-exports ``_status`` and ``_is_openrouter_upstream_error`` from ``agents.retry`` — neither symbol had any callers.
- Add dedicated unit tests for ``_paginated_get`` (10 tests covering single/multi-page, 401 retry, exception fallback, boundary cases, URL/params forwarding).
- Implement agent now has a robotsix-standards-specific README TOC rule
  that explicitly covers the three prose tables (Every repository,
  Deployable components, Deployment system), closing the remaining gap
  where new standards pages updated mkdocs.yml and docs/index.md but
  missed README.md.
- Extract `_persist_artifacts_and_run_guardrail` helper from duplicated block in `_handle_rename_only_change` and `_handle_spec_exact_edits` (copy-paste cleanup).
- Add deterministic programmatic gates (meta runner + refine stage) that reject tickets proposing to enable internal (non-portable) periodic workflows on managed repos, using the data-driven portability map in `workflow_portability.py` — stops `state_sync` and other mill-only workflows from being proposed for non-mill repos before implement is ever reached.
- Mill now handles empty GitHub repos gracefully: when cloning a freshly-created repo with no commits, `git_ops.clone()` auto-seeds an initial commit (author: robotsix-mill bot, message: "Initial bootstrap commit") instead of failing. This eliminates the long-standing papercut where every new repo registration required a manual first commit before the mill could process tickets against it.
- Install tini as the container PID 1 via the Docker image ENTRYPOINT, so orphaned subprocess grandchildren (e.g. ``git`` spawned by the transient ``claude`` CLI that exits before waiting on its children) are reaped instead of accumulating as zombies. Observed as 4067 defunct ``git`` processes after ~21h uptime when running under central-deploy (whose v1 contract does not support ``init: true``).
- Annotate advanced/expert-only config settings with `"advanced": true` in the JSON schema so the deploy UI can hide them behind an "Show advanced settings" toggle. Common operator-facing settings (URLs, feature toggles, secrets, paths) remain visible.
- Add workflow portability classification (`is_portable`, `render_workflow_portability`) to the periodic loader, derived from the existing `_BUILTIN_KINDS` kind map. Refine and meta agents now receive a data-driven **Workflow Portability** table and gate internal-workflow enablement proposals (e.g. `state_sync`, `frontend_sync`) instead of hardcoding individual workflow names.
- Add `vulture` (dead-code detection) to the implement stage's mandatory pre-flight checks, alongside ruff, mypy, and deptry, so lint failures are caught locally before the PR/CI round-trip.
- Emit `CI_FAILURE` diagnostic events on every ticket entering `fixing_ci`, with a stable normalized failure key so recurring failure modes cluster. Add `RecurringCIFailureCheck` diagnostic check that auto-files fix-proposal draft tickets when a failure key has been hit by enough distinct tickets (threshold configurable via `diagnostic_ci_failure_threshold`, default 3).
- Remove outdated per-repo `langfuse:` blocks from `docs/config/configuration.md` — Langfuse credentials are configured globally in the `secrets:` block, not per-repo.
- Extract special-case edit handlers from `implementation_logic.py` into new `implementation_editing.py` module — `_verify_repo_changes`, `_handle_rename_only_change`, `_handle_spec_exact_edits`, and `_find_insertion_point` now live in `_ImplementationEditingMixin` (~565 lines moved).
- Extract scope-guardrail + preflight tests (~1238 lines) from
  `tests/stages/implement/test_implement.py` into new
  `tests/stages/implement/test_implement_preflight.py` (module-size split)
- Enable `module_size` periodic agent by adding its per-repo presence file (`.robotsix-mill/periodic/module_size.yaml`).
- Fix Alembic proxy-registry race condition: add global ``_alembic_lock`` to serialize all ``_run_alembic_migrations`` calls across boards, preventing ``KeyError: 'config'`` when concurrent ``init_db`` calls collide on Alembic's process-global proxy registry (observed as CI flake in ``test_generate_children_applies_epic_body`` under xdist).
- Replace cached `github_token()` with on-demand `github_push_token()` in deliver and periodic runner push paths (changelog autofill, roadmap sync, pin bump), following the same per-push App-token renewal pattern already used by ci_fix/rebase. Eliminates stale-token push failures when a cached App installation token expires mid-flight.
- Implement agent now checks and updates `README.md` TOC tables when
  creating, renaming, or removing documentation pages under `docs/`,
  preventing the recurring TOC-drift defect seen in robotsix-standards.
- Implement agent: add investigation-only ticket detection. When a ticket spec asks the implement agent to investigate a failure on a different board/repo, the agent now produces a diagnosis via `no_change_needed` (written to `implement.md`) instead of making spurious code edits, since the implement stage has no cross-repo trace/log access.
- Add `hypothesis` as a dev dependency and introduce property-based
  tests for pure-function invariants: `parse_duration`/`format_duration`
  round-trip, `normalize` idempotency, `_slug` invariants, and
  `_stamp_frontmatter`/`_parse_frontmatter` body-lossless round-trip.
- Fix ``_validate_changelog_fragments``: import validation logic in-process
  from ``_changelog_validate`` instead of shelling out to a script
  resolved via ``parents[3]`` (which pointed at ``src/``, not the repo
  root, so the validation never ran in production).
- Add docstring to `CaseTolerantEnum.process_bind_param` in `src/robotsix_mill/core/models.py`.
- Add docstring to `CaseTolerantEnum.process_result_value` in `src/robotsix_mill/core/models.py`.
- Add ``scripts/validate-changelog.py`` — pre-commit changelog fragment validator that ensures trailing newlines and ``docs/modules.yaml`` registration, called automatically by the implement stage's ``_finalize`` before committing. Also fixes a missing trailing newline in ``maybe_generate_towncrier_fragment``.
- `github_push_token()` now requests `workflows:write` alongside
  `contents:write` when minting a GitHub App installation access
  token, fixing push failures for pushes that touch
  `.github/workflows/` files.
- Add docstring to `RunEntry` dataclass in `run_registry.py` documenting all seven fields and lifecycle semantics.
- `doc_classifier` system prompt refined with explicit user-facing criteria (new public API, config field changes, exception contracts, CLI changes) and standardized classification format with examples
- Implement stage: clear cached summary and reference files on resume-blocked to prevent the agent from being fed its own prior output as context, which caused byte-identical replay across cycles.  Preserve stall-detection state in `implement_stall_state.json` so the cross-spawn stall guard survives operator-initiated resume/reset cycles.  The preflight stall guard now falls back to this JSON file when `implement.md` is absent.
- `review_revision.py`: migrate from unscoped `github_token()` to `github_push_token()` (scoped `contents:write`) for force-push and pre-push reconcile fetch, matching the ci_fix/rebase push paths (PR #2483).
- Expand sandbox-path guard in doc and review agent prompts: the old
  prompt only warned against reading from outside paths (`/tmp/`, etc.);
  now also warns against writing — `write_file` and `edit_file` will
  reject them too. Prevents recurring ~$0.002-per-occurrence tool-call
  waste from agents attempting to write to unreachable paths.
- git push operations (ci_fix, rebase) now use `github_push_token()`, which mints a fresh, least-privilege GitHub App installation token scoped to `contents: write` on the target repository — eliminating the dependency on long-lived, expiring PATs for push authentication.
- Fix: implement stage now clears stale conversation state on spawn-limit BLOCK and resume_blocked, so corrective operator feedback is loaded into a fresh agent conversation instead of being drowned out by a replayed prior transcript.
- Add cross-spawn stall guard to implement stage: detects when the agent's output is byte-identical across consecutive BLOCKED cycles, stops consuming spawn rounds before the limit is exhausted, and blocks with an actionable diagnostic that surfaces unaddressed review comment IDs.  Controlled by new ``implement_stall_threshold`` setting (default 2).
- Fingerprint guard now respects operator force-retry: `resume-blocked` with a justification note (or any BLOCKED→READY transition with a note) clears the stale-spec guard for exactly one implement cycle, instead of silently re-blocking on an unchanged fingerprint.
  The automatic-refusal diagnostic now names `resume-blocked` as a remedy alongside the existing spec-update and reset-fingerprint options.
- Register the `module_size` periodic agent in all three registration points: ``_BUILTIN_KINDS`` (periodic loader), ``_PASS_REGISTRY`` (passes API), and CLI ``_RUNNERS`` + ``add_parser`` (``robotsix-mill module-size`` subcommand).
- Add `.shellcheckrc` with Bash dialect and external-sources settings for consistent shellcheck behavior across scripts.
- Add shellcheck pre-commit hook (`shellcheck-py`, severity=warning) to lint shell scripts at commit time.
- Add `lint-sh` Makefile target that runs `shellcheck` on all shell scripts, and chain it into the `lint` target.
- Add `module_size` to the periodic-agent lists in `docs/agents/agent-yaml-schema.md` (category and read_ticket fields).
- Stale `CHANGES_REQUESTED` forge reviews are now actively dismissed instead of only being silently discarded. Added `dismiss_review` to the Forge interface (`base.py`, `github_pr.py`, `gitlab/core.py`) and extended `_pr_review_status` to return `review_id`. The core fix in `_review_changes_requested_outcome` detects stale reviews regardless of `review_feedback_enabled`, preventing approved MRs from bouncing back to `human_mr_approval` on a stale review artifact.
- Remove the review-artifact requirement from the auto-merge eligibility gate.
  The upstream `human_mr_approval` operator gate is the authoritative review
  decision point; the redundant downstream artifact check in
  `_auto_merge_eligible()` has been removed.  Approved, CI-green tickets in
  `waiting_auto_merge` now merge to `done` without bouncing back to
  `human_mr_approval`.
- Add `alembic check` drift gate in CI (`mill-specific` job) to catch un-generated migrations when models change.  Also add `make check-migrations` target for local use.
- Implement stage: edit-claim contradiction guard now retries within the pass
  (with diagnostic feedback) instead of immediately BLOCKING on fresh runs,
  preventing the BLOCKED→READY→BLOCKED loop across stage-level retries. The
  guard also produces a detailed root-cause diagnostic (missing args, lost
  edits, un-replayable tool kind) via the new ``_build_edit_claim_diagnostic``
  helper.
- Remove dead no-op function ``_validate_cross_repo_forge_compat`` from
  ``config/repos.py`` and its lone call site. Both GitHub and GitLab adapters
  already support cross-fork MRs.
- Add module-level docstrings to ``cli/ticket.py`` and ``cli/serve.py``.
- Relocate implement-stage test files into `tests/stages/implement/` subdirectory.
- Refactor `_evaluate_test_results` and `_run_scope_guardrail`: extract six cohesive helpers (`_run_smoke_gate`, `_detect_no_change_contradiction`, `_verify_repo_changes`, `_clean_binary_artifacts`, `_filter_vendored_deps`, `_run_scope_triage_classification`) to reduce complexity hotspots (~60% line-count reduction in both functions).
- Review stage: ``_verify_action_sha()`` now accepts an optional ``token`` parameter and uses ``git_ops._authed_url()`` to authenticate ``git ls-remote`` calls against private repos — previously unauthenticated calls produced false-positive "SHA not found" errors for private repos like ``damien-robotsix/robotsix-github-workflows``.  Added ``_reusable_workflow_sha_refs_from_diff()`` to extract SHA refs from reusable-workflow ``uses:`` lines (previously skipped by ``_action_refs_from_diff()``).  ``ReviewStage.run()`` fetches the GitHub token and passes it to both the action-ref and reusable-workflow-ref validation loops.
- Resolve `skills_dir` / `language_instructions_dir` robustly: skill
  injection, the language-snippet loader, and the implement preflight now
  fall back to the packaged resource directories (with a one-time warning)
  when the configured directory doesn't exist, instead of hard-blocking
  every ticket with "missing skill file". Also drop the CWD-relative
  `skills_dir`/`language_instructions_dir` entries from
  `config.example.json` — they resolve against /app in the container and
  were the source of the 2026-07-19 board-wide preflight blocks (the fix
  ticket itself could not run: circular dependency).
- Fix `Worker.stop()` to handle `ExceptionGroup` wrapping `CancelledError`
  in Python ≥3.11 by using `except*` instead of bare `except`.
- Fix pipeline-wide agent-run crash: bump `robotsix-llmio` pin past the
  sync-wrapper fix (sync `call_with_retry`/`run_agent` invoked the caller's
  `run_sync`-style fn inside `asyncio.run()`, breaking every draft-refine and
  triage call), and add a running-loop guard to mill's `run_agent` mirroring
  #2451: when called with an event loop running (e.g. on the Claude SDK's
  loop), the retry session is delegated to a thread so `run_sync` can create
  its own loop.
- Fix redraft/re-block loop for tickets with existing all-green branches: implement stage now detects when a remote branch has green CI but no open PR and routes to IMPLEMENT_COMPLETE so the deliver stage re-opens the PR instead of re-running the implement loop. Also add a guard ensuring every BLOCKED transition records a reason in the history event.
- Add deploy-freshness gate to prevent wasted implement attempts on stale worker images. The implement preflight and resume-blocked paths now check ``GET /services/mill`` (when ``deploy_api_url`` is configured) and park tickets with an explicit "awaiting redeploy" note when the running image predates the latest digest.
- Add `state_sync` to the periodic-agent lists in `docs/agents/agent-yaml-schema.md` (category field reference and `read_ticket` field reference).
- Update `docs/agents/agent-yaml-schema.md`: replace stale `board` skill references with the three actual skills (`ask_user_guardrails`, `board-read`, `board-report`) and reflect `refine.yaml`'s real `skills` list
- Implement stage now bootstraps empty remote repos (no commits, no branches) with an initial README commit instead of blocking the ticket. Ports the cd2c pattern from the periodic meta agent's `clone_all_repos` path.
- Correct stale `modules: true` opt-in claim in `AGENT.md`: `refine.yaml` has opted in, `meta.yaml` explicitly sets `modules: false`.
- Remove dead `.src-security-posture` CSS rule from board-mill.css (no matching SourceKind enum member exists)
- `human_mr_approval`: discard stale `REQUEST_CHANGES` reviews when the PR head has changed since the review was cast (compare `review.commit_id` against `pr.head.sha`). Prevents the verified 7-cycle verdict-replay loop that dominated tickets where the diff issue was externally remediated.
- Fix `language_instructions_dir` default to resolve via `importlib.resources` instead of a bare relative `Path`, so the built-in language snippets are found in installed (container) mode. Add a preflight check that hard-blocks when the directory is absent, catching container-only path-resolution gaps before a model pass opens.
- Fix: meta-ticket workspace setup crashes on freshly-created empty repos — `build_meta_workspace` now detects empty remotes and bootstraps them with an initial commit, matching the existing `clone_all_repos` behaviour.
- Implement stage preflight now hard-blocks (instead of silently degrading) when the agent definition has no tools, a referenced skill file is missing, or the workspace directory is inaccessible. Each failure includes the specific path/condition in the error note, preventing the zero-tool-call no-op loop seen on non-mill boards.
- Bump `robotsix-llmio` git pin past 2026-07-16 to pick up
  optional-`RunContext` fix (`_tool_converter` now accepts tools
  with `ctx: RunContext[None] = None`, resolving a Claude SDK
  `takes_ctx=True` block on `read_file` and similar tools).
- Clear stale review artifact and stage-outcome cache on successful rebase so the review gate re-evaluates the current diff instead of replaying a cached REQUEST_CHANGES verdict. Also invalidate the review cache when the auto-merge eligibility gate detects a stale (head-SHA-mismatched) verdict.
- Fix zero-edit implement loop persisting after prior fix: the `reprompt_if_unstructed` guard now checks for zero tool calls BEFORE the `isinstance(expected_type)` short-circuit, so structured `ImplementResult` envelopes with no tool calls trigger a re-prompt (unless `no_change_needed=True`). Also, the per-pass stuck-loop detection in `_implement_loop` now computes `progress` regardless of `has_diff`, so leftover changes from a prior session cannot mask a current pass that contributed zero tool calls.
- Survey periodic pass now classifies findings as repo-specific or fleet-wide convention candidates, and can file companion tickets on the ``robotsix-standards`` board for generalizable conventions. The runner supports cross-board ticket creation via the new ``draft_target_repo_ids`` field on ``PeriodicAgentResult``, with creation-time dedup on the target board to prevent duplicate standards proposals across repos.
- Add zero-tool-call guard in implement stage: a pass where the agent issues no tool calls and produces no diff now surfaces a distinct BLOCKED error immediately in both the retry loop and the resume→CODE_REVIEW path, rather than masquerading as a generic no-edit stall. (mill: Implement agent spins with zero tool calls / zero edits on robotsix-auto-mail workspace (20260718T152204Z-implement-agent-spins-with-zero-tool-cal-1620))
- Resolve six trivial `# type: ignore` suppressions with proper type annotations, shrinking the mypy baseline by 4 entries (708→704).
- `state_sync` is now a mill-internal periodic agent (kind `mill_only`) — it no longer appears as an opt-in presence-file pass for managed repos. The agent continues to run against robotsix-mill on its existing schedule via the periodic supervisor's mill-repo guard.
- Add `verify_diff` tool to the implement agent: replaces 3-5 `run_command` grep/awk verification calls per `edit_file` with a single `git diff --stat` call plus optional expected-file cross-check. Registered in `ToolRegistry` category `git` and steered by a new "Batch verification with `verify_diff`" prompt section.
- Add module-level docstrings to `runtime/worker/processing.py` and `runtime/worker/epic.py`, matching the style of the other worker submodules.
- `resume-blocked` now only resets the implement spawn counter when the ticket was actually blocked at the spawn limit (counter ≥ `implement_max_spawns_per_ticket`), and records the reset as a history event ("spawn counter reset via resume-blocked"). Tickets blocked from READY for other reasons keep their counter intact.
- Add tiered test-run policy to implement agent prompt: targeted tests first, broader related tests second, never escalate to full suite (pipeline job).
- Add batching discipline rule to implement agent prompt: batch `git grep` / `run_command` questions into a single `explore` or `parallel_explore` call to reduce round-trips and wall-clock cost.
- Add `changelog_autofill_periodic` and `changelog_autofill_interval_seconds` settings fields, giving the changelog-autofill schedule-only pass a configurable kill-switch and interval (previously hardcoded to 86400 s with no disable option).
- GitLab forge: implement cross-project merge request support via `target_project_id` when `head_repo` is provided, matching the GitHub adapter's cross-fork PR workflow. Remove the `NotImplementedError` stub and the `_validate_cross_repo_forge_compat` guard that rejected `cross_repo_target` for GitLab.
- Document stage: deterministic short-circuit for doc-only diffs (all paths are `.md` or under `docs/`), skipping the classifier + doc agent and saving $0.005–0.01 per occurrence.
- Add class-level docstring to `PeriodicPassesMixin` describing its per-repo periodic pass orchestration.
- Added docstring to ``health_ready`` endpoint in ``_health.py`` documenting the readiness probe's Args, Returns shape, and 503-on-failure behaviour.
- Add docstring to `WorkerPool.start()` method in `src/robotsix_mill/runtime/worker/core.py`.
- Merge gate: stale review verdicts no longer block auto-merge after a rebase. The review artifact now records the branch head SHA; when the current PR head differs the stale verdict is ignored. Prevents the merge gate from re-posting byte-identical REQUEST_CHANGES verdicts that no longer apply to the rebased branch.
- Document `sandbox.image` dev-vs-prod dual default: the Pydantic model default is `python:3.14-slim` for lightweight local development, while the production JSON config overrides to `robotsix/mill-sandbox:latest` (includes `uv` and toolchain). Added inline docstring comment and updated config docs table to match.
- Fix orphan `agent run` Langfuse traces: propagate OTel/contextvars across
  `ThreadPoolExecutor` boundaries in watchdog/timeout helpers so pydantic-ai
  agent spans nest under the stage-named root trace instead of creating
  unattributed root spans.
- Update `docs/agents/agent-yaml-schema.md` to match the current `AgentDefinition` model: replace `model` with `level`, replace `web` with `web_knowledge`, add missing field docs (`list_epic_children`, `list_threads`, `ask_user`, `inject_agent_md`, `inject_language_conventions`, `max_tokens`), update category listings and tools table, fix `read_ticket` section.
- Fix `coordinator_timeout_seconds` model default drift: changed from 900 to 600 in `_settings_core.py` to match `config/config.example.json` and documentation.
- Fixed typo `rebasin` → `rebasing` in the valid State values list in the chat-skill endpoint docstring.
- Save conversation state on `AgentBudgetError` (budget exhaustion) so
  the implement agent can resume from where it left off instead of
  restarting from scratch. The BLOCKED→READY resume path now loads
  saved conversation state alongside `previous_attempt_summary`.
- Auto-generate board passes dropdown from the pass registry; remove hand-wired routes and buttons for trace_health, langfuse_cleanup, meta, and run_health — all passes now trigger via the generic ``POST /passes/{pass_id}/run`` endpoint.  Passes are grouped by kind (LLM Agents, Runners, Global) in the dropdown.
- **Board UI**: replaced hand-wired "Agents" dropdown with a dynamically-populated "Passes" dropdown driven by the periodic pass registry (`GET /passes` + `POST /passes/{pass_id}/run`). Passes are grouped by kind (LLM Agents / Runners). Adding a new pass to `_PASS_REGISTRY` is now the only wiring needed to make it manually triggerable from the board.
- Sync `STATE_TRACE` in `board-mill.js` with the canonical `STAGE_FOR_STATE` mapping from `states.py`: corrected `ready`→`"implement"`, `implement_complete`→`"merge"`, `rebasing`→`"merge"`, `done`→`"retrospect"`; added missing `draft: "refine"`; removed terminal `closed` (no stage).
- Extract shared standards-awareness prompt block into `agent_definitions/_shared/standards-awareness.yaml` and add `!include` resolution to `yaml_loader.py`. Survey and audit agents now consume the canonical block via `!include` instead of maintaining separate copies.
- Route small mechanical refactors (≤40 lines, no new files) to level-1 review model, reducing review cost for fully-prescribed extraction/move tickets by ~10×.
- Remove deprecated `env_doc_sync` periodic agent (agent definition, implementation module, route, CLI, board UI, config settings, and all test coverage). Env-var documentation consistency is now governed by robotsix-standards policy with audit enforcement.
- Remove the `security_posture` periodic agent entirely: delete the agent definition, source module, tests, runner config, CLI entry, HTTP route, board UI button, settings fields, and all code/docs references. Security posture is being codified in robotsix-standards as an auditable standard.
- New periodic agent `docstring_coverage`: scans Python source modules for public functions, classes, and methods with zero docstring, prioritizes by complexity, and files draft tickets. Includes YAML definition, Python module, presence file, SourceKind entry, settings, periodic-runner registration, CLI/API/board-UI wiring, and test suite.
- Add module-level docstring to `src/robotsix_mill/dev_tooling/__init__.py`.
- Add module-level docstrings to worker submodules (`core.py`, `poll_loops.py`, `periodic_passes.py`), describing each mixin's role in the event-driven consumer assembly.
- Fix SQLite engine leak in Alembic migrations: `alembic/env.py` now disposes its engine after each run, and `init_db` skips redundant `create_all` + Alembic passes when the board is already initialized. Together these eliminate a file-descriptor leak that caused "unable to open database file" errors in CI under test suites with many tests sharing a worker process.
- Fix infinite auto-approval loop: the mechanical draft fast-path now rejects empty/whitespace drafts, preventing tickets with empty descriptions from being auto-approved in a cycle (approve → refine produces empty body → fast-path approves again).
- ci_fix: rebase onto main before scanning CI so stale branches produce a fresh run against current main; include branch HEAD SHA in the consecutive-identical failure fingerprint to prevent the re-block loop on already-resolved upstream failures; clear depends-on after spawning an out-of-scope dependency fix so the operator's resume-blocked is not silently parked by the unmet-dependency gate
- Changed the Pydantic default for `api_host` from `"127.0.0.1"` to `"0.0.0.0"` to match the shipped `config/config.example.json`. Updated `docs/config/configuration.md` accordingly, closing a three-way config-drift gap.
- Fix stale `tester` reference in `docs/agents/agent-yaml-schema.md` — renamed to `run_tests` to match the renamed agent definition.
- Remove 11 backward-compat aliases (`AuditPassResult`, `AgentCheckPassResult`, etc.) from `periodic_runner.py`; all callers now import `PeriodicPassResult` directly.
- Register five missing CLI subcommands (`state-sync`, `env-doc-sync`, `frontend-sync`, `security-posture`, `triage-boilerplate`) in argparse so they are reachable from the command line.
- Deduplicate ``_resolve_repo_config`` by delegating repo-id resolution to
  ``_resolve_repo_id``; collapse three identical ``elif`` arms in
  ``_run_and_print`` into a single ``elif cmd in (...)`` block.
- Change all 14 built-in periodic workflow defaults from daily (86400 s) to weekly (604800 s): `agent_check`, `bc_check`, `completeness_check`, `diagnostic`, `env_doc_sync`, `frontend_sync`, `health`, `meta`, `module_curator`, `repo_description_sync`, `run_health`, `state_sync`, `survey`, `test_gap`. Per-repo overrides via `.robotsix-mill/periodic/<name>.yaml` (`interval:` field) are unchanged — repos that need faster cadence can override back to `1d`.
- Sandbox test timeouts (rc=124) now produce a deterministic ENV-ERROR diagnosis
  instead of invoking the expensive LLM distiller, letting the fix-loop circuit
  breaker fire immediately without burning 30+ requests per cycle.
- Add `MILL_MEMBER_SYNC_PERIODIC` and `MILL_MEMBER_SYNC_INTERVAL_SECONDS` env var rows to the "Env-var-only periodic agents" table in `docs/config/configuration.md`.
- Document `MILL_CONFIG_SYNC_PERIODIC` and `MILL_CONFIG_SYNC_INTERVAL_SECONDS` env vars in the "Env-var-only periodic agents" table (`docs/config/configuration.md`).
- docs: add `stale_branch_cleanup` to footnote ² exception list in `docs/config/configuration.md` so that its 86400 s (1 day) default is documented, matching the Pydantic model default.
- Docs: add `langfuse_cleanup` to the footnote ² list of agents that default to 86400 s (1 day) interval in `docs/config/configuration.md`.
- Updated the periodic-agent generic interval default in `docs/config/configuration.md` from `86400` (1 day) to `604800` (7 days), matching the actual model defaults for the majority of periodic agents. Added a footnote listing the agents that default to `86400` (1 day) or `3600` (1 hour).
- Document `implement_pass_timeout` / `MILL_IMPLEMENT_PASS_TIMEOUT` in the config reference table (section 2, Request limits).
- Document `sandbox_op_timeout` / `MILL_SANDBOX_OP_TIMEOUT` in the configuration reference (section 9, Sandbox).
- Added the ``diagnostic`` periodic pass to ``_SCHEDULE_ONLY_RUNNERS`` so repos with a ``diagnostic.yaml`` presence file have it scheduled (previously it was silently skipped). Extended ``scripts/check_builtin_kinds.py`` with invariant 5 to catch future ``schedule_only`` entries that are missing scheduler wiring.
- Document `ci_debt_recheck` periodic agent in `docs/config/configuration.md`: add to periodic agent list and document `MILL_CI_DEBT_RECHECK_PERIODIC` (default `true`) and `MILL_CI_DEBT_RECHECK_INTERVAL_SECONDS` (default `3600`) env vars
- Docs: added `roadmap_sync`, `frontend_sync`, and `pin_bump` to the periodic agent list and env-var reference table in `docs/config/configuration.md`.
- Document `docstring_coverage` and `module_size` periodic agents in `docs/config/configuration.md` — add them to the periodic agent list and add dedicated subsections with env-var tables.
- Add `repo-description-sync` CLI subcommand, wiring the already-registered periodic pass into the CLI `_RUNNERS` dict, add_parser block, and `_resolve_repo_config` branch.
- Added `changelog-autofill` CLI subcommand, wiring the existing schedule_only runner for manual invocation.
- Add missing `.robotsix-mill/periodic/roadmap_sync.yaml` presence file so the periodic scheduler discovers and runs the `roadmap_sync` workflow.
