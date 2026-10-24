// 取得先 URL の安全確認（内部アドレス・ローカル・管理用アドレス等を拒否）。
// Workers では接続先 IP を直接確認できないため、名前解決（DNS over HTTPS）の結果を確認する。

export class UnsafeUrlError extends Error {}

const BLOCKED_HOSTS = new Set(["localhost", "metadata", "metadata.google.internal"]);
const BLOCKED_SUFFIXES = [".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet", ".arpa"];

export function normalizeUrl(input: string): string {
  let url = (input || "").trim();
  if (!url) throw new UnsafeUrlError("URLが空です");
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(url)) url = "https://" + url;
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    throw new UnsafeUrlError("URLの形式が正しくありません");
  }
  u.hash = "";
  u.hostname = u.hostname.toLowerCase().replace(/\.$/, "");
  return u.toString();
}

export function hostKey(url: string): string {
  try {
    const h = new URL(url.includes("://") ? url : "https://" + url).hostname.toLowerCase().replace(/\.$/, "");
    return h.startsWith("www.") ? h.slice(4) : h;
  } catch {
    return "";
  }
}

function ipv4ToInt(ip: string): number | null {
  const m = ip.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!m) return null;
  const parts = m.slice(1).map(Number);
  if (parts.some((p) => p > 255)) return null;
  return ((parts[0] << 24) >>> 0) + (parts[1] << 16) + (parts[2] << 8) + parts[3];
}

const V4_BLOCKS: [string, number][] = [
  ["0.0.0.0", 8], ["10.0.0.0", 8], ["100.64.0.0", 10], ["127.0.0.0", 8], ["169.254.0.0", 16], ["172.16.0.0", 12],
  ["192.0.0.0", 24], ["192.0.2.0", 24], ["192.168.0.0", 16], ["198.18.0.0", 15], ["198.51.100.0", 24],
  ["203.0.113.0", 24], ["224.0.0.0", 4], ["240.0.0.0", 4],
];

export function isPublicIPv4(ip: string): boolean {
  const n = ipv4ToInt(ip);
  if (n === null) return false;
  for (const [base, bits] of V4_BLOCKS) {
    const b = ipv4ToInt(base)!;
    const mask = bits === 0 ? 0 : (~0 << (32 - bits)) >>> 0;
    if (((n & mask) >>> 0) === ((b & mask) >>> 0)) return false;
  }
  return true;
}

function expandIPv6(ip: string): number[] | null {
  let s = ip.replace(/^\[|\]$/g, "").split("%")[0].toLowerCase();
  let tail: number[] = [];
  const v4 = s.match(/:(\d+\.\d+\.\d+\.\d+)$/);
  if (v4) {
    const n = ipv4ToInt(v4[1]);
    if (n === null) return null;
    tail = [(n >>> 16) & 0xffff, n & 0xffff];
    s = s.slice(0, -v4[1].length) + "0:0";
  }
  const halves = s.split("::");
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(":") : [];
  const rest = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  const fill = halves.length === 2 ? 8 - head.length - rest.length : 0;
  const all = [...head, ...Array(Math.max(fill, 0)).fill("0"), ...rest];
  if (all.length !== 8 || all.some((p) => !/^[0-9a-f]{1,4}$/.test(p))) return null;
  const words = all.map((p) => parseInt(p, 16));
  if (tail.length) words.splice(6, 2, ...tail);
  return words;
}

export function isPublicIPv6(ip: string): boolean {
  const w = expandIPv6(ip);
  if (!w) return false;
  if (w.every((x) => x === 0)) return false; // ::
  if (w.slice(0, 7).every((x) => x === 0) && w[7] === 1) return false; // ::1
  if (w.slice(0, 5).every((x) => x === 0) && w[5] === 0xffff) {
    return isPublicIPv4(`${w[6] >> 8}.${w[6] & 255}.${w[7] >> 8}.${w[7] & 255}`); // ::ffff:a.b.c.d
  }
  if (w[0] === 0x64 && w[1] === 0xff9b) return isPublicIPv4(`${w[6] >> 8}.${w[6] & 255}.${w[7] >> 8}.${w[7] & 255}`);
  if ((w[0] & 0xfe00) === 0xfc00) return false; // fc00::/7
  if ((w[0] & 0xffc0) === 0xfe80) return false; // fe80::/10
  if ((w[0] & 0xff00) === 0xff00) return false; // multicast
  if (w[0] === 0x2001 && w[1] === 0x0db8) return false; // documentation
  return true;
}

export function isPublicIp(ip: string): boolean {
  return ip.includes(":") ? isPublicIPv6(ip) : isPublicIPv4(ip);
}

/** 構文だけで判定できる拒否。戻り値はホスト名。 */
export function checkUrlSyntax(url: string): string {
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    throw new UnsafeUrlError("URLの形式が正しくありません");
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") throw new UnsafeUrlError("http/https 以外のURLは扱いません");
  if (u.username || u.password) throw new UnsafeUrlError("認証情報を含むURLは扱いません");
  if (u.port && u.port !== "80" && u.port !== "443") throw new UnsafeUrlError("標準以外のポート番号のURLは扱いません");
  const host = u.hostname.toLowerCase().replace(/\.$/, "");
  if (!host) throw new UnsafeUrlError("ホスト名がありません");
  if (BLOCKED_HOSTS.has(host) || BLOCKED_SUFFIXES.some((s) => host.endsWith(s))) throw new UnsafeUrlError("内部向けのホスト名は扱いません");
  const bare = host.replace(/^\[|\]$/g, "");
  const isV4 = ipv4ToInt(bare) !== null;
  const isV6 = bare.includes(":");
  if (isV4 || isV6) {
    if (!isPublicIp(bare)) throw new UnsafeUrlError("内部・予約済みのIPアドレスは扱いません");
    return host;
  }
  if (/^\d+$/.test(bare) || /^0x/i.test(bare)) throw new UnsafeUrlError("数値表記のホストは扱いません");
  if (!host.includes(".")) throw new UnsafeUrlError("ドメイン名ではないホストは扱いません");
  return host;
}

export type Resolver = (host: string) => Promise<string[]>;

/** DNS over HTTPS（Cloudflare 1.1.1.1）で A/AAAA を引く */
export const dohResolver: Resolver = async (host) => {
  const ips: string[] = [];
  for (const type of ["A", "AAAA"]) {
    const r = await fetch(`https://cloudflare-dns.com/dns-query?name=${encodeURIComponent(host)}&type=${type}`, {
      headers: { accept: "application/dns-json" },
    });
    if (!r.ok) continue;
    const data = (await r.json()) as { Answer?: { type: number; data: string }[] };
    for (const a of data.Answer || []) if (a.type === 1 || a.type === 28) ips.push(a.data);
  }
  return ips;
};

const resolvedCache = new Map<string, number>();

export async function assertSafeUrl(url: string, resolver: Resolver | null = dohResolver): Promise<void> {
  const host = checkUrlSyntax(url);
  const bare = host.replace(/^\[|\]$/g, "");
  if (!resolver || ipv4ToInt(bare) !== null || bare.includes(":")) return;
  const cached = resolvedCache.get(host);
  if (cached && Date.now() - cached < 10 * 60 * 1000) return;
  const ips = await resolver(host);
  if (!ips.length) throw new UnsafeUrlError(`名前解決できません（${host}）`);
  for (const ip of ips) {
    if (!isPublicIp(ip)) throw new UnsafeUrlError(`${host} は内部・予約済みのアドレス（${ip}）に解決されるため扱いません`);
  }
  resolvedCache.set(host, Date.now());
}
