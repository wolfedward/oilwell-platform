# Final foundation review repair

## RED / GREEN record

- RED: `python -m unittest tests.test_lifecycle tests.test_seed.SeedDatabaseTests.test_concurrent_legacy_manifest_upgrade_accepts_the_winner_hash_once -v` failed because `backend.lifecycle` and `_upgrade_manifest_hash` did not exist.
- GREEN: the same focused command passed after adding OS locking, owned atomic runtime state, the synchronous launcher, and the manifest-CAS helper.
- Additional real-process checks cover a second foreground `--port 0` process, stale state replacement, stop/restart, a foreign-CWD Windows batch launch/stop, and a bound-port child failure.  They use isolated `PLATFORM_LOG_PATH` and `PLATFORM_DATABASE_PATH` roots and clean their child processes.

## Lifecycle design

`backend.lifecycle.InstanceLock` owns `PLATFORM_LOG_PATH/platform.lock` before migration, seeding, and bind, and holds it until shutdown.  It uses `msvcrt.locking(..., LK_NBLCK, 1)` on Windows and `fcntl.flock(..., LOCK_EX | LOCK_NB)` on POSIX.  The operating system releases either lock when a process crashes.  Therefore the log root, rather than a requested port, is the instance boundary.

`PlatformHTTPServer` explicitly sets `allow_reuse_address = False` and daemon request threads.  Startup records a random `instance_id`, PID, host, assigned port, and shutdown token in `runtime.json`; publication writes a same-directory temporary file, fsyncs it, then calls `os.replace`.  Shutdown removes that file only if both instance ID and token still match.  A stale file is safe to replace only after the OS lock is acquired.  Stop continues to send the random token only to loopback `/internal/shutdown`; it never acts on PID data.

## Windows launch behavior

The actual batch commands are:

```bat
pushd "%~dp0"
python "%~dp0server.py" --launch --open
exit /b %RESULT%
```

and equivalently `python "%~dp0server.py" --stop` for stop.  `--launch` starts a hidden child with `CREATE_NO_WINDOW`, then waits for a runtime record whose PID is that child plus `/api/health` 200 before returning zero.  Child exit, lock conflict, bind/seed startup failure, and timeout return nonzero and leave diagnostics in `logs/runtime.log`. `PLATFORM_NO_BROWSER=1` suppresses browser opening in automation.

## Manifest race

Legacy hash migration now compares-and-swaps under the existing SQLite transaction/busy timeout.  If the update affects zero rows, it rereads the manifest: a matching target hash means another starter won and is accepted without another audit record; any other value stays fail-closed.  The concurrent regression proves both callers return successfully and exactly one upgrade audit is written.

## Verification

- `python -m py_compile server.py backend/config.py backend/lifecycle.py backend/http_server.py backend/seed.py` — exit 0.
- Full suite was run in timeout-safe groups: auth API 8/8, auth unit 11/11, database 23/23, health 10/10, seed 12/12, well API 15/15, lifecycle 5/5 — 84 tests passing.
- `git diff --check` — no whitespace errors.

## Residual risk

Windows may transiently deny replacement while a reader has runtime state open; atomic publication retries that short-lived sharing conflict, while readers also retry malformed/transient reads.  An operator wanting deliberately independent instances must provide distinct log roots (and normally distinct database paths), which is the intentional safety boundary.

## Fix round 2 — stop completion barrier

- RED: `test_stop_is_a_completion_barrier_before_an_immediate_launcher_restart` failed on the prior code because `--stop` returned zero while the acknowledged owner's `runtime.json` still existed.
- GREEN: stop now records the target instance ID and shutdown token before its authenticated request.  A 200 `stopping` acknowledgement begins a bounded 10-second completion wait; it returns zero only after the same state file has disappeared and a non-destructive `InstanceLock` probe can acquire and release the OS lock.
- If a different runtime owner appears, stop returns nonzero and records a completion-conflict diagnostic.  If the target remains or a transient atomic-state read never resolves before the deadline, stop returns nonzero with an explicit timeout diagnostic.  No PID is acted upon.
- The lifecycle suite repeats direct stop → immediate `--launch --port 0` three times and the foreign-CWD Windows `start-platform.cmd` → `stop-platform.cmd` cycle twice.  Cleanup requests authenticated stop before terminating only a directly-owned test process, so background launcher children do not outlive the temporary root.
