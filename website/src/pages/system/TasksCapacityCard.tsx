/**
 * Tasks & capacity — the durable task queue and the concurrency it runs under.
 *
 * Answers the questions the Sessions plane cannot: how much accepted work is
 * WAITING for capacity (and for how long), what the effective concurrency is
 * right now against the ceiling the user configured, why it was lowered, and
 * which live runs have yielded their slot and for what reason (children,
 * permission, dependency cooldown, real user input) or are being recovered.
 *
 * Read from `/api/tasks/summary` (`dashboard/handlers/tasks.py`), which folds
 * the task store, the structured session health and the adaptive controller
 * into one payload. Polled at the same cadence as the spawn panels: the store
 * read is cheap and the numbers move on the scale of seconds when a burst
 * lands.
 *
 * Purely additive to the Services plane: the card composes the plane's own
 * `Card` / section-row shape and the design tokens; it introduces no new
 * theme variables. It self-describes when the gateway runs without a task
 * store rather than rendering zeros as if the queue were empty.
 */
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Card, CardTitle, Badge } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import InfoTip from '../../components/InfoTip'
import { api } from '../../api/client'
import type { LaneCap, TaskRow, TasksSummary } from '../../api/tasks'
import { fmtDuration, fmtNumber, type FormatUnit } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { sessionChatPath } from './sessionRows'

/** Same cadence as the spawn/activity panels (`/api/spawn` every 5s). */
export const TASKS_SUMMARY_REFETCH_MS = 5_000
/** Rows shown in the waits list before it folds into "N more not shown". */
const WAIT_ROWS_SHOWN = 8

/**
 * Catalog key per wait/health classification. A flat map of FULL literal keys
 * indexed inline at the `i18nT()` call, so `check-i18n-keys.mjs` validates the
 * union of the values; a key assembled from the wire's state string would be
 * invisible to that gate and render its raw dotted path the day a state is
 * renamed. Unknown states fall back to the raw token (a fact, not copy).
 */
const STATE_LABEL_KEY: Record<string, string> = {
  queued: 'pages.tasksCapacityCard.state_queued',
  admitted: 'pages.tasksCapacityCard.state_queued',
  starting: 'pages.tasksCapacityCard.state_running',
  running: 'pages.tasksCapacityCard.state_running',
  waiting_children: 'pages.tasksCapacityCard.state_waiting_children',
  waiting_permission: 'pages.tasksCapacityCard.state_waiting_permission',
  waiting_dependency: 'pages.tasksCapacityCard.state_waiting_dependency',
  waiting_input: 'pages.tasksCapacityCard.state_waiting_input',
  waiting_infra: 'pages.tasksCapacityCard.state_waiting_infra',
  retry_wait: 'pages.tasksCapacityCard.state_retry_wait',
  recovering: 'pages.tasksCapacityCard.state_recovering',
  stalled: 'pages.tasksCapacityCard.state_stalled',
}

/** Lane names the gateway publishes; anything else renders as its own key. */
const LANE_LABEL_KEY: Record<string, string> = {
  subagents: 'pages.tasksCapacityCard.lane_subagents',
  spawn_gate: 'pages.tasksCapacityCard.lane_spawn_gate',
  system: 'pages.tasksCapacityCard.lane_system',
}

/**
 * Catalog key per degrade reason the gateway publishes
 * (`slack/gateway.py::_wire_overload_health`). The wire token stays snake_case
 * because it is also the `taskq.pressure_reason` metric attribute, so it is
 * mapped here the same way `STATE_LABEL_KEY` maps wait states. Nothing couples
 * that Python closed set to this map: a token added there falls through to the
 * verbatim branch rather than breaking. Anything else — the controller's own
 * free-text reading, which `handlers/tasks.py` falls back to — is machine text.
 */
const DEGRADE_REASON_KEY: Record<string, string> = {
  adaptive_decrease: 'pages.tasksCapacityCard.degrade_adaptive_decrease',
  adaptive_pause: 'pages.tasksCapacityCard.degrade_adaptive_pause',
  adaptive_probe: 'pages.tasksCapacityCard.degrade_adaptive_probe',
}

type BadgeVariant = 'ok' | 'err' | 'warn' | 'aim' | 'muted'

/**
 * When the gateway publishes no stall threshold, the queue is "backed up"
 * once its oldest row has waited this long — the same 10 minutes
 * `session_health` uses before it calls a silent run stalled.
 */
const BACKLOG_WAIT_SECS_FALLBACK = 600
/**
 * Queued rows per effective slot of the widest lane before the backlog
 * counts as a warning: at 2× every slot already has a full turn of work
 * behind it, so a new request waits at least two dispatch cycles.
 */
const BACKLOG_DEPTH_FACTOR = 2

export type CardHealthState = 'healthy' | 'degraded' | 'backlog' | 'stalled'

export interface CardHealth {
  variant: BadgeVariant
  state: CardHealthState
}

/** Literal keys per health state, indexed inline (see `STATE_LABEL_KEY`). */
const HEALTH_LABEL_KEY: Record<CardHealthState, string> = {
  healthy: 'pages.tasksCapacityCard.badge_healthy',
  degraded: 'pages.tasksCapacityCard.badge_degraded',
  backlog: 'pages.tasksCapacityCard.badge_backlog',
  stalled: 'pages.tasksCapacityCard.badge_stalled',
}

/**
 * The badge state, derived from the numbers the card already shows so the
 * two can never disagree. Severity order, first match wins:
 *
 *  1. `err`  — any stalled run (`stalled` non-empty): the same red the
 *              "Stalled" row badge uses, because a run with no progress and
 *              no wait reason is the one thing nothing here recovers on its own.
 *  2. `warn` — the controller lowered the effective cap (`degrade_reason`
 *              set): the strongest signal about capacity, kept ahead of the
 *              backlog rules since it explains why the backlog exists.
 *  3. `warn` — backed up: the oldest queued row has waited at least
 *              `stall_after_secs` (fallback `BACKLOG_WAIT_SECS_FALLBACK`), or
 *              `queued` exceeds `BACKLOG_DEPTH_FACTOR` × the largest effective
 *              lane cap (skipped when no lane reports one). Amber to match the
 *              "Retrying now" row badge: work is late, not lost.
 *  4. `ok`   — otherwise.
 */
export function cardHealth(d: TasksSummary): CardHealth {
  if (Object.keys(d.stalled).length > 0) return { variant: 'err', state: 'stalled' }
  if (d.degrade_reason) return { variant: 'warn', state: 'degraded' }
  const waitLimit = d.stall_after_secs ?? BACKLOG_WAIT_SECS_FALLBACK
  const widestCap = Math.max(0, ...Object.values(d.lanes).map(cap => cap.effective ?? 0))
  const longWait = d.oldest_wait_secs >= waitLimit
  const deepQueue = widestCap > 0 && d.depth.queued > BACKLOG_DEPTH_FACTOR * widestCap
  if (longWait || deepQueue) return { variant: 'warn', state: 'backlog' }
  return { variant: 'ok', state: 'healthy' }
}

/**
 * Two recovery states, two colours: `recovering` (amber) is a retry IN FLIGHT
 * — the backend is being restarted right now — while `retry_wait` (grey) is a
 * row PARKED until its next attempt, with nothing happening to it. Painted the
 * same amber they read as one state with two names, which is what the badge
 * copy exists to prevent.
 */
function stateVariant(state: string): BadgeVariant {
  if (state === 'stalled') return 'err'
  if (state === 'recovering' || state === 'waiting_infra') return 'warn'
  if (state.startsWith('waiting_')) return 'aim'
  if (state === 'running' || state === 'starting') return 'ok'
  return 'muted'
}

/** A row the USER has to act on: the run is parked on their answer, not on the system. */
function blockedOnHuman(state: string): boolean {
  return state === 'waiting_input' || state === 'waiting_permission'
}

export function stateLabel(state: string): string {
  const key = STATE_LABEL_KEY[state]
  return key ? i18nT(key) : state
}

function laneLabel(lane: string): string {
  const key = LANE_LABEL_KEY[lane]
  return key ? i18nT(key) : lane
}

/** Catalog copy for a known degrade token; null for anything unmapped. */
function degradeLabel(reason: string): string | null {
  const key = DEGRADE_REASON_KEY[reason]
  return key ? i18nT(key) : null
}

/**
 * An age in seconds as a compound duration. Seconds are kept: a 40-second
 * dependency cooldown is the common case here, and "0 min" would read as a
 * frozen counter.
 */
export function fmtAge(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return '—'
  const s = Math.floor(seconds)
  const parts: Array<[number, FormatUnit]> = [
    [Math.floor(s / 86400), 'day'],
    [Math.floor((s % 86400) / 3600), 'hour'],
    [Math.floor((s % 3600) / 60), 'minute'],
    [s % 60, 'second'],
  ]
  // Past an hour the seconds are noise; past a day so are the minutes.
  const trimmed = s >= 86400 ? parts.slice(0, 2) : s >= 3600 ? parts.slice(0, 3) : parts
  return fmtDuration(trimmed, { dropZero: true, maximumFractionDigits: 0 })
}

/**
 * "Up to N at once" for a lane — the runs it may hold right now. Not
 * "N of M slots": beside a "0 running now" line that reads as "N of M in use"
 * and contradicts it. The user's own limit and the running count go on the muted
 * line beneath (`fmtCapDetail`), so the headline stays short enough not to wrap
 * in a third of the card.
 */
function fmtCap(cap: LaneCap): string {
  const eff = cap.effective
  if (eff == null) return '—'
  return i18nT('pages.tasksCapacityCard.cap_at_once', { effective: fmtNumber(eff) })
}

/**
 * The muted lines under the headline — whichever the lane reports. "Your limit"
 * names the second number's AUTHOR, which is the only thing separating it from
 * the headline: both are maxima, and one is the system's reading of right now.
 */
function fmtCapDetail(cap: LaneCap): string[] {
  const parts: string[] = []
  if (cap.user_max != null) parts.push(i18nT('pages.tasksCapacityCard.cap_ceiling', { max: fmtNumber(cap.user_max) }))
  if (cap.running != null) parts.push(i18nT('pages.tasksCapacityCard.lane_running', { count: fmtNumber(cap.running) }))
  return parts
}

/** One waiting/recovering entry, whether it came from the store or a live slot. */
interface WaitEntry {
  key: string
  id: string
  state: string
  reason: string
  ageSecs: number
  nextRunAt: number | null
  attempts: number
  /**
   * Where the user acts on a row blocked on THEM (an answer, an approval):
   * the session's chat, by the same route the Sessions plane rows use. Null
   * when the row is not waiting on a person or its session has no chat window
   * (cron, channel, `_bg`), so no dead link is rendered.
   */
  href: string | null
}

function entriesOf(data: TasksSummary): WaitEntry[] {
  // A parked row (retry_wait / recovering) carries no wait record; its
  // "reason" is when the dispatcher will pick it up again.
  const retryReason = (row: TaskRow): string =>
    row.next_run_at != null
      ? i18nT('pages.tasksCapacityCard.next_retry_in', {
        age: fmtAge(Math.max(0, row.next_run_at - data.generated_at)),
      })
      : ''
  const actPath = (state: string, sessionKey: string): string | null =>
    blockedOnHuman(state) ? sessionChatPath(sessionKey) : null
  const fromTask = (row: TaskRow): WaitEntry => ({
    key: `task:${row.id}`,
    id: row.id,
    state: row.state,
    reason: row.wait_reason ?? row.wait?.dependency_scope ?? retryReason(row),
    ageSecs: row.age_secs,
    nextRunAt: row.next_run_at,
    attempts: row.attempts,
    href: actPath(row.state, row.session_key),
  })
  const taskIds = new Set<string>()
  const out: WaitEntry[] = []
  for (const row of data.waiting) { out.push(fromTask(row)); taskIds.add(row.id) }
  for (const row of data.recovering.tasks) { out.push(fromTask(row)); taskIds.add(row.id) }
  // Live slots the store does not know (main chat sessions): their own
  // classification is the reason. A slot key is the bare dashboard slot; the
  // backend spells its session `dashboard:<slot>`.
  for (const slot of data.slots) {
    if (slot.classification === 'running' || taskIds.has(slot.key)) continue
    out.push({
      key: `slot:${slot.key}`,
      id: slot.key,
      state: slot.classification,
      reason: slot.evidence.join(' · '),
      ageSecs: slot.age_secs,
      nextRunAt: null,
      attempts: 0,
      href: actPath(slot.classification, `dashboard:${slot.key}`),
    })
  }
  // Longest wait first: that is the one the operator is asking about.
  out.sort((a, b) => b.ageSecs - a.ageSecs)
  return out
}

/* ── Rows (the Services plane's own label/value shape) ── */

interface Row {
  label: string
  value: React.ReactNode
  tip?: string
}

function SectionBlock({ title, rows }: { title: string; rows: Row[] }) {
  return (
    <div className="mb-4" style={{ breakInside: 'avoid' }}>
      <h4 className="text-[11.5px] font-semibold text-muted uppercase tracking-wide mb-2">{title}</h4>
      {rows.map(row => (
        <div
          key={row.label}
          className="flex justify-between gap-3 py-1.5 border-b border-border text-[12.5px] last:border-b-0"
        >
          <span className="text-muted shrink-0 inline-flex items-center gap-1">
            {row.label}
            {row.tip && <InfoTip text={row.tip} />}
          </span>
          <span className="text-text-strong font-mono tabular-nums text-right break-words">{row.value}</span>
        </div>
      ))}
    </div>
  )
}

/* ── Main component ── */

export default function TasksCapacityCard() {
  const { data, error, isError } = useQuery<TasksSummary>({
    queryKey: ['tasksSummary'],
    queryFn: () => api.tasksSummary(),
    refetchInterval: TASKS_SUMMARY_REFETCH_MS,
  })

  const d = data ?? null
  const depth = d?.depth
  const lanes = d ? Object.entries(d.lanes) : []
  const entries = d ? entriesOf(d) : []
  const shown = entries.slice(0, WAIT_ROWS_SHOWN)
  const hidden = entries.length - shown.length
  const health = d ? cardHealth(d) : null
  const degrade = d?.degrade_reason ?? null
  const degradeText = degrade ? degradeLabel(degrade) : null

  const queueRows: Row[] = [
    { label: i18nT('pages.tasksCapacityCard.queued'), value: depth ? fmtNumber(depth.queued) : '—',
      tip: i18nT('pages.tasksCapacityCard.queued_tip') },
    { label: i18nT('pages.tasksCapacityCard.running'), value: depth ? fmtNumber(depth.running) : '—' },
    // A task row and a live chat slot can both be parked mid-run, but
    // `depth.waiting` counts store rows only, so a session held on the
    // operator's approval was a list row under no counter at all. Counted off
    // `entries` — the array the list itself is built from — so the number and
    // the rows beneath it cannot disagree.
    { label: i18nT('pages.tasksCapacityCard.waiting'),
      value: d ? fmtNumber(entries.filter(e => e.state.startsWith('waiting_')).length) : '—',
      tip: i18nT('pages.tasksCapacityCard.waiting_tip') },
    { label: i18nT('pages.tasksCapacityCard.recovering'), value: depth ? fmtNumber(depth.recovering) : '—',
      tip: i18nT('pages.tasksCapacityCard.recovering_tip') },
    { label: i18nT('pages.tasksCapacityCard.oldest_wait'), value: d ? fmtAge(d.oldest_wait_secs) : '—',
      tip: i18nT('pages.tasksCapacityCard.oldest_wait_tip') },
  ]

  const laneRows: Row[] = lanes.length > 0
    ? lanes.map(([lane, cap]) => ({
      label: laneLabel(lane),
      tip: i18nT('pages.tasksCapacityCard.lane_tip'),
      // Three short labelled facts, not one run of numbers: "Up to 4 at once"
      // is the cap this lane may fill right now; the muted lines beneath give
      // the limit the user set and how many live runs it holds. One fact per
      // line, each kept whole: the column is a third of the card and a lane
      // label can take half the row, so a sentence here wraps mid-phrase.
      value: (
        <span className="inline-flex flex-col items-end leading-snug" data-testid="tasks-capacity-lane">
          <span className="whitespace-nowrap">{fmtCap(cap)}</span>
          {fmtCapDetail(cap).map(line => (
            <span key={line} className="text-muted whitespace-nowrap">{line}</span>
          ))}
        </span>
      ),
    }))
    : [{ label: i18nT('pages.tasksCapacityCard.effective_cap'), value: '—' }]
  laneRows.push({
    label: i18nT('pages.tasksCapacityCard.degrade_reason'),
    // The badge beside the title already reads "Degraded" in the reader's own
    // language, so the explanation next to it cannot be a wire token. An
    // unmapped reason is machine text and carries `translate="no"`.
    value: degrade
      ? (
        <span
          style={{ color: 'var(--warn)' }}
          data-testid="tasks-capacity-degrade"
          translate={degradeText ? undefined : 'no'}
        >
          {degradeText ?? degrade}
        </span>
      )
      : (
        <span className="text-muted" data-testid="tasks-capacity-degrade">
          {i18nT('pages.tasksCapacityCard.not_degraded')}
        </span>
      ),
    tip: i18nT('pages.tasksCapacityCard.degrade_reason_tip'),
  })

  const recovery = d?.recovering
  const recoveryRows: Row[] = [
    { label: i18nT('pages.tasksCapacityCard.task_retries'), value: recovery ? fmtNumber(recovery.task_attempts) : '—',
      tip: i18nT('pages.tasksCapacityCard.task_retries_tip') },
    { label: i18nT('pages.tasksCapacityCard.recovering_runs'), value: recovery ? fmtNumber(recovery.slots.length) : '—',
      tip: i18nT('pages.tasksCapacityCard.recovering_runs_tip') },
    { label: i18nT('pages.tasksCapacityCard.stalled'), value: d ? fmtNumber(Object.keys(d.stalled).length) : '—',
      tip: d?.stall_after_secs != null
        ? i18nT('pages.tasksCapacityCard.stalled_tip', { age: fmtAge(d.stall_after_secs) })
        : undefined },
  ]

  return (
    <Card data-testid="tasks-capacity-card">
      <CardTitle>
        {i18nT('pages.tasksCapacityCard.title')}
        {health && (
          <Badge variant={health.variant} data-testid="tasks-capacity-health">
            {i18nT(HEALTH_LABEL_KEY[health.state])}
          </Badge>
        )}
      </CardTitle>
      {isError && (
        <ErrorNotice
          message={error instanceof Error ? error.message : String(error)}
          title={i18nT('pages.tasksCapacityCard.load_failed')}
          askAgent
          className="mb-3"
          testId="tasks-capacity-error"
        />
      )}
      {d && !d.available && (
        <p className="text-[12.5px] text-muted mb-3">{i18nT('pages.tasksCapacityCard.no_store')}</p>
      )}
      <div className="columns-3 gap-6 max-[900px]:columns-2 max-[600px]:columns-1">
        <SectionBlock title={i18nT('pages.tasksCapacityCard.section_queue')} rows={queueRows} />
        <SectionBlock title={i18nT('pages.tasksCapacityCard.section_capacity')} rows={laneRows} />
        <SectionBlock title={i18nT('pages.tasksCapacityCard.section_recovery')} rows={recoveryRows} />
      </div>

      <h4 className="text-[11.5px] font-semibold text-muted uppercase tracking-wide mb-2 mt-1 flex items-center gap-1">
        {i18nT('pages.tasksCapacityCard.section_waits')}
        <InfoTip text={i18nT('pages.tasksCapacityCard.section_waits_tip')} />
      </h4>
      {d && shown.length === 0 && (
        <p className="text-[12.5px] text-muted py-1.5" data-testid="tasks-capacity-empty">
          {i18nT('pages.tasksCapacityCard.waits_empty')}
        </p>
      )}
      {shown.length > 0 && (
        <ul className="m-0 p-0 list-none" aria-label={i18nT('pages.tasksCapacityCard.section_waits')}>
          {shown.map(entry => (
            <li
              key={entry.key}
              data-testid="tasks-capacity-wait-row"
              className="flex flex-col gap-1 py-1.5 border-b border-border text-[12.5px] last:border-b-0 sm:flex-row sm:items-center sm:gap-3"
            >
              {/* Narrow-first: three stacked lines (badge + id / reason / action +
                  attempts + age) so the reason keeps its full width on a phone,
                  where a truncated span has no hover to reveal it. From `sm` up the
                  two wrappers dissolve (`contents`) and everything sits on one row. */}
              <div className="flex items-center gap-2 min-w-0 sm:contents">
                <Badge variant={stateVariant(entry.state)}>{stateLabel(entry.state)}</Badge>
                {/* The id is a fact, never the control. Up to WAIT_ROWS_SHOWN
                    identically shaped mono ids sit in this list, so an accented
                    one carries no at-rest cue that it is the actionable row. */}
                <span className="font-mono text-text-strong truncate min-w-0 max-w-[220px]" title={entry.id}>
                  {entry.id}
                </span>
              </div>
              <span
                className="text-muted break-words sm:min-w-0 sm:flex-1 sm:truncate"
                data-testid="tasks-capacity-wait-reason"
                title={entry.reason || undefined}
              >
                {entry.reason}
              </span>
              <div className="flex items-center gap-3 sm:contents">
                {/* A row parked on the USER needs a named way to reach where they
                    act, not a coloured id. The card stays read-only: this only
                    navigates, and the answer/approve lever lives in that chat. */}
                {entry.href && (
                  <Link
                    to={entry.href}
                    className="text-accent hover:underline shrink-0 whitespace-nowrap"
                    data-testid="tasks-capacity-wait-link"
                  >
                    {i18nT('pages.tasksCapacityCard.open_chat')}
                  </Link>
                )}
                {entry.attempts > 1 && (
                  <span className="text-muted shrink-0">
                    {i18nT('pages.tasksCapacityCard.attempts', { count: fmtNumber(entry.attempts) })}
                  </span>
                )}
                {/* "5s so far", not a bare "5s": on a retry row the reason column
                    already carries a clock ("next retry in 1m 29s"), so the age
                    says which one it is. Not "for 5s": a value opening with a
                    connector word reads as half a sentence to the source-string
                    gate, and to a translator. */}
                <span
                  className="font-mono tabular-nums text-text-strong shrink-0"
                  data-testid="tasks-capacity-wait-age"
                  title={i18nT('pages.tasksCapacityCard.age_tip')}
                >
                  {i18nT('pages.tasksCapacityCard.age_for', { age: fmtAge(entry.ageSecs) })}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
      {hidden > 0 && (
        <p className="text-[12.5px] text-muted pt-1.5">
          {i18nT('pages.tasksCapacityCard.and_more', { count: fmtNumber(hidden) })}
        </p>
      )}
    </Card>
  )
}
