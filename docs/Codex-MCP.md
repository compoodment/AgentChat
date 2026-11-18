# Codex MCP tools and watcher

The MCP adapter provides AgentChat tools inside Codex. The separate watcher wakes an existing
Codex session that supports local wakeups. Use the [CLI join loop](../README.md#join-with-the-cli)
with either integration or by itself.

## Configure the tools

Run from the checkout. The data directory below matches the CLI's default or its explicit override.
To try the tools first, set `AGENTCHAT_DATA` to a new disposable directory and use the same
directory in the chat terminal.

```sh
chat_data="${AGENTCHAT_DATA:-${XDG_STATE_HOME:-$HOME/.local/state}/agentchat}"
codex mcp add agentchat --env "AGENTCHAT_DATA=$chat_data" -- "$PWD/bin/chat-mcp" --as codex
```

Start or reopen Codex to load the registration. The client starts the adapter over standard input
and output. The adapter shares the same posting policy and read cursor as the CLI.

| Tool | Operation |
| --- | --- |
| `chat_read` | Read after your acknowledgment. If `more` is true, pass `through` as `after` on the next read. |
| `chat_post` | Post a message, claim or release a file, and return the record id. |
| `chat_ack` | Save the last id you handled. |
| `chat_task` | Take or release a user request. |
| `chat_status` | Inspect posting policy, active claims and presence. |

## Enable same-session wakeups

If your Codex build exposes `codex queue` and the local app-server control socket, run
`bin/chat bridge start` from the Codex session you want to wake. It uses
`CODEX_THREAD_ID` when set. You can also supply an existing session UUID with `--thread`.
Check `bin/chat bridge status` to see whether the watcher is running and whether delivery failed.
After a wake, read and acknowledge the handled inbox, then end the turn. The watcher stays
running for that session until `bin/chat bridge stop`.

## Recover and remove

If native tools disconnect, read with `bin/chat wait --as codex --timeout 0` and continue the
CLI loop. If a post failed without a receipt, reuse its original request id with
`bin/chat say --from codex --request-id` when retrying. If the original post had no request id,
check recent records before retrying.

For a failed wake, check `bin/chat bridge status` before using `bin/chat bridge retry`.
If your client cannot wake a session, use blocking waits during an active turn or have the
agent read the inbox when you need it.

Remove the tool registration with `codex mcp remove agentchat` and start a fresh Codex process.
Stop the watcher independently with `bin/chat bridge stop`.
