# Launch Review Fixes — 2026-09-20

Fix the verified findings from the production launch review. Scope = the 15 reported
findings plus 5 named extras (E5, E6, G1, G2, S5). All line references were verified
against the current HEAD (7ee495b).

## Global Constraints

- Suites must stay green after every task: backend `cd backend && python -m pytest`
  (260 passing today), frontend `cd desktop-tauri && npm test` (170) and
  `npm run typecheck`, Rust `cd desktop-tauri/src-tauri && cargo test --locked` (72)
  and `cargo fmt --check`.
- Surgical changes only: touch what the fix requires; no drive-by refactors, no
  comment/formatting churn on untouched lines (repo CLAUDE.md).
- Each fix gets covering tests where the existing suites make it feasible; follow
  the repo's existing test style.
- No new third-party dependencies (backup scheduling uses a shell loop; audio device
  polling uses existing WASAPI APIs).
- Protocol changes (Task 3) are lockstep across backend/Rust/TS — pre-launch, no
  back-compat shims beyond the legacy handling that already exists in the reducers.
- Windows host, bash shell. Backend venv exists at `backend/.venv` (use
  `backend/.venv/Scripts/python.exe -m pytest` if bare `python` resolves elsewhere).
- Commits: one commit per task, conventional-commit style matching `git log`
  (e.g. `fix: ...`).

## Task 1 — Backend realtime correctness (A1, A2, A5)

Files: `backend/app/realtime.py`, `backend/tests/test_realtime.py`.

**A1 — committed_prefix double-join.** On the non-streaming ASR path
(`_persist_single_final`, reached when engine != funasr, codec != wav_pcm_s16le, or
AI_FUNASR_STREAM=false): line ~935-938 computes
`cumulative = join_transcript_text(previous, text)` where
`previous = thread.committed_prefix`, then `_enqueue_question_revision` (~:697) joins
`thread.committed_prefix` again into `thread.question`, and `_commit_question_prefix`
(~:942) then commits the doubled text. Result: chunk N's question contains the prefix
2× and it compounds every chunk. Fix so the prefix is joined exactly once on this
path (mirror how the streaming path feeds `_enqueue_question_revision` — verify with
a test that pushes 3 sequential finals through `_persist_single_final` and asserts
the enqueued/committed question equals prefix+texts with no duplication).

**A2 — uncancelled processing tasks / unbounded flush.** `stop_session` (~:1818-1825)
and `shutdown` (~:1888-1895) cancel `_audio_tasks`/`_answer_tasks` but never the
in-flight `_audio_processing_tasks` (created ~:1308); `flush_session` (~:1791-1792)
gathers them with no timeout, so end_session can hang when FunASR TCP wedges.
Fix: cancel `_audio_processing_tasks` in stop/shutdown (same pattern as the other
task groups) and bound the flush gather with `asyncio.wait_for` using the existing
flush timeout env. Add a test that a never-completing processing task does not hang
stop_session beyond the bound.

**A5 — streamed answer lost on session end.** `flush_session` waits at most
`AI_FINAL_ANSWER_FLUSH_TIMEOUT_SECONDS` (default 10) via `queue.join()` while LLM
streams run under a 60s timeout; the subsequent worker-cancel then discards the
in-flight answer (budget already consumed). Goal: a normally-finishing stream
persisted even when end arrives mid-stream, with end_session latency still bounded
(≤ ~65s worst case; typically milliseconds). Suggested mechanism: after the flush
timeout, wait for in-flight generation tasks to complete persisting, bounded by the
LLM stream timeout, before cancelling; if still unfinished, cancel but persist the
accumulated partial text as an answer row (existing `source`/partial semantics) so
nothing silently vanishes. Choose the least invasive mechanism that meets the goal;
cover with a test (end_session during a slow stream → answer row exists).

## Task 2 — Backend infra (A3, A4, B2)

Files: `backend/app/llm.py`, `backend/app/asr.py`, `backend/app/main.py`,
`backend/tests/test_llm.py`, `backend/tests/test_asr.py`, `backend/tests/test_main.py`.

**A3 — blocking reads on the event loop.** `llm.py` calls
`asr.http_client_kwargs(...)` bare at :220, :384, :761 (inside `_chat`,
`_chat_stream`, `transcribe_via_llm`); the helper does SQLite + Fernet decrypt +
winreg. `asr.py:963` already wraps the identical reads in `asyncio.to_thread` with a
docstring saying they must be off-loop. Wrap the three llm.py call sites the same way.

**A4 — FunASR stream pins the global "asr" slot for the whole session.**
`FunAsrStream.connect` (asr.py:631-633) acquires `paid_call_slot("asr")`, released
only in `_release_connection`; realtime keeps streams alive across utterances, so one
`radio_mode="both"` session holds both permits of default
`AI_ASR_MAX_CONCURRENCY=2` and any second session fails with PaidCallBusyError.
Fix: hold the slot only while actively sending/awaiting transcription work (acquire
per push/finish cycle, release between utterances), preserving the
one-permit-per-active-call semantics the cost control intends. Update/extend
cost-control tests to cover: two concurrent sessions with one idle stream each do
not starve each other.

**B2 — /health/ready fails open for llm/groq engines.** main.py:174 only probes
FunASR when `AI_ASR_ENGINE=funasr`; `asr.validate_funasr_config` (:284-285) returns
early otherwise without checking a GROQ key or an active LLM config. Fix: for
engine=llm/groq, ready-check the actual paid path (groq key configured / an active
LLM config exists) so an unconfigured deployment reports unhealthy. Update
test_main/test_deployment_config accordingly.

## Task 3 — Protocol: answer thread fields + token frames (C1, H1)

Files: `backend/app/db.py`, `backend/app/models.py`, `backend/app/realtime.py`,
`desktop-tauri/src-tauri/src/protocol.rs`, `desktop-tauri/src/stores/live.ts`,
`desktop-tauri/src/shared/overlay-feed.ts`, plus their tests
(`test_db.py`, `test_sessions_api.py`, `test_realtime.py`, `tests/live-store.test.mjs`,
`tests/overlay.test.mjs`, `tests/shared.test.mjs`).

**C1 — answers lose thread identity over REST.** `thread_id`/`request_id`/`revision`
exist only in the WS answer event (realtime.py:1621-1641 broadcast path); the DB
insert (db.py:1087) and `AnswerResponse` (models.py:83-89) lack them, so REST history
answers can never attach to thread cards, and when the REST copy lands before the WS
replay copy, live.ts:116 id-dedupe drops the thread-bearing copy (thread card loses
its persisted marker; SessionDetailPage renders flat). Fix: add the three columns to
the answers table via the existing migration mechanism, persist them in
`add_answer`, include them in `AnswerResponse`, and add the fields to the Rust
passthrough struct (protocol.rs:80-89) so serde keeps them. Frontend `Answer` type
already declares them. Tests: DB migration roundtrip + REST response shape + a
reducer test where the REST copy lands first and the WS replay attaches
thread_id.

**H1 — O(n²) token broadcast.** realtime.py:1591-1593 sends every token with the full
accumulated text duplicated in two fields (`text=answer_text, answer=answer_text`)
plus `delta`. Change the contract lockstep: per-token frames carry `delta` only
(empty/omitted full-text fields), and the terminal `done=true` frame carries the full
text once. Update both reducers (live.ts answer_stream handler, overlay-feed.ts) to
accumulate from `delta` and trust full text only on the done frame — the existing
"catch-up swap" logic already handles terminal frames. Drop one of the twin fields
if both reducers can agree (keep `answer`, drop `text`, or vice versa — pick one and
be consistent across server + both reducers + tests). Update reducer tests to assert
accumulation from deltas and full-text-on-done. Also update F2's five hand-built
payload sites in `_generate_answer` consistently (a local frame helper is welcome
but keep the diff minimal).

## Task 4 — Rust client reliability (E1, E3, E4, E5, E6)

Files: `desktop-tauri/src-tauri/src/ws_client.rs`, `engine.rs`, `outbox.rs`,
`lib.rs` (if the stopped-flag needs plumbing), plus Rust tests.

**E4 — corrupt manifest wipes pending WAVs.** engine.rs:218 silently resets to
`OutboxManifest::empty` on parse failure; startup `sweep_orphans` (engine.rs:252-264)
then deletes every unreferenced WAV; `atomic_write` (:168-170) has no fsync and falls
back to non-atomic copy+delete on Windows rename failure. Fix: (1) on parse failure,
rename the corrupt file to `<name>.corrupt` (keep it) and log; (2) when the manifest
was unreadable/corrupt, skip `sweep_orphans` for that launch so audio files survive
for recovery; (3) call `file.sync_all()` on the tmp file before the rename in
`atomic_write`. Cover with tests (corrupt manifest → no WAV deletion, quarantine file
exists; atomic_write writes readable content).

**E1 — stale engine clobbers the replacement's manifest.** Two mechanisms: (a)
startup `server_next_chunk_seq` (ws_client.rs:173 → engine.rs:195) paginates up to
50 pages × 15s with no cmd_rx polling, so Stop is unprocessed for minutes and the old
task later persists its stale manifest (ws_client.rs:184) over the file the new
engine writes; (b) `spawn_reconcile` (ws_client.rs:83-85) detaches tasks that are
never cancelled and persist after Stop (engine.rs:533). Fix: (a) bound the startup
scan with an overall deadline (~30s) and poll cmd_rx inside the pagination loop
(abort the scan on Stop); (b) track spawned reconcile handles and abort them on Stop;
(c) add a stopped guard (e.g. `Arc<AtomicBool>` in the shared context, set on Stop)
checked before every `persist_manifest` call from ws_client/reconcile paths so a
post-stop persist is a no-op. Tests for (c) at least: persist after stop is skipped.

**E3 — Stop discards buffered end_session.** ws_client.rs:766-769 breaks the loop
without draining `pending_commands`, while `live_end_session` already returned
success on channel-send; the REST fallback only runs when the send itself fails. Fix:
on Stop, drain `pending_commands`; for any buffered `end_session` (inspect the JSON
`type`), best-effort call the REST end (short timeout, errors logged not fatal)
before exiting. Test: queued end_session + Stop → REST end invoked (mock/stub as the
existing tests do).

**E5 — paid_usage_limited retries forever.** outbox.rs:258-263 moves the record to
RetryableFailed with attempts+1 and no cap (contrast `processing_failed` capped at
`MAX_PROCESSING_FAILED_ATTEMPTS`=3). Fix: cap paid_usage_limited attempts (3) then
transition to the terminal failed state with an event so the user knows transcription
is quota-blocked; no infinite ~30s loop. Update outbox tests.

**E6 — reconnect storm after handshake-then-drop.** ws_client.rs:391 resets
`attempt = 0` on every sync_complete; with the `.max(500)` floor (ws_client.rs:829) a
server that completes the handshake then drops produces a permanent 0.5-1.5s
reconnect cycle. Fix: only reset the attempt counter when the connection has stayed
up for a minimum stable window (e.g. 30s since sync_complete); otherwise keep the
attempt count so backoff keeps growing. Test the counter logic.

## Task 5 — Rust overlay + audio device (S1, S4)

Files: `desktop-tauri/src-tauri/src/overlay.rs`, `system_audio.rs`, plus tests.

**S1 — overlay collapse strip impossible.** overlay.rs:264 builds the window with
`min_inner_size(280, 200)`; collapse (:512) calls `set_size(LogicalSize::new(240,
18))` which tao clamps via WM_GETMINMAXINFO, so Ctrl+Alt+E yields a 280×200 block.
Fix: before shrinking, lower the window's min size to the strip geometry (or 0), and
restore the normal min (280×200) on expand, in both the collapse and the seed/restore
paths that can apply strip geometry. Keep the existing geometry tests meaningful
(update them to cover the min-size dance).

**S4 — mid-session default-device switch silently captures silence.**
system_audio.rs pins the default render endpoint once at capture start (:462, no
IMMNotificationClient, no re-enumeration). Fix without new deps: in the capture
packet loop, re-check `GetDefaultAudioEndpoint(eRender, eConsole)` periodically
(e.g. every 2s of loop iterations), compare the endpoint id; on change: emit an
engine event (reuse the ServerError channel with a specific code such as
`system_audio_device_changed`, or a dedicated event if one fits better) and attempt
to restart capture on the new endpoint; if the restart fails, emit the existing
failure event AND send `SetCaptureActive(false)` so the UI/snapshot state corrects.
Test the decision logic (endpoint-id compare + restart-or-correct path) with the
existing mock style used in system_audio tests; if the COM layer cannot be exercised
in tests, factor the pure decision part and test that.

## Task 6 — Frontend perf + test quality (H2, S5)

Files: `desktop-tauri/src/pages/LivePage.tsx`, `src/pages/feeds/AnswerFeed.tsx`,
`src/components/Markdown.tsx`, `src/pages/llm-form.ts` (read-only ref for S5),
`desktop-tauri/tests/llm-form.test.mjs`, plus existing page/store tests.

**H2 — per-token full re-render + O(n²) markdown re-parse.** LivePage.tsx:196
subscribes to the whole store (`useLiveStore()`), so every token re-renders the page,
rebuilds the whole feed (`buildAnswerFeed`), re-renders unmemoized cards, and
Markdown.tsx:186 re-runs `parseBlocks` over each card's full accumulated text. Fix
within the current architecture (no virtualization library): (1) slice the LivePage
subscription into selector-based `useLiveStore(s => ...)` reads so token updates
re-render only the components that need them; (2) memoize answer cards
(`React.memo`) keyed stably; (3) memoize the markdown block parse per source string
(`useMemo` in Markdown, or a module-level bounded cache keyed by source). Verify no
behavioral regressions with the existing tests (history-scroll, live-page-stop,
live-store, markdown) and add a perf-shaped test only if the harness supports it
cheaply — otherwise assert parse caching behavior in markdown.test.mjs.

**S5 — source-string test gives false confidence.** llm-form.test.mjs:131 (and the
69-137 block) asserts SettingsPage/bridge/HomePage behavior by grepping source
strings. Convert at least the edit-mode semantics (editing sends `config_id`) into a
behavioral test that imports the real module (`src/pages/llm-form.ts`) and exercises
the body builder: new → no config_id; edit → config_id = editTarget.id. Keep or
rewrite neighboring greps only as needed; do not delete coverage.

## Task 7 — Ops hygiene (B1, G1, G2)

Files: `backend/compose.prod.yml`, `backend/DEPLOYMENT.md`, `.gitignore` /
`desktop-tauri/.gitignore`, deletions under `desktop-tauri/src/audio/`,
`desktop-tauri/public/worklets/`, `desktop-tauri/src-tauri/src/lib.rs`,
`desktop-tauri/src/api/bridge.ts`, test deletions.

**B1 — backups manual-only, same-host.** Add a scheduled backup service to
compose.prod.yml using the same backend image and a shell loop (no new images):
run `python scripts/backup_sqlite.py` daily (env-configurable interval), keep the
existing `--profile tools` manual container untouched. Read backup_sqlite.py first
and reuse its args/env. Update DEPLOYMENT.md: schedule now exists; document mounting
an off-host path (e.g. NFS/network volume) to satisfy the off-site requirement, and
keep the honest note that off-site sync is an operator responsibility.
test_deployment_config.py likely asserts compose shape — update it.

**G1 — 8MB Vite cache tracked in git.** `git rm -r --cached
desktop-tauri/.vite.bak.0916` and add a `.vite*` ignore rule under
desktop-tauri/.gitignore (do NOT delete the working-tree files if the dev machine
still needs them — removing from index only; state this in the commit).

**G2 — dead browser-mic capture stack.** Delete: `src/audio/recorder.ts`,
`src/audio/wav-meta.ts` (verify no other importer first — G2 audit says only
recorder.ts uses it), `public/worklets/wav-worklet.js`, the `audio_chunk` Tauri
command in lib.rs (:681-705 + its registration :1340) and `api.audio.chunk` in
bridge.ts (:85-92), plus `tests/recorder.test.mjs` and `tests/wav-worklet.test.mjs`.
Re-run full suites; fix any references (e.g. tests/helpers.mjs, tsconfig, README
stop-flow step J3 mentions AudioWorklet — updating that README paragraph is in scope
here since the flow no longer exists after deletion).
