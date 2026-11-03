// 팀이 작성한 합성 도구. 검사를 별도 함수로 빼서 규칙이 보지 못한다(알려진 오탐)
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const row = await loadRow(input.id);
  if (!isOwner(row, currentUser())) return 'denied';
  await helpers.httpRequest({ method: 'DELETE', url: `https://example.invalid/notes?id=eq.${input.id}` });
  return 'deleted';
}
