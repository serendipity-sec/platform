// Team-written synthetic tool. The write runs only inside the branch an owner reaches.
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const rows = await helpers.httpRequest({ method: 'GET', url: `https://example.invalid/notes?id=eq.${input.id}` });
  const caller = $('Caller').first().json.userId;
  if (rows[0].owner_user_id === caller) {
    await helpers.httpRequest({ method: 'DELETE', url: `https://example.invalid/notes?id=eq.${input.id}` });
    return 'deleted';
  }
  return 'denied';
}
