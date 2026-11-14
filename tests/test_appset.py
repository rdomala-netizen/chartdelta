"""ApplicationSet preview tests, built around a real-world matrix(git files x clusters) set."""
import json
from pathlib import Path

import pytest

from chartdelta.appset import clusters_from_secrets, normalise_url
from chartdelta.cli import main

from conftest import git

pytestmark = pytest.mark.usefixtures("go_engine")

APPSET = (Path(__file__).parent / "addons_appset.yaml").read_text()

CLUSTERS = """\
clusters:
  - name: dev
    server: https://dev:6443
    labels: {env: dev, cloud: kind, cluster_name: dev, enable_kyverno: "true"}
  - name: prod
    server: https://prod:6443
    labels: {env: prod, cloud: aws, cluster_name: prod, enable_kyverno: "true", enable_certmgr: "true"}
  - name: canary
    server: https://canary:6443
    labels: {env: prod, ring: canary, cloud: aws, cluster_name: canary, enable_kyverno: "true"}
  - name: pinned
    server: https://pinned:6443
    labels: {env: dev, cloud: kind, cluster_name: pinned, enable_kyverno: "true", kyverno_version: "0.9.0"}
  - name: spare
    server: https://spare:6443
    labels: {env: dev, cloud: kind, cluster_name: spare}
"""


def _chart(d: Path, name: str, version: str) -> None:
    (d / "templates").mkdir(parents=True)
    (d / "Chart.yaml").write_text(f"apiVersion: v2\nname: {name}\nversion: {version}\n")
    (d / "values.yaml").write_text(f"replicas: 1\nimage: {name}:{version}\nlogLevel: info\n")
    (d / "templates" / "deploy.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{ .Release.Name }}\n"
        "  namespace: {{ .Release.Namespace }}\nspec:\n  replicas: {{ .Values.replicas }}\n"
        "  template:\n    spec:\n      containers:\n        - name: main\n          image: {{ .Values.image }}\n"
        "          args: [\"--log={{ .Values.logLevel }}\"]\n")


@pytest.fixture
def gitops(tmp_path: Path) -> Path:
    root = tmp_path / "argocd-manifests"
    (root / "appsets").mkdir(parents=True)
    (root / "appsets" / "addons.yaml").write_text(APPSET)
    (root / "clusters.yaml").write_text(CLUSTERS)
    k = root / "addons" / "kyverno"
    for v in ("0.9.0", "1.0.0", "1.1.0"):
        _chart(k / v, "kyverno", v)
    (k / "addon.yaml").write_text(
        "addon:\n  name: kyverno\n  label: kyverno\n  namespace: kyverno\n  releaseName: kyverno\n"
        "  versions: {dev: 1.0.0, prod: 1.0.0, canary: 1.1.0}\n")
    (k / "common").mkdir()
    (k / "common" / "values.yaml").write_text("logLevel: warn\n")
    (k / "common" / "values-dev.yaml").write_text("replicas: 1\n")
    (k / "common" / "values-prod.yaml").write_text("replicas: 3\n")
    c = root / "addons" / "cert-manager"
    _chart(c / "2.0.0", "cert-manager", "2.0.0")
    (c / "addon.yaml").write_text(
        "addon:\n  name: cert-manager\n  label: certmgr\n  namespace: cert-manager\n  releaseName: cert-manager\n"
        "  versions: {dev: 2.0.0, prod: 2.0.0}\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "remote", "add", "origin", "git@github.com:rdomala-netizen/argocd-manifests.git")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


def run(gitops, helm_bin, capsys, *extra, cmd="diff"):
    code = main(["appset", cmd, str(gitops / "appsets" / "addons.yaml"), "--clusters", str(gitops / "clusters.yaml"),
                 "--helm", helm_bin, *extra])
    return code, capsys.readouterr()


def test_url_normalisation():
    a = normalise_url("https://github.com/rdomala-netizen/argocd-manifests.git")
    assert a == normalise_url("git@github.com:rdomala-netizen/argocd-manifests.git") == "github.com/rdomala-netizen/argocd-manifests"


def test_generate_applies_matrix_selector_and_version_precedence(gitops, helm_bin, capsys):
    code, out = run(gitops, helm_bin, capsys, "-o", "json", cmd="generate")
    assert code == 0, out.err
    apps = {a["metadata"]["name"]: a for a in json.loads(out.out)["applications"]}
    # the spare cluster has no enable_ label; cert-manager is only switched on for prod
    assert sorted(apps) == ["cert-manager-prod", "kyverno-canary", "kyverno-dev", "kyverno-pinned", "kyverno-prod"]
    path = lambda n: apps[n]["spec"]["source"]["path"]
    assert path("kyverno-dev") == "addons/kyverno/1.0.0"       # versions.<env>
    assert path("kyverno-canary") == "addons/kyverno/1.1.0"    # ring label wins over env
    assert path("kyverno-pinned") == "addons/kyverno/0.9.0"    # cluster pin wins over everything
    dev = apps["kyverno-dev"]
    assert dev["metadata"]["labels"] == {"addon": "kyverno", "env": "dev", "ring": "main"}
    assert dev["metadata"]["namespace"] == "argocd"
    assert dev["spec"]["destination"] == {"server": "https://dev:6443", "namespace": "kyverno"}
    assert dev["spec"]["source"]["helm"]["valueFiles"][2:] == [
        "../common/values-kind.yaml", "../common/values-dev.yaml", "../common/values-dev.yaml"]
    info = {i["name"]: i for i in json.loads(out.out)["sources"]}
    assert "../common/values-kind.yaml" in info["kyverno-dev"]["missingValueFiles"]


def test_diff_no_changes(gitops, helm_bin, capsys):
    code, out = run(gitops, helm_bin, capsys, "--exit-code")
    assert code == 0, out.err
    assert "5 unchanged" in out.out and "No Application is affected." in out.out


def test_values_change_only_touches_the_apps_that_use_it(gitops, helm_bin, capsys):
    (gitops / "addons" / "kyverno" / "common" / "values-prod.yaml").write_text("replicas: 5\n")
    code, out = run(gitops, helm_bin, capsys, "-o", "json", "--exit-code")
    assert code == 2, out.err
    apps = {a["name"]: a for a in json.loads(out.out)["applications"]}
    assert {n for n, a in apps.items() if a["status"] == "changed"} == {"kyverno-prod", "kyverno-canary"}
    assert apps["kyverno-dev"]["status"] == "unchanged"
    change = apps["kyverno-prod"]["resources"]["changes"][0]
    assert (change["kind"], change["name"], change["changedPaths"]) == ("Deployment", "kyverno", ["spec.replicas"])
    assert apps["kyverno-prod"]["specDiff"] == ""


def test_version_bump_changes_app_spec_and_manifests(gitops, helm_bin, capsys):
    f = gitops / "addons" / "kyverno" / "addon.yaml"
    f.write_text(f.read_text().replace("dev: 1.0.0", "dev: 1.1.0"))
    code, out = run(gitops, helm_bin, capsys)
    assert code == 0, out.err
    text = out.out
    assert "~ kyverno-dev  (changed, https://dev:6443)" in text
    assert "-    path: addons/kyverno/1.0.0" in text and "+    path: addons/kyverno/1.1.0" in text
    assert "+        image: kyverno:1.1.0" in text
    assert "kyverno-pinned" not in text  # pinned cluster ignores the bump


def test_new_addon_and_missing_version_folder(gitops, helm_bin, capsys):
    f = gitops / "addons" / "cert-manager" / "addon.yaml"
    f.write_text(f.read_text().replace("prod: 2.0.0", "prod: 9.9.9"))     # folder does not exist
    m = gitops / "addons" / "metrics"
    _chart(m / "0.1.0", "metrics", "0.1.0")
    (m / "addon.yaml").write_text(
        "addon:\n  name: metrics\n  label: kyverno\n  namespace: kube-system\n  releaseName: metrics\n"
        "  versions: {dev: 0.1.0, prod: 0.1.0}\n")
    code, out = run(gitops, helm_bin, capsys, "--summary", "--app", "metrics-*", "--app", "cert-*")
    assert code == 1
    assert "+ metrics-dev  (added, https://dev:6443)" in out.out
    assert "! cert-manager-prod" in out.out and "addons/cert-manager/9.9.9' does not exist" in out.out
    assert "kyverno-" not in out.out


def test_markdown_and_other_repo_mapping(gitops, helm_bin, tmp_path, capsys):
    # the ApplicationSet file kept outside the source repo: map the repo explicitly
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text(APPSET)
    (gitops / "addons" / "kyverno" / "common" / "values-dev.yaml").write_text("replicas: 1\nlogLevel: debug\n")
    args = ["appset", "diff", str(elsewhere), "--clusters", str(gitops / "clusters.yaml"), "--helm", helm_bin]
    assert main(args) == 1
    assert "--repo https://github.com/rdomala-netizen/argocd-manifests.git=" in capsys.readouterr().err
    code = main(args + ["--repo", f"https://github.com/rdomala-netizen/argocd-manifests.git={gitops}", "-o", "markdown", "--summary"])
    md = capsys.readouterr().out
    assert code == 0 and "### ~ kyverno-dev" in md and "`spec.template.spec.containers[name=main].args[0]`" in md


def test_template_error_names_the_field(gitops, helm_bin, capsys):
    f = gitops / "appsets" / "addons.yaml"
    f.write_text(f.read_text().replace("name: '{{ .addon.name }}-{{ .name }}'", "name: '{{ .addon.name | nosuchfunc }}'"))
    code, out = run(gitops, helm_bin, capsys)
    assert code == 1 and "Go template error" in out.err


def test_clusters_from_secrets_drops_credentials():
    import base64

    b = lambda s: base64.b64encode(s.encode()).decode()
    secrets = {"items": [{
        "metadata": {"name": "cluster-dev", "labels": {"argocd.argoproj.io/secret-type": "cluster", "env": "dev"}},
        "data": {"name": b("dev"), "server": b("https://dev:6443"), "config": b('{"bearerToken":"SECRET"}')},
    }]}
    out = clusters_from_secrets(secrets)
    assert out == [{"name": "dev", "server": "https://dev:6443", "labels": {"env": "dev"}}]
    assert "SECRET" not in json.dumps(out)
