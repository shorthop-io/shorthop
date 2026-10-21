import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

/** Light / dark / follow-the-system, applied as `data-theme` on <html>.
 *
 *  **Per DEVICE, not per account** — deliberately different from units and language, which
 *  live on the User and follow the person across all three portals. Those are properties of
 *  a person ("I think in knots"). This is a property of a situation: the office laptop at
 *  06:00 in winter wants dark, the tablet on the pontoon in full sun does not, and the same
 *  charter manager uses both. Storing it on the account would force one answer onto both
 *  screens — and would also make the choice follow them into myboat and the marina portal,
 *  where the lighting is a different question again.
 *
 *  Consequences of that choice, both intended: it needs no backend field, and `system` is
 *  the default — so most people get the right thing without ever opening a setting.
 */

export type ThemeChoice = "system" | "light" | "dark";

const LS_KEY = "shorthop.theme";

function stored(): ThemeChoice {
  const v = localStorage.getItem(LS_KEY);
  return v === "light" || v === "dark" || v === "system" ? v : "system";
}

/** What `system` currently resolves to. Read live rather than cached — the OS can flip it
 *  on a schedule (sunset), and a boat app is exactly where that matters. */
function systemPrefersDark(): boolean {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

interface ThemeValue {
  /** What the user picked, including "system". */
  choice: ThemeChoice;
  /** What is actually on screen — never "system". */
  resolved: "light" | "dark";
  setChoice: (c: ThemeChoice) => void;
}

const Ctx = createContext<ThemeValue>({ choice: "system", resolved: "light", setChoice: () => {} });

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [choice, setChoiceState] = useState<ThemeChoice>(stored);
  const [systemDark, setSystemDark] = useState(systemPrefersDark);

  // Follow the OS while the choice is "system". Without this the page keeps the theme it
  // loaded with and does not darken at sunset, which is the one moment it should.
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const onChange = (e: MediaQueryListEvent) => setSystemDark(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const resolved: "light" | "dark" = choice === "system" ? (systemDark ? "dark" : "light") : choice;

  useEffect(() => {
    // Only the dark case sets the attribute; light is the stylesheet's default, so there is
    // no state in which both a light override and the base tokens are fighting.
    const root = document.documentElement;
    if (resolved === "dark") root.setAttribute("data-theme", "dark");
    else root.removeAttribute("data-theme");
    // Keeps the browser UI (address bar, form controls) in step with the page.
    root.style.colorScheme = resolved;
  }, [resolved]);

  const setChoice = useCallback((c: ThemeChoice) => {
    localStorage.setItem(LS_KEY, c);
    setChoiceState(c);
  }, []);

  return <Ctx.Provider value={{ choice, resolved, setChoice }}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeValue {
  return useContext(Ctx);
}

export const THEME_CHOICES: ThemeChoice[] = ["system", "light", "dark"];
