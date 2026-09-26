const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.goto('http://127.0.0.1:8811/login');
  await page.fill('input[name=email]', 'admin@browser.test');
  await page.fill('input[name=password]', 'browser-pass-123');
  await page.click('button[type=submit]');
  await page.goto('http://127.0.0.1:8811/?owner=unassigned');
  console.log('overflow at 390:', await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth));
  console.log('active tab:', await page.locator('.queue-tabs [aria-current=page]').innerText());
  // keyboard: Tab until a queue tab is focused, check a visible focus indicator exists
  await page.focus('.queue-tabs a');
  const outline = await page.evaluate(() => { const s = getComputedStyle(document.activeElement); return s.outlineStyle + ' ' + s.outlineWidth; });
  console.log('focus indicator on queue tab:', outline);
  await page.locator('.queue-tabs').screenshot({ path: 'queues-mobile.png' });
  await browser.close();
})().catch(e => { console.error('SCRIPT FAILED:', e.message); process.exit(1); });
