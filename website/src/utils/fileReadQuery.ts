/**
 * The ONE `/api/file-read` read for a side-panel tab.
 *
 * Three callers read a file into a tab: `usePanelDocumentActions.openFile`
 * (chip / tree click), `ChatPage`'s cold-tab hydration (after a reload), and
 * `MarkdownPanel`'s own disk refresh (Refresh, Cancel, the file watch). Each
 * replaces the tab's buffer AND its "is this still text" verdict, so the one
 * thing that must never happen is an OLDER read landing after a NEWER one: the
 * editor would come back over bytes it cannot represent and a save would write
 * them over the newer file. A per-caller counter cannot rule that out, because
 * the callers race each other, not just themselves. So every read takes a
 * ticket here, and a read that was overtaken before it resolved does not hand
 * back its own bytes: it hands back the outcome of the path's NEWEST read, so
 * every caller that asked receives the same, latest answer -- a click that
 * raced the panel's refresh still opens its tab, with the refresh's bytes.
 * Only a read its caller withdrew (an abort on unmount or path change) answers
 * `superseded`. There is deliberately no cache: one extra GET on a reopen is
 * the price of having exactly one order.
 */
import { fileReadUrl } from './fileReadUrl'

/** Outcome of an arbitrated read. `superseded` means the caller withdrew this
 *  read (aborted it) and must apply nothing and say nothing. An overtaken read
 *  never answers `superseded`: it answers with the newest read's outcome. */
export type FileRead =
  | { kind: 'ok'; text: string; binary: boolean }
  | { kind: 'failed'; status: number; error?: unknown }
  | { kind: 'superseded' }

interface LatestRead {
  ticket: number
  promise: Promise<FileRead>
  inflight: number
}

/** The newest read per path, shared by every read still in flight for that
 *  path. An overtaken read adopts the entry's newest promise. The final read
 *  to settle releases the entry, so nothing remains when no read is running. */
const latest = new Map<string, LatestRead>()

// Test-only visibility for proving settled paths do not stay retained.
export function __inflightPathsForTest(): string[] { return [...latest.keys()] }

let nextTicket = 0

const isAbort = (error: unknown): boolean =>
  error instanceof DOMException && error.name === 'AbortError'

export function readFile(filePath: string, signal?: AbortSignal): Promise<FileRead> {
  const ticket = ++nextTicket
  const overtaken = () => latest.get(filePath)?.ticket !== ticket
  // The newest read's outcome, for a read that was overtaken: the caller asked
  // "what is this file now" and the answer is whatever the latest read found,
  // not the older bytes this read happened to fetch. Awaiting the newest
  // promise chains through any further overtakes until one read finishes as
  // the latest.
  const adoptNewest = (): Promise<FileRead> => latest.get(filePath)!.promise

  const run = async (): Promise<FileRead> => {
    // readFile never rejects on transport or body errors: cold-tab hydration
    // and the panel refresh apply its result from a `.then` without a `.catch`.
    const outcomeForThrow = (error: unknown): Promise<FileRead> | FileRead => {
      // An abort is the caller withdrawing (unmount, path change): nothing to
      // say, whether or not a newer read exists.
      if (isAbort(error)) return { kind: 'superseded' }
      if (overtaken()) return adoptNewest()
      return { kind: 'failed', status: 0, error }
    }
    let res: Response
    try {
      res = signal ? await fetch(fileReadUrl(filePath), { signal }) : await fetch(fileReadUrl(filePath))
    } catch (error) {
      return outcomeForThrow(error)
    }
    // The verdict comes from the HEADER, not the body shape: a `.json` text file
    // is served as application/json too, so the content type cannot tell them apart.
    const binary = res.headers.get('X-File-Binary') === 'true'
    let text = ''
    if (res.ok && !binary) {
      try {
        text = await res.text()
      } catch (error) {
        return outcomeForThrow(error)
      }
    }
    if (overtaken()) return adoptNewest()
    return res.ok ? { kind: 'ok', text, binary } : { kind: 'failed', status: res.status }
  }

  // Registered before `run()` starts so that even a synchronous throw inside
  // it sees this read as the path's latest.
  const entry = latest.get(filePath) ?? {
    ticket,
    promise: undefined as unknown as Promise<FileRead>,
    inflight: 0,
  }
  entry.ticket = ticket
  entry.inflight += 1
  latest.set(filePath, entry)
  entry.promise = run().finally(() => {
    entry.inflight -= 1
    if (entry.inflight === 0 && latest.get(filePath) === entry) {
      latest.delete(filePath)
    }
  })
  return entry.promise
}
