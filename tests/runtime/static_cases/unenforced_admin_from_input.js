// 팀이 작성한 합성 도구. 관리자 예외처럼 보이지만 모델 입력 속성이라 비소유자가 통과할 수 있다
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const rows = await helpers.httpRequest({ method: "GET", url: `https://example.invalid/notes?id=eq.${input.id}` });
  const caller = $("C").first().json.userId;
  const own = rows[0].owner_user_id === caller;
  if (!own && !input.adminOverride) return "no";
  await helpers.httpRequest({ method: "DELETE", url: `https://example.invalid/notes?id=eq.${input.id}` });
}
