"""Render a Helm chart twice (base vs. target) and diff the manifests per resource."""

from __future__ import annotations

import difflib
import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml


class ChartDeltaError(Exception):
    """A user-facing error (bad input, helm failure, git failure)."""


# --------------------------------------------------------------------------- #
# Options and data model
# --------------------------------------------------------------------------- #


@dataclass
class RenderOptions:
    release: str = "release"
    namespace: str = "default"
    set_args: list[str] = field(default_factory=list)
    set_string_args: list[str] = field(default_factory=list)
    include_crds: bool = False
    no_hooks: bool = False
    kube_version: str | None = None
    api_versions: list[str] = field(default_factory=list)
    helm_bin: str = "helm"
    helm_args: list[str] = field(default_factory=list)


@dataclass(frozen=True, order=True)
class ResourceKey:
    kind: str
    name: str
    namespace: str = ""
    group: str = ""

    def __str__(self) -> str:
        ns = f"{self.namespace}/" if self.namespace else ""
        return f"{self.kind}/{ns}{self.name}"


@dataclass
class Resource:
    key: ResourceKey
    api_version: str
    source: str
    body: dict[str, Any]
    is_hook: bool = False


@dataclass
class Change:
    key: ResourceKey
    status: str  # "added" | "removed" | "changed"
    api_version: str
    source: str
    is_hook: bool
    diff: str
    changed_paths: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.key.kind,
            "name": self.key.name,
            "namespace": self.key.namespace,
            "apiVersion": self.api_version,
            "status": self.status,
            "source": self.source,
            "hook": self.is_hook,
            "changedPaths": self.changed_paths,
            "diff": self.diff,
        }


@dataclass
class DiffResult:
    base_label: str
    target_label: str
    changes: list[Change]
    unchanged: int

    def count(self, status: str) -> int:
        return sum(1 for c in self.changes if c.status == status)

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base": self.base_label,
            "target": self.target_label,
            "summary": {
                "added": self.count("added"),
                "removed": self.count("removed"),
                "changed": self.count("changed"),
                "unchanged": self.unchanged,
            },
            "changes": [c.to_dict() for c in self.changes],
        }


# --------------------------------------------------------------------------- #
# Subprocess helpers
# --------------------------------------------------------------------------- #


def _run(cmd: list[str], cwd: Path | None = None, binary: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=not binary, check=False)
    except FileNotFoundError:
        raise ChartDeltaError(
            f"'{cmd[0]}' was not found on PATH. Install it first"
            + (" (https://helm.sh/docs/intro/install/)." if "helm" in cmd[0] else ".")
        ) from None


def _git(repo: Path, *args: str, binary: bool = False) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(repo), *args], binary=binary)


def _load_yaml_file(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ChartDeltaError(f"Cannot read {path}: {exc}") from None


def chart_metadata(chart: Path) -> dict[str, Any]:
    f = chart / "Chart.yaml"
    if not f.is_file():
        raise ChartDeltaError(f"{chart} is not a Helm chart (no Chart.yaml).")
    data = _load_yaml_file(f)
    if not isinstance(data, dict):
        raise ChartDeltaError(f"{f} is not a valid Chart.yaml.")
    return data


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def helm_template(chart: Path, values: Iterable[Path], opts: RenderOptions, label: str) -> str:
    cmd = [opts.helm_bin, "template", opts.release, str(chart), "--namespace", opts.namespace]
    for v in values:
        cmd += ["-f", str(v)]
    for s in opts.set_args:
        cmd += ["--set", s]
    for s in opts.set_string_args:
        cmd += ["--set-string", s]
    if opts.include_crds:
        cmd.append("--include-crds")
    if opts.no_hooks:
        cmd.append("--no-hooks")
    if opts.kube_version:
        cmd += ["--kube-version", opts.kube_version]
    for a in opts.api_versions:
        cmd += ["--api-versions", a]
    cmd += opts.helm_args
    proc = _run(cmd)
    if proc.returncode != 0:
        raise ChartDeltaError(f"helm template failed for the {label} side:\n{proc.stderr.strip()}")
    return proc.stdout


_DOC_SPLIT = re.compile(r"^---\s*$", re.MULTILINE)
_SOURCE = re.compile(r"^# Source: (.+)$", re.MULTILINE)


def parse_manifests(text: str) -> dict[ResourceKey, Resource]:
    """Split `helm template` output into resources keyed by kind/namespace/name."""
    out: dict[ResourceKey, Resource] = {}
    for chunk in _DOC_SPLIT.split(text):
        if not chunk.strip():
            continue
        try:
            doc = yaml.safe_load(chunk)
        except yaml.YAMLError as exc:
            raise ChartDeltaError(f"Rendered output is not valid YAML: {exc}") from None
        if not isinstance(doc, dict) or "kind" not in doc:
            continue
        m = _SOURCE.search(chunk)
        source = m.group(1).strip() if m else ""
        # A `kind: List` wrapper holds real resources; compare those, not the wrapper.
        if str(doc["kind"]).endswith("List") and isinstance(doc.get("items"), list):
            docs = [i for i in doc["items"] if isinstance(i, dict) and "kind" in i]
        else:
            docs = [doc]
        for item in docs:
            _add_resource(out, item, source)
    return out


def _add_resource(out: dict[ResourceKey, Resource], doc: dict[str, Any], source: str) -> None:
    if True:
        meta = doc.get("metadata") or {}
        api_version = str(doc.get("apiVersion", ""))
        key = ResourceKey(
            kind=str(doc["kind"]),
            name=str(meta.get("name") or meta.get("generateName") or "<unnamed>"),
            namespace=str(meta.get("namespace") or ""),
            group=api_version.split("/")[0] if "/" in api_version else "",
        )
        n = 2
        base_key = key
        while key in out:  # same resource rendered twice: keep both visible
            key = ResourceKey(base_key.kind, f"{base_key.name}#{n}", base_key.namespace, base_key.group)
            n += 1
        annotations = meta.get("annotations") or {}
        out[key] = Resource(
            key=key,
            api_version=api_version,
            source=source,
            body=doc,
            is_hook="helm.sh/hook" in annotations,
        )


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


class _Dumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.SafeDumper, data: str):
    if "\n" in data:
        # Block style keeps multi-line ConfigMap payloads diffable line by line.
        cleaned = "\n".join(line.rstrip() for line in data.split("\n"))
        return dumper.represent_scalar("tag:yaml.org,2002:str", cleaned, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_representer)


def _redact_secret(doc: dict[str, Any]) -> dict[str, Any]:
    doc = dict(doc)
    for fld in ("data", "stringData"):
        payload = doc.get(fld)
        if isinstance(payload, dict):
            doc[fld] = {
                k: "REDACTED sha256:%s len:%d"
                % (hashlib.sha256(str(v).encode()).hexdigest()[:12], len(str(v)))
                for k, v in payload.items()
            }
    return doc


def _strip_labels(node: Any, labels: set[str]) -> Any:
    if isinstance(node, dict):
        res = {}
        for k, v in node.items():
            if k in ("labels", "matchLabels", "annotations") and isinstance(v, dict):
                v = {lk: lv for lk, lv in v.items() if lk not in labels}
            res[k] = _strip_labels(v, labels)
        return res
    if isinstance(node, list):
        return [_strip_labels(i, labels) for i in node]
    return node


def normalise(res: Resource, show_secrets: bool, ignore_labels: set[str]) -> dict[str, Any]:
    doc = res.body
    if res.key.kind == "Secret" and not show_secrets:
        doc = _redact_secret(doc)
    if ignore_labels:
        doc = _strip_labels(doc, ignore_labels)
    return doc


def to_yaml(doc: dict[str, Any]) -> str:
    return yaml.dump(doc, Dumper=_Dumper, sort_keys=True, default_flow_style=False, width=10_000)


# --------------------------------------------------------------------------- #
# Structural diff (which fields changed)
# --------------------------------------------------------------------------- #


def _named(items: list[Any]) -> dict[str, Any] | None:
    """Lists like containers/env/volumes are better matched by name than by index."""
    if not items or not all(isinstance(i, dict) and isinstance(i.get("name"), str) for i in items):
        return None
    names = [i["name"] for i in items]
    if len(set(names)) != len(names):
        return None
    return {i["name"]: i for i in items}


def changed_paths(old: Any, new: Any, path: str = "") -> list[str]:
    if old == new:
        return []
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[str] = []
        for k in sorted(set(old) | set(new), key=str):
            sub = f"{path}.{k}" if path else str(k)
            if k not in old or k not in new:
                out.append(sub)
            else:
                out += changed_paths(old[k], new[k], sub)
        return out
    if isinstance(old, list) and isinstance(new, list):
        o_named, n_named = _named(old), _named(new)
        if o_named is not None and n_named is not None:
            out = []
            for name in sorted(set(o_named) | set(n_named)):
                sub = f"{path}[name={name}]"
                if name not in o_named or name not in n_named:
                    out.append(sub)
                else:
                    out += changed_paths(o_named[name], n_named[name], sub)
            # Same items, different order.
            return out or [path]
        out = []
        for i in range(max(len(old), len(new))):
            sub = f"{path}[{i}]"
            if i >= len(old) or i >= len(new):
                out.append(sub)
            else:
                out += changed_paths(old[i], new[i], sub)
        return out
    return [path or "<root>"]


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #


def diff_manifests(
    base_text: str,
    target_text: str,
    *,
    base_label: str = "base",
    target_label: str = "target",
    context: int = 3,
    show_secrets: bool = False,
    ignore_labels: Iterable[str] = (),
    kinds: Iterable[str] = (),
) -> DiffResult:
    ignore = set(ignore_labels)
    wanted = {k.lower() for k in kinds}
    base = parse_manifests(base_text)
    target = parse_manifests(target_text)
    changes: list[Change] = []
    unchanged = 0
    for key in sorted(set(base) | set(target)):
        if wanted and key.kind.lower() not in wanted:
            continue
        b, t = base.get(key), target.get(key)
        b_doc = normalise(b, show_secrets, ignore) if b else None
        t_doc = normalise(t, show_secrets, ignore) if t else None
        if b_doc == t_doc:
            unchanged += 1
            continue
        status = "added" if b is None else "removed" if t is None else "changed"
        ref = t or b
        assert ref is not None
        b_yaml = to_yaml(b_doc).splitlines() if b_doc is not None else []
        t_yaml = to_yaml(t_doc).splitlines() if t_doc is not None else []
        diff = "\n".join(
            difflib.unified_diff(
                b_yaml,
                t_yaml,
                fromfile=f"{base_label}: {key}" if b else "/dev/null",
                tofile=f"{target_label}: {key}" if t else "/dev/null",
                n=context,
                lineterm="",
            )
        )
        changes.append(
            Change(
                key=key,
                status=status,
                api_version=ref.api_version,
                source=ref.source,
                is_hook=ref.is_hook,
                diff=diff,
                changed_paths=changed_paths(b_doc, t_doc) if status == "changed" else [],
            )
        )
    return DiffResult(base_label, target_label, changes, unchanged)


# --------------------------------------------------------------------------- #
# Resolving the base side
# --------------------------------------------------------------------------- #


def _repo_root(path: Path) -> Path:
    proc = _git(path, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        raise ChartDeltaError(
            f"{path} is not inside a git repository, so there is no git 'original' to compare with.\n"
            "Use --base-chart DIR or --base-values FILE to say what to compare against."
        )
    return Path(proc.stdout.strip()).resolve()


def _exists_at(repo: Path, ref: str, rel: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{ref}:{rel}").returncode == 0


def _rel(repo: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(repo).as_posix()
    except ValueError:
        return None


def _local_deps(meta: dict[str, Any]) -> list[str]:
    deps = meta.get("dependencies") or []
    return [
        d["repository"][len("file://") :]
        for d in deps
        if isinstance(d, dict) and str(d.get("repository", "")).startswith("file://")
    ]


def export_from_git(
    chart: Path, values: list[Path], ref: str, dest: Path
) -> tuple[Path, list[Path], str]:
    """Materialise the chart (and values files, and file:// deps) as they were at `ref`.

    Returns (base_chart_dir, base_values, resolved_sha).
    """
    repo = _repo_root(chart)
    sha_proc = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if sha_proc.returncode != 0:
        raise ChartDeltaError(f"Unknown git ref '{ref}': {sha_proc.stderr.strip()}")
    sha = sha_proc.stdout.strip()

    chart_rel = _rel(repo, chart)
    assert chart_rel is not None
    chart_rel = chart_rel if chart_rel != "." else ""
    probe = f"{chart_rel}/Chart.yaml" if chart_rel else "Chart.yaml"
    if not _exists_at(repo, sha, probe):
        raise ChartDeltaError(
            f"The chart '{chart_rel or '.'}' does not exist at {ref} ({sha[:8]}); nothing to compare with."
        )

    paths = [chart_rel or "."]
    meta_proc = _git(repo, "show", f"{sha}:{probe}")
    try:
        base_meta = yaml.safe_load(meta_proc.stdout) or {}
    except yaml.YAMLError:
        base_meta = {}
    for dep in _local_deps(base_meta):
        dep_rel = _rel(repo, chart / dep)
        if dep_rel and _exists_at(repo, sha, dep_rel):
            paths.append(dep_rel)

    base_values: list[Path] = []
    for v in values:
        v_rel = _rel(repo, v)
        if v_rel and _exists_at(repo, sha, v_rel):
            paths.append(v_rel)
            base_values.append(dest / v_rel)
        else:
            # Untracked or new values file: the same file is used on both sides.
            base_values.append(v)

    archive = _git(repo, "archive", "--format=tar", sha, "--", *paths, binary=True)
    if archive.returncode != 0:
        raise ChartDeltaError(f"git archive failed: {archive.stderr.decode(errors='replace').strip()}")
    tar = subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, capture_output=True)
    if tar.returncode != 0:
        raise ChartDeltaError(f"Could not unpack git archive: {tar.stderr.decode(errors='replace').strip()}")
    return (dest / chart_rel if chart_rel else dest), base_values, sha


def ensure_dependencies(
    chart: Path, opts: RenderOptions, build: bool, label: str, donor: Path | None = None
) -> None:
    """Make sure declared subcharts are present in charts/ before templating."""
    deps = chart_metadata(chart).get("dependencies") or []
    if not deps:
        return
    charts_dir = chart / "charts"
    if charts_dir.is_dir() and any(charts_dir.iterdir()):
        return
    # Reuse already-downloaded subcharts when the lock file is identical.
    if donor is not None and (donor / "charts").is_dir():
        lock_a, lock_b = chart / "Chart.lock", donor / "Chart.lock"
        same_lock = lock_a.is_file() and lock_b.is_file() and lock_a.read_bytes() == lock_b.read_bytes()
        same_decl = deps == (chart_metadata(donor).get("dependencies") or [])
        if same_lock or same_decl:
            shutil.copytree(donor / "charts", charts_dir, dirs_exist_ok=True)
            return
    if not build:
        raise ChartDeltaError(
            f"The {label} chart declares dependencies but charts/ is empty.\n"
            "Re-run with --build-deps (runs 'helm dependency build', needs repo access)."
        )
    proc = _run([opts.helm_bin, "dependency", "build", str(chart)])
    if proc.returncode != 0:
        raise ChartDeltaError(f"helm dependency build failed for the {label} chart:\n{proc.stderr.strip()}")


def merge_base(chart: Path, ref: str) -> str:
    repo = _repo_root(chart)
    proc = _git(repo, "merge-base", ref, "HEAD")
    if proc.returncode != 0:
        raise ChartDeltaError(f"Cannot find a merge base between {ref} and HEAD: {proc.stderr.strip()}")
    return proc.stdout.strip()


def run_diff(
    chart: Path,
    values: list[Path],
    opts: RenderOptions,
    *,
    base_ref: str | None = None,
    base_chart: Path | None = None,
    base_values: list[Path] | None = None,
    use_merge_base: bool = False,
    build_deps: bool = False,
    context: int = 3,
    show_secrets: bool = False,
    ignore_labels: Iterable[str] = (),
    kinds: Iterable[str] = (),
) -> DiffResult:
    """Render base and target and return the per-resource diff.

    Base resolution, in order:
      1. base_chart given          -> that directory (values: base_values or `values`)
      2. base_ref given            -> the chart + values files as of that git ref
      3. only base_values given    -> same chart, different values (pure values diff)
      4. nothing given             -> git HEAD
    """
    chart = chart.resolve()
    chart_metadata(chart)  # validates
    for v in list(values) + list(base_values or []):
        if not v.is_file():
            raise ChartDeltaError(f"Values file not found: {v}")
    if base_chart is not None and base_ref is not None:
        raise ChartDeltaError("Use either --base (git ref) or --base-chart (directory), not both.")

    target_label = "working tree"
    with tempfile.TemporaryDirectory(prefix="chartdelta-") as tmp:
        if base_chart is not None:
            b_chart = base_chart.resolve()
            chart_metadata(b_chart)
            b_values = base_values if base_values is not None else values
            base_label = str(base_chart)
            target_label = str(chart)
        elif base_ref is None and base_values is not None:
            b_chart, b_values = chart, base_values
            base_label = "base values"
            target_label = "target values"
        else:
            ref = base_ref or "HEAD"
            label_ref = ref
            if use_merge_base:
                ref = merge_base(chart, ref)
                label_ref = f"merge-base({label_ref})"
            b_chart, git_values, sha = export_from_git(chart, values, ref, Path(tmp))
            b_values = base_values if base_values is not None else git_values
            base_label = f"{label_ref}@{sha[:8]}"

        ensure_dependencies(chart, opts, build_deps, "target")
        if b_chart != chart:
            ensure_dependencies(b_chart, opts, build_deps, "base", donor=chart)

        target_text = helm_template(chart, values, opts, "target")
        base_text = helm_template(b_chart, b_values, opts, "base")

    return diff_manifests(
        base_text,
        target_text,
        base_label=base_label,
        target_label=target_label,
        context=context,
        show_secrets=show_secrets,
        ignore_labels=ignore_labels,
        kinds=kinds,
    )


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #


def render_chart(
    chart: Path,
    values: list[Path],
    opts: RenderOptions,
    *,
    kinds: Iterable[str] = (),
    names: Iterable[str] = (),
    show_secrets: bool = False,
    build_deps: bool = False,
) -> list[tuple[Resource, dict[str, Any]]]:
    """Render the chart with the given values; return (resource, manifest) pairs.

    `names` match as substrings of the resource name. Secret values are
    replaced by hashes unless show_secrets is set.
    """
    chart = chart.resolve()
    chart_metadata(chart)
    for v in values:
        if not v.is_file():
            raise ChartDeltaError(f"Values file not found: {v}")
    ensure_dependencies(chart, opts, build_deps, "target")
    wanted_kinds = {k.lower() for k in kinds}
    wanted_names = list(names)
    out = []
    for res in parse_manifests(helm_template(chart, values, opts, "target")).values():
        if wanted_kinds and res.key.kind.lower() not in wanted_kinds:
            continue
        if wanted_names and not any(n in res.key.name for n in wanted_names):
            continue
        out.append((res, normalise(res, show_secrets, set())))
    return out


# --------------------------------------------------------------------------- #
# Inspect
# --------------------------------------------------------------------------- #


def _shape(value: Any) -> str:
    if isinstance(value, dict):
        return f"map({len(value)} keys)"
    if isinstance(value, list):
        return f"list({len(value)})"
    if value is None:
        return "null"
    return type(value).__name__


def inspect_chart(chart: Path, values: list[Path], opts: RenderOptions, render: bool = True) -> dict[str, Any]:
    chart = chart.resolve()
    meta = chart_metadata(chart)
    defaults = _load_yaml_file(chart / "values.yaml") if (chart / "values.yaml").is_file() else {}
    templates_dir = chart / "templates"
    info: dict[str, Any] = {
        "path": str(chart),
        "name": meta.get("name"),
        "version": meta.get("version"),
        "appVersion": meta.get("appVersion"),
        "type": meta.get("type", "application"),
        "description": meta.get("description"),
        "dependencies": [
            {k: d.get(k) for k in ("name", "version", "repository", "condition", "alias") if d.get(k)}
            for d in (meta.get("dependencies") or [])
            if isinstance(d, dict)
        ],
        "valuesFiles": sorted(p.name for p in chart.glob("values*.y*ml")),
        "hasSchema": (chart / "values.schema.json").is_file(),
        "values": {k: _shape(v) for k, v in (defaults or {}).items()} if isinstance(defaults, dict) else {},
        "templates": sorted(
            p.relative_to(templates_dir).as_posix() for p in templates_dir.rglob("*") if p.is_file()
        )
        if templates_dir.is_dir()
        else [],
    }
    if render:
        try:
            text = helm_template(chart, values, opts, "inspected")
            info["resources"] = [
                {"kind": r.key.kind, "name": r.key.name, "namespace": r.key.namespace, "source": r.source, "hook": r.is_hook}
                for r in parse_manifests(text).values()
            ]
        except ChartDeltaError as exc:
            info["renderError"] = str(exc)
    return info
