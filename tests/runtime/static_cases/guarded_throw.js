// Team-written synthetic tool. A non-owner is stopped by throwing before the write.
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const rows = await helpers.httpRequest({ method: 'GET', url: `https://example.invalid/notes?id=eq.${input.id}` });
  const caller = $('Caller').first().json.userId;
  if (rows[0].owner_user_id !== caller) {
    throw new Error('denied');
  }
  await helpers.httpRequest({ method: 'DELETE', url: `https://example.invalid/notes?id=eq.${input.id}` });
  return 'deleted';
}
