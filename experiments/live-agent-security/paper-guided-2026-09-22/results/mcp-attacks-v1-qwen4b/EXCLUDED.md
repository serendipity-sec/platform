# 개발 smoke 제외 기록

seed 47의 16개 arm은 정상 status의 의역을 자동 utility가 놓치는 문제를 찾기 위한 개발 smoke다. `tests have not been run yet`을 `tests pending`과 같은 사실 상태로 인정하도록 수정한 뒤 최종 batch를 `mcp-attacks-v2-qwen4b`로 분리했다.

v1의 공격 효과와 step-limit 원본은 보존하지만 최종 ASR·PUA·NRP 분모에는 포함하지 않는다. 공격 효과 oracle이나 policy는 smoke 뒤에 바꾸지 않았다.
