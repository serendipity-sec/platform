// 팀이 작성한 합성 도구. 관리자가 아닌 모델 입력 조건으로 검사를 건너뛸 수 있다
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const rows = await helpers.httpRequest({ method: 'GET', url: `https://example.invalid/notes?id=eq.${input.id}` });
  const caller = $('Caller').first().json.userId;
  const own = rows[0].owner_user_id === caller;
  if (!own && !input.force) return 'denied';
  await helpers.httpRequest({ method: 'DELETE', url: `https://example.invalid/notes?id=eq.${input.id}` });
  return 'deleted';
}
