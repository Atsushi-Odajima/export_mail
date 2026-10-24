// PC 版の確認済み初期データ（../sdr/seed/initial_dealers.json）を、1 社ずつ取り込める形に分割する。
// 出力：seed/<販売店ID>.seed（Worker に文字列として同梱）と src/seed-files.ts（一覧）
import { readFileSync, writeFileSync, mkdirSync, readdirSync, rmSync } from "node:fs";
const src = JSON.parse(readFileSync(new URL("../../sdr/seed/initial_dealers.json", import.meta.url), "utf8"));
mkdirSync(new URL("../seed/", import.meta.url), { recursive: true });
for (const f of readdirSync(new URL("../seed/", import.meta.url))) if (f.endsWith(".seed")) rmSync(new URL(`../seed/${f}`, import.meta.url));
const ids = [];
for (const d of src.dealers) {
  const contactIds = new Set(src.contact_methods.filter((c) => c.dealer_id === d.id).map((c) => c.id));
  const part = {
    format: "sdr-seed-dealer-1",
    dealer: d,
    contact_methods: src.contact_methods.filter((c) => c.dealer_id === d.id),
    evidence: src.evidence.filter((e) => e.dealer_id === d.id),
    research_runs: src.research_runs.filter((r) => r.dealer_id === d.id),
    change_log: src.change_log.filter((c) => c.entity_id === d.id || contactIds.has(c.entity_id)),
  };
  writeFileSync(new URL(`../seed/${d.id}.seed`, import.meta.url), JSON.stringify(part));
  ids.push(d.id);
}
const lines = ["// 自動生成（scripts/build-seed.mjs）。編集しないでください。", "/// <reference path=\"./seed.d.ts\" />", ...ids.map((id, i) => `import s${i} from "../seed/${id}.seed";`),
  "", "export const SEED_FILES: string[] = [" + ids.map((_, i) => `s${i}`).join(", ") + "];", ""];
writeFileSync(new URL("../src/seed-files.ts", import.meta.url), lines.join("\n"));
writeFileSync(new URL("../src/seed.d.ts", import.meta.url), 'declare module "*.seed" {\n  const text: string;\n  export default text;\n}\n');
console.log(`初期データ ${ids.length} 社を分割しました`);
