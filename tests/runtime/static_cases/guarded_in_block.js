// 팀이 작성한 합성 도구. 소유자 검사가 일반 블록 안에 있다
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
