// An admin variable name cannot authenticate the value supplied by the model.
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const rows = await helpers.httpRequest({ method: 'GET', url: `https://example.invalid/notes?id=${input.id}` });
  const requester = $('Caller').first().json.userId;
  const isAdmin = input.isAdmin;
  if (rows[0].owner_user_id !== requester && !isAdmin) return 'denied';
  await helpers.httpRequest({ method: 'DELETE', url: `https://example.invalid/notes?id=${input.id}` });
}
