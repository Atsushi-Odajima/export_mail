// HTMLRewriter（Workers 標準・逐次処理）で HTML を読み、PageFacts を作る。
// 外部ページの HTML は実行せず、文字列として読むだけ。ページ内の文章をアプリへの命令として扱わない。

import {
  classifyForm, decodeCfEmail, emailsFromText, FoundEmail, PageFacts, phonesFromText, postcodesFromText, RawForm, validEmail,
} from "./extract";
import { clean } from "./util";

const EMBED_PROVIDERS: [string, string][] = [
  ["hsforms", "HubSpot フォーム"], ["hubspot", "HubSpot フォーム"], ["jotform", "Jotform"], ["typeform", "Typeform"],
  ["docs.google.com/forms", "Google フォーム"], ["forms.gle", "Google フォーム"], ["formstack", "Formstack"],
  ["cognitoforms", "Cognito Forms"], ["wufoo", "Wufoo"], ["123formbuilder", "123FormBuilder"],
  ["gravityforms", "Gravity Forms"], ["wpforms", "WPForms"], ["contact-form-7", "Contact Form 7"],
  ["ninja-forms", "Ninja Forms"], ["formspree", "Formspree"], ["zohopublic", "Zoho Forms"], ["forms.office.com", "Microsoft Forms"],
];

export async function parseHtml(html: string, url: string, role: string, textLimit = 40000): Promise<PageFacts> {
  const facts: PageFacts = {
    url, role, title: "", siteName: "", metaDescription: "", h1: [], text: "", footer: "", links: [], emails: [],
    phones: [], forms: [], embeds: [], jsonld: [], postcodes: [], truncated: false,
  };
  let text = "";
  let footer = "";
  let title = "";
  let skipDepth = 0;
  let footerDepth = 0;
  let inTitle = false;
  let h1Depth = 0;
  let h1Buf = "";
  let jsonldDepth = 0;
  let jsonldBuf = "";
  const jsonldTexts: string[] = [];
  let inlineScript = "";
  let inScript = false;
  let anchor: { h: string; a: string } | null = null;
  const formStack: RawForm[] = [];
  const known = new Set<string>();

  const addEmail = (e: FoundEmail) => {
    if (known.has(e.email) || !validEmail(e.email)) return;
    known.add(e.email);
    facts.emails.push(e);
  };
  const addEmbed = (label: string) => {
    if (!facts.embeds.includes(label)) facts.embeds.push(label);
  };
  const tail = () => text.slice(-160);

  const rewriter = new HTMLRewriter()
    .on("script, style, noscript, template, svg", {
      element(el) {
        skipDepth++;
        const tag = el.tagName.toLowerCase();
        if (tag === "script") {
          inScript = true;
          const src = (el.getAttribute("src") || "").toLowerCase();
          for (const [k, label] of EMBED_PROVIDERS) if (src.includes(k)) addEmbed(label);
          if ((el.getAttribute("type") || "").toLowerCase().includes("ld+json")) jsonldDepth++;
        }
        el.onEndTag(() => {
          skipDepth--;
          if (tag === "script") {
            inScript = false;
            if (jsonldDepth > 0) {
              jsonldDepth--;
              if (jsonldBuf) jsonldTexts.push(jsonldBuf);
              jsonldBuf = "";
            }
          }
        });
      },
    })
    .on("iframe[src]", {
      element(el) {
        const src = (el.getAttribute("src") || "").toLowerCase();
        for (const [k, label] of EMBED_PROVIDERS) if (src.includes(k)) addEmbed(label);
      },
    })
    .on('[class*="wpcf7"], [class*="gform_wrapper"], [class*="wpforms"], [class*="nf-form"], [class*="frm_forms"]', {
      element() {
        addEmbed("WordPress フォームプラグイン");
      },
    })
    .on("title", {
      element(el) {
        inTitle = true;
        el.onEndTag(() => {
          inTitle = false;
        });
      },
    })
    .on("meta", {
      element(el) {
        const prop = (el.getAttribute("property") || "").toLowerCase();
        const name = (el.getAttribute("name") || "").toLowerCase();
        const content = clean(el.getAttribute("content") || "");
        if (prop === "og:site_name") facts.siteName = content.slice(0, 120);
        if (name === "description") facts.metaDescription = content.slice(0, 400);
      },
    })
    .on("h1", {
      element(el) {
        h1Depth++;
        el.onEndTag(() => {
          h1Depth--;
          if (h1Depth === 0 && facts.h1.length < 5) facts.h1.push(clean(h1Buf).slice(0, 200));
          if (h1Depth === 0) h1Buf = "";
        });
      },
    })
    .on("footer", {
      element(el) {
        footerDepth++;
        el.onEndTag(() => {
          footerDepth--;
        });
      },
    })
    .on("[data-cfemail]", {
      element(el) {
        const email = decodeCfEmail(el.getAttribute("data-cfemail") || "").toLowerCase();
        addEmail({ email, method: "保護表示の復号", context: tail(), inFooter: footerDepth > 0 });
      },
    })
    .on("a[href]", {
      element(el) {
        const href = (el.getAttribute("href") || "").trim();
        const low = href.toLowerCase();
        if (low.startsWith("mailto:")) {
          let addr = href.slice(7).split("?")[0];
          try {
            addr = decodeURIComponent(addr);
          } catch {
            /* そのまま */
          }
          for (const part of addr.split(",")) {
            addEmail({ email: part.trim().toLowerCase(), method: "mailto", context: tail(), inFooter: footerDepth > 0 });
          }
        } else if (low.startsWith("tel:")) {
          let num = href.slice(4);
          try {
            num = decodeURIComponent(num);
          } catch {
            /* そのまま */
          }
          if (num.replace(/\D/g, "").length >= 9 && facts.phones.length < 10) facts.phones.push({ n: clean(num), a: "" });
        } else if (low.includes("/cdn-cgi/l/email-protection#")) {
          const email = decodeCfEmail(href.split("#")[1] || "").toLowerCase();
          addEmail({ email, method: "保護表示の復号", context: tail(), inFooter: footerDepth > 0 });
        } else if (!low.startsWith("#") && !/^[a-z][a-z0-9+.-]*:/.test(low) || low.startsWith("http://") || low.startsWith("https://")) {
          if (facts.links.length < 400) {
            try {
              const abs = new URL(href, url);
              abs.hash = "";
              anchor = { h: abs.toString(), a: "" };
              facts.links.push(anchor);
            } catch {
              /* 無効なリンク */
            }
          }
        }
        el.onEndTag(() => {
          anchor = null;
        });
      },
    })
    .on("form", {
      element(el) {
        formStack.push({
          attrs: ["action", "id", "class", "name", "role"].map((a) => el.getAttribute(a) || "").join(" "),
          role: (el.getAttribute("role") || "").toLowerCase(),
          text: "",
          inputs: [],
        });
        el.onEndTag(() => {
          const f = formStack.pop();
          if (f && facts.forms.length < 20) facts.forms.push(classifyForm(f));
        });
      },
    })
    .on("form input, form textarea, form select", {
      element(el) {
        const f = formStack[formStack.length - 1];
        if (!f || f.inputs.length > 40) return;
        const tag = el.tagName.toLowerCase();
        const type = tag === "input" ? (el.getAttribute("type") || "text").toLowerCase() : tag;
        const names = ["name", "id", "placeholder", "aria-label"].map((a) => el.getAttribute(a) || "").join(" ");
        f.inputs.push({ type, names });
      },
    })
    .onDocument({
      text(t) {
        const s = t.text;
        if (!s) return;
        if (inTitle) title += s;
        if (jsonldDepth > 0) {
          if (jsonldBuf.length < 30000) jsonldBuf += s;
          return;
        }
        if (inScript) {
          if (inlineScript.length < 20000) inlineScript += s.slice(0, 2000);
          return;
        }
        if (skipDepth > 0) return;
        if (text.length < textLimit) text += s + " ";
        else facts.truncated = true;
        if (footerDepth > 0 && footer.length < 3000) footer += s + " ";
        if (h1Depth > 0 && h1Buf.length < 300) h1Buf += s;
        if (anchor && anchor.a.length < 120) anchor.a += s;
        const f = formStack[formStack.length - 1];
        if (f && f.text.length < 600) f.text += s + " ";
      },
    });

  await rewriter.transform(new Response(html, { headers: { "content-type": "text/html; charset=utf-8" } })).arrayBuffer();

  const lowScript = inlineScript.toLowerCase();
  for (const [k, label] of EMBED_PROVIDERS) if (lowScript.includes(k)) addEmbed(label);
  facts.title = clean(title).slice(0, 300);
  facts.text = clean(decodeEntities(text));
  facts.footer = clean(decodeEntities(footer));
  for (const l of facts.links) l.a = clean(decodeEntities(l.a)).slice(0, 120);
  for (const e of facts.emails) e.context = clean(decodeEntities(e.context)).slice(0, 240);
  for (const raw of jsonldTexts) {
    try {
      collectJsonLd(JSON.parse(raw), facts.jsonld);
    } catch {
      /* 壊れた JSON-LD は無視 */
    }
  }
  for (const obj of facts.jsonld) {
    const email = String(obj.email || "").replace("mailto:", "").trim().toLowerCase();
    if (email) addEmail({ email, method: "構造化データ", context: `${obj["@type"] || ""} ${obj.name || ""}`, inFooter: false });
  }
  for (const e of emailsFromText(facts.text, facts.footer, known)) facts.emails.push(e);
  facts.postcodes = postcodesFromText(facts.text);
  if (!facts.phones.length) facts.phones = phonesFromText(facts.text);
  return facts;
}

function collectJsonLd(data: unknown, out: Record<string, unknown>[]): void {
  if (out.length >= 20) return;
  if (Array.isArray(data)) {
    for (const d of data) collectJsonLd(d, out);
  } else if (data && typeof data === "object") {
    const obj = data as Record<string, unknown>;
    if (obj["@graph"]) collectJsonLd(obj["@graph"], out);
    // 必要な項目だけ残す
    out.push({ "@type": obj["@type"], name: obj.name, email: obj.email, telephone: obj.telephone, address: obj.address });
  }
}

const ENTITIES: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", "#39": "'", pound: "£" };

export function decodeEntities(s: string): string {
  return s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+\d*);/gi, (m, e: string) => {
    const k = e.toLowerCase();
    if (k in ENTITIES) return ENTITIES[k];
    if (k.startsWith("#x")) return String.fromCodePoint(parseInt(k.slice(2), 16) || 32);
    if (k.startsWith("#")) return String.fromCodePoint(parseInt(k.slice(1), 10) || 32);
    return m;
  });
}
