#!/usr/bin/env python3
"""A minimal stdio MCP server that records every launch.

Used by ``test_mcp_gateway_pool_integ.py`` as the REAL process gatewayd spawns
behind the pool. Appending one line per launch to the file named in ``argv[1]``
turns the question "how many backends did the pool actually create?" into a
line count -- a closed-box observation that needs no access to the pool's
private state, and therefore keeps working when that state is refactored.

Answers ``initialize``: the pool spawns a backend lazily on the first
non-register frame, so one ``initialize`` per stub is enough to force the
spawn-or-reuse decision the launch count is about.

Given an OPTIONAL second path argument, it also advertises
``kirocrew.caller-identity`` and appends the ``sessionKey`` of every
``tools/call``'s caller block to that file -- turning "did gatewayd hand this
shared backend each session's own identity?" into another line read. Advertising
is bundled with recording deliberately: gatewayd injects only into a backend that
advertised, so a recorder that stayed silent would observe nothing and read as a
broken gateway. Without the argument the handshake is byte-for-byte what the
launch-counting tests already assert against.

An OPTIONAL third path argument records the per-CONNECTION ``nonce`` the same way,
which is the separate question of whether each connection got its OWN namespace --
the one that matters for a caller gatewayd cannot name, where there is no identity
to tell co-tenants apart by. Either path alone turns advertising on.

Stdlib only, and launched as ``sys.executable <this file> <log>`` -- never
through a shell and never via ``-c`` -- so no quoting or backslash assumption
travels onto Windows.
"""

from __future__ import annotations

import json
import os
import sys
import time


def _record(log: str, line: str) -> None:
    """Append *line* to this PROCESS's own file under ``<log>.d/``.

    Never a shared append. ``open(path, "a")`` from several processes is atomic
    only on POSIX, where ``O_APPEND`` makes the seek-to-end and the write one
    step; the Windows CRT emulates append with a separate seek and write, so two
    backends writing at the same moment land at the same offset and one line is
    LOST. The observations here are exactly the clustered kind that hits: the
    storm test's backends all answer ``initialize`` after the same delay, and on
    Windows the coarse timer tick wakes them on the same clock edge, which is how
    a run counted 11 of 12 windows while all 12 stubs were answered.

    One file per pid, appended only by that pid, and the reader concatenates.
    Closed immediately: a backend that lingers must not hold a handle the test
    reads, which on Windows would fail the read with a sharing violation.
    """
    directory = f"{log}.d"
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f"{os.getpid()}.txt"), "a", encoding="utf-8") as fh:
        fh.write(line)


def main() -> int:
    log = sys.argv[1]
    # Optional second argument: a path to record the caller block each tools/call
    # arrived with, one line per call. Passing it also makes this server ADVERTISE
    # ``kirocrew.caller-identity`` -- gatewayd injects the block only into a
    # backend that advertised, so a recorder that did not advertise would observe
    # nothing and read as "injection is broken". Default off so the pooling tests
    # that only count launches keep the exact handshake they assert against.
    caller_log = sys.argv[2] if len(sys.argv) > 2 else ""
    # Optional third argument: a path to record the per-CONNECTION nonce each
    # tools/call arrived with, one line per call. Separate from ``caller_log`` so
    # the tests that assert on identities keep their exact line format: the nonce
    # is a namespace separator, present even when the identity is not, and the
    # question it answers ("did gatewayd give each connection its own?") is a
    # different one.
    tenant_log = sys.argv[3] if len(sys.argv) > 3 else ""

    # One line per process launch.
    _record(log, f"{os.getpid()}\n")

    # Optional slow handshake, for the admission tests: sleep this long before
    # answering ``initialize`` so spawn+initialize windows overlap and the
    # daemon's spawn gate has something to bound. ``FAKE_POOL_WINDOW_LOG`` then
    # records ``<pid> <launch_epoch> <init_answered_epoch>`` per process, from
    # which a test computes how many windows were ever open at once -- again a
    # closed-box observation, needing nothing from the pool's internals.
    init_delay = float(os.environ.get("FAKE_POOL_INIT_DELAY_SECS", "0") or 0)
    window_log = os.environ.get("FAKE_POOL_WINDOW_LOG", "")
    launched_at = time.time()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        if method == "tools/call" and (caller_log or tenant_log):
            params = msg.get("params") or {}
            meta = params.get("_meta") or {}
            block = meta.get("kirocrew.caller") or {}
            tenant = meta.get("kirocrew.tenant") or {}
            # The empty string is a meaningful observation -- it is what a
            # backend sees when nothing injected -- so record it rather than
            # skipping the line.
            if caller_log:
                _record(caller_log, f"{block.get('sessionKey', '')}\n")
            if tenant_log:
                _record(tenant_log, f"{tenant.get('nonce', '')}\n")
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}) + "\n"
            )
            sys.stdout.flush()
            continue
        if method != "initialize":
            continue
        if init_delay > 0:
            time.sleep(init_delay)
        if window_log:
            _record(window_log, f"{os.getpid()} {launched_at:.6f} {time.time():.6f}\n")
        params = msg.get("params") or {}
        capabilities: dict = {"tools": {}}
        if caller_log or tenant_log:
            capabilities["experimental"] = {"kirocrew.caller-identity": {"schemaVersion": 1}}
        reply = {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": capabilities,
                "serverInfo": {"name": "fake-pool-mcp", "version": "1.0.0"},
            },
        }
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
