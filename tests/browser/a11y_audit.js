const { chromium } = require('/home/claude/.npm-global/lib/node_modules/playwright');
const fs = require('fs');
const AXE = fs.readFileSync('/home/claude/a11y/node_modules/axe-core/axe.min.js', 'utf8');
const BASE = 'http://127.0.0.1:8811';
const PAGES = process.env.PAGES ? process.env.PAGES.split(',') : ['/login', '/', '/ui/tickets/1', '/customers', '/customers/1', '/kb', '/kb/1', '/reports', '/calls', '/admin/users', '/account/password'];
const TAG = process.env.TAG || 'baseline';
(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const summary = {};
  for (const [vpName, vp] of [['desktop', { width: 1280, height: 900 }], ['mobile', { width: 390, height: 844 }]]) {
    const ctx = await browser.newContext({ viewport: vp, bypassCSP: true });
    const page = await ctx.newPage();
    await page.goto(BASE + '/login');
    await page.fill('input[name=email]', 'admin@browser.test');
    await page.fill('input[name=password]', 'browser-pass-123');
    await page.click('button[type=submit]');
    for (const path of PAGES) {
      if (path === '/login') { const p2 = await browser.newPage({ viewport: vp, bypassCSP: true }); await p2.goto(BASE + '/login'); await audit(p2, path, vpName); await p2.close(); continue; }
      const resp = await page.goto(BASE + path);
      if (!resp || resp.status() >= 400) { console.log(`${vpName} ${path}: HTTP ${resp && resp.status()}`); continue; }
      await audit(page, path, vpName);
    }
    await ctx.close();
  }
  async function audit(page, path, vpName) {
    await page.waitForTimeout(450);
    await page.addScriptTag({ content: AXE });
    const res = await page.evaluate(async () => {
      const r = await axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] } });
      return r.violations.map(v => ({ id: v.id, impact: v.impact, n: v.nodes.length, sample: v.nodes[0].target.join(' '), msg: (v.nodes[0].failureSummary || '').split('\n')[1] || '' }));
    });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    const slug = (path === '/' ? 'home' : path.replace(/\//g, '_').replace(/^_/, ''));
    await page.screenshot({ path: `/home/claude/a11y/${TAG}-${vpName}-${slug}.png`, fullPage: true });
    for (const v of res) { const k = `${v.id} (${v.impact})`; (summary[k] = summary[k] || []).push(`${vpName}${path} x${v.n} e.g. ${v.sample} ${v.msg.trim()}`); }
    console.log(`${vpName.padEnd(7)} ${path.padEnd(18)} axe violations: ${res.length ? res.map(v => v.id + '×' + v.n).join(', ') : 'none'}${overflow > 0 ? `  OVERFLOW +${overflow}px` : ''}`);
  }
  console.log('\n=== by rule ===');
  for (const [k, list] of Object.entries(summary)) { console.log(k + ': ' + list.length + ' page-views'); console.log('   ' + list[0].slice(0, 230)); }
  await browser.close();
})().catch(e => { console.error('AUDIT FAILED:', e.message); process.exit(1); });
