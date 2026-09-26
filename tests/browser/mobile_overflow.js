const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.goto('http://127.0.0.1:8811/login');
  await page.fill('input[name=email]', 'admin@browser.test');
  await page.fill('input[name=password]', 'browser-pass-123');
  await page.click('button[type=submit]');
  for (const path of ['/', '/ui/tickets/1', '/customers', '/kb', '/login']) {
    await page.goto('http://127.0.0.1:8811' + path);
    const res = await page.evaluate(() => {
      const vw = document.documentElement.clientWidth, out = [];
      for (const el of document.querySelectorAll('body *')) {
        const r = el.getBoundingClientRect();
        if (r.right > vw + 1 && r.width > 0) {
          const parentOver = el.parentElement && el.parentElement.getBoundingClientRect().right > vw + 1;
          if (!parentOver) out.push(`${el.tagName.toLowerCase()}${el.className ? '.' + String(el.className).split(' ')[0] : ''} right=${Math.round(r.right)}`);
        }
      }
      return { scrollW: document.documentElement.scrollWidth, vw, culprits: out.slice(0, 6) };
    });
    console.log(`${path}: scrollWidth=${res.scrollW} viewport=${res.vw} ${res.culprits.length ? 'OUTERMOST OVERFLOW: ' + res.culprits.join(', ') : 'ok'}`);
  }
  await browser.close();
})().catch(e => { console.error('SCRIPT FAILED:', e.message); process.exit(1); });
