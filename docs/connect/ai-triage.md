# AI triage: a summary on every new incident

When an incident opens, Shorthop can hand it to an AI agent of your own. The agent looks into
it with whatever tools you give it (logs, metrics, deploys) and writes back **what is wrong,
the likely cause and how to fix it** as a note on the incident. The note appears on the
incident page, on every open alert's timeline, in the incident's Slack thread and as a comment
on its [Jira issue](jira.md).

Shorthop does not run the agent. It sends the incident out and accepts the answer back, so
the agent, its model and its tools stay yours: an n8n workflow, a small service, anything that
can receive a webhook.

```
incident opens ─▶ webhook channel ─▶ your agent ─▶ your tools (logs, metrics, deploys…)
                                         │
Shorthop ◀── note, marked "suggestion" ──┘
```

## 1. A token for the agent

Create a user for the agent (for example `ai-triage@…`) and, under **My account → API & MCP**,
a token with `alerts:read` and `alerts:write`. Nothing more: the agent only reads and writes
notes. Its name is what the note shows as its author.

## 2. A webhook channel to the agent

**Channels → New → Webhook**, pointing at your agent, with **events: triggered** so it runs
once per incident rather than on every escalation and resolve. A body template that gives the
agent what it needs:

```json
{
  "incident_id": "{{incident_id}}",
  "title": "{{title}}",
  "severity": "{{severity}}",
  "url": "{{url}}",
  "alerts": "{{alerts_text}}",
  "history": "{{history}}"
}
```

`history` is the line Shorthop already knows ("Seen 3 times before. Last fixed by …"). Pass it
on: what fixed this alert last time is often the best lead the agent will get.

Then add the channel as a target in the **first step** of the escalation policies whose
incidents should be triaged, next to the people. The agent then starts at the same moment the
page goes out, and never delays it.

## 3. The agent writes back

Over MCP (the agent is usually an MCP client already):

```
add_incident_note(incident_id, text, suggestion=true)
```

or over the REST API:

```bash
curl -X POST https://oncall.internal.example.com/api/v1/incidents/$INCIDENT_ID/notes \
  -H "Authorization: Bearer sh_…" -H "Content-Type: application/json" \
  -d '{"text": "…", "kind": "suggestion"}'
```

## Why "suggestion"

A note with `kind: "suggestion"` (or `suggestion=true`) is shown as **suggestion · unverified**,
apart from people's notes, in the web app, the phone app and Slack. It is a guess written by
something that was not there, and a responder should never mistake it for a colleague's
finding. It does not feed an alert's history either: "what fixed it" is only ever what a person
records when resolving.

## Good practice

- Give the agent **read-only** tools. It suggests; people act.
- Keep it short: what is wrong, the likely cause, the next step, and links to the evidence.
- Alert text goes to your agent and to whatever model it uses. Decide whether that is
  acceptable for your alerts before you switch it on.
