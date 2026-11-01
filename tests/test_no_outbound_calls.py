"""Mirage is inbound-only. This test fails if the repo ever gains a way to place calls.

It looks at three things: Python imports, package names in requirements files,
and script/import names in the browser code. It reads imports with `ast`, so a
comment or docstring that merely mentions a provider does not trip it.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "data", "__pycache__", ".pytest_cache"}

# Telephony / dialing / SIP / cloud-calling libraries (module names and PyPI names).
FORBIDDEN_PACKAGES = {
    "twilio", "plivo", "vonage", "nexmo", "telnyx", "signalwire", "exotel", "sinch",
    "infobip", "messagebird", "kaleyra", "knowlarity", "ozonetel", "bandwidth-sdk",
    "pjsua", "pjsua2", "pyvoip", "sipsimple", "aiosip", "pysip", "pysipcall",
    "panoramisk", "pyst2", "asterisk", "freeswitch", "plivohelper",
}
# Function names that would mean "place a call".
FORBIDDEN_FUNCTIONS = re.compile(
    r"^(dial|dial_out|dial_number|place_call|make_call|start_call|call_number|originate|outbound_call)$"
)
# Browser-side softphone/dialer libraries.
FORBIDDEN_WEB = re.compile(r"\b(sip\.js|jssip|twilio|plivo|vonage|telnyx|signalwire)\b", re.IGNORECASE)


def _normalise(name: str) -> str:
    return name.lower().replace("_", "-")


def find_violations(root: Path) -> list[str]:
    found: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        rel = path.relative_to(root)
        if path.suffix == ".py":
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                found.append(f"{rel}: cannot parse, so cannot be checked")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if FORBIDDEN_FUNCTIONS.match(node.name):
                        found.append(f"{rel}:{node.lineno}: function named {node.name!r}")
                    continue
                else:
                    continue
                for module in modules:
                    if _normalise(module.split(".")[0]) in FORBIDDEN_PACKAGES:
                        found.append(f"{rel}:{node.lineno}: imports {module!r}")
        elif path.name.startswith("requirements") and path.suffix == ".txt":
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                package = re.split(r"[<>=!~\[; ]", line.strip(), maxsplit=1)[0]
                if package and _normalise(package) in FORBIDDEN_PACKAGES:
                    found.append(f"{rel}:{lineno}: depends on {package!r}")
        elif path.suffix in {".js", ".mjs", ".html"}:
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if FORBIDDEN_WEB.search(line):
                    found.append(f"{rel}:{lineno}: browser code mentions a dialer library")
    return found


def test_repo_has_no_telephony_or_dialing_code():
    assert find_violations(ROOT) == []


def test_checker_actually_catches_violations(tmp_path: Path):
    """Guard against the check passing only because it looks at nothing."""
    (tmp_path / "bad_import.py").write_text("from twilio.rest import Client\n")
    (tmp_path / "bad_func.py").write_text("def place_call(number):\n    pass\n")
    (tmp_path / "requirements.txt").write_text("httpx>=0.27\nTwilio==9.0\n")
    (tmp_path / "app.js").write_text("import { UA } from 'jssip';\n")
    (tmp_path / "fine.py").write_text("# we never use twilio here\nimport json\n")
    problems = "\n".join(find_violations(tmp_path))
    assert "bad_import.py" in problems
    assert "bad_func.py" in problems
    assert "requirements.txt" in problems
    assert "app.js" in problems
    assert "fine.py" not in problems
