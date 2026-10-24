// ログイン（アプリ内）。
// - 利用者が自分でパスワードを決めるのではなく、アプリが十分に長い乱数の「ログイン用キー」（約 120 ビット）を発行する。
//   推測できない長さの乱数なので、保存は SHA-256 のハッシュで十分（平文は保存しない・既定の共通パスワードなし）。
//   iPhone のパスワード管理（iCloud キーチェーン）に保存して使う想定。
// - 最初の管理者は、デプロイ時に設定した SETUP_TOKEN を知っている人だけが作れる。
// - セッションは乱数トークン（ハッシュのみ保存）、HttpOnly・Secure・SameSite=Lax の Cookie、30 日。
// - 更新操作は CSRF トークンと Origin を確認する。5 回連続で失敗すると 15 分間ログインを受け付けない。

import { HttpError, nowIso, randomToken, sha256Hex, timingSafeEqual } from "./util";

const KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"; // 読み違えやすい文字（0 O 1 I）を除く
export const SESSION_DAYS = 30;
export const COOKIE = "__Host-sdr_session";
const MAX_FAILURES = 5;
const WINDOW_MS = 15 * 60 * 1000;

export interface User {
  id: number;
  username: string;
  role: string;
  csrf: string;
}

export function generateLoginKey(): string {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  const chars = Array.from(bytes, (b) => KEY_ALPHABET[b % 32]).join("");
  return chars.match(/.{4}/g)!.join("-");
}

export function normalizeKey(key: string): string {
  return key.toUpperCase().replace(/[^A-Z0-9]/g, "");
}

async function keyHash(username: string, key: string): Promise<string> {
  return sha256Hex(`sdr-login-v1:${username}:${normalizeKey(key)}`);
}

export function validUsername(name: string): string {
  const u = (name || "").trim();
  if (!/^[A-Za-z0-9._-]{2,40}$/.test(u)) throw new HttpError(400, "利用者名は英数字・._- の 2〜40 文字にしてください");
  return u;
}

export async function userCount(db: D1Database): Promise<number> {
  const r = await db.prepare("SELECT COUNT(*) AS n FROM users WHERE disabled=0").first<{ n: number }>();
  return Number(r?.n || 0);
}

/** 最初の管理者を作る。戻り値はログイン用キー（この時だけ表示）。 */
export async function setupFirstAdmin(db: D1Database, setupToken: string | undefined, given: string, username: string): Promise<string> {
  if (!setupToken || setupToken.length < 20) {
    throw new HttpError(400, "設定用トークン（SETUP_TOKEN）が未設定か短すぎます。Cloudflare の管理画面で 20 文字以上を設定してください");
  }
  if (!timingSafeEqual(given || "", setupToken)) throw new HttpError(403, "設定用トークンが違います");
  if ((await userCount(db)) > 0) throw new HttpError(409, "初期設定は完了しています");
  return createUser(db, username, "admin");
}

export async function createUser(db: D1Database, username: string, role: "admin" | "member"): Promise<string> {
  const u = validUsername(username);
  const key = generateLoginKey();
  await db.prepare("INSERT INTO users(username, key_hash, role, created_at) VALUES(?,?,?,?) ON CONFLICT(username) DO UPDATE SET key_hash=excluded.key_hash, disabled=0")
    .bind(u, await keyHash(u, key), role, nowIso()).run();
  await db.prepare("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)").bind(u).run();
  return key;
}

export async function setUserDisabled(db: D1Database, userId: number, disabled: boolean): Promise<void> {
  await db.batch([
    db.prepare("UPDATE users SET disabled=? WHERE id=?").bind(disabled ? 1 : 0, userId),
    db.prepare("DELETE FROM sessions WHERE user_id=?").bind(userId),
  ]);
}

export async function login(db: D1Database, username: string, key: string, ip: string): Promise<{ token: string; user: User }> {
  const u = (username || "").trim();
  const failKey = `${u.toLowerCase()}|${ip}`;
  const since = Date.now() - WINDOW_MS;
  const f = await db.prepare("SELECT COUNT(*) AS n FROM login_failures WHERE key=? AND at_ms > ?").bind(failKey, since).first<{ n: number }>();
  if (Number(f?.n || 0) >= MAX_FAILURES) throw new HttpError(429, "ログインの失敗が続いたため、15分ほど待ってから再度お試しください");
  const row = await db.prepare("SELECT * FROM users WHERE username=? AND disabled=0").bind(u).first<any>();
  const hash = await keyHash(u, key || "");
  if (!row || !timingSafeEqual(hash, row.key_hash)) {
    await db.batch([
      db.prepare("INSERT INTO login_failures(key, at_ms) VALUES(?, ?)").bind(failKey, Date.now()),
      db.prepare("DELETE FROM login_failures WHERE at_ms < ?").bind(Date.now() - 24 * 3600 * 1000),
    ]);
    throw new HttpError(401, "利用者名またはログイン用キーが違います");
  }
  const token = randomToken(32);
  const csrf = randomToken(24);
  const now = new Date();
  await db.batch([
    db.prepare("DELETE FROM login_failures WHERE key=?").bind(failKey),
    db.prepare("DELETE FROM sessions WHERE expires_at < ?").bind(now.toISOString()),
    db.prepare("INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, expires_at) VALUES(?,?,?,?,?)")
      .bind(await sha256Hex(token), row.id, csrf, now.toISOString(), new Date(now.getTime() + SESSION_DAYS * 86400000).toISOString()),
    db.prepare("UPDATE users SET last_login_at=? WHERE id=?").bind(nowIso(), row.id),
  ]);
  return { token, user: { id: row.id, username: row.username, role: row.role, csrf } };
}

export async function sessionUser(db: D1Database, token: string | null): Promise<User | null> {
  if (!token) return null;
  const row = await db.prepare(
    "SELECT s.csrf_token, s.expires_at, u.id, u.username, u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND u.disabled=0",
  ).bind(await sha256Hex(token)).first<any>();
  if (!row) return null;
  if (Date.parse(row.expires_at) < Date.now()) return null;
  return { id: row.id, username: row.username, role: row.role, csrf: row.csrf_token };
}

export async function logout(db: D1Database, token: string | null): Promise<void> {
  if (token) await db.prepare("DELETE FROM sessions WHERE token_hash=?").bind(await sha256Hex(token)).run();
}

export function readCookie(req: Request, name: string): string | null {
  const header = req.headers.get("cookie") || "";
  for (const part of header.split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return v.join("=");
  }
  return null;
}

export function sessionCookie(token: string): string {
  return `${COOKIE}=${token}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${SESSION_DAYS * 86400}`;
}

export function clearCookie(): string {
  return `${COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0`;
}
