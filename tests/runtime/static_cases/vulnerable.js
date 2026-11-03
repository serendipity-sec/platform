// Team-written synthetic tool. The model-written id reaches a DELETE with no owner check.
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  await helpers.httpRequest({ method: 'DELETE', url: `https://example.invalid/notes?id=eq.${input.id}` });
  return 'deleted';
}
