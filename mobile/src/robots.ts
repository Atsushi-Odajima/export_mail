// robots.txt の解釈（User-agent のグループ、Allow / Disallow の最長一致、* と $）。

export const ROBOTS_TOKEN = "samuraidealerresearch";

interface Rule {
  allow: boolean;
  pattern: string;
}

function parseGroups(text: string): { agents: string[]; rules: Rule[] }[] {
  const groups: { agents: string[]; rules: Rule[] }[] = [];
  let current: { agents: string[]; rules: Rule[] } | null = null;
  let lastWasAgent = false;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/#.*$/, "").trim();
    if (!line) continue;
    const idx = line.indexOf(":");
    if (idx < 0) continue;
    const field = line.slice(0, idx).trim().toLowerCase();
    const value = line.slice(idx + 1).trim();
    if (field === "user-agent") {
      if (!current || !lastWasAgent) {
        current = { agents: [], rules: [] };
        groups.push(current);
      }
      current.agents.push(value.toLowerCase());
      lastWasAgent = true;
    } else if ((field === "allow" || field === "disallow") && current) {
      lastWasAgent = false;
      if (field === "disallow" && value === "") continue; // 空の Disallow は全許可
      current.rules.push({ allow: field === "allow", pattern: value });
    } else {
      lastWasAgent = false;
    }
  }
  return groups;
}

function toRegex(pattern: string): RegExp {
  const anchored = pattern.endsWith("$");
  const body = (anchored ? pattern.slice(0, -1) : pattern)
    .split("*")
    .map((s) => s.replace(/[.+?^${}()|[\]\\]/g, "\\$&"))
    .join(".*");
  return new RegExp("^" + body + (anchored ? "$" : ""));
}

export function robotsAllows(robotsTxt: string, url: string, token = ROBOTS_TOKEN): boolean {
  const groups = parseGroups(robotsTxt);
  let chosen = groups.filter((g) => g.agents.some((a) => a !== "*" && token.includes(a)));
  if (!chosen.length) chosen = groups.filter((g) => g.agents.includes("*"));
  const rules = chosen.flatMap((g) => g.rules);
  const u = new URL(url);
  const path = decodeSafe(u.pathname) + u.search;
  let best: Rule | null = null;
  for (const r of rules) {
    if (!r.pattern) continue;
    if (toRegex(decodeSafe(r.pattern)).test(path)) {
      if (!best || r.pattern.length > best.pattern.length || (r.pattern.length === best.pattern.length && r.allow)) best = r;
    }
  }
  return best ? best.allow : true;
}

function decodeSafe(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}
