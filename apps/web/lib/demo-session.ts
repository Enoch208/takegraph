import type { DemoSession } from "@/lib/api";

const TOKEN_KEY = "takegraph.demo.token";
const SESSION_KEY = "takegraph.demo.session";
const SSE_CURSOR_PREFIX = "takegraph.demo.sse.";

export function readDemoSession(): DemoSession | null {
  if (typeof window === "undefined") {
    return null;
  }
  const raw = sessionStorage.getItem(SESSION_KEY);
  if (!raw) {
    return null;
  }
  try {
    return JSON.parse(raw) as DemoSession;
  } catch {
    return null;
  }
}

export function writeDemoSession(session: DemoSession): void {
  sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
  sessionStorage.setItem(TOKEN_KEY, session.access_token);
}

export function readDemoToken(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  return sessionStorage.getItem(TOKEN_KEY);
}

export function readSseCursor(buildId: string): number {
  if (typeof window === "undefined") {
    return 0;
  }
  const raw = sessionStorage.getItem(`${SSE_CURSOR_PREFIX}${buildId}`);
  if (!raw) {
    return 0;
  }
  const value = Number.parseInt(raw, 10);
  return Number.isFinite(value) && value >= 0 ? value : 0;
}

export function writeSseCursor(buildId: string, sequence: number): void {
  sessionStorage.setItem(`${SSE_CURSOR_PREFIX}${buildId}`, String(sequence));
}
