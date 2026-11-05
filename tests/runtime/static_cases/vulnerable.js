// 팀이 작성한 합성 도구. 소유자 검사 없이 지운다
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  await helpers.httpRequest({ method: 'DELETE', url: `https://example.invalid/notes?id=eq.${input.id}` });
  return 'deleted';
}
