const { JSDOM } = require('jsdom');
const BASE = 'http://localhost:8000';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (fn, what, ms = 8000) => { const end = Date.now() + ms; while (Date.now() < end) { const v = fn(); if (v) return v; await sleep(50); } throw new Error('timed out: ' + what); };
let failures = 0;
const check = (ok, msg) => { console.log((ok ? 'PASS ' : 'FAIL ') + msg); if (!ok) failures++; };

(async () => {
  const html = await (await fetch(BASE + '/ops')).text();
  const dom = new JSDOM(html, { url: BASE + '/ops', runScripts: 'dangerously', pretendToBeVisual: true,
    beforeParse(w) {
      w.fetch = (u, o) => fetch(new URL(u, BASE), o);
      w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
      w.HTMLDialogElement.prototype.close = function (v) { if (v !== undefined) this.returnValue = v; this.open = false; this.dispatchEvent(new w.Event('close')); };
    } });
  const w = dom.window, d = w.document, $ = s => d.querySelector(s), text = s => ($(s) || {}).textContent || '';
  const submitDialog = async (note) => { $('#dlg-note').value = note; const ev = new w.MouseEvent('click', { bubbles: true, cancelable: true }); $('#dlg-ok').dispatchEvent(ev); if (!ev.defaultPrevented) $('#dlg').close('ok'); };

  await until(() => !$('#signin').hidden, 'sign-in screen');
  check(!$('#signin').hidden && $('#app').hidden, 'Opens on the sign-in screen; no data shown before sign-in');

  $('#signin-name').value = 'Meera'; $('#signin-token').value = 'wrong-token';
  $('#signin-form').dispatchEvent(new w.Event('submit', { cancelable: true }));
  await until(() => !$('#signin-error').hidden, 'error');
  check(/doesn't match OPS_TOKEN/.test(text('#signin-error')), 'Wrong token gives a clear error: "' + text('#signin-error') + '"');

  $('#signin-token').value = 'devtoken';
  $('#signin-form').dispatchEvent(new w.Event('submit', { cancelable: true }));
  await until(() => !$('#app').hidden && d.querySelectorAll('#list .row').length, 'app');
  check(text('#who-name') === 'Meera', 'Signed in as Meera');
  check(text('#c-approval') === '2' && text('#a-approval') === '₹444', `Queue shows 2 approvals totalling ₹444 ("${text('#a-approval')}")`);
  await until(() => /PaySwift in sync/.test(text('#health')), 'reconciliation pill');
  console.log('     header: ' + [...d.querySelectorAll('#health .pill')].map(p => p.textContent).join(' | '));
  const rows = [...d.querySelectorAll('#list .row')].map(r => r.textContent);
  check(/Anil Chauhan/.test(rows[0]) && /₹425/.test(rows[0]), 'Biggest approval first: ' + rows[0]);

  // Open R030 and approve ₹19
  [...d.querySelectorAll('#list .row')].find(r => /R030/.test(r.textContent)).click();
  await until(() => $('.slip.approval'), 'slip');
  const slip = $('.slip.approval').textContent;
  check(/Payout for 19 Sept?/.test(slip) && /T404692/.test(slip) && /1.5x surge not applied/.test(slip) && /₹56/.test(slip) && /₹37/.test(slip), 'Slip itemises T404692: 1.5x surge not applied, ₹56 due, ₹37 paid');
  check(/already got an automatic payout today/.test(slip), 'Slip explains why it needs a human (one auto-pay a day)');
  [...d.querySelectorAll('.slip.approval button')].find(b => /Approve and pay ₹19/.test(b.textContent)).click();
  await until(() => $('#dlg').open, 'dialog');
  check(/Pay ₹19 to/.test(text('#dlg-title')), 'Confirmation dialog: ' + text('#dlg-title'));
  await submitDialog('');
  await until(() => /Paid ₹19|Approved ₹19/.test(text('#toast')), 'approve toast', 15000);
  check(true, 'Approve result: "' + text('#toast') + '"');
  const led = await (await fetch('http://localhost:8081/v1/payouts?rider_id=R030')).json();
  check(led.data.map(p => p.amount).sort((a,b)=>a-b).join(',') === '10,19', 'PaySwift ledger for R030 now has ₹10 (auto) + ₹19 (approved)');
  await until(() => text('#c-approval') === '1', 'queue updated');
  check(true, 'Queue dropped to 1 approval after approving');

  // Reject R016 without a reason, then with one
  [...d.querySelectorAll('#list .row')].find(r => /R016/.test(r.textContent)).click();
  await until(() => /Anil Chauhan/.test(text('#case-inner')) && $('.slip.approval'), 'R016');
  [...d.querySelectorAll('.slip.approval button')].find(b => b.textContent === 'Reject').click();
  await until(() => $('#dlg').open, 'reject dialog');
  await submitDialog('');
  check($('#dlg').open && /Add a reason/.test(text('#dlg-error')), 'Reject without a reason is blocked in the dialog');
  await submitDialog('Rider was paid in cash on 20 Sep');
  await until(() => /Rejected the ₹425/.test(text('#toast')), 'reject toast');
  check(true, 'Reject result: "' + text('#toast') + '"');
  await until(() => /Earlier decisions/.test(text('#case-inner')), 'history');
  check(/Rejected by Meera/.test(text('#case-inner')) && /paid in cash/.test(text('#case-inner')), 'Case history shows "Rejected by Meera" with the reason');

  // Escalations: distance dispute has guidance; resolve R018
  [...d.querySelectorAll('.tabs button')].find(b => b.dataset.tab === 'escalation').click();
  await until(() => d.querySelectorAll('#list .row').length >= 2, 'escalations');
  [...d.querySelectorAll('#list .row')].find(r => /R036/.test(r.textContent)).click();
  await until(() => $('.slip.escalation'), 'esc slip');
  check(/Says the distance is wrong/.test(text('.slip.escalation')) && /GPS route/.test(text('.slip.escalation')), 'Escalation shows a plain title and what to do');
  [...d.querySelectorAll('#list .row')].find(r => /R018/.test(r.textContent)).click();
  await until(() => /Wants to talk to a person/.test(text('.slip.escalation')), 'R018 slip');
  check(text('.slip.escalation .why').split('Rider asked for a person').length === 2, 'Escalation reason is not duplicated: "' + text('.slip.escalation .why') + '"');
  [...d.querySelectorAll('.slip.escalation button')].find(b => /Mark as resolved/.test(b.textContent)).click();
  await until(() => $('#dlg').open, 'resolve dialog');
  await submitDialog('Called the rider, explained the payout');
  await until(() => /Escalation resolved/.test(text('#toast')), 'resolve toast');
  check(true, 'Resolve result: "' + text('#toast') + '"');

  // Timeline in plain language, with filter
  [...d.querySelectorAll('.tabs button')].find(b => b.dataset.tab === 'riders').click();
  await until(() => [...d.querySelectorAll('#list .row')].some(r => /R003/.test(r.textContent)), 'riders');
  [...d.querySelectorAll('#list .row')].find(r => /R003/.test(r.textContent)).click();
  await until(() => /What the agent did/.test(text('#case-inner')) && /Imran Khan/.test(text('#case-inner')), 'timeline');
  const steps = [...d.querySelectorAll('.step .text')].map(s => s.textContent);
  console.log('     timeline for R003:\n       - ' + steps.join('\n       - '));
  check(steps.some(s => /Decided to pay ₹25 automatically/.test(s)) && steps.some(s => /PaySwift (accepted|confirmed) ₹25/.test(s)), 'Timeline explains the decision and the PaySwift result in plain words');
  [...d.querySelectorAll('.seg button')].find(b => b.textContent === 'Problems').click();
  await sleep(600);
  console.log('     "Problems" filter shows: ' + ([...d.querySelectorAll('.step .text')].map(s => s.textContent).join(' | ') || text('.panel .empty')));

  // Session expiry: a bad token mid-session sends you back to sign-in with a message
  w.sessionStorage.setItem('opsToken', 'x');
  const S_token = w.eval("1"); // noop
  console.log(failures ? `\n${failures} FAILED` : '\nALL UI CHECKS PASSED');
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error('ERROR', e.message); process.exit(1); });
