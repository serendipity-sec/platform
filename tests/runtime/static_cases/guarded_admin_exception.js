// 팀이 작성한 합성 도구. 소유자가 아니고 관리자도 아니면 막는다(n8n-claw 보완 형태)
async function __n8n_tool(query) {
  const input = JSON.parse(query);
  const rows = await helpers.httpRequest({ method: 'GET', url: `https://example.invalid/notes?id=eq.${input.id}` });
  const row = rows[0];
  let requester = null;
  try { requester = $('Merge Input').first().json.qualifiedUserId || null; } catch (_e) {}
  const own = !!row.owner_user_id && row.owner_user_id === requester;
  if (!own && !(await isMemoryAdmin(requester))) return 'denied';
  await helpers.httpRequest({ method: 'DELETE', url: `https://example.invalid/notes?id=eq.${input.id}` });
  return 'deleted';
}
