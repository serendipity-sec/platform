// 팀이 작성한 합성 도구. method를 변수에 담아 지운다
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const method = 'DELETE';
  await helpers.httpRequest({ method, url: `https://example.invalid/notes?id=eq.${input.id}` });
  return 'deleted';
}
