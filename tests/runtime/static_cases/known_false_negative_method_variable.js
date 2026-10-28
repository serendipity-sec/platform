// Team-written synthetic tool. The method is stored in a variable, which the rule does not
// follow: a known false negative of rule 0.4.0.
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const method = 'DELETE';
  await helpers.httpRequest({ method, url: `https://example.invalid/notes?id=eq.${input.id}` });
  return 'deleted';
}
