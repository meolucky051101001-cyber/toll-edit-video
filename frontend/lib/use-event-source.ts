"use client";

import { useEffect, useRef, useState } from "react";

type EventCallback = (data: unknown) => void;

class SharedEventSourceManager {
  private static instance: SharedEventSourceManager | null = null;
  private es: EventSource | null = null;
  private listeners: Map<string, Set<EventCallback>> = new Map();
  private subscriberCount: number = 0;
  private disconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectAttempts: number = 0;
  private _status: "connecting" | "connected" | "disconnected" = "disconnected";
  private statusListeners: Set<(status: "connecting" | "connected" | "disconnected") => void> = new Set();

  private constructor() {}

  public static getInstance(): SharedEventSourceManager {
    if (!SharedEventSourceManager.instance) {
      SharedEventSourceManager.instance = new SharedEventSourceManager();
    }
    return SharedEventSourceManager.instance;
  }

  public get status(): "connecting" | "connected" | "disconnected" {
    return this._status;
  }

  private setStatus(newStatus: "connecting" | "connected" | "disconnected") {
    if (this._status !== newStatus) {
      this._status = newStatus;
      this.statusListeners.forEach((fn) => fn(newStatus));
    }
  }

  public subscribeStatus(fn: (status: "connecting" | "connected" | "disconnected") => void): () => void {
    this.statusListeners.add(fn);
    fn(this._status);
    return () => {
      this.statusListeners.delete(fn);
    };
  }

  private connect() {
    if (this.es || typeof window === "undefined") return;

    this.setStatus("connecting");
    try {
      this.es = new EventSource("/api/events");

      this.es.addEventListener("connected", () => {
        this.setStatus("connected");
        this.reconnectAttempts = 0;
      });

      this.es.addEventListener("search:update", (e: MessageEvent) => {
        try {
          const parsed = JSON.parse(e.data);
          this.emit("search:update", parsed);
        } catch {
          // Ignore parse errors
        }
      });

      this.es.addEventListener("download:update", (e: MessageEvent) => {
        try {
          const parsed = JSON.parse(e.data);
          this.emit("download:update", parsed);
        } catch {
          // Ignore parse errors
        }
      });

      this.es.onerror = () => {
        this.setStatus("disconnected");
        if (this.es) {
          this.es.close();
          this.es = null;
        }
        // Auto-reconnect with backoff if we still have active subscribers
        if (this.subscriberCount > 0) {
          const delay = Math.min(1000 * 2 ** this.reconnectAttempts, 15000);
          this.reconnectAttempts++;
          setTimeout(() => {
            if (this.subscriberCount > 0 && !this.es) {
              this.connect();
            }
          }, delay);
        }
      };
    } catch {
      this.setStatus("disconnected");
    }
  }

  private disconnect() {
    if (this.es) {
      this.es.close();
      this.es = null;
    }
    this.setStatus("disconnected");
  }

  public addSubscriber() {
    this.subscriberCount++;
    if (this.disconnectTimer) {
      clearTimeout(this.disconnectTimer);
      this.disconnectTimer = null;
    }
    if (!this.es) {
      this.connect();
    }
  }

  public removeSubscriber() {
    this.subscriberCount = Math.max(0, this.subscriberCount - 1);
    if (this.subscriberCount === 0) {
      // Grace period before closing connection to tolerate quick page route changes
      this.disconnectTimer = setTimeout(() => {
        if (this.subscriberCount === 0) {
          this.disconnect();
        }
      }, 5000);
    }
  }

  public on(event: string, callback: EventCallback): () => void {
    if (!this.listeners.has(event)) {
      this.listeners.set(event, new Set());
    }
    this.listeners.get(event)!.add(callback);
    return () => {
      this.listeners.get(event)?.delete(callback);
    };
  }

  private emit(event: string, data: unknown) {
    const handlers = this.listeners.get(event);
    if (handlers) {
      handlers.forEach((fn) => {
        try {
          fn(data);
        } catch (err) {
          console.error(`Error in event listener for ${event}:`, err);
        }
      });
    }
  }
}

/**
 * React hook to listen for real-time Server-Sent Events (SSE).
 * Uses a shared single connection to /api/events.
 */
export function useEventSource() {
  const [status, setStatus] = useState<"connecting" | "connected" | "disconnected">("disconnected");

  useEffect(() => {
    const manager = SharedEventSourceManager.getInstance();
    manager.addSubscriber();
    const unsubStatus = manager.subscribeStatus(setStatus);

    return () => {
      unsubStatus();
      manager.removeSubscriber();
    };
  }, []);

  return { status };
}

/**
 * React hook to subscribe to a specific SSE event.
 */
export function useSSEListener<T = unknown>(eventName: string, callback: (data: T) => void) {
  const callbackRef = useRef(callback);

  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);

  useEffect(() => {
    const manager = SharedEventSourceManager.getInstance();
    manager.addSubscriber();

    const unsubscribe = manager.on(eventName, (data: unknown) => {
      callbackRef.current(data as T);
    });

    return () => {
      unsubscribe();
      manager.removeSubscriber();
    };
  }, [eventName]);
}
