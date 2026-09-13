/**
 * Screenshot harness for the System > Services "Tasks & capacity" card.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server with
 * every /api/** call answered by the shared boot stub — no gateway, no token.
 * Only `/api/tasks/summary` is scene-specific. Each scene exists because some
 * copy or layout the UX lane judges is unreachable from the others: the empty
 * and populated pair for the cap line vs the running line, the "Open chat"
 * action on a row blocked on the user and "Retrying now" vs "Waiting to
 * retry"; `degraded` for the mapped degrade reason and its warn badge;
 * `stalled` for the red badge; `folded` for the "N more not shown" line past
 * `WAIT_ROWS_SHOWN`; `narrow` for the 320px stack; and `no-store` / `error`
 * for the two arms that render a notice instead of a queue.
 *
 * Every shot is the card element alone, at 1x, so they stay small and match
 * the earlier evidence frames at the same paths.
 *
 * Usage: node scripts/capture-tasks-capacity.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/overload-resilience'
mkdirSync(OUT, { recursive: true })

const NOW = 1_757_800_000
const AGE = 15 * 60 + 55

/** Shared skeleton; the store IS available so zeros read as facts. */
const base = {
  generated_at: NOW,
  available: true,
  depth: { by_state: {}, queued: 0, waiting: 0, recovering: 0, running: 0, total: 0 },
  oldest_wait_secs: 0,
  lanes: {
    subagents: { effective: 4, user_max: 14, running: 0 },
    spawn_gate: { effective: 4, user_max: 8 },
  },
  degrade_reason: null,
  adaptive: null,
  slots: [],
  waiting: [],
  recovering: { tasks: [], task_attempts: 0, slots: [], ladder: [] },
  stalled: {},
  counts: {},
  stall_after_secs: 600,
}

function row(id, state, extra = {}) {
  return {
    id, kind: 'workflow_agent', state, lane: 'chat-1', session_key: 'dashboard:chat-1',
    parent_id: null, root_id: id, attempts: 1, generation: 1, next_run_at: null,
    deadline_at: null, lease_owner: null, lease_expires_at: null, wait: null,
    wait_reason: null, wait_since: null, wait_deadline_at: null, age_secs: 0,
    created_at: NOW - 1000, updated_at: NOW, terminal: false, ...extra,
  }
}

const EMPTY = base

const POPULATED = {
  ...base,
  depth: {
    by_state: { queued: 14, running: 2, waiting_input: 1, waiting_permission: 1, waiting_children: 1, waiting_dependency: 1, recovering: 1, retry_wait: 1 },
    queued: 14, waiting: 4, recovering: 2, running: 2, total: 22,
  },
  oldest_wait_secs: AGE,
  waiting: [
    row('input-sudo', 'waiting_input', { wait_reason: '`sudo apt-get install` is waiting for a password', age_secs: AGE }),
    row('perm-deploy', 'waiting_permission', { wait_reason: 'deploy_artifact needs approval', age_secs: AGE, session_key: 'dashboard:chat-2' }),
    row('parent-wave', 'waiting_children', { wait_reason: 'waiting on 3 subagent(s)', age_secs: AGE }),
    row('dep-gh-1', 'waiting_dependency', { wait_reason: 'GitHub API rate limited; retry at reset', age_secs: 7 * 60 + 18 }),
  ],
  recovering: {
    tasks: [
      row('rec-backend', 'recovering', { attempts: 2, age_secs: 12, next_run_at: NOW + 14 }),
      row('retry-429', 'retry_wait', { attempts: 1, age_secs: 12, next_run_at: NOW + 81 }),
    ],
    task_attempts: 3,
    slots: [],
    ladder: [],
  },
}

/** A row per wait state the fold line has to count past `WAIT_ROWS_SHOWN` (8). */
const MANY = {
  ...POPULATED,
  waiting: Array.from({ length: 11 }, (_, i) =>
    row(`dep-gh-${i + 1}`, 'waiting_dependency', {
      wait_reason: 'GitHub API rate limited; retry at reset',
      age_secs: AGE - i * 30,
    }),
  ),
}

const DEGRADED = {
  ...POPULATED,
  degrade_reason: 'adaptive_decrease',
  lanes: { subagents: { effective: 2, user_max: 14, running: 2 }, spawn_gate: { effective: 2, user_max: 8 } },
}

const STALLED = {
  ...POPULATED,
  stalled: {
    'dashboard:chat-7': {
      reason: 'no tool output for 11m',
      since_ts: NOW - 11 * 60,
      age_secs: 11 * 60,
      evidence: ['shell child absent', 'no cpu delta'],
    },
  },
}

const NO_STORE = { ...base, available: false }

// `degrade_reason` and `stalled` are the two states the card colours and
// explains, and neither is reachable from the two baseline scenes; the fold
// line, the 320px stack and the two store-unavailable notices are likewise
// only observable from a payload shaped for them.
const SHOTS = [
  { name: 'tasks-capacity-empty', payload: EMPTY },
  { name: 'tasks-capacity-populated', payload: POPULATED },
  { name: 'tasks-capacity-degraded', payload: DEGRADED },
  { name: 'tasks-capacity-stalled', payload: STALLED },
  { name: 'tasks-capacity-folded', payload: MANY },
  { name: 'tasks-capacity-narrow', payload: POPULATED, viewport: { width: 320, height: 1200 } },
  { name: 'tasks-capacity-no-store', payload: NO_STORE },
  // The load-error arm renders its own notice and never mounts the badge, so
  // it waits on a different element than every other scene.
  { name: 'tasks-capacity-error', status: 500, ready: 'tasks-capacity-error' },
]

const { srv, base: origin } = await serveDist()
const browser = await chromium.launch()

try {
  for (const { name, payload, viewport, status, ready } of SHOTS) {
    const context = await browser.newContext({
      viewport: viewport ?? { width: 1280, height: 1000 },
      deviceScaleFactor: 1,
    })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      theme: 'light',
      extra: async (path, route) => {
        if (path !== '/api/tasks/summary') return false
        if (status) await route.fulfill({ status, body: '{}', contentType: 'application/json' })
        else await json(route, payload)
        return true
      },
    })
    await page.goto(`${origin}/developer?tab=system&plane=services`, { waitUntil: 'domcontentloaded' })
    const card = page.getByTestId('tasks-capacity-card')
    await card.waitFor({ timeout: 10000 })
    // The badge only mounts once the summary resolved; a frame before that is a
    // card with dashes, which would pass the harness and prove nothing.
    await page.getByTestId(ready ?? 'tasks-capacity-health').waitFor({ timeout: 15000 })
    await page.waitForTimeout(300)
    const path = `${OUT}/${name}.png`
    await card.screenshot({ path })
    console.log('wrote', path)
    await context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
