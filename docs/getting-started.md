# First steps

You are signed in as the first admin. To get your first page:

1. **People** — invite your team by email. They sign in with Google (if configured) or with a
   password an admin sets for them. Roles: *viewer* (read), *responder* (acknowledge, resolve,
   silence, take shifts), *admin* (everything).
2. **People → Teams** — group people; routes and schedules belong to teams.
3. **Schedules** — who is on call when: rotations, layers and overrides. Anyone can take a
   shift or swap one from the web or the app.
4. **Channels** — where pages can go besides people: email, Slack, any webhook (Teams,
   Discord, Telegram, Jira … through their webhook URL).
5. **Escalation** — a policy is a list of steps: page the person on call now, the whole team
   after 10 minutes, a channel after 20. Steps repeat until someone acknowledges.
6. **Routing** — match alerts by their labels (`severity=critical`, `namespace=payments`) to a
   team and a policy. The first matching route wins; a route with no matchers at the bottom
   catches everything else. **Test the routing** shows which route a set of labels lands on.
7. **Integrations** — create one per sender and copy its token: see
   [connect Prometheus](connect/prometheus.md).

An alert that matches no route is recorded, but nobody is paged — so always end with a
catch-all route.

**My account → How I'm paged** sets how *you* are paged (phone, email, after how long), and
**My account → Phones** pairs your phone.
