const { chromium } = require('/home/claude/.npm-global/lib/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  for (const [n, vp] of [['desktop', { width: 1366, height: 900 }], ['mobile', { width: 390, height: 844 }]]) {
    const page = await browser.newPage({ viewport: vp });
    await page.goto('http://127.0.0.1:8811/login');
    await page.fill('input[name=email]', 'admin@browser.test'); await page.fill('input[name=password]', 'browser-pass-123');
    await Promise.all([page.waitForURL('**/'), page.keyboard.press('Enter')]);
    await page.goto('http://127.0.0.1:8811/ui/tickets/1'); await page.waitForTimeout(400);
    const info = await page.evaluate(() => {
      const main = document.querySelector('.ticket-main').getBoundingClientRect(), side = document.querySelector('.ticket-side').getBoundingClientRect();
      const conv = document.querySelector('#conversation').getBoundingClientRect();
      return { twoColumn: side.left > main.right - 1, conversationTop: Math.round(conv.top), overflow: document.documentElement.scrollWidth - innerWidth };
    });
    console.log(`${n}: two-column=${info.twoColumn} conversation starts at y=${info.conversationTop}px overflow=${info.overflow}`);
    // call confirm: first click must NOT submit; it only reveals the real button
    const summary = page.locator('.call-confirm summary').first();
    if (await summary.count()) {
      let posted = false; page.on('request', r => { if (r.method() === 'POST' && r.url().includes('/call')) posted = true; });
      await summary.click(); await page.waitForTimeout(200);
      console.log(`${n}: after first click - call placed=${posted}, confirm button visible=${await page.locator('.call-confirm[open] button').isVisible()}`);
    }
    await page.screenshot({ path: `/home/claude/a11y/layout-${n}.png` });
    await page.close();
  }
  await browser.close();
})().catch(e => { console.error('FAILED:', e.message); process.exit(1); });
