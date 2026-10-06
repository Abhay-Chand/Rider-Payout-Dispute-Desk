const { JSDOM } = require('jsdom');
const BASE = 'http://localhost:8000', sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (fn, what, ms = 8000) => { const e = Date.now() + ms; while (Date.now() < e) { const v = fn(); if (v) return v; await sleep(50); } throw new Error('timed out: ' + what); };
(async () => {
  const dom = new JSDOM(await (await fetch(BASE + '/ops')).text(), { url: BASE + '/ops', runScripts: 'dangerously', pretendToBeVisual: true,
    beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, BASE), o); } });
  const w = dom.window, d = w.document, $ = s => d.querySelector(s);
  await until(() => !$('#signin').hidden, 'signin');
  $('#signin-name').value = 'Meera'; $('#signin-token').value = 'devtoken';
  $('#signin-form').dispatchEvent(new w.Event('submit', { cancelable: true }));
  await until(() => !$('#app').hidden && d.querySelectorAll('#list .row').length, 'app');
  [...d.querySelectorAll('.tabs button')].find(b => b.dataset.tab === 'riders').click();
  await until(() => [...d.querySelectorAll('#list .row')].some(r => /R018/.test(r.textContent)), 'R018');
  [...d.querySelectorAll('#list .row')].find(r => /R018/.test(r.textContent)).click();
  await until(() => /What the agent did/.test($('#case-inner').textContent), 'case');
  const stray = [...$('#case-inner').childNodes].filter(n => n.nodeType === 3 && n.textContent.trim() === 'null').length;
  console.log(stray ? 'FAIL rider view still has stray "null"' : 'PASS rider with no payouts: no stray "null"');
  [...d.querySelectorAll('.views button')].find(b => b.dataset.view === 'insights').click();
  await until(() => $('.headlines'), 'insights');
  const stray2 = [...$('#insights-inner').childNodes].filter(n => n.nodeType === 3 && /null/.test(n.textContent)).length;
  console.log(stray2 ? 'FAIL insights has stray "null"' : 'PASS insights: no stray "null"');
  process.exit(stray || stray2 ? 1 : 0);
})().catch(e => { console.error('ERROR', e.message); process.exit(1); });
