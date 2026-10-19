// Team-written synthetic receiver. Original code is run only inside a networkless container.
// vm is not the isolation boundary; Docker resource, filesystem and network controls are required.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const versions = ['before-feature', 'feature', 'before-fix', 'first-fix', 'reverted', 'before-september', 'september'];

async function observe(version, relationship) {
  const raw = fs.readFileSync(`/sources/${version}/workflows/n8n-claw-agent.json`);
  const workflow = JSON.parse(raw);
  const node = workflow.nodes.find(item => item.id === 'tool-memory-delete');
  if (!node) {
    assert.equal(version, 'before-feature');
    return {version, relationship, workflow_sha256: hash(raw), tool_present: false,
      deleted: false, observation: 'TOOL_ABSENT', passed: true};
  }
  assert(workflow.connections[node.name].ai_tool.flat().some(edge => edge.type === 'ai_tool'));
  const caller = {qualifiedUserId: 'web:fixture-a', sessionId: 'fixture-session', source: 'web'};
  const initial = {id: 8, scope: 'personal', owner_user_id:
    relationship === 'own' ? caller.qualifiedUserId : 'web:fixture-b', entity_name: null};
  let rows = [structuredClone(initial)];
  const requests = [];
  const helpers = {httpRequest: async request => {
    const url = new URL(request.url.replace('{{SUPABASE_URL}}', 'https://fixture.invalid'));
    assert.equal(url.hostname, 'fixture.invalid');
    const method = request.method || 'GET';
    requests.push({method, path: url.pathname + url.search});
    if (url.pathname === '/rest/v1/claw_agents') {
      assert.equal(method, 'GET');
      return [{content: relationship === 'admin' ? caller.qualifiedUserId : ''}];
    }
    if (url.pathname === '/rest/v1/tool_audit_log' && method === 'POST') return {};
    assert.equal(url.pathname, '/rest/v1/memory_long');
    assert.equal(url.searchParams.get('id'), 'eq.8');
    if (method === 'GET') return structuredClone(rows);
    assert.equal(method, 'DELETE');
    const removed = rows;
    rows = [];
    return request.returnFullResponse ? {body: removed} : {};
  }};
  const source = '(async function(query,helpers,$,$execution){' + node.parameters.jsCode + '\n})';
  const execute = vm.runInNewContext(source, {}, {timeout: 1000});
  const result = JSON.parse(await execute(JSON.stringify({id: 8}), helpers, name => {
    assert.equal(name, 'Merge Input');
    return {first: () => ({json: caller})};
  }, {id: 'fixture'}));
  const deleted = rows.length === 0;
  const guarded = ['first-fix', 'before-september', 'september'].includes(version);
  assert.equal(deleted, !(guarded && relationship === 'foreign'));
  return {version, relationship, workflow_sha256: hash(raw), tool_present: true,
    initial_state: [initial], final_state: rows, deleted, returned_error: !!result.error,
    requests, passed: true};
}

(async () => {
  const observations = [];
  for (const version of versions) {
    for (const relationship of ['foreign', 'own', 'admin']) {
      observations.push(await observe(version, relationship));
    }
  }
  console.log(JSON.stringify({schema: 'bounded-memory-observation-v1',
    runtime: {node: process.version, network: 'none'},
    scope: {original_tool_js: true, http_receiver: 'SYNTHETIC', caller_context: 'SYNTHETIC',
      full_workflow_run: false, llm_run: false, real_database: false, historical_deployment_reproduced: false},
    observations, passed: observations.every(item => item.passed)}, null, 2));
})().catch(() => { console.error('BOUNDED_REPLAY_FAILED'); process.exitCode = 1; });
