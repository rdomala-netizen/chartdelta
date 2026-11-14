---
name: helm-chart-diff
description: Read a Helm chart, render it with the real values files (helm template), and diff the rendered Kubernetes manifests between the user's modifications and the original (git ref, other values file, or other chart directory). Use whenever the user changes a Helm chart or values file and asks what the change does, wants a helm diff without a cluster, wants a chart or merge request reviewed, or asks "what will this render to" for a given environment.
---

# Helm chart diff

Answer "what does this chart change actually do?" from rendered manifests, not
from reading templates. Uses the `chartdelta` CLI, which runs `helm template`
for both sides and diffs per resource. No cluster access is needed or used.

## 1. Check prerequisites

```sh
helm version --short && chartdelta --version
```

- `chartdelta` missing: use `uvx chartdelta ...` if `uv` exists, otherwise
  `pipx install chartdelta` (or `pip install chartdelta`).
- `helm` missing: tell the user; do not try to emulate templating by hand.

## 2. Find the chart and the values

- The chart is the directory containing `Chart.yaml`. If several exist, pick
  the one the user named or whose files changed (`git status --short`).
- Use the values files the user actually deploys with. Look for
  `values-<env>.yaml`, an `envs/` folder, or Argo CD `Application` manifests
  (`spec.source.helm.valueFiles`, `releaseName`, `parameters`). Pass them with
  `-f` in the same order. If it is unclear which environment matters, ask once,
  or run each environment separately.
- Match release name and namespace when known (`-r`, `-n`), since both appear
  in rendered names.

## 3. Read the chart

```sh
chartdelta inspect <chart> -f <values> -o json
```

Gives metadata, dependencies, top-level values, templates and the rendered
resource list. Open individual templates only where the diff needs explaining.

## 4. Render the manifests when asked

When the user wants to see what the chart renders to with their values (not a
comparison), or you need the full manifest to explain a diff:

```sh
chartdelta render <chart> -f <values> --list                    # what gets created
chartdelta render <chart> -f <values> --kind Deployment --name <text>
chartdelta render <chart> -f <values>                           # everything
```

- Start with `--list`, then print only the resources that matter; a full
  render of a large chart is long. To hand over the full output, redirect it
  to a file (`> rendered.yaml`) and give the user the file.
- Secret values are shown as hashes. Do not add `--show-secrets` unless asked.
- A render error is the answer: show helm's message, the template and line it
  names, and the value that caused it.
- Output is normalised (sorted keys, no comments). If the user needs helm's
  raw output, run `helm template <release> <chart> -f <values>` directly.

## 5. Check the values files for ignored keys

```sh
chartdelta check-values <chart> -f <values> -o json
```

Helm silently accepts unknown keys, so run this whenever the user edited a
values file, and always when a diff comes back empty or smaller than expected.
Exit code 2 means findings:

- `unknown-key` with `noEffect: true`: the key is not read by the chart and
  removing it changes nothing. Report it as ignored and pass on `suggestion`
  (typo or wrong nesting).
- `unknown-key` with `noEffect: null`: static result only (the chart did not
  render, or `--no-render`). Say it is likely ignored, not certain.
- `type-mismatch`: the value has a different shape than the chart default.

Keys inside lists are not checked; read the template if one is in doubt.

## 6. Run the diff

Choose the base to match what the user means by "original":

| Situation | Command |
|---|---|
| Uncommitted edits vs. last commit | `chartdelta diff <chart> -f <values> -o json` |
| Branch vs. main (merge request review) | add `--base origin/main --merge-base` |
| Against a tag or commit | add `--base <ref>` |
| Two values files, same chart | `chartdelta diff <chart> -f new.yaml --base-values old.yaml -o json` |
| Two chart directories | `chartdelta diff <new> --base-chart <old> -f <values> -o json` |

Notes:
- Start with `-o json --summary` on large charts, then fetch full diffs for
  the interesting kinds with `--kind`.
- After a chart version bump, add `--ignore-version-labels`.
- "declares dependencies but charts/ is empty": re-run with `--build-deps`.
- Exit code 1 means a render or git error. Show the helm error and which side
  (base or target) failed; a base-side failure means the original was already
  broken with these values.
- Never pass `--show-secrets` unless the user asks.

## 7. Chart version upgrades

When the user is moving a third-party chart from one version to another, do
not pull and unpack by hand. One command pulls both versions and compares
them with the user's values:

```sh
chartdelta upgrade <repo/chart | oci://...> --from <current> --to <target> -f <values> -o json --summary
```

- Find the current version in the Argo CD `Application` (`targetRevision`),
  `Chart.yaml` dependencies, or `helm list`. Add `--repo <url>` when the repo
  is not added locally. Local directories also work: `--from ./old --to ./new`.
- Then report in this order:
  1. `crds` by `severity`. `breaking`: an API version in use today is removed
     or no longer served, or the CRD is removed; manifests and objects must
     move to a served version before the upgrade, and an intermediate chart
     version that serves both is usually the way. `check`: the storage
     version moved or an unserved version was removed; safe unless
     `kubectl get crd <name> -o jsonpath='{.status.storedVersions}'` still
     lists a removed version. Do not call a `check` item breaking.
  2. `valuesFindings` with `newInThisUpgrade: true`: the user's overrides that
     the new version ignores. These are the renamed options; give the
     suggested new key.
  3. `resources`: what changes in the cluster, as in the Report step.
  4. `defaults.changed` / `removed` that the user does not override, since
     these change behaviour without any edit on their side.
  5. `metadataChanges.kubeVersion` and subchart bumps.
- CRDs in a chart's `crds/` directory are not touched by `helm upgrade`. With
  Argo CD they are applied on sync; large CRDs may need server-side apply.
- Fetch full text only where needed: re-run without `--summary`, or with
  `--crd-diff` for one CRD.
- This compares charts only. Always recommend reading the chart's upgrade
  notes or changelog for manual steps the manifests cannot show.

## 8. Report

Lead with the outcome, then the evidence:

1. One line: N added / removed / changed, and against which base.
2. Per changed resource: what changed in plain terms, using `changedPaths`
   (e.g. "Deployment/app: memory limit 512Mi → 1Gi").
3. Call out operational impact when it applies:
   - a change under `spec.template` of a Deployment/StatefulSet/DaemonSet
     rolls the pods; a ConfigMap/Secret change does not, unless a checksum
     annotation changed too
   - removed resources will be deleted (pruned, if GitOps pruning is on)
   - changes to immutable fields (`spec.selector`, Service `clusterIP`,
     StatefulSet `volumeClaimTemplates`, Job spec) need a recreate
   - changes to replicas, resources, probes, PDBs, affinity, HPA bounds
   - new or changed RBAC, hostPath, privileged or host-network settings
4. Unexpected changes: anything in the diff the user's edit does not explain.
5. If there are no differences, say so and name the likely reason (the value
   is unused by any template, overridden by a later values file, or behind a
   disabled condition).

Show raw diff hunks only for the parts that matter; offer `-o markdown` output
if the user wants something to paste into a merge request.
