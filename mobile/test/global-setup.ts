// 試験の前に本番と同じ手順でバンドルし、試験用の補助 Worker（HTMLRewriter 等を直接呼ぶ）も作る。
import { execSync } from "node:child_process";
import { resolve } from "node:path";

export default function () {
  const root = resolve(__dirname, "..");
  execSync("npx wrangler deploy --dry-run --outdir dist", { cwd: root, stdio: "ignore", env: { ...process.env, CI: "1", WRANGLER_SEND_METRICS: "false" } });
  execSync("npx esbuild test/unit-worker.ts --bundle --format=esm --platform=neutral --outfile=dist-test/unit-worker.js --loader:.seed=text", { cwd: root, stdio: "ignore" });
}
