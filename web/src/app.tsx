import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, setCsrf, type Me } from "./api";

/* ---------------------------------------------------------------------------------------
   Routing: plain History API. Few enough pages that a router library buys nothing, and the
   server already answers every non-API path with index.html. */

export function usePath(): [string, (to: string) => void] {
  const [path, setPath] = useState(window.location.pathname + window.location.search);
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname + window.location.search);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const navigate = useCallback((to: string) => {
    if (to === window.location.pathname + window.location.search) return;
    window.history.pushState(null, "", to);
    setPath(to);
    window.scrollTo(0, 0);
  }, []);
  return [path, navigate];
}

const NavCtx = createContext<(to: string) => void>(() => {});
export const NavProvider = NavCtx.Provider;
export const useNavigate = () => useContext(NavCtx);

/** An in-app link: a real <a href> (so open-in-new-tab and middle-click work), with plain
 *  left-clicks handled by the History API instead of a full page load. */
export function Link({ to, children, className, title }: {
  to: string; children: ReactNode; className?: string; title?: string;
}) {
  const navigate = useNavigate();
  return (
    <a
      href={to}
      className={className}
      title={title}
      onClick={(e) => {
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}

/* ---------------------------------------------------------------------------------------
   The signed-in user. Loaded once; its CSRF token is what every write sends back. */

interface MeValue { me: Me; can: (scope: string) => boolean; reload: () => void }
const MeCtx = createContext<MeValue | null>(null);

export function MeProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api.me().then((m) => { setCsrf(m.csrf_token); setMe(m); setError(null); })
      .catch((e) => setError(e.message));
  }, []);
  useEffect(load, [load]);
  if (error) return <div className="boot-msg"><p className="error">{error}</p></div>;
  if (!me) return <div className="boot-msg"><p className="muted">Loading…</p></div>;
  const can = (scope: string) => me.scopes.includes("admin") || me.scopes.includes(scope);
  return <MeCtx.Provider value={{ me, can, reload: load }}>{children}</MeCtx.Provider>;
}

export function useMe(): MeValue {
  const v = useContext(MeCtx);
  if (!v) throw new Error("useMe outside MeProvider");
  return v;
}

/* ---------------------------------------------------------------------------------------
   Small shared hooks. */

/** Load something, and re-load it every `everyMs` while the page is visible. A refresh that
 *  fails keeps the last good data on screen and says so, rather than blanking the page. */
export function usePoll<T>(load: () => Promise<T>, deps: unknown[], everyMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    let live = true;
    load().then((d) => { if (live) { setData(d); setError(null); } })
      .catch((e) => { if (live) setError(e.message); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  useEffect(() => {
    if (!everyMs) return;
    const id = window.setInterval(() => { if (!document.hidden) refresh(); }, everyMs);
    return () => window.clearInterval(id);
  }, [everyMs, refresh]);
  return { data, error, refresh, setData };
}

/** A clock that advances, so relative ages ("3 min ago") stay true without a refetch. */
export function useNow(everyMs = 15000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(id);
  }, [everyMs]);
  return now;
}

/** Only http(s) URLs become links. Annotation values come from whoever wrote the alert
 *  rule, and a `javascript:` URL in an href is script execution on click. */
export function safeHref(url: string | undefined | null): string | null {
  if (!url) return null;
  try {
    const u = new URL(url, window.location.origin);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
