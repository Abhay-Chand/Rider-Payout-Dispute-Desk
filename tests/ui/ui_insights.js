const { JSDOM } = require('jsdom');
const BASE = 'http://localhost:8000', sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (fn, what, ms = 8000) => { const e = Date.now() + ms; while (Date.now() < e) { const v = fn(); if (v) return v; await sleep(50); } throw new Error('timed out: ' + what); };
let bad = 0; const check = (ok, m) => { console.log((ok ? 'PASS ' : 'FAIL ') + m); if (!ok) bad++; };
(async () => {
  const api = await (await fetch(BASE + '/ops/api/insights', { headers: { Authorization: 'Bearer devtoken' } })).json();
  const dom = new JSDOM(await (await fetch(BASE + '/ops')).text(), { url: BASE + '/ops', runScripts: 'dangerously', pretendToBeVisual: true,
    beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, BASE), o); w.HTMLDialogElement.prototype.showModal = function () { this.open = true; }; } });
  const w = dom.window, d = w.document, $ = s => d.querySelector(s), t = s => ($(s) || {}).textContent || '';
  await until(() => !$('#signin').hidden, 'signin');
  $('#signin-name').value = 'Meera'; $('#signin-token').value = 'devtoken';
  $('#signin-form').dispatchEvent(new w.Event('submit', { cancelable: true }));
  await until(() => !$('#app').hidden && d.querySelectorAll('#list .row').length, 'app');
  [...d.querySelectorAll('.views button')].find(b => b.dataset.view === 'insights').click();
  await until(() => $('.headlines'), 'insights');
  check($('.main').hidden && !$('#insights').hidden, 'Insights view replaces the work queue');
  const lines = [...d.querySelectorAll('.headlines li')].map(l => l.textContent);
  lines.forEach(l => console.log('     · ' + l));
  check(lines[0].startsWith(`${api.riders.agent_only} of ${api.riders.total} riders`), 'Headline 1 matches API riders numbers');
  check(lines[1].includes('₹' + (api.money.paid_automatically.amount + api.money.paid_after_approval.amount).toLocaleString('en-IN') + ' paid') &&
        lines[1].includes('₹' + api.money.waiting_for_approval.amount.toLocaleString('en-IN') + ' waiting'), 'Headline 2 matches paid and waiting amounts');
  check(lines[2].includes(`${Math.round(api.disputes.rider_right_pct)}% of disputes`), 'Headline 3 matches rider-right rate');
  const segs = [...d.querySelectorAll('.moneybar span')];
  const widths = segs.map(s => parseFloat(s.style.width));
  check(segs.length === 2 && Math.abs(widths.reduce((a, b) => a + b, 0) - 100) < 0.1, `Money bar has 2 segments summing to 100% (${widths.join('% + ')}%)`);
  const panels = [...d.querySelectorAll('#insights .panel')].map(p => p.querySelector('h3').textContent);
  console.log('     panels: ' + panels.join(' | '));
  check(panels.length === 7, 'All 7 sections render');
  const found = [...d.querySelectorAll('#insights .panel')][1];
  check(/Trips not paid at all/.test(found.textContent) && found.querySelectorAll('tbody tr').length === api.underpayments_found.length && found.querySelector('.panel-note'),
        'Underpayment table: one row per problem, note sits inside the same panel');
  const claims = [...d.querySelectorAll('#insights .panel')][2];
  const inc = [...claims.querySelectorAll('tr')].find(r => /incentive/.test(r.textContent));
  check(inc && /4 of 8 right/.test(inc.textContent), 'Claims table shows incentive claims: 4 of 8 right');
  const outcomes = [...d.querySelectorAll('#insights .panel')][3];
  check(outcomes.querySelectorAll('tbody tr').length === api.disputes.outcomes.filter(o => o.count).length && outcomes.querySelector('.panel-note'),
        'Outcomes table rows match non-zero outcomes; note in the right panel');
  check(/LLM/.test(t('#insights')) && /duplicate WhatsApp deliveries/.test(t('#insights')), 'Health facts render');
  [...d.querySelectorAll('.views button')].find(b => b.dataset.view === 'queue').click();
  check(!$('.main').hidden && $('#insights').hidden, 'Switching back to Work queue works');
  console.log(bad ? `\n${bad} FAILED` : '\nALL INSIGHTS UI CHECKS PASSED'); process.exit(bad ? 1 : 0);
})().catch(e => { console.error('ERROR', e.message); process.exit(1); });
