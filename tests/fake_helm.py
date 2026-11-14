#!/usr/bin/env python3
"""A tiny stand-in for `helm template`, used when real helm is not installed.

Supports only what the fixture chart needs: values merging (-f, --set) and
plain `{{ .Values.a.b }}` / `{{ .Release.Name }}` / `{{ .Release.Namespace }}`.
"""
import re
import sys
from pathlib import Path

import yaml


def merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            merge(a[k], v)
        else:
            a[k] = v
    return a


def main(argv):
    if argv[:1] == ["pull"]:
        # pull <ref> --version V --untar --untardir DIR, served from $FAKE_HELM_REPO/<name>-<V>/
        import os, shutil
        name = argv[1].rstrip("/").split("/")[-1]
        version = argv[argv.index("--version") + 1]
        src = Path(os.environ.get("FAKE_HELM_REPO", "/nonexistent")) / f"{name}-{version}"
        if not src.is_dir():
            sys.exit(f"Error: chart \"{name}\" version \"{version}\" not found")
        shutil.copytree(src, Path(argv[argv.index("--untardir") + 1]) / name)
        return
    if argv[:1] != ["template"]:
        sys.exit(f"fake helm: unsupported command {argv[:1]}")
    release, chart = argv[1], Path(argv[2])
    rest = argv[3:]
    namespace, files, sets = "default", [], []
    i = 0
    while i < len(rest):
        if rest[i] == "--namespace":
            namespace = rest[i + 1]; i += 2
        elif rest[i] == "-f":
            files.append(rest[i + 1]); i += 2
        elif rest[i] == "--set":
            sets.append(rest[i + 1]); i += 2
        else:
            i += 1
    values = yaml.safe_load((chart / "values.yaml").read_text()) or {}
    for f in files:
        merge(values, yaml.safe_load(Path(f).read_text()) or {})
    for s in sets:
        key, val = s.split("=", 1)
        node = values
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = yaml.safe_load(val)
    name = yaml.safe_load((chart / "Chart.yaml").read_text())["name"]

    def sub(m):
        expr = m.group(1).strip()
        if expr == ".Release.Name":
            return release
        if expr == ".Release.Namespace":
            return namespace
        if expr.startswith(".Values."):
            node = values
            for p in expr[len(".Values."):].split("."):
                if not isinstance(node, dict) or p not in node:
                    sys.stderr.write(f"Error: nil pointer evaluating {expr}\n")
                    sys.exit(1)
                node = node[p]
            return str(node)
        sys.stderr.write(f"Error: fake helm cannot evaluate {expr}\n")
        sys.exit(1)

    if "--include-crds" in rest:
        for crd in sorted((chart / "crds").glob("*.yaml")):
            print(f"---\n# Source: {name}/crds/{crd.name}\n{crd.read_text().rstrip()}")
    for tpl in sorted((chart / "templates").glob("*.yaml")):
        body = re.sub(r"\{\{-?\s*(.*?)\s*-?\}\}", sub, tpl.read_text())
        print(f"---\n# Source: {name}/templates/{tpl.name}\n{body.rstrip()}")


if __name__ == "__main__":
    main(sys.argv[1:])
