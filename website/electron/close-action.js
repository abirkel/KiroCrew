"use strict";
//
// What the main window's close button does, extracted from window-lifecycle.js
// so the decision is unit-testable without Electron (mirrors local-gateway.js).
//
// "background" is the long-standing behaviour: the window hides and the app
// keeps running, reachable from the tray / menu bar icon, the Dock on macOS, or
// a relaunch (a second launch shows the existing window). "quit" makes the close
// button end the app through the same path as the Quit menu item, so an
// app-spawned gateway stops with it and a gateway the app only connected to
// keeps running.

/** electron-store key holding the user's choice. */
const CLOSE_ACTION_KEY = "closeAction";
const CLOSE_ACTION_BACKGROUND = "background";
const CLOSE_ACTION_QUIT = "quit";

/**
 * The user's choice, defaulting to "background".
 *
 * Only an exact "quit" makes the close button quit. A store that never held the
 * key, a hand-edited value, or a stale one from another build keeps today's
 * behaviour, so a typo in the config file can never turn closing a window into
 * ending the user's sessions.
 *
 * @param {{get: (key: string) => unknown}} store
 * @returns {"background"|"quit"}
 */
function getCloseAction(store) {
  return store.get(CLOSE_ACTION_KEY) === CLOSE_ACTION_QUIT
    ? CLOSE_ACTION_QUIT
    : CLOSE_ACTION_BACKGROUND;
}

/**
 * Record the user's choice. Anything other than an exact "quit" is stored as
 * "background", so the store only ever holds one of the two values
 * `getCloseAction` reads.
 *
 * @param {{set: (key: string, value: unknown) => void}} store
 * @param {unknown} value
 * @returns {"background"|"quit"} the value written
 */
function setCloseAction(store, value) {
  const next = value === CLOSE_ACTION_QUIT ? CLOSE_ACTION_QUIT : CLOSE_ACTION_BACKGROUND;
  // electron-store rewrites the whole file on every set; skip a no-op.
  if (store.get(CLOSE_ACTION_KEY) !== next) store.set(CLOSE_ACTION_KEY, next);
  return next;
}

/**
 * What one `close` event on the main window should do.
 *
 * - "persist": a quit is already under way, let the window close and save its
 *   geometry (the existing path every quit takes).
 * - "quit": the user wants the close button to quit. The caller cancels this
 *   close and starts the app's quit, which closes the window again with the
 *   quit flag set and so lands on "persist".
 * - "hide": hide the window and keep the app running.
 *
 * @param {{quitting: boolean, action: string}} o
 * @returns {"persist"|"quit"|"hide"}
 */
function decideClose({ quitting, action }) {
  if (quitting) return "persist";
  return action === CLOSE_ACTION_QUIT ? "quit" : "hide";
}

/**
 * Build the main window's `close` listener from its effects, so the whole
 * close sequence is exercised in tests without a BrowserWindow.
 *
 * The action is read per event, so a change applies to the next close without a
 * relaunch. On "quit" this close is cancelled and the app's own quit starts
 * instead: `requestQuit` sets the quit flag before `app.quit()`, so the close
 * that quit issues comes back here as "persist" and the before-quit teardown (an
 * app-spawned gateway, the updater) runs exactly as for the Quit menu item. A
 * macOS fullscreen tray hide still pending from an earlier close is cancelled
 * first so it cannot fire into the quit.
 *
 * @param {object} deps
 * @param {() => boolean} deps.isQuitting
 * @param {() => string} deps.getAction
 * @param {() => void} deps.cancelPendingHide
 * @param {() => void} deps.requestQuit
 * @param {() => void} deps.hide
 * @param {() => void} deps.persist
 * @returns {(event: {preventDefault: () => void}) => "persist"|"quit"|"hide"}
 */
function createCloseHandler({ isQuitting, getAction, cancelPendingHide, requestQuit, hide, persist }) {
  return (event) => {
    const decision = decideClose({ quitting: isQuitting(), action: getAction() });
    if (decision === "quit") {
      event.preventDefault();
      cancelPendingHide();
      requestQuit();
    } else if (decision === "hide") {
      event.preventDefault();
      hide();
    } else {
      persist();
    }
    return decision;
  };
}

module.exports = {
  CLOSE_ACTION_KEY,
  CLOSE_ACTION_BACKGROUND,
  CLOSE_ACTION_QUIT,
  getCloseAction,
  setCloseAction,
  decideClose,
  createCloseHandler,
};
