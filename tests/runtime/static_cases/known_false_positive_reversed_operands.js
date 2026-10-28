// Team-written synthetic tool. The caller is on the left of the comparison, a form the rule
// does not list, so it is reported: a known false positive of rule 0.4.0.
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const rows = await helpers.httpRequest({ method: 'GET', url: `https://example.invalid/notes?id=eq.${input.id}` });
  const caller = $('Caller').first().json.userId;
  if (caller !== rows[0].owner_user_id) return 'denied';
  await helpers.httpRequest({ method: 'DELETE', url: `https://example.invalid/notes?id=eq.${input.id}` });
  return 'deleted';
}
