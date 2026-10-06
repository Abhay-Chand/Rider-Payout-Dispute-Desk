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
  await until(() => !$('#app').hidden, 'app'); await sleep(300);
  [...d.querySelectorAll('.views button')].find(b => b.dataset.view === 'insights').click();
  await until(() => /No conversations yet/.test($('#insights').textContent), 'empty');
  console.log('PASS Empty system shows: "' + $('#insights .empty').textContent + '"'); process.exit(0);
})().catch(e => { console.error('FAIL', e.message); process.exit(1); });
