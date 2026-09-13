/**
 * System > Services > "Tasks & capacity".
 *
 * The card is the only dashboard surface that shows the durable task queue
 * and the effective concurrency the gateway is running under. These cases pin
 * the render to the `/api/tasks/summary` payload: depth by state, the oldest
 * wait, the effective cap against the user's ceiling with the degrade reason,
 * one row per waiting/recovering task or slot with its reason and age, the
 * empty state, the no-store notice, and the failure notice.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import type { TasksSummary } from '../api/tasks'

const tasksSummary = vi.fn<() => Promise<TasksSummary>>()

vi.mock('../api/client', () => ({
  api: {
    tasksSummary: () => tasksSummary(),
  },
}))

import TasksCapacityCard, { cardHealth, fmtAge } from '../pages/system/TasksCapacityCard'

function summary(overrides: Partial<TasksSummary> = {}): TasksSummary {
  return {
    generated_at: 1_000,
    available: true,
    depth: { by_state: {}, queued: 0, waiting: 0, recovering: 0, running: 0, total: 0 },
    oldest_wait_secs: 0,
    lanes: {},
    degrade_reason: null,
    adaptive: null,
    slots: [],
    waiting: [],
    recovering: { tasks: [], task_attempts: 0, slots: [], ladder: [] },
    stalled: {},
    counts: {},
    stall_after_secs: 600,
    ...overrides,
  }
}

function row(id: string, state: string, extra: Record<string, unknown> = {}) {
  return {
    id, kind: 'subagent', state, lane: 'web-a', session_key: 'web-a', parent_id: null, root_id: id,
    attempts: 1, generation: 1, next_run_at: null, deadline_at: null, lease_owner: null,
    lease_expires_at: null, wait: null, wait_reason: null, wait_since: null, wait_deadline_at: null,
    age_secs: 0, created_at: 0, updated_at: 0, terminal: false, ...extra,
  }
}

beforeEach(() => {
  tasksSummary.mockReset()
})

describe('TasksCapacityCard', () => {
  it('renders the empty state with a healthy badge and zero counts', async () => {
    tasksSummary.mockResolvedValue(summary())
    renderWithProviders(<TasksCapacityCard />)

    await screen.findByTestId('tasks-capacity-empty')
    expect(screen.getByText('Tasks & capacity')).toBeTruthy()
    expect(screen.getByText('Healthy')).toBeTruthy()
    expect(screen.getByText('Nothing is waiting or recovering.')).toBeTruthy()
    // No store notice when the store IS available.
    expect(screen.queryByText(/without a task store/)).toBeNull()
  })

  it('says so when the gateway has no task store instead of showing zeros as facts', async () => {
    tasksSummary.mockResolvedValue(summary({ available: false }))
    renderWithProviders(<TasksCapacityCard />)
    await screen.findByText(/runs without a task store/)
  })

  it('shows depth, oldest wait, cap vs ceiling, the degrade reason and each wait with its age', async () => {
    tasksSummary.mockResolvedValue(summary({
      depth: {
        by_state: { queued: 12, running: 3, waiting_dependency: 1, waiting_input: 1, recovering: 2 },
        queued: 12, waiting: 2, recovering: 2, running: 3, total: 19,
      },
      oldest_wait_secs: 754,
      lanes: {
        subagents: { effective: 4, user_max: 8, running: 3 },
        spawn_gate: { effective: 2, user_max: 8 },
      },
      degrade_reason: 'loop_lag=410ms',
      waiting: [
        row('dep-1', 'waiting_dependency', {
          wait_reason: 'github:api rate limited', age_secs: 42,
          wait: { reason: 'github:api rate limited', since: 900, deadline_at: null, resume_kind: 'at_time', dependency_scope: 'github:api', cancel_semantics: 'task', tool_call_id: '' },
        }),
        row('inp-1', 'waiting_input', { wait_reason: 'sudo wants a password', age_secs: 3_700, session_key: 'dashboard:web-inp' }),
      ],
      recovering: {
        tasks: [
          row('rec-1', 'recovering', { attempts: 3, age_secs: 5, next_run_at: 1_000 + 90 }),
          row('park-1', 'retry_wait', { attempts: 2, age_secs: 4, next_run_at: 1_000 + 30 }),
        ],
        task_attempts: 3,
        slots: [{ key: 'web-z', age_secs: 12, evidence: ['retry in flight: infra'] }],
        ladder: [],
      },
      slots: [
        { key: 'web-z', classification: 'recovering', age_secs: 12, evidence: ['retry in flight: infra'] },
        { key: 'web-ok', classification: 'running', age_secs: 1, evidence: [] },
      ],
      stalled: { 'web-s': { reason: 'no_progress', since_ts: 0, age_secs: 700, evidence: [] } },
    }))
    renderWithProviders(<TasksCapacityCard />)

    // A stalled run outranks the degrade reason: the badge goes red, not amber.
    const badge = await screen.findByTestId('tasks-capacity-health')
    expect(badge.textContent).toBe('Stalled')
    expect(badge.className).toContain('text-danger')
    // Queue depth.
    const queued = screen.getByText('Waiting for a slot').closest('div')!
    expect(queued.textContent).toContain('12')
    expect(screen.getByText('Oldest wait').closest('div')!.textContent).toContain(fmtAge(754))
    // Effective cap as a short headline, with the user's own limit and the
    // running count as separately labelled facts beneath — never "N of M" beside
    // "0 running", which reads as two answers to the same question. The second
    // line names its author so two maxima are not two anonymous ceilings.
    const [subagents, spawnGate] = screen.getAllByTestId('tasks-capacity-lane')
    const subagentLines = Array.from(subagents.querySelectorAll('span')).map(s => s.textContent)
    expect(subagentLines).toEqual(['Up to 4 at once', 'Your limit: 8', '3 running now'])
    const spawnGateLines = Array.from(spawnGate.querySelectorAll('span')).map(s => s.textContent)
    expect(spawnGateLines).toEqual(['Up to 2 at once', 'Your limit: 8'])
    expect(screen.queryByText(/of \d+ slots/)).toBeNull()
    expect(screen.getByText('Subagent runs').closest('div')!.textContent).not.toContain('running:')
    // The lane is named by what it does, not by the mechanism behind it.
    expect(screen.getByText('Backend starts')).toBeTruthy()
    expect(screen.queryByText('Spawn gate')).toBeNull()
    expect(screen.getByText('loop_lag=410ms')).toBeTruthy()
    // Recovery column: attempts across recovering rows, recovering slots, stalled.
    expect(screen.getByText('Task retries').closest('div')!.textContent).toContain('3')
    expect(screen.getByText('Backends restarting').closest('div')!.textContent).toContain('1')
    expect(screen.getByText('Stalled', { selector: 'span:not([data-testid])' }).closest('div')!.textContent).toContain('1')
    // Waits list: one row per task/slot with its reason, age and state badge;
    // the running slot is NOT a wait.
    const list = screen.getByRole('list', { name: 'Waiting & recovering' })
    const items = list.querySelectorAll('li')
    expect(items).toHaveLength(5)
    expect(list.textContent).toContain('github:api rate limited')
    expect(list.textContent).toContain('sudo wants a password')
    expect(list.textContent).toContain('Waiting for dependency')
    expect(list.textContent).toContain('Waiting for input')
    expect(list.textContent).toContain('attempts: 3')
    expect(list.textContent).toContain(`next retry in ${fmtAge(90)}`)
    // The age column names its clock: "for 5s" beside "next retry in 1m 30s",
    // never two bare durations on one row.
    const ages = screen.getAllByTestId('tasks-capacity-wait-age')
    expect(ages).toHaveLength(5)
    expect(ages.map(a => a.textContent)).toContain(`${fmtAge(3_700)} so far`)
    const recRow = Array.from(items).find(li => li.textContent!.includes('rec-1'))!
    expect(recRow.textContent).toContain(`next retry in ${fmtAge(90)}`)
    expect(recRow.querySelector('[data-testid="tasks-capacity-wait-age"]')!.textContent).toBe(`${fmtAge(5)} so far`)
    for (const a of ages) expect(a.getAttribute('title')).toBe('Time spent in the state the badge names.')
    expect(list.textContent).not.toContain('web-ok')
    // Longest wait first.
    expect(items[0].textContent).toContain('inp-1')
    expect(screen.queryByTestId('tasks-capacity-empty')).toBeNull()

    // A retry IN FLIGHT and a row PARKED until its next attempt are two states:
    // different words, different colours. Same "next retry in …" clock on both,
    // so the badge is the only thing telling them apart.
    const badgeOf = (li: Element) => li.querySelector('span.rounded-full')!
    const parkRow = Array.from(items).find(li => li.textContent!.includes('park-1'))!
    expect(badgeOf(recRow).textContent).toBe('Retrying now')
    expect(badgeOf(parkRow).textContent).toBe('Waiting to retry')
    expect(badgeOf(recRow).className).toContain('text-warn')
    expect(badgeOf(parkRow).className).not.toContain('text-warn')
    expect(badgeOf(recRow).className).not.toBe(badgeOf(parkRow).className)
    expect(list.textContent).not.toContain('Retry wait')
    expect(list.textContent).not.toMatch(/\bRecovering\b(?! tasks)/)

    // A row blocked on the USER carries a NAMED link to where they act — the
    // session's chat, by the Sessions plane's own route. The id stays plain text
    // on every row, so colour alone never distinguishes an actionable one.
    const links = screen.getAllByTestId('tasks-capacity-wait-link')
    expect(links).toHaveLength(1)
    expect(links[0].textContent).toBe('Open chat')
    expect(links[0].getAttribute('href')).toBe('/chat?sid=web-inp')
    expect(links[0].getAttribute('aria-label')).toBeNull()
    const inpRow = Array.from(items).find(li => li.textContent!.includes('inp-1'))!
    expect(inpRow.querySelector('a')!.textContent).toBe('Open chat')
    // Rows waiting on the system (dependency, retry) are not links.
    const depRow = Array.from(items).find(li => li.textContent!.includes('dep-1'))!
    expect(depRow.querySelector('a')).toBeNull()
    expect(recRow.querySelector('a')).toBeNull()
  })

  it('links a slot waiting for approval to its chat, and renders no link when the session has no chat window', async () => {
    tasksSummary.mockResolvedValue(summary({
      waiting: [
        // A cron session is real but has nowhere to navigate to: plain text, no dead link.
        row('inp-cron', 'waiting_input', { wait_reason: 'needs a password', age_secs: 10, session_key: 'cron_abc' }),
        row('inp-none', 'waiting_input', { wait_reason: 'needs a token', age_secs: 9, session_key: '' }),
      ],
      slots: [
        { key: 'chat-7', classification: 'waiting_permission', age_secs: 30, evidence: ['approval pending'] },
      ],
    }))
    renderWithProviders(<TasksCapacityCard />)
    const links = await screen.findAllByTestId('tasks-capacity-wait-link')
    expect(links).toHaveLength(1)
    expect(links[0].textContent).toBe('Open chat')
    expect(links[0].getAttribute('href')).toBe('/chat?sid=chat-7')
    const list = screen.getByRole('list', { name: 'Waiting & recovering' })
    expect(list.querySelectorAll('a')).toHaveLength(1)
    expect(list.textContent).toContain('chat-7')
    expect(list.textContent).toContain('inp-cron')
    expect(list.textContent).toContain('inp-none')
  })

  it('names the action on a human-blocked row instead of only colouring its id', async () => {
    tasksSummary.mockResolvedValue(summary({
      waiting: [
        row('inp-1', 'waiting_input', { wait_reason: 'sudo wants a password', age_secs: 30, session_key: 'dashboard:web-inp' }),
        row('dep-1', 'waiting_dependency', { wait_reason: 'github:api rate limited', age_secs: 20 }),
      ],
    }))
    renderWithProviders(<TasksCapacityCard />)
    const link = await screen.findByTestId('tasks-capacity-wait-link')
    // The visible catalog text IS the accessible name: an aria-label that does
    // not contain it would break WCAG 2.5.3 in the locales that reorder words.
    expect(link.textContent).toBe('Open chat')
    expect(link.getAttribute('aria-label')).toBeNull()
    expect(link.getAttribute('title')).toBeNull()
    expect(link.getAttribute('href')).toBe('/chat?sid=web-inp')
    // No row renders its id as the control, actionable or not.
    for (const li of screen.getAllByTestId('tasks-capacity-wait-row')) {
      const anchor = li.querySelector('a')
      if (anchor) expect(anchor.textContent).toBe('Open chat')
    }
    expect(screen.queryByRole('link', { name: 'inp-1' })).toBeNull()
  })

  it('reads the degrade reason out of the catalog and passes an unknown one through', async () => {
    const copy: Record<string, string> = {
      adaptive_decrease: 'Concurrency lowered under load',
      adaptive_pause: 'New runs paused until the host recovers',
      adaptive_probe: 'Testing one run before resuming',
    }
    for (const [token, text] of Object.entries(copy)) {
      tasksSummary.mockReset()
      tasksSummary.mockResolvedValue(summary({ degrade_reason: token }))
      const { unmount } = renderWithProviders(<TasksCapacityCard />)
      await waitFor(() =>
        expect(screen.getByTestId('tasks-capacity-degrade').textContent).toBe(text))
      // The badge says "Degraded" in the reader's language, so the explanation
      // beside it must not be the snake_case wire token.
      expect(screen.getByTestId('tasks-capacity-health').textContent).toBe('Degraded')
      expect(screen.queryByText(token)).toBeNull()
      unmount()
    }

    // The controller's own free-text reading is machine text: verbatim, and
    // marked untranslatable rather than blanked.
    tasksSummary.mockReset()
    tasksSummary.mockResolvedValue(summary({ degrade_reason: 'paused: memory below critical' }))
    renderWithProviders(<TasksCapacityCard />)
    await waitFor(() => expect(screen.getByTestId('tasks-capacity-degrade').textContent)
      .toBe('paused: memory below critical'))
    expect(screen.getByTestId('tasks-capacity-degrade').getAttribute('translate')).toBe('no')
  })

  it('counts a chat session paused on the operator, not only the task rows', async () => {
    // `depth.waiting` is the store's own number, so a live slot parked on an
    // approval used to be a list row that no counter on the card admitted to.
    tasksSummary.mockResolvedValue(summary({
      depth: { by_state: {}, queued: 0, waiting: 1, recovering: 0, running: 0, total: 1 },
      waiting: [row('inp-2', 'waiting_input', { wait_reason: 'needs a token', age_secs: 20, session_key: 'dashboard:web-inp' })],
      slots: [{ key: 'chat-7', classification: 'waiting_permission', age_secs: 30, evidence: ['approval pending'] }],
    }))
    renderWithProviders(<TasksCapacityCard />)
    const list = await screen.findByRole('list', { name: 'Waiting & recovering' })
    expect(list.querySelectorAll('li')).toHaveLength(2)
    const paused = screen.getByText('Paused mid-run').closest('div')!
    expect(paused.textContent).toContain('2')
    expect(paused.textContent).not.toContain('1')
  })

  it('names nested work with ONE word everywhere the card mentions it', async () => {
    // Never a second word for nested work: the badge, the lane and the tip name
    // it, and a card whose whole point is comparing its own lines reads two
    // words as two concepts. "Task" is taken here — it means a queue ROW
    // ("Recovering tasks", "Task retries"). The `wait_reason` is the backend's
    // untranslated evidence prose, in the same row as the badge, so the sweep
    // below covers it too rather than only the catalog.
    tasksSummary.mockResolvedValue(summary({
      depth: { by_state: {}, queued: 0, waiting: 1, recovering: 0, running: 1, total: 2 },
      lanes: { subagents: { effective: 4, user_max: 8, running: 1 } },
      waiting: [row('kid-1', 'waiting_children',
        { wait_reason: 'waiting on 2 subagent(s)', age_secs: 12 })],
    }))
    renderWithProviders(<TasksCapacityCard />)
    const list = await screen.findByRole('list', { name: 'Waiting & recovering' })
    expect(list.textContent).toContain('Waiting for subagents')
    expect(screen.getByText('Subagent runs')).toBeTruthy()
    const pausedTip = screen.getByText('Paused mid-run').closest('div')!.querySelector('button')!
    expect(pausedTip.getAttribute('title')).toContain('subagents')
    const card = screen.getByTestId('tasks-capacity-card')
    const prose = [card.textContent ?? '',
      ...Array.from(card.querySelectorAll('button[title]')).map(b => b.getAttribute('title'))].join(' ')
    expect(prose).not.toMatch(/child|subtask|nested/i)
  })

  it('folds a long wait list and names how many are hidden', async () => {
    tasksSummary.mockResolvedValue(summary({
      waiting: Array.from({ length: 11 }, (_, i) => row(`w-${i}`, 'waiting_children', { age_secs: i })),
    }))
    renderWithProviders(<TasksCapacityCard />)
    await screen.findByText('3 more not shown')
    expect(screen.getByRole('list', { name: 'Waiting & recovering' }).querySelectorAll('li')).toHaveLength(8)
  })

  it('derives the badge from the numbers the card shows, not from degrade_reason alone', async () => {
    // 14 queued, an oldest wait past the stall threshold and a stalled run
    // with NO degrade reason: the reviewer's case, which used to read Healthy.
    tasksSummary.mockResolvedValue(summary({
      depth: { by_state: {}, queued: 14, waiting: 4, recovering: 2, running: 2, total: 22 },
      oldest_wait_secs: 1_308,
      lanes: { subagents: { effective: 4, user_max: 14, running: 0 } },
      stalled: { 'web-s': { reason: 'no_progress', since_ts: 0, age_secs: 700, evidence: [] } },
    }))
    renderWithProviders(<TasksCapacityCard />)
    const badge = await screen.findByTestId('tasks-capacity-health')
    expect(badge.textContent).toBe('Stalled')
    expect(badge.className).toContain('text-danger')
    expect(screen.queryByText('Healthy')).toBeNull()
  })

  it('renders the backlog badge in amber when the queue is late but nothing is stalled', async () => {
    tasksSummary.mockResolvedValue(summary({
      depth: { by_state: {}, queued: 14, waiting: 0, recovering: 0, running: 2, total: 16 },
      oldest_wait_secs: 1_308,
      lanes: { subagents: { effective: 4, user_max: 14, running: 2 } },
    }))
    renderWithProviders(<TasksCapacityCard />)
    const badge = await screen.findByTestId('tasks-capacity-health')
    expect(badge.textContent).toBe('Backed up')
    expect(badge.className).toContain('text-warn')
  })

  it('keeps a long reason un-truncated at the narrow layout and only clips from sm up', async () => {
    const reason = 'GitHub API rate limited for repo kirodotdev/KiroCrew; the coordinator resumes this run once the reset window at the top of the hour has passed'
    tasksSummary.mockResolvedValue(summary({
      waiting: [row('dep-long', 'waiting_dependency', { wait_reason: reason, age_secs: 70, attempts: 3 })],
    }))
    renderWithProviders(<TasksCapacityCard />)
    const el = await screen.findByTestId('tasks-capacity-wait-reason')
    // The full sentence is in the DOM (not elided) ...
    expect(el.textContent).toBe(reason)
    // ... and truncation is a `sm:`-scoped utility only: at narrow widths the
    // span wraps (`break-words`) instead of relying on a hover-only title.
    const classes = el.className.split(/\s+/)
    expect(classes).toContain('break-words')
    expect(classes).toContain('sm:truncate')
    expect(classes).not.toContain('truncate')
    // The row stacks narrow-first and becomes a single row from sm up.
    const li = screen.getByTestId('tasks-capacity-wait-row')
    expect(li.className.split(/\s+/)).toEqual(expect.arrayContaining(['flex-col', 'sm:flex-row']))
  })

  it('renders the failure through ErrorNotice, not a bare div', async () => {
    tasksSummary.mockRejectedValue(new Error('503 task store unavailable'))
    renderWithProviders(<TasksCapacityCard />)
    await waitFor(() => expect(screen.getByTestId('tasks-capacity-error')).toBeTruthy())
    expect(screen.getByTestId('tasks-capacity-error').textContent).toContain('Could not load the task queue')
  })
})

describe('cardHealth', () => {
  const base = () => summary({
    depth: { by_state: {}, queued: 0, waiting: 0, recovering: 0, running: 0, total: 0 },
    lanes: { subagents: { effective: 4, user_max: 8, running: 0 } },
    stall_after_secs: 600,
  })

  it('is healthy on an empty queue with nothing degraded', () => {
    expect(cardHealth(base())).toEqual({ variant: 'ok', state: 'healthy' })
  })

  it('ranks stalled above degraded above backlog', () => {
    const all = summary({
      ...base(),
      degrade_reason: 'loop_lag=410ms',
      oldest_wait_secs: 5_000,
      depth: { by_state: {}, queued: 40, waiting: 0, recovering: 0, running: 0, total: 40 },
      stalled: { s: { reason: 'no_progress', since_ts: 0, age_secs: 1, evidence: [] } },
    })
    expect(cardHealth(all)).toEqual({ variant: 'err', state: 'stalled' })
    expect(cardHealth({ ...all, stalled: {} })).toEqual({ variant: 'warn', state: 'degraded' })
    expect(cardHealth({ ...all, stalled: {}, degrade_reason: null })).toEqual({ variant: 'warn', state: 'backlog' })
  })

  it('calls the queue backed up at the stall threshold, and one second under it healthy', () => {
    expect(cardHealth({ ...base(), oldest_wait_secs: 600 }).state).toBe('backlog')
    expect(cardHealth({ ...base(), oldest_wait_secs: 599 }).state).toBe('healthy')
    // No published threshold → the 10-minute fallback.
    expect(cardHealth({ ...base(), stall_after_secs: null, oldest_wait_secs: 600 }).state).toBe('backlog')
    expect(cardHealth({ ...base(), stall_after_secs: null, oldest_wait_secs: 599 }).state).toBe('healthy')
  })

  it('calls the queue backed up past 2× the widest effective lane cap, and never without a cap', () => {
    const depth = (queued: number) => ({ by_state: {}, queued, waiting: 0, recovering: 0, running: 0, total: queued })
    expect(cardHealth({ ...base(), depth: depth(9) }).state).toBe('backlog')
    expect(cardHealth({ ...base(), depth: depth(8) }).state).toBe('healthy')
    // The widest lane sets the bar, not the narrowest.
    const lanes = { subagents: { effective: 4, user_max: 8 }, spawn_gate: { effective: 2, user_max: 8 } }
    expect(cardHealth({ ...base(), lanes, depth: depth(8) }).state).toBe('healthy')
    // No lane reports an effective cap: depth alone cannot say backlog.
    expect(cardHealth({ ...base(), lanes: {}, depth: depth(500) }).state).toBe('healthy')
  })
})

describe('fmtAge', () => {
  it('keeps seconds under an hour, drops them past it, and dashes bad input', () => {
    expect(fmtAge(42)).toContain('42')
    expect(fmtAge(3_700)).not.toContain('40')   // 1h 1m 40s → seconds dropped
    expect(fmtAge(3_700)).toContain('1')
    expect(fmtAge(-1)).toBe('—')
    expect(fmtAge(null)).toBe('—')
  })
})
