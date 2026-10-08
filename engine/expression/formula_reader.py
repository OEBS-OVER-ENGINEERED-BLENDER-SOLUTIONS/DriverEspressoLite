"""Calculate a driver expression using only a short list of plain operations.

Driver Espresso shows a preview of every motion, so it has to calculate the same
numbers Blender's driver will. Those expressions are a small language: numbers,
names, arithmetic, comparisons, ``a if c else b`` and calls to a fixed set of
maths functions. This module reads the expression with ``ast.parse`` and turns
each part into a small Python function once; calling the result with a dictionary
of names does the calculation.

Only the parts listed in ``_build`` are understood. Anything else (attribute
access, subscripts, lambdas, comprehensions, keyword arguments, strings) is
refused when the expression is read, so an expression can never do more than
arithmetic on the names it is given.
"""

from __future__ import annotations

import ast
import operator

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}

_COMPARE = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def _build(node):
    """Return ``fn(names) -> value`` for one expression node."""
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or isinstance(value, (int, float)):
            return lambda names: value
        raise ValueError("Unsupported constant: %r" % (value,))

    if isinstance(node, ast.Name):
        name = node.id

        def read(names):
            try:
                return names[name]
            except KeyError:
                raise NameError("name %r is not defined" % name) from None

        return read

    if isinstance(node, ast.BinOp):
        apply = _BINARY.get(type(node.op))
        if apply is None:
            raise ValueError("Unsupported operator: %s" % type(node.op).__name__)
        left, right = _build(node.left), _build(node.right)
        return lambda names: apply(left(names), right(names))

    if isinstance(node, ast.UnaryOp):
        apply = _UNARY.get(type(node.op))
        if apply is None:
            raise ValueError("Unsupported operator: %s" % type(node.op).__name__)
        operand = _build(node.operand)
        return lambda names: apply(operand(names))

    if isinstance(node, ast.BoolOp):
        parts = [_build(value) for value in node.values]
        if isinstance(node.op, ast.And):
            def conjunction(names):
                result = True
                for part in parts:
                    result = part(names)
                    if not result:
                        return result
                return result
            return conjunction

        def disjunction(names):
            result = False
            for part in parts:
                result = part(names)
                if result:
                    return result
            return result
        return disjunction

    if isinstance(node, ast.Compare):
        first = _build(node.left)
        steps = []
        for op, comparator in zip(node.ops, node.comparators):
            apply = _COMPARE.get(type(op))
            if apply is None:
                raise ValueError("Unsupported comparison: %s" % type(op).__name__)
            steps.append((apply, _build(comparator)))

        def compare(names):
            left = first(names)
            result = True
            for apply, right_fn in steps:
                right = right_fn(names)
                result = apply(left, right)
                if not result:
                    return result
                left = right
            return result

        return compare

    if isinstance(node, ast.IfExp):
        test, body, orelse = _build(node.test), _build(node.body), _build(node.orelse)
        return lambda names: body(names) if test(names) else orelse(names)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise ValueError("Only plain function calls are supported")
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            raise ValueError("Only plain function calls are supported")
        name = node.func.id
        args = [_build(arg) for arg in node.args]

        def call(names):
            try:
                function = names[name]
            except KeyError:
                raise NameError("name %r is not defined" % name) from None
            return function(*[arg(names) for arg in args])

        return call

    raise ValueError("Unsupported expression: %s" % type(node).__name__)


def parse_expression(source):
    """Read one expression into its syntax tree. Raises SyntaxError if it is not a single expression.

    Leading spaces and tabs are ignored, as Python itself ignores them in front of an expression.
    """
    tree = ast.parse(source.lstrip(" 	"))
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Expr):
        raise SyntaxError("invalid syntax")
    return tree.body[0].value


def literal(node):
    """The plain value of a number or string written in the source: ``3``, ``-0.5``, ``"name"``.

    Raises ValueError for anything else (names, arithmetic, calls).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)):
        return node.value
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd))
            and isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, (int, float))
            and not isinstance(node.operand.value, bool)):
        return -node.operand.value if isinstance(node.op, ast.USub) else node.operand.value
    raise ValueError("not a plain number or string")


class Expression:
    """A parsed driver expression. Call it with a dict of names for its value."""

    __slots__ = ("source", "_run")

    def __init__(self, source):
        self.source = source
        # parse_expression raises SyntaxError for malformed text.
        self._run = _build(parse_expression(source))

    def __call__(self, names):
        return self._run(names)


def parse(source):
    """Read ``source`` once. Raises SyntaxError, or ValueError for unsupported syntax."""
    return Expression(source)
