# Claude and MCP

Shorthop is an MCP server, so an assistant such as Claude can read your alerts and their
history, and — if you allow it — acknowledge, resolve and silence.

1. **My account → API & MCP**: create a token with `alerts:read` (add `alerts:write` to let the
   assistant act).
2. Add it to Claude Code:

```bash
claude mcp add --transport http shorthop https://oncall.internal.example.com/mcp \
  --header "Authorization: Bearer sh_…"
```

Tokens that can change configuration (`config:write`, `admin`) are refused, because MCP tokens
live in plain text on a laptop. Silences set through MCP last at most 24 hours. Every call is in
the audit log.

An automation can write back too: `add_incident_note` with `suggestion=true` puts an AI-written
summary on an incident, shown apart from people's notes. See [AI triage](connect/ai-triage.md).
