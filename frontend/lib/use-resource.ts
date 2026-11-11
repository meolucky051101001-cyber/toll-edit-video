"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

type Snapshot<T> = { path: string | null; data: T | null; error: string };
export function useResource<T>(
  path: string | null,
  interval = 0,
  stopWhen?: (data: T) => boolean,
) {
  const [snapshot, setSnapshot] = useState<Snapshot<T>>({
    path: null,
    data: null,
    error: "",
  });
  const [revision, setRevision] = useState(0);
  // Keep the latest predicate in a ref so callers can pass an inline function
  // without it restarting the poll loop on every render.
  const stopWhenRef = useRef(stopWhen);
  useEffect(() => {
    stopWhenRef.current = stopWhen;
  }, [stopWhen]);
  useEffect(() => {
    if (!path) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    async function run() {
      // Skip network work while the tab is hidden; keep the timer alive so
      // polling resumes automatically when the tab becomes visible again.
      if (typeof document !== "undefined" && document.hidden) {
        if (active && interval) timer = setTimeout(run, interval);
        return;
      }
      let stop = false;
      try {
        const result = await api<T>(path!, {
          signal: AbortSignal.any([
            controller.signal,
            AbortSignal.timeout(10000),
          ]),
        });
        if (active) setSnapshot({ path, data: result, error: "" });
        stop = stopWhenRef.current?.(result) ?? false;
      } catch (error) {
        if (active)
          setSnapshot((previous) => ({
            path,
            data: previous.path === path ? previous.data : null,
            error: (error as Error).message,
          }));
      } finally {
        // Once the predicate is satisfied (e.g. a job reached a terminal
        // state) stop rescheduling instead of hammering a finished resource.
        if (active && interval && !stop) timer = setTimeout(run, interval);
      }
    }
    void run();
    return () => {
      active = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, [path, interval, revision]);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  const current = snapshot.path === path;
  return {
    data: current ? snapshot.data : null,
    error: current ? snapshot.error : "",
    loading: Boolean(path && !current),
    refresh,
  };
}

/** Returns `value` delayed by `delayMs`; resets the timer on every change. */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}
