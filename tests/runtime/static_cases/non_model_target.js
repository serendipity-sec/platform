async function __n8n_tool(query) {
  const input = JSON.parse(query);
  return await helpers.httpRequest({ method: 'PATCH', url: 'https://example.invalid/fixed-note', body: input.text });
}
