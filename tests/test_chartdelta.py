import json
import os
from pathlib import Path

import pytest

from chartdelta.cli import main
from chartdelta.core import RenderOptions, changed_paths, diff_manifests, run_diff

from conftest import git

BASE = """\
---
# Source: demo/templates/deploy.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  labels: {helm.sh/chart: demo-0.1.0}
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: web
          image: nginx:1.27
        - name: sidecar
          image: envoy:1
---
apiVersion: v1
kind: Secret
metadata: {name: creds}
stringData: {password: old}
---
apiVersion: v1
kind: Service
metadata: {name: gone}
"""

TARGET = """\
---
# Source: demo/templates/deploy.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  labels: {helm.sh/chart: demo-0.2.0}
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: sidecar
          image: envoy:2
        - name: web
          image: nginx:1.27
---
apiVersion: v1
kind: Secret
metadata: {name: creds}
stringData: {password: new}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: fresh}
"""


def by_name(result):
    return {c.key.name: c for c in result.changes}


def test_diff_statuses_and_paths():
    r = by_name(diff_manifests(BASE, TARGET))
    assert r["gone"].status == "removed"
    assert r["fresh"].status == "added"
    assert r["web"].status == "changed"
    assert r["web"].source == "demo/templates/deploy.yaml"
    # containers are matched by name, not position
    assert "spec.template.spec.containers[name=sidecar].image" in r["web"].changed_paths
    assert not any("name=web" in p for p in r["web"].changed_paths)


def test_secrets_are_redacted_but_still_detected():
    r = by_name(diff_manifests(BASE, TARGET))
    assert r["creds"].status == "changed"
    assert "old" not in r["creds"].diff and "new" not in r["creds"].diff
    assert "REDACTED" in r["creds"].diff
    shown = by_name(diff_manifests(BASE, TARGET, show_secrets=True))
    assert "password: new" in shown["creds"].diff


def test_ignore_labels_hides_version_noise():
    r = by_name(diff_manifests(BASE, TARGET, ignore_labels=["helm.sh/chart"]))
    assert r["web"].changed_paths == ["spec.template.spec.containers[name=sidecar].image"]


def test_kind_filter():
    r = diff_manifests(BASE, TARGET, kinds=["secret"])
    assert [c.key.kind for c in r.changes] == ["Secret"]


def test_changed_paths_reorder_only():
    a = {"env": [{"name": "A", "value": 1}, {"name": "B", "value": 2}]}
    b = {"env": [{"name": "B", "value": 2}, {"name": "A", "value": 1}]}
    assert changed_paths(a, b) == ["env"]


def test_git_base_detects_values_and_template_changes(repo: Path, helm_bin):
    chart = repo / "charts" / "demo"
    (chart / "values.yaml").write_text((chart / "values.yaml").read_text().replace("replicaCount: 2", "replicaCount: 5"))
    (chart / "templates" / "service.yaml").write_text(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: {{ .Release.Name }}-svc\n"
    )
    result = run_diff(chart, [], RenderOptions(helm_bin=helm_bin, release="rel"))
    r = by_name(result)
    assert result.base_label.startswith("HEAD@")
    assert r["rel-web"].changed_paths == ["spec.replicas"]
    assert r["rel-svc"].status == "added"
    assert result.unchanged == 2  # ConfigMap and Secret


def test_git_base_uses_values_file_as_of_ref(repo: Path, helm_bin):
    prod = repo / "envs" / "prod.yaml"
    prod.write_text("replicaCount: 9\nlogLevel: debug\n")
    result = run_diff(repo / "charts" / "demo", [prod], RenderOptions(helm_bin=helm_bin, release="rel"))
    r = by_name(result)
    assert "-  replicas: 4" in r["rel-web"].diff and "+  replicas: 9" in r["rel-web"].diff
    assert "+    log.level=debug" in r["rel-config"].diff  # multi-line payloads diff per line


def test_older_ref_and_committed_changes(repo: Path, helm_bin):
    chart = repo / "charts" / "demo"
    (chart / "values.yaml").write_text((chart / "values.yaml").read_text().replace('tag: "1.27"', 'tag: "1.28"'))
    git(repo, "commit", "-qam", "bump")
    opts = RenderOptions(helm_bin=helm_bin)
    assert not run_diff(chart, [], opts).has_changes  # HEAD == working tree
    assert run_diff(chart, [], opts, base_ref="HEAD~1").count("changed") == 1


def test_values_only_diff(repo: Path, helm_bin, tmp_path: Path):
    old, new = tmp_path / "old.yaml", tmp_path / "new.yaml"
    old.write_text("replicaCount: 1\n")
    new.write_text("replicaCount: 3\n")
    result = run_diff(repo / "charts" / "demo", [new], RenderOptions(helm_bin=helm_bin), base_values=[old])
    assert [c.changed_paths for c in result.changes] == [["spec.replicas"]]


def test_base_chart_directory(repo: Path, helm_bin, tmp_path: Path):
    import shutil

    other = tmp_path / "other"
    shutil.copytree(repo / "charts" / "demo", other)
    (other / "templates" / "secret.yaml").unlink()
    result = run_diff(repo / "charts" / "demo", [], RenderOptions(helm_bin=helm_bin), base_chart=other)
    assert [(c.key.kind, c.status) for c in result.changes] == [("Secret", "added")]


def test_cli_outputs_and_exit_codes(repo: Path, helm_bin, capsys):
    chart = str(repo / "charts" / "demo")
    assert main(["diff", chart, "--helm", helm_bin, "--exit-code"]) == 0
    assert "No differences" in capsys.readouterr().out

    # --set applies to both sides, so on its own it produces no diff
    assert main(["diff", chart, "--helm", helm_bin, "--set", "replicaCount=7", "--exit-code"]) == 0
    capsys.readouterr()
    values = repo / "charts" / "demo" / "values.yaml"
    values.write_text(values.read_text().replace("replicaCount: 2", "replicaCount: 7"))
    args = ["diff", chart, "--helm", helm_bin]
    assert main(args + ["--exit-code", "-o", "json"]) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["summary"] == {"added": 0, "removed": 0, "changed": 1, "unchanged": 2}
    assert data["changes"][0]["changedPaths"] == ["spec.replicas"]

    assert main(args + ["-o", "markdown"]) == 0
    md = capsys.readouterr().out
    assert "```diff" in md and "`Deployment/release-web`" in md


def test_cli_errors(repo: Path, helm_bin, tmp_path: Path, capsys):
    assert main(["diff", str(tmp_path), "--helm", helm_bin]) == 1
    assert "not a Helm chart" in capsys.readouterr().err
    # render failure is reported with the failing side
    chart = repo / "charts" / "demo"
    (chart / "templates" / "bad.yaml").write_text("x: {{ .Values.missing.key }}\n")
    assert main(["diff", str(chart), "--helm", helm_bin]) == 1
    assert "target side" in capsys.readouterr().err
    assert main(["diff", str(chart), "--helm", "no-such-helm-binary"]) == 1
    assert "not found on PATH" in capsys.readouterr().err


def test_not_a_git_repo(helm_bin, tmp_path: Path, repo: Path, capsys):
    import shutil

    lone = tmp_path / "lone"
    shutil.copytree(repo / "charts" / "demo", lone)
    assert main(["diff", str(lone), "--helm", helm_bin]) == 1
    assert "--base-chart" in capsys.readouterr().err


def test_inspect(repo: Path, helm_bin, capsys):
    assert main(["inspect", str(repo / "charts" / "demo"), "--helm", helm_bin, "-o", "json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["name"] == "demo" and info["values"]["image"] == "map(2 keys)"
    assert {r["kind"] for r in info["resources"]} == {"Deployment", "ConfigMap", "Secret"}


# ----------------------------- check-values ------------------------------ #

from chartdelta.values_check import check_values  # noqa: E402


def _check(repo, helm_bin, tmp_path, text, **kw):
    vf = tmp_path / "user.yaml"
    vf.write_text(text)
    found = check_values(repo / "charts" / "demo", [vf], RenderOptions(helm_bin=helm_bin), **kw)
    return {f.path: f for f in found}


def test_check_values_flags_typo_and_wrong_nesting(repo, helm_bin, tmp_path):
    found = _check(repo, helm_bin, tmp_path, "replicacount: 3\ntag: '1.28'\nimage:\n  tga: x\nlogLevel: debug\n")
    assert set(found) == {"replicacount", "tag", "image.tga"}
    assert found["replicacount"].no_effect is True
    assert "replicaCount" in found["replicacount"].suggestion
    assert "image.tag" in found["tag"].suggestion and "nesting" in found["tag"].suggestion
    assert "image.tag" in found["image.tga"].suggestion


def test_check_values_accepts_keys_only_used_in_templates(repo, helm_bin, tmp_path):
    chart = repo / "charts" / "demo"
    (chart / "templates" / "extra.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: extra\ndata:\n  v: \"{{ .Values.optional.flag }}\"\n"
    )
    (chart / "values.yaml").write_text((chart / "values.yaml").read_text() + "optional: {}\nfreeform: {}\n")
    # optional.flag is not in defaults but is referenced; freeform.* is an open map
    assert _check(repo, helm_bin, tmp_path, "optional:\n  flag: on\nfreeform:\n  anything: 1\n") == {}


def test_check_values_probe_clears_keys_that_do_change_output(repo, helm_bin, tmp_path):
    chart = repo / "charts" / "demo"
    # 'extraData' is read through a parent passed whole, invisible to the static check
    (chart / "templates" / "dump.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: dump\ndata:\n  all: \"{{ .Values.passthrough }}\"\n"
    )
    (chart / "values.yaml").write_text((chart / "values.yaml").read_text() + "passthrough:\n  a: 1\n")
    static = _check(repo, helm_bin, tmp_path, "passthrough:\n  extraData: 2\n", probe=False)
    assert "may still be used" in static["passthrough.extraData"].message
    assert _check(repo, helm_bin, tmp_path, "passthrough:\n  extraData: 2\n") == {}


def test_check_values_type_mismatch_and_cli(repo, helm_bin, tmp_path, capsys):
    found = _check(repo, helm_bin, tmp_path, "image: nginx:1.28\n", probe=False)
    assert found["image"].kind == "type-mismatch"
    vf = tmp_path / "user.yaml"
    chart = str(repo / "charts" / "demo")
    vf.write_text("replicas: 3\n")
    assert main(["check-values", chart, "-f", str(vf), "--helm", helm_bin]) == 2
    assert "IGNORED  replicas" in capsys.readouterr().out
    vf.write_text("replicaCount: 3\n")
    assert main(["check-values", chart, "-f", str(vf), "--helm", helm_bin, "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"findings": []}


def test_check_values_subchart(repo, helm_bin, tmp_path):
    chart = repo / "charts" / "demo"
    sub = chart / "charts" / "cache"
    (sub / "templates").mkdir(parents=True)
    (sub / "Chart.yaml").write_text("apiVersion: v2\nname: cache\nversion: 1.0.0\n")
    (sub / "values.yaml").write_text("size: 1\n")
    with (chart / "Chart.yaml").open("a") as f:
        f.write("dependencies:\n  - name: cache\n    version: 1.0.0\n    condition: cache.enabled\n")
    found = _check(repo, helm_bin, tmp_path, "cache:\n  enabled: true\n  sizee: 2\nglobal:\n  x: 1\n", probe=False)
    assert set(found) == {"cache.sizee"}
    assert "cache.size" in found["cache.sizee"].suggestion or "size" in found["cache.sizee"].suggestion


def test_render(repo, helm_bin, capsys):
    import yaml

    chart = str(repo / "charts" / "demo")
    prod = str(repo / "envs" / "prod.yaml")
    assert main(["render", chart, "-f", prod, "--helm", helm_bin, "-r", "rel"]) == 0
    out = capsys.readouterr().out
    docs = {d["kind"]: d for d in yaml.safe_load_all(out)}
    assert docs["Deployment"]["spec"]["replicas"] == 4  # prod.yaml applied
    assert "hunter2" not in out and "REDACTED" in out
    assert "# Source: demo/templates/deployment.yaml" in out

    assert main(["render", chart, "--helm", helm_bin, "--kind", "secret", "--show-secrets"]) == 0
    out = capsys.readouterr().out
    assert "hunter2" in out and "Deployment" not in out

    assert main(["render", chart, "--helm", helm_bin, "--list", "--name", "config"]) == 0
    assert capsys.readouterr().out.strip() == "ConfigMap/release-config  [demo/templates/configmap.yaml]"


# ------------------------------- upgrade --------------------------------- #

CRD_V1 = """\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.example.com
spec:
  group: example.com
  versions:
    - name: v1alpha1
      served: true
      storage: true
      schema: {openAPIV3Schema: {type: object, properties: {spec: {type: object, properties: {size: {type: integer}}}}}}
"""
CRD_V2 = """\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.example.com
spec:
  group: example.com
  versions:
    - name: v1
      served: true
      storage: true
      schema: {openAPIV3Schema: {type: object, properties: {spec: {type: object, properties: {size: {type: string}}}}}}
"""


def _two_versions(repo, tmp_path, monkeypatch):
    import shutil

    src = repo / "charts" / "demo"
    store = tmp_path / "store"
    v1, v2 = store / "demo-0.1.0", store / "demo-0.2.0"
    shutil.copytree(src, v1)
    shutil.copytree(src, v2)
    (v1 / "crds").mkdir(); (v1 / "crds" / "widget.yaml").write_text(CRD_V1)
    (v2 / "crds").mkdir(); (v2 / "crds" / "widget.yaml").write_text(CRD_V2)
    # 0.2.0 renames replicaCount -> replicas, bumps the image, adds a Service
    (v2 / "Chart.yaml").write_text("apiVersion: v2\nname: demo\nversion: 0.2.0\nappVersion: '2.0'\n")
    vals = (v2 / "values.yaml").read_text().replace("replicaCount: 2", "replicas: 2").replace('"1.27"', '"1.28"').replace('"0.1.0"', '"0.2.0"')
    (v2 / "values.yaml").write_text(vals)
    dep = v2 / "templates" / "deployment.yaml"
    dep.write_text(dep.read_text().replace(".Values.replicaCount", ".Values.replicas"))
    (v2 / "templates" / "service.yaml").write_text("apiVersion: v1\nkind: Service\nmetadata:\n  name: {{ .Release.Name }}-svc\n")
    monkeypatch.setenv("FAKE_HELM_REPO", str(store))
    return v1, v2


needs_fake_pull = pytest.mark.skipif(
    os.environ.get("CHARTDELTA_REAL_HELM") == "1",
    reason="uses the stand-in's fake 'helm pull'; real pulls need a chart repository",
)


@needs_fake_pull
def test_upgrade_report(repo, helm_bin, tmp_path, monkeypatch, capsys):
    _two_versions(repo, tmp_path, monkeypatch)
    mine = tmp_path / "mine.yaml"
    mine.write_text("replicaCount: 6\n")
    args = ["upgrade", "myrepo/demo", "--from", "0.1.0", "--to", "0.2.0", "-f", str(mine), "--helm", helm_bin]
    assert main(args + ["-o", "json", "--exit-code"]) == 2
    d = json.loads(capsys.readouterr().out)
    assert (d["from"], d["to"]) == ("0.1.0", "0.2.0")
    assert d["metadataChanges"]["appVersion"] == ["1.0", "2.0"]
    crd = d["crds"][0]
    assert crd["versions_removed"] == ["v1alpha1"] and crd["versions_added"] == ["v1"]
    assert (crd["storage_from"], crd["storage_to"], crd["severity"]) == ("v1alpha1", "v1", "breaking")
    res = {c["kind"]: c for c in d["resources"]["changes"]}
    assert res["Service"]["status"] == "added"
    # my replicaCount: 6 no longer applies -> replicas falls back to the default
    assert "spec.replicas" in res["Deployment"]["changedPaths"]
    assert not any("helm.sh/chart" in p for p in res["Deployment"]["changedPaths"])
    assert d["defaults"]["removed"] == {"replicaCount": 2} and d["defaults"]["added"] == {"replicas": 2}
    assert d["defaults"]["changed"]["image.tag"] == ["1.27", "1.28"]
    finding = d["valuesFindings"][0]
    assert finding["path"] == "replicaCount" and finding["newInThisUpgrade"] is True

    assert main(args) == 0
    text = capsys.readouterr().out
    assert "BREAKING changed: widgets.example.com" in text and "version removed: v1alpha1 (was served)" in text and "storage version v1alpha1 -> v1" in text
    assert "NOT updated by 'helm upgrade'" in text and "stops working in this upgrade" in text
    assert main(args + ["-o", "markdown", "--summary"]) == 0
    assert "### CRDs (1 changed)" in capsys.readouterr().out


@needs_fake_pull
def test_upgrade_with_directories_and_bad_version(repo, helm_bin, tmp_path, monkeypatch, capsys):
    v1, v2 = _two_versions(repo, tmp_path, monkeypatch)
    assert main(["upgrade", "--from", str(v1), "--to", str(v2), "--helm", helm_bin, "--summary"]) == 0
    assert "demo upgrade: 0.1.0 -> 0.2.0" in capsys.readouterr().out
    assert main(["upgrade", "myrepo/demo", "--from", "0.1.0", "--to", "9.9.9", "--helm", helm_bin]) == 1
    assert "helm pull" in capsys.readouterr().err
    assert main(["upgrade", "--from", "0.1.0", "--to", "0.2.0", "--helm", helm_bin]) == 1
    assert "not a chart directory" in capsys.readouterr().err


def test_chart_yaml_path_is_accepted(repo, helm_bin, capsys):
    assert main(["render", str(repo / "charts" / "demo" / "Chart.yaml"), "--helm", helm_bin, "--list"]) == 0
    assert "Deployment/release-web" in capsys.readouterr().out


def test_crd_severity_check_vs_breaking(repo, helm_bin, tmp_path, monkeypatch, capsys):
    v1, v2 = _two_versions(repo, tmp_path, monkeypatch)
    # old version stays served in the new chart; only the storage version moves
    both = CRD_V2.replace("    - name: v1\n", "    - name: v1alpha1\n      served: true\n      storage: false\n    - name: v1\n")
    (v2 / "crds" / "widget.yaml").write_text(both)
    args = ["upgrade", "--from", str(v1), "--to", str(v2), "--helm", helm_bin, "--summary"]
    assert main(args + ["--exit-code"]) == 0  # nothing breaking
    text = capsys.readouterr().out
    assert "CHECK changed: widgets.example.com" in text and "BREAKING" not in text


def test_list_wrapper_is_expanded():
    from chartdelta.core import parse_manifests

    text = """\
# Source: c/templates/ingressclass.yaml
apiVersion: v1
kind: List
metadata: {name: ingress-class}
items:
  - apiVersion: elbv2.k8s.aws/v1beta1
    kind: IngressClassParams
    metadata: {name: alb}
  - apiVersion: networking.k8s.io/v1
    kind: IngressClass
    metadata: {name: alb}
"""
    res = parse_manifests(text)
    assert sorted(str(k) for k in res) == ["IngressClass/alb", "IngressClassParams/alb"]
    assert all(r.source == "c/templates/ingressclass.yaml" for r in res.values())
    changed = diff_manifests(text, text.replace("IngressClassParams\n    metadata: {name: alb}", "IngressClassParams\n    metadata: {name: alb2}"))
    assert sorted((c.status, str(c.key)) for c in changed.changes) == [("added", "IngressClassParams/alb2"), ("removed", "IngressClassParams/alb")]
