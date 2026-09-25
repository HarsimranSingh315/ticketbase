const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const out = (k, v) => console.log(`${k}: ${v}`);
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  page.on('response', r => { if (r.status() >= 400) console.log('HTTP', r.status(), r.request().method(), r.url()); });
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });

  await page.goto('http://127.0.0.1:8811/login');
  await page.fill('input[name=email]', 'admin@browser.test');
  await page.fill('input[name=password]', 'browser-pass-123');
  await page.click('button[type=submit]');
  await page.goto('http://127.0.0.1:8811/ui/tickets/1');

  // 1. happy path: result appears AND the form survives
  await page.click('#suggest-form button');
  await page.waitForSelector('#suggestion-result .suggestion-box:not(.is-pending)');
  out('form still present after result', await page.locator('#suggest-form').count() === 1);
  out('button label after result', await page.locator('#suggest-form button').innerText());
  out('source links to KB article', await page.locator('#suggestion-result a[href^="/kb/"]').count() > 0);
  out('no percentage framing', !(await page.locator('#suggestion-result').innerText()).includes('% similar'));
  await page.waitForTimeout(600);  // let the 0.35s reveal animation finish
  const box = page.locator('#suggestion-result .suggestion-box');
  out('result opacity after animation', await box.evaluate(e => getComputedStyle(e).opacity));
  await box.screenshot({ path: 'b12-result.png' });

  // 2. retry works (second request succeeds)
  await page.click('#suggest-form button');
  await page.waitForSelector('#suggestion-result .suggestion-box:not(.is-pending)');
  out('retry produced a result', true);

  // 3. network failure -> readable error, button re-enabled
  await page.route('**/tickets/1/suggest', r => r.abort('failed'));
  await page.click('#suggest-form button');
  await page.waitForSelector('#suggestion-result [role=alert]');
  out('network failure message', await page.locator('#suggestion-result [role=alert]').innerText());
  out('button re-enabled after failure', await page.locator('#suggest-form button').isEnabled());
  await page.unroute('**/tickets/1/suggest');

  // 4. hung request -> aborted by timeout (clock fast-forwarded, not waited)
  await page.clock.install();
  await page.route('**/tickets/1/suggest', () => {});  // never responds
  await page.click('#suggest-form button');
  out('button disabled while pending', !(await page.locator('#suggest-form button').isEnabled()));
  out('aria-busy while pending', await page.locator('#suggestion-result').getAttribute('aria-busy'));
  await page.clock.fastForward(31000);
  await page.waitForSelector('#suggestion-result [role=alert]');
  out('timeout message', await page.locator('#suggestion-result [role=alert]').innerText());
  out('button re-enabled after timeout', await page.locator('#suggest-form button').isEnabled());

  out('page/console errors', errors.length ? errors.join(' | ') : 'none');
  await browser.close();
})().catch(e => { console.error('SCRIPT FAILED:', e.message); process.exit(1); });
