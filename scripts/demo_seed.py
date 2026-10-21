"""Fill a LOCAL Shorthop with realistic demo data, for development and the UI render check.

    DATABASE_URL=sqlite:///demo.db python scripts/demo_seed.py

Refuses to run against Postgres (POSTGRES_HOST set): it creates a known password.
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if os.environ.get("POSTGRES_HOST"):
    sys.exit("demo_seed.py is for a local SQLite database only")

os.environ.setdefault("SCHEDULER_ENABLED", "false")

from app import create_app  # noqa: E402
from app import alerts as ops  # noqa: E402
from app.auth import INTEGRATION_TOKEN_PREFIX, hash_password, new_token  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Channel, EscalationPolicy, Incident, Inhibition, Integration, LabelRule, Notification, Occurrence, Route,
    Schedule,
    ScheduleOverride, Team, TeamMember, User, now,
)

DEMO_PASSWORD = "demo-password-123"

create_app(start_background=False)
db = SessionLocal

if db.query(User).filter(User.email == "admin@example.com").first():
    sys.exit("already seeded")

admin = User(email="admin@example.com", name="Ada Admin", role="admin",
             password_hash=hash_password(DEMO_PASSWORD))
bob = User(email="bob@example.com", name="Bob Responder", role="responder")
vic = User(email="vic@example.com", name="", role="viewer")
db.add_all([admin, bob, vic])
team = Team(slug="platform", name="Platform", description="")
team.members = [TeamMember(user=admin, role="owner"), TeamMember(user=bob)]
db.add(team)
dba = Team(slug="dba", name="Databases", description="")
dba.members = [TeamMember(user=admin, role="owner")]
db.add(dba)


def integ(name, kind, **kw):
    _, digest, prefix = new_token(INTEGRATION_TOKEN_PREFIX)
    row = Integration(name=name, kind=kind, token_hash=digest, prefix=prefix,
                      extra_labels=kw.pop("extra_labels", {}), **kw)
    db.add(row)
    return row


ops_mail = Channel(name="ops mailbox", kind="email", config={"to": ["ops@example.com"]})
slack_ch = Channel(name="ops-alerts (Slack)", kind="slack",
                   config={"channel_id": "C0DEMO0001", "channel_name": "ops-alerts"})
db.add(slack_ch)
hook_ch = Channel(name="team chat", kind="webhook", config={"url": "https://hooks.example.com/shorthop"},
                  secret="shs_demo")
db.add_all([ops_mail, hook_ch])
db.flush()
policy = EscalationPolicy(name="platform on-call", team_id=team.id, repeat=1, steps=[
    {"delay_minutes": 0, "targets": [{"type": "user", "id": bob.id}, {"type": "channel", "id": hook_ch.id}]},
    {"delay_minutes": 10, "targets": [{"type": "team", "id": team.id}, {"type": "channel", "id": ops_mail.id}]},
])
db.add(policy)
db.add(EscalationPolicy(name="databases on-call", team_id=dba.id, repeat=0, steps=[
    {"delay_minutes": 0, "targets": [{"type": "team", "id": dba.id}]}]))
db.flush()
db.add_all([
    Route(position=10, name="database", matchers=[{"name": "namespace", "value": "database", "isRegex": False, "isEqual": True}],
          team_id=team.id, escalation_policy_id=policy.id, group_by=["alertname", "instance"]),
    Route(position=20, name="platform", matchers=[{"name": "severity", "value": "critical|warning", "isRegex": True, "isEqual": True}],
          team_id=team.id, escalation_policy_id=policy.id, group_by=["alertname", "namespace"]),
    Route(position=30, name="everything else", matchers=[], group_by=["alertname"]),
])
# Label rules: the general rule first, the exception below it (a later rule wins).
WARN = {"name": "severity", "value": "warning", "isRegex": False, "isEqual": True}
db.add_all([
    LabelRule(position=10, name="warnings are P2", matchers=[WARN], set_labels={"level": "P2"}),
    LabelRule(position=20, name="silent socket is P1", matchers=[WARN], text_pattern="silent socket",
              set_labels={"level": "P1"}),
])
db.add(Inhibition(name="node down mutes its pods",
                  source_matchers=[{"name": "alertname", "value": "NodeDown", "isRegex": False, "isEqual": True}],
                  target_matchers=[{"name": "severity", "value": "warning", "isRegex": False, "isEqual": True}],
                  equal=["cluster", "node"]))
db.commit()

prom = integ("prometheus-big", "prometheus")
hook = integ("ci-webhook", "webhook")
integ("old-alertmanager", "alertmanager", enabled=False)
db.commit()
# The screenshot stack runs a demo relay (shorthop-relay scripts/demo_seed.py) and passes its
# fixed check id here, so Integrations shows prometheus-big "watched from outside". The ping
# URL is never called: the stack runs with SCHEDULER_ENABLED=false.
if os.getenv("DEMO_WATCH_CHECK_ID"):
    prom.watch_check_id = os.environ["DEMO_WATCH_CHECK_ID"]
    prom.watch_ping_url = "https://ping.shorthop.io/p/demo-not-a-real-token"
    prom.watch_forwarded_at = now() - dt.timedelta(seconds=40)
    db.commit()

t = now()
A = ops.IncomingAlert


def fire(i, name, sev, labels, summary, started_min, desc="", runbook=""):
    ann = {"summary": summary}
    if desc:
        ann["description"] = desc
    if runbook:
        ann["runbook_url"] = runbook
    ops.ingest(i, [A(labels={"alertname": name, "severity": sev, "cluster": "big", **labels},
                     annotations=ann, starts_at=t - dt.timedelta(minutes=started_min),
                     ends_at=t + dt.timedelta(minutes=4),
                     generator_url="https://prometheus.example.com/graph")])


# History first: the same fault twice before, once with a recorded fix.
for days, pod, fix in ((9, "api-6c9f", ("restarted", "Restarted the consumer; the queue drained in 2 min.")),
                       (3, "api-1b2e", None)):
    fire(prom, "KubePodCrashLooping", "warning", {"namespace": "service", "pod": pod},
         "Pod is crash looping", 0)
    occ = db.query(Occurrence).filter(Occurrence.resolved_at.is_(None)).first()
    occ.started_at = t - dt.timedelta(days=days)
    occ.status, occ.resolved_at = "resolved", t - dt.timedelta(days=days) + dt.timedelta(minutes=37)
    occ.resolved_how = "manual" if fix else "timeout"
    occ.resolved_by = "bob@example.com" if fix else "shorthop"
    if fix:
        occ.resolution_category, occ.resolution_note = fix
    db.commit()

fire(prom, "KubePodCrashLooping", "warning", {"namespace": "service", "pod": "api-7f3a"},
     "Pod service/api-7f3a is crash looping", 12,
     "Container api has restarted 6 times in the last 10 minutes.",
     "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubepodcrashlooping")
fire(prom, "KubeJobFailed", "warning", {"namespace": "service", "job_name": "charter-weather-scan-29860333"},
     "Job service/charter-weather-scan-29860333 failed to complete.", 95)
fire(prom, "AisFeedStale", "warning", {"namespace": "service", "org": "msida"},
     "AIS feed: silent socket for 15 min", 6)
fire(prom, "PostgresReplicationLag", "critical", {"namespace": "database", "instance": "postgres-standby:5432"},
     "The standby is 412 s behind the primary", 6)
fire(prom, "DiskSpaceLow", "info", {"instance": "big:9100", "mountpoint": "/opt"},
     "Filesystem /opt has 18% free", 300)
fire(hook, "NightlyBackupFailed", "critical", {"service": "billing"},
     "Nightly backup of billing failed: upload timed out", 40)

from app import incidents  # noqa: E402
incidents.run_due()
db.expire_all()
for n in db.query(Notification).all():
    n.status, n.attempts, n.sent_at = "sent", 1, now()
    if n.kind == "webhook":
        n.status, n.attempts, n.error = "failed", 5, "webhook answered HTTP 404"
db.commit()

ack = db.query(Occurrence).filter(Occurrence.alertname == "KubeJobFailed").first()
ops.acknowledge(ack.id, "bob@example.com")
ops.add_note(ack.id, "bob@example.com", "Weather API returned 503 — retrying at the next run.")
ops.create_silence([{"name": "alertname", "value": "DiskSpaceLow"}], None,
                   t + dt.timedelta(hours=6), "Volume resize scheduled tonight", "admin@example.com")

prom.last_watchdog_at = t - dt.timedelta(seconds=40)
prom.prometheus_url = "http://monitoring-prometheus.monitoring.svc.cluster.local:9090"
prom.shadow_status = {
    "checked_at": (t - dt.timedelta(seconds=25)).isoformat(), "error": None,
    "prometheus_firing": 5, "shorthop_open": 4, "unconfirmed": 0,
    "missing": [{"alertname": "TargetDown", "labels": {"job": "kube-state-metrics", "namespace": "monitoring", "severity": "warning"}}],
    "extra": [], "missing_keys": [], "extra_keys": []}
db.commit()
from app import selfmon  # noqa: E402
selfmon._raise("ShorthopShadowMismatch", prom, True, "warning",
               "prometheus-big: 1 firing in Prometheus not in Shorthop, 0 the other way",
               "Missing: TargetDown")
monday = (t - dt.timedelta(days=t.weekday() + 7)).strftime("%Y-%m-%dT09:00")
sched = Schedule(name="platform primary", team_id=team.id, timezone="Europe/Stockholm", layers=[
    {"name": "Weekly", "users": [admin.id, bob.id, vic.id], "rotation_hours": 168, "start": monday, "restriction": None},
    {"name": "Office hours", "users": [bob.id], "rotation_hours": 168, "start": monday,
     "restriction": {"days": [0, 1, 2, 3, 4], "start": "08:00", "end": "17:00"}},
])
db.add(sched)
db.flush()
db.add(ScheduleOverride(schedule_id=sched.id, user_id=vic.id, starts_at=t + dt.timedelta(days=2),
                        ends_at=t + dt.timedelta(days=3), reason="Ada at a conference", created_by="admin@example.com"))
db.commit()

# --- three weeks of history, so Insight has something true to say ----------------------------
# A node fault that drags pod crashes with it (correlation), a CPU alert that flaps and ends on
# its own (noise), and incidents answered at varied speeds (MTTA/MTTR). Inserted directly as
# resolved rows: they are the past, and must not page anyone today.
crash = db.query(Occurrence).filter(Occurrence.alertname == "KubePodCrashLooping",
                                    Occurrence.resolved_at.is_(None)).first()


def past(name, sig, fp, start, minutes, how="source", sev="warning", acked=None, cat=None, labels=None):
    db.add(Occurrence(integration_id=prom.id, team_id=team.id, fingerprint=fp, signature=sig,
                      alertname=name, severity=sev, labels={"alertname": name, **(labels or {})},
                      signature_labels={"alertname": name, **(labels or {})}, annotations={},
                      started_at=start, last_seen_at=start, expires_at=start,
                      status="resolved", resolved_at=start + dt.timedelta(minutes=minutes),
                      resolved_how=how, acked_at=acked, resolution_category=cat))


for k in range(5):
    s = t - dt.timedelta(days=18 - k * 4, hours=k)
    past("NodeNotReady", "demo-node", "demo-node-big", s, 25, how="manual", sev="critical",
         acked=s + dt.timedelta(minutes=3 + k * 2), cat="restarted", labels={"node": "big"})
    past("KubePodCrashLooping", crash.signature, f"demo-crash-{k}", s + dt.timedelta(minutes=2), 20,
         labels={"namespace": "service"})
for k in range(9):  # flaps: one series, back within minutes, nobody acts
    s = t - dt.timedelta(days=6, minutes=-k * 9)
    past("CPUThrottlingHigh", "demo-cpu", "demo-cpu-1", s, 3, labels={"namespace": "monitoring"})
for k, (ack_min, res_min) in enumerate(((2, 25), (4, 40), (6, 33), (11, 90), (3, 18), (None, 7), (27, 140), (5, 22))):
    o = t - dt.timedelta(days=15 - k * 2)
    db.add(Incident(number=900 + k, team_id=team.id, group_key=f"demo-hist-{k}", title="history",
                    status="resolved", opened_at=o,
                    acked_at=o + dt.timedelta(minutes=ack_min) if ack_min else None,
                    acked_by="bob@example.com" if ack_min else None,
                    resolved_at=o + dt.timedelta(minutes=res_min)))
# Phones in every push-relay state, so My account → Phones renders each one.
from app.models import Device  # noqa: E402
for i, (name, platform, tok, status, msg) in enumerate((
        ("Ada's iPhone", "ios", "ExponentPushToken[demoipho000000000000]", "registered", None),
        ("Work Pixel", "android", "ExponentPushToken[demopixe000000000000]", "refused",
         "3 phones are already signed in, and the first 3 are free. Add a payment method at "
         "https://shorthop.io to sign in more (€1.99 per phone per month), or sign out a phone "
         "you no longer use."),
        ("Old iPad", "ios", "ExponentPushToken[demoipad000000000000]", "error",
         "could not reach the push relay (ConnectTimeout)"),
        ("Spare phone", "android", None, None, None),
        ("Drawer Android", "android", "ExponentPushToken[demodraw000000000000]", "registered", None),
        ("Retired iPhone", "ios", "ExponentPushToken[demoreti000000000000]", "signed_out",
         "signed out of the push relay after going unused"))):
    db.add(Device(user_id=admin.id, name=name, platform=platform, token_hash=f"{i:064d}",
                  prefix=f"shd_dem{i}", push_token=tok, relay_status=status, relay_message=msg,
                  relay_device_id="00000000-0000-0000-0000-00000000000" + str(i) if status == "registered" else None,
                  relay_signs_out_at=t + dt.timedelta(days=9) if name == "Drawer Android" else None,
                  created_at=t - dt.timedelta(days=3 + i), last_seen_at=t - dt.timedelta(hours=i)))
db.commit()
print("seeded — sign in as admin@example.com /", DEMO_PASSWORD)
