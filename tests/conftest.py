import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

TEMPLATES = {
    "deployment.yaml": """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}-web
  labels:
    app.kubernetes.io/name: web
    helm.sh/chart: demo-{{ .Values.chartLabel }}
spec:
  replicas: {{ .Values.replicaCount }}
  selector:
    matchLabels:
      app.kubernetes.io/name: web
  template:
    metadata:
      labels:
        app.kubernetes.io/name: web
    spec:
      containers:
        - name: web
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          env:
            - name: LOG_LEVEL
              value: "{{ .Values.logLevel }}"
""",
    "configmap.yaml": """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-config
data:
  app.properties: |
    log.level={{ .Values.logLevel }}
    feature.enabled=true
""",
    "secret.yaml": """\
apiVersion: v1
kind: Secret
metadata:
  name: {{ .Release.Name }}-secret
stringData:
  password: "{{ .Values.password }}"
""",
}

VALUES = """\
replicaCount: 2
chartLabel: "0.1.0"
logLevel: info
password: hunter2
image:
  repository: nginx
  tag: "1.27"
"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True, capture_output=True,
    )


@pytest.fixture(scope="session")
def helm_bin(tmp_path_factory) -> str:
    """Real helm when CHARTDELTA_REAL_HELM=1 (CI), otherwise the bundled stand-in."""
    if os.environ.get("CHARTDELTA_REAL_HELM") == "1":
        real = shutil.which("helm")
        assert real, "CHARTDELTA_REAL_HELM=1 but helm is not on PATH"
        return real
    wrapper = tmp_path_factory.mktemp("bin") / "helm"
    fake = Path(__file__).parent / "fake_helm.py"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo containing charts/demo and envs/prod.yaml, committed once."""
    root = tmp_path / "repo"
    chart = root / "charts" / "demo"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: demo\nversion: 0.1.0\nappVersion: '1.0'\n")
    (chart / "values.yaml").write_text(VALUES)
    for name, body in TEMPLATES.items():
        (chart / "templates" / name).write_text(body)
    (root / "envs").mkdir()
    (root / "envs" / "prod.yaml").write_text("replicaCount: 4\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture(scope="session")
def go_engine(tmp_path_factory, helm_bin):
    """Go-template engine for ApplicationSet tests: real helm, or a Go stand-in built on the fly."""
    if os.environ.get("CHARTDELTA_REAL_HELM") == "1":
        return
    if not shutil.which("go"):
        pytest.skip("ApplicationSet tests need real helm (CHARTDELTA_REAL_HELM=1) or the Go toolchain")
    binary = tmp_path_factory.mktemp("engine") / "engine"
    src = Path(__file__).parent / "fake_helm_engine" / "main.go"
    built = subprocess.run(["go", "build", "-o", str(binary), str(src)], capture_output=True, text=True)
    if built.returncode != 0:
        pytest.skip(f"could not build the Go template stand-in: {built.stderr}")
    os.environ["FAKE_HELM_ENGINE"] = str(binary)
