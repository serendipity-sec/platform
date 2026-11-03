// 팀이 작성한 합성 도구. 주인이 달라도 로그만 남기고 지운다
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const rows = await helpers.httpRequest({ method: 'GET', url: `https://example.invalid/notes?id=eq.${input.id}` });
  const caller = $('Caller').first().json.userId;
  if (rows[0].owner_user_id !== caller) {
    console.log('mismatch');
  }
  await helpers.httpRequest({ method: 'DELETE', url: `https://example.invalid/notes?id=eq.${input.id}` });
  return 'deleted';
}
