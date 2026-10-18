import { useState, useEffect, useCallback, useRef } from 'react'

// Close-button bridge exposed by electron/preload.js. Like the local-gateway
// switch, the choice lives in the desktop app's own config, because it decides
// how the app on THIS machine behaves, which the gateway cannot answer for the
// client asking it.
//
// Unlike useLocalGateway, a present bridge is not enough to show the control:
// the main process refuses both calls from any page that is not this shell's
// own local dashboard (a connection window on a remote gateway shares the same
// preload). So `supported` turns true only once a read succeeds, and a refused
// read leaves the row hidden; the tray menu's checkbox covers that case.
const closeActionAPI = (): CloseActionAPI | undefined => window.closeActionAPI

const canonical = (v: unknown): CloseAction => (v === 'quit' ? 'quit' : 'background')

/** Read/write what the desktop app's window close button does. */
export function useCloseAction() {
  const [supported, setSupported] = useState(false)
  // Starts at the shipped default so a first paint matches what an untouched
  // install does.
  const [closeAction, setCloseActionState] = useState<CloseAction>('background')
  // Every read and write takes a ticket; only the newest one may update the
  // switch, so a slow earlier answer can never overwrite a later choice.
  const ticket = useRef(0)
  // The last value the main process confirmed, restored when a write fails.
  const confirmed = useRef<CloseAction>('background')
  // Writes not yet answered. The main process applies them in order, so the
  // newest write's answer is authoritative and reads wait for none.
  const pendingWrites = useRef(0)

  const accept = useCallback((mine: number, v: unknown) => {
    if (mine !== ticket.current) return
    confirmed.current = canonical(v)
    setCloseActionState(confirmed.current)
    setSupported(true)
  }, [])

  const read = useCallback(() => {
    const api = closeActionAPI()
    if (!api) return
    // A read made while a write is in flight could return the value from
    // before that write, then shadow its confirmation. The write's own answer
    // is the fresher value, so skip the read.
    if (pendingWrites.current > 0) return
    const mine = ++ticket.current
    void api.get().then(v => accept(mine, v)).catch(() => {
      // Refused (not this machine's dashboard) or the bridge is gone: hide the
      // row rather than show a value the app may not be using.
      if (mine === ticket.current) setSupported(false)
    })
  }, [accept])

  useEffect(() => {
    if (!closeActionAPI()) return
    read()
    // The tray menu can change the value while Settings is open; re-read when
    // the window regains focus so the row never shows a stale choice.
    window.addEventListener('focus', read)
    return () => {
      ticket.current += 1 // drop anything still in flight
      window.removeEventListener('focus', read)
    }
  }, [read])

  // The stored value is authoritative, so the control renders what main.js
  // actually wrote rather than what was asked for.
  const setCloseAction = useCallback((next: CloseAction) => {
    const api = closeActionAPI()
    if (!api) return
    const mine = ++ticket.current
    pendingWrites.current += 1
    setCloseActionState(next)
    void api.set(next).then(v => {
      pendingWrites.current -= 1
      accept(mine, v)
    }).catch(() => {
      pendingWrites.current -= 1
      // Nothing was written: show the last confirmed value at once, then ask
      // the main process what it actually holds.
      if (mine !== ticket.current) return
      setCloseActionState(confirmed.current)
      read()
    })
  }, [accept, read])

  return { closeAction, closeActionSupported: supported, setCloseAction }
}
