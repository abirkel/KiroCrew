import { afterEach, describe, expect, it, vi } from 'vitest'

import { __inflightPathsForTest, readFile } from './fileReadQuery'

const realFetch = globalThis.fetch

afterEach(() => {
  globalThis.fetch = realFetch
  vi.restoreAllMocks()
})

function textResponse(text: string, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    text: vi.fn(() => Promise.resolve(text)),
  } as unknown as Response
}

describe('readFile', () => {
  it('releases a path after its only read settles', async () => {
    const path = '/tmp/settled.md'
    globalThis.fetch = vi.fn(() => Promise.resolve(textResponse('done'))) as unknown as typeof fetch

    await expect(readFile(path)).resolves.toEqual({ kind: 'ok', text: 'done', binary: false })

    expect(__inflightPathsForTest()).not.toContain(path)
  })

  it('keeps a path while an older read remains in flight', async () => {
    const path = '/tmp/still-reading.md'
    let resolveOlder!: (response: Response) => void
    globalThis.fetch = vi.fn()
      .mockImplementationOnce(() => new Promise<Response>(resolve => { resolveOlder = resolve }))
      .mockResolvedValueOnce(textResponse('newer')) as unknown as typeof fetch

    const older = readFile(path)
    const newer = readFile(path)
    await expect(newer).resolves.toEqual({ kind: 'ok', text: 'newer', binary: false })
    expect(__inflightPathsForTest()).toContain(path)

    resolveOlder(textResponse('older'))
    await expect(older).resolves.toEqual({ kind: 'ok', text: 'newer', binary: false })
    expect(__inflightPathsForTest()).not.toContain(path)
  })

  it('an older read of the same path that resolves last hands back the newer read\'s bytes', async () => {
    // Two surfaces asked "what is this file now" and the disk answered twice;
    // both must end up with the LATEST answer, never one with the older bytes
    // and never one with nothing (a click that "opened nothing").
    let resolveOlder!: (response: Response) => void
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => new Promise<Response>(resolve => { resolveOlder = resolve }))
      .mockResolvedValueOnce(textResponse('newer'))
    globalThis.fetch = fetchMock as unknown as typeof fetch

    const older = readFile('/tmp/ordered.md')
    const newer = readFile('/tmp/ordered.md')
    await expect(newer).resolves.toEqual({ kind: 'ok', text: 'newer', binary: false })
    resolveOlder(textResponse('older'))
    await expect(older).resolves.toEqual({ kind: 'ok', text: 'newer', binary: false })
  })

  it('an overtaken read chains to whichever read finishes as the latest', async () => {
    const resolvers: Array<(response: Response) => void> = []
    globalThis.fetch = vi.fn(() => new Promise<Response>(resolve => { resolvers.push(resolve) })) as unknown as typeof fetch

    const first = readFile('/tmp/chain.md')
    const second = readFile('/tmp/chain.md')
    const third = readFile('/tmp/chain.md')
    resolvers[2](textResponse('third'))
    await expect(third).resolves.toEqual({ kind: 'ok', text: 'third', binary: false })
    resolvers[1](textResponse('second'))
    resolvers[0](textResponse('first'))
    await expect(second).resolves.toEqual({ kind: 'ok', text: 'third', binary: false })
    await expect(first).resolves.toEqual({ kind: 'ok', text: 'third', binary: false })
  })

  it('an overtaken read adopts the newer read\'s failure too, never its own stale success', async () => {
    let resolveOlder!: (response: Response) => void
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => new Promise<Response>(resolve => { resolveOlder = resolve }))
      .mockResolvedValueOnce(textResponse('', 500))
    globalThis.fetch = fetchMock as unknown as typeof fetch

    const older = readFile('/tmp/failing.md')
    const newer = readFile('/tmp/failing.md')
    await expect(newer).resolves.toEqual({ kind: 'failed', status: 500 })
    resolveOlder(textResponse('older'))
    await expect(older).resolves.toEqual({ kind: 'failed', status: 500 })
  })

  it('treats an aborted read as superseded and releases its path', async () => {
    const path = '/tmp/aborted.md'
    globalThis.fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener(
          'abort',
          () => reject(new DOMException('aborted', 'AbortError')),
          { once: true },
        )
      }),
    ) as unknown as typeof fetch
    const controller = new AbortController()

    const reading = readFile(path, controller.signal)
    controller.abort()

    await expect(reading).resolves.toEqual({ kind: 'superseded' })
    expect(__inflightPathsForTest()).not.toContain(path)
  })

  it('returns a failed result for a 404', async () => {
    globalThis.fetch = vi.fn(() => Promise.resolve(textResponse('', 404))) as unknown as typeof fetch

    await expect(readFile('/tmp/missing.md')).resolves.toEqual({ kind: 'failed', status: 404 })
  })

  it('returns a binary verdict without reading the response body', async () => {
    const text = vi.fn(() => Promise.resolve('{"binary": true}'))
    globalThis.fetch = vi.fn(() => Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: (name: string) => (name === 'X-File-Binary' ? 'true' : null) },
      text,
    } as unknown as Response)) as unknown as typeof fetch

    await expect(readFile('/tmp/archive.bin')).resolves.toEqual({
      kind: 'ok',
      text: '',
      binary: true,
    })
    expect(text).not.toHaveBeenCalled()
  })

  it('returns a failed result when reading the response body fails', async () => {
    const error = new Error('body dropped')
    globalThis.fetch = vi.fn(() => Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: () => null },
      text: vi.fn(() => Promise.reject(error)),
    } as unknown as Response)) as unknown as typeof fetch

    const result = await readFile('/tmp/dropped.md')

    expect(result.kind).toBe('failed')
    if (result.kind !== 'failed') throw new Error('expected a failed read')
    expect(result.status).toBe(0)
    expect(result.error).toBe(error)
  })

  it('returns the network error when no newer read superseded it', async () => {
    const error = new Error('offline')
    globalThis.fetch = vi.fn(() => Promise.reject(error)) as unknown as typeof fetch

    await expect(readFile('/tmp/offline.md')).resolves.toEqual({
      kind: 'failed',
      status: 0,
      error,
    })
  })
})
