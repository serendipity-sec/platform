// Team-written bounded receiver; Docker remains the isolation boundary, not node:vm.
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const {Worker, isMainThread, parentPort, workerData} = require('node:worker_threads');
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const versions = ['before-feature', 'feature', 'before-fix', 'first-fix', 'reverted', 'before-september', 'september'];

function initialObservation({version, relationship, workflow_sha256, tool_present = true}) {
  const row = {id: 8, scope: 'personal', owner_user_id:
    relationship === 'own' ? 'web:fixture-a' : 'web:fixture-b', entity_name: null};
  return {version, relationship, workflow_sha256, tool_present,
    initial_state: [row], final_state: [structuredClone(row)], deleted: false,
    requests: [], context_lookups: [], harness_violations: [], execution_status: 'RUNNING'};
}

async function executeCase(input) {
  const observation = initialObservation(input);
  const caller = {qualifiedUserId: 'web:fixture-a', sessionId: 'fixture-session', source: 'web'};
  const progress = () => parentPort?.postMessage({type: 'progress', observation});
  const violation = code => {
    observation.harness_violations.push({code, request_index: observation.requests.length - 1});
    progress();
    throw new Error(code);
  };
  const helpers = {httpRequest: async request => {
    // Log the attempt before validation; source try/catch cannot erase a harness failure.
    const attempt = {method: String(request?.method || 'GET').toUpperCase(),
      url_sha256: hash(String(request?.url))};
    observation.requests.push(attempt);
    progress();
    let url;
    try { url = new URL(String(request.url).replace('{{SUPABASE_URL}}', 'https://fixture.invalid')); }
    catch { violation('UNSUPPORTED_URL'); }
    attempt.host = url.hostname;
    attempt.path = url.pathname + url.search;
    if (url.origin !== 'https://fixture.invalid' || url.username || url.password || url.hash)
      violation('UNSUPPORTED_HOST');
    const params = [...url.searchParams];
    if (new Set(params.map(([key]) => key)).size !== params.length)
      violation('DUPLICATE_QUERY_PARAMETER');
    const headers = Object.fromEntries(Object.entries(request.headers || {}).map(([k,v]) => [k.toLowerCase(), v]));
    const prefer = headers.prefer || 'return=minimal';
    if (!['return=minimal', 'return=representation'].includes(prefer)) violation('UNSUPPORTED_PREFER');
    const respond = (body, statusCode = 200) => request.returnFullResponse
      ? {body, statusCode, headers: {'preference-applied': prefer}} : body;
    if (url.pathname === '/rest/v1/claw_agents') {
      if (attempt.method !== 'GET' || url.searchParams.get('key') !== 'eq.memory_admins'
          || url.searchParams.get('select') !== 'content' || params.length !== 2)
        violation('UNSUPPORTED_ADMIN_QUERY');
      progress();
      return respond([{content: input.relationship === 'admin' ? caller.qualifiedUserId : ''}]);
    }
    if (url.pathname === '/rest/v1/tool_audit_log') {
      if (attempt.method !== 'POST' || params.length) violation('UNSUPPORTED_AUDIT_REQUEST');
      progress();
      return respond(prefer === 'return=representation' ? [] : '', 201);
    }
    if (url.pathname !== '/rest/v1/memory_long') violation('UNSUPPORTED_PATH');
    if (!['GET', 'DELETE'].includes(attempt.method)) violation('UNSUPPORTED_METHOD');
    if (!url.searchParams.has('id')) violation('UNBOUNDED_MEMORY_QUERY');
    const columns = new Set(['id', 'scope', 'owner_user_id', 'entity_name']);
    const filters = [];
    let select = null;
    for (const [key, value] of params) {
      if (key === 'select') {
        select = value.split(',');
        if (select.some(column => !columns.has(column))) violation('UNSUPPORTED_SELECT');
      } else {
        if (!columns.has(key)) violation('UNSUPPORTED_FILTER');
        if (value.startsWith('eq.')) filters.push(row => String(row[key]) === value.slice(3));
        else if (value === 'is.null') filters.push(row => row[key] === null);
        else violation('UNSUPPORTED_FILTER');
      }
    }
    const matches = row => filters.every(filter => filter(row));
    const rows = observation.final_state.filter(matches);
    const projected = rows.map(row => select
      ? Object.fromEntries(select.map(key => [key, row[key]])) : structuredClone(row));
    if (attempt.method === 'GET') { progress(); return respond(projected); }
    observation.final_state = observation.final_state.filter(row => !matches(row));
    observation.deleted = observation.final_state.length === 0;
    attempt.removed_count = rows.length;
    progress();
    return respond(prefer === 'return=representation' ? projected : '',
      prefer === 'return=representation' ? 200 : 204);
  }};
  const lookup = name => {
    observation.context_lookups.push(String(name));
    if (name !== 'Merge Input') violation('UNSUPPORTED_CONTEXT_LOOKUP');
    return {first: () => ({json: structuredClone(caller)})};
  };
  try {
    // Invoke inside the timed VM. Worker deadline also covers async/microtask loops.
    const result = await vm.runInNewContext('(async function(){' + input.code + '\n})()',
      {query: JSON.stringify({id: 8}), helpers, $: lookup, $execution: {id: 'fixture'}},
      {timeout: input.syncTimeoutMs || 1000});
    observation.private_return = String(result);
    const parsed = JSON.parse(result);
    observation.returned_error = Boolean(parsed.error);
    observation.returned_error_reason = !parsed.error ? null
      : /nicht erlaubt|not owner|not allowed|denied/i.test(String(parsed.error)) ? 'OWNERSHIP_DENIED'
      : /not found/i.test(String(parsed.error)) ? 'MEMORY_NOT_FOUND' : 'UNCLASSIFIED_TOOL_ERROR';
    observation.returned_error_sha256 = parsed.error ? hash(String(parsed.error)) : null;
    observation.execution_status = 'COMPLETED';
  } catch (error) {
    observation.execution_status = error.code === 'ERR_SCRIPT_EXECUTION_TIMEOUT' ? 'TIMED_OUT' : 'FAILED';
    observation.execution_error = error.code === 'ERR_SCRIPT_EXECUTION_TIMEOUT'
      ? 'SCRIPT_TIMEOUT' : 'TOOL_EXECUTION_OR_RESULT_ERROR';
    observation.private_error = String(error.message);
  }
  return observation;
}

function assess(observation, expectedDeleted) {
  const expectedMatch = observation.deleted === expectedDeleted;
  return {...observation, expected_deleted: expectedDeleted, expectation_match: expectedMatch,
    passed: observation.execution_status === 'COMPLETED'
      && observation.harness_violations.length === 0 && expectedMatch};
}

async function observeCode(input, expectedDeleted, timeoutMs = 2500) {
  return new Promise(resolve => {
    const worker = new Worker(__filename, {workerData: input,
      resourceLimits: {maxOldGenerationSizeMb: 64}});
    let latest = initialObservation(input), settled = false;
    const finish = async result => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      await worker.terminate();
      resolve(assess(result, expectedDeleted));
    };
    const timer = setTimeout(() => finish({...latest, execution_status: 'TIMED_OUT',
      execution_error: 'WORKER_DEADLINE'}), timeoutMs);
    worker.on('message', message => {
      latest = message.observation;
      if (message.type === 'result') finish(latest);
    });
    worker.on('error', () => finish({...latest, execution_status: 'FAILED', execution_error: 'WORKER_ERROR'}));
    worker.on('exit', code => {
      if (!settled) finish({...latest, execution_status: 'FAILED', execution_error: 'WORKER_EXIT', worker_exit: code});
    });
  });
}

async function observe(version, relationship) {
  const raw = fs.readFileSync(`/sources/${version}/workflows/n8n-claw-agent.json`);
  const workflow = JSON.parse(raw);
  const node = workflow.nodes.find(item => item.id === 'tool-memory-delete');
  const input = {version, relationship, workflow_sha256: hash(raw), tool_present: Boolean(node)};
  if (!node) return {...initialObservation(input), execution_status: 'NOT_RUN',
    observation: 'TOOL_ABSENT', scope: 'SELECTED_NODE_ONLY', expected_deleted: null,
    expectation_match: version === 'before-feature', passed: version === 'before-feature'};
  const connected = workflow.connections[node.name]?.ai_tool?.flat().some(edge => edge.type === 'ai_tool');
  const guarded = ['first-fix', 'before-september', 'september'].includes(version);
  const observation = await observeCode({...input, code: node.parameters.jsCode},
    !(guarded && relationship === 'foreign'));
  if (!connected) {
    observation.harness_violations.push({code: 'AI_TOOL_CONNECTION_MISSING'});
    observation.passed = false;
  }
  return observation;
}

if (!isMainThread) {
  // An unresolved Promise alone does not keep a Node worker alive. Keep the port
  // referenced so only a completed result or the parent's deadline finishes a case.
  parentPort.on('message', () => {});
  executeCase(workerData).then(observation => parentPort.postMessage({type: 'result', observation}));
} else if (require.main === module) {
  (async () => {
    const observations = [];
    for (const version of versions) for (const relationship of ['foreign', 'own', 'admin'])
      observations.push(await observe(version, relationship));
    const passed = observations.every(item => item.passed);
    console.log(JSON.stringify({schema: 'bounded-memory-observation-v2',
      runtime: {node: process.version, network: 'none'},
      scope: {original_tool_js: true, http_receiver: 'SYNTHETIC_SUBSET', caller_context: 'SYNTHETIC',
        full_workflow_run: false, llm_run: false, real_database: false, historical_deployment_reproduced: false},
      observations, passed}, null, 2));
    if (!passed) process.exitCode = 1;
  })().catch(() => { console.error('BOUNDED_REPLAY_FAILED'); process.exitCode = 1; });
}
module.exports = {observeCode};
