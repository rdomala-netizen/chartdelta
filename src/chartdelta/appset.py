"""Preview an Argo CD ApplicationSet offline.

Evaluates the generators against local checkouts, expands the template into
Applications, renders each Application's source, and diffs base vs. target.
This is a fast local approximation of what Argo CD would do, not Argo CD itself.
"""

from __future__ import annotations

import base64
import dataclasses
import difflib
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .core import (
    ChartDeltaError, DiffResult, RenderOptions, _git, _load_yaml_file, _run, diff_manifests,
    ensure_dependencies, helm_template, parse_manifests, to_yaml,
)
from .gotemplate import render_fast, render_objects

SUPPORTED = ("list", "git", "clusters", "matrix")


# --------------------------------------------------------------------------- #
# Repositories and sides
# --------------------------------------------------------------------------- #


def normalise_url(url: str) -> str:
    u = url.strip()
    u = re.sub(r"^[a-z+]+://", "", u)       # scheme
    u = re.sub(r"^[^@/]+@", "", u)          # user@
    u = u.replace(":", "/", 1) if re.match(r"^[^/]+:[^/0-9]", u) else u   # git@host:org/repo
    u = re.sub(r"\.git/?$", "", u).rstrip("/")
    return u.lower()


def repo_root_and_url(path: Path) -> tuple[Path, str] | None:
    start = path if path.is_dir() else path.parent
    top = _git(start, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None
    root = Path(top.stdout.strip()).resolve()
    url = _git(root, "remote", "get-url", "origin")
    return root, (normalise_url(url.stdout) if url.returncode == 0 and url.stdout.strip() else "")


@dataclass
class Side:
    label: str
    repos: dict[str, Path]          # normalised repo URL -> directory holding that repo's tree

    def repo(self, url: str) -> Path | None:
        return self.repos.get(normalise_url(url))


def _export(repo: Path, ref: str, dest: Path) -> str:
    sha = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if sha.returncode != 0:
        raise ChartDeltaError(f"Unknown git ref '{ref}' in {repo}: {sha.stderr.strip()}")
    dest.mkdir(parents=True, exist_ok=True)
    archive = _git(repo, "archive", "--format=tar", sha.stdout.strip(), binary=True)
    if archive.returncode != 0:
        raise ChartDeltaError(f"git archive failed in {repo}: {archive.stderr.decode(errors='replace').strip()}")
    tar = subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, capture_output=True)
    if tar.returncode != 0:
        raise ChartDeltaError(f"Could not unpack {repo} at {ref}.")
    return sha.stdout.strip()


# --------------------------------------------------------------------------- #
# Clusters
# --------------------------------------------------------------------------- #


def load_clusters(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    data = _load_yaml_file(path)
    items = data.get("clusters") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ChartDeltaError(f"{path} must contain a list of clusters (or a 'clusters:' key holding one).")
    out = []
    for c in items:
        if not isinstance(c, dict) or c.get("name") in (None, "") or c.get("server") in (None, ""):
            raise ChartDeltaError(f"Every cluster in {path} needs 'name' and 'server'.")
        out.append({
            "name": str(c["name"]), "server": str(c["server"]),
            "labels": {str(k): str(v) for k, v in (c.get("labels") or {}).items()},
            "annotations": {str(k): str(v) for k, v in (c.get("annotations") or {}).items()},
        })
    return out


def clusters_from_secrets(secret_list: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn `kubectl get secret -l argocd.argoproj.io/secret-type=cluster -o json` into cluster entries.

    Only name, server, labels and annotations are kept; credentials are never read.
    """
    out = []
    for item in secret_list.get("items", []):
        data = item.get("data") or {}
        meta = item.get("metadata") or {}

        def dec(key: str) -> str:
            try:
                return base64.b64decode(data.get(key, "")).decode()
            except Exception:
                return ""

        labels = {k: v for k, v in (meta.get("labels") or {}).items() if k != "argocd.argoproj.io/secret-type"}
        annotations = {k: v for k, v in (meta.get("annotations") or {}).items()
                       if not k.startswith("kubectl.kubernetes.io/") and k != "managed-by"}
        entry: dict[str, Any] = {"name": dec("name") or meta.get("name", ""), "server": dec("server"), "labels": labels}
        if annotations:
            entry["annotations"] = annotations
        out.append(entry)
    return sorted(out, key=lambda c: c["name"])


def export_clusters(namespace: str, context: str | None) -> list[dict[str, Any]]:
    cmd = ["kubectl"] + (["--context", context] if context else []) + [
        "get", "secret", "-n", namespace, "-l", "argocd.argoproj.io/secret-type=cluster", "-o", "json"]
    proc = _run(cmd)
    if proc.returncode != 0:
        raise ChartDeltaError(f"kubectl failed:\n{proc.stderr.strip()}")
    return clusters_from_secrets(json.loads(proc.stdout))


def _dns(name: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "-", name.lower()).strip("-")


def _selector_matches(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != str(v):
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op = expr.get("key"), expr.get("operator")
        values = [str(v) for v in expr.get("values") or []]
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and labels.get(key) in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #


@dataclass
class Context:
    go: bool
    clusters: list[dict[str, Any]]
    opts: RenderOptions
    warnings: list[str] = field(default_factory=list)


def _glob_regex(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"; i += 3
        elif pattern.startswith("**", i):
            out += ".*"; i += 2
        elif pattern[i] == "*":
            out += "[^/]*"; i += 1
        elif pattern[i] == "?":
            out += "[^/]"; i += 1
        else:
            out += re.escape(pattern[i]); i += 1
    return re.compile(out + r"\Z")


def _walk(root: Path) -> tuple[list[str], list[str]]:
    files, dirs = [], []
    for base, dnames, fnames in os.walk(root):
        dnames[:] = sorted(d for d in dnames if d != ".git")
        rel = Path(base).relative_to(root).as_posix()
        prefix = "" if rel == "." else rel + "/"
        dirs += [prefix + d for d in dnames]
        files += [prefix + f for f in sorted(fnames)]
    return files, dirs


def _path_params(directory: str, filename: str | None) -> dict[str, Any]:
    base = directory.rsplit("/", 1)[-1] if directory else ""
    p: dict[str, Any] = {"path": directory, "basename": base, "basenameNormalized": _dns(base),
                         "segments": directory.split("/") if directory else []}
    if filename is not None:
        p["filename"] = filename
        p["filenameNormalized"] = _dns(filename)
    return p


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    return out


def _git_params(spec: dict[str, Any], side: Side, ctx: Context) -> list[dict[str, Any]]:
    url = spec.get("repoURL", "")
    root = side.repo(url)
    if root is None:
        raise ChartDeltaError(
            f"The git generator reads {url}, but no local checkout is known for it.\n"
            f"Pass --repo {url}=/path/to/checkout")
    files, dirs = _walk(root)
    out: list[dict[str, Any]] = []
    if spec.get("files"):
        for entry in spec["files"]:
            rx = _glob_regex(str(entry.get("path", "")))
            for f in files:
                if not rx.match(f):
                    continue
                try:
                    content = yaml.safe_load((root / f).read_text(encoding="utf-8"))
                except (OSError, yaml.YAMLError) as exc:
                    raise ChartDeltaError(f"{side.label}: cannot parse {f}: {exc}") from None
                directory, _, name = f.rpartition("/")
                for item in content if isinstance(content, list) else [content or {}]:
                    if not isinstance(item, dict):
                        raise ChartDeltaError(f"{side.label}: {f} must contain a map (or a list of maps).")
                    out.append({**item, "path": _path_params(directory, name)})
    elif spec.get("directories"):
        include = [e for e in spec["directories"] if not e.get("exclude")]
        exclude = [_glob_regex(str(e.get("path", ""))) for e in spec["directories"] if e.get("exclude")]
        for entry in include:
            rx = _glob_regex(str(entry.get("path", "")))
            for d in dirs:
                if rx.match(d) and not any(x.match(d) for x in exclude):
                    out.append({"path": _path_params(d, None)})
    else:
        raise ChartDeltaError("git generator needs 'files' or 'directories'.")
    if spec.get("values"):
        out = [{**p, "values": spec["values"]} for p in out]
    return out


def _cluster_params(spec: dict[str, Any], ctx: Context) -> list[dict[str, Any]]:
    if not ctx.clusters:
        raise ChartDeltaError(
            "This ApplicationSet uses the clusters generator; pass --clusters FILE listing your clusters "
            "(create one with 'chartdelta appset export-clusters').")
    selector = spec.get("selector") or {}
    out = []
    for c in ctx.clusters:
        if not _selector_matches(selector, c["labels"]):
            continue
        p: dict[str, Any] = {"name": c["name"], "nameNormalized": _dns(c["name"]), "server": c["server"],
                             "metadata": {"labels": c["labels"], "annotations": c["annotations"]}}
        if spec.get("values"):
            p["values"] = spec["values"]
        out.append(p)
    return out


def generate_params(gen: dict[str, Any], side: Side, ctx: Context) -> list[dict[str, Any]]:
    kinds = [k for k in gen if k not in ("selector",)]
    if len(kinds) != 1:
        raise ChartDeltaError(f"Each generator entry must have exactly one type, got: {', '.join(kinds) or 'none'}.")
    kind = kinds[0]
    spec = gen[kind] or {}
    if kind == "list":
        return [dict(e) for e in spec.get("elements") or []]
    if kind == "git":
        return _git_params(spec, side, ctx)
    if kind == "clusters":
        return _cluster_params(spec, ctx)
    if kind == "matrix":
        children = spec.get("generators") or []
        if len(children) != 2:
            raise ChartDeltaError("A matrix generator needs exactly two child generators.")
        first = generate_params(children[0], side, ctx)
        # The second generator may use values produced by the first.
        if ctx.go:
            seconds = render_objects(children[1], first, ctx.opts, "matrix generator")
        else:
            seconds = [render_fast(children[1], p) for p in first]
        out = []
        for p0, child in zip(first, seconds):
            for p1 in generate_params(child, side, ctx):
                out.append(_deep_merge(p0, p1))
        return out
    raise ChartDeltaError(
        f"The '{kind}' generator is not supported offline yet (supported: {', '.join(SUPPORTED)}). "
        "Use 'argocd appset generate' against a running Argo CD for this one.")


def load_appset(text: str, origin: str) -> dict[str, Any]:
    try:
        docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except yaml.YAMLError as exc:
        raise ChartDeltaError(f"{origin} is not valid YAML: {exc}") from None
    sets = [d for d in docs if d.get("kind") == "ApplicationSet"]
    if len(sets) != 1:
        raise ChartDeltaError(f"{origin} must contain exactly one ApplicationSet (found {len(sets)}).")
    return sets[0]


def generate_applications(appset: dict[str, Any], side: Side, ctx: Context) -> list[dict[str, Any]]:
    spec = appset.get("spec") or {}
    ctx.go = bool(spec.get("goTemplate"))
    if spec.get("templatePatch"):
        ctx.warnings.append("spec.templatePatch is not applied by this preview.")
    params: list[dict[str, Any]] = []
    for gen in spec.get("generators") or []:
        params += generate_params(gen, side, ctx)
    template = spec.get("template") or {}
    rendered = (render_objects(template, params, ctx.opts, "template") if ctx.go
                else [render_fast(template, p) for p in params])
    namespace = (appset.get("metadata") or {}).get("namespace", "argocd")
    apps, seen = [], set()
    for r in rendered:
        meta = dict(r.get("metadata") or {})
        meta.setdefault("namespace", namespace)
        name = meta.get("name", "")
        if not name:
            ctx.warnings.append(f"{side.label}: a generated Application has an empty name (missing template value?).")
        if name in seen:
            ctx.warnings.append(f"{side.label}: Application name '{name}' is generated more than once; Argo CD would reject this.")
            continue
        seen.add(name)
        apps.append({"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": meta, "spec": r.get("spec") or {}})
    return sorted(apps, key=lambda a: a["metadata"].get("name", ""))


# --------------------------------------------------------------------------- #
# Rendering one Application's sources
# --------------------------------------------------------------------------- #


@dataclass
class SourcePlan:
    kind: str                      # helm | directory | remote-chart | skip
    chart: Path | None = None
    values: list[Path] = field(default_factory=list)
    missing_values: list[str] = field(default_factory=list)
    used_values: list[str] = field(default_factory=list)
    opts: RenderOptions | None = None
    note: str = ""
    remote: str = ""
    recurse: bool = False


def _sources(app: dict[str, Any]) -> list[dict[str, Any]]:
    spec = app.get("spec") or {}
    return list(spec.get("sources") or ([spec["source"]] if spec.get("source") else []))


def plan_sources(app: dict[str, Any], side: Side, opts: RenderOptions, tmp: Path) -> list[SourcePlan]:
    spec = app.get("spec") or {}
    name = (app.get("metadata") or {}).get("name", "app")
    namespace = (spec.get("destination") or {}).get("namespace") or "default"
    sources = _sources(app)
    refs = {s["ref"]: side.repo(s.get("repoURL", "")) for s in sources if s.get("ref")}
    plans: list[SourcePlan] = []
    for idx, s in enumerate(sources):
        if s.get("ref") and not s.get("path") and not s.get("chart"):
            continue  # a values-only source
        helm = s.get("helm") or {}
        o = dataclasses.replace(
            opts, release=helm.get("releaseName") or name, namespace=namespace,
            include_crds=not helm.get("skipCrds", False),
            set_args=[f"{p['name']}={p.get('value', '')}" for p in helm.get("parameters") or [] if not p.get("forceString")],
            set_string_args=[f"{p['name']}={p.get('value', '')}" for p in helm.get("parameters") or [] if p.get("forceString")],
        )
        root = side.repo(s.get("repoURL", ""))
        plan = SourcePlan(kind="helm", opts=o)
        if s.get("chart"):
            plan.kind = "remote-chart"
            repo_url = s.get("repoURL", "")
            if "://" not in repo_url or repo_url.startswith("oci://"):
                plan.remote = "oci://" + re.sub(r"^oci://", "", repo_url).rstrip("/") + "/" + s["chart"]
                o.helm_args = list(o.helm_args) + ["--version", str(s.get("targetRevision", ""))]
            else:
                plan.remote = s["chart"]
                o.helm_args = list(o.helm_args) + ["--repo", repo_url, "--version", str(s.get("targetRevision", ""))]
            base_dir = None
        else:
            if root is None:
                plans.append(SourcePlan(kind="skip", note=f"source repo {s.get('repoURL')} has no local checkout (pass --repo URL=PATH)"))
                continue
            base_dir = (root / str(s.get("path", "")).lstrip("/")).resolve()
            if not base_dir.is_dir():
                raise ChartDeltaError(f"source path '{s.get('path')}' does not exist in {s.get('repoURL')}")
            plan.chart = base_dir
            if not (base_dir / "Chart.yaml").is_file():
                if any((base_dir / k).is_file() for k in ("kustomization.yaml", "kustomization.yml", "Kustomization")):
                    plans.append(SourcePlan(kind="skip", note=f"'{s.get('path')}' is a Kustomize source (not rendered by this preview)"))
                    continue
                plan.kind = "directory"
                plan.recurse = bool((s.get("directory") or {}).get("recurse"))
                plans.append(plan)
                continue
        for vf in helm.get("valueFiles") or []:
            vf = str(vf)
            if vf.startswith("$"):
                ref, _, rest = vf[1:].partition("/")
                ref_root = refs.get(ref)
                target = (ref_root / rest) if ref_root else None
            elif vf.startswith("/"):
                target = (root / vf.lstrip("/")) if root else None
            else:
                target = (base_dir / vf) if base_dir else None
            if target is not None and target.is_file():
                plan.values.append(target.resolve())
                plan.used_values.append(vf)
            elif helm.get("ignoreMissingValueFiles"):
                plan.missing_values.append(vf)
            else:
                raise ChartDeltaError(f"values file '{vf}' not found (and ignoreMissingValueFiles is not set)")
        for key in ("values", "valuesObject"):
            if helm.get(key):
                inline = tmp / f"{_dns(name)}-{idx}-{key}.yaml"
                inline.write_text(helm[key] if isinstance(helm[key], str) else yaml.safe_dump(helm[key]), encoding="utf-8")
                plan.values.append(inline)
        plans.append(plan)
    return plans


def _digest(app: dict[str, Any], plans: list[SourcePlan]) -> str:
    h = hashlib.sha256(json.dumps(app.get("spec"), sort_keys=True, default=str).encode())
    for p in plans:
        h.update(f"|{p.kind}|{p.remote}|{p.note}".encode())
        if p.chart is not None:
            for base, dnames, fnames in os.walk(p.chart):
                dnames[:] = sorted(d for d in dnames if not (Path(base) == p.chart and d == "charts"))
                for f in sorted(fnames):
                    fp = Path(base) / f
                    h.update(fp.relative_to(p.chart).as_posix().encode())
                    h.update(fp.read_bytes())
        for v in p.values:
            h.update(v.read_bytes())
    return h.hexdigest()


def render_plans(plans: list[SourcePlan], build_deps: bool, label: str, donors: list[SourcePlan] | None = None) -> tuple[str, list[str]]:
    texts, notes = [], []
    for i, p in enumerate(plans):
        if p.kind == "skip":
            notes.append(p.note)
        elif p.kind == "directory":
            assert p.chart is not None
            pattern = "**/*" if p.recurse else "*"
            for f in sorted(p.chart.glob(pattern)):
                if f.is_file() and f.suffix in (".yaml", ".yml", ".json"):
                    texts.append(f"---\n# Source: {f.relative_to(p.chart).as_posix()}\n{f.read_text(encoding='utf-8')}")
        elif p.kind == "remote-chart":
            assert p.opts is not None
            cmd = [p.opts.helm_bin, "template", p.opts.release, p.remote, "--namespace", p.opts.namespace]
            for v in p.values:
                cmd += ["-f", str(v)]
            for s in p.opts.set_args:
                cmd += ["--set", s]
            if p.opts.include_crds:
                cmd.append("--include-crds")
            cmd += p.opts.helm_args
            proc = _run(cmd)
            if proc.returncode != 0:
                raise ChartDeltaError(f"helm template failed for remote chart {p.remote}:\n{proc.stderr.strip()}")
            texts.append(proc.stdout)
        else:
            assert p.chart is not None and p.opts is not None
            donor = donors[i].chart if donors and i < len(donors) and donors[i].chart else None
            ensure_dependencies(p.chart, p.opts, build_deps, label, donor=donor)
            texts.append(helm_template(p.chart, p.values, p.opts, label))
    return "\n".join(texts), notes


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #


@dataclass
class AppResult:
    name: str
    status: str                 # added | removed | changed | unchanged | error
    destination: str = ""
    spec_diff: str = ""
    resources: DiffResult | None = None
    notes: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "status": self.status, "destination": self.destination,
                             "specDiff": self.spec_diff, "notes": self.notes, "error": self.error}
        d["resources"] = self.resources.to_dict() if self.resources else None
        return d


@dataclass
class AppsetReport:
    name: str
    base_label: str
    target_label: str
    apps: list[AppResult]
    warnings: list[str]

    def count(self, status: str) -> int:
        return sum(1 for a in self.apps if a.status == status)

    @property
    def has_changes(self) -> bool:
        return any(a.status != "unchanged" for a in self.apps)

    def to_dict(self) -> dict[str, Any]:
        return {"applicationSet": self.name, "base": self.base_label, "target": self.target_label,
                "summary": {s: self.count(s) for s in ("added", "removed", "changed", "unchanged", "error")},
                "warnings": self.warnings, "applications": [a.to_dict() for a in self.apps]}


def _dest(app: dict[str, Any]) -> str:
    d = (app.get("spec") or {}).get("destination") or {}
    return str(d.get("name") or d.get("server") or "")


def resolve_repos(appset_file: Path, repo_args: list[str]) -> dict[str, Path]:
    """Map repo URLs to local checkouts: explicit --repo first, then the repos we are sitting in."""
    repos: dict[str, Path] = {}
    for candidate in (appset_file.resolve(), Path.cwd()):
        found = repo_root_and_url(candidate)
        if found and found[1]:
            repos.setdefault(found[1], found[0])
    for arg in repo_args:
        url, sep, path = arg.rpartition("=")
        if not sep or not url:
            raise ChartDeltaError(f"--repo expects URL=PATH, got '{arg}'.")
        p = Path(path).expanduser().resolve()
        if not p.is_dir():
            raise ChartDeltaError(f"--repo path does not exist: {p}")
        repos[normalise_url(url)] = p
    return repos


def run_appset_generate(appset_file: Path, clusters_file: Path | None, repo_args: list[str], opts: RenderOptions
                        ) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """Return (applications, warnings, per-app source info) for the working tree."""
    repos = resolve_repos(appset_file, repo_args)
    side = Side("working tree", repos)
    ctx = Context(False, load_clusters(clusters_file), opts)
    appset = load_appset(appset_file.read_text(encoding="utf-8"), str(appset_file))
    apps = generate_applications(appset, side, ctx)
    info = []
    with tempfile.TemporaryDirectory(prefix="chartdelta-") as tmp:
        for app in apps:
            entry: dict[str, Any] = {"name": app["metadata"].get("name"), "destination": _dest(app),
                                     "namespace": ((app["spec"].get("destination") or {}).get("namespace") or ""),
                                     "paths": [s.get("path") or s.get("chart") for s in _sources(app) if s.get("path") or s.get("chart")]}
            try:
                plans = plan_sources(app, side, opts, Path(tmp))
                entry["valueFiles"] = [v for p in plans for v in p.used_values]
                entry["missingValueFiles"] = [m for p in plans for m in p.missing_values]
                entry["notes"] = [p.note for p in plans if p.note]
            except ChartDeltaError as exc:
                entry["error"] = str(exc)
            info.append(entry)
    return apps, ctx.warnings, info


def run_appset_diff(
    appset_file: Path, clusters_file: Path | None, repo_args: list[str], opts: RenderOptions, *,
    base_ref: str = "HEAD", build_deps: bool = True, context: int = 3, show_secrets: bool = False,
    ignore_labels: list[str] | None = None, only: list[str] | None = None,
) -> AppsetReport:
    repos = resolve_repos(appset_file, repo_args)
    clusters = load_clusters(clusters_file)
    target = Side("working tree", repos)
    appset_t = load_appset(appset_file.read_text(encoding="utf-8"), str(appset_file))
    name = (appset_t.get("metadata") or {}).get("name", appset_file.stem)

    with tempfile.TemporaryDirectory(prefix="chartdelta-") as tmp_s:
        tmp = Path(tmp_s)
        base_repos: dict[str, Path] = {}
        shas = []
        for i, (url, root) in enumerate(repos.items()):
            if repo_root_and_url(root) is None:
                base_repos[url] = root   # not a git checkout: same content on both sides
                continue
            sha = _export(root, base_ref, tmp / f"base{i}")
            shas.append(sha[:8])
            base_repos[url] = tmp / f"base{i}"
        base = Side(f"{base_ref}@{'/'.join(sorted(set(shas))) or 'n/a'}", base_repos)

        # The ApplicationSet file itself as of the base ref, when it lives in a mapped repo.
        appset_b: dict[str, Any] | None = appset_t
        found = repo_root_and_url(appset_file.resolve())
        if found:
            rel = appset_file.resolve().relative_to(found[0]).as_posix()
            shown = _git(found[0], "show", f"{base_ref}:{rel}")
            appset_b = load_appset(shown.stdout, f"{rel}@{base_ref}") if shown.returncode == 0 else None

        ctx_t, ctx_b = Context(False, clusters, opts), Context(False, clusters, opts)
        apps_t = {a["metadata"]["name"]: a for a in generate_applications(appset_t, target, ctx_t)}
        apps_b = {a["metadata"]["name"]: a for a in generate_applications(appset_b, base, ctx_b)} if appset_b else {}
        warnings = list(dict.fromkeys(ctx_t.warnings + ctx_b.warnings))
        if appset_b is None:
            warnings.append(f"The ApplicationSet file does not exist at {base_ref}; every Application counts as added.")

        results: list[AppResult] = []
        for app_name in sorted(set(apps_t) | set(apps_b)):
            if only and not any(fnmatch.fnmatch(app_name, pat) for pat in only):
                continue
            a_t, a_b = apps_t.get(app_name), apps_b.get(app_name)
            res = AppResult(app_name, "unchanged", destination=_dest(a_t or a_b or {}))
            try:
                plans_t = plan_sources(a_t, target, opts, tmp) if a_t else []
                plans_b = plan_sources(a_b, base, opts, tmp) if a_b else []
                if a_t and a_b:
                    y_b, y_t = to_yaml(a_b).splitlines(), to_yaml(a_t).splitlines()
                    if y_b != y_t:
                        res.spec_diff = "\n".join(difflib.unified_diff(
                            y_b, y_t, f"{base.label}: Application/{app_name}", f"{target.label}: Application/{app_name}",
                            n=context, lineterm=""))
                    if not res.spec_diff and _digest(a_b, plans_b) == _digest(a_t, plans_t):
                        res.notes.append("inputs identical; not rendered")
                        results.append(res)
                        continue
                text_t, notes_t = render_plans(plans_t, build_deps, f"target ({app_name})") if a_t else ("", [])
                text_b, notes_b = render_plans(plans_b, build_deps, f"base ({app_name})", donors=plans_t) if a_b else ("", [])
                res.notes += list(dict.fromkeys(notes_t + notes_b))
                res.resources = diff_manifests(text_b, text_t, base_label=base.label, target_label=target.label,
                                               context=context, show_secrets=show_secrets, ignore_labels=ignore_labels or [])
                if not a_b:
                    res.status = "added"
                elif not a_t:
                    res.status = "removed"
                elif res.spec_diff or res.resources.has_changes:
                    res.status = "changed"
            except ChartDeltaError as exc:
                res.status, res.error = "error", str(exc)
            results.append(res)
    return AppsetReport(name, base.label, target.label, results, warnings)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

_SYM = {"added": "+", "removed": "-", "changed": "~", "unchanged": "=", "error": "!"}


def render_appset_report(r: AppsetReport, *, markdown: bool = False, summary: bool = False, show_unchanged: bool = False) -> str:
    code = (lambda t: f"`{t}`") if markdown else (lambda t: t)
    L = [("## " if markdown else "") + f"ApplicationSet {r.name}: {r.base_label} -> {r.target_label}",
         "Applications: " + ", ".join(f"{r.count(s)} {s}" for s in ("added", "removed", "changed", "unchanged", "error"))]
    L += [f"WARNING: {w}" for w in r.warnings]
    for a in r.apps:
        if a.status == "unchanged" and not show_unchanged:
            continue
        L.append("")
        head = f"{_SYM[a.status]} {a.name}  ({a.status}" + (f", {a.destination}" if a.destination else "") + ")"
        L.append(("### " + head) if markdown else head)
        if a.error:
            L.append(f"  ERROR: {a.error}")
            continue
        L += [f"  note: {n}" for n in a.notes]
        if a.spec_diff:
            L.append("  Application spec changed" + (":" if not summary else ""))
            if not summary:
                L += ["```diff", a.spec_diff, "```"] if markdown else [a.spec_diff]
        res = a.resources
        if res is None:
            continue
        L.append(f"  resources: {res.count('added')} added, {res.count('removed')} removed, "
                 f"{res.count('changed')} changed, {res.unchanged} unchanged")
        for ch in res.changes:
            L.append(f"  {_SYM[ch.status]} {code(str(ch.key))}")
            if summary:
                L += [f"      {code(p)}" for p in ch.changed_paths[:8]]
                if len(ch.changed_paths) > 8:
                    L.append(f"      (+{len(ch.changed_paths) - 8} more)")
        if not summary and a.status == "changed":
            for ch in res.changes:
                L += ["", "```diff", ch.diff, "```"] if markdown else ["", ch.diff]
    if not r.has_changes:
        L += ["", "No Application is affected."]
    return "\n".join(L)
