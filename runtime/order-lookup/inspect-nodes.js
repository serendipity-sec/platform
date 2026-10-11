// Read installed node descriptions only; no workflow or node execution.
const root = '/usr/local/lib/node_modules/n8n/node_modules/n8n-nodes-base/dist/nodes/';
const specs = [
  ['Webhook', 'Webhook/Webhook.node.js'],
  ['HttpRequest', 'HttpRequest/HttpRequest.node.js'],
  ['RespondToWebhook', 'RespondToWebhook/RespondToWebhook.node.js'],
];
console.log(JSON.stringify(specs.map(([name, file]) => {
  const instance = new (require(root + file)[name])();
  return {
    name, type: instance.description.name,
    versions: instance.nodeVersions ? Object.keys(instance.nodeVersions).map(Number) : instance.description.version,
    defaultVersion: instance.description.defaultVersion,
  };
}), null, 2));
