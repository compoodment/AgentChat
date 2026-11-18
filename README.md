# AgentChat

## What it is
A small local terminal chat for a person, Claude and Codex. Agents read a shared log, claim requests,
post replies and acknowledge messages they handled. The chat uses Python's standard library and runs
on Linux. Each agent uses its own separately installed client.

## Where it lives
Keep the checkout anywhere writable. Conversation data lives separately in
`$XDG_STATE_HOME/agentchat`, or `~/.local/state/agentchat` when that variable is unset.
Set `AGENTCHAT_DATA` to choose another directory, for example:

```sh
export AGENTCHAT_DATA="$HOME/.local/state/agentchat"
```

Use the same data directory in all participating terminals and optional integrations. If choosing
removable storage, check that it is mounted before launching. Keep messages, drafts, cursors and
watcher state private and outside Git.

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
Pasted text stays in the composer until you send it. A large paste appears as one chip. Backspace
can remove that whole paste, and sending expands the chip to its full text.
Press `?` on an empty line for keys, or `/` for commands. Use Shift+drag to select text while mouse
reporting is enabled, or use `/mouse off`. Drafts are saved as you type. Abrupt termination can
lose changes since the last draft save.
Desktop notifications use optional `notify-send`. Set `AGENTCHAT_NOTIFY=0` to disable them.
`AGENTCHAT_THEME=light|dark` overrides terminal theme detection; `NO_COLOR` disables colour.

### Join with the CLI
Install and authenticate the agent clients separately. Give each agent the same checkout and data
directory. Ask each agent to read this README and follow the loop below using its own name.

1. Run `bin/chat wait --as codex` (or `claude`). It prints unread JSON lines; exit 2 means timeout.
2. Read the entire returned inbox. Before starting a user request, claim its id with
   `bin/chat take --as codex ID`. A conflict means another agent owns it.
3. Reply when useful using `bin/chat say --from codex 'message'`. Post only as yourself.
4. Acknowledge only handled records: `bin/chat ack --as codex ID`. There is no automatic ack.
5. Release completed tasks with `bin/chat release-task --as codex ID`, then read again.

File claims (`say --kind claim PATH`, released with `--kind release`) coordinate edits inside this
checkout. Separate Git worktrees keep concurrent edits apart. Claims and sender names coordinate
trusted processes that share write access to the data directory. Agent tools use their existing
permissions. Keep the data directory writable only by participants you trust.
A blocking shell command works while the agent's turn is active.

### Optional agent integrations

- [Codex MCP tools and watcher](docs/Codex-MCP.md) covers inbox tools, same-session wakeups and
  falling back to shell commands. The tools and watcher are separate.
- [Claude Code channel](docs/Claude-channel.md) covers an experimental way to deliver messages to
  an interactive Claude Code session.

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

Test with disposable data. Update the [contract](docs/CONTRACT.md) with interface changes.
Run `bin/chat --version` to check the version being used.

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
- [Change history](CHANGELOG.md)
- [Contract and command reference](docs/CONTRACT.md)
