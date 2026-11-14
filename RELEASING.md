# Releasing a new version of chartdelta

Publishing a GitHub release triggers `.github/workflows/release.yml`, which
builds the package and uploads it to PyPI. No password or token is involved:
PyPI trusts this repository's `release.yml` workflow (trusted publishing).

## Before you start

- You are on the `main` branch with your changes committed.
- PyPI never accepts the same version number twice. Every release needs a
  new, higher version, even to fix a mistake in the previous one.

## Steps

### 1. Pick the new version number

| Kind of change | Example |
|---|---|
| Bug fix only | `0.5.1` -> `0.5.2` |
| New feature or option | `0.5.1` -> `0.6.0` |
| Breaking change (renamed command, changed output) | `0.5.1` -> `1.0.0` |

The examples below use `0.5.2`. Replace it with your number everywhere.

### 2. Update the version in the code

The version lives in exactly one place, `src/chartdelta/__init__.py`:

```python
__version__ = "0.5.2"
```

### 3. Run the tests

```sh
pip install -e ".[dev]"
pytest -q                          # expect: all passed
CHARTDELTA_REAL_HELM=1 pytest -q   # expect: all passed, 2 skipped
```

Do not release if anything fails.

### 4. Commit and push

```sh
git add -A
git commit -m "Release 0.5.2"
git push
```

Open the Actions tab and wait for the `ci` run on this commit to go green:
https://github.com/rdomala-netizen/chartdelta/actions

### 5. Publish the GitHub release

The tag is the version with a `v` in front.

Terminal:

```sh
gh release create v0.5.2 --title "v0.5.2" --notes "What changed in this version"
```

Or in the browser: repository page -> Releases -> Draft a new release ->
type `v0.5.2` as the tag and choose to create it -> add a title and notes ->
Publish release. A release saved as a draft does not trigger the upload.

### 6. Watch the upload

In the Actions tab, a `release` run starts within a few seconds and takes
about a minute. A green tick means the package is on PyPI.

### 7. Verify

```sh
pipx install --force chartdelta    # or: pipx upgrade chartdelta
chartdelta --version               # should print the new version
```

Also check https://pypi.org/project/chartdelta/ shows the new version. If
pipx still installs the old one right after a green run, wait a minute or two
and try again.

## If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `release` run fails with `invalid-publisher` | PyPI has no trusted publisher matching this repo and workflow | On PyPI -> Publishing, check the entry: owner `rdomala-netizen`, repository `chartdelta`, workflow `release.yml`. Then open the failed run and click "Re-run all jobs" |
| `release` run fails with "File already exists" | That version number is already on PyPI | Bump `__version__` to the next number and release again (steps 2 to 5) |
| No `release` run appears | The release is still a draft, or only a tag was pushed | Publish the release on the Releases page |
| `release` run is waiting | The `pypi` environment has a protection rule | Approve the run, or remove the rule under Settings -> Environments -> pypi |
| Wrong version on PyPI | The tag and `__version__` differ; the package version always comes from `__version__` | Bump `__version__` and release again with a matching tag |
| `pipx install` says "not found in the package registry" | The `release` run has not finished green yet | Check the Actions tab |

A failed run can be retried without a new release: open the run and click
"Re-run all jobs". A new release is only needed for a new version number.

## Installing without a PyPI release

To try unreleased code straight from GitHub:

```sh
pipx install --force git+https://github.com/rdomala-netizen/chartdelta.git
```
