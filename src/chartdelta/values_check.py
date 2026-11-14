"""Find keys in values files that the chart silently ignores.

Helm merges whatever you give it: a typo (`replicas` for `replicaCount`) or a
wrongly nested key is accepted and simply never read. This module flags such
keys in three steps:

1. static: the key is not in the chart's default values and no template
   references `.Values.<path>`;
2. render probe: the chart is rendered again with that key removed. If the
   output is identical, the key has no effect with these values;
3. suggestions: a close sibling name, or the place the key really lives.
"""

from __future__ import annotations

import difflib
import re
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .core import ChartDeltaError, RenderOptions, _load_yaml_file, chart_metadata, helm_template

_VALUES_REF = re.compile(r"\.Values((?:\.[A-Za-z_][A-Za-z0-9_-]*)+)")


@dataclass
class ChartInfo:
    name: str
    defaults: dict[str, Any]
    refs: set[tuple[str, ...]]
    subcharts: dict[str, "ChartInfo"] = field(default_factory=dict)
    is_library: bool = False


@dataclass
class Finding:
    path: str
    kind: str  # "unknown-key" | "type-mismatch"
    message: str
    file: str = ""
    suggestion: str = ""
    # True: removing the key changes nothing. None: not probed.
    no_effect: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "kind": self.kind, "message": self.message,
            "file": self.file, "suggestion": self.suggestion, "noEffect": self.no_effect,
        }


# --------------------------------------------------------------------------- #
# Loading the chart's idea of valid values
# --------------------------------------------------------------------------- #


def _template_refs(chart: Path) -> set[tuple[str, ...]]:
    refs: set[tuple[str, ...]] = set()
    tdir = chart / "templates"
    if not tdir.is_dir():
        return refs
    for f in tdir.rglob("*"):
        if f.is_file() and f.suffix in (".yaml", ".yml", ".tpl", ".txt"):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in _VALUES_REF.finditer(text):
                refs.add(tuple(m.group(1).strip(".").split(".")))
    return refs


def _find_subchart(chart: Path, name: str, tmp: Path) -> Path | None:
    charts = chart / "charts"
    if (charts / name / "Chart.yaml").is_file():
        return charts / name
    for tgz in sorted(charts.glob(f"{name}-*.tgz")):
        dest = tmp / f"{chart.name}-{tgz.stem}"
        try:
            with tarfile.open(tgz) as tar:
                try:
                    tar.extractall(dest, filter="data")
                except TypeError:  # Python < 3.12 without the filter argument
                    tar.extractall(dest)
        except (tarfile.TarError, OSError):
            continue
        if (dest / name / "Chart.yaml").is_file():
            return dest / name
    return None


def load_chart_info(chart: Path, tmp: Path) -> ChartInfo:
    meta = chart_metadata(chart)
    defaults = _load_yaml_file(chart / "values.yaml") if (chart / "values.yaml").is_file() else {}
    info = ChartInfo(
        name=str(meta.get("name", chart.name)),
        defaults=defaults if isinstance(defaults, dict) else {},
        refs=_template_refs(chart),
        is_library=meta.get("type") == "library",
    )
    for dep in meta.get("dependencies") or []:
        if not isinstance(dep, dict) or not dep.get("name"):
            continue
        key = str(dep.get("alias") or dep["name"])
        # `condition: redis.enabled` is a values path Helm itself reads.
        for cond in str(dep.get("condition") or "").split(","):
            if cond.strip():
                info.refs.add(tuple(cond.strip().split(".")))
        sub_dir = _find_subchart(chart, str(dep["name"]), tmp)
        if sub_dir is None:
            info.subcharts[key] = ChartInfo(name=key, defaults={}, refs=set(), is_library=False)
            info.subcharts[key].defaults = {"__unresolved__": True}
            continue
        sub = load_chart_info(sub_dir, tmp)
        if sub.is_library:
            info.refs |= sub.refs  # library helpers read the parent's .Values
        else:
            info.subcharts[key] = sub
    return info


# --------------------------------------------------------------------------- #
# Static check
# --------------------------------------------------------------------------- #


def _kind(v: Any) -> str:
    if isinstance(v, dict):
        return "map"
    if isinstance(v, list):
        return "list"
    if isinstance(v, bool):
        return "bool"
    return "scalar"


def _all_paths(tree: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    out = []
    for k, v in tree.items():
        p = prefix + (str(k),)
        out.append(p)
        if isinstance(v, dict):
            out += _all_paths(v, p)
    return out


def _suggest(key: str, here: tuple[str, ...], siblings: list[str], info: ChartInfo) -> str:
    close = difflib.get_close_matches(key, siblings, n=1, cutoff=0.6)
    if close:
        return f"did you mean '{'.'.join(here + (close[0],))}'?"
    known = _all_paths(info.defaults) + sorted(info.refs)
    elsewhere = sorted({".".join(p) for p in known if p[-1] == key and p[:-1] != here})
    if elsewhere:
        return f"'{key}' exists at '{elsewhere[0]}' — wrong nesting?"
    lower = {s.lower(): s for s in siblings}
    if key.lower() in lower:
        return f"did you mean '{'.'.join(here + (lower[key.lower()],))}'? (keys are case-sensitive)"
    return ""


def _walk(
    user: dict[str, Any], defaults: dict[str, Any], info: ChartInfo,
    here: tuple[str, ...], display_prefix: str, out: list[Finding], candidates: list[tuple[str, ...]],
) -> None:
    for key, value in user.items():
        key = str(key)
        path = here + (key,)
        shown = display_prefix + ".".join(path)
        if not here:
            if key == "global":
                continue  # shared with every subchart; cannot be judged from one chart
            if key in info.subcharts:
                sub = info.subcharts[key]
                if "__unresolved__" in sub.defaults:
                    continue  # subchart not downloaded: nothing to compare with
                if isinstance(value, dict):
                    merged = dict(sub.defaults)
                    parent_side = defaults.get(key)
                    if isinstance(parent_side, dict):
                        merged = {**merged, **parent_side}
                    # Parent-side references such as `condition: cache.enabled` count too.
                    parent_refs = {r[1:] for r in info.refs if len(r) > 1 and r[0] == key}
                    sub_view = ChartInfo(sub.name, merged, sub.refs | parent_refs, sub.subcharts)
                    sub_candidates: list[tuple[str, ...]] = []
                    _walk(value, merged, sub_view, (), f"{shown}.", out, sub_candidates)
                    candidates += [(key,) + c for c in sub_candidates]
                continue
        if key in defaults:
            default = defaults[key]
            if default is None or value is None:
                continue
            if isinstance(default, dict) and isinstance(value, dict):
                if default:  # an empty default map is free-form (annotations, nodeSelector, ...)
                    _walk(value, default, info, path, display_prefix, out, candidates)
                continue
            dk, vk = _kind(default), _kind(value)
            # Only structural mismatches; "8080" vs 8080 is usually harmless.
            if dk != vk and not (isinstance(default, (dict, list)) and not default):
                if "map" in (dk, vk) or "list" in (dk, vk) or "bool" in (dk, vk):
                    out.append(Finding(shown, "type-mismatch",
                                       f"chart default is a {dk}, values file gives a {vk}"))
            continue
        # Not in the defaults. Is it read by a template?
        if any(r[: len(path)] == path for r in info.refs):
            continue
        siblings = sorted(set(map(str, defaults)) | {r[len(here)] for r in info.refs if len(r) > len(here) and r[: len(here)] == here})
        parent_read_whole = any(path[: len(r)] == r for r in info.refs if len(r) < len(path))
        msg = "not in the chart's values.yaml and not referenced by any template"
        if parent_read_whole:
            msg = "not in the chart's values.yaml; a parent map is passed through whole, so it may still be used"
        out.append(Finding(shown, "unknown-key", msg, suggestion=_suggest(key, here, siblings, info)))
        candidates.append(path)


def _nested(path: tuple[str, ...], leaf: Any) -> dict[str, Any]:
    node: Any = leaf
    for part in reversed(path):
        node = {part: node}
    return node


def check_values(
    chart: Path, values: list[Path], opts: RenderOptions, *, probe: bool = True
) -> list[Finding]:
    """Return findings for keys in `values` that the chart appears to ignore."""
    chart = chart.resolve()
    if not values:
        raise ChartDeltaError("Nothing to check: pass at least one values file with -f.")
    findings: list[Finding] = []
    with tempfile.TemporaryDirectory(prefix="chartdelta-") as tmp:
        info = load_chart_info(chart, Path(tmp))
        candidates: list[tuple[str, ...]] = []
        for vf in values:
            if not vf.is_file():
                raise ChartDeltaError(f"Values file not found: {vf}")
            data = _load_yaml_file(vf)
            if data is None:
                continue
            if not isinstance(data, dict):
                raise ChartDeltaError(f"{vf} must contain a YAML map at the top level.")
            before = len(findings)
            _walk(data, info.defaults, info, (), "", findings, candidates)
            for f in findings[before:]:
                f.file = str(vf)

        unknown = [f for f in findings if f.kind == "unknown-key"]
        if probe and unknown:
            try:
                baseline = helm_template(chart, values, opts, "target")
            except ChartDeltaError:
                baseline = None  # chart does not render; keep the static result
            if baseline is not None:
                for f, path in zip(unknown, candidates):
                    probe_file = Path(tmp) / "probe.yaml"
                    # A null in the last values file removes the key (Helm semantics).
                    probe_file.write_text(yaml.safe_dump(_nested(path, None)), encoding="utf-8")
                    try:
                        without = helm_template(chart, values + [probe_file], opts, "probe")
                    except ChartDeltaError:
                        f.no_effect = False  # removing it breaks rendering: it is used
                        continue
                    f.no_effect = without == baseline
                # A key whose removal changes the output is in use after all.
                findings = [f for f in findings if not (f.kind == "unknown-key" and f.no_effect is False)]
    return findings


def render_findings(findings: list[Finding]) -> str:
    if not findings:
        return "All keys in the values files are known to the chart."
    lines = []
    for f in findings:
        if f.kind == "unknown-key":
            tag = "IGNORED" if f.no_effect else "UNKNOWN"
            detail = f.message + ("; removing it does not change the rendered output" if f.no_effect else "")
        else:
            tag, detail = "TYPE", f.message
        lines.append(f"{tag:8} {f.path}  ({f.file})")
        lines.append(f"         {detail}")
        if f.suggestion:
            lines.append(f"         {f.suggestion}")
    n = len(findings)
    lines += ["", f"{n} problem{'s' if n != 1 else ''} found."]
    return "\n".join(lines)
