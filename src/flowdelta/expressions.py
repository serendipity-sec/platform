"""Recognize a small expression grammar without evaluating JavaScript or Python."""

import re
from dataclasses import dataclass

TOKEN = re.compile(
    r"\s*(\$json(?:\.[A-Za-z_][A-Za-z0-9_]*)*|"
    r"'(?:[^'\\]|\\['\\])*'|\"(?:[^\"\\]|\\[\"\\])*\"|"
    r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[{}:,+])"
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
        result = {self.value} if self.kind == "field" else set()
        for _, member in self.members:
            result.update(member.reads())
        for term in self.terms:
            result.update(term.reads())
        return result


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
        elif token.startswith(("'", '"')):
            expression = Expression("string", token)
        elif token.isdigit():
            expression = Expression("integer", token)
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
            if expression.kind not in {"string", "field"} or right.kind not in {
                "string",
                "field",
                "concat",
            }:
                raise UnsupportedExpression("UNSUPPORTED_ADDITION")
            expression = Expression("concat", terms=(expression, right))
        return expression


def parse_expression(source: str) -> Expression:
    if not source.startswith("={{") or not source.endswith("}}"):
        raise UnsupportedExpression("EXPRESSION_WRAPPER_REQUIRED")
    parser = Parser(source[3:-2].strip())
    result = parser.value()
    if parser.peek() is not None:
        raise UnsupportedExpression("TRAILING_EXPRESSION_TOKENS")
    return result
