"use client";
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

type Snapshot<T> = { path: string | null; data: T | null; error: string };
export function useResource<T>(path: string | null, interval = 0) {
  const [snapshot, setSnapshot] = useState<Snapshot<T>>({
    path: null,
    data: null,
    error: "",
  });
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!path) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    async function run() {
      try {
        const result = await api<T>(path!, {
          signal: AbortSignal.any([
            controller.signal,
            AbortSignal.timeout(10000),
          ]),
        });
        if (active) setSnapshot({ path, data: result, error: "" });
      } catch (error) {
        if (active)
          setSnapshot((previous) => ({
            path,
            data: previous.path === path ? previous.data : null,
            error: (error as Error).message,
          }));
      } finally {
        if (active && interval) timer = setTimeout(run, interval);
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
