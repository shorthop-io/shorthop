import { useState, type ReactNode } from "react";
import type { Alert } from "../api";
import { copyText } from "../app";
import { Icon, type IconName } from "./Icon";

/** Severity → badge tone. Unknown severities are neutral rather than guessed at. */
export function severityTone(sev: string): "danger" | "warn" | "info" | "neutral" {
  const s = sev.toLowerCase();
  if (["critical", "page", "error", "high", "p1", "emergency"].includes(s)) return "danger";
  if (["warning", "warn", "medium", "p2"].includes(s)) return "warn";
  if (["info", "low", "none", "p3", "p4"].includes(s)) return "info";
  return "neutral";
}

export function SeverityBadge({ severity }: { severity: string }) {
  return <span className={`badge ${severityTone(severity)}`}>{severity || "no severity"}</span>;
}

export function StatusBadge({ alert }: { alert: Pick<Alert, "status" | "silenced_by"> }) {
  if (alert.status === "resolved") return <span className="badge ok">resolved</span>;
  const silenced = alert.silenced_by?.length > 0;
  return (
    <>
      {alert.status === "acknowledged"
        ? <span className="badge info">acknowledged</span>
        : <span className="badge danger">firing</span>}
      {silenced && <span className="badge neutral">silenced</span>}
    </>
  );
}

export function Labels({ labels, highlight, skip = [], ruleSet }: {
  labels: Record<string, string>; highlight?: Record<string, string>; skip?: string[];
  /** label -> name of the label rule that set it; those chips are marked. */
  ruleSet?: Record<string, string> | null;
}) {
  const entries = Object.entries(labels).filter(([k]) => !skip.includes(k))
    .sort(([a], [b]) => a.localeCompare(b));
  if (!entries.length) return null;
  return (
    <div className="label-chips">
      {entries.map(([k, v]) => (
        <span key={k} className={`label-chip${highlight && k in highlight ? " sig" : ""}${ruleSet && k in ruleSet ? " ruled" : ""}`}
          title={[highlight && k in highlight ? "part of the signature" : "", ruleSet && k in ruleSet ? `set by label rule "${ruleSet[k]}"` : ""].filter(Boolean).join(" · ") || undefined}>
          {ruleSet && k in ruleSet && <span className="rule-mark" aria-label="set by a label rule">✎</span>}
          <span className="lk">{k}</span>=<span className="lv">{v === "" ? <em className="muted">(none)</em> : v}</span>
        </span>
      ))}
    </div>
  );
}

export function ErrorNotice({ children }: { children: ReactNode }) {
  if (!children) return null;
  return <p className="notice notice--danger notice--icon" role="alert">
    <Icon name="alert" size={15} /><span>{children}</span></p>;
}

export function Empty({ icon, title, children }: { icon: IconName; title: string; children?: ReactNode }) {
  return (
    <div className="empty card">
      <Icon name={icon} size={26} />
      <p className="empty-title">{title}</p>
      {children && <div className="muted small">{children}</div>}
    </div>
  );
}

/** A secret shown exactly once, with a copy button and a warning that says so. */
export function ShowOnce({ label, value, children }: { label: string; value: string; children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="show-once card">
      <p className="notice notice--warn notice--icon"><Icon name="key" size={15} />
        <span><strong>{label}</strong> — shown only now. Copy it before leaving this page.</span></p>
      <div className="secret-row">
        <code className="secret">{value}</code>
        <button className="btn-ghost btn-sm" onClick={async () => setCopied(await copyText(value))}>
          <Icon name={copied ? "check" : "clipboard"} size={14} /> {copied ? "Copied" : "Copy"}
        </button>
      </div>
      {children}
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label>
      {label}
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}
