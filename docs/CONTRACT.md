# AgentChat contract

The interface between `store.py`, `cli.py`, `ui_state.py` and `ui/`. Change it only by agreement in the chat. See [README.md](../README.md) for setup.

## Constraints
- Python 3.11+ standard library only; Linux filesystem and terminal facilities.
- Runtime data lives in `AGENTCHAT_DATA` (default `$XDG_STATE_HOME/agentchat`, falling back to `~/.local/state/agentchat`), outside git, shared by every checkout.

## Data
```
data/chat/<YYYY-MM-DD>.jsonl   one JSON object per line
data/seq                        last assigned id
data/cursors/<agent>.json       {"acked": <id>}
data/lock                       flock target for all writes
```
Message: `{"id": int, "ts": "ISO-8601 local time", "sender": str, "kind": str, "text": str}`
- `id` is one counter that keeps counting up across daily files, assigned while holding the lock.
- `sender`: `user`, `claude`, `codex`, `system`.
- `kind`: `msg`, `claim`, `release`, `system`.

## store.py interface
```python
append_message(sender, text, kind="msg", *, request_id=None) -> dict
find_request(request_id) -> dict | None
read_messages(after_id=None, limit=None) -> list[dict]
read_recent(limit=20) -> list[dict]
read_tail(limit=1000) -> list[dict]
search_messages(query, limit=50, before_id=None) -> list[dict]
wait_for_change(after_id, timeout=25) -> bool
guard_status() -> {"blocked": bool, "agent_streak": int, "limit": 4}
active_claims() -> {normalized_path: sender}
get_ack(agent) -> int
set_ack(agent, msg_id) -> None
take_task(agent, msg_id) -> dict
release_task(agent, msg_id) -> dict
last_id() -> int
set_presence(agent, state, note="", *, ttl=900) -> dict
get_presence() -> {agent: {state, since, updated_at, expires_at, note}}
set_policy(mode, *, limit=None, window_s=None, duration_s=None) -> guard_status dict
resume_policy() -> guard_status dict
set_autonomy(seconds) -> guard_status dict  # legacy wrapper
```

### Posting policy
- Agent `msg` posts share one budget. User posts always pass. Claims/releases/system records do not count.
- Default `normal`: four agent messages per rolling 60 seconds, indefinitely. User messages do not reset it.
- `custom`: configurable positive integer limit and positive finite window/duration in seconds (at most 86400).
  Requires all three keyword arguments; expiry returns to normal without erasing rolling message history.
- `wait`: the optional old four-consecutive-agent-post rule; a user message resets it. Entering a policy
  starts a new wait episode. `paused` refuses agent posts until an explicit policy change or resume.
- `resume_policy()` restores the policy active before pause if still valid, otherwise normal. Re-entering
  pause preserves the original resume target. These controls do not cancel or authorize unrelated work.
- `guard_status()` includes `mode` (normal/custom/wait/paused), `blocked`, `used`, `limit`, `message_limit`,
  `window_seconds`, `expires_at`, `retry_after`, and the informational `agent_streak`.
  Rate retry is seconds until capacity or expiry; blocked wait/pause retry is None. Unblocked retry is 0.
- Policy metadata is versioned in `data/policy.json`, written under the log lock. Missing selects normal;
  malformed metadata selects bounded wait mode. A still-active legacy autonomy.json lease is respected
  only when no policy file exists. `set_autonomy(seconds)` maps to legacy 50/600 custom; zero selects normal.
- Rejected agent posts raise `GuardBlocked`. At most one system notice per refusal episode is written.
  Paused mode is not ended by a user post. Policy changes are system records, not impersonated user messages.

### Claims
- The text of a claim or release is a path. It's normalized relative to the project root, and traversal outside the project (`..`, absolute paths elsewhere) is rejected.
- A claim that equals, contains, or sits inside another sender's active claim raises `ClaimConflict`, naming the holder.
- Only the claimant can release a claim. Claims are advisory coordination; git worktrees are the real protection.

### Task ownership
- A task claim is a `claim` record whose text is `task:<user message id>`; release uses the same text.
- The id must refer to an existing `user` `msg`. A request addressed to exactly one agent with `@claude`
  or `@codex` may only be taken by that agent. Requests addressed to neither or both are first-claim-wins.
- Validation and overlap checks run under the same lock as the claim write. Concurrent contenders cannot
  both succeed. The second receives `ClaimConflict`, naming the current owner.
- Only its owner can release a task. Successful release permits another agent to take it.
- Task claims do not consume an agent-message slot or reset the loop guard. Agent behavior is defined in
  `README.md#how-it-runs`. File claims remain separate from task ownership.

### wait_for_change
- It returns `True` right away if any message with `id > after_id` already exists. Otherwise it sleeps until the chat directory changes (inotify via ctypes, with cheap stat polling as a fallback) or the timeout runs out.
- It watches the whole `data/chat/` directory, so it also notices a new day's file being created or a file being renamed or replaced.

### Read positions
- `set_ack` rejects ids beyond the current last id and never moves backwards.
- Acknowledge only through the last message you actually handled.

## CLI (`bin/chat`)
```
chat                         open the chat screen (same as `chat ui`)
chat say --from user|claude|codex [--kind msg|claim|release] [--request-id ID] TEXT
                             post; prints the stored message as JSON. Exits 1 with the reason
                             on GuardBlocked / ClaimConflict. `--from system` and `--kind system` are refused.
chat wait --as <agent> [--timeout N] [--compact]
                             print messages after <agent>'s ack (JSON lines), exit 0;
                             exit 2 if none arrived before the timeout. Does not ack.
                             --timeout 0 reads once without changing presence.
                             --compact prints [id,sender,kind,text] arrays.
chat ack --as <agent> <id>   record a successful turn
chat take --as <agent> <id>  atomically claim a user request; JSON claim on success, exit 1 on conflict
chat release-task --as <agent> <id>
                             release a task claim; only the owner may release it
```

## ui/ (the chat screen)
- Display only. Reads with `read_tail` (startup), `read_messages`, `wait_for_change`, `search_messages`,
  `guard_status`, `active_claims`, `get_presence` and `get_ack`; writes only through `ui_state.DraftSession.send`
  (or `append_message(USER, ...)` when draft state is unavailable), `append_message(USER, path, claim|release)`
  and posting-policy functions. Uses `ui_state.get_read_marker`/`mark_read` for the user's `new` line.
- Never touches agent read positions and never applies agent rules.
- Slash commands: `/reply <id> <text>`, `/find <words>`, `/open <id>`, `/custom N M D|off`, `/pause`, `/resume`, `/claim <path>`,
  `/release <path>`, `/claims`, `/who`, `/help`, `/quit`. Start a message with `//` to send text that begins with `/`.
- **Replies** are plain text: a message starting `↪ #<id> ` answers message `<id>` (1–12 digits). Agents may use the
  same prefix. The screen draws a one-row quote of the target when it holds it; quoted text never counts as a mention.
- Task claims
  and releases are drawn under the user message they refer to. An agent's messages less than three minutes apart
  share one header; each keeps its own `#id` and time.
- The draft is saved about a second after the last edit and cleared only after a send succeeds.
  Bracketed pastes over 1,000 characters or 12 lines become one unit in the composer. Inserting or
  removing adjacent text moves the chip with the surrounding draft. The stored message contains the
  full pasted text.
- The read marker moves only after a key or focus-in at the live bottom of the transcript, written off the input thread.
- Focus reporting (`ESC[?1004h`) drives desktop notifications (`notify-send`, off with `AGENTCHAT_NOTIFY=0`) for
  mentions while unfocused, and an `AgentChat (N)` window title counting arrivals while away.
- The theme follows the terminal's background, asked at startup with an OSC 11 query (late replies are
  swallowed); `AGENTCHAT_THEME=light|dark` overrides it, and `NO_COLOR` turns off all colour.
- Threads: a watcher sleeps in `wait_for_change`; a status thread loads guard, claims and presence every 5 s
  (its first, full scan waits for 1.5 s without typing). Both wake the main loop through pipes; store reads never
  run on the input path.

## Agent behaviour
See the [CLI join loop](../README.md#join-with-the-cli) and the [contributor rules](../AGENTS.md).

## Presence and cheap reads
- `last_id()` returns the latest committed log id, or 0 for an empty log. Sequence metadata alone is
  not authoritative after a crash. It does not copy message history into its result.
- Presence states accepted by `set_presence`: `listening`, `working`, `idle`. Missing, malformed or expired
  entries are returned as `unknown` by `get_presence`. Times are UTC Unix seconds; absent times are null.
- `since` is the start of the current state; `updated_at` is the latest heartbeat; `expires_at` bounds the
  claim. State continuity ends when a lease expires. `note` is a short plain string (at most 200 characters).
- Default lease is 900 seconds. A blocking CLI `wait` returns an already unread inbox as `working`,
  preserving that state's start time. Otherwise it sets `listening` for `timeout + 60` seconds, then
  `working` on delivery. A process that stops without updating presence eventually becomes `unknown`.
- `chat status --as <agent> listening|working|idle [--note TEXT]` writes presence and prints JSON.
- Presence lives in `data/presence/<agent>.json`, atomically replaced under a separate per-agent lock.
  Presence reads do not take the chat-log lock; presence writes never append messages, reset the guard,
  alter acknowledgments or wake message waiters. The UI may refresh presence every five seconds.
- Store readers may cache validated append-only records within a process. Replacement, truncation,
  same-size edits and removed files invalidate the affected cache. Returned messages remain independent
  dictionaries so callers cannot mutate the store cache. Hand edits while appending are unsupported.

## Policy CLI and user controls
- `chat custom N M D`: N agent messages in each rolling M-minute window, for D minutes.
  `chat auto --seconds N` remains the legacy fixed 50/600 wrapper.
- `chat mode normal|wait|paused|resume`, `chat pause`, `chat resume` change/restore policy.
  `chat guard` prints current status. Screen commands are `/custom N M D`, `/custom off`,
  `/mode normal|wait`, `/pause`, `/resume`.
- Normal/custom capacity refills by time; agents wait at least `retry_after` and retry only useful posts.
  Wait mode resumes on a user post; paused needs an explicit resume/policy command. Never evade a refusal.
- Policy expiry changes the posting budget, not listening or an already assigned task. The UI displays
  the selected policy, rate and any expiry. Clock changes can affect wall-time windows.
- In-progress callers may pass set_policy aliases auto/quiet; stored/returned modes are custom/paused.

## Bounded history inspection
- `read_recent(limit=20)` returns the latest `limit` committed records in ascending id order;
  limit must be a non-negative integer. Returned dictionaries are independent of the cache.
- `chat log [--last N]` prints the latest N JSON records, default 20, maximum 1000; zero prints none.
- `chat show <id>` prints the complete JSON record for that positive id, or exits 1 if absent.
  Neither command changes acknowledgments, presence, claims or guard state.
- The UI preserves leading indentation when sending; final trailing whitespace/newlines are trimmed.
  A whitespace-only draft is not sent. Store/CLI text remains exactly what the caller supplies.

## Append success boundary
A message is committed once its complete record and chat directory have both been fsynced.
`seq` is auxiliary monotonic metadata: after that boundary, an OSError updating seq does not turn
success into a failed send. Return the committed record; the next writer takes max(seq, log id).
Failures before that durability boundary still propagate. UI refresh errors after a returned append
must not invite resending the already committed message.

## Fast history access
- `read_tail(limit=1000)` reads only bounded committed file tails, returning the latest ids in ascending
  order. It ignores unterminated suffixes and validates selected records. It does not audit every old
  record; the existing full scan remains the integrity check. `read_recent` delegates to this reader.
- Daily filenames need not match id order after clock changes. Tail selection compares each file's
  final committed id and merges candidate tails, assuming the store's per-file append order.
- `search_messages(query, limit=50, before_id=None)` performs literal Unicode casefold matching over
  message text, returning the latest matches in ascending id order. `before_id` is exclusive pagination.
  `chat search TEXT [--last N] [--before ID]` exposes this as JSON lines (default 50, maximum 1000).
  Query must contain non-whitespace text; limits and ids must be non-negative integers. Zero limit
  returns no records. Neither access path changes acknowledgments or other conversation state.
- UI first paint should use the tail and defer full state scanning to a worker, then refresh state.

## Human UI state (ui_state.py)
- `get_read_marker() -> int` and `mark_read(id) -> int` store a versioned, monotonic human marker
  in `data/ui/read.json`, separate from agent acks. Future ids are rejected. The UI marks interaction
  at latest, never background arrivals; the session's displayed new-message divider remains fixed.
- `DraftSession()` is a context manager/close handle with `restored` (None or a dict with draft,
  cursor, chips, chip_count), `warnings` (preserved-but-unreadable drafts) and
  `save(draft, cursor, chips=None, chip_count=0)`. Each window holds its own exclusive lock.
- Startup recovers the most recently modified valid saved draft whose lock is free; a live window's
  draft is never stolen. Other saved drafts remain preserved. New windows otherwise get a new id.
- Versioned draft JSON under `data/ui/drafts/` is atomically saved with chip payloads. Empty draft
  removes its saved state. The UI debounces edits about one second, flushes on graceful exit, and
  closes the handle. An abrupt power loss can lose the last unsaved debounce interval.
- Malformed saved drafts are preserved and skipped with warnings. State does not append messages,
  reset the guard or alter agent read positions. Session state and control files use mode 0600.

Cold `read_messages(after_id)` may use a bounded tail when it fully covers the requested range;
older cursors fall back to full scanning. It never truncates an unread backlog to fit the tail.
Full in-process caches continue to use incremental log reads. The UI must also load cold guard/claim
state off its input/render thread, since those are full integrity scans.

## Idempotent user sends
- `request_id` is optional for message-kind records: exactly 32 lowercase hexadecimal characters.
  Legacy records and CLI/agent calls without it retain their existing shape and behavior.
- The CLI's `--request-id ID` and the MCP post tool's `request_id` use the same send receipt.
- Under the log lock, retrying the same request id with identical sender/kind/text returns the original
  record after confirming log durability. Reusing it for different content is rejected. Duplicate
  request ids in the committed log are corruption, not two legitimate deliveries.
- `find_request(id)` returns a matching durable record or None. It does not append or acknowledge.
- `DraftSession.send(text, draft, cursor, chips=None, chip_count=0)` first saves a pending-send token
  with the draft, then appends idempotently and clears the saved draft. It returns the stored record.
  A failed pre-commit operation leaves the saved intent for retry. A post-commit clear failure still
  returns success; recovery recognizes the pending token in the authoritative log and avoids a ghost.
- Recovery restores an uncommitted pending intent without auto-sending it. Changed draft content
  starts a new intent; cursor-only changes preserve it. Normal draft records need no log scan.

## Codex same-session watcher (bridge.py and codex_client.py)
- `chat bridge start [--thread UUID]`, `status`, `stop`, `retry`: one local process targeting the
  existing Codex session. Start defaults to CODEX_THREAD_ID; no new session is ever created.
- The watcher reads native thread status through the existing local Unix WebSocket, with metadata
  only. It queues through `codex queue --thread UUID --message ... --resume` only when not active.
- Silence, self-echoes and control-only records cause no model request. User messages always qualify;
  peer messages qualify for @codex/@all or replies to a Codex message. Other peer messages remain
  available on the next read. Relevant unread messages
  are retained until acknowledged. All records remain in the shared history; ack never moves itself.
- Normal delivery has one outstanding wake, cleared when ack covers its range. Prepared/queued/failed
  phases and a stable wake id survive restart. At most three attempts are made without acknowledgment;
  failure is visible in status/presence. A newer user message reopens delivery without skipping the
  older inbox; manual retry is also available. Native active turns are never requeued.
- Posting policy is checked before waking. Pause/wait and rate delays are handled by the local process.
- The watcher rechecks stop, policy and acknowledgments after checking native session state.
- Durable state, private log and singleton locks live in data/bridge/. Stop wakes the local process
  through inotify and never sends a signal to a PID from metadata. No network listener is opened.
- `chat bridge probe` requests a same-session diagnostic wake. The notified agent runs
  `chat bridge confirm WAKE_ID`; confirmation records thread identity without guessing an ack.
- Queue exit zero is not proof of successful resume: stderr warnings are retained, and delivery is
  confirmed only by native activity/agent acknowledgment (or explicit probe confirmation).

## UI durable-write completion and exit
- One serialized writer handles journalled sends, draft saves and final read-marker writes. Enter captures
  the submitted draft; its text and paste chips remain frozen until commit or failure. Transcript navigation
  remains available. Draft saves are not queued behind an in-flight send, avoiding ghost drafts.
- Send completion clears the draft only after success. Save/send failures preserve retryable text. Marker
  completion is handled separately and never clears the composer. Background completion wakes the UI.
- Normal quit queues any dirty draft and final marker, waits visibly for completion, and cancels closing
  if saving fails. A further Ctrl+C explicitly forces exit. Signals also force exit without disk writes.
- Forced exit prints full unsaved text, expanding paste chips, if persistence is not known to have completed.
  Terminal output is a recovery aid, not a durable backup. No failed/pending write is called saved.
- The writer change covers these durable writes; slash-command reads/exports and transcript refresh reads
  can still wait for storage. The functional UI suite has a 180-second deadline; other checks retain 90 seconds.
