from pathlib import Path

RULE_ID = "flowdelta.n8n.agent-tool-write-without-ownership-check"
RULE_VERSION = "0.5.0"
RULE_PATH = Path("runtime/static-scan/rules/agent-tool-write-without-ownership-check.yaml")

OWNER_FIELDS = (
    "owner_user_id",
    "owner_id",
    "ownerId",
    "user_id",
    "userId",
    "created_by",
    "createdBy",
    "author_id",
    "authorId",
)
MATCH = ("===", "==")
MISMATCH = ("!==", "!=")
WRITE_METHODS = ("DELETE", "PATCH", "PUT")
# n8n 다른 노드 출력을 읽는 접근자. 모델 입력이 아닌 실행 문맥에서 온 요청자만 인정한다
NODE_ACCESSORS = ("first().json", "last().json", "item.json")


def _exits(condition: str) -> list[list[str]]:
    return [
        [f"if ({condition}) {{", "  ...", "  return ...;", "}"],
        [f"if ({condition}) return ...;"],
        [f"if ({condition}) {{", "  ...", "  throw $ERR;", "}"],
        [f"if ({condition}) throw $ERR;"],
    ]


def _mismatch_conditions(row: str) -> list[str]:
    out = []
    for op in MISMATCH:
        cmp = f"{row}.$F {op} $REQ"
        out += [cmp, f"{cmp} || $X", f"$X || {cmp}", f"{cmp} && !$ADMIN"]
    return out


def _owner_definitions(row: str) -> list[str]:
    out = []
    for op in MATCH:
        cmp = f"{row}.$F {op} $REQ"
        out += [f"$V = {cmp};", f"$V = $A && {cmp};", f"$V = {cmp} && $A;"]
    return out


def _foreign_definitions(row: str) -> list[str]:
    return [f"$V = {row}.$F {op} $REQ;" for op in MISMATCH]


def guard_forms(row: str) -> list[list[str]]:
    forms: list[list[str]] = []
    for condition in _mismatch_conditions(row):
        forms += _exits(condition)
    for definition in _owner_definitions(row):
        for condition in ("!$V", "!$V || $X", "$X || !$V", "!$V && !$ADMIN"):
            forms += [[definition, "...", *exit_] for exit_ in _exits(condition)]
    for definition in _foreign_definitions(row):
        for condition in ("$V", "$V || $X", "$X || $V", "$V && !$ADMIN"):
            forms += [[definition, "...", *exit_] for exit_ in _exits(condition)]
    return forms


def branch_forms(row: str) -> list[list[str]]:
    forms: list[list[str]] = []
    for op in MATCH:
        cmp = f"{row}.$F {op} $REQ"
        for condition in (cmp, f"{cmp} && $X", f"$X && {cmp}"):
            forms.append([f"if ({condition}) {{", "  ...", "}"])
    for definition in _owner_definitions(row):
        for condition in ("$V", "$V && $X", "$X && $V"):
            forms.append([definition, "...", f"if ({condition}) {{", "  ...", "}"])
    return forms


def requester_forms() -> list[list[str]]:
    forms: list[list[str]] = []
    for accessor in NODE_ACCESSORS:
        value = f"$D($NODE).{accessor}.$KEY"
        for assign in (f"$REQ = {value};", f"$REQ = {value} || $FALLBACK;"):
            forms.append([assign])
            forms.append(["try {", f"  {assign}", "} catch ($E) {", "  ...", "}"])
    return forms


FETCH = "$RS = await helpers.httpRequest(...);"
# 대상 행은 도구가 읽어 온 결과의 원소이거나 그 원소를 담은 변수다
ROW_SOURCES = (
    ("$RS[$I]", [FETCH]),
    ("$ROW", [FETCH, "...", "$ROW = $RS[$I];"]),
)


def _indent(lines: list[str], depth: int) -> list[str]:
    return [" " * depth + line for line in lines]


def _regions(statements: list[str]) -> list[list[str]]:
    # 검사가 일반 블록 안에 있고 쓰기가 블록 밖에 있는 경우도 본다
    return [
        [*statements, "..."],
        ["{", "  ...", *_indent(statements, 2), "  ...", "}", "..."],
    ]


def _inside(pattern: list[str], depth: int) -> list[str]:
    return [" " * depth + "- inside: |", *_indent(pattern, depth + 4)]


def _any(patterns: list[list[str]], depth: int) -> list[str]:
    out = [" " * depth + "- any:"]
    for pattern in patterns:
        out += _inside(pattern, depth + 4)
    return out


def _guard_block(row: str, admin: bool, depth: int) -> list[str]:
    forms = [form for form in guard_forms(row) if ("$ADMIN" in " ".join(form)) == admin]
    out = [" " * depth + "- any:"]
    for form in forms:
        for region in _regions(form):
            out += _inside(region, depth + 4)
    if not admin:
        for form in branch_forms(row):
            out += _inside(form, depth + 4)
    return out


def _requester_block(depth: int) -> list[str]:
    regions = [region for form in requester_forms() for region in _regions(form)]
    return _any(regions, depth)


def _owner_check_block(depth: int) -> list[str]:
    pad = " " * depth
    fields = "|".join(OWNER_FIELDS)
    out = [pad + "any:"]
    for row, source in ROW_SOURCES:
        for admin in (False, True):
            out += [pad + "  - all:"]
            out += _guard_block(row, admin, depth + 6)
            out += _requester_block(depth + 6)
            out += _any(_regions(source), depth + 6)
            out += [
                pad + "    where:",
                pad + "      - metavariable: $F",
                pad + f"        regex: ^({fields})$",
                pad + "      - metavariable: $D",
                pad + "        regex: ^\\$$",
            ]
            if admin:
                # 관리자 예외는 admin이 들어간 변수·함수 호출만 인정한다(모델 입력의 속성 등 제외)
                out += [
                    pad + "      - metavariable: $ADMIN",
                    pad + "        regex: (?i)^[^.\\[]*admin",
                ]
    return out


def rule_yaml() -> str:
    methods = "|".join(WRITE_METHODS)
    lines = [
        "# Generated by flowdelta.runtime.static_rules. Do not edit by hand.",
        "rules:",
        f"  - id: {RULE_ID}",
        "    metadata:",
        f'      rule_version: "{RULE_VERSION}"',
        f"      owner_fields: [{', '.join(OWNER_FIELDS)}]",
        "      category: broken-access-control",
        '      cwe: "CWE-639: Authorization Bypass Through User-Controlled Key"',
        "      scope: >-",
        "        jsCode of an n8n toolCode node that an AI Agent can call through ai_tool,",
        "        extracted as one function per tool. Static condition only: it does not show",
        "        that the agent calls the tool or that a record changes.",
        "      meaning: >-",
        "        A finding means no supported guard was found that stops a non-owner before",
        "        the write. It does not mean that no owner comparison exists.",
        "    message: >-",
        "      Agent tool sends a write request whose target comes from the model-written",
        "      tool input, and no supported owner guard appears before it: a guard compares",
        "      the owner field of a row fetched by the tool with a requester read from another",
        "      node, and its whole condition stops a non-owner (admin exception allowed).",
        "    severity: ERROR",
        "    languages: [javascript]",
        "    taint:",
        "      sources:",
        "        - pattern: JSON.parse(query)",
        "        - pattern: query",
        "      sinks:",
        "        - all:",
        '            - inside: "helpers.httpRequest({..., method: $METHOD, ...})"',
        '            - inside: "helpers.httpRequest({..., url: $SINK, ...})"',
        "            - not:",
        *_owner_check_block(16),
        "          where:",
        "            - metavariable: $METHOD",
        f"              regex: (?i)^['\"]?({methods})['\"]?$",
        "            - focus: $SINK",
    ]
    return "\n".join(lines) + "\n"
