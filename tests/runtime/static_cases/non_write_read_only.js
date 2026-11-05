async function __n8n_tool(query) {
  const input = JSON.parse(query);
  return await helpers.httpRequest({ method: 'GET', url: `https://example.invalid/notes?id=${input.id}` });
}
