const { chromium } = require('/home/claude/.npm-global/lib/node_modules/playwright');
const fs = require('fs');
const AXE = fs.readFileSync('/home/claude/a11y/node_modules/axe-core/axe.min.js', 'utf8');
(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 }, bypassCSP: true })).newPage();
  await page.goto('http://127.0.0.1:8811/login');
  await page.fill('input[name=email]', 'admin@browser.test'); await page.fill('input[name=password]', 'browser-pass-123');
  await page.keyboard.press('Enter'); await page.waitForURL('**/');
  await page.goto('http://127.0.0.1:8811/ui/tickets/2');
  // Keyboard only from here: Tab until the customer search field has focus.
  let found = false;
  for (let i = 0; i < 60 && !found; i++) { await page.keyboard.press('Tab'); found = await page.evaluate(() => document.activeElement && document.activeElement.id === 'customer-search'); }
  console.log('reached search field by Tab:', found);
  const ring = await page.evaluate(() => { const s = getComputedStyle(document.activeElement); return `${s.outlineStyle} ${s.outlineWidth}`; });
  console.log('visible focus indicator on field:', ring);
  await page.keyboard.type('north'); await Promise.all([page.waitForURL('**customer_q=north**'), page.keyboard.press('Enter')]);
  console.log('results shown:', await page.locator('.pick-list li').count());
  let onLink = false;
  await page.focus('#customer-search');
  for (let i = 0; i < 10 && !onLink; i++) { await page.keyboard.press('Tab'); onLink = await page.evaluate(() => (document.activeElement.innerText || '').startsWith('Link')); }
  console.log('reached Link button by Tab:', onLink, '| accessible name:', await page.evaluate(() => document.activeElement.innerText.replace(/\s+/g, ' ')));
  await Promise.all([page.waitForURL(u => !u.search.includes('customer_q')), page.keyboard.press('Enter')]);
  console.log('customer now linked on page:', (await page.locator('main').innerText()).includes('Northwind Traders'));
  await page.goto('http://127.0.0.1:8811/ui/tickets/2?customer_q=north');
  await page.addScriptTag({ content: AXE });
  const v = await page.evaluate(async () => (await axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] } })).violations.map(x => x.id));
  console.log('axe violations with results open:', v.length ? v.join(', ') : 'none');
  await browser.close();
})().catch(e => { console.error('FAILED:', e.message); process.exit(1); });
