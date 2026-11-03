// 팀이 작성한 합성 도구. 불일치 조건에 다른 조건이 &&로 붙어 비소유자가 통과할 수 있다
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const rows = await helpers.httpRequest({ method: 'GET', url: `https://example.invalid/notes?id=eq.${input.id}` });
  const caller = $('Caller').first().json.userId;
  if (rows[0].owner_user_id !== caller && input.strict) return 'denied';
  await helpers.httpRequest({ method: 'DELETE', url: `https://example.invalid/notes?id=eq.${input.id}` });
  return 'deleted';
}
