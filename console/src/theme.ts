import { useEffect, useState } from "react";

/** Light, dark, or follow the computer ("system"). Remembered per browser. */
export type Theme = "system" | "light" | "dark";
const KEY = "laminar.theme";

function read(): Theme {
  try {
    const t = localStorage.getItem(KEY);
    return t === "light" || t === "dark" ? t : "system";
  } catch {
    return "system";
  }
}

function apply(t: Theme) {
  const root = document.documentElement;
  if (t === "system") delete root.dataset.theme;
  else root.dataset.theme = t;
}

apply(read());   // before the first paint, so the page never flashes the wrong theme

export function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(read);
  useEffect(() => {
    apply(theme);
    try {
      localStorage.setItem(KEY, theme);
    } catch {
      /* private mode: theme still applies for this visit */
    }
  }, [theme]);
  return [theme, setTheme];
}
