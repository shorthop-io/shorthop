import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { paging } from "./api";
import { Link, MeProvider, NavProvider, useMe, usePath, usePoll } from "./app";
import { Icon, type IconName } from "./components/Icon";
import { AlertDetail } from "./pages/AlertDetail";
import { Alerts } from "./pages/Alerts";
import { Audit } from "./pages/Audit";
import { Channels } from "./pages/Channels";
import { Deliveries } from "./pages/Deliveries";
import { Insight } from "./pages/Insight";
import { Escalation } from "./pages/Escalation";
import { IncidentDetail, Incidents } from "./pages/Incidents";
import { Inhibitions } from "./pages/Inhibitions";
import { LabelRules } from "./pages/LabelRules";
import { Routing } from "./pages/Routing";
import { ScheduleDetail, ScheduleEdit, Schedules } from "./pages/Schedules";
import { Integrations } from "./pages/Integrations";
import { People } from "./pages/People";
import { Settings } from "./pages/Settings";
import { SilenceNew, Silences } from "./pages/Silences";
import { Account } from "./pages/Account";
import { ThemeProvider, useTheme, type ThemeChoice } from "./theme";
import "./styles.css";

interface NavItem { to: string; label: string; icon: IconName; scope?: string; match: RegExp }

const NAV: { group: string; items: NavItem[] }[] = [
  { group: "Respond", items: [
    { to: "/incidents", label: "Incidents", icon: "alert", match: /^\/(incidents)?(\/|$|\?)/ },
    { to: "/alerts", label: "Alerts", icon: "bell", match: /^\/alerts/ },
    { to: "/silences", label: "Silences", icon: "bellOff", match: /^\/silences/ },
    { to: "/deliveries", label: "Delivery log", icon: "mail", match: /^\/deliveries/ },
    { to: "/insight", label: "Insight", icon: "chart", match: /^\/insight/ },
  ] },
  { group: "On call", items: [
    { to: "/schedules", label: "Schedules", icon: "calendar", scope: "oncall:read", match: /^\/schedules/ },
  ] },
  { group: "Configure", items: [
    { to: "/label-rules", label: "Label rules", icon: "tag", scope: "oncall:read", match: /^\/label-rules/ },
    { to: "/routing", label: "Routing", icon: "route", scope: "oncall:read", match: /^\/routing/ },
    { to: "/escalation", label: "Escalation", icon: "clock", scope: "oncall:read", match: /^\/escalation/ },
    { to: "/channels", label: "Channels", icon: "megaphone", scope: "oncall:read", match: /^\/channels/ },
    { to: "/inhibitions", label: "Inhibitions", icon: "ban", scope: "oncall:read", match: /^\/inhibitions/ },
    { to: "/integrations", label: "Integrations", icon: "plug", match: /^\/integrations/ },
    { to: "/people", label: "People", icon: "users", scope: "oncall:read", match: /^\/people/ },
    { to: "/settings", label: "Settings", icon: "gear", scope: "admin", match: /^\/settings/ },
  ] },
  { group: "Account", items: [
    { to: "/account", label: "My account", icon: "user", match: /^\/(account|tokens)/ },
    { to: "/audit", label: "Audit log", icon: "scroll", scope: "admin", match: /^\/audit/ },
  ] },
];

const THEME_NEXT: Record<ThemeChoice, ThemeChoice> = { system: "light", light: "dark", dark: "system" };
const THEME_ICON: Record<ThemeChoice, IconName> = { system: "display", light: "sun", dark: "moon" };

function Page({ path }: { path: string }) {
  const url = new URL(path, window.location.origin);
  const p = url.pathname;
  const search = url.searchParams;
  const m = /^\/alerts\/([^/]+)$/.exec(p);
  if (m) return <AlertDetail id={decodeURIComponent(m[1])} />;
  const mi = /^\/incidents\/([^/]+)$/.exec(p);
  if (mi) return <IncidentDetail id={decodeURIComponent(mi[1])} />;
  if (p === "/alerts") return <Alerts search={search} />;
  if (p === "/schedules") return <Schedules />;
  if (p === "/schedules/new") return <ScheduleEdit id={null} />;
  const ms = /^\/schedules\/([^/]+)(\/edit)?$/.exec(p);
  if (ms) return ms[2] ? <ScheduleEdit id={decodeURIComponent(ms[1])} /> : <ScheduleDetail id={decodeURIComponent(ms[1])} />;
  if (p === "/routing") return <Routing />;
  if (p === "/escalation") return <Escalation />;
  if (p === "/channels") return <Channels />;
  if (p === "/inhibitions") return <Inhibitions />;
  if (p === "/deliveries") return <Deliveries />;
  if (p === "/insight") return <Insight />;
  if (p === "/silences/new") return <SilenceNew search={search} />;
  if (p === "/silences") return <Silences search={search} />;
  if (p === "/integrations") return <Integrations />;
  if (p === "/people") return <People />;
  if (p === "/account") return <Account search={search} />;
  // Old link (and the QR flow's "Phones & tokens"): the same content now lives in My account.
  if (p === "/tokens") return <Account search={new URLSearchParams("tab=phones")} />;
  if (p === "/audit") return <Audit />;
  if (p === "/settings") return <Settings search={search} />;
  if (p === "/label-rules") return <LabelRules />;
  return <Incidents search={search} />;
}

function Shell() {
  const [path, navigate] = usePath();
  const { me, can } = useMe();
  const { choice, setChoice } = useTheme();
  const [menuOpen, setMenuOpen] = useState(false);
  // The count on the nav row: incidents still paging (triggered, not yet acknowledged) for
  // the caller's own teams, matching the Incidents page's default.
  const scope = me.teams.length > 0 ? "mine" : "all";
  const { data: open } = usePoll(() => paging.listIncidents("triggered", scope), [scope], 30000);
  const firing = open?.length ?? 0;

  return (
    <NavProvider value={(to) => { setMenuOpen(false); navigate(to); }}>
      <div className="shell">
        <aside className={`sidebar${menuOpen ? " open" : ""}`}>
          <div className="brand">
            <img src="/favicon.svg" alt="" className="brand-logo" />
            <h1>Shorthop</h1>
            <button className="menu-btn" aria-label="Menu" aria-expanded={menuOpen} onClick={() => setMenuOpen(!menuOpen)}>
              <Icon name={menuOpen ? "x" : "menu"} size={20} />
            </button>
          </div>
          <nav className="side-nav">
            {NAV.map((g) => {
              const items = g.items.filter((i) => !i.scope || can(i.scope));
              if (!items.length) return null;
              return (
                <div key={g.group}>
                  <div className="side-group">{g.group}</div>
                  {items.map((i) => (
                    <Link key={i.to} to={i.to} className={`side-item${i.match.test(new URL(path, window.location.origin).pathname) ? " active" : ""}`}>
                      <Icon name={i.icon} size={17} /> <span>{i.label}</span>
                      {i.to === "/incidents" && firing > 0 && <span className="side-count danger">{firing}</span>}
                    </Link>
                  ))}
                </div>
              );
            })}
          </nav>
          <div className="sidebar-foot">
            <span className="muted" title={me.email}>{me.name || me.email}</span>
            <div className="foot-row">
              <button className="btn-ghost btn-sm theme-btn" onClick={() => setChoice(THEME_NEXT[choice])}
                title={`Theme: ${choice} — click to change`} aria-label={`Theme: ${choice}`}>
                <Icon name={THEME_ICON[choice]} size={14} /> {choice}
              </button>
              <form method="post" action="/logout">
                <button className="btn-ghost btn-sm"><Icon name="signout" size={14} /> Sign out</button>
              </form>
            </div>
          </div>
        </aside>
        <main className="main">
          <Page path={path} />
        </main>
      </div>
    </NavProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <MeProvider>
        <Shell />
      </MeProvider>
    </ThemeProvider>
  </StrictMode>,
);
