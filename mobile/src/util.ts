// 共通の小物（時刻・ID・応答・入力検証）。

export interface Env {
  DB: D1Database;
  ASSETS: Fetcher;
  SETUP_TOKEN?: string;
  FIRECRAWL_API_KEY?: string;
  APP_ENV?: string;
}

export function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

// 変更されない内部ID（接頭辞-ULID）。店名・URL・メールが変わっても ID は変えない。
export function ulid(): string {
  let time = Date.now();
  const timeChars: string[] = [];
  for (let i = 0; i < 10; i++) {
    timeChars.unshift(ALPHABET[time % 32]);
    time = Math.floor(time / 32);
  }
  const rand = new Uint8Array(16);
  crypto.getRandomValues(rand);
  const randChars = Array.from(rand, (b) => ALPHABET[b % 32]).join("");
  return timeChars.join("") + randChars;
}

export const newId = (prefix: "DLR" | "CNT" | "CAN" | "JOB" | "RUN") => `${prefix}-${ulid()}`;

export class HttpError extends Error {
  constructor(public status: number, message: string, public extra: Record<string, unknown> = {}) {
    super(message);
  }
}

export const SECURITY_HEADERS: Record<string, string> = {
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "no-referrer",
  "Cache-Control": "no-store",
  "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
};

export function json(data: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...SECURITY_HEADERS, ...headers },
  });
}

export function clean(text: string | null | undefined): string {
  return (text || "").replace(/\s+/g, " ").trim();
}

export function excerptAround(text: string, needle: string, width = 240): string {
  const idx = text.toLowerCase().indexOf(needle.toLowerCase());
  if (idx < 0) return text.slice(0, width);
  const start = Math.max(0, idx - Math.floor(width / 2));
  const end = Math.min(text.length, idx + needle.length + Math.floor(width / 2));
  return (start > 0 ? "…" : "") + text.slice(start, end) + (end < text.length ? "…" : "");
}

export function str(v: unknown, max = 2000): string {
  if (v === null || v === undefined) return "";
  return String(v).slice(0, max);
}

export async function sha256Hex(text: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0")).join("");
}

export function randomToken(bytes = 32): string {
  const arr = new Uint8Array(bytes);
  crypto.getRandomValues(arr);
  return btoa(String.fromCharCode(...arr)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function timingSafeEqual(a: string, b: string): boolean {
  const ea = new TextEncoder().encode(a);
  const eb = new TextEncoder().encode(b);
  let diff = ea.length ^ eb.length;
  for (let i = 0; i < Math.max(ea.length, eb.length); i++) diff |= (ea[i] ?? 0) ^ (eb[i] ?? 0);
  return diff === 0;
}
