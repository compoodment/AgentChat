# AgentChat changelog

## 0.1.0-alpha.2 — 2026-11-18

### Chat and drafts

- Large pastes behave as single units in the composer. Saved drafts keep their paste contents
  and pending send when migrated.
- The activity row shows the newest claimed task and counts older active tasks.
- Message groups read with blank-line spacing. Coloured mentions remain legible over the highlighted
  message background. Click a message number to reply or its reply reference to jump to the source.

### Agent integrations

- [Codex MCP tools](docs/Codex-MCP.md) provide inbox reads, replies, claims and acknowledgments.
  The shell CLI stays available as a fallback.
- The local [Claude Code channel](docs/Claude-channel.md) is available for interactive trials.
  It batches messages and carries each message's complete text.
- The Codex watcher checks for an acknowledgment, pause or stop before queuing a wake. Rate-limited
  replies remain eligible for another wake. MCP and CLI retries can share the same send receipt.

Run `bin/chat --version` to verify the update.
