#!/usr/bin/env python3
"""Check custom resources against the CRD schemas of the pinned charts, without a cluster.

    check_schemas.py --crds <file-with-CRDs.yaml>... -- <manifest.yaml>...

This is the offline stand-in for `kubectl apply --dry-run=server`: for every object whose group and kind have a
CustomResourceDefinition in the given files it checks, against the openAPIV3Schema of the object's version:
  - no field the schema does not know (the usual mistake: a misspelt or misplaced field, silently dropped or
    rejected by the API server)
  - required fields are there
  - enum values, basic types
It does NOT run CEL validation rules or admission webhooks, and it does not check defaults.
Objects of kinds without a CRD in the given files (Deployment, Service, ...) are counted and skipped.

Run through validate.sh (needs PyYAML: `uv run --no-project --with pyyaml python check_schemas.py ...`).
Exit code 1 when a problem is found.
"""
from __future__ import annotations

import sys

import yaml


class Loader(yaml.SafeLoader):
    """SafeLoader that reads a bare `=` as the string "=" (it appears as an enum value in some CRDs)."""


Loader.add_constructor("tag:yaml.org,2002:value", lambda loader, node: loader.construct_scalar(node))


def load_all(path: str) -> list[dict]:
    with open(path) as f:
        return [d for d in yaml.load_all(f, Loader=Loader) if isinstance(d, dict)]


def check(obj, schema: dict, path: str, errors: list[str]) -> None:
    if schema.get("x-kubernetes-int-or-string"):
        if not isinstance(obj, (int, str)) or isinstance(obj, bool):
            errors.append(f"{path}: expected an integer or a string, got {type(obj).__name__}")
        return
    if obj is None:
        if not schema.get("nullable") and schema.get("type") in ("object", "array", "string", "integer", "boolean", "number"):
            errors.append(f"{path}: null is not allowed")
        return
    t = schema.get("type")
    if "enum" in schema and obj not in schema["enum"]:
        errors.append(f"{path}: {obj!r} is not one of {schema['enum']}")
    if t == "object" or (t is None and ("properties" in schema or "additionalProperties" in schema)):
        if not isinstance(obj, dict):
            errors.append(f"{path}: expected an object, got {type(obj).__name__}")
            return
        props = schema.get("properties") or {}
        extra = schema.get("additionalProperties")
        open_ended = schema.get("x-kubernetes-preserve-unknown-fields") or (not props and extra is None)
        for req in schema.get("required") or []:
            if req not in obj:
                errors.append(f"{path}: required field '{req}' is missing")
        for k, v in obj.items():
            if k in props:
                check(v, props[k], f"{path}.{k}", errors)
            elif isinstance(extra, dict):
                check(v, extra, f"{path}.{k}", errors)
            elif extra is True or open_ended:
                continue
            else:
                near = [p for p in props if p.lower() == k.lower() or p.startswith(k[:4])][:4]
                errors.append(f"{path}: unknown field '{k}'" + (f" (did you mean: {', '.join(near)}?)" if near else ""))
    elif t == "array":
        if not isinstance(obj, list):
            errors.append(f"{path}: expected a list, got {type(obj).__name__}")
            return
        items = schema.get("items") or {}
        for i, v in enumerate(obj):
            check(v, items, f"{path}[{i}]", errors)
    elif t == "string":
        if not isinstance(obj, str):
            errors.append(f"{path}: expected a string, got {type(obj).__name__} ({obj!r})")
    elif t == "integer":
        if not isinstance(obj, int) or isinstance(obj, bool):
            errors.append(f"{path}: expected an integer, got {type(obj).__name__} ({obj!r})")
    elif t == "boolean":
        if not isinstance(obj, bool):
            errors.append(f"{path}: expected true or false, got {type(obj).__name__} ({obj!r})")
    elif t == "number":
        if not isinstance(obj, (int, float)) or isinstance(obj, bool):
            errors.append(f"{path}: expected a number, got {type(obj).__name__} ({obj!r})")


def main() -> int:
    args = sys.argv[1:]
    if "--" not in args or not args or args[0] != "--crds":
        print(__doc__)
        return 2
    crd_files, manifests = args[1:args.index("--")], args[args.index("--") + 1:]

    schemas: dict[tuple[str, str, str], dict] = {}
    kinds: dict[tuple[str, str], list[str]] = {}
    for path in crd_files:
        for d in load_all(path):
            if d.get("kind") != "CustomResourceDefinition":
                continue
            group, kind = d["spec"]["group"], d["spec"]["names"]["kind"]
            for v in d["spec"]["versions"]:
                if v.get("served", True) and v.get("schema", {}).get("openAPIV3Schema"):
                    schemas[(group, v["name"], kind)] = v["schema"]["openAPIV3Schema"]
                    kinds.setdefault((group, kind), []).append(v["name"])

    checked = skipped = 0
    problems: list[str] = []
    seen: dict[str, int] = {}
    for path in manifests:
        for d in load_all(path):
            api, kind = d.get("apiVersion", ""), d.get("kind", "")
            group, _, version = api.rpartition("/")
            name = d.get("metadata", {}).get("name") or d.get("metadata", {}).get("generateName", "?")
            if (group, kind) not in kinds:
                skipped += 1
                continue
            if (group, version, kind) not in schemas:
                problems.append(f"{path}: {kind}/{name}: apiVersion {api} is not served by the CRD "
                                f"(served: {', '.join(kinds[(group, kind)])})")
                continue
            errors: list[str] = []
            body = {k: v for k, v in d.items() if k not in ("apiVersion", "kind", "metadata")}
            schema = schemas[(group, version, kind)]
            check(body, {**schema, "properties": {k: v for k, v in (schema.get("properties") or {}).items()
                                                  if k not in ("apiVersion", "kind", "metadata")},
                         "required": [r for r in schema.get("required") or [] if r not in ("apiVersion", "kind", "metadata")]},
                  f"{kind}/{name}", errors)
            checked += 1
            seen[f"{group}/{version} {kind}"] = seen.get(f"{group}/{version} {kind}", 0) + 1
            problems += [f"{path}: {e}" for e in errors]

    for k in sorted(seen):
        print(f"  checked {seen[k]:>3} x {k}")
    print(f"  {checked} custom resources checked against {len(schemas)} CRD versions; {skipped} objects of built-in kinds skipped")
    for p in problems:
        print(f"  PROBLEM {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
