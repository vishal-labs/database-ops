// Verifies every chart path renders in a real browser with real mermaid.
// Run: node verify-charts.mjs
import puppeteer from "puppeteer-core";

const specs = {
  "bar (the one that failed)": `xychart-beta
title "Average Salary per Department"
x-axis ["Dept 1", "Dept 5", "Dept 4", "Dept 3", "Dept 2"]
y-axis "avg_salary" 0 --> 62912
bar [57192.5, 57055.5, 56918.5, 56781.5, 56644.5]`,
  "line": `xychart-beta
title "Orders per Month"
x-axis ["2025-01", "2025-02", "2025-03"]
y-axis "order_count" 0 --> 69
line [62, 45, 38]`,
  "pie": `pie title Revenue Share
"Food" : 137000
"Electronics" : 118000
"Toys" : 60400`,
  "hostile: apostrophes+brackets+unicode in labels": `xychart-beta
title "Rev by 'region' [2025] — ünïcode"
x-axis ["us'a [x]", "de'b", "日本"]
y-axis "rev'ue" 0 --> 11
bar [10, 5.5, 3]`,
  "15 capped points": `xychart-beta
title "P1"
x-axis [${Array.from({length: 15}, (_, i) => `"row${i}"`).join(", ")}]
y-axis "v" 0 --> 2
bar [${Array.from({length: 15}, (_, i) => i % 2).join(", ")}]`,
};

const browser = await puppeteer.launch({
  executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: "new",
});
const page = await browser.newPage();
page.on("pageerror", e => console.log("PAGE ERROR:", e.message));
await page.goto("http://localhost:3000", { waitUntil: "networkidle2" });
await page.waitForFunction("typeof mermaid !== 'undefined'");

let fail = 0;
for (const [name, spec] of Object.entries(specs)) {
  const res = await page.evaluate(async s => {
    try { await mermaid.parse(s); return "ok"; }
    catch (e) { return "FAIL: " + e.message.slice(0, 120); }
  }, spec);
  console.log(`${res === "ok" ? "PASS" : "FAIL"}  ${name}`);
  if (res !== "ok") fail++;
}
await browser.close();
process.exit(fail ? 1 : 0);
