"""Evaluate Go-template strings (as used by Argo CD ApplicationSets) by borrowing Helm.

Helm templates are Go templates plus the sprig functions, which is what Argo CD
uses for `goTemplate: true`. Instead of reimplementing the language, each
templated string is wrapped in a named template inside a throwaway chart and
rendered with `helm template`; the parameter sets are passed as values.
"""

from __future__ import annotations

import copy
import json
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .core import ChartDeltaError, RenderOptions, _run

ENGINE_CHART = "chartdelta-appset-engine"
_FAST = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")


def _collect(node: Any, path: tuple, out: list[tuple[tuple, str]]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _collect(v, path + (k,), out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _collect(v, path + (i,), out)
    elif isinstance(node, str) and "{{" in node:
        out.append((path, node))


def _set(node: Any, path: tuple, value: str) -> None:
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value


def _path_str(path: tuple) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out


def render_objects(obj: Any, param_sets: list[dict[str, Any]], opts: RenderOptions, what: str) -> list[Any]:
    """Render every templated string inside `obj` once per parameter set (goTemplate mode)."""
    fields: list[tuple[tuple, str]] = []
    _collect(obj, (), fields)
    if not fields or not param_sets:
        return [copy.deepcopy(obj) for _ in param_sets]

    with tempfile.TemporaryDirectory(prefix="chartdelta-tpl-") as tmp:
        chart = Path(tmp) / ENGINE_CHART
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text(f"apiVersion: v2\nname: {ENGINE_CHART}\nversion: 0.0.0\n")
        (chart / "values.yaml").write_text("{}\n")
        # Inside a named template, both `.` and `$` are the argument: the parameter set.
        defs = "".join('{{ define "f%d" }}%s{{ end }}\n' % (j, s) for j, (_, s) in enumerate(fields))
        (chart / "templates" / "_fields.tpl").write_text(defs, encoding="utf-8")
        lines = "".join(
            '  p{{ $i }}_f%d: {{ include "f%d" $p | toJson }}\n' % (j, j) for j in range(len(fields))
        )
        (chart / "templates" / "out.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: " + ENGINE_CHART + "\ndata:\n"
            "{{- range $i, $p := .Values.paramSets }}\n" + lines + "{{- end }}\n",
            encoding="utf-8",
        )
        params = Path(tmp) / "params.yaml"  # JSON is valid YAML
        params.write_text(json.dumps({"paramSets": param_sets}, default=str), encoding="utf-8")
        proc = _run([opts.helm_bin, "template", "engine", str(chart), "-f", str(params)])
        if proc.returncode != 0:
            err = proc.stderr.strip()
            m = re.search(r'"f(\d+)"', err) or re.search(r"\bf(\d+)\b", err)
            where = f" in field '{_path_str(fields[int(m.group(1))][0])}'" if m and int(m.group(1)) < len(fields) else ""
            raise ChartDeltaError(f"Go template error in the {what}{where}:\n{err}")
        data: dict[str, Any] = {}
        for doc in yaml.safe_load_all(proc.stdout):
            if isinstance(doc, dict) and doc.get("kind") == "ConfigMap":
                data = doc.get("data") or {}

    results = []
    for i in range(len(param_sets)):
        rendered = copy.deepcopy(obj)
        for j, (path, _) in enumerate(fields):
            key = f"p{i}_f{j}"
            if key not in data:
                raise ChartDeltaError(f"Template engine returned no value for {what} field '{_path_str(path)}'.")
            _set(rendered, path, str(data[key]))
        results.append(rendered)
    return results


def _flatten(node: Any, prefix: str, out: dict[str, str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = "" if node is None else str(node)


def render_fast(obj: Any, params: dict[str, Any]) -> Any:
    """Classic (non-Go) ApplicationSet templating: `{{ name }}` and `{{ path.basename }}`."""
    flat: dict[str, str] = {}
    _flatten(params, "", flat)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            return _FAST.sub(lambda m: flat.get(m.group(1), m.group(0)), node)
        return node

    return walk(obj)
