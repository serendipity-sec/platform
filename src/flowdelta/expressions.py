"""Recognize a small expression grammar without evaluating JavaScript or Python."""

import re
from dataclasses import dataclass
from typing import Literal, cast

from flowdelta.contracts import ValueExpression

QUOTED = r"(?:'(?:[^'\\]|\\['\\])*'|\"(?:[^\"\\]|\\[\"\\])*\")"
NAMED = re.compile(
    r"\$\((" + QUOTED + r")\)\.(item|first\(\))\.json((?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
)

TOKEN = re.compile(
    r"\s*(" + NAMED.pattern + r"|\$json(?:\.[A-Za-z_][A-Za-z0-9_]*)*|"
    r"'(?:[^'\\]|\\['\\])*'|\"(?:[^\"\\]|\\[\"\\])*\"|"
    r"[A-Za-z_][A-Za-z0-9_]*|-?[0-9]+(?:\.[0-9]+)?|===|!==|>=|<=|[{}:,+><])"
)


class UnsupportedExpression(ValueError):
    pass


@dataclass(frozen=True)
class Expression:
    kind: str
    value: str = ""
    members: tuple[tuple[str, "Expression"], ...] = ()
    terms: tuple["Expression", ...] = ()

    def reads(self) -> set[str]:
        result = {self.value} if self.kind in {"field", "named_field"} else set()
        for _, member in self.members:
            result.update(member.reads())
        for term in self.terms:
            result.update(term.reads())
        return result

    def to_contract(self) -> ValueExpression:
        if self.kind == "field":
            return ValueExpression(kind="current_field", field_path=self.value.split(".")[1:])
        if self.kind == "named_field":
            match = NAMED.fullmatch(self.value)
            if match is None:
                raise UnsupportedExpression("INVALID_NAMED_REFERENCE")
            return ValueExpression(
                kind="named_field",
                node_name=_unquote(match.group(1)),
                item_selection="LINKED" if match.group(2) == "item" else "FIRST",
                field_path=match.group(3).split(".")[1:],
            )
        if self.kind == "string":
            return ValueExpression(kind="constant", value=_unquote(self.value))
        if self.kind == "integer":
            return ValueExpression(kind="constant", value=int(self.value))
        if self.kind == "number":
            return ValueExpression(kind="constant", value=float(self.value))
        if self.kind == "literal":
            return ValueExpression(
                kind="constant", value={"true": True, "false": False, "null": None}[self.value]
            )
        if self.kind == "object":
            return ValueExpression(
                kind="object", members={k: v.to_contract() for k, v in self.members}
            )
        if self.kind == "concat":
            return ValueExpression(kind="concat", terms=[item.to_contract() for item in self.terms])
        if self.kind == "compare":
            operators = {"===": "eq", "!==": "neq", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}
            operator = cast(Literal["eq", "neq", "gt", "gte", "lt", "lte"], operators[self.value])
            return ValueExpression(
                kind="compare", operator=operator, terms=[item.to_contract() for item in self.terms]
            )
        raise UnsupportedExpression("UNSUPPORTED_EXPRESSION_TERM")


def _unquote(value: str) -> str:
    return re.sub(r"\\(['\"\\])", r"\1", value[1:-1])


class Parser:
    def __init__(self, source: str) -> None:
        if len(source) > 4096:
            raise UnsupportedExpression("EXPRESSION_SIZE_LIMIT")
        self.tokens: list[str] = []
        index = 0
        while index < len(source):
            if not source[index:].strip():
                break
            match = TOKEN.match(source, index)
            if match is None:
                raise UnsupportedExpression("UNSUPPORTED_EXPRESSION_TOKEN")
            self.tokens.append(match.group(1))
            index = match.end()
        self.index = 0

    def take(self) -> str:
        if self.index >= len(self.tokens):
            raise UnsupportedExpression("INCOMPLETE_EXPRESSION")
        token = self.tokens[self.index]
        self.index += 1
        return token

    def peek(self) -> str | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def value(self, depth: int = 0) -> Expression:
        if depth > 8:
            raise UnsupportedExpression("EXPRESSION_DEPTH_LIMIT")
        token = self.take()
        if token.startswith("$json"):
            expression = Expression("field", token)
        elif token.startswith("$("):
            expression = Expression("named_field", token)
        elif token.startswith(("'", '"')):
            expression = Expression("string", token)
        elif re.fullmatch(r"-?[0-9]+", token):
            expression = Expression("integer", token)
        elif re.fullmatch(r"-?[0-9]+\.[0-9]+", token):
            expression = Expression("number", token)
        elif token in {"true", "false", "null"}:
            expression = Expression("literal", token)
        elif token == "{":
            members: list[tuple[str, Expression]] = []
            if self.peek() == "}":
                self.take()
                return Expression("object")
            while True:
                key = self.take()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or any(
                    key == k for k, _ in members
                ):
                    raise UnsupportedExpression("INVALID_OBJECT_KEY")
                if self.take() != ":":
                    raise UnsupportedExpression("OBJECT_COLON_REQUIRED")
                members.append((key, self.value(depth + 1)))
                delimiter = self.take()
                if delimiter == "}":
                    break
                if delimiter != ",":
                    raise UnsupportedExpression("OBJECT_SEPARATOR_REQUIRED")
            expression = Expression("object", members=tuple(members))
        else:
            raise UnsupportedExpression("UNSUPPORTED_EXPRESSION_TERM")
        if self.peek() == "+":
            self.take()
            right = self.value(depth + 1)
            if expression.kind not in {"string", "field", "named_field"} or right.kind not in {
                "string",
                "field",
                "named_field",
                "concat",
            }:
                raise UnsupportedExpression("UNSUPPORTED_ADDITION")
            if "string" not in {expression.kind, right.kind} and right.kind != "concat":
                raise UnsupportedExpression("STRING_LITERAL_REQUIRED_FOR_CONCAT")
            if right.kind == "concat":
                # Three terms are supported only when a literal prefix forces string
                # concatenation. Do not guess whether two input fields add numerically.
                if (
                    expression.kind != "string"
                    or right.terms[0].kind not in {"field", "named_field"}
                    or right.terms[1].kind != "string"
                ):
                    raise UnsupportedExpression("UNSUPPORTED_ADDITION")
                expression = Expression(
                    "concat",
                    terms=(
                        Expression("concat", terms=(expression, right.terms[0])),
                        right.terms[1],
                    ),
                )
            else:
                expression = Expression("concat", terms=(expression, right))
        elif self.peek() in {"===", "!==", ">", ">=", "<", "<="}:
            operator = self.take()
            right = self.value(depth + 1)
            if expression.kind in {"object", "compare"} or right.kind in {"object", "compare"}:
                raise UnsupportedExpression("UNSUPPORTED_COMPARISON")
            expression = Expression("compare", operator, terms=(expression, right))
        return expression


def parse_expression(source: str) -> Expression:
    if not source.startswith("={{") or not source.endswith("}}"):
        raise UnsupportedExpression("EXPRESSION_WRAPPER_REQUIRED")
    parser = Parser(source[3:-2].strip())
    result = parser.value()
    if parser.peek() is not None:
        raise UnsupportedExpression("TRAILING_EXPRESSION_TOKENS")
    return result
