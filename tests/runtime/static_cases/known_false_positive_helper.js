// Team-written synthetic tool. The owner check is in a helper function, which the rule
// cannot see: a known false positive of rule 0.4.0.
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const row = await loadRow(input.id);
  if (!isOwner(row, currentUser())) return 'denied';
  await helpers.httpRequest({ method: 'DELETE', url: `https://example.invalid/notes?id=eq.${input.id}` });
  return 'deleted';
}
