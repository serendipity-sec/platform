// 팀이 작성한 합성 도구. 모델 입력을 변수에 담아 요청자처럼 비교한다
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const rows = await helpers.httpRequest({ method: 'GET', url: `https://example.invalid/notes?id=eq.${input.id}` });
  const caller = input.owner_id;
  if (rows[0].owner_id !== caller) return 'denied';
  await helpers.httpRequest({ method: 'DELETE', url: `https://example.invalid/notes?id=eq.${input.id}` });
  return 'deleted';
}
