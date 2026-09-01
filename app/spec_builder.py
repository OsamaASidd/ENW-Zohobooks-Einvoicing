"""Merges the per-resource Zoho Books OpenAPI files in ../API into one document.

Each source file is a self-contained OpenAPI 3 doc with its own `components`
section. To combine them without name collisions, every component is
renamed with a `<file-stem>__` prefix and every internal $ref is rewritten
to match before the documents are merged.
"""

from pathlib import Path

import yaml


def _rewrite_refs(node, name_map):
    if isinstance(node, dict):
        if list(node.keys()) == ["$ref"] and node["$ref"] in name_map:
            return {"$ref": name_map[node["$ref"]]}
        return {k: _rewrite_refs(v, name_map) for k, v in node.items()}
    if isinstance(node, list):
        return [_rewrite_refs(v, name_map) for v in node]
    return node


def _load_namespaced(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}

    prefix = path.stem
    components = doc.get("components") or {}
    name_map = {}
    renamed_components = {}

    for section, items in components.items():
        if not isinstance(items, dict):
            renamed_components[section] = items
            continue
        renamed_items = {}
        for name, definition in items.items():
            new_name = f"{prefix}__{name}"
            name_map[f"#/components/{section}/{name}"] = f"#/components/{section}/{new_name}"
            renamed_items[new_name] = definition
        renamed_components[section] = renamed_items

    doc["components"] = _rewrite_refs(renamed_components, name_map)
    doc["paths"] = _rewrite_refs(doc.get("paths") or {}, name_map)
    return doc


def build_merged_spec(api_dir: Path, title: str, description: str) -> dict:
    merged_paths = {}
    merged_components = {}
    merged_tags = []
    seen_tag_names = set()
    scopes_union = {}

    for path in sorted(api_dir.glob("*.yml")):
        doc = _load_namespaced(path)

        for p, item in (doc.get("paths") or {}).items():
            merged_paths.setdefault(p, {}).update(item)

        for section, items in (doc.get("components") or {}).items():
            if section == "securitySchemes":
                merged_components.setdefault(section, {}).update(items)
                for scheme in items.values():
                    for flow in (scheme.get("flows") or {}).values():
                        scopes_union.update(flow.get("scopes") or {})
                continue
            merged_components.setdefault(section, {}).update(items)

        for tag in doc.get("tags") or []:
            if tag.get("name") not in seen_tag_names:
                seen_tag_names.add(tag.get("name"))
                merged_tags.append(tag)

    for scheme in merged_components.get("securitySchemes", {}).values():
        for flow in (scheme.get("flows") or {}).values():
            flow["scopes"] = scopes_union

    return {
        "openapi": "3.0.0",
        "info": {"title": title, "version": "1.0.0", "description": description},
        "servers": [{"url": "/", "description": "This FastAPI app (proxies to Zoho)"}],
        "tags": merged_tags,
        "paths": merged_paths,
        "components": merged_components,
    }


def get_paths_with_method(paths: dict, method: str) -> list:
    method = method.lower()
    return [p for p, item in paths.items() if method in item]
