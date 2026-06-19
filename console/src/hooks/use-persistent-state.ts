"use client";

import { useCallback, useMemo, useState, useSyncExternalStore } from "react";

/**
 * Persisted UI state backed by localStorage, hydration-safe via `useSyncExternalStore`
 * (server + first client paint use `initial`, then it reconciles to the stored value with no
 * mismatch and no set-state-in-effect). Used for the data-table "saved views" — column
 * visibility, sorting, etc. Writes notify same-tab subscribers and survive across tabs.
 */
export function usePersistentState<T>(key: string, initial: T): [T, (value: T) => void] {
  // Capture the first `initial` as a stable fallback (callers may pass a fresh literal each render).
  const [fallback] = useState(initial);

  const subscribe = useCallback(
    (onChange: () => void) => {
      const onStorage = (e: StorageEvent) => {
        if (e.key === key || e.key === null) onChange();
      };
      const onLocal = () => onChange();
      window.addEventListener("storage", onStorage);
      window.addEventListener(`pstate:${key}`, onLocal);
      return () => {
        window.removeEventListener("storage", onStorage);
        window.removeEventListener(`pstate:${key}`, onLocal);
      };
    },
    [key],
  );

  const raw = useSyncExternalStore(
    subscribe,
    () => {
      try {
        return window.localStorage.getItem(key);
      } catch {
        return null;
      }
    },
    () => null,
  );

  const value = useMemo<T>(() => {
    if (raw == null) return fallback;
    try {
      return JSON.parse(raw) as T;
    } catch {
      return fallback;
    }
  }, [raw, fallback]);

  const setValue = useCallback(
    (next: T) => {
      try {
        window.localStorage.setItem(key, JSON.stringify(next));
      } catch {
        // ignore — storage disabled
      }
      window.dispatchEvent(new Event(`pstate:${key}`));
    },
    [key],
  );

  return [value, setValue];
}
