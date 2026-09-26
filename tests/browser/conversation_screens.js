const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  for (const [name, vp] of [['desktop', { width: 1280, height: 900 }], ['mobile', { width: 390, height: 844 }]]) {
    const page = await browser.newPage({ viewport: vp });
    await page.goto('http://127.0.0.1:8811/login');
    await page.fill('input[name=email]', 'admin@browser.test');
    await page.fill('input[name=password]', 'browser-pass-123');
    await page.click('button[type=submit]');
    await page.goto('http://127.0.0.1:8811/ui/tickets/1');
    if (name === 'desktop') {
      await page.fill('#note-body', 'Customer called twice already - escalate if the reinstall fails.\nSecond line to check wrapping.');
      await page.click('.note-form button');
      await page.waitForLoadState('load');
    }
    const panel = page.locator('#conversation');
    await panel.scrollIntoViewIfNeeded();
    await page.waitForTimeout(400);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    console.log(`${name}: horizontal overflow=${overflow}, timeline items=${await page.locator('.timeline-item').count()}`);
    await panel.screenshot({ path: `notes-${name}.png` });
    await page.close();
  }
  await browser.close();
})().catch(e => { console.error('SCRIPT FAILED:', e.message); process.exit(1); });
