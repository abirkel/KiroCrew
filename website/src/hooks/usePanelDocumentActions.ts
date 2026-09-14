import { useCallback } from 'react'
import { type QueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { clearInlineDraft, getInlineDraft, type usePanelTabs } from './usePanelTabs'
import { i18nT } from '../i18n/t'
import type { Artifact } from '../types'
import { readFile } from '../utils/fileReadQuery'
import { errMessage } from '../utils/thunkError'
import { optsForReplace } from '../pages/chat/replaceGuard'

type MutableRef<T> = { current: T }

export interface PanelDocumentActionsOptions {
  tabsCtl: ReturnType<typeof usePanelTabs>
  /** The slot a document is opened FOR — read at call time, not at render, so
   *  an open that resolves after a chat switch still lands in the slot that
   *  asked (the tab carries `slot`, and artifact breadcrumbs are per slot). */
  slotRef: MutableRef<string | null>
  queryClient: QueryClient
  /** Where a failed read is reported. Nothing in the viewer to lose at that
   *  point, so the host's notice may hand off to the agent. */
  showActionError: (message: string, title?: string) => void
  /** Runs after a tab was opened and focused. ChatPage uses it to reveal the
   *  right dock and dismiss the find pane, which shares that dock; a host whose
   *  panel is permanent passes nothing. Never called on a failed open. */
  onOpened?: () => void
}

/**
 * The three document actions every SidePanel host needs — open a file as a
 * tab, open an artifact as a tab, save a file tab's buffer — shared by ChatPage
 * (through `useChatPageResourcesController`) and the Crew Members page.
 *
 * One implementation, not a copy per host: the read path decides how a 404
 * renders versus how any other failure is REPORTED, the artifact path records
 * the session-involvement breadcrumb, and the save path restamps the tab's
 * dirty baseline and reconciles the inline draft. Each of those is a contract
 * a second hand-written copy would drift from (the Members page's earlier
 * surfaces were exactly such copies of chat infrastructure).
 */
export function usePanelDocumentActions({ tabsCtl, slotRef, queryClient, showActionError, onOpened }: PanelDocumentActionsOptions) {
  // Open a file as a panel tab. Its content is read through the arbitrated
  // `readFile`, so a click cannot land older bytes after a newer refresh,
  // hydration or click already served the path. A 404 renders a placeholder;
  // any other failure is reported instead of shown as the file's text.
  const openFile = useCallback(async (filePath: string, opts?: { replaceId?: string; line?: number; endLine?: number; diffMode?: boolean; canReplace?: () => boolean }) => {
    try { window.dispatchEvent(new CustomEvent('kirocrew-file-open', { detail: { path: filePath } })) } catch { /* ignore */ }
    if ((window as unknown as { __kirocrewPluginHandlesFiles?: boolean }).__kirocrewPluginHandlesFiles) return
    // Capture the slot BEFORE awaiting the read — the same discipline as
    // `saveFile`. `tabsCtl` was bound at the click, so the tab lands in the
    // INITIATING slot's bucket; stamping it with whatever slot is active once
    // the read resolves would route its comment submissions to a chat the user
    // switched to mid-load.
    const slot = slotRef.current ?? null
    try {
      void queryClient.prefetchQuery({
        queryKey: ['file-diff', filePath],
        queryFn: () => api.fileDiff(filePath),
      }).catch(() => { /* best-effort prefetch */ })
      const r = await readFile(filePath)
      // The ticket order protects this result only until readFile resolves, so
      // apply it without awaiting unrelated work.
      if (r.kind === 'superseded') return
      if (r.kind === 'failed' && r.status !== 404) {
        const reason = r.status
          ? i18nT('pages.chatPage.http_status', { status: r.status })
          : errMessage(r.error) || i18nT('pages.chatPage.unknown_error')
        showActionError(i18nT('pages.chatPage.could_not_read_file_reason', { path: filePath, reason }))
        return
      }
      // A 404 is a real answer about the file, so this surface opens a
      // placeholder. Refresh callers report the same status instead.
      const text = r.kind === 'ok'
        ? r.text
        : i18nT('pages.chatPage.file_not_found_on_disk_it_may_have_been_moved_or')
      tabsCtl.openFile(filePath, text, slot, {
        ...optsForReplace(opts),
        binary: r.kind === 'ok' && r.binary,
      })
      onOpened?.()
    } catch (e) {
      // Unexpected failures from the diff prefetch or surrounding work are
      // reported rather than rendered as a tab's content.
      showActionError(i18nT('pages.chatPage.could_not_read_file_reason', { path: filePath, reason: errMessage(e) || i18nT('pages.chatPage.unknown_error') }))
    }
  }, [queryClient, tabsCtl, slotRef, showActionError, onOpened])

  // Open an artifact as a side-panel tab — the artifact twin of openFile, and
  // the single entry point every in-panel artifact affordance routes through
  // (the Artifacts tab's rows and `/artifacts/<slug>` links inside messages).
  // Rendering inline here instead of hard-navigating to the standalone detail
  // page keeps the host's conversation mounted.
  const openArtifact = useCallback(async (slug: string) => {
    if (!slug) return
    const slot = slotRef.current ?? null
    // Opening an artifact is an act of session involvement: record the
    // `referenced` breadcrumb so a merely-read (or merely-linked) artifact
    // joins "This session" instead of sitting in the library section forever.
    // Deliberately fire-and-forget and deliberately NOT awaited — the panel
    // must open at click speed, and the store already enforces
    // one-breadcrumb-per-session so a double click cannot spam the event log.
    // The 403 an incognito slot returns is expected, not an error to surface.
    if (slot) {
      api.recordArtifactReference(slug, slot)
        .then(() => {
          // Re-run the involvement scan so the row moves sections live.
          queryClient.invalidateQueries({ queryKey: ['session-artifact-records', slot] })
        })
        .catch(() => { /* best-effort breadcrumb */ })
    }
    // Seed the tab from the artifact list cache when it is already warm so the
    // body paints immediately; ArtifactPanel's own query is authoritative and
    // overrides kind/content once it resolves, so a miss here costs a spinner,
    // not correctness.
    let kind: Artifact['kind'] = 'markdown'
    let content = ''
    try {
      const art = await queryClient.fetchQuery<Artifact>({
        queryKey: ['artifact', slug],
        queryFn: () => api.artifact(slug),
        staleTime: 10_000,
      })
      kind = art.kind
      content = art.content ?? ''
    } catch { /* fall through — the panel's own query renders the error state */ }
    tabsCtl.openArtifact({ slug, kind }, content, slot)
    onOpened?.()
  }, [queryClient, tabsCtl, slotRef, onOpened])

  const saveFile = useCallback(async (filePath: string, content: string) => {
    // Capture the slot BEFORE awaiting: if the user switches chats mid-save, the
    // draft we reconcile must be the one that owned this save, not whatever slot
    // is active when the write resolves.
    const requestSlot = slotRef.current ?? ''
    const res = await fetch('/api/file-write', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: filePath, content }),
    })
    if (!res.ok) throw new Error(`Save failed: ${res.status}`)
    // The saved bytes become the tab's dirty baseline, so a later re-open of
    // the same path refreshes the buffer instead of (needlessly) preserving it
    // as if it still held unsaved work. Best-effort: a tab that is not open
    // right now is simply not found by id.
    tabsCtl.patchTab(`file:${filePath}`, { savedContent: content })
    // Reconcile the inline-preview draft for the SAVING slot (drafts are
    // slot+path keyed). Clear it ONLY if it still equals what we just saved -
    // if the user typed more while the write was in flight, the draft now holds
    // newer content and must be preserved, not dropped.
    if (getInlineDraft(requestSlot, filePath) === content) clearInlineDraft(requestSlot, filePath)
  }, [tabsCtl, slotRef])

  return { openFile, openArtifact, saveFile }
}
