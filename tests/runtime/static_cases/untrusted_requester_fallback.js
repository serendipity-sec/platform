// The model can supply the fallback even if the primary node sounds trusted.
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const rows = await helpers.httpRequest({ method: 'GET', url: `https://example.invalid/notes?id=${input.id}` });
  const requester = $('Caller').first().json.userId || input.owner_id;
  if (rows[0].owner_user_id !== requester) return 'denied';
  await helpers.httpRequest({ method: 'DELETE', url: `https://example.invalid/notes?id=${input.id}` });
}
