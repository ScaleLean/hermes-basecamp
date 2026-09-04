"""Freeze generic-tool routes from the reviewed Basecamp SDK and policy registry."""

from __future__ import annotations

import argparse
import ast
import inspect
import re
from pathlib import Path

from basecamp import AsyncClient
from basecamp.async_auth import AsyncStaticTokenProvider

ROOT = Path(__file__).resolve().parents[1]


def _string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        return None
    values = []
    for part in node.values:
        if isinstance(part, ast.Constant):
            values.append(str(part.value))
        elif isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name):
            values.append("{" + part.value.id + "}")
        else:
            return None
    return "".join(values)


def _sdk_routes() -> dict[str, tuple[str, str]]:
    routes: dict[str, tuple[str, str]] = {}
    account = AsyncClient(token_provider=AsyncStaticTokenProvider("inventory")).for_account(1)
    for service_name in dir(account):
        if service_name.startswith("_"):
            continue
        service = getattr(account, service_name)
        for service_class in type(service).__mro__:
            try:
                module_path = inspect.getsourcefile(service_class)
            except TypeError:
                continue
            if not module_path or "basecamp" not in module_path:
                continue
            tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
            class_node = next(
                (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == service_class.__name__),
                None,
            )
            if class_node is None:
                continue
            for function in class_node.body:
                if not isinstance(function, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                calls = [
                    node for node in ast.walk(function)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {
                        "_request", "_request_void", "_request_paginated", "_request_list",
                        "_request_paginated_wrapped",
                    }
                ]
                for call in calls:
                    if call.func.attr in {
                        "_request_paginated", "_request_list", "_request_paginated_wrapped"
                    } and len(call.args) >= 2:
                        method, path = "GET", _string(call.args[1])
                    elif len(call.args) >= 3:
                        method, path = _string(call.args[1]), _string(call.args[2])
                    else:
                        continue
                    if method and path:
                        routes.setdefault(f"{service_name}.{function.name}", (method, path))
                        break
    return routes


def _pattern(path: str) -> str:
    pieces = re.split(r"(\{[a-zA-Z_][a-zA-Z0-9_]*\})", path)
    return "".join(
        (
            rf"(?P<{piece[1:-1]}>\d{{4}}-\d{{2}}-\d{{2}})"
            if piece == "{date}"
            else rf"(?P<{piece[1:-1]}>\d+)"
        )
        if piece.startswith("{")
        else re.escape(piece)
        for piece in pieces
    )


def render() -> str:
    from policy import RiskClass, default_registry

    sdk = _sdk_routes()
    lines = [
        '"""Generated official SDK route inventory. Run scripts/generate_sdk_routes.py to update."""',
        "",
        "from __future__ import annotations",
        "",
        "# HTTP method, anchored path regex, policy capability.",
        "SDK_ROUTES: tuple[tuple[str, str, str], ...] = (",
    ]
    aliases = {
        "documents.update": "documents.replace",
        "schedules.update_entry": "schedules.replace_entry",
        "todolists.update": "todolists.replace",
        "todos.update": "todos.replace",
    }
    missing = []
    for capability in default_registry().list():
        if capability.risk is RiskClass.ADMINLAND_DENIED:
            continue
        sdk_method = aliases.get(
            f"{capability.service}.{capability.method}",
            f"{capability.service}.{capability.method}",
        )
        route = sdk.get(sdk_method)
        if route is None:
            missing.append(capability.name)
            continue
        method, path = route
        lines.append(f"    ({method!r}, {_pattern(path)!r}, {capability.name!r}),")
    if missing:
        raise RuntimeError("Policy capabilities lack official SDK routes: " + ", ".join(missing))
    lines.extend((")", ""))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()
    output = ROOT / "sdk_routes.py"
    rendered = render()
    if args.stdout:
        print(rendered, end="")
        return 0
    if args.check:
        if not output.exists() or output.read_text(encoding="utf-8") != rendered:
            raise SystemExit("sdk_routes.py is stale")
        return 0
    output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
