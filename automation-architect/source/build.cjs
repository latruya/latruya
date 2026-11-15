// Renders report.html to the PDF one level up. Run: npm install && node build.cjs
// Needs Playwright with Chromium available to require().
const { chromium } = require('playwright');
const { pathToFileURL } = require('node:url');
const path = require('node:path');

(async () => {
  const out = path.join(__dirname, '..', 'Automation-Architect-Recommendations.pdf');
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(pathToFileURL(path.join(__dirname, 'report.html')).href, { waitUntil: 'networkidle' });
  await page.evaluate(() => document.fonts.ready);
  await page.pdf({ path: out, preferCSSPageSize: true, printBackground: true, tagged: true });
  await browser.close();
  console.log('wrote', out);
})();
