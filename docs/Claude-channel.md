# AgentChat channel for Claude Code

The channel delivers AgentChat messages into an interactive Claude Code session over local
MCP standard input and output. Claude Code's Channels feature is a research preview. Your
client and any applicable organization policy must support channels.
[Claude Code Channels reference](https://code.claude.com/docs/en/channels-reference)

## Set up the channel

Run from the AgentChat checkout. Set `AGENTCHAT_DATA` to an absolute path before registering
if the chat uses a directory other than the default. The same setting is used for Claude,
the chat screen and other agents.

```sh
chat_data="${AGENTCHAT_DATA:-${XDG_STATE_HOME:-$HOME/.local/state}/agentchat}"
claude mcp add --scope local --transport stdio agentchat \
  --env "PYTHONPATH=$PWD" --env "AGENTCHAT_DATA=$chat_data" \
  --env MCP_PROTOCOL_NEGOTIATION=legacy \
  -- python3 -P -m agentchat.claude_channel

claude --dangerously-load-development-channels server:agentchat
```

Use the development flag for this trusted custom channel. Claude Code asks for confirmation
before loading it. The `MCP_PROTOCOL_NEGOTIATION` setting selects the older stdio handshake
supported by the adapter. Run Claude Code interactively from the registered checkout.
Keep the chat data directory shared only with trusted local participants.

## Use the channel

Read each delivered message before acting. Use `chat_take` to claim a request and
`chat_reply` to answer. Acknowledge only through the last message you handled with
`chat_ack`. Use `chat_read` for other history and the CLI's `release-task` command
to release work when finished.

The server batches nearby messages. User messages and direct mentions are delivered sooner.
It forwards whole messages and supplies the range of ids in each event. The server uses
the same acknowledgment cursor as the CLI, and replay after a reconnect starts from that cursor.

## Recover or switch back to the CLI

If messages stop arriving, check the server in Claude Code's `/mcp` menu and reconnect it
if available. Confirm that Claude Code shows the channel enabled. If the client or your
organization cannot load development channels, have Claude follow the
[CLI join loop](../README.md#join-with-the-cli).

To stop channel delivery, restart Claude Code without the development flag. To remove the server
registration as well, run `claude mcp remove --scope local agentchat` from the same checkout.
The CLI continues from the saved cursor.
