// 公開ページの取得（転送を 1 回ずつ安全確認・サイズ上限・制限時間・ボット確認は回避しない）。

import { assertSafeUrl, Resolver, UnsafeUrlError } from "./netsafe";

export const USER_AGENT = "Mozilla/5.0 (compatible; SamuraiDealerResearch/2.0; small-scale business research; respects robots.txt)";
const HTML_TYPES = ["text/html", "application/xhtml+xml"];
const CHALLENGE_TITLE = /<title>\s*(?:just a moment|one moment,? please|attention required|checking your browser|access denied|please wait while we verify|ddos-guard|security check)[^<]*<\/title>/i;

export interface FetchResult {
  url: string;
  finalUrl: string;
  status: number;
  contentType: string;
  text: string;
  error: string;
  truncated: boolean;
}

export interface FetchOptions {
  maxBytes: number;
  timeoutMs: number;
  acceptAnyType?: boolean;
  resolver: Resolver | null;
  fetchImpl?: typeof fetch;
}

export async function fetchPage(url: string, opt: FetchOptions): Promise<FetchResult> {
  const doFetch = opt.fetchImpl || fetch;
  let current = url;
  for (let hop = 0; hop <= 5; hop++) {
    try {
      await assertSafeUrl(current, opt.resolver);
    } catch (e) {
      const msg = e instanceof UnsafeUrlError ? e.message : "安全確認に失敗しました";
      return fail(url, current, hop === 0 ? msg : `転送先を拒否: ${msg}`);
    }
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), opt.timeoutMs);
    let resp: Response;
    try {
      resp = await doFetch(current, {
        redirect: "manual",
        signal: ctrl.signal,
        headers: { "User-Agent": USER_AGENT, Accept: "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5", "Accept-Language": "en-GB,en;q=0.8" },
      });
    } catch (e) {
      clearTimeout(timer);
      return fail(url, current, (e as Error).name === "AbortError" ? "接続がタイムアウトしました" : `接続エラー: ${(e as Error).name}`);
    }
    if ([301, 302, 303, 307, 308].includes(resp.status) && resp.headers.get("location")) {
      clearTimeout(timer);
      await resp.body?.cancel();
      try {
        current = new URL(resp.headers.get("location")!, current).toString();
      } catch {
        return fail(url, current, "転送先のURLが不正です");
      }
      continue;
    }
    const ctype = (resp.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();
    const result: FetchResult = { url, finalUrl: current, status: resp.status, contentType: ctype, text: "", error: "", truncated: false };
    const finish = async (error: string) => {
      clearTimeout(timer);
      await resp.body?.cancel().catch(() => undefined);
      result.error = error;
      return result;
    };
    if ((resp.headers.get("cf-mitigated") || "").toLowerCase() === "challenge") return finish("アクセス制限（CAPTCHA・ボット確認等）が表示されたため取得しませんでした");
    if (resp.status === 401 || resp.status === 403) return finish(`HTTP ${resp.status}（アクセス拒否。ボット対策の可能性があるため回避せず、ブラウザーでの手動確認が必要）`);
    if (resp.status === 429) return finish("アクセスが多すぎると応答されたため中止しました（HTTP 429）");
    if (resp.status >= 400) return finish(`HTTP ${resp.status}`);
    if (!opt.acceptAnyType && ctype && !HTML_TYPES.includes(ctype)) return finish(`HTML以外の内容のため読み取りません（${ctype}）`);
    try {
      const reader = resp.body!.getReader();
      const chunks: Uint8Array[] = [];
      let size = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        size += value.byteLength;
        if (size > opt.maxBytes) {
          result.truncated = true;
          await reader.cancel();
          break;
        }
      }
      clearTimeout(timer);
      const bytes = new Uint8Array(Math.min(size, opt.maxBytes));
      let off = 0;
      for (const c of chunks) {
        const n = Math.min(c.byteLength, bytes.length - off);
        if (n <= 0) break;
        bytes.set(c.subarray(0, n), off);
        off += n;
      }
      const charset = ((resp.headers.get("content-type") || "").match(/charset=([\w-]+)/i) || [])[1] || "utf-8";
      let text: string;
      try {
        text = new TextDecoder(charset).decode(bytes);
      } catch {
        text = new TextDecoder("utf-8").decode(bytes);
      }
      if (CHALLENGE_TITLE.test(text.slice(0, 5000))) {
        result.error = "アクセス制限（ボット確認ページ）が表示されたため取得しませんでした";
        return result;
      }
      result.text = text;
      return result;
    } catch (e) {
      clearTimeout(timer);
      return fail(url, current, (e as Error).name === "AbortError" ? "接続がタイムアウトしました" : "読み取り中に接続が切れました");
    }
  }
  return fail(url, current, "転送が多すぎるため中止しました");
}

function fail(url: string, finalUrl: string, error: string): FetchResult {
  return { url, finalUrl, status: 0, contentType: "", text: "", error, truncated: false };
}
