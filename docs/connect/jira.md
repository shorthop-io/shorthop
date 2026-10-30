# Jira: one issue per incident

A **Jira channel** opens an issue when an incident starts, keeps it in step with the incident,
and closes it when the incident resolves:

- **Opened** — summary `[P2] #42 KubePodCrashLooping`, the alerts, the incident's link and its
  history line, labelled `shorthop` (plus your own labels).
- **Commented** — everything that happens to the incident afterwards: acknowledged, escalated,
  moved to another team, level changed, notes, [AI triage](ai-triage.md) suggestions, and what
  fixed it. These are the same lines the incident's Slack thread gets.
- **Closed** — when the incident resolves, through the first transition to a "done" status, or
  one you name.

It goes one way only. **Closing the issue in Jira does not resolve the incident**: alerts that
are still firing keep paging. Jira is where the follow-up and post-mortem live, and Shorthop
stays in charge of whether something is still broken.

## Set it up

1. **A Jira account for Shorthop.** Use a service account or a person who may create, comment
   on and transition issues in the project.
   - *Jira Cloud*: create an API token at id.atlassian.com → Security → API tokens. Shorthop
     needs the account's **email** and the **token**.
   - *Jira Data Center / Server*: create a **personal access token** and leave the email empty.
2. **Channels → New → Jira**: the site (`https://acme.atlassian.net`, not a board link), the
   email and token, the **project key** and the **issue type** (`Task`, `Incident`, …).
   Optionally add labels, and a transition name if your workflow has more than one way to done.
3. **Send test** checks the token, the project and the issue type **without** opening an issue.
4. Add the channel to the **first step** of the escalation policies whose incidents should
   become issues. A later step that pages the same channel adds a comment. It never opens a
   second issue.

The token is write-only: it is never shown again after saving.

The incident page links to its issue. If an update could not be posted, the page says how many
are waiting and why. Shorthop keeps retrying, in order.

## A webhook is not enough

The "Jira issue" webhook preset can open an issue, but a webhook never learns the issue's key,
so it cannot comment on the issue or close it. Use the Jira channel kind for that.

A Jira site on a private address (Data Center inside your network) must be added to
`WEBHOOK_PRIVATE_HOSTS` in Settings, as for a webhook.
