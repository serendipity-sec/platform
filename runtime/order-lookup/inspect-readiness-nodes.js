// Inspect the selected official engine's installed descriptions. No workflow runs.
const root = '/usr/local/lib/node_modules/n8n/node_modules/n8n-nodes-base/dist/nodes/';
const specs = [
  ['Set', 'Set/Set.node.js'], ['If', 'If/If.node.js'], ['Merge', 'Merge/Merge.node.js'],
  ['ExecuteWorkflow', 'ExecuteWorkflow/ExecuteWorkflow/ExecuteWorkflow.node.js'],
  ['ExecuteWorkflowTrigger', 'ExecuteWorkflow/ExecuteWorkflowTrigger/ExecuteWorkflowTrigger.node.js'],
  ['SplitOut', 'Transform/SplitOut/SplitOut.node.js'], ['ManualTrigger', 'ManualTrigger/ManualTrigger.node.js'],
];
console.log(JSON.stringify(specs.map(([name, file]) => {
  const instance = new (require(root + file)[name])();
  const version = instance.description.defaultVersion ?? (Array.isArray(instance.description.version) ? Math.max(...instance.description.version) : instance.description.version);
  const selected = instance.nodeVersions ? instance.nodeVersions[version] : instance;
  return { name, type: instance.description.name, version,
    versions: instance.nodeVersions ? Object.keys(instance.nodeVersions).map(Number) : instance.description.version,
    properties: selected.description.properties.map(p => ({name:p.name, default:p.default})) };
}), null, 2));
