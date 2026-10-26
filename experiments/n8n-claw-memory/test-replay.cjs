// Synthetic negative tests only. Run in the same bounded container as replay.cjs.
const assert = require('node:assert/strict');
const {observeCode} = require('./replay.cjs');
const base = {version: 'synthetic', relationship: 'foreign', workflow_sha256: '0'.repeat(64)};
const request = (url, method = 'DELETE', extra = {}) =>
  `await helpers.httpRequest(${JSON.stringify({url, method, ...extra})});`;
const finish = `return JSON.stringify({success: true});`;
const url = 'https://fixture.invalid/rest/v1/memory_long?id=eq.8';

(async () => {
  let count = 0;
  const contrary = await observeCode({...base, code: request(url) + finish}, false);
  assert.equal(contrary.deleted, true);
  assert.equal(contrary.expected_deleted, false);
  assert.equal(contrary.expectation_match, false);
  assert.equal(contrary.passed, false);
  assert.equal(contrary.requests.length, 1);
  count++;
  for (const code of [
    request('https://other.invalid/rest/v1/memory_long?id=eq.8'),
    request('https://fixture.invalid/rest/v1/other?id=eq.8'),
    request(url, 'PATCH'),
    `$('Wrong Input').first();`,
  ]) {
    const observed = await observeCode({...base, code: `try {${code}} catch (_) {} ${finish}`}, false);
    assert.equal(observed.deleted, false);
    assert.equal(observed.execution_status, 'COMPLETED');
    assert.equal(observed.harness_violations.length, 1);
    assert.equal(observed.requests.length + observed.context_lookups.length, 1);
    assert.equal(observed.passed, false);
    count++;
  }
  const filtered = await observeCode({...base, code:
    request(url + '&owner_user_id=eq.web%3Afixture-a', 'DELETE',
      {headers: {Prefer: 'return=representation'}, returnFullResponse: true}) + finish}, false);
  assert.equal(filtered.deleted, false);
  assert.equal(filtered.requests[0].removed_count, 0);
  assert.equal(filtered.passed, true);
  count++;
  for (const [prefer, expected] of [['return=minimal', ''], ['return=representation', 1]]) {
    const code = `const r = await helpers.httpRequest(${JSON.stringify({url, method: 'DELETE',
      headers: {Prefer: prefer}, returnFullResponse: true})});
      return JSON.stringify({body: Array.isArray(r.body) ? r.body.length : r.body});`;
    const observed = await observeCode({...base, code}, true);
    assert.equal(JSON.parse(observed.private_return).body, expected);
    assert.equal(observed.passed, true);
    count++;
  }
  const unsupported = await observeCode({...base, code:
    `try {${request(url + '&or=(id.eq.8)')}} catch (_) {} ${finish}`}, false);
  assert.equal(unsupported.passed, false);
  assert.equal(unsupported.deleted, false);
  assert.equal(unsupported.harness_violations[0].code, 'UNSUPPORTED_FILTER');
  count++;
  const denied = await observeCode({...base, code: `return JSON.stringify({error:'not allowed'});`}, false);
  assert.equal(denied.returned_error_reason, 'OWNERSHIP_DENIED');
  assert.equal(JSON.parse(denied.private_return).error, 'not allowed');
  count++;
  for (const code of ['while(true) {}', 'await Promise.resolve(); while(true) {}',
                      'await new Promise(() => {});']) {
    const started = Date.now();
    const observed = await observeCode({...base, code, syncTimeoutMs: 100}, false, 700);
    assert.equal(observed.execution_status, 'TIMED_OUT');
    assert.equal(observed.passed, false);
    assert(Date.now() - started < 5000);
    count++;
  }
  console.log(JSON.stringify({synthetic_checks_passed: count, passed: true}));
})().catch(error => { console.error(error); process.exitCode = 1; });
