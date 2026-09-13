import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* Fire (design step 5): the drawer's fire panel. Two steps; the confirm says
 * what goes and what stays; purge is an explicit tick; the outcome is shown
 * -- including a thread the history path kept -- before the roster returns. */

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return { ...actual, api: { fireMember: vi.fn() } }
})
import { api, ApiError } from '../../api/client'
import FirePanel, { firedOutcomeKey, firedOutcomeText } from './FirePanel'

const onFired = vi.fn()

const MEMBER = {
  name: 'Pager-triage',
  slug: 'pager-triage',
  slot_key: '',
  running: false,
  kiro_agent: 'Pager-triage',
  workspace: 'default',
  memory_store: 'default',
  model: '',
  source: 'kirocrew',
  starred: false,
  display_name: 'Pager triage',
} as never

beforeEach(() => {
  vi.mocked(api.fireMember).mockReset()
  onFired.mockReset()
})

const render = () => renderWithProviders(<FirePanel member={MEMBER} label="Pager triage" onFired={onFired} />)
const t = (key: string, opts?: Record<string, unknown>) => {
  const table: Record<string, string> = {
    'pages.membersPage.fired_summary': '{{name}} has been fired.',
    'pages.membersPage.fired_thread_archived': 'Their thread, activity and notes are archived; the thread still opens from <history/> on the Chat page.',
    'pages.membersPage.fired_thread_kept': 'Their activity and notes were deleted, but the thread could not be; it stays in <history/>.',
    'pages.membersPage.fired_thread_kept_cron_claim': "Their activity and notes were deleted, but the thread could not be: a scheduled job's claim on it could not be read. It stays in <history/>.",
    'pages.membersPage.fired_thread_kept_store': 'Their activity and notes were deleted, but the thread could not be: the schedule store could not be read. It stays in <history/>.',
    'pages.membersPage.fired_history_link': 'History',
    'pages.membersPage.fired_thread_purged': 'Their thread, activity and notes were deleted.',
    'pages.membersPage.fired_thread_none': 'They had no thread; their activity and notes are archived.',
    'pages.membersPage.fired_thread_none_purged': 'They had no thread; their activity and notes were deleted.',
  }
  return (table[key] ?? key).replace('{{name}}', String(opts?.name ?? ''))
}

describe('FirePanel', () => {
  it('fires only after the confirm step, archiving by default, and hands the outcome up', async () => {
    const result = {
      ok: true,
      thread: { history_key: 'dashboard:member-pager-triage', state: 'archived' as const },
      lived_state: 'archived' as const,
    }
    vi.mocked(api.fireMember).mockResolvedValue(result)
    render()
    // Step 0 says a confirmation follows and uses the confirm step's own word,
    // "archived": the entry control must read as safe to press, and the two
    // steps must not describe the same place with two vocabularies.
    expect(screen.getByTestId('member-fire-explain')).toHaveTextContent('Nothing is removed until you confirm.')
    expect(screen.getByTestId('member-fire-explain')).toHaveTextContent('archived, not deleted')
    expect(screen.getByTestId('member-fire-explain')).not.toHaveTextContent('History')
    fireEvent.click(screen.getByTestId('member-fire-start'))
    expect(api.fireMember).not.toHaveBeenCalled()
    expect(screen.getByTestId('member-fire-explain')).toHaveTextContent('Fire “Pager triage”?')
    expect(screen.getByTestId('member-fire-explain')).toHaveTextContent('archived, not deleted')
    expect(screen.getByTestId('confirm-fire-member')).toHaveTextContent('Yes, fire')
    fireEvent.click(screen.getByTestId('cancel-fire-member'))
    expect(screen.queryByTestId('confirm-fire-member')).toBeNull()
    fireEvent.click(screen.getByTestId('member-fire-start'))
    fireEvent.click(screen.getByTestId('confirm-fire-member'))
    await waitFor(() => expect(api.fireMember).toHaveBeenCalledWith('Pager-triage', { purge: false }))
    await waitFor(() => expect(onFired).toHaveBeenCalledWith(result, { name: 'Pager-triage', label: 'Pager triage' }))
    // The sentence the roster shows once the drawer is gone with the member.
    // Plain text: the link markup stripped for the accessible name.
    expect(firedOutcomeText('Pager triage', result, t)).toBe(
      'Pager triage has been fired. Their thread, activity and notes are archived; the thread still opens from History on the Chat page.',
    )
  })

  it('purge is an explicit tick, and a thread the history path kept is said, not hidden', async () => {
    vi.mocked(api.fireMember).mockResolvedValue({
      ok: true,
      thread: { history_key: 'dashboard:member-pager-triage', state: 'kept' },
      lived_state: 'purged',
    })
    render()
    fireEvent.click(screen.getByTestId('member-fire-start'))
    expect(screen.getByTestId('member-fire-explain')).toHaveTextContent('archived, not deleted')
    fireEvent.click(screen.getByTestId('member-fire-purge'))
    // The confirm copy follows the tick: it now says what the purge does,
    // never "archived, not deleted" beside a box that says the opposite.
    expect(screen.getByTestId('member-fire-explain')).toHaveTextContent('DELETED, not archived')
    expect(screen.getByTestId('member-fire-explain')).not.toHaveTextContent('archived, not deleted')
    expect(screen.getByTestId('confirm-fire-member')).toHaveTextContent('Yes, fire and delete')
    fireEvent.click(screen.getByTestId('confirm-fire-member'))
    await waitFor(() => expect(api.fireMember).toHaveBeenCalledWith('Pager-triage', { purge: true }))
    await waitFor(() => expect(onFired).toHaveBeenCalledTimes(1))
    const kept = firedOutcomeText('Pager triage', onFired.mock.calls[0][0], t)
    expect(kept).toContain('the thread could not be')
    expect(kept).toContain('stays in History')
    expect(kept).not.toContain('<history')
  })

  it('names the reason a kept thread stayed when the server gave one', () => {
    const base = { ok: true, lived_state: 'purged' as const }
    const key = (reason?: 'cron_claim_unreadable' | 'store_unreadable' | 'refused') =>
      firedOutcomeKey({ ...base, thread: { history_key: 'k', state: 'kept', kept_reason: reason } })
    expect(key('cron_claim_unreadable')).toBe('pages.membersPage.fired_thread_kept_cron_claim')
    expect(key('store_unreadable')).toBe('pages.membersPage.fired_thread_kept_store')
    expect(key('refused')).toBe('pages.membersPage.fired_thread_kept')
    expect(key(undefined)).toBe('pages.membersPage.fired_thread_kept')
    expect(firedOutcomeText('P', { ...base, thread: { history_key: 'k', state: 'kept', kept_reason: 'store_unreadable' } }, t))
      .toContain('the schedule store could not be read')
  })

  it('a fire completing after the user selected another member is attributed to the member it was for', async () => {
    // Fire Alice, select Bob while the request is in flight: the completion
    // names Alice (the member the server deleted), Bob's panel shows no
    // "Firing…" and no error, and Bob is never evicted or announced as fired.
    let settle: (r: unknown) => void = () => {}
    vi.mocked(api.fireMember).mockImplementation(() => new Promise((resolve) => { settle = resolve }))
    const bob = { ...(MEMBER as Record<string, unknown>), name: 'Bob', slug: 'bob', display_name: 'Bob' } as never
    const view = renderWithProviders(<FirePanel member={MEMBER} label="Pager triage" onFired={onFired} />)
    fireEvent.click(screen.getByTestId('member-fire-start'))
    fireEvent.click(screen.getByTestId('confirm-fire-member'))
    await waitFor(() => expect(api.fireMember).toHaveBeenCalledWith('Pager-triage', { purge: false }))
    expect(screen.getByTestId('confirm-fire-member')).toHaveTextContent('Firing…')
    view.rerender(<FirePanel member={bob} label="Bob" onFired={onFired} />)
    // Bob's panel: fresh step 0, no in-flight label of Alice's on it.
    expect(screen.getByTestId('member-fire-start')).toHaveTextContent('Bob')
    expect(screen.queryByText('Firing…')).toBeNull()
    const result = {
      ok: true,
      thread: { history_key: 'dashboard:member-pager-triage', state: 'archived' as const },
      lived_state: 'archived' as const,
    }
    settle(result)
    await waitFor(() => expect(onFired).toHaveBeenCalledTimes(1))
    expect(onFired).toHaveBeenCalledWith(result, { name: 'Pager-triage', label: 'Pager triage' })
    expect(screen.queryByTestId('member-fire-error')).toBeNull()
  })

  it('a refused fire keeps the member in view with the error', async () => {
    vi.mocked(api.fireMember).mockRejectedValue(new ApiError(500, 'history save failed', JSON.stringify({ code: 'thread_close_failed' })))
    render()
    fireEvent.click(screen.getByTestId('member-fire-start'))
    fireEvent.click(screen.getByTestId('confirm-fire-member'))
    expect(await screen.findByTestId('member-fire-error')).toHaveTextContent('history save failed')
    expect(onFired).not.toHaveBeenCalled()
    expect(screen.getByTestId('confirm-fire-member')).toBeInTheDocument()
    // No hand-off on this notice: Ask Agent would navigate away and lose the
    // purge selection and the open confirm step.
    expect(within(screen.getByTestId('member-fire-error')).queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('a refused fire keeps the purge selection in place for the retry', async () => {
    vi.mocked(api.fireMember).mockRejectedValue(new ApiError(500, 'history save failed', JSON.stringify({ code: 'thread_close_failed' })))
    render()
    fireEvent.click(screen.getByTestId('member-fire-start'))
    fireEvent.click(screen.getByTestId('member-fire-purge'))
    fireEvent.click(screen.getByTestId('confirm-fire-member'))
    await screen.findByTestId('member-fire-error')
    expect(screen.getByTestId('member-fire-purge')).toBeChecked()
  })
})
