import { chromium } from "playwright";
const [out, url = "http://127.0.0.1:3002/demo", w = "1600", h = "1050", wait = "3000"] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: +w, height: +h }, deviceScaleFactor: 2 });
let b2 = 0;
page.on("response", (r) => { if (r.url().includes("backblazeb2.com")) b2 += 1; });
const t0 = Date.now();
await page.goto(url, { waitUntil: "load", timeout: 60000 });
await page.getByRole("heading", { name: /storyboard/i }).waitFor({ timeout: 30000 }).catch(() => {});
const tReady = Date.now() - t0;
await page.waitForFunction(
  () => [...document.querySelectorAll("img")].filter((i) => i.naturalWidth > 0).length >= 4,
  { timeout: 20000 },
).catch(() => {});
const tMedia = Date.now() - t0;
await page.waitForTimeout(+wait);
await page.screenshot({ path: out });
const painted = await page.evaluate(() => [...document.querySelectorAll("img")].filter((i) => i.naturalWidth > 0).length);
console.log(`ready=${tReady}ms  media=${tMedia}ms  painted=${painted}  b2Requests=${b2}`);
await browser.close();
