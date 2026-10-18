/**
 * useCloseAction — the Settings > Display > Desktop app "Keep running when the
 * window is closed" switch (#12204).
 *
 * The hook talks to window.closeActionAPI, injected by electron/preload.js. The
 * main process refuses both calls from any page that is not this machine's own
 * dashboard, so the row may only appear after a read succeeds.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useCloseAction } from '../hooks/useCloseAction'

type Api = {
  get(): Promise<string>
  set(action: string): Promise<string>
}

function installBridge(api: Api) {
  ;(window as unknown as { closeActionAPI?: Api }).closeActionAPI = api
}

afterEach(() => {
  delete (window as unknown as { closeActionAPI?: Api }).closeActionAPI
})

describe('useCloseAction', () => {
  it('reports unsupported and stays inert without a bridge (browser, PWA)', () => {
    const { result } = renderHook(() => useCloseAction())
    expect(result.current.closeActionSupported).toBe(false)
    expect(result.current.closeAction).toBe('background')
    act(() => { result.current.setCloseAction('quit') })
    expect(result.current.closeAction).toBe('background')
  })

  it('stays unsupported when the main process refuses the read (remote gateway window)', async () => {
    const get = vi.fn(() => Promise.reject(new Error('close-action:get is restricted to the local dashboard')))
    installBridge({ get, set: vi.fn() })
    const { result } = renderHook(() => useCloseAction())
    await waitFor(() => expect(get).toHaveBeenCalled())
    await act(async () => { await Promise.resolve() })
    expect(result.current.closeActionSupported).toBe(false)
  })

  it('becomes supported with the stored value once a read succeeds', async () => {
    installBridge({ get: () => Promise.resolve('quit'), set: vi.fn() })
    const { result } = renderHook(() => useCloseAction())
    expect(result.current.closeActionSupported).toBe(false)
    await waitFor(() => expect(result.current.closeActionSupported).toBe(true))
    expect(result.current.closeAction).toBe('quit')
  })

  it('reads an unexpected stored value as background', async () => {
    installBridge({ get: () => Promise.resolve('exit'), set: vi.fn() })
    const { result } = renderHook(() => useCloseAction())
    await waitFor(() => expect(result.current.closeActionSupported).toBe(true))
    expect(result.current.closeAction).toBe('background')
  })

  it('writes through the bridge and keeps the value main.js returns', async () => {
    const set = vi.fn(() => Promise.resolve('background'))
    installBridge({ get: () => Promise.resolve('background'), set })
    const { result } = renderHook(() => useCloseAction())
    await waitFor(() => expect(result.current.closeActionSupported).toBe(true))

    await act(async () => { result.current.setCloseAction('quit') })
    expect(set).toHaveBeenCalledWith('quit')
    // The stored value wins over the requested one.
    await waitFor(() => expect(result.current.closeAction).toBe('background'))
  })

  it('re-reads after a failed write so the switch never shows an unstored choice', async () => {
    installBridge({
      get: () => Promise.resolve('background'),
      set: () => Promise.reject(new Error('ipc gone')),
    })
    const { result } = renderHook(() => useCloseAction())
    await waitFor(() => expect(result.current.closeActionSupported).toBe(true))
    await act(async () => { result.current.setCloseAction('quit') })
    await waitFor(() => expect(result.current.closeAction).toBe('background'))
  })

  it('re-reads on window focus, so a tray-menu change shows in an open Settings page', async () => {
    let stored = 'background'
    installBridge({ get: () => Promise.resolve(stored), set: vi.fn() })
    const { result } = renderHook(() => useCloseAction())
    await waitFor(() => expect(result.current.closeActionSupported).toBe(true))
    stored = 'quit'
    await act(async () => { window.dispatchEvent(new Event('focus')) })
    await waitFor(() => expect(result.current.closeAction).toBe('quit'))
  })

  it('a slow earlier write cannot overwrite a later choice', async () => {
    const pending: Array<(v: string) => void> = []
    installBridge({
      get: () => Promise.resolve('background'),
      set: () => new Promise<string>(resolve => { pending.push(resolve) }),
    })
    const { result } = renderHook(() => useCloseAction())
    await waitFor(() => expect(result.current.closeActionSupported).toBe(true))
    act(() => { result.current.setCloseAction('quit') })
    act(() => { result.current.setCloseAction('background') })
    // The later write settles first, then the earlier one.
    await act(async () => { pending[1]('background') })
    await act(async () => { pending[0]('quit') })
    expect(result.current.closeAction).toBe('background')
  })

  it('a focus read that settles after a write does not undo it', async () => {
    const reads: Array<(v: string) => void> = []
    let first = true
    installBridge({
      get: () => {
        if (first) { first = false; return Promise.resolve('background') }
        return new Promise<string>(resolve => { reads.push(resolve) })
      },
      set: () => Promise.resolve('quit'),
    })
    const { result } = renderHook(() => useCloseAction())
    await waitFor(() => expect(result.current.closeActionSupported).toBe(true))
    act(() => { window.dispatchEvent(new Event('focus')) })
    await act(async () => { result.current.setCloseAction('quit') })
    await act(async () => { reads[0]('background') })
    expect(result.current.closeAction).toBe('quit')
  })

  it('a focus during a pending write cannot show the pre-write value', async () => {
    let stored = 'background'
    const writes: Array<() => void> = []
    const get = vi.fn(() => Promise.resolve(stored))
    installBridge({
      get,
      set: (v: string) => new Promise<string>(resolve => {
        writes.push(() => { stored = v; resolve(v) })
      }),
    })
    const { result } = renderHook(() => useCloseAction())
    await waitFor(() => expect(result.current.closeActionSupported).toBe(true))
    const readsBefore = get.mock.calls.length
    act(() => { result.current.setCloseAction('quit') })
    // The window regains focus while the write still waits on its gate.
    await act(async () => { window.dispatchEvent(new Event('focus')) })
    expect(get.mock.calls.length).toBe(readsBefore)
    expect(result.current.closeAction).toBe('quit')
    await act(async () => { writes[0]() })
    expect(result.current.closeAction).toBe('quit')
    // Once the write is answered, focus reads resume.
    await act(async () => { window.dispatchEvent(new Event('focus')) })
    expect(get.mock.calls.length).toBe(readsBefore + 1)
    expect(result.current.closeAction).toBe('quit')
  })

  it('restores the last confirmed value at once when a write fails, and hides the row if the re-read is refused', async () => {
    let refuse = false
    installBridge({
      get: () => (refuse ? Promise.reject(new Error('refused')) : Promise.resolve('background')),
      set: () => Promise.reject(new Error('refused')),
    })
    const { result } = renderHook(() => useCloseAction())
    await waitFor(() => expect(result.current.closeActionSupported).toBe(true))
    refuse = true
    await act(async () => { result.current.setCloseAction('quit') })
    expect(result.current.closeAction).toBe('background')
    await waitFor(() => expect(result.current.closeActionSupported).toBe(false))
  })
})
