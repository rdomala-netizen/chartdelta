"""Command-line interface for chartdelta."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile

import yaml
from pathlib import Path
from typing import Any

from . import __version__
from .core import ChartDeltaError, DiffResult, RenderOptions, inspect_chart, render_chart, run_diff, to_yaml
from .appset import export_clusters, render_appset_report, run_appset_diff, run_appset_generate
from .skill_install import install_skill, skill_text
from .upgrade import render_report, run_upgrade
from .values_check import check_values, render_findings

# Labels that change on every chart version bump and drown out real changes.
VERSION_LABELS = ["helm.sh/chart", "app.kubernetes.io/version"]

_RED, _GREEN, _CYAN, _BOLD, _DIM, _RESET = "\033[31m", "\033[32m", "\033[36m", "\033[1m", "\033[2m", "\033[0m"
_SYMBOL = {"added": "+", "removed": "-", "changed": "~"}


def _add_render_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("chart", nargs="?", default=".", type=Path, help="path to the chart directory (default: .)")
    p.add_argument("-f", "--values", action="append", default=[], type=Path, metavar="FILE",
                   help="values file for the target side (repeatable, same order semantics as helm)")
    p.add_argument("--set", dest="set_args", action="append", default=[], metavar="K=V", help="passed to helm --set")
    p.add_argument("--set-string", dest="set_string_args", action="append", default=[], metavar="K=V")
    p.add_argument("-r", "--release", default="release", help="release name used for templating (default: release)")
    p.add_argument("-n", "--namespace", default="default")
    p.add_argument("--include-crds", action="store_true")
    p.add_argument("--no-hooks", action="store_true", help="leave helm hooks out of the comparison")
    p.add_argument("--kube-version", metavar="VER", help="Kubernetes version for .Capabilities")
    p.add_argument("--api-versions", action="append", default=[], metavar="API")
    p.add_argument("--helm", dest="helm_bin", default="helm", help="helm binary to use (default: helm)")
    p.add_argument("--helm-arg", dest="helm_args", action="append", default=[], metavar="ARG",
                   help="extra argument passed verbatim to 'helm template' (repeatable)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chartdelta",
        description="Render a Helm chart with real values and diff the manifests against the original.",
    )
    parser.add_argument("--version", action="version", version=f"chartdelta {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser(
        "diff",
        help="diff rendered manifests: your changes vs. the original",
        description="Without base options, compares the working tree against git HEAD.",
    )
    _add_render_args(d)
    base = d.add_argument_group("what to compare against (the 'original')")
    base.add_argument("-b", "--base", metavar="REF", help="git ref for the original (default: HEAD)")
    base.add_argument("--merge-base", action="store_true",
                      help="compare against the merge base of --base and HEAD (what a merge request shows)")
    base.add_argument("--base-chart", type=Path, metavar="DIR", help="compare against another chart directory instead of git")
    base.add_argument("--base-values", action="append", type=Path, metavar="FILE",
                      help="values file(s) for the original side. Given alone, the same chart is rendered "
                           "with both value sets (pure values diff)")
    out = d.add_argument_group("output")
    out.add_argument("-o", "--output", choices=["text", "markdown", "json"], default="text")
    out.add_argument("-C", "--context", type=int, default=3, help="lines of diff context (default: 3)")
    out.add_argument("--kind", dest="kinds", action="append", default=[], metavar="KIND", help="only these kinds (repeatable)")
    out.add_argument("--ignore-label", action="append", default=[], metavar="KEY",
                     help="ignore this label/annotation key everywhere (repeatable)")
    out.add_argument("--ignore-version-labels", action="store_true",
                     help="ignore " + " and ".join(VERSION_LABELS))
    out.add_argument("--show-secrets", action="store_true", help="show Secret values instead of hashes")
    out.add_argument("--summary", action="store_true", help="list changed resources without the diffs")
    out.add_argument("--exit-code", action="store_true", help="exit 2 when there are differences")
    out.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    d.add_argument("--check-values", action="store_true",
                   help="also warn (on stderr) about keys in the values files that the chart ignores")
    d.add_argument("--build-deps", action="store_true", help="run 'helm dependency build' when subcharts are missing")

    i = sub.add_parser("inspect", help="summarise a chart: metadata, values, templates, rendered resources")
    _add_render_args(i)
    i.add_argument("-o", "--output", choices=["text", "json"], default="text")
    i.add_argument("--no-render", action="store_true", help="skip 'helm template' (no helm needed)")

    u = sub.add_parser(
        "upgrade",
        help="compare two versions of a chart before upgrading: resources, CRDs, default values, your values",
        description="CHART is what 'helm pull' accepts (repo/name, oci://..., or a name with --repo). "
                    "--from/--to take versions, or paths to already-unpacked chart directories.",
    )
    _add_render_args(u)
    u.add_argument("--from", dest="from_side", required=True, metavar="VERSION|DIR", help="version you run today")
    u.add_argument("--to", dest="to_side", required=True, metavar="VERSION|DIR", help="version you want to upgrade to")
    u.add_argument("--repo", metavar="URL", help="chart repository URL (passed to helm pull --repo)")
    u.add_argument("--keep", type=Path, metavar="DIR", help="keep the pulled charts in DIR instead of a temp dir")
    u.add_argument("-o", "--output", choices=["text", "markdown", "json"], default="text")
    u.add_argument("--summary", action="store_true", help="no diff bodies")
    u.add_argument("--crd-diff", action="store_true", help="include full CRD diffs (large)")
    u.add_argument("-C", "--context", type=int, default=3)
    u.add_argument("--show-secrets", action="store_true")
    u.add_argument("--keep-version-labels", action="store_true",
                   help="do not hide helm.sh/chart and app.kubernetes.io/version label changes")
    u.add_argument("--build-deps", action="store_true")
    u.add_argument("--exit-code", action="store_true", help="exit 2 on a breaking CRD change or a values key that stops working")

    r = sub.add_parser(
        "render",
        help="render the chart with your values and print the manifests (helm template, filtered and secret-safe)",
    )
    _add_render_args(r)
    r.add_argument("--kind", dest="kinds", action="append", default=[], metavar="KIND", help="only these kinds (repeatable)")
    r.add_argument("--name", dest="names", action="append", default=[], metavar="TEXT",
                   help="only resources whose name contains TEXT (repeatable)")
    r.add_argument("--list", action="store_true", help="list the rendered resources instead of printing manifests")
    r.add_argument("--show-secrets", action="store_true", help="show Secret values instead of hashes")
    r.add_argument("-o", "--output", choices=["yaml", "json"], default="yaml")
    r.add_argument("--build-deps", action="store_true", help="run 'helm dependency build' when subcharts are missing")

    a = sub.add_parser("appset", help="preview an Argo CD ApplicationSet offline: generated Applications and manifest changes")
    asub = a.add_subparsers(dest="appset_command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("appset_file", type=Path, metavar="APPSET", help="path to the ApplicationSet YAML file")
        p.add_argument("--clusters", type=Path, metavar="FILE",
                       help="YAML list of clusters (name, server, labels) for the clusters generator")
        p.add_argument("--repo", dest="repos", action="append", default=[], metavar="URL=PATH",
                       help="local checkout to use for a repoURL (repeatable). The repo containing APPSET "
                            "and the current directory's repo are detected automatically")
        p.add_argument("--helm", dest="helm_bin", default="helm")
        p.add_argument("--kube-version", metavar="VER")
        p.add_argument("--api-versions", action="append", default=[], metavar="API")

    ag = asub.add_parser("generate", help="list the Applications the ApplicationSet generates from your working tree")
    common(ag)
    ag.add_argument("-o", "--output", choices=["text", "yaml", "json"], default="text")

    ad = asub.add_parser("diff", help="show which Applications and manifests change between a git ref and your working tree")
    common(ad)
    ad.add_argument("-b", "--base", default="HEAD", metavar="REF", help="git ref to compare against (default: HEAD)")
    ad.add_argument("--app", dest="only", action="append", default=[], metavar="GLOB", help="only Applications matching GLOB (repeatable)")
    ad.add_argument("-o", "--output", choices=["text", "markdown", "json"], default="text")
    ad.add_argument("--summary", action="store_true", help="changed resources and fields only, no diff bodies")
    ad.add_argument("--show-unchanged", action="store_true", help="also list unaffected Applications")
    ad.add_argument("-C", "--context", type=int, default=3)
    ad.add_argument("--show-secrets", action="store_true")
    ad.add_argument("--ignore-version-labels", action="store_true")
    ad.add_argument("--no-build-deps", action="store_true", help="do not run 'helm dependency build' for charts with missing subcharts")
    ad.add_argument("--exit-code", action="store_true", help="exit 2 when any Application is affected, 1 on errors")

    ae = asub.add_parser("export-clusters", help="write a clusters file from Argo CD's cluster secrets (names, servers, labels only)")
    ae.add_argument("--context", help="kubectl context of the cluster running Argo CD")
    ae.add_argument("-n", "--namespace", default="argocd")

    c = sub.add_parser(
        "check-values",
        help="find keys in your values files that the chart silently ignores (typos, wrong nesting)",
        description="Exits 2 when problems are found.",
    )
    _add_render_args(c)
    c.add_argument("-o", "--output", choices=["text", "json"], default="text")
    c.add_argument("--no-render", action="store_true",
                   help="static check only; skip the render probe (no helm needed)")

    k = sub.add_parser("install-skill", help="install the bundled Claude skill (helm-chart-diff) for Claude Code")
    k.add_argument("--dir", type=Path, default=Path("~/.claude/skills"), metavar="DIR",
                   help="skills directory (default: ~/.claude/skills; use .claude/skills for one project)")
    k.add_argument("--print", dest="print_only", action="store_true", help="print the skill instead of installing it")
    return parser


def _opts(a: argparse.Namespace) -> RenderOptions:
    return RenderOptions(
        release=a.release, namespace=a.namespace, set_args=a.set_args, set_string_args=a.set_string_args,
        include_crds=a.include_crds, no_hooks=a.no_hooks, kube_version=a.kube_version,
        api_versions=a.api_versions, helm_bin=a.helm_bin, helm_args=a.helm_args,
    )


def _summary_line(r: DiffResult) -> str:
    return (f"{r.count('added')} added, {r.count('removed')} removed, "
            f"{r.count('changed')} changed, {r.unchanged} unchanged")


def render_text(r: DiffResult, color: bool, summary_only: bool) -> str:
    def c(code: str, s: str) -> str:
        return f"{code}{s}{_RESET}" if color else s

    lines = [c(_BOLD, f"{r.base_label} -> {r.target_label}"), _summary_line(r), ""]
    if not r.changes:
        lines.append("No differences in rendered manifests.")
        return "\n".join(lines)
    for ch in r.changes:
        code = {"added": _GREEN, "removed": _RED, "changed": _CYAN}[ch.status]
        hook = " (hook)" if ch.is_hook else ""
        lines.append(c(code, f"{_SYMBOL[ch.status]} {ch.key}{hook}") + c(_DIM, f"  [{ch.source}]"))
        if summary_only:
            for p in ch.changed_paths:
                lines.append(f"    {p}")
    if summary_only:
        return "\n".join(lines)
    for ch in r.changes:
        lines.append("")
        for ln in ch.diff.splitlines():
            if ln.startswith(("+++", "---")):
                lines.append(c(_BOLD, ln))
            elif ln.startswith("+"):
                lines.append(c(_GREEN, ln))
            elif ln.startswith("-"):
                lines.append(c(_RED, ln))
            elif ln.startswith("@@"):
                lines.append(c(_CYAN, ln))
            else:
                lines.append(ln)
    return "\n".join(lines)


def render_markdown(r: DiffResult, summary_only: bool) -> str:
    lines = [f"### Helm diff: `{r.base_label}` → `{r.target_label}`", "", _summary_line(r), ""]
    if not r.changes:
        lines.append("No differences in rendered manifests.")
        return "\n".join(lines)
    lines += ["| | Resource | Namespace | Template | Fields |", "|---|---|---|---|---|"]
    for ch in r.changes:
        fields = ", ".join(f"`{p}`" for p in ch.changed_paths[:6])
        if len(ch.changed_paths) > 6:
            fields += f" (+{len(ch.changed_paths) - 6} more)"
        lines.append(f"| {ch.status} | `{ch.key.kind}/{ch.key.name}` | {ch.key.namespace or '-'} | `{ch.source}` | {fields} |")
    if not summary_only:
        for ch in r.changes:
            lines += ["", f"<details><summary>{ch.status}: <code>{ch.key}</code></summary>", "", "```diff", ch.diff, "```", "", "</details>"]
    return "\n".join(lines)


def render_inspect(info: dict[str, Any]) -> str:
    lines = [f"{info['name']} {info['version']} (appVersion {info.get('appVersion')}, {info['type']})"]
    if info.get("description"):
        lines.append(info["description"])
    lines += ["", f"Values files: {', '.join(info['valuesFiles']) or 'none'}"
              + ("   schema: values.schema.json" if info["hasSchema"] else "")]
    if info["dependencies"]:
        lines += ["", "Dependencies:"]
        lines += [f"  {d.get('name')} {d.get('version', '')} {d.get('repository', '')}".rstrip() for d in info["dependencies"]]
    lines += ["", "Top-level values:"] + [f"  {k}: {v}" for k, v in info["values"].items()]
    lines += ["", "Templates:"] + [f"  {t}" for t in info["templates"]]
    if "resources" in info:
        lines += ["", f"Rendered resources ({len(info['resources'])}):"]
        lines += [f"  {x['kind']}/{x['name']}" + (" (hook)" if x["hook"] else "") + f"  [{x['source']}]" for x in info["resources"]]
    if "renderError" in info:
        lines += ["", "Render failed:", info["renderError"]]
    return "\n".join(lines)


def _appset_main(args: argparse.Namespace) -> int:
    try:
        if args.appset_command == "export-clusters":
            print(yaml.safe_dump({"clusters": export_clusters(args.namespace, args.context)}, sort_keys=False), end="")
            return 0
        if not args.appset_file.is_file():
            raise ChartDeltaError(f"ApplicationSet file not found: {args.appset_file}")
        opts = RenderOptions(helm_bin=args.helm_bin, kube_version=args.kube_version, api_versions=args.api_versions)
        if args.appset_command == "generate":
            apps, warnings, info = run_appset_generate(args.appset_file, args.clusters, args.repos, opts)
            if args.output == "yaml":
                print(yaml.safe_dump_all(apps, sort_keys=False), end="")
            elif args.output == "json":
                print(json.dumps({"applications": apps, "sources": info, "warnings": warnings}, indent=2, default=str))
            else:
                print(f"{len(apps)} Application{'s' if len(apps) != 1 else ''} generated")
                for w in warnings:
                    print(f"WARNING: {w}")
                for i in info:
                    print(f"\n{i['name']}  ->  {i['destination']} / {i['namespace']}")
                    for path in i["paths"]:
                        print(f"  source: {path}")
                    if i.get("error"):
                        print(f"  ERROR: {i['error']}")
                        continue
                    if i.get("valueFiles"):
                        print(f"  values used: {', '.join(i['valueFiles'])}")
                    if i.get("missingValueFiles"):
                        print(f"  values not present (ignored): {', '.join(i['missingValueFiles'])}")
                    for note in i.get("notes", []):
                        print(f"  note: {note}")
            return 1 if any(i.get("error") for i in info) else 0

        report = run_appset_diff(
            args.appset_file, args.clusters, args.repos, opts, base_ref=args.base,
            build_deps=not args.no_build_deps, context=args.context, show_secrets=args.show_secrets,
            ignore_labels=VERSION_LABELS if args.ignore_version_labels else [], only=args.only,
        )
        if args.output == "json":
            data = report.to_dict()
            if args.summary:
                for app in data["applications"]:
                    for ch in (app["resources"] or {}).get("changes", []):
                        ch.pop("diff")
            print(json.dumps(data, indent=2, default=str))
        else:
            print(render_appset_report(report, markdown=args.output == "markdown", summary=args.summary,
                                       show_unchanged=args.show_unchanged))
        if report.count("error"):
            return 1
        return 2 if (args.exit_code and report.has_changes) else 0
    except ChartDeltaError as exc:
        print(f"chartdelta: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Accept a path to Chart.yaml as well as the chart directory.
    if args.command == "appset":
        return _appset_main(args)
    if args.command == "install-skill":
        try:
            if args.print_only:
                print(skill_text(), end="")
            else:
                path, state = install_skill(args.dir)
                print(f"Skill {state}: {path}")
                if state != "unchanged":
                    print("Start a new Claude Code session to load it.")
            return 0
        except ChartDeltaError as exc:
            print(f"chartdelta: {exc}", file=sys.stderr)
            return 1
    if args.chart.name in ("Chart.yaml", "Chart.yml") and args.chart.is_file():
        args.chart = args.chart.parent
    try:
        if args.command == "inspect":
            info = inspect_chart(args.chart, args.values, _opts(args), render=not args.no_render)
            print(json.dumps(info, indent=2) if args.output == "json" else render_inspect(info))
            return 0

        if args.command == "upgrade":
            # With --from/--to as directories, the positional is not needed.
            ref = None if str(args.chart) == "." else str(args.chart)
            with tempfile.TemporaryDirectory(prefix="chartdelta-") as tmp:
                work = args.keep if args.keep else Path(tmp)
                report = run_upgrade(
                    ref, args.from_side, args.to_side, args.values, _opts(args), repo=args.repo,
                    workdir=work, build_deps=args.build_deps, context=args.context,
                    show_secrets=args.show_secrets, keep_version_labels=args.keep_version_labels,
                )
            if args.output == "json":
                data = report.to_dict()
                if args.summary or not args.crd_diff:
                    for c in data["crds"]:
                        c.pop("diff")
                if args.summary:
                    for ch in data["resources"]["changes"]:
                        ch.pop("diff")
                print(json.dumps(data, indent=2, default=str))
            else:
                print(render_report(report, markdown=args.output == "markdown", crd_diff=args.crd_diff, summary=args.summary))
            risky = any(c.severity == "breaking" for c in report.crds) or any(new for _, new in report.values_findings)
            return 2 if (args.exit_code and risky) else 0

        if args.command == "render":
            rendered = render_chart(
                args.chart, args.values, _opts(args), kinds=args.kinds, names=args.names,
                show_secrets=args.show_secrets, build_deps=args.build_deps,
            )
            if not rendered:
                print("chartdelta: nothing rendered (no resources match).", file=sys.stderr)
                return 0
            if args.output == "json":
                print(json.dumps([
                    {"kind": res.key.kind, "name": res.key.name, "namespace": res.key.namespace,
                     "source": res.source, "hook": res.is_hook, **({} if args.list else {"manifest": doc})}
                    for res, doc in rendered
                ], indent=2, default=str))
            elif args.list:
                for res, _ in rendered:
                    print(f"{res.key}" + (" (hook)" if res.is_hook else "") + f"  [{res.source}]")
            else:
                for res, doc in rendered:
                    print(f"---\n# Source: {res.source}\n{to_yaml(doc)}", end="")
            return 0

        if args.command == "check-values":
            findings = check_values(args.chart, args.values, _opts(args), probe=not args.no_render)
            if args.output == "json":
                print(json.dumps({"findings": [f.to_dict() for f in findings]}, indent=2))
            else:
                print(render_findings(findings))
            return 2 if findings else 0

        if args.check_values and args.values:
            findings = check_values(args.chart, args.values, _opts(args))
            if findings:
                print(render_findings(findings) + "\n", file=sys.stderr)

        ignore = list(args.ignore_label) + (VERSION_LABELS if args.ignore_version_labels else [])
        result = run_diff(
            args.chart, args.values, _opts(args),
            base_ref=args.base, base_chart=args.base_chart, base_values=args.base_values,
            use_merge_base=args.merge_base, build_deps=args.build_deps, context=args.context,
            show_secrets=args.show_secrets, ignore_labels=ignore, kinds=args.kinds,
        )
        if args.output == "json":
            data = result.to_dict()
            if args.summary:
                for ch in data["changes"]:
                    ch.pop("diff")
            print(json.dumps(data, indent=2))
        elif args.output == "markdown":
            print(render_markdown(result, args.summary))
        else:
            color = args.color == "always" or (args.color == "auto" and sys.stdout.isatty())
            print(render_text(result, color, args.summary))
        return 2 if (args.exit_code and result.has_changes) else 0
    except ChartDeltaError as exc:
        print(f"chartdelta: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
