// Team-written synthetic tool. The owner check sits inside a plain block, as in some tools.
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  {
    const rows = await helpers.httpRequest({ method: 'GET', url: `https://example.invalid/notes?id=eq.${input.id}` });
    const caller = $('Caller').first().json.userId;
    const own = rows[0].owner_user_id === caller;
    if (!own) return 'denied';
  }
  await helpers.httpRequest({ method: 'DELETE', url: `https://example.invalid/notes?id=eq.${input.id}` });
  return 'deleted';
}
