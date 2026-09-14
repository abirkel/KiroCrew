import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'

import { usePanelDocumentActions } from '../hooks/usePanelDocumentActions'
import { readFile } from '../utils/fileReadQuery'

const realFetch = globalThis.fetch

afterEach(() => {
  globalThis.fetch = realFetch
  vi.restoreAllMocks()
})

function response(text: string, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    text: () => Promise.resolve(text),
  } as unknown as Response
}

function harness() {
  const openFile = vi.fn()
  const showActionError = vi.fn()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tabsCtl = { openFile } as unknown as Parameters<
    typeof usePanelDocumentActions
  >[0]['tabsCtl']
  const { result } = renderHook(() => usePanelDocumentActions({
    tabsCtl,
    slotRef: { current: 'slot-1' },
    queryClient,
    showActionError,
  }))
  return { result, openFile, showActionError }
}

describe('usePanelDocumentActions superseded reads', () => {
  it('opens the tab with the newer read\'s bytes when a newer read overtakes openFile', async () => {
    let resolveOlder!: (response: Response) => void
    let fileReadCalls = 0
    globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes('/api/file-read')) {
        fileReadCalls += 1
        if (fileReadCalls === 1) {
          return new Promise<Response>(resolve => { resolveOlder = resolve })
        }
        return Promise.resolve(response('newer'))
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
      } as unknown as Response)
    }) as unknown as typeof fetch
    const { result, openFile, showActionError } = harness()

    let opening!: Promise<void>
    act(() => {
      opening = result.current.openFile('/x.md')
    })
    await waitFor(() => expect(fileReadCalls).toBe(1))
    await expect(readFile('/x.md')).resolves.toEqual({
      kind: 'ok',
      text: 'newer',
      binary: false,
    })
    resolveOlder(response('older'))
    await act(async () => { await opening })

    // The click still lands (a click that opened nothing would read as a dead
    // chip), but with the bytes the newer read found, never the older ones.
    expect(openFile).toHaveBeenCalledTimes(1)
    expect(openFile.mock.calls[0][1]).toBe('newer')
    expect(showActionError).not.toHaveBeenCalled()
  })

  it('opens the file while the diff prefetch is still pending', async () => {
    let resolveDiff!: (response: Response) => void
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes('/api/file-read')) return Promise.resolve(response('hello'))
      if (String(input).includes('/api/file-diff')) {
        return new Promise<Response>(resolve => { resolveDiff = resolve })
      }
      return Promise.resolve(response(''))
    })
    globalThis.fetch = fetchMock as unknown as typeof fetch
    const { result, openFile } = harness()

    let opening!: Promise<void>
    act(() => {
      opening = result.current.openFile('/x.md')
    })
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/file-read'),
    ))
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })

    try {
      expect(openFile).toHaveBeenCalledWith(
        '/x.md',
        'hello',
        'slot-1',
        expect.objectContaining({ binary: false }),
      )
    } finally {
      resolveDiff({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ diff: '', original: '', status: 'clean' }),
      } as unknown as Response)
      await act(async () => { await opening })
    }
  })

  it('opens a placeholder tab for a 404', async () => {
    globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes('/api/file-read')) return Promise.resolve(response('', 404))
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
      } as unknown as Response)
    }) as unknown as typeof fetch
    const { result, openFile, showActionError } = harness()

    await act(async () => { await result.current.openFile('/missing.md') })

    expect(openFile).toHaveBeenCalledWith(
      '/missing.md',
      expect.stringContaining('File not found on disk'),
      'slot-1',
      expect.objectContaining({ binary: false }),
    )
    expect(showActionError).not.toHaveBeenCalled()
  })
})
