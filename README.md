# chartdelta

See what your Helm chart change **actually renders to**, before you commit it.

`chartdelta` renders a chart twice with `helm template` — once as it was (the
original) and once as it is now (your modifications) — and shows a
per-resource diff of the manifests. No cluster, no installed release, no Helm
plugin: just `helm`, `git`, and this CLI.

```console
$ chartdelta diff charts/app1 -f envs/prod.yaml
HEAD@3f2a9c1e -> working tree
0 added, 0 removed, 1 changed, 7 unchanged

~ Deployment/release-app1  [app1/templates/deployment.yaml]

--- HEAD@3f2a9c1e: Deployment/release-app1
+++ working tree: Deployment/release-app1
@@ -22,7 +22,7 @@
         resources:
           limits:
-            memory: 512Mi
+            memory: 1Gi
```

It also ships a [Claude skill](https://github.com/rdomala-netizen/chartdelta/blob/main/skills/helm-chart-diff/SKILL.md) so Claude can
read a chart, run the diff, and explain the impact.

## Install

```sh
pipx install chartdelta      # or: uv tool install chartdelta
uvx chartdelta --help        # run without installing
```

Requires Python 3.12+, `helm` 3 on `PATH`, and `git` for git-based comparisons.

## What is "the original"?

| You want to compare… | Command |
|---|---|
| uncommitted changes vs. last commit (default) | `chartdelta diff CHART -f values.yaml` |
| your branch vs. main, like a merge request | `chartdelta diff CHART -f values.yaml --base origin/main --merge-base` |
| against any tag / commit | `chartdelta diff CHART --base v1.4.0` |
| two values files on the same chart | `chartdelta diff CHART -f new.yaml --base-values old.yaml` |
| two chart directories | `chartdelta diff ./new-chart --base-chart ./old-chart -f values.yaml` |

In git mode, both the chart **and** the `-f` values files are taken as they
were at the base ref, so a change to either shows up. A values file that did
not exist at the base ref (or lives outside the repo) is used on both sides.

## Useful options

- `-o text|markdown|json` — `markdown` is made for merge-request comments,
  `json` includes `changedPaths` per resource (e.g.
  `spec.template.spec.containers[name=app].image`) for tooling and AI agents.
- `--summary` — changed resources and field paths only, no diff bodies.
- `--exit-code` — exit `2` when there are differences (`0` none, `1` error).
- `--ignore-version-labels` — hide `helm.sh/chart` / `app.kubernetes.io/version`
  churn after a chart version bump. `--ignore-label KEY` for anything else.
- `--kind Deployment` — restrict to some kinds.
- `--set`, `--set-string`, `-r/--release`, `-n/--namespace`, `--kube-version`,
  `--api-versions`, `--include-crds`, `--no-hooks`, `--helm-arg` — forwarded
  to `helm template` on both sides.
- `--build-deps` — run `helm dependency build` when subcharts are missing.
  If the base side's dependencies are unchanged, your already-downloaded
  `charts/` is reused, with no network access.
- Secret values are replaced by a short hash by default, so you see *that* a
  secret changed without printing it. `--show-secrets` disables this.

`chartdelta inspect CHART [-f values.yaml]` summarises a chart: metadata,
dependencies, top-level values, templates, and the resources it renders.

## Rendering with your values

```sh
chartdelta render charts/app1 -f envs/prod.yaml                  # all manifests
chartdelta render charts/app1 -f envs/prod.yaml --kind Deployment --name api
chartdelta render charts/app1 -f envs/prod.yaml --list           # just the resource list
```

This is `helm template` with conveniences: filter by kind and name, keys in a
stable sorted order, each manifest labelled with its source template, and
Secret values replaced by hashes unless `--show-secrets` is given. Use
`-o json` for tooling. The output is normalised YAML (comments from the
templates are dropped), so use plain `helm template` when you need the raw
text.

## Reviewing a chart upgrade

```sh
chartdelta upgrade kyverno/kyverno --from 3.2.6 --to 3.3.4 -f values.yaml
chartdelta upgrade oci://registry.example.com/charts/app --from 1.4.0 --to 2.0.0 -f values.yaml
chartdelta upgrade --from ./old-chart --to ./new-chart -f values.yaml     # already unpacked
```

Pulls both versions with `helm pull` (no manual download or unzip), renders
each with **your** values, and reports:

- **Chart metadata**: appVersion, `kubeVersion` constraint, subchart versions.
- **CRDs**: added / removed / changed, and for each changed CRD the API
  versions added or removed, versions no longer served, storage-version
  changes, and how many schema fields changed. Each is marked `BREAKING`
  (a version clients can use today is removed or no longer served) or
  `CHECK` (storage version moved, or an unserved version was removed). Full CRD diffs only with `--crd-diff`.
- **Rendered resources**: the per-resource diff, with version-label churn
  hidden by default.
- **Default values**: defaults that were removed, changed or added.
- **Your values**: keys in your values files that the new version no longer
  reads (renamed or removed options), separated from keys that were already
  ignored before.

`--summary` drops the diff bodies, `-o markdown|json` as usual, `--keep DIR`
keeps the pulled charts, `--exit-code` exits 2 on a breaking CRD change or a
values key that stops working.

Remember that `helm upgrade` does not update CRDs shipped in a chart's
`crds/` directory; they must be applied separately (Argo CD applies them on
sync). This command compares charts, not the cluster: it cannot see which CRD
versions your existing objects are stored at.

## Previewing an Argo CD ApplicationSet

Edit a values file, an `addon.yaml`, or the ApplicationSet itself, and see
which generated Applications are affected and what changes in their
manifests, before you push. No cluster and no running Argo CD.

```sh
# one-off: list your clusters (names, servers, labels only; no credentials)
chartdelta appset export-clusters --context my-argocd-cluster > clusters.yaml

# which Applications does it generate, from which path, with which values files?
chartdelta appset generate appsets/addons.yaml --clusters clusters.yaml

# what do my uncommitted changes do, per Application?
chartdelta appset diff appsets/addons.yaml --clusters clusters.yaml --summary
chartdelta appset diff appsets/addons.yaml --clusters clusters.yaml --base origin/main --app 'kyverno-*'
```

```console
ApplicationSet addons: HEAD@b19d7dc5 -> working tree
Applications: 0 added, 0 removed, 2 changed, 3 unchanged, 0 error

~ kyverno-dev  (changed, https://dev:6443)
  Application spec changed
  resources: 0 added, 0 removed, 1 changed, 0 unchanged
  ~ Deployment/kyverno/kyverno
      spec.template.spec.containers[name=main].image
```

How it works: the generators are evaluated against your local checkout, the
template is expanded (Go templates are evaluated by Helm's own engine, so
`index`, `with`, variables and sprig functions behave as they do in Argo CD),
each Application's Helm source is rendered at the base ref and in your working
tree, and the results are diffed per resource.

- **Repositories**: a `repoURL` is matched to a local checkout by its `origin`
  remote. The repo containing the ApplicationSet file and the repo you are
  standing in are found automatically; add others with
  `--repo https://github.com/org/values-repo.git=~/git/values-repo`.
- **Generators**: `list`, `git` (files and directories), `clusters`, `matrix`.
- **Sources**: Helm charts in Git (`path`), with `valueFiles` (including
  `ignoreMissingValueFiles` and `$ref/...` from a second source), `values`,
  `valuesObject`, `parameters`, `releaseName`; plain YAML directories; charts
  from a Helm or OCI repository (`chart:`).
- A missing source path or values file is reported per Application as an
  error, which catches a version folder that does not exist yet.

This is a fast local approximation, not Argo CD itself. Not covered: `merge`,
SCM provider, pull request and plugin generators, `templatePatch`, Kustomize
sources, config management plugins, and `goTemplateOptions`
(`missingkey=error` is not enforced: a missing key renders as empty). The
`revision` and `targetRevision` fields are ignored in favour of your local
tree. For an exact check in CI, use
[argocd-diff-preview](https://github.com/dag-andersen/argocd-diff-preview).

## Catching values that Helm silently ignores

Helm accepts any key in a values file. A typo (`replicas` instead of
`replicaCount`) or a wrongly nested key is merged and never read, unless the
chart ships a `values.schema.json`.

```console
$ chartdelta check-values charts/app1 -f envs/prod.yaml
IGNORED  replicas  (envs/prod.yaml)
         not in the chart's values.yaml and not referenced by any template; removing it does not change the rendered output
         did you mean 'replicaCount'?
IGNORED  tag  (envs/prod.yaml)
         ...
         'tag' exists at 'image.tag' — wrong nesting?
```

A key is reported when it is missing from the chart's default values, no
template references `.Values.<path>`, **and** rendering the chart without it
gives identical output. Subchart values are checked against the subchart
(when it is present in `charts/`), `global` is skipped, and maps whose default
is `{}` (annotations, nodeSelector, …) are treated as free-form. Structural
type mismatches (map vs. scalar, list, bool) are reported too. Exit code is
`2` when problems are found, so it works as a CI gate. `chartdelta diff
--check-values` prints the same warnings alongside a diff.

Not covered: keys inside lists (e.g. a typo in an `env` entry), and keys that
are valid but currently behind a disabled feature are not reported.

## GitLab CI example

```yaml
helm-diff:
  image: python:3.12
  script:
    - curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
    - pip install chartdelta
    - git fetch origin "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME"
    - chartdelta diff charts/app1 -f envs/prod.yaml
        --base "origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME" --merge-base -o markdown > helm-diff.md
  artifacts:
    paths: [helm-diff.md]
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
```

## How it differs from the helm-diff plugin

[helm-diff](https://github.com/databus23/helm-diff) compares a chart against a
**release installed in a cluster**. `chartdelta` compares two **renderings**
(git ref, directory, or values), which fits GitOps workflows where Argo CD or
Flux does the deploy and you want to review the change at merge-request time.
It does not see live-cluster drift.

## Limitations

- Output depends on `helm template`, so `lookup` calls return empty and
  `.Capabilities` come from `--kube-version` / `--api-versions`, not a cluster.
- Charts are local directories; pull OCI/repo charts first (`helm pull --untar`).

## Using the Claude skill

The skill ships inside the package. Install it for Claude Code with:

```sh
chartdelta install-skill                       # into ~/.claude/skills
chartdelta install-skill --dir .claude/skills  # for one project only
```

Start a new Claude Code session, then ask things like "what does my change to
this chart actually do with prod values?". The skill file itself is
[on GitHub](https://github.com/rdomala-netizen/chartdelta/blob/main/skills/helm-chart-diff/SKILL.md);
`chartdelta install-skill --print` shows the copy bundled with your version.

## Development

```sh
pip install -e ".[dev]"
pytest                          # uses a small helm stand-in
CHARTDELTA_REAL_HELM=1 pytest   # runs the same suite against real helm
```

MIT licensed.
