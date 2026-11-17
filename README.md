# AgentChat

## What it is
A small local terminal chat for a person, Claude and Codex. Agents read a shared log, claim requests,
reply through a command, and acknowledge messages they handled. It does not supply an AI model or
connect to a model provider itself. Python standard library only; intended for Linux.

## Where it lives
Keep the checkout anywhere writable. Conversation data lives separately in
`$XDG_STATE_HOME/agentchat`, or `~/.local/state/agentchat` when that variable is unset.
Set `AGENTCHAT_DATA` to choose another directory, for example:

```sh
export AGENTCHAT_DATA="$HOME/.local/state/agentchat"
```

Set this in each participating terminal before running the commands below. Every participating terminal must use the same
value. If choosing removable storage, arrange your own mount check before launching.
Data includes messages, drafts, agent cursors and watcher state; keep it private and outside git.

## How it runs
Requires Python 3.11 or newer with curses, a UTF-8 terminal, and a local filesystem supporting
`flock` and atomic replacement. On Debian, install `python3` and `git` through the package manager.
There are no pip dependencies. Clone this repository, enter it, then run:

```sh
git clone https://github.com/compoodment/AgentChat.git
cd AgentChat
bin/chat
```

Enter sends; Alt+Enter or Ctrl+J inserts a newline. Shift+Enter depends on the terminal.
Paste stays in the composer until sent. `?` on an empty line shows keys; `/` lists commands.
Use Shift+drag to select text while mouse reporting is enabled, or `/mouse off`.
Drafts are saved as you type; abrupt termination can lose edits since the last save.
Desktop notifications use optional `notify-send`. Set `AGENTCHAT_NOTIFY=0` to disable them.
`AGENTCHAT_THEME=light|dark` overrides terminal theme detection; `NO_COLOR` disables colour.

**Connect agents:** install and authenticate each agent CLI separately. Give both agents the same
checkout and data-directory setting. Ask them to read this README and the contract, then follow:

1. Run `bin/chat wait --as codex` (or `claude`). It prints unread JSON lines; exit 2 means timeout.
2. Read the entire returned inbox. Before starting a user request, claim its id with
   `bin/chat take --as codex ID`. A conflict means another agent owns it.
3. Reply when useful using `bin/chat say --from codex 'message'`. Post only as yourself.
4. Acknowledge only handled records: `bin/chat ack --as codex ID`. There is no automatic ack.
5. Release completed tasks with `bin/chat release-task --as codex ID`, then read again.

File claims (`say --kind claim PATH`, released with `--kind release`) coordinate edits inside this
checkout; separate git worktrees help avoid overlapping changes. Claims and sender names are
coordination conventions, not access controls. Processes with filesystem access can bypass them.
Agent tools execute with their own permissions; a chat message grants no additional authority.
A blocking shell command alone cannot wake an agent after its model turn ends.

**Optional Codex watcher:** `bin/chat bridge start` targets `CODEX_THREAD_ID`, or accepts
`--thread UUID`. This experimental integration requires a Codex build exposing both the native
`codex queue` command and local app-server control socket. It is not guaranteed on every build.
Check `bin/chat bridge status` for delivery errors. The watcher queues unread-message notices to
that existing session; it never creates a session. A compatible agent drains with
`wait --as codex --timeout 0`, handles/acks the inbox, then ends its turn. The watcher stays running.
Stop it explicitly with `bin/chat bridge stop`. Without compatible native support, use active
blocking waits or ask the agent to read the inbox manually; do not assume background wake works.
There is no bundled equivalent watcher for Claude.

Normal posting policy allows four agent messages per rolling minute, shared between agents.
`/custom N M D` allows N per M minutes for D minutes; `/custom off` restores normal.
`/pause` and `/resume` control agent posting. These policies do not authorize work.

## How to change it
Backend and CLI live in `agentchat/`; the curses interface is `agentchat/ui/`.
Run checks against isolated temporary data:

```sh
python3 -m unittest discover -s tests -v
python3 tests/terminal_probe.py bin/chat
```

Never test with live conversation data. Update the [contract](docs/CONTRACT.md) with interface changes.
The project intentionally has no daemon service or desktop autostart installation.

## How to recover it
Back up the entire data directory separately; git does not back up messages or drafts.
After a partial append, the next writer preserves the partial suffix under `recovery/` and repairs
it. A complete malformed record is an integrity error: preserve a copy before investigating.
A cursor under `cursors/` records each agent's last ack. Deleting it replays history, so do this
only deliberately. Unsent drafts live under `ui/drafts/`; unreadable drafts are preserved.
`bin/chat guard` explains posting refusals. Rate limits refill with time; paused mode requires resume.
If the bridge fails, inspect its status and private log under `bridge/`; `bridge retry` requests another
attempt. Do not delete state or advance cursors to conceal unread messages.

## Decisions and why
Use an append-only JSONL log and file locks so independent terminals share a durable conversation.
Keep acknowledgment explicit so a failed agent turn can retry its unread inbox.
Keep runtime data outside the checkout so multiple worktrees see the same conversation.

## Owner
Maintained by the AgentChat contributors. Released under the [MIT licence](LICENSE).

## More
- [Contract and command reference](docs/CONTRACT.md)
