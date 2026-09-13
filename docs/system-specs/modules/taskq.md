# Task Queue (`kiro_crew/taskq/`)

## Overview

`taskq` is the durable store and state machine under every unit of work the
gateway accepts and owes an outcome for. A subagent spawn is written to
`$KIROCREW_HOME/tasks/tasks.db` **before** its id is returned; a gateway that
crashes finds the row on restart, settles what the previous incarnation left
active, and dispatches what never started. Memory pressure defers a row instead
of refusing it. The in-memory spawn queue is a bounded window over the store's
rows, so 2000 accepted tasks are 2000 rows and at most
`agent.task_dispatch_window` Python objects.

The package is the single scheduling source of truth. TaskRunner's
`runs.json`, the workflow `RunRegistry`, and the subagent run folders
(`state.json`, `tombstone.json`, `result.txt`) remain **artifact and evidence
stores**, referenced from a row's `result_ref`; none of them is read to decide
what to dispatch. Design record:
[`docs/request-for-change/rfc-overload-resilience.md`](../../request-for-change/rfc-overload-resilience.md)
§3, §8, §9.

Files:

| Module | Owns |
|---|---|
| `model.py` | `TaskRecord`, the state vocabulary (`STATES`), the one validated `TRANSITIONS` table, `check_transition`, side-effect classes, lease/backoff constants. |
| `store.py` | `TaskStore`: open/journal selection, write-before-ack `accept`, atomic `claim`, generation-fenced writes, `cancel`, `defer`, `task_events`, the window reads. `TaskStoreUnavailable`. Network-filesystem detection. |
| `migrate.py` | Schema versioning (`SCHEMA_VERSION`, `apply_schema`) and the idempotent legacy import. |
| `reconcile.py` | `reconcile_on_boot`: settle every row a dead incarnation still owned. |
| `__init__.py` | `open_default_store(home)`: open, import, reconcile, in that order. |

Adapters (who writes rows today): the subagent manager, through the
`taskq_*` glue in `subagent_manager/admission/taskq_bridge.py` — see
[subagent.md](subagent.md) § Durable task queue. TaskRunner and workflows are
imported as rows but not yet dispatched from them (`awaiting_adapter`).

## Schema (v1, the only shape any build has written)

```sql
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,            -- stable; the subagent id / run id
  parent_id TEXT, root_id TEXT NOT NULL,
  session_key TEXT NOT NULL,      -- owning session; "" for cron/hook roots
  kind TEXT NOT NULL,             -- subagent | workflow_agent | taskrunner_step | chat_turn | cron | hook
  harness TEXT NOT NULL,          -- acp backend id, never a model id
  provider TEXT,                  -- provider lane (the model override for a subagent)
  params_json TEXT NOT NULL,      -- full spawn kwargs: enough to re-dispatch from the row alone
  workspace TEXT,
  scope_ref TEXT NOT NULL,        -- {memory_store, allowed_tools, approval_mode, app}: references, not grants
  state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,   -- dispatches so far (bumped by claim)
  next_run_at REAL,               -- NULL = eligible now
  lease_owner TEXT, lease_expires_at REAL,
  generation INTEGER NOT NULL DEFAULT 0, -- bumped by every claim and every cancel
  progress_json TEXT, result_ref TEXT, deadline_at REAL,
  idempotency_key TEXT,           -- UNIQUE when present (NULLs never collide)
  side_effect_class TEXT NOT NULL DEFAULT 'unknown',
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  wait_json TEXT,                 -- the WaitRecord while the row is in a WAITING state
  lane TEXT NOT NULL DEFAULT ''   -- fairness lane (root session key or 'system'); nested rows inherit the root's
);
CREATE INDEX tasks_parent ON tasks(parent_id, state);  -- wake-by-child lookup
CREATE INDEX tasks_lane ON tasks(lane, state, next_run_at, created_at);  -- per-lane dispatch heads
CREATE TABLE task_events (task_id, seq, ts, kind, data_json, PRIMARY KEY(task_id, seq));
CREATE TABLE meta (key PRIMARY KEY, value);   -- schema_version, incarnation
```

`task_events` is append-only. Kinds: `accepted`, `claimed`, `transition`,
`deferred`, `stale_result`, `rejected_transition`, `deliver`, `imported`,
`awaiting_adapter`, `wake`, `wait_updated`, `child_settled`. `apply_schema`
runs the one `CREATE ... IF NOT EXISTS` set above on every open (idempotent; a
missing index is regained) and stamps `meta.schema_version = SCHEMA_VERSION`
(`1`); there is no older shape to upgrade from, so the first `if current < N`
step lands only when the shape changes. It refuses a NEWER version.
`meta.incarnation` is the id of the process that last
opened the store; `TaskStore.previous_incarnation` exposes the one before it.

The store file and its directory are owner-only (`platform_compat`).
`params_json` carries the unredacted task text, exactly as the in-memory queue
entry did; it is written nowhere else.

## State machine

Fifteen states, one table (`model.TRANSITIONS`), consulted by every write.
`succeeded` in the owner's vocabulary is `done` here (`model.SUCCEEDED`).

```text
queued ──claim──▶ admitted ──▶ starting ──▶ running ──▶ done
  ▲                 │            │            ├─▶ waiting_children ──┐ (WAITING: wake ▶ running,
  │   admit wait    │            │            ├─▶ waiting_permission ┤  park ▶ retry_wait,
  ├─────────────────┘            │            ├─▶ waiting_dependency ┤  or recovering)
  │                              │            ├─▶ waiting_input ─────┘
  │                              │            └─▶ recovering ◀── (claim)
  │                              ▼                 │
  └── retry_wait ◀── waiting_infra ◀───────────────┘
 terminal: done | failed | cancelled | unknown_side_effect
```

| Set | States | Meaning |
|---|---|---|
| `CLAIMABLE` | `queued`, `retry_wait`, `recovering` | a claim may take the row (subject to `next_run_at` and an absent or expired lease) |
| `WAITING` | `waiting_children`, `waiting_permission`, `waiting_dependency`, `waiting_input` | a LIVE run yielded its lane slot; the runtime stays resident; a `WaitRecord` says why (see Waits) |
| `ACTIVE` | `admitted`, `starting`, `running`, `recovering` + `WAITING` | some incarnation owns the row (slot, runtime, or pending rebuild) |
| `TERMINAL` | `done`, `failed`, `cancelled`, `unknown_side_effect` | never regress |

`recovering` is in both `CLAIMABLE` and `ACTIVE`: a live owner holds its lease
while rebuilding the runtime; a dead owner's lease lapses and the dispatcher
re-claims it. Rules folded into the table:

- `cancelled` is reachable from every non-terminal state and beats everything.
- `failed` is reachable from every non-terminal state (auth re-validation, an
  agent name that stops resolving, a runtime that cannot be built).
- `done` and `unknown_side_effect` are reachable from every `ACTIVE` state,
  the `WAITING` states included: a run can finish before its `running` or
  wake write landed, and reconcile settles what the artifacts prove from
  wherever the crash left the row. `waiting_infra` and `retry_wait` hold no
  runtime and cannot have finished.
- A `WAITING` state leaves only to `running` (wake), `recovering`,
  `retry_wait` (park: the runtime was reclaimed) or a terminal; never straight
  to another wait -- one reason per record. `running → retry_wait` is the
  transient in-run failure.
- `unknown_side_effect` is terminal-pending: only a reconciling adapter that
  can query the external system moves it, and only to `done` or `failed`.
- `admitted → queued` is the expired admit wait (`agent.admit_wait_secs`) and
  the reconcile verdict for a claimed-but-never-started row.

A forbidden edge is never raised at the store boundary: `transition()` returns
`False` and appends `rejected_transition`, because a late writer trying to
regress a terminal row is an expected event, not a bug in the caller. An
unknown state NAME raises `InvalidTransition` — that is a typo.

### Side-effect classes

`side_effect_class ∈ {none, idempotent_key, unknown}` (default `unknown`).
`none` and `idempotent_key` rows whose owner died are re-dispatched
(`recovering` with backoff -- `model.recovery_backoff_secs` is the shared
recovery ladder's DEFAULT schedule from `recovery/policy.py`, bound at import:
deterministic because `next_run_at` orders rows (the dispatcher jitters on
wake), and pure, so it is the one wait that does NOT follow the
`agent.recovery_backoff_*` snapshot the process ladder takes at boot
([session.md](session.md) § Recovery ladder)); `unknown`
rows go to `unknown_side_effect`. Every terminal `transition()` and every
`cancel()` emits `kirocrew.taskq.completions{outcome=<state>}`. A
subagent run is `unknown` unless its caller says otherwise: its task is
arbitrary and may have pushed a PR. No blind replay of send/pay/submit.

## Write-before-ack

`TaskStore.accept(records)` commits every record in one transaction and
returns the ids only after `COMMIT`. Any failure — a locked database past
`BUSY_TIMEOUT_SECS` (2s), disk full, a duplicate id or idempotency key, a
schema this build cannot write — rolls the whole batch back and raises
`TaskStoreUnavailable`. A caller holding that exception has accepted nothing.
The subagent adapter turns it into a rejection with
`error_code="task_store_unavailable"`, which `POST /api/spawn` forwards as
`code`, and `spawn_run` reports as a task that failed to start. Items are
stored one row each; nothing is batched into a summary row.

Policy refusals — empty task, memory identity, cwd, governance — run BEFORE
accept and leave no row. From accept on, every exit either starts the row,
defers it, or marks it failed.

**Off the event loop.** `BEGIN IMMEDIATE` waits up to the busy timeout for
another connection's writer lock, so no store call may run ON the gateway's
loop thread from an async caller. The cost is worse than that timeout alone:
`TaskStore._lock` is ONE `RLock` held across the whole transaction including
the busy wait, and the writer thread is a second contender on it, so a
loop-thread call can queue behind the writer's entire transaction. Past
`dashboard.loop_stall_exit_after_secs` (25 s on the desktop) the watchdog dumps
stacks and kills the gateway.

`TaskStore.run(fn, *args)` executes a store method on the store's ONE dedicated
writer thread (`taskq-writer`) and awaits it. The discipline is diagnosed at
the connection accessor by a module-level `on_loop_db.OnLoopDBGuard` (label
`task store`), which is where a check has to live to catch the interprocedural
case `scripts/check_sync_io_in_async.py` cannot see -- an `async def` that calls
a plain helper that runs the query. Its switch is this surface's own
`taskq.store.STRICT_ON_LOOP_ENV` (`KIROCREW_STRICT_ON_LOOP_TASK_STORE`), NOT
`on_loop_db.STORE_STRICT_ENV`, so arming a task-queue test does not also arm the
knowledge store's tracked backlog; `dev_mode_arms_strict=False` for the reason
`knowledge/store.py` gives. Unarmed it is one throttled WARNING with a stack and
the call proceeds; armed it raises `OnLoopStoreError`. `guard.allow_on_loop()`
is the vetted per-block opt-out. `TaskStore.loop_thread_calls` counts on-loop
takes beside it, because a test asserting "this path took NO on-loop call"
needs a number rather than the absence of a log line.

`POST /api/spawn` therefore goes through
`SubagentManager.spawn_async`: `prepare_spawn` runs every policy gate and
returns a `PreparedSpawn` (id, params, the `TaskRecord`), the record is written
through `store.run(admission.taskq_accept_record, record)`, and only then does
the sync `spawn(**params, _preassigned_id=id, _store_accepted=True)` start the
run -- write-before-ack, with the write off-loop. The same shape serves every
other accept path: `continue_conversation_async` (the follow-up watcher
and `POST /api/subagents/continue`; the sync `continue_conversation` shares its
prelude, `_continue_prelude`), the app `SpawnSDK` (`apps/spawn_sdk.py`,
which awaits `spawn_async` when the manager has it), and the CHANNEL keyword
spawn door -- `spawn <task>` / `bg <task>` from Slack and `/spawn <task>` from
Telegram, which reach `messaging.commands.spawn_task_reply`. That helper and
`spawn_command_reply` are `async` for exactly this reason, and
`commands._spawn_off_loop` is the same `spawn_async`-when-present shape the
dashboard and the SDK use, so a channel handler on the gateway loop never takes
`BEGIN IMMEDIATE` itself. The `/api/tasks` reads run in `asyncio.to_thread`.

Reads are held to the same rule, because a queue-depth answer that counts rows
outside the window is a `count_pending` on the same connection.
`SubagentManager.queued_count_for_async` / `has_pending_work_for_async` (over
`admission.taskq_overflow_async`) serve the cron reset-deferral guards and the
chat slot-mode gate; `admission.taskq_pending_ids_for_async` serves
`cancel_for_parent`. Each snapshots the in-memory exclusion set on the loop and
runs only the `count_pending` / `list_pending` half on the writer thread, and the
sync entries stay for the sync callers. `cancel_for_parent` drains the in-memory
queue BEFORE that await, so its "queue entries are removed before the first
suspending await" guarantee still holds.

The three channel/dashboard sites reach those entries through a local probe
(`slack.gateway._subagent_work_pending` / `_subagent_queued_count` /
`_subagent_batch_pending`, `dashboard.chat_folders._subagent_work_pending`)
rather than calling them
directly, for the same reason `spawn_sdk` and `handlers.messaging._spawn_on_loop`
probe for `spawn_async`: the manager is duck-typed at those sites, and a double
without the async sibling must answer synchronously instead of having an
`await` land on a non-awaitable.

The same rule reaches the whole-wave reads. `admission.taskq_batch_pending_async`
(a `fetch_pending_by_batch` on the writer thread) is what
`SubagentManager.batch_members_pending_async` and the reaper's
`_sweep_stuck_waves_async` / `_sweep_digest_holds_async` take, and
`admission.lane_snapshot_async` serves `GET /api/spawn/lanes`: it reads the rows
per lane AND resolves a session key whose parent is no longer live
(`lane_for_session`'s `store.get`) in one `store.run`, then assembles the window,
the live runs and the scheduler balance on the loop. The dependency
coordinator's FIRST build reads every waiting row (`rebuild`), so a coroutine
takes `SubagentManager.dependency_coordinator_async` — the run loop's dependency
arms do, and the gateway builds it off-loop
(`_ensure_subagent_coordinator`) before each loop-side wiring pass reads
`_subagent_dependency_coordinator`. `monitoring.taskq_coordinator` re-raises
`OnLoopStoreError` out of its rebuild guard rather than reporting it as a failed
rebuild, so an armed run names the on-loop caller instead of silently restoring
nothing.

The classes that remain are ORDERING problems rather than un-offloaded work —
they are why `dev_mode_arms_strict` is still `False`. An un-offloaded READ is
never one of them: it is a defect, found with `TaskStore.loop_thread_calls` under
an armed pin (see `TestStoreOffLoop`), because a `try/except Exception` around a
caller swallows the guard's raise.

- the terminal write on an unwinding arm (`except CancelledError` /
  `except BaseException` in `taskrunner`, `workflows.agent_pool`, and both the
  `waiting_dependency` cancel and `_end_row_on_cancel` — `admit`'s
  `CancelledError` arm, for a row whose handle never reached the caller — in
  `adapters.runner`) stays SYNCHRONOUS, because an
  `await` there can be interrupted before the write is submitted and a dropped
  terminal write leaves the row active for the next boot's reconciler;
- `admission.taskq_cancel_queued`, reached from `_unqueue` on the Stop-all path,
  has to cancel the row before a drain can claim it AND drop it from the window
  before a stagger timer can start it — an await between those two is a race in
  either order, so closing it is a restructure, not an offload;
- the DRAINED spawn's `taskq_defer` write (`gate.py`'s `_deferred`, reached from
  the pump's `_dispatch_async_impl`) stays inline because its BOOLEAN is what
  tells a row that exists apart from a legacy in-memory `_queue` entry that
  never reached the store; `_from_queue` alone does not prove a row, so posting
  it would refuse the legacy entry instead of deferring the durable one. This is
  the one un-offloaded WRITE, and it is on the pressure path. Closing it needs a
  window-sourced signal threaded to the gate, not an offload.

**The pump is a coroutine on the loop.** `_drain_queue()` stays the sync entry
every call site uses; on a running loop with a store it schedules ONE
`_drain_queue_async` task (a request landing while one runs is coalesced into
one more pass). That coroutine runs the wait-expiry sweep
(`taskq_expire_waits_store` on the writer thread, `taskq_expire_waits_apply`
on the loop) and the window refill (`taskq_refill_window_async`: `pending_lanes`
/ `fetch_dispatchable_fair` / `next_eligible_at` through `store.run`, the
eviction and the append on the loop) BEFORE the sync pick-and-spawn half
(`_drain_queue_sync_impl`, whose *refill* callable is then a no-op). The timer
pump (`taskq_pump`) runs its expiry sweep the same way. Without a running loop,
or with `SpawnAdmissionCoordinator.pump_off_loop=False` (the test suite's root
fixture and the virtual-clock experiment driver, which settle with `sleep(0)`
loops), the pump runs inline on the calling thread; the off-loop path is pinned
by `test_overload_integration_glue.py::TestStoreOffLoop` with nothing stubbed:
the dispatch of a picked row is split at the claim (`spawn_impl(...,
_stop_before_claim=True)` returns a `ClaimPoint` once every gate passed AND
the slot is reserved -- running count and stagger token taken synchronously,
so a concurrent admission during the await sees the cap spent;
`claim_and_start` awaits `store.run(taskq_claim)` and re-enters with
`_claimed=`, which consumes the reservation instead of re-checking capacity,
and every non-start outcome releases it), the same split serves the ACCEPT
path -- `spawn_async` awaits the window decision (`taskq_should_window_async`)
and the claim on the writer thread and posts a pressure defer
(`taskq_defer_posted`), so the sync re-entry with `_store_accepted` performs
no store I/O; while its awaits are in flight -- and until a posted pressure
defer has LANDED (`await_pending_defer`), so the pump never sees the row before
its `next_run_at` is set -- the row is in `_admitting_ids`, which
`taskq_excluded_ids` adds so the refill cannot start it a second time. The
nested W3 branch (a child of a parent blocked in `spawn_sub_agents`) takes the
same route for event-loop callers: `taskq_child_registered_async` reads the
ledger's outstanding children and the parent's deadline on the writer thread,
yields the parent's slot on the loop, and posts `enter_wait` / `update_wait`
(`spawn_impl(_child_registration=False)` skips the inline branch). A re-entry
that raises releases the reservation like any other non-start
(`claim_and_start`'s `finally`, keyed on registration) -- the
registration and terminal writes (`taskq_mark`, `taskq_fail`, `taskq_settle`'s
`finish`) are POSTED to the writer thread through `_post_store_write` /
`store.run` -- the single worker keeps them in submission order, so
`admitted -> starting` lands before the run's later `running` -- and
`taskq_settle`'s tree propagation runs back on the loop after its write. The
queue-depth chip's `count_pending` and the child-reserve count
(`pending_children`, cached per pass by `refresh_pending_children_async`) read
on the writer thread too; the coordinator's one-time `rebuild` is done there on
the first pass (`ensure_coordinator_async`, also before the accept path's
re-entry). Every posted write and the pump task itself join `_report_tasks`
(`track_store_task`), the set `cancel_all` drains with a bounded wait, so a
terminal `finish` still on its way to the writer thread when the gateway stops
lands before the loop closes.

**A posted write and an inline write are not ordered against each other.** They
are ordered only WITHIN the posted set, by the single writer's submission order.
So a write whose precondition is another write must share its channel, and every
site on the wait/wake boundary is split explicitly into a DATABASE phase and a
loop-apply phase rather than dropping a whole coordinator into a thread:

- `yield_slot`'s `enter_wait` is POSTED, joining `taskq_mark` / `taskq_fail` /
  `taskq_settle`. The transition table accepts a wait only FROM `running` and
  the run's `running` mark is itself posted, so an inline wait write reaches a
  still-`starting` row on the run's FIRST frame batch, is refused, and the late
  mark leaves the row `running` with no durable wait reason
  (`test_runloop_integration.py::test_a_wait_in_the_first_frame_batch_lands_behind_the_running_mark`).
- The dependency path is the exception, and INLINE for a stated reason:
  `DependencyCoordinator.report`'s wait write chooses wait-vs-park from its own
  result in one critical section, so its `running` mark cannot be posted behind
  it. `_dependency_verdict` hands the mark decision into
  `_dependency_report_db`, where the mark and `report` run as ONE unit through
  `store.run` (inline when there is no off-loop pump, where program order
  already holds). Without it the wait is refused, the row is PARKED in
  `retry_wait` — which has an edge to neither `running` nor `done` — and the
  completed run's terminal write is refused too, leaving finished work
  claimable.
- `resume_grant` needs `wake_wait`'s new generation before it publishes, so it
  is awaited, not posted: the pump RESERVES the lane slot on the loop
  (`resume_reserve`, the same reserve-then-commit shape as `ClaimPoint`), the
  wake runs on the writer thread (`resume_grant_async`), and `_resume_publish`
  writes `_slot_released` / `_wait_record` / `_taskq_generation` and sets the
  resume event only from its result. `_resume_release` gives the reservation back
  on every other outcome.
- `DependencyCoordinator.tick()` runs through `store.run` from the monitoring
  loop, with only its `callbacks` on the loop.
- Child-terminal propagation is split the same way
  (`taskq_child_terminal_async`, `taskq_cancel_children_of_async`): offloading
  only the outermost `finish` and then reaching the `WaitLedger` from its
  callback puts the propagation's OWN writes back on the loop.
- The `RunnerAdmission` surface has async entry points for its event-loop
  callers: `accept_async` (the whole multi-statement accept as one unit),
  `claim_only_async`, and `Admitted.write_async` / `running_async` /
  `recovering_async` / `settle_async` / `done_async` / `fail_async` /
  `cancel_async`, plus `recorded_answer_async` / `consume_answer_async` for the
  operator-answer replay the step body drives. `admit` routes each of its own
  store touches through `_db`, and so do those two. A terminal write inside an
  `except CancelledError` arm stays SYNCHRONOUS on purpose: an `await` there can
  be interrupted before the write is submitted, and a dropped terminal write
  leaves the row active for the next boot's reconciler.

**A claim the store cannot take never starts a run.** `taskq_claim` returns
`(generation, proceed, reason)`; `TaskStoreUnavailable` during the claim is
`CLAIM_UNAVAILABLE` and `proceed=False`: the row stays `queued` on disk, the
caller keeps a QUEUED handle and the pump retries after the admit wait. Only a
row no store ever saw (legacy in-memory queue) proceeds at generation 0 -- a
run started without a lease would be invisible to reconcile and restartable
by the next pump.

**A corrupt file is quarantined, not obeyed.** `TaskStore.open` runs `PRAGMA
integrity_check`; SQLite's own damage verdicts (`file is not a database`,
`database disk image is malformed`, `malformed database schema`, a non-`ok`
check) move the file aside as
`tasks.db.corrupt-<utc-microseconds>Z-<pid>[-<n>]` -- the base name is reserved
by exclusive creation, so no quarantine ever overwrites an earlier one
(`test_two_quarantines_in_the_same_second_keep_both_copies`) -- with its
`-journal`/`-wal`/`-shm` sidecars moved first under the same name and the
database last (`test_stale_journal_is_quarantined_with_the_corrupt_file`); a
sidecar that cannot move is named in the warning, never a reason to raise.
The store then recreates the schema, logs once
at warning level and records the fact on `TaskStore.quarantined_to` /
`warnings`; `kirocrew doctor` reports any quarantined copy beside the live
file. Work accepted only into the old file is not recovered. Locked, busy,
disk-full, read-only, `unable to open` and a schema NEWER than this build stay
refusals (`_is_corruption` matches only those exact phrases), and doctor's
diagnostic open never moves anything.

**An enabled queue never falls back.** With `agent.task_queue_enabled` on, a
store that fails to open is recorded on the manager (`_taskq_unavailable`) and
EVERY spawn is refused typed (`task_store_unavailable`, naming the cause) --
never accepted into the in-memory queue a restart forgets. The runner adapters
are built with `require_store=True` for the same setting: `accept` and `admit`
raise `RunnerAdmissionRefused`; a failed `claim` raises too and starts nothing
(no generation-0 handle while the queued row stays dispatchable). The
lane-only, generation-0 shape exists ONLY when the queue is deliberately off.

**That refusal is not terminal, though.** Every condition kept as a refusal
instead of a quarantine -- locked, busy, disk-full, read-only -- is transient, so
a one-shot open would turn a two-second lock into a gateway that refuses every
spawn until someone restarts it. The reaper sweep therefore re-attempts the open
(`taskq_reopen_if_due`), on the shared recovery schedule
(`recovery/policy.py`'s `backoff_secs`: capped exponential backoff with equal
jitter, `agent.recovery_backoff_*` -- the same clock `dependency_backoff()`
uses), armed by whoever RECORDED the failure (`taskq_arm_reopen`, off the loop,
where the config is already in hand) so the delay runs from the failure rather
than from the sweep that notices it. An attempt is skipped while one is in
flight, before its deadline, and once a store is attached. The re-open is the
BOOT open repeated: schema, legacy import and reconcile all re-run through
`open_default_store` and the result attaches on the loop exactly as at boot,
which is safe because
- while the store is unavailable the manager holds no accepted work at all
  (every spawn was refused before queueing), so the process state at a retry is
  the boot state;
- the new incarnation's `reconcile_on_boot` examines rows not leased by it, and
  the `gateway.lock` singleton means no second gateway holds the same home,
  while `taskq_excluded_ids()` keeps the rows of runs live in this process --
  adopted orphans included -- out of the refill; and
- the store's consumers all bind it late (the runner adapters' `_store()`, the
  dependency coordinator's lazy build, the dashboard handlers' per-request read),
  so nothing needs re-wiring and the window is drained on attach.
A corrupt file is still quarantined at open rather than retried into, and the
attach is one-way: a re-open that finds no store never un-attaches one that
opened. Because the attempt re-reads the config, turning
`agent.task_queue_enabled` OFF while a refusal stands takes effect at the next
attempt (the in-memory queue) instead of at the next restart; turning it on
still needs a restart, since with the queue off there is no refusal to retry.

## Atomic claim, lease, generation

```sql
UPDATE tasks SET state='admitted', lease_owner=?, lease_expires_at=now+60,
                 generation=generation+1, attempts=attempts+1, next_run_at=NULL
WHERE id=? AND state IN ('queued','retry_wait','recovering')
  AND (next_run_at IS NULL OR next_run_at<=now)
  AND (lease_expires_at IS NULL OR lease_expires_at<now)
```

Zero rows affected means someone else has it (`claim()` returns `None`).
`claim_next(kind)` picks the oldest eligible row and claims it, retrying a
bounded number of times under contention. A claimed (`admitted`..`running`) row
is never re-claimed however stale its lease: a lapsed lease inside a live
process is a bug the reconciler surfaces, not a takeover.

Every write a run makes afterwards carries the generation it was dispatched
under (`transition`, `finish`, `record_progress`, `renew_lease`). A stale
generation is refused and recorded as `stale_result`, so a worker from
dispatch *n* can neither overwrite *n+1*'s outcome nor be reported twice.
`cancel()` bumps the generation as well as writing `cancelled`, which is what
makes cancel-vs-dispatch safe in either order: cancel first → the claim's
`WHERE state IN (...)` fails; claim first → the spawn's next fenced write
(`starting`) fails and the run is not started.

Lease: `LEASE_SECS` = 60. The subagent adapter renews at the `running` mark
(written at the run's first stream event); the runner adapters renew through
`Admitted.renew`. A `running` row is not claimable, so the lease is
load-bearing only once a row is `recovering` — periodic renewal from the run
loop is deliberately not added (the reaper sweep is `LEASE_SECS`).

## Bounded dispatch window

The store keeps no dispatch state in memory. The adapter's in-memory queue
(`SubagentManager._queue`) is a FIFO window of at most `TaskStore.window`
(`agent.task_dispatch_window`, default 64) entries:

- A newly accepted row joins the window only when there is room AND no older
  row is waiting outside it (`count_pending(exclude_ids=window ∪ {new})` is
  0). Otherwise it is store-only.
- The drain refills the window lane-fairly (see § Fairness lanes) at the
  start of every pump and after every pop: first the head of every lane that
  has a store row waiting but no window entry (a window full of one lane
  drops that lane's youngest entries back to store-only to make room, never
  below one entry per lane), then `fetch_dispatchable_fair(kind, limit=room,
  exclude_ids=window)` — weighted round-robin across lanes, oldest
  `created_at` first inside a lane, deferred rows skipped. FIFO therefore
  holds inside a lane across the window boundary, and every pending lane is
  represented in the window.
- Depth for a parent = window entries for that session + `count_pending(...,
  session_key=…)` outside the window; wave accounting consults
  `fetch_pending_by_batch` the same way.
- When nothing is eligible but rows wait on a `next_run_at`, the pump arms
  one `call_later` at the earliest of those (capped at `admit_wait_secs`).

## Fairness lanes (`lanes.py`; RFC §6, §13 Q5)

A **lane** is one root session's queue. `lane_key_for(session_key)` maps a
root: `cron:` / `cron_` / `hook:` / `webhook:` prefixes, `_hb`, `_bg` and the
empty key → `system`; any other key is its own lane. A nested row (a
subagent's child) inherits its parent's lane, which is the root's by
induction (`TaskStore._lane_in_tx`, inside the accept transaction); a row
whose parent is gone keeps its own `subagent:<id>` key rather than being
guessed into another lane. An explicit `params["lane"]` (the runner adapters
name the lane from the run's `source`) wins over derivation. The lane is a
column on the row, so `/api/tasks`, the health sampler and the dispatch
queries read it without a join.

Across lanes the dispatcher runs **smooth weighted round-robin**
(`LaneScheduler`): every pick adds each contending lane's weight to its
credit, takes from the lane with the most credit and charges it the total.
Equal weights are plain round-robin; weight 3 is three picks per round,
spread out. A tie on credit goes to the lane whose head has waited longest.
A lane with nothing pending is forgotten (`forget`), so a returning lane
starts even and is never owed a burst. Inside a lane the order is FIFO by
`created_at` — one session's own ordering is untouched.

Weights use `agent.lane_weights{lane: w}` (1..64), including `system`.
Every unlisted lane has weight 1.

Two store readers serve the dispatcher: `pending_lanes(kind, exclude_ids,
children_only)` → `{lane: eligible count}`, and `fetch_dispatchable_fair(kind,
limit, scheduler, exclude_ids, children_only, lanes, per_lane_limit)` — each
lane's oldest eligible rows (a window function, `ROW_NUMBER() OVER (PARTITION
BY lane ...)`) interleaved by the scheduler. `children_only` restricts the read
to nested rows (`parent_id` set): the rows allowed to take the reserved child
slot (see [subagent.md](subagent.md) § Fairness lanes and the child reserve).
The subagent manager keeps two scheduler balances — one for the window pick,
one for the store→window refill — so filling the window never spends the
credit the drain picks with. With one lane pending the fair order is exactly
`fetch_dispatchable`'s FIFO.

Metrics: a lane key is a session key and so never a metric attribute (the
`kirocrew.taskq.*` attribute sets are closed). Per-lane depth is exposed as
data instead: `GET /api/spawn/lanes` (`admission.lane_snapshot_async()`): per-lane
`queued` / `running` / `waiting` / `weight`, the scheduler credit, and the
`CapacityView` (`cap_total`, `roots_cap`, `running`, `child_reserve`,
`reserve_active`, `waiting_parents`, `lifted_from`). A closed
`lane_kind ∈ {system, session}` attribute on `kirocrew.taskq.depth` is left to
the health sampler.

## Deferral (memory pressure)

`defer(task_id, until, reason)` sets `next_run_at` on a claimable row and
appends `deferred`; the state does not change and the row holds nothing. The
subagent adapter calls it — instead of refusing — when
`check_memory_available` or `cached_admission_check` says no, with
`until = now + agent.admit_wait_secs`, and arms a pump wake-up for then. The
caller receives a `queued` id. Only when there is no store (the feature is
off, or the row is a legacy in-memory entry the store never saw) does pressure
still refuse, exactly as before. `agent.admission_gate=false` still turns the
posture tier off entirely.

## Journal mode and network filesystems

WAL, `synchronous=NORMAL`, `busy_timeout` 2s. `detect_network_filesystem`
reads `/proc/mounts` (Linux; longest mount-point prefix), `statfs`
(`f_fstypename`, `MNT_LOCAL`; macOS) or — Windows having no mount table — the
volume ROOT's drive type (`platform_compat.path_volume_is_remote`, which reports
a UNC path and a mapped network drive alike as remote and asks about the root
alone, so a disconnected drive costs no SMB round trip). A network mount gets
`journal_mode=DELETE` and a warning in `TaskStore.warnings`; a detector that
cannot see the mount table answers `None` and WAL is kept — which is also what
`DRIVE_UNKNOWN` / `DRIVE_NO_ROOT_DIR` and a failed query answer, since neither
establishes anything. A mapped drive onto a share on this same host reads as
remote and gets DELETE: the safe direction (slower, never a refusal), and
`agent.task_store_journal_mode=wal` overrides it.
`agent.task_store_journal_mode` (`auto` | `wal` | `delete`, RFC §13 Q6
reversal) is passed by `taskq_open` → `open_default_store(journal_mode=)` →
`TaskStore(network_fs=None|False|True)`: `auto` detects, the other two force
the mode and skip detection (a forced `delete` still warns, naming the key).
The store never refuses to open over the filesystem. `store.doctor_lines()` renders depth,
oldest wait and the warnings for `kirocrew doctor` (`cli_doctor.py` prints
them under the task-store section).

## Legacy import (`migrate.import_legacy`)

Runs on every open before reconcile; keyed on id, so a second boot inserts
nothing. Sources:

| Source | Rows | Notes |
|---|---|---|
| `<home>/subagents/<id>/state.json` without `tombstone.json` | `kind=subagent`, `state=recovering`, `attempts=1`, `result_ref=<folder>`, class `unknown` | the same set `list_orphans()` scans; a folder whose state names another id, or is unreadable, is skipped |
| TaskRunner `runs.json` entries with `status=="paused"` | `id=taskrunner:<task_id>`, `kind=taskrunner_step`, `state=recovering`, `scope_ref={"auto_approve": false}` | `runs.json` lives in the runner's work dir, so its path is a parameter of `open_default_store`; the persisted `auto_approve` is never carried |

Old files are never deleted or rewritten.

## Reconcile-first boot (`reconcile.reconcile_on_boot`)

Examines every `ACTIVE` row not leased by the current incarnation, once, before
any dispatch. Idempotent: a second pass over the same rows changes nothing, and
rows this incarnation has since claimed are skipped.

| Row | Verdict |
|---|---|
| terminal (incl. `cancelled`) | untouched — **cancelled never revives**, whatever the artifacts say |
| `artifact_probe(row)` says `done` / `failed` / `cancelled` | that terminal (subagent probe: `tombstone.json` cause `delivered→done`, `user_stop`/`cancelled→cancelled`, `error`/`timeout`/`turn_limit`/`child_escalation_limit→failed`; `gateway_restart` says nothing) |
| `admitted` | `queued` — claimed, never started, no side effect |
| kind without a recovery adapter (today: everything but `subagent`) | state kept, lease dropped, `awaiting_adapter` event |
| class `unknown` | `unknown_side_effect` |
| class `none` / `idempotent_key` | `recovering`, `next_run_at = now + backoff(attempts)` (`2·2^attempts`, cap 120s); the dispatcher re-claims it |

The reconciler deliberately still examines only `ACTIVE` rows: it is
kind-agnostic, and a CLAIMABLE row is normally its dispatcher's — a `queued`
`subagent` row is exactly what the window refill exists to pick up. The
`queued` rows of a kind with NO dispatcher are therefore the kind-scoped
adopter's half of the job (§ Runner adapters, `adopt_orphaned_rows`), and the
two are complementary rather than overlapping: the adopter also picks up the
row this table just requeued from `admitted`, which for such a kind would
otherwise sit claimable with nothing to claim it.

## Waits (`waits.py`)

Every pause is a wait with a reason (SPEC-ADDENDUM §1-3, RFC §14.1-14.3).
A live run that cannot progress enters a `WAITING` state carrying a
`WaitRecord` on the row (`wait_json`) and in `task_events`:

```text
WaitRecord {state, reason, since,
            resume_condition {kind: at_time|signal|children|input|permission, at, key, ids},
            dependency_scope, cancel_semantics: cancel_call|cancel_task|cancel_tree,
            evidence_source: execution_layer|liveness_oracle|dependency_adapter,
            checkpoint_ref, tool_call_id, deadline_at,
            slot_released=True, residency_charged=True}
```

`model_text` is a named evidence source precisely so it can be REFUSED: a
record built from it alone raises. `state` and `resume_condition.kind` must
agree (`children`↔`waiting_children`, `permission`↔`waiting_permission`,
`input`↔`waiting_input`, `at_time|signal`↔`waiting_dependency`).
`residency_charged` cannot be False on a record: reclaiming the runtime is a
separate act (`park`), never an edit.

Identity, execution quota and real resources are three things on every entry
([RFC §14.2](../../request-for-change/rfc-overload-resilience.md#142-yielding-identity-quota-and-real-resources-are-three-things));
here the quota is `SubagentManager._running_count` (decremented, pump run) and
the record on disk is the identity.

| Kind | Detected by | Stored | Released | Retained | Wake event | On failure |
|---|---|---|---|---|---|---|
| `waiting_children` (W3) | admission, from the parent's trusted in-flight tool (`_meta.kiro` `tool_name` ends in `spawn_sub_agents`) when a child registers or queues under `subagent:<id>` | row `wait_json{ids}`, `parent_id`/`root_id` on the children | lane slot | parent runtime residency | LAST awaited child terminal (`WaitLedger.on_child_terminal`) → `wake` (generation+1) → resume entry at the FRONT of the pump → slot granted by capacity | `on_child_failure` param: `continue` (default) wakes on the last child whatever its state; `fail_parent` → parent `failed{reason=child_failed}`, remaining children cancelled (children first); `deadline_at` → `failed{reason=wait_deadline}` + the live run cancelled |
| `waiting_dependency` (W2) | dependency adapter (`DependencySignal`, `dependency.py`) | `wait_json{dependency_scope, at_time|signal}` | lane slot | runtime residency until parked | `WaitLedger.signal(scope)` / `due_dependency_waits` → wake; the coordinator meters by capacity | coordinator caps / `deadline_at` → `failed`; `park` → `retry_wait` when the idle runtime is reclaimed |
| `waiting_input` (W4) | tool layer (interactive classifier / oracle `STUCK_INPUT`) | `wait_json{tool_call_id}` | lane slot | the blocked process | user input routed to the call → wake; `cancel_call` cancels the call, the task continues | `deadline_at` → `failed` |
| `waiting_permission` (W5) | approval broker | `wait_json{approval id}` | lane slot | runtime residency | approval decision → wake | rejection fails the call, task continues; `deadline_at` → `failed` |

`WaitLedger` (store-backed, stateless): `enter` (`running → state`, same
generation -- only correct behind a LANDED `running` write, which is why
`yield_slot` posts its `enter_wait` onto the same writer thread as the mark
rather than hoping about timing), `wake` (`→ running`, generation+1, lease
refreshed -- the
re-admission fence: a callback from the wait period carries the old generation
and is `stale_result`), `park` (`→ retry_wait`, the one write that ends the
residency charge), `fail`, `on_child_terminal`, `cancel_tree` (children first),
`expire` (deadlines → `failed`), `signal`, `rebuild` (boot: wake parents whose
awaited children are all terminal, cancel non-terminal children of a terminal
parent -- `orphaned_children`, fail past deadlines; idempotent). `store.cancel`
clears `wait_json`; every exit from a wait clears it.

**Re-admission never bypasses admission.** A wake writes `running` in the
store but the run holds NO slot until the pump pops its `_resume_id` entry
(`admission.request_resume` / `resume_grant`): capacity and the stagger apply,
so a dependency recovering for 500 waiting trees produces 500 rows eligible
for re-admission, not 500 simultaneous runtimes. Resume entries sit at the
front of the window because the run is already resident. Between the wake and
the grant the run is `_resume_pending`; `admission.resume_granted(id)` answers
whether it holds a slot again.

**A wake writes `running` only with the slot in hand.** `store.wake_wait(...,
to=)` / `WaitLedger.wake(..., resident=)` have two targets. `resident=True`
(`to=running`) is used by exactly one caller, admission's `resume_grant`, which
runs AFTER the pump reserved the slot for a run whose runtime is still resident
— and it is the PRECONDITION for the publish, not a write beside it. The
in-memory slot, the generation and the resume event appear only once that wake
landed. A store failure leaves the run parked on its bounded
`_await_lane_resume` timeout (the existing path for a wait that never woke) with
the reservation released; a row that is not waiting and not `running` (a
`retry_wait` park, a terminal row, a generation another incarnation moved past)
means the row went out from under this run, so the grant is refused too. The one
benign refusal is a row already `running`: its wait write was refused or is
still on the writer thread behind the wake, and `running` is the state a granted
resume wants anyway. A run with no row at all (the legacy in-memory queue) is
granted — there is no durable state to protect and refusing would strand a
resident run.
Every other wake -- a `waiting_input` answer, a dependency scope recovering for
a parked row, the boot `rebuild`, the runner's own waits -- lands in
`retry_wait` with `next_run_at = now`, no lease and a new generation: claimable
at once, so the dispatcher (or the runner's `_resume_after_wait` → `admit` →
`claim`) writes `running` when capacity is actually granted. A crash in the gap
leaves a claimable row (`retry_wait` is not ACTIVE, so the boot reconciler
leaves it alone) instead of a dead-owner `running` row that reconciles to
`unknown_side_effect`. For a LIVE parent whose last child ended,
`on_child_terminal(defer_wake=True)` records `children_settled` and leaves the
row `waiting_children` until `resume_grant`. `answer_input` stores the answer
text on the `wake` event so a crash cannot lose it: `waiting_input` reads the
RAM copy first and falls back to `recorded_answer` (the latest `wake` event's
`answer` not yet followed by an `input_consumed` event), and a step
re-dispatched by a NEW incarnation restores it the same way -- `execute_task`
appends the recorded answer to the step before its first attempt and marks it
consumed (`consume_answer`) -- but only once the step has durably COMPLETED
(`execute_task` records `input_consumed` with the `PASSED` result), so a crash
between applying the answer and the turn finishing leaves it replayable
(`test_taskq_answer_replay.py`). The
window refill excludes
every row with a live run in this process (`taskq_excluded_ids`), so a
momentarily claimable row never starts a second copy of a resident run.

**Terminal writes are retried, never forgotten -- and durable by reconcile.**
A pending terminal write (`defer_terminal_write`) keeps the row `running` under
this incarnation's lease and generation: nothing releases the lease, a running
row is not claimable, so no second copy starts while the write is owed. If the
process dies before `retry_terminal_writes` lands it, the row is a dead-owner
`running` row and the next boot settles it exactly like any crash mid-run:
`reconcile_on_boot` -> `awaiting_adapter` -> `adopt_orphaned_rows` (runner
kinds) -> `unknown_side_effect` for the default class, `recovering` /
re-dispatch for retry-safe work
(`test_taskq_runner_adapter.py::test_crash_during_a_deferred_terminal_write_reconciles_the_row`).
The pending write is therefore NOT persisted as its own event: the row's own
state plus the lease already say everything reconcile needs, and a second
record would only add a place for the two to disagree. `Admitted.settle` performs
the terminal `finish` FIRST; only a committed write (or a write fenced by a
newer generation, i.e. another owner ended the row) settles the handle. If the
store is unavailable the write is handed to `RunnerAdmission.defer_terminal_write`
and replayed by `tick()` (`retry_terminal_writes`) until it commits; the handle
is then settled and the lane slot released, but the task is NOT forgotten while
its write is outstanding (`stats()["pending_terminal_writes"]`).

**Nested S → A → B.** B waits (W2); A, blocked in `spawn_sub_agents`, is in
W3 with `ids=[B]`; S likewise. The chain holds zero lane slots. Siblings without
a dependency keep running. B ends → A wakes and is re-admitted; A ends → S.
Downward: a CANCELLED parent cancels its children (`taskq_cancel_children_of`);
a `done`/`failed` parent leaves them running (results delivered by id).
Upward: per the policy above. Restart: `parent_id` links and wait records are
rows; the reconciler settles the dead owner's `WAITING` rows like any ACTIVE
row (class `unknown` → `unknown_side_effect`, never a revival), then `rebuild`
cancels orphans.

## Dependency waits (`dependency.py`, `adapters/`)

An external dependency — the GitHub API, an HTTP host, a model provider —
refuses work for a while. Every entry point used to notice that on its own and
run its own retry loop, so five sessions hitting one GitHub rate limit produced
five backoff timers and five simultaneous retries when it lifted. Two things
replace that (RFC §14.4, SPEC-ADDENDUM §4):

### `DependencySignal`

The ONE shape every adapter translates a service error into:

```text
DependencySignal {kind, dependency_scope, source, retry_at: float?, retryable: bool, detail}
```

| `kind` | Meaning | Retryable | Example shapes |
|---|---|---|---|
| `dependency_unavailable` | down or unreachable | yes | HTTP 5xx, `ECONNREFUSED`, `could not resolve host`, model capacity rollout, IAM propagation delay |
| `rate_limited` | up, asked us to slow down | yes; `retry_at` honoured exactly | GitHub 403/429 with `X-RateLimit-Reset` / `Retry-After`, GraphQL `RATE_LIMITED`, Bedrock `ThrottlingException`, any HTTP 429 |
| `concurrency_exceeded` | too many in flight from us | yes | Bedrock `ServiceQuotaExceededException` |
| `quota_exhausted` | an allowance is spent until it resets | only with a known `retry_at` | `monthly usage limit`, `MonthlyLimitError` |
| `auth_failed` | credentials rejected or expired | **never** | 401, 403 (non-rate-limit), `bad credentials`, `AccessDeniedException`, session expired |
| `permanent_param_error` | the request itself is wrong | **never** | 404, 422, `could not resolve to a repository`, `Improperly formed request` |

`retryable` is forced `False` for the two terminal kinds and for a quota
exhaustion with no reset time. `dependency_scope` names the shared budget:
`github:api` (primary limit, per token, whole REST API), `github:graphql`,
`github:secondary`, `http:<host>`, `provider:<model>` / `provider:acp`. Every
task reporting the same scope waits on the same schedule; scopes never share
one.

`classify_exception(exc, scope)` runs the registered adapters in order —
`github` (headers, GraphQL `errors[]`, `gh` stderr wording), `http` (status
codes, `Retry-After` delay-seconds or HTTP-date, `X-RateLimit-Reset` epoch,
duck-typed over `urllib`/`aiohttp`/`httpx` error shapes) and `acp_provider`
(reuses `acp.client`'s own throttle / usage-limit / auth / 5xx patterns and
`_is_transient_raw_error`, so a third copy cannot drift) — and returns the
first match, or `None` when the error is not a dependency error at all. An
exception carrying a pre-attached `dependency_signal` wins outright.
`register_adapter(name, fn, first=False)` adds one; an adapter that raises is
skipped. The two GitHub monitors (`monitoring/github_pull_request.py`,
`monitoring/github_workflow_run.py`) map `adapters.github.parse_gh_stderr()`'s
category onto `ProviderErrorKind` instead of each carrying its own parser.

### `DependencyCoordinator`

One `ScopeSchedule` per `dependency_scope`; the rules:

| Rule | Behaviour |
|---|---|
| one schedule per scope | the first `report(task_id, signal)` creates it; later reports JOIN it (no second timer). A later server-stated `retry_at` extends it; an earlier one never shortens it |
| honour the server | `retry_at` (`Retry-After` / `X-RateLimit-Reset`) is used exactly and is not clamped by the backoff cap. A RECORDED server instant (`ScopeSchedule.server_retry_at`) still ahead of now is a FLOOR under every later recomputation for that scope: a headerless failure or an earlier server statement can only leave it where it is. A ramp batch fails item by item and only some members carry a header, so without the floor one headerless 503 would discard the reset a sibling's 429 stated and retry against a dependency that said it would refuse. Two exits only: the instant passing (after which the ladder resumes normally) and `recovered(scope)` |
| bounded backoff | otherwise the shared recovery schedule (`recovery/policy.py` `LayerPolicy.backoff_secs`: `raw = min(agent.recovery_backoff_max_secs, base·2^(attempts-1))`, `delay ~ U[raw/2, raw]` — equal jitter; `dependency.dependency_backoff()` builds it from `RecoveryPolicy.from_config`) — a dependency wait has no backoff keys of its own |
| one attempt per scope | when the scope is due, exactly ONE waiter is woken as the probe (`phase=probe`). A fresh report from the scope while a probe or ramp batch is in flight is the probe FAILING: `attempts += 1`, new backoff (never earlier than an unexpired server `retry_at`), back to `waiting` — not one attempt per woken task |
| staged wake by capacity | `wake_spacing_secs` after a probe that did not fail (or as soon as the probe completes, via `forget(task_id)`), the scope ramps: `wake_per_tick` waiters (0 = the current effective admission capacity) per spacing until none remain. A wake never bypasses admission — a live waiter goes `waiting_dependency → running` under a NEW generation (`WaitLedger.wake`) and re-enters the lane queue; a parked one goes `retry_wait → queued` with `next_run_at = now` for the dispatcher to claim |
| never infinite | `attempts > agent.dependency_max_attempts` or `now − since > agent.dependency_wait_deadline_secs` fails every waiter in the scope with the reason (`dependency_failed` event, `failed` row) and drops the schedule |
| terminal signals | `permanent_param_error` and a reset-less `quota_exhausted` → `failed` at once; `auth_failed` → `waiting_input` (`WaitRecord.input("auth:<scope>")` — signing in is real user input) when the row is running, else `failed`. Never scheduled, never retried |
| fault isolation | `tick()` handles each due scope independently; a throttled scope never delays another scope's wake, and a scope's failure fails only its own waiters |
| `recovered(scope)` | an external recovery signal makes the scope due now; the wake is still staged (probe, then batches) |

Where a waiter is parked: a LIVE run (`running`) enters `waiting_dependency`
with `WaitRecord.dependency(scope, since, retry_at, deadline_at = since +
wait_deadline)` — the row keeps its runtime resident and releases its lane
slot (W2 in RFC §14.1); a row that is not running yet (`starting`) is parked in
`retry_wait` with `next_run_at` = the scope DEADLINE, deliberately not the
retry instant, so the dispatcher cannot pick it up on its own and bypass the
staged wake (that eligibility is the safety net for a process that dies before
`rebuild()` runs).

Persistence: the schedule is in memory; every entry appends
`task_events(kind="dependency_wait", {signal…, retry_at, server_retry_at,
attempts, since, state})`, every wake `dependency_wake` and every give-up
`dependency_failed`. Both instants are the SCHEDULE's as of that report, not the
reporter's signal: `server_retry_at` is the scope's recorded floor (`null` when
it has none), which is why it is a key of its own and never folded into
`retry_at`.
`rebuild()` after a restart re-joins every `waiting_dependency` row (scope,
since and `retry_at` from its `WaitRecord`, attempts from its newest
`dependency_wait` event) and every `retry_wait` row whose newest
`dependency_wait` is newer than its newest `dependency_wake`/`dependency_failed`;
a scope's `retry_at` / `attempts` / `since` are the latest / max / earliest
across its waiters, so a scope that was mid-backoff resumes it instead of
retrying at once. The floor is restored the same way (latest across the
waiters), so a restart cannot turn an authoritative deadline back into a ladder
delay; a row written without the key restores no floor rather than failing the
rebuild, and a restored instant that has already passed is no floor either, so
the floor can never outlive the reset it states. `next_deadline()` tells the
pump when to call `tick()`.

`coordinator_from_config(store, agent_config, capacity=..., on_wake=...)`
builds it from the `agent.dependency_*` keys.

**Run-loop wiring (X1).** The subagent manager owns ONE coordinator (`subagent_manager/monitoring.py::taskq_coordinator`, built lazily from `agent.dependency_*` over the admission store, `capacity = manager._max_concurrent`, registered via `register_coordinator`; `current_coordinator()` / `shared_retry_at(scope)` are the read-only accessors for callers with no task row, e.g. the main chat). Two seams distinguish a LIVE run from a parked row on wake: `wake_through(task_id, generation) -> bool` is asked before the store wake — `True` means the manager owns a yielded live run and has queued its `request_resume`, so the coordinator writes only the `dependency_wake` event (`via: admission`) and admission's `resume_grant` performs the `wake_wait` under the run's own generation (one wake, one write, no stale-generation fence); `False` keeps the row path (`ledger.wake` for a waiting row, `retry_wait -> queued` for a parked one, then `on_wake`). `on_fail(task_id, reason)` is told about every waiter a deadline or the attempts cap fails from `tick()` / `report()`, so a run parked on its resume event ends instead of waiting for a grant that will never come. The pump (`taskq_pump`) calls `tick()` at `next_deadline()` via a one-shot timer, from every reaper sweep, and whenever a run parks; `forget()` is called when a run reaches a terminal state. `admission.yield_slot(..., persist=False)` is the lane-slot release for a wait the coordinator already wrote. The sub-agent run's stop recovery uses the SAME wait shape without the coordinator: `WaitRecord.dependency("session:<stop class>", source=liveness_oracle)` + `request_resume` (see [subagent.md](subagent.md) § Stop reason → state); the gateway-capacity L1 rung joins the coordinator on scope `mcp_gateway:<class>` with the ladder's delay as `retry_at`.

## Runner adapters (`adapters/runner.py`)

TaskRunner steps and workflow `ctx.agent()` calls run on `SessionManager`
sessions, not on `SubagentManager.spawn`, so they cannot share that code path;
this module gives them the same contract over the same store.

| Piece | What it is |
|---|---|
| `lane_for(session_key, source)` | `lanes.lane_key_for(session_key)` (the ONE lane-key policy) plus the runner's launch `source`: a `cron` / `hook` root is `system` whatever its key says. Stored on the row as `params.lane` and honoured by `_lane_in_tx`. |
| `owner_of(task_id)` | `(owner, run_id)` for a runner row id (`taskrunner:<run>[:task<N>]` → the TaskRunner run; `workflow:<run>:agent<N>` → the workflow run); the `/api/tasks/{id}/cancel` adapter routes on it: a parked row ends through `cancel_wait`, a row whose runtime is live (`starting`, `running`) cancels its run (`TaskRunner.cancel` / `WorkflowService.cancel`) -- a step is one unit of its run. |
| `RunnerLane(cap, mode=, pinned=)` | FIFO execution gate bounded by the LIVE effective cap. `cap` is a callable read on every decision (the factory passes `SubagentManager.max_concurrent`, already the controller's clamp under the user ceiling), so the lane always SEES the current cap — but reading it is not a wake. `pump()` is the raise EDGE, called from `SubagentManager._notify_cap_raised` through the `set_cap_raise_listener` handle the gateway installs, at BOTH the manager's raise sites (`set_effective_cap`, `apply_limits`). It has to be pushed: a waiter parked while the cap was `0` holds no slot, so no `release()` of its own will ever come, and `RunnerAdmission.tick` does not pump. `pump` grants exactly the FIFO prefix the new cap allows and nothing at all while the effective cap is still `0`, so a pause keeps pausing. `set_effective_cap(n \| None)` is the lane-local override (it pumps too); `mode=fixed` pins the bound at `pinned` (or the ceiling) whatever the controller says. Waiters hold a future, nothing else; no asyncio primitive exists at import or construction |
| `RunnerAdmission(store, lane=, pressure=, admit_wait_secs=, clock=, sleep=, ladder=, coordinator=)` | `accept(kind, task_id, ...)` writes the row before returning it (a `TaskStoreUnavailable` is `RunnerAdmissionRefused`: nothing accepted); an existing claimable / waiting row is returned, not duplicated (that is how a resume re-attaches) with `params.accepted_by` re-stamped to THIS incarnation (`TaskStore.restamp_accepted_by`) so the queued-orphan sweep cannot cancel a row the returning `admit` is about to park on, a terminal one gets a `~N` suffix. `admit(task_id)` runs the subagent order minus the policy refusals: pressure (`resource_status.cached_admission_check` shape) → `store.defer` + wait, a lane slot, `store.claim` → `starting`; a row cancelled meanwhile raises `RunnerTaskCancelled`. A CALLER cancel delivered while `admit` waits (the pressure sleep, the lane slot, a deferred row's re-check, the `starting` write) is `admit`'s OWN to clean up: the lane slot goes back and the row is ENDED `cancelled` before the `CancelledError` is re-raised, because nothing ran under it and these kinds have no dispatcher — a row left claimable there would hold a queue place for as long as the store lives. Only two rows are its to end: one this call claimed itself (the generation it took fences the write) and one still `queued` and unleased. `retry_wait` / `recovering` are excluded — the first can carry the operator's persisted answer, the second an attempt count, and `accept` re-attaches both on resume. `yield_dependency` owns its cancel the same way (the awaiting coroutine is the wait's only wake path in this incarnation); `waiting_input` deliberately does not. `claim_only` claims a container row without a slot. With no store the lane is the whole admission (legacy behaviour, never a refused run) |
| `Admitted` | the claimed row's handle: `running` / `progress` / `renew` / `settle` (`done` / `fail` / `cancel`, once; releases the slot), `recovering(reason, delay_secs)` (`running → recovering`, `next_run_at`, slot released) + `reclaim()` (a fresh claim = new generation; late writes are `stale_result`) |
| waits | `yield_dependency(handle, signal)`: coordinator `report` when one is attached (the scope's ONE schedule), else `WaitRecord.dependency` with `retry_at`; slot released, session resident; `tick()` / `signal(scope)` / the coordinator's `on_wake` resume it through capacity, and a `retry_wait`-parked row is re-claimed. Returns False when the wait ended terminal. `waiting_input(handle, tool_call_id)` ↔ `answer_input(task_id, text)` / `cancel_wait(task_id)` |
| `decide_recovery(handle, unit, reason)` | the ladder's L3 verdict for one stalled turn (`None` without a ladder); `recovered(unit)` clears it |
| `adopt_orphaned_rows(store, kinds=, resume=)` | the recovery adapter for the kinds the boot reconciler stamps `awaiting_adapter`: `admitted → queued`; an unleased ACTIVE run row that `is_safe_retry` (`params.safe_retry`, or class `none` / `idempotent_key`) → `recovering` + `resume(rec)`; not safe → `unknown_side_effect` (never re-run blind); step rows (`parent_id` set) → `failed` when safe (the run's resume re-runs them from its checkpoint) else `unknown_side_effect`; rows this incarnation still leases are skipped. It ALSO settles the `queued` rows of its kinds that a previous incarnation accepted and never claimed → `cancelled` ("never started"), counted in `AdoptReport.cancelled`: nothing ran under such a row, so `unknown_side_effect` would be a lie, and a resume is a new row anyway. The owner test is `params.accepted_by` (the accepting `TaskStore.incarnation`), NOT the lease — a queued row has never been claimed and so has no lease, and a row this incarnation just accepted, or RE-accepted (`accept` re-stamps it), is waiting for a lane slot inside a live `admit`. `retry_wait` / `recovering` are left alone (persisted answer, attempt count, and `recovering` is ACTIVE so the reconciler already sees it) |
| `runner_admission_for(manager, cfg=, ladder=, coordinator=)` | the gateway's one-call factory over a `SubagentManager`: store getter `manager._taskq`, lane ceiling `manager.max_concurrent`, mode `agent.adaptive_concurrency_mode` (live), defer interval `agent.admit_wait_secs`. The store getter is LIVE and catches up on its own; `coordinator` is a plain field, which is why the late `attach_coordinator` below exists |

The boot reconciler deliberately keeps these kinds OUT of its adapter set: it
would settle a class-`unknown` run row as `unknown_side_effect` before the
runner could read `params.safe_retry` and resume from its checkpoint. It drops
the dead lease and stamps `awaiting_adapter`; `adopt_orphaned_rows` is the
adapter, run by `TaskRunner.attach_task_admission` (`taskrunner_step`) and
`WorkflowService.attach_task_admission` (`workflow_agent`, always settled --
a workflow run restarts through its own registry, never from one call's row).
Both arm the sweep only on the attach that already HAS a store, so the pass the
gateway makes before the store finished opening adopts nothing and the
store-ready pass adopts exactly once -- two concurrent sweeps over one set of
rows would let each settle a row the other is resuming.

Gateway wiring is TWO passes over ONE admission (`slack/gateway.py`).
`_wire_runner_admission` runs while the dashboard socket is being bound, so both
consumers hold the admission -- and therefore the typed
`task_store_unavailable` refusal -- from the moment they can serve; it also
installs the lane's raise edge on the manager
(`SubagentManager.set_cap_raise_listener`, unconditional: the lane's ceiling is
the manager's cap whether or not a coordinator exists).
`_runner_admission_store_ready`, awaited from `run()` between
`wait_taskq_ready()` and the adaptive controller, is the second pass and is
IDEMPOTENT: it `attach_coordinator`s + `subscribe`s only when the first pass had
no coordinator, re-reads the rows with `rebuild()` right AFTER the attach (a
wait parked through the ledger between the coordinator's own build and the
handover is in neither schedule, and an attached coordinator stops `tick`
scanning the ledger), drops the `_runner_admission_tick` fallback, and re-runs
the adoption attach only if no pass has yet carried a store. A boot that never
lost the race passes straight through it. `agent.task_queue_enabled=false` (or a
store that never opens) keeps the fallback tick as the steady state.

Consumers: `taskrunner.md` § Durable task queue (run + step rows, adoption),
`workflows.md` § Agent execution adapters (`admitted_agent_fn`) and § Gateway
wiring (the ONE `RunnerAdmission` the gateway attaches to both, subscribed to
the manager's coordinator through `DependencyCoordinator.subscribe(on_wake=,
on_fail=)`). Row ids: `taskrunner:<run>` / `taskrunner:<run>:task<N>` /
`workflow:<run>:agent<N>`. `POST /api/tasks/{id}` (`answer_input` /
`cancel_wait`) is the operator's lever on a runner row's `waiting_input`, and it
is WAIT-only: a row whose runtime is live is refused there (409 `not_waiting`)
because ending a wait cannot stop an executor
(`learn-cron-dashboard.md` § Tasks & capacity). Pinned by
`test/test_taskq_runner_adapter.py`.

**Every accepted row ends with an owner, a wait, or a terminal state.** The two
ways one could have neither, and what closes each: a caller cancel between
`accept` and the claim (closed inside `admit` / `yield_dependency`, above), and a
restart between them (closed by the adopter's `queued` sweep). Pinned per path —
`test_taskq_runner_adapter.py` (queued cancel, cancel after the grant, cancel
after the claim, a row another owner claimed, no-store, dependency-wait cancel;
adopt over accept-only + admit-only rows, twice, and the three rows it must NOT
touch: this incarnation's live `queued` row, a `retry_wait` row holding an
answer, any `subagent` row), `test_workflows_agent_pool.py` and
`test_taskrunner_taskq.py` (the same cancel and restart cases end to end through
`admitted_agent_fn` and `_execute_single_task`).

## Configuration

### Reverting to pre-queue behaviour

Three flags, all live, and the section is explicit about their LIMIT because a
default-on rewrite of core spawn semantics is only safe with an accurate rollback
story. `agent.task_queue_enabled=false` restores the in-memory `_queue` dispatch
(`tasks.db` stays in place, unread); `agent.adaptive_concurrency=false` stops the
controller moving any cap; `agent.adaptive_concurrency_mode="fixed"` pins both
actuators as plain semaphores (the execution cap at
`min(agent.adaptive_initial, max_subagents)`, the daemon's spawn gate at
`mcp_gateway.spawn_concurrency_initial`).

**What the three flags do NOT restore.** An operator who sets all three does not
get pre-queue behaviour, because these are new bounds and new results that no
flag reverts:

- The gatewayd spawn gate still bounds concurrent backend spawns at
  `mcp_gateway.spawn_concurrency_initial` (4 by default). Before this change a
  private/exclusive backend was bounded by nothing, so this is a NEW ceiling that
  the fixed mode pins rather than removes. `mcp_gateway.spawn_concurrency_max`
  raises it; nothing returns it to unbounded.
- `agent.session_start_concurrency` bounds concurrent `session/new` calls, and
  `agent.interactive_command_policy` decides what happens to a tool call that is
  waiting on a human at a terminal. Both are new, both are outside the set above,
  and each has its own key.
- `spawn_sub_agents` no longer reports a wait expiry as `timed_out`; it returns
  `still_running` with per-agent state. That change is UNCONDITIONAL -- a caller
  parsing the old value sees the new one with the queue disabled.
- The recovery ladder (`recovery/`) and the daemon's host budget
  (`mcp_gateway/host_budget.py`) have no off switch: each REPLACES the code it
  supersedes (the per-layer ad-hoc retry loops; the unbounded per-key fork), so
  there is no earlier path to switch back to -- the RFC's rule for such a change
  is "reversal: none by flag; a different design is a new RFC" (§13).

| Key | Default | Live? | Meaning |
|---|---|---|---|
| `agent.task_queue_enabled` | `true` | yes | `false` keeps the in-memory queue for one release; `tasks.db` stays in place, unread |
| `agent.task_dispatch_window` | `64` (1..4096) | restart | bound on in-memory queued entries |
| `agent.task_store_journal_mode` | `auto` (`auto` \| `wal` \| `delete`) | restart | SQLite journal for `tasks.db`: `auto` = WAL locally, DELETE on a detected network filesystem; the other two force one. Unknown values read as `auto` |
| `agent.admit_wait_secs` | `30` (1..3600) | restart | admitted → queued after this; also the deferral re-check interval |
| `agent.start_collect_timeout_secs` | `300` (10..3600) | restart | reserved for the session-start gate's start collector |
| `agent.dependency_max_attempts` | `20` (1..1000) | yes | coordinated probes a scope gets before its waiters fail |
| `agent.dependency_wait_deadline_secs` | `3600` (0..86400) | yes | wall-clock ceiling on one dependency wait; 0 = attempts cap only |
| `agent.dependency_wake_per_tick` | `0` (0..4096) | yes | waiters released per wake tick after the probe; 0 = current effective admission capacity |
| `agent.dependency_wake_spacing_secs` | `1.0` (0..60) | yes | pause between staged wake ticks |
| `agent.lane_weights` | `{}` (values 1..64) | yes | per-lane weights keyed by root session key or `system`; unlisted lanes weigh 1 |
| `agent.child_reserve` | `1` (0..8) | yes | slots a depth-0 start may never take while a nested row or a resume waits for a slot; also lifts an adaptive squeeze to `adaptive_floor + child_reserve` while a parent waits (never above `max_subagents`) |

## Invariants (pinned by tests)

- `test_taskq_state_machine.py`: 15 states, every edge in one table, terminals have no exits (except `unknown_side_effect→done|failed`), cancel from every non-terminal state, record round-trip.
- `test_taskq_store.py`: `apply_schema` is one shape at `SCHEMA_VERSION` (fresh file has `wait_json`, `lane`, both indexes) and a second apply changes nothing; a newer-stamped file is refused; ids only after commit; an injected write failure raises `TaskStoreUnavailable` and commits nothing (not even the batch's first row); duplicate id / idempotency key refuse; locked past busy timeout refuses rather than hangs; network FS → `delete` journal + warning; defer keeps `queued` but ineligible.
- `test_taskq_claim_fencing.py`: two real threads on separate connections, 40 rows → each row won exactly once; stale generation `finish` is a no-op with a `stale_result` event; terminal never regresses; duplicate completion no-op; cancel before/after claim and during run all end `cancelled`.
- `test_taskq_reconcile.py`: crash at queued / admitted / running / result-written-not-acked → nothing lost, terminals kept, `admitted→queued`, class-driven recovery, cancelled never revives, idempotent; legacy import fields and idempotency.
- `test_taskq_admission_integration.py` + `test_subagent_scale.py::TestDurableQueueScale`: real admission + fake worker; 2000 submissions → 2000 rows before any id returned, window ≤ 64 throughout, 2000 unique `done`, depth 0; FIFO across the window boundary; store-only cancel never starts; restart with a fresh manager re-dispatches queued rows; `task_queue_enabled=false` leaves no `tasks/` directory.
- `test_fairness_lanes.py`: `lane_key_for` mapping; smooth WRR is round-robin at equal weights, spreads a weight-3 lane, ties go to the oldest head, FIFO inside a lane; accept derives root / `system` / inherited lanes and never guesses an orphan's; `fetch_dispatchable_fair` interleaves lanes (and is FIFO with one lane), `children_only` and `pending_lanes`; on the real manager: 200 rows from one session, one row from another and one cron root at cap 1 → the small lane and `system` each start within three grants while the big lane stays FIFO; `lane_weights` 3:1 gives 6:2 of eight grants; `lane_weights["system"]=2` gives 4:2 of six; the child reserve on a three-level tree at cap 2 (children and resumes take the last slot, roots wait, roots fill the cap again once nothing nested is pending); a waiting parent whose child runs reserves nothing; `child_reserve=0` disables the rule; an adaptive cap of 1 is lifted to 2 for the child while a parent waits, roots still see 1, and the lift ends with the wait; no lift for a non-adaptive cap; the lift never exceeds `max_subagents`; settings clamp; no checkpoint-pause flag exists (RFC Q3); `wait_resume_granted` is immediate for a run holding its slot, times out for a yielded one, and is released by the grant event (one event per wait); `GET /api/spawn/{id}/resume` reports, holds and releases on the grant; the MCP hold re-asks after each held request, releases on `granted`, on `known=False`, on an error payload and for a chat-turn parent, and names `resume_pending` only after it observed an ungranted slot at the deadline; `spawn_sub_agents` holds until the second answer says granted and still returns the children's results.
- `test_taskq_waits.py`: each wait kind's record fields and round-trip; `model_text` alone refused; entry keeps the generation and the lease, is fenced, and refuses wait→wait; wake bumps the generation, clears the record and fences old callbacks; park ends residency into `retry_wait` re-claimable at `next_run_at`; cancel semantics per kind (tree children-first with done siblings untouched); deadline → `failed{wait_deadline}`; `signal` wakes one scope only.
- `test_taskq_nested_propagation.py` (real admission, fake worker): 3-level tree holds zero slots while unrelated work completes; one slot + waiting parents still progresses to completion; wake on the LAST child (`child_settled` events); two waking parents re-admitted one at a time; `continue` vs `fail_parent`; parent cancel cascades to live and store-only children; wait deadline fails the parent; restart preserves links, revives nothing, cancels the orphaned queued grandchild; `rebuild` wakes a parent whose children finished; a `spawn_run` (non-blocking) parent keeps its slot; yield/resume idempotent and generation-fenced.
- `test_dependency_signals.py`: each adapter maps its real error shapes (GitHub 403 rate limit + `X-RateLimit-Reset`, 429 + `Retry-After` delay and HTTP-date, GraphQL `RATE_LIMITED`, `gh` stderr; generic 429/503 with host scope; Bedrock throttle / usage limit / auth / model-unavailable via `AcpError`); `Retry-After` and `X-RateLimit-Reset` become an exact `retry_at`; auth and parameter errors are terminal whatever the adapter said; pre-attached signals win; a raising adapter is skipped; the monitors' `_classify_cli_error` answers exactly what the shared parser does.
- `test_dependency_coordinator.py`: five tasks on one scope → one schedule, one probe at `retry_at`, and a probe failure costs the scope ONE attempt; two scopes: one throttled, the other's waiter completes; staged wake = probe, then capacity-sized batches per spacing, never all at once; a server `retry_at` is honoured exactly and only extends, and an unexpired one floors the backoff a headerless probe or ramp failure would otherwise shorten (the published `shared_retry_at` included), with expiry and `recovered` as its only exits, so a scope keeps its attempts for after the stated reset instead of failing its waiters early; jitter stays in `[0, min(cap, base·2^(n−1))]`; attempts cap and wall-clock deadline fail every waiter with the reason; `auth_failed` → `waiting_input`, `permanent_param_error` → `failed`, neither scheduled; a fresh coordinator's `rebuild()` restores the schedule, its waiters and the server floor from the rows and events, so a restart mid-throttle keeps the stated reset (a row written without the floor key restores no floor, and a restored floor still expires into the ladder); a `starting` row parks in `retry_wait` with `next_run_at` = the scope deadline and wakes to `queued`.
- `test_taskq_startup.py`: the open is off-loop and every spawn is refused typed until it attaches; a failed open keeps that refusal immediately after startup; a TRANSIENT failure (`database is locked`) is re-attempted and the store then attaches, `taskq_accept` going from the refusal reason to a committed row; the reaper sweep is what asks; the delay is `RecoveryPolicy`'s equal-jitter step over `agent.recovery_backoff_*`, nothing is armed before its deadline, and a re-open never un-attaches an open store.
