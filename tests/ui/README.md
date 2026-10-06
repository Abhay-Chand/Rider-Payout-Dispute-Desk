Drives the real ops page in a headless DOM (jsdom) against a running service: sign-in, approve, reject, resolve,
the timeline, and shadow mode. Not part of CI (it needs a seeded, running stack).

```bash
cd tests/ui && npm install
# live mode, OPS_TOKEN=devtoken, fresh system:
bash seed.sh && node ui_test.js
# shadow mode (AUTO_PAY_MODE=shadow AUTO_PAY_DAILY_BUDGET_INR=100), fresh system: seed R003 and R005 claims, then
node ui_shadow.js
```

Insights view (any populated system, e.g. after `python evals/run.py http://localhost:8000`): `node ui_insights.js`
Empty state (fresh system): `node ui_empty.js`
