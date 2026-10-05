"""The agent HTTP API contract (#264): api_schema.json plus a tiny stdlib validator.

One schema describes every response of agent/http_api.py. Producer tests validate real
responses against it, consumer tests (wowmap, agent-runner) build their fixtures from it
with sample(), and docs/AGENT-API.md is generated from it (python -m agent.api_contract).
Only the integer API_VERSION is needed at runtime, so no image has to ship the JSON.
"""
import json
import pathlib
import sys

SCHEMA_PATH = pathlib.Path(__file__).with_name("api_schema.json")


def load() -> dict:
    return json.loads(SCHEMA_PATH.read_text("utf-8"))


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is(value, t) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[t])


def validate(value, schema, path="$") -> list[str]:
    """Errors for value against the schema subset; [] when valid. Unknown fields are fine."""
    t = schema.get("type")
    if t is not None and not any(_is(value, x) for x in ([t] if isinstance(t, str) else t)):
        return [f"{path}: expected {t}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: {value!r} not in {schema['enum']}"]
    errs = []
    if isinstance(value, dict):
        for k in schema.get("required", ()):
            if k not in value:
                errs.append(f"{path}.{k}: missing")
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                errs += validate(value[k], sub, f"{path}.{k}")
    elif isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            errs += validate(v, schema["items"], f"{path}[{i}]")
    return errs


def sample(schema, **overrides):
    """A valid instance with every declared property filled in; overrides replace top-level keys."""
    t = schema.get("type")
    t = next((x for x in ([t] if isinstance(t, str) else t or []) if x != "null"), "null") if t else "null"
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        out = {k: sample(s) for k, s in schema.get("properties", {}).items()}
        out.update(overrides)
        return out
    if t == "array":
        return [sample(schema["items"])] if "items" in schema else []
    return {"string": "x", "integer": 1, "number": 1.5, "boolean": True}.get(t)


def response_schema(endpoint: str, response: str, contract: dict | None = None) -> dict:
    return (contract or load())["endpoints"][endpoint]["responses"][response]


def render_markdown(contract: dict | None = None) -> str:
    c = contract or load()

    def rows(schema, prefix=""):
        req = set(schema.get("required", ()))
        for k, s in schema.get("properties", {}).items():
            t = s.get("type", "any")
            yield f"| `{prefix}{k}` | {'/'.join(t) if isinstance(t, list) else t} | {'yes' if k in req else ''} |"
            if s.get("type") == "object" and "properties" in s:
                yield from rows(s, f"{prefix}{k}.")
            elif s.get("type") == "array" and s.get("items", {}).get("properties"):
                yield from rows(s["items"], f"{prefix}{k}[].")

    out = ["<!-- Generated from agent/api_schema.json by `python -m agent.api_contract`; do not edit. -->",
           f"# {c['title']} (api_version {c['api_version']})", "", c["description"], ""]
    for ep, spec in c["endpoints"].items():
        out += [f"## `{ep if ' ' in ep else 'GET ' + ep}`", "", spec["summary"], ""]
        for name, schema in spec["responses"].items():
            out += [f"Response: {name}", "", "| field | type | always present |", "|---|---|---|", *rows(schema), ""]
    out += ["## Errors", "", c["errors"]["summary"], ""]
    out += ["## Notes", "", *[f"- {n}" for n in c["notes"]], ""]
    return "\n".join(out)


if __name__ == "__main__":
    doc = SCHEMA_PATH.parents[1] / "docs/AGENT-API.md"
    if "--check" in sys.argv:  # scripts/generate.py --check
        if doc.read_text() != render_markdown():
            sys.exit("docs/AGENT-API.md is out of date: it is generated, do not edit it; "
                     "run python3 scripts/generate.py (generator: python -m agent.api_contract --write)")
    elif "--write" in sys.argv:  # scripts/generate.py
        doc.write_text(render_markdown())
    else:
        print(render_markdown(), end="")
