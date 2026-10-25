// Team-written synthetic tool. The record owner is compared with the caller before the write.
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const rows = await helpers.httpRequest({ method: 'GET', url: `https://example.invalid/notes?id=eq.${input.id}` });
  const caller = $('Caller').first().json.userId;
  if (rows[0].owner_user_id !== caller) return 'denied';
  await helpers.httpRequest({ method: 'PATCH', url: `https://example.invalid/notes?id=eq.${input.id}`, body: input.text });
  return 'updated';
}
