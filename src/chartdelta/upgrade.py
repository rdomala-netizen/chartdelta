"""Compare two versions of a chart for an upgrade: manifests, CRDs, defaults, your values."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core import (
    Change, ChartDeltaError, DiffResult, RenderOptions, _load_yaml_file, _run, chart_metadata,
    diff_manifests, ensure_dependencies, helm_template, parse_manifests,
)
from .values_check import Finding, check_values

CRD = "CustomResourceDefinition"
VERSION_LABELS = ["helm.sh/chart", "app.kubernetes.io/version"]


@dataclass
class CrdChange:
    name: str
    status: str  # added | removed | changed
    versions_added: list[str] = field(default_factory=list)
    versions_removed: list[str] = field(default_factory=list)
    storage_from: str = ""
    storage_to: str = ""
    no_longer_served: list[str] = field(default_factory=list)
    served_versions_removed: list[str] = field(default_factory=list)  # subset of versions_removed
    schema_paths_changed: int = 0
    diff: str = ""

    @property
    def severity(self) -> str:
        """breaking: clients using an API version lose it. check: depends on cluster state."""
        if self.status == "removed" or self.no_longer_served or self.served_versions_removed:
            return "breaking"
        if self.versions_removed or (self.storage_from and self.storage_from != self.storage_to):
            return "check"
        return "none"

    @property
    def risky(self) -> bool:
        return self.severity != "none"

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["risky"] = self.risky
        d["severity"] = self.severity
        return d


@dataclass
class UpgradeReport:
    chart: str
    from_version: str
    to_version: str
    metadata_changes: dict[str, list[Any]]      # field -> [old, new]
    dependency_changes: dict[str, list[Any]]    # subchart -> [old version, new version]
    crds: list[CrdChange]
    crds_in_crds_dir: bool
    resources: DiffResult                        # CRDs excluded
    defaults_added: dict[str, Any]
    defaults_removed: dict[str, Any]
    defaults_changed: dict[str, list[Any]]
    values_findings: list[tuple[Finding, bool]]  # (finding, newly broken by the upgrade)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chart": self.chart, "from": self.from_version, "to": self.to_version,
            "metadataChanges": self.metadata_changes, "dependencyChanges": self.dependency_changes,
            "crds": [c.to_dict() for c in self.crds], "crdsInCrdsDir": self.crds_in_crds_dir,
            "resources": self.resources.to_dict(),
            "defaults": {"added": self.defaults_added, "removed": self.defaults_removed, "changed": self.defaults_changed},
            "valuesFindings": [{**f.to_dict(), "newInThisUpgrade": new} for f, new in self.values_findings],
        }


def pull_chart(ref: str, version: str, dest: Path, opts: RenderOptions, repo: str | None = None) -> Path:
    """`helm pull --untar` one version of a chart into dest; return the chart directory."""
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [opts.helm_bin, "pull", ref, "--version", version, "--untar", "--untardir", str(dest)]
    if repo:
        cmd += ["--repo", repo]
    proc = _run(cmd)
    if proc.returncode != 0:
        raise ChartDeltaError(f"helm pull {ref} --version {version} failed:\n{proc.stderr.strip()}")
    found = [p for p in dest.iterdir() if (p / "Chart.yaml").is_file()]
    if len(found) != 1:
        raise ChartDeltaError(f"helm pull {ref} --version {version} did not produce a chart directory.")
    return found[0]


def _resolve(side: str, ref: str | None, dest: Path, opts: RenderOptions, repo: str | None) -> Path:
    as_dir = Path(side)
    if (as_dir / "Chart.yaml").is_file():
        return as_dir.resolve()
    if not ref:
        raise ChartDeltaError(
            f"'{side}' is not a chart directory. Give the chart to pull (e.g. repo/name or oci://...) "
            "as the first argument, or pass directories to --from/--to."
        )
    return pull_chart(ref, side, dest, opts, repo)


def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(node, dict) and node:
        out: dict[str, Any] = {}
        for k, v in node.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return {prefix: node}


def _defaults(chart: Path) -> dict[str, Any]:
    f = chart / "values.yaml"
    data = _load_yaml_file(f) if f.is_file() else {}
    return _flatten(data) if isinstance(data, dict) and data else {}


def _crd_versions(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    versions = (body.get("spec") or {}).get("versions") or []
    return {str(v.get("name")): v for v in versions if isinstance(v, dict)}


def _crd_change(ch: Change, old: dict[str, Any] | None, new: dict[str, Any] | None) -> CrdChange:
    out = CrdChange(name=ch.key.name, status=ch.status, diff=ch.diff)
    ov, nv = _crd_versions(old or {}), _crd_versions(new or {})
    if old is not None and new is not None:
        out.versions_added = sorted(set(nv) - set(ov))
        out.versions_removed = sorted(set(ov) - set(nv))
        out.served_versions_removed = [n for n in out.versions_removed if ov[n].get("served")]
        out.no_longer_served = sorted(n for n in set(ov) & set(nv) if ov[n].get("served") and not nv[n].get("served"))
        out.storage_from = next((n for n, v in ov.items() if v.get("storage")), "")
        out.storage_to = next((n for n, v in nv.items() if v.get("storage")), "")
        out.schema_paths_changed = len(ch.changed_paths)
    return out


def run_upgrade(
    ref: str | None, from_side: str, to_side: str, values: list[Path], opts: RenderOptions,
    *, repo: str | None = None, workdir: Path, build_deps: bool = False, context: int = 3,
    show_secrets: bool = False, keep_version_labels: bool = False,
) -> UpgradeReport:
    for v in values:
        if not v.is_file():
            raise ChartDeltaError(f"Values file not found: {v}")
    old = _resolve(from_side, ref, workdir / "from", opts, repo)
    new = _resolve(to_side, ref, workdir / "to", opts, repo)
    old_meta, new_meta = chart_metadata(old), chart_metadata(new)
    ensure_dependencies(old, opts, build_deps, "from")
    ensure_dependencies(new, opts, build_deps, "to")

    crd_opts = dataclasses.replace(opts, include_crds=True)
    old_text = helm_template(old, values, crd_opts, "from")
    new_text = helm_template(new, values, crd_opts, "to")
    from_v, to_v = str(old_meta.get("version")), str(new_meta.get("version"))
    full = diff_manifests(
        old_text, new_text, base_label=from_v, target_label=to_v, context=context,
        show_secrets=show_secrets, ignore_labels=() if keep_version_labels else VERSION_LABELS,
    )
    old_res, new_res = parse_manifests(old_text), parse_manifests(new_text)
    crds = [
        _crd_change(c, old_res[c.key].body if c.key in old_res else None, new_res[c.key].body if c.key in new_res else None)
        for c in full.changes if c.key.kind == CRD
    ]
    unchanged_crds = sum(1 for k in new_res if k.kind == CRD and k in old_res) - sum(1 for c in crds if c.status == "changed")
    resources = DiffResult(from_v, to_v, [c for c in full.changes if c.key.kind != CRD], full.unchanged - unchanged_crds)

    meta_changes = {
        k: [old_meta.get(k), new_meta.get(k)]
        for k in ("version", "appVersion", "kubeVersion", "apiVersion", "type", "deprecated")
        if old_meta.get(k) != new_meta.get(k)
    }
    def deps(meta: dict[str, Any]) -> dict[str, Any]:
        return {str(d.get("alias") or d.get("name")): d.get("version") for d in (meta.get("dependencies") or []) if isinstance(d, dict)}
    od, nd = deps(old_meta), deps(new_meta)
    dep_changes = {k: [od.get(k), nd.get(k)] for k in sorted(set(od) | set(nd)) if od.get(k) != nd.get(k)}

    o_def, n_def = _defaults(old), _defaults(new)
    findings: list[tuple[Finding, bool]] = []
    if values:
        before = {f.path for f in check_values(old, values, opts)}
        findings = [(f, f.path not in before) for f in check_values(new, values, opts)]

    return UpgradeReport(
        chart=str(new_meta.get("name")), from_version=from_v, to_version=to_v,
        metadata_changes=meta_changes, dependency_changes=dep_changes, crds=crds,
        crds_in_crds_dir=(new / "crds").is_dir() or (old / "crds").is_dir(),
        resources=resources,
        defaults_added={k: n_def[k] for k in sorted(set(n_def) - set(o_def))},
        defaults_removed={k: o_def[k] for k in sorted(set(o_def) - set(n_def))},
        defaults_changed={k: [o_def[k], n_def[k]] for k in sorted(set(o_def) & set(n_def)) if o_def[k] != n_def[k]},
        values_findings=findings,
    )


def _short(v: Any) -> str:
    s = repr(v) if not isinstance(v, str) else (v if v else "''")
    return s if len(s) <= 60 else s[:57] + "..."


def render_report(r: UpgradeReport, *, markdown: bool = False, crd_diff: bool = False, summary: bool = False) -> str:
    h = (lambda t: f"\n### {t}") if markdown else (lambda t: f"\n== {t} ==")
    code = (lambda t: f"`{t}`") if markdown else (lambda t: t)
    L = [("## " if markdown else "") + f"{r.chart} upgrade: {r.from_version} -> {r.to_version}"]

    L.append(h("Chart metadata"))
    L += [f"- {k}: {_short(a)} -> {_short(b)}" for k, (a, b) in r.metadata_changes.items()] or ["- no changes"]
    L += [f"- dependency {code(k)}: {a or 'absent'} -> {b or 'removed'}" for k, (a, b) in r.dependency_changes.items()]

    L.append(h(f"CRDs ({len(r.crds)} changed)"))
    for c in r.crds:
        mark = {"breaking": "BREAKING ", "check": "CHECK ", "none": ""}[c.severity]
        if c.status != "changed":
            L.append(f"- {mark}{c.status}: {code(c.name)}")
            continue
        bits = []
        for v in c.versions_removed:
            bits.append(f"version removed: {v}" + (" (was served)" if v in c.served_versions_removed else " (was not served)"))
        if c.no_longer_served:
            bits.append(f"no longer served: {', '.join(c.no_longer_served)}")
        if c.storage_from != c.storage_to:
            bits.append(f"storage version {c.storage_from} -> {c.storage_to}")
        if c.versions_added:
            bits.append(f"versions added: {', '.join(c.versions_added)}")
        bits.append(f"{c.schema_paths_changed} schema fields changed")
        L.append(f"- {mark}changed: {code(c.name)} ({'; '.join(bits)})")
    if any(c.severity == "breaking" for c in r.crds):
        L.append("BREAKING: an API version that clients can use today is gone or no longer served. "
                 "Move manifests and objects to a served version first.")
    if any(c.severity == "check" for c in r.crds):
        L.append("CHECK: storage version changed or an unserved version was removed. Safe unless the cluster "
                 "still lists a removed version in the CRD's status.storedVersions.")
    if not r.crds:
        L.append("- no CRD changes")
    elif r.crds_in_crds_dir:
        L.append("Note: CRDs in the chart's crds/ directory are NOT updated by 'helm upgrade'; "
                 "apply them separately. Argo CD does apply them when it syncs the chart.")
    if crd_diff and not summary:
        for c in r.crds:
            L += ["", "```diff", c.diff, "```"] if markdown else ["", c.diff]

    res = r.resources
    L.append(h(f"Rendered resources with your values ({res.count('added')} added, {res.count('removed')} removed, "
               f"{res.count('changed')} changed, {res.unchanged} unchanged)"))
    for ch in res.changes:
        L.append(f"- {ch.status}: {code(str(ch.key))}" + (" (hook)" if ch.is_hook else ""))
        if summary or markdown:
            L += [f"    - {code(p)}" for p in ch.changed_paths[:8]]
            if len(ch.changed_paths) > 8:
                L.append(f"    - (+{len(ch.changed_paths) - 8} more)")
    if not summary:
        for ch in res.changes:
            if markdown:
                L += ["", f"<details><summary>{ch.status}: <code>{ch.key}</code></summary>", "", "```diff", ch.diff, "```", "", "</details>"]
            else:
                L += ["", ch.diff]

    L.append(h("Default values"))
    L += [f"- removed: {code(k)} (was {_short(v)})" for k, v in r.defaults_removed.items()]
    L += [f"- changed: {code(k)}: {_short(a)} -> {_short(b)}" for k, (a, b) in r.defaults_changed.items()]
    L += [f"- added: {code(k)} = {_short(v)}" for k, v in r.defaults_added.items()]
    if not (r.defaults_removed or r.defaults_changed or r.defaults_added):
        L.append("- no changes")

    if r.values_findings:
        L.append(h("Your values files against the new version"))
        for f, new in r.values_findings:
            when = "stops working in this upgrade" if new else "already ignored before the upgrade"
            L.append(f"- {code(f.path)}: {f.message} [{when}]" + (f" — {f.suggestion}" if f.suggestion else ""))
    return "\n".join(L).strip()
