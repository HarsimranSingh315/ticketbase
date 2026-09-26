const { chromium } = require('/home/claude/.npm-global/lib/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });   // CSP ENFORCED here (no bypass)
  const external = [], failed = [], cspErrors = [];
  page.on('request', r => { const u = new URL(r.url()); if (u.host !== '127.0.0.1:8811') external.push(u.host); });
  page.on('response', r => { if (r.status() >= 400) failed.push(`${r.status()} ${r.url()}`); });
  page.on('console', m => { if (/Content Security Policy|Refused/.test(m.text())) cspErrors.push(m.text().slice(0, 120)); });
  await page.goto('http://127.0.0.1:8811/login');
  await page.fill('input[name=email]', 'admin@browser.test'); await page.fill('input[name=password]', 'browser-pass-123');
  await Promise.all([page.waitForURL('**/'), page.keyboard.press('Enter')]);
  await page.goto('http://127.0.0.1:8811/ui/tickets/1');
  await page.evaluate(() => document.fonts.ready);
  const fonts = await page.evaluate(() => [...document.fonts].map(f => `${f.family.replace(/"/g,'')} ${f.weight}: ${f.status}`));
  console.log(fonts.join('\n'));
  const used = await page.evaluate(() => ({ body: getComputedStyle(document.body).fontFamily.split(',')[0], h1: getComputedStyle(document.querySelector('.wordmark')).fontFamily.split(',')[0] }));
  console.log('computed body font:', used.body, '| wordmark font:', used.h1);
  console.log('requests to other origins:', external.length ? [...new Set(external)].join(', ') : 'NONE');
  console.log('failed responses:', failed.length ? failed.join('; ') : 'none');
  console.log('CSP violations:', cspErrors.length ? cspErrors.join('; ') : 'none');
  const r = await page.request.get('http://127.0.0.1:8811/static/fonts/public-sans-latin-400-normal.woff2');
  console.log('font response:', r.status(), r.headers()['content-type'], '| cache-control:', r.headers()['cache-control'] || '(none)');
  await page.screenshot({ path: '/home/claude/a11y/fonts-ticket.png' });
  await browser.close();
})().catch(e => { console.error('FAILED:', e.message); process.exit(1); });
