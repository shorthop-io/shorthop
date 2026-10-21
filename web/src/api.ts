/** Shorthop API client. Same-origin, session cookie; every write sends the CSRF token the
 *  server handed out on /api/v1/me (cookie-authenticated writes without it are refused). */

export interface TimelineEvent { at: string; kind: string; actor: string; detail: string }

export interface Alert {
  id: string;
  integration_id: string;
  team_id: string | null;
  fingerprint: string;
  signature: string;
  alertname: string;
  severity: string;
  status: "firing" | "acknowledged" | "resolved";
  labels: Record<string, string>;
  annotations: Record<string, string>;
  generator_url: string;
  started_at: string;
  last_seen_at: string;
  expires_at: string;
  acked_at: string | null;
  acked_by: string | null;
  resolved_at: string | null;
  resolved_how: "source" | "timeout" | "manual" | null;
  resolved_by: string | null;
  resolution_category: string | null;
  resolution_note: string | null;
  send_count: number;
  incident_id: string | null;
  silenced_by: string[];
  /** Labels a label rule set or changed: label -> rule name. */
  rule_labels?: Record<string, string> | null;
}

export interface HistoryItem {
  id: string; status: string; started_at: string; resolved_at: string | null;
  resolved_how: string | null; resolved_by: string | null;
  resolution_category: string | null; resolution_note: string | null;
  labels: Record<string, string>;
}

export interface History {
  signature: string; total: number; last_90_days: number; last_started_at: string | null;
  recent: HistoryItem[]; recent_fixes: HistoryItem[];
}

export interface AlertDetail extends Alert {
  signature_labels: Record<string, string>;
  events: TimelineEvent[];
  history: History;
}

export interface Matcher { name: string; value: string; isRegex: boolean; isEqual: boolean }

export interface Silence {
  id: string; matchers: Matcher[]; starts_at: string; ends_at: string; comment: string;
  created_by: string; team_id: string | null; created_at: string; expired_by: string | null;
  state: "pending" | "active" | "expired";
}

export interface TeamRef { team_id: string; slug: string; name: string; role: string }

export interface User {
  id: string; email: string; name: string; role: "admin" | "responder" | "viewer";
  disabled: boolean; has_password: boolean; created_at: string; last_login_at: string | null;
  teams: TeamRef[];
}

export interface ContactRule { method: "push" | "email" | "slack"; delay_minutes: number; severities: string[] | null }
export interface Me extends User {
  scopes: string[]; csrf_token: string; via: string;
  timezone: string | null; phone: string | null;
  /** The rules in force — the default ones when contact_rules_default. */
  contact_rules: ContactRule[]; contact_rules_default: boolean;
  /** Some severity would reach this person only after a delay (or not at once). */
  contact_gap: boolean;
}

export interface Team {
  id: string; slug: string; name: string; description: string; created_at: string;
  members: { user_id: string; email: string; name: string; role: "owner" | "member" }[];
}

export interface ShadowDiff { alertname: string; labels: Record<string, string> }

export interface ShadowStatus {
  checked_at: string;
  error: string | null;
  prometheus_firing?: number;
  shorthop_open?: number;
  missing: ShadowDiff[];
  extra: ShadowDiff[];
  unconfirmed?: number;
}

export interface Integration {
  archived_at?: string | null;
  id: string; name: string; kind: "prometheus" | "alertmanager" | "webhook" | "internal";
  prometheus_url: string | null; shadow_status: ShadowStatus | null;
  team_id: string | null; prefix: string; extra_labels: Record<string, string>;
  signature_ignore: string[] | null; enabled: boolean; created_at: string;
  last_received_at: string | null; last_watchdog_at: string | null; ingest_path: string;
  token?: string;
  // Shorthop Watch: the check this integration's Watchdog is forwarded to (the ping URL itself
  // is a credential and never comes back).
  watch_check_id?: string | null; watch_forwarded_at?: string | null;
}

/** A Shorthop Watch check — a dead man's switch run by the push relay, outside this install. */
export interface WatchCheck {
  id: string; name: string; interval_s: number; grace_s: number; renotify_s: number | null;
  paused_until: string | null; paused: boolean; state: "new" | "up" | "down";
  last_ping_at: string | null; last_note: string | null; down_since: string | null;
  created_at: string; device_ids: string[];
  forwarded_by: { id: string; name: string } | null;
  // Only in the create / rotate response, ONCE.
  ping_url?: string; snippets?: { curl: string; cron: string; heartbeat_env: string };
}
export interface WatchList { checks: WatchCheck[]; count: number; free: number; can_add_check: boolean }

export interface Token {
  id: string; name: string; prefix: string; scopes: string[]; user_id: string;
  created_at: string; last_used_at: string | null; expires_at: string | null; revoked: boolean;
  token?: string;
}

export interface AuditEntry {
  id: number; at: string; actor: string; action: string; target: string; status: string;
  detail: string;
}

export const RESOLUTION_CATEGORIES: { value: string; label: string }[] = [
  { value: "fixed", label: "Fixed" },
  { value: "restarted", label: "Restarted" },
  { value: "scaled", label: "Scaled" },
  { value: "config_change", label: "Config change" },
  { value: "rollback", label: "Rollback" },
  { value: "upstream", label: "Upstream / dependency" },
  { value: "false_positive", label: "False positive" },
  { value: "no_action", label: "No action needed" },
  { value: "other", label: "Other" },
];

export const SCOPES = [
  "alerts:read", "alerts:write", "oncall:read", "oncall:write", "config:write", "admin",
] as const;

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** A marshmallow 422 names the field under `errors.json` and carries NO `message`; a
 *  hand-written abort carries `message` only. Read both, or every schema rejection renders
 *  as a bare "Unprocessable Content" that nobody can act on. A nested numeric key is a list
 *  INDEX, shown as #1 counting from one. */
export function apiErrorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    const json = (b.errors as Record<string, unknown> | undefined)?.json;
    if (json && typeof json === "object") {
      const parts: string[] = [];
      const walk = (node: unknown, path: string[]) => {
        if (parts.length >= 4) return;
        if (Array.isArray(node) && node.every((x) => typeof x === "string")) {
          parts.push(`${path.join(" ")}: ${node.join(" ")}`.trim());
        } else if (node && typeof node === "object") {
          for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
            walk(v, [...path, /^\d+$/.test(k) ? `#${Number(k) + 1}` : k.replace(/_/g, " ")]);
          }
        }
      };
      walk(json, []);
      if (parts.length) return parts.join("; ");
    }
    if (typeof b.message === "string" && b.message) return b.message;
    if (typeof b.error === "string" && b.error) return b.error;
  }
  return fallback;
}

let csrf = "";

export function setCsrf(token: string) {
  csrf = token;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrf;
  const resp = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (resp.status === 401) {
    // The session expired: back to the login page, returning here afterwards.
    window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
    throw new ApiError(401, "Signed out");
  }
  if (resp.status === 204) return undefined as T;
  const data = await resp.json().catch(() => null);
  if (!resp.ok) throw new ApiError(resp.status, apiErrorMessage(data, resp.statusText));
  return data as T;
}

/** Only `append` and `toString`: the native app runs this file too (generated by
 *  shorthop-native/scripts/sync-api.mjs), and React Native's URLSearchParams has no `set`. */
const q = (params: Record<string, string | string[] | number | undefined>) => {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === "") continue;
    for (const x of Array.isArray(v) ? v : [String(v)]) sp.append(k, x);
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
};

export const api = {
  me: () => request<Me>("GET", "/api/v1/me"),
  updateMe: (body: { name?: string; timezone?: string | null; phone?: string | null }) =>
    request<Me>("PUT", "/api/v1/me", body),
  setContactRules: (rules: ContactRule[] | null) => request<Me>("PUT", "/api/v1/me/contact-rules", { rules }),

  listAlerts: (p: { status?: string; q?: string; label?: string[]; limit?: number }) =>
    request<Alert[]>("GET", `/api/v1/alerts${q(p)}`),
  getAlert: (id: string) => request<AlertDetail>("GET", `/api/v1/alerts/${encodeURIComponent(id)}`),
  ack: (id: string) => request<AlertDetail>("POST", `/api/v1/alerts/${encodeURIComponent(id)}/ack`),
  resolve: (id: string, body: { category?: string; note?: string }) =>
    request<AlertDetail>("POST", `/api/v1/alerts/${encodeURIComponent(id)}/resolve`, body),
  note: (id: string, text: string) =>
    request<AlertDetail>("POST", `/api/v1/alerts/${encodeURIComponent(id)}/notes`, { text }),

  listSilences: (state = "current") => request<Silence[]>("GET", `/api/v1/silences${q({ state })}`),
  createSilence: (body: { matchers: Matcher[]; ends_at: string; comment: string; starts_at?: string }) =>
    request<Silence>("POST", "/api/v1/silences", body),
  previewSilence: (matchers: Matcher[]) =>
    request<Alert[]>("POST", "/api/v1/silences/preview", { matchers }),
  expireSilence: (id: string) =>
    request<Silence>("DELETE", `/api/v1/silences/${encodeURIComponent(id)}`),

  listIntegrations: () => request<Integration[]>("GET", "/api/v1/integrations"),
  createIntegration: (body: { name: string; kind: string; extra_labels: Record<string, string> }) =>
    request<Integration>("POST", "/api/v1/integrations", body),
  updateIntegration: (id: string, body: Partial<Pick<Integration, "name" | "enabled" | "extra_labels" | "prometheus_url">>) =>
    request<Integration>("PATCH", `/api/v1/integrations/${encodeURIComponent(id)}`, body),
  checkIntegration: (id: string) =>
    request<Integration>("POST", `/api/v1/integrations/${encodeURIComponent(id)}/check`),
  deleteIntegration: (id: string) =>
    request<{ deleted: boolean; archived: boolean }>("DELETE", `/api/v1/integrations/${encodeURIComponent(id)}`),
  rotateIntegration: (id: string) =>
    request<Integration>("POST", `/api/v1/integrations/${encodeURIComponent(id)}/rotate`),
  listWatchChecks: () => request<WatchList>("GET", "/api/v1/watch/checks"),
  createWatchCheck: (body: { name: string; interval_s?: number; grace_s?: number; integration_id?: string }) =>
    request<WatchCheck>("POST", "/api/v1/watch/checks", body),
  deleteWatchCheck: (id: string) =>
    request<{ deleted: boolean }>("DELETE", `/api/v1/watch/checks/${encodeURIComponent(id)}`),
  rotateWatchCheck: (id: string) =>
    request<WatchCheck>("POST", `/api/v1/watch/checks/${encodeURIComponent(id)}/rotate`),
  pauseWatchCheck: (id: string, until: string) =>
    request<WatchCheck>("POST", `/api/v1/watch/checks/${encodeURIComponent(id)}/pause`, { until }),
  resumeWatchCheck: (id: string) =>
    request<WatchCheck>("POST", `/api/v1/watch/checks/${encodeURIComponent(id)}/resume`),
  unbindWatch: (integrationId: string) =>
    request<Integration>("DELETE", `/api/v1/integrations/${encodeURIComponent(integrationId)}/watch`),

  listUsers: () => request<User[]>("GET", "/api/v1/users"),
  createUser: (body: { email: string; name: string; role: string }) =>
    request<User>("POST", "/api/v1/users", body),
  updateUser: (id: string, body: Partial<Pick<User, "name" | "role" | "disabled">>) =>
    request<User>("PATCH", `/api/v1/users/${encodeURIComponent(id)}`, body),
  setPassword: (id: string, password: string) =>
    request<void>("PUT", `/api/v1/users/${encodeURIComponent(id)}/password`, { password }),

  listTeams: () => request<Team[]>("GET", "/api/v1/teams"),
  createTeam: (body: { slug: string; name: string; description: string }) =>
    request<Team>("POST", "/api/v1/teams", body),
  addMember: (teamId: string, userId: string, role: string) =>
    request<Team>("POST", `/api/v1/teams/${encodeURIComponent(teamId)}/members`, { user_id: userId, role }),
  removeMember: (teamId: string, userId: string) =>
    request<Team>("DELETE", `/api/v1/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(userId)}`),

  listTokens: () => request<Token[]>("GET", "/api/v1/tokens"),
  createToken: (body: { name: string; scopes: string[]; expires_in_days?: number }) =>
    request<Token>("POST", "/api/v1/tokens", body),
  revokeToken: (id: string) => request<Token>("DELETE", `/api/v1/tokens/${encodeURIComponent(id)}`),

  audit: () => request<AuditEntry[]>("GET", "/api/v1/audit"),
};

/* --- M2: routing, incidents, escalation, delivery ------------------------------------- */

export interface Route {
  id: string; position: number; name: string; matchers: Matcher[];
  integration_ids: string[] | null; team_id: string | null; escalation_policy_id: string | null;
  group_by: string[]; notify_resolved: boolean; enabled: boolean; created_at: string;
}
export type RouteIn = Omit<Route, "id" | "position" | "created_at">;

export interface RouteVerdict { route_id: string; name: string; matched: boolean; why: string; chosen: boolean }
export interface RouteTest { chosen_route_id: string | null; group_labels: Record<string, string>; routes: RouteVerdict[] }

export interface LabelRule {
  id: string; position: number; name: string; matchers: Matcher[];
  text_pattern: string | null; text_field: string | null; set_labels: Record<string, string>;
  integration_ids: string[] | null; stop: boolean; enabled: boolean; created_at: string;
}
export type LabelRuleIn = Omit<LabelRule, "id" | "position" | "created_at">;
export interface LabelRuleTest {
  rules: { id: string; name: string; applied: boolean; why: string; set: Record<string, string> }[];
  labels: Record<string, string>; changed: Record<string, string>; routing: RouteTest;
}

export interface Target { type: "user" | "team" | "channel" | "schedule"; id: string }
export interface Step { delay_minutes: number; targets: Target[] }
export interface Policy { id: string; name: string; team_id: string | null; steps: Step[]; repeat: number; created_at: string }
export type PolicyIn = Omit<Policy, "id" | "created_at">;

export interface Channel {
  id: string; name: string; kind: "email" | "webhook" | "slack"; team_id: string | null;
  config: ChannelConfig; has_secret: boolean; enabled: boolean; created_at: string;
  secret?: string | null;
}
export type WebhookEvent = "triggered" | "escalated" | "resolved";
export interface ChannelConfig {
  to?: string[];
  /** Slack: the channel the app posts into (picked from slackApi.channels()). */
  channel_id?: string;
  channel_name?: string;
  url?: string;
  method?: "POST" | "PUT" | "PATCH";
  /** Values come back masked ("••••••"); send the mask back to keep a stored value. */
  headers?: Record<string, string>;
  body_template?: string;
  events?: WebhookEvent[];
}
export type ChannelIn = Pick<Channel, "name" | "kind" | "team_id" | "config" | "enabled">;

export interface Inhibition {
  id: string; name: string; source_matchers: Matcher[]; target_matchers: Matcher[];
  equal: string[]; enabled: boolean; created_at: string;
}
export type InhibitionIn = Omit<Inhibition, "id" | "created_at">;

export interface Notification {
  id: string; incident_id: string; event: string; step: number | null; kind: string;
  target: string; destination: string; status: "pending" | "sent" | "failed" | "suppressed";
  attempts: number; error: string | null; created_at: string; sent_at: string | null;
}

export interface Incident {
  id: string; number: number; title: string; severity: string;
  status: "triggered" | "acknowledged" | "resolved";
  route_id: string | null; team_id: string | null; escalation_policy_id: string | null;
  group_labels: Record<string, string>; opened_at: string; acked_at: string | null;
  acked_by: string | null; resolved_at: string | null; resolved_by: string | null;
  next_step: number; next_step_at: string | null; open_alerts: number; alert_count: number;
  reassigned_at: string | null; reassigned_by: string | null; reassign_note: string | null;
  team_name: string | null;
  /** P1 … — derived from the alerts until a person sets it (level_manual). */
  level: string | null; level_manual: boolean; level_set_by: string | null; level_set_at: string | null;
}
export interface Level { name: string; severity: string; description: string }
export interface IncidentDetail extends Incident {
  alerts: Alert[]; notifications: Notification[]; route_name: string | null; policy_name: string | null;
}
export interface ReassignIn { team_id: string; escalation_policy_id?: string | null; note?: string; page?: boolean }

export const paging = {
  /** team: "all", "mine" (the caller's teams + unowned), or a team id. */
  listIncidents: (status = "open", team = "all") =>
    request<Incident[]>("GET", `/api/v1/incidents${q({ status, team })}`),
  getIncident: (id: string) => request<IncidentDetail>("GET", `/api/v1/incidents/${encodeURIComponent(id)}`),
  ackIncident: (id: string) => request<IncidentDetail>("POST", `/api/v1/incidents/${encodeURIComponent(id)}/ack`),
  reassignIncident: (id: string, body: ReassignIn) =>
    request<IncidentDetail>("POST", `/api/v1/incidents/${encodeURIComponent(id)}/reassign`, body),
  resolveIncident: (id: string, body: { category?: string; note?: string }) =>
    request<IncidentDetail>("POST", `/api/v1/incidents/${encodeURIComponent(id)}/resolve`, body),
  setIncidentLevel: (id: string, level: string) =>
    request<IncidentDetail>("PUT", `/api/v1/incidents/${encodeURIComponent(id)}/level`, { level }),
  escalateIncident: (id: string) =>
    request<IncidentDetail>("POST", `/api/v1/incidents/${encodeURIComponent(id)}/escalate`),
  addResponder: (id: string, body: { user_id?: string | null; schedule_id?: string | null; note?: string }) =>
    request<IncidentDetail>("POST", `/api/v1/incidents/${encodeURIComponent(id)}/responders`, body),
  addIncidentNote: (id: string, text: string) =>
    request<IncidentDetail>("POST", `/api/v1/incidents/${encodeURIComponent(id)}/notes`, { text }),
  snoozeIncident: (id: string, minutes: number, comment?: string) =>
    request<{ silence_id: string; ends_at: string; matchers: Matcher[] }>(
      "POST", `/api/v1/incidents/${encodeURIComponent(id)}/snooze`, { minutes, comment: comment || null }),
  listLevels: () => request<{ levels: Level[] }>("GET", "/api/v1/levels"),

  listRoutes: () => request<Route[]>("GET", "/api/v1/routes"),
  createRoute: (body: RouteIn) => request<Route>("POST", "/api/v1/routes", body),
  updateRoute: (id: string, body: RouteIn) => request<Route>("PUT", `/api/v1/routes/${encodeURIComponent(id)}`, body),
  deleteRoute: (id: string) => request<void>("DELETE", `/api/v1/routes/${encodeURIComponent(id)}`),
  orderRoutes: (ids: string[]) => request<Route[]>("PUT", "/api/v1/routes/order", { ids }),
  listLabelRules: () => request<LabelRule[]>("GET", "/api/v1/label-rules"),
  createLabelRule: (body: LabelRuleIn) => request<LabelRule>("POST", "/api/v1/label-rules", body),
  updateLabelRule: (id: string, body: LabelRuleIn) => request<LabelRule>("PUT", `/api/v1/label-rules/${encodeURIComponent(id)}`, body),
  deleteLabelRule: (id: string) => request<void>("DELETE", `/api/v1/label-rules/${encodeURIComponent(id)}`),
  orderLabelRules: (ids: string[]) => request<LabelRule[]>("PUT", "/api/v1/label-rules/order", { ids }),
  testLabelRules: (labels: Record<string, string>, annotations: Record<string, string>, integration_id = "") =>
    request<LabelRuleTest>("POST", "/api/v1/label-rules/test", { labels, annotations, integration_id }),
  testRoutes: (labels: Record<string, string>, integration_id = "") =>
    request<RouteTest>("POST", "/api/v1/routes/test", { labels, integration_id }),

  listPolicies: () => request<Policy[]>("GET", "/api/v1/escalation-policies"),
  createPolicy: (body: PolicyIn) => request<Policy>("POST", "/api/v1/escalation-policies", body),
  updatePolicy: (id: string, body: PolicyIn) => request<Policy>("PUT", `/api/v1/escalation-policies/${encodeURIComponent(id)}`, body),
  deletePolicy: (id: string) => request<void>("DELETE", `/api/v1/escalation-policies/${encodeURIComponent(id)}`),

  listChannels: () => request<Channel[]>("GET", "/api/v1/channels"),
  createChannel: (body: ChannelIn) => request<Channel>("POST", "/api/v1/channels", body),
  updateChannel: (id: string, body: ChannelIn) => request<Channel>("PUT", `/api/v1/channels/${encodeURIComponent(id)}`, body),
  deleteChannel: (id: string) => request<void>("DELETE", `/api/v1/channels/${encodeURIComponent(id)}`),
  testChannel: (id: string) => request<{ ok: boolean; error: string | null }>("POST", `/api/v1/channels/${encodeURIComponent(id)}/test`),
  rotateChannelSecret: (id: string) => request<Channel>("POST", `/api/v1/channels/${encodeURIComponent(id)}/rotate-secret`),

  listInhibitions: () => request<Inhibition[]>("GET", "/api/v1/inhibitions"),
  createInhibition: (body: InhibitionIn) => request<Inhibition>("POST", "/api/v1/inhibitions", body),
  updateInhibition: (id: string, body: InhibitionIn) => request<Inhibition>("PUT", `/api/v1/inhibitions/${encodeURIComponent(id)}`, body),
  deleteInhibition: (id: string) => request<void>("DELETE", `/api/v1/inhibitions/${encodeURIComponent(id)}`),

  listNotifications: (status?: string) => request<Notification[]>("GET", `/api/v1/notifications${q({ status })}`),
};

/* --- M3: on-call schedules -------------------------------------------------------------- */

export interface UserRef { id: string; email: string; name: string }
export interface Restriction { days: number[]; start: string; end: string }
export interface Layer { name: string; users: string[]; rotation_hours: number; start: string; restriction: Restriction | null }
export interface OnCallNow { user: UserRef | null; source: string | null; until: string | null }
export interface Override {
  id: string; user_id: string; user: UserRef | null; starts_at: string; ends_at: string;
  reason: string; created_by: string; created_at: string;
}
export interface Schedule {
  id: string; name: string; team_id: string | null; timezone: string; layers: Layer[];
  handover_notify: boolean; created_at: string; on_call: OnCallNow; overrides?: Override[];
}
export type ScheduleIn = Pick<Schedule, "name" | "team_id" | "timezone" | "layers" | "handover_notify">;
/** start/end are clipped to the window asked for (what to DRAW); shift_start/shift_end are the
 *  whole shift when the window cut it (what "override this shift" covers). */
export interface Segment { user: UserRef | null; source: string | null; start: string; end: string; shift_start?: string; shift_end?: string }
export interface Shifts { start: string; end: string; timezone: string; final: Segment[]; layers: { name: string; shifts: Segment[] }[] }
export interface OnCallRow { schedule_id: string; schedule: string; timezone: string; user: UserRef | null; source: string | null; until: string | null }
export interface MyShift { schedule_id: string; schedule: string; timezone: string; source: string | null; start: string; end: string }

export const oncallApi = {
  list: () => request<Schedule[]>("GET", "/api/v1/schedules"),
  get: (id: string) => request<Schedule>("GET", `/api/v1/schedules/${encodeURIComponent(id)}`),
  create: (body: ScheduleIn) => request<Schedule>("POST", "/api/v1/schedules", body),
  update: (id: string, body: ScheduleIn) => request<Schedule>("PUT", `/api/v1/schedules/${encodeURIComponent(id)}`, body),
  remove: (id: string) => request<void>("DELETE", `/api/v1/schedules/${encodeURIComponent(id)}`),
  shifts: (id: string, days = 14, start?: string) =>
    request<Shifts>("GET", `/api/v1/schedules/${encodeURIComponent(id)}/shifts${q({ days, start })}`),
  addOverride: (id: string, body: { user_id?: string; starts_at: string; ends_at: string; reason: string }) =>
    request<Override>("POST", `/api/v1/schedules/${encodeURIComponent(id)}/overrides`, body),
  removeOverride: (id: string, oid: string) =>
    request<void>("DELETE", `/api/v1/schedules/${encodeURIComponent(id)}/overrides/${encodeURIComponent(oid)}`),
  now: () => request<OnCallRow[]>("GET", "/api/v1/oncall"),
  mine: (days = 14) => request<MyShift[]>("GET", `/api/v1/me/shifts${q({ days })}`),
};

/* --- M4: phones ------------------------------------------------------------------------ */

export interface Device {
  id: string; name: string; platform: string; prefix: string; has_push: boolean;
  created_at: string; last_seen_at: string | null; revoked: boolean;
  /** Push relay state, null when no relay is configured or the phone was never tried. */
  relay_status?: "registered" | "refused" | "error" | "signed_out" | null;
  relay_message?: string | null;
  /** The relay will sign this unused phone out at this time unless the app is opened. */
  relay_signs_out_at?: string | null;
}
export interface PairCode { code: string; expires_at: string; link: string }

export const devicesApi = {
  list: () => request<Device[]>("GET", "/api/v1/devices"),
  pairCode: () => request<PairCode>("POST", "/api/v1/devices/pair-codes"),
  revoke: (id: string) => request<Device>("DELETE", `/api/v1/devices/${encodeURIComponent(id)}`),
  // The two calls only a PAIRED PHONE makes (the server refuses them from a browser session);
  // here so the native app's generated client has them.
  registerPush: (pushToken: string) => request<Device>("PUT", "/api/v1/devices/me/push", { push_token: pushToken }),
  unpairSelf: () => request<void>("DELETE", "/api/v1/devices/me"),
};

/* --- calendar (iCal) feeds ------------------------------------------------------------- */

export interface CalendarFeed {
  id: string; kind: "mine" | "schedule"; schedule_id: string | null; name: string; prefix: string;
  created_at: string; last_fetched_at: string | null; revoked: boolean;
  /** Only in the response that created it: the subscription URL is shown once. */
  url?: string; webcal_url?: string;
}

export const calendarApi = {
  list: () => request<CalendarFeed[]>("GET", "/api/v1/me/calendar-feeds"),
  create: (body: { kind: "mine" | "schedule"; schedule_id?: string }) =>
    request<CalendarFeed>("POST", "/api/v1/me/calendar-feeds", body),
  revoke: (id: string) => request<CalendarFeed>("DELETE", `/api/v1/me/calendar-feeds/${encodeURIComponent(id)}`),
  /** A one-off .ics download (same origin, so the session cookie authorises it). */
  downloadPath: (scheduleId?: string) => `/api/v1/me/calendar.ics${q({ schedule_id: scheduleId })}`,
};

/* --- M5 insight ------------------------------------------------------------------------- */

export interface Times { n: number; p50_s: number | null; p90_s: number | null }
export interface ResponseStats {
  incidents: number; acknowledged: number; resolved: number; resolved_unacked: number; open: number;
  time_to_ack: Times; time_to_resolve: Times;
}
export interface NoiseRow {
  signature: string; alertname: string; severity: string; labels: Record<string, string>;
  team_id: string | null; team: string | null; episodes: number; series: number; open: number;
  flaps: number; acked: number; self_resolved: number; median_duration_s: number | null;
  firing_s: number; last_started_at: string; usual_fix: string | null;
  flags: ("flapping" | "nobody_acts" | "short_lived")[];
}
export interface Correlation {
  a: string; a_alertname: string; a_episodes: number;
  b: string; b_alertname: string; b_episodes: number; together: number; ratio: number;
}
export interface InsightReport {
  days: number; since: string; truncated: boolean; episodes: number;
  response: ResponseStats;
  response_by_team: (ResponseStats & { team_id: string | null; team: string | null })[];
  notifications: { sent: number; failed: number; pending: number; suppressed: number };
  noise: NoiseRow[]; noise_total: number; correlations: Correlation[];
  thresholds: { flap_gap_s: number; co_window_s: number; min_episodes_for_flag: number };
}
export interface Related {
  signature: string; days: number; truncated: boolean;
  related: { signature: string; alertname: string; episodes: number; together: number; ratio: number }[];
}

/* --- admin settings ------------------------------------------------------------------- */

export type SettingKey = "prometheus_allowed_hosts" | "webhook_private_hosts" | "smtp_host" | "smtp_port" | "smtp_user"
  | "smtp_password" | "smtp_from" | "smtp_starttls" | "relay_url" | "relay_key"
  | "slack_bot_token" | "slack_app_token" | "incident_levels";
export interface SettingItem {
  key: SettingKey; env: string; source: "settings" | "environment";
  /** null for the SMTP password and the relay key: never returned, see is_set. */
  value: string | number | boolean | string[] | Level[] | null;
  is_set?: boolean; updated_at: string | null; updated_by: string | null;
}
/** Partial: only keys present are written; null = back to the environment value. */
export type SettingsIn = Partial<Record<SettingKey, string | number | boolean | string[] | Level[] | null>>;
export interface SlackStatus {
  configured: boolean; connected: boolean; team: string | null; team_id: string | null;
  bot_user: string | null; error: string | null; since: string | null; command: string;
}
export interface SlackChannel { id: string; name: string; private: boolean; member: boolean }

export const settingsApi = {
  get: () => request<{ settings: SettingItem[] }>("GET", "/api/v1/settings"),
  put: (body: SettingsIn) => request<{
    settings: SettingItem[]; changed: string[];
    /** Present when the relay URL or key changed: every phone was registered again. */
    relay_phones?: { registered: number; refused: number; error: number };
    /** Present when a Slack token changed: this server's connection after reconnecting. */
    slack?: SlackStatus;
  }>("PUT", "/api/v1/settings", body),
  testSmtp: (to?: string) => request<{ ok: boolean; error: string | null; to: string }>("POST", "/api/v1/settings/smtp/test", { to: to || null }),
  /** The relay's view of this install (SHOR-14): seats, card, and the account page. */
  relayStatus: () => request<RelayStatus>("GET", "/api/v1/settings/relay/status"),
};

export interface RelayStatus {
  configured: boolean;
  ok: boolean;
  /** Why the relay could not be asked; null when it answered (or none is configured). */
  error: string | null;
  name?: string;
  signed_in?: number;
  free?: number;
  card_on_file?: boolean;
  can_add_device?: boolean;
  /** The customer account page (https only), where the installation's contact manages it. */
  account_url?: string | null;
}

export const slackApi = {
  status: () => request<SlackStatus>("GET", "/api/v1/slack/status"),
  manifest: () => request<{ manifest: Record<string, unknown> }>("GET", "/api/v1/slack/manifest"),
  channels: () => request<{ channels: SlackChannel[]; error: string | null }>("GET", "/api/v1/slack/channels"),
};

export const insightApi = {
  report: (days: number) => request<InsightReport>("GET", `/api/v1/insight${q({ days })}`),
  related: (signature: string, days = 30) => request<Related>("GET", `/api/v1/insight/related${q({ signature, days })}`),
};
