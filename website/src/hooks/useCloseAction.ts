import { useState, useEffect, useCallback, useRef } from 'react'

// Close-button bridge exposed by electron/preload.js. Like the local-gateway
// switch, the choice lives in the desktop app's own config, because it decides
// how the app on THIS machine behaves, which the gateway cannot answer for the
// client asking it.
//
// Unlike useLocalGateway, a present bridge is not enough to show the control:
// the setting governs only the main window's close button, and the main
// process answers only that window's own dashboard. So `supported` turns true
// only once a read succeeds, and a connection window never shows the row.
//
// The switch only ever shows a value the main process returned. A change in
// flight disables the switch instead of showing a value the close button would
// not yet act on, and Settings is the only writer, so the reply to the newest
// request is the current value.
const closeActionAPI = (): CloseActionAPI | undefined => window.closeActionAPI

const canonical = (v: unknown): CloseAction => (v === 'quit' ? 'quit' : 'background')

/** Read/write what the desktop app's main-window close button does. */
export function useCloseAction() {
  const [supported, setSupported] = useState(false)
  // Starts at the shipped default so a first paint matches what an untouched
  // install does.
  const [closeAction, setCloseActionState] = useState<CloseAction>('background')
  const [pending, setPending] = useState(false)
  // A write the main process refused or failed to store. Shown under the
  // switch until the next attempt, so a toggle that did nothing says so.
  const [failed, setFailed] = useState(false)
  const pendingRef = useRef(false)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    const api = closeActionAPI()
    if (api) {
      void api.get().then(v => {
        if (!alive.current) return
        setCloseActionState(canonical(v))
        setSupported(true)
      }).catch(() => {
        // Refused (not the main window's dashboard) or the bridge is gone:
        // hide the row rather than show a value the app may not be using.
        if (alive.current) setSupported(false)
      })
    }
    return () => { alive.current = false }
  }, [])

  const setCloseAction = useCallback((next: CloseAction) => {
    const api = closeActionAPI()
    if (!api || pendingRef.current) return
    pendingRef.current = true
    setPending(true)
    setFailed(false)
    void api.set(next).then(v => {
      if (alive.current) setCloseActionState(canonical(v))
    }).catch(() => {
      // Nothing was stored, so the switch keeps the value it already shows.
      if (alive.current) setFailed(true)
    }).finally(() => {
      pendingRef.current = false
      if (alive.current) setPending(false)
    })
  }, [])

  return {
    closeAction,
    closeActionSupported: supported,
    closeActionPending: pending,
    closeActionFailed: failed,
    setCloseAction,
  }
}
