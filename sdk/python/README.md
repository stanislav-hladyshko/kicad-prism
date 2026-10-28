# KiCAD Prism Python SDK and `prism` CLI

A small, dependency-free client for KiCAD Prism's `/api/v1` API, for scripts,
automation and CI. It is generated from the committed OpenAPI document, so it
tracks the API rather than a hand-maintained list of routes.

**It is not on PyPI yet.** Install it from this repository.

## Install

```bash
pip install ./sdk/python
```

The package installs nothing beyond the Python standard library
(`urllib.request` does the HTTP). Python 3.10 or newer is required.

## Configure

The client reads two environment variables:

| Variable | Meaning |
| --- | --- |
| `PRISM_URL` | Prism's origin, for example `http://localhost:8000` |
| `PRISM_TOKEN` | A personal access token, created in Prism under **Settings → Personal access tokens** |

```bash
export PRISM_URL=http://localhost:8000
export PRISM_TOKEN=prism_pat_...
```

`Client(base_url, token=...)` takes both explicitly; an argument wins over the
environment. A base URL that already ends in `/api/v1` is accepted too.

## Library

```python
from prism import Client, PrismError

client = Client("http://localhost:8000", token="prism_pat_...")

projects = client.list_projects()                       # list[Project]
bom = client.get_project_bom("project-id", assembly="assembly")
component = client.get_design_component("project-id", "R1", revision="HEAD")
net = client.get_design_net("project-id", "USB_D+")

for event in client.iter_events(after=0):               # follows next
    print(event["kind"], event["topic"])

job = client.wait_for_job("job-id", timeout=600, poll=2)
print(job["status"])                                    # completed | failed | cancelled

try:
    client.get_project_detail("missing")
except PrismError as error:
    print(error.status, error.code, error.detail)
```

Every method on `Client` is named after its OpenAPI operation id: path
parameters are positional, query parameters are keyword-only, and an operation
with a JSON body takes `body`. Writes carry a fresh `Idempotency-Key`
automatically. The generated module is `prism/_generated.py`; `Client` adds
auth, errors, `iter_events`, and `wait_for_job` in `prism/client.py`.

Tests inject a transport instead of touching the network:

```python
def transport(method, url, headers, body):
    return 200, {"content-type": "application/json"}, b"[]"

client = Client("http://localhost:8000", transport=transport)
```

## CLI

```bash
prism projects list [--json]
prism project <id> [--json]
prism bom <project> [--revision R] [--assembly all|assembly] [--csv] [--json]
prism design search <project> <query> [--revision R] [--json]
prism design component <project> <ref> [--revision R] [--json]
prism design net <project> <name> [--revision R] [--json]
prism comment add <project> (--component REF | --net NAME | --sheet PATH) [--commit SHA] [--severity S] [--class C] <text> [--json]
prism comment reply <project> <comment_id> <text> [--json]
prism comment list <project> [--json]
prism sync <project> [--wait] [--json]
prism release build <project> --commit SHA [--config KEY] [--variant V] [--wait] [--json]
prism compare <project> <base> <head> [--wait] [--json]
prism events [--after N] [--follow] [--json]
prism job <id> [--wait] [--json]
```

Human-readable tables are the default; `--json` prints the API body. `bom`
prints CSV with `--csv`. A text argument of `-` reads standard input. `--wait`
polls the queued job and exits 1 when it fails or is cancelled. A refused
request exits 1 and prints `code: detail` on stderr; a missing base URL exits 2.

Examples:

```bash
prism projects list
prism bom my-board --revision v1.2.0 --assembly assembly --csv > bom.csv
prism design search my-board "10k"
prism design net my-board "/USB_D+"
prism comment add my-board --component R1 "check the derating" --severity warning
prism comment reply my-board 0f3c... "confirmed"
prism comment list my-board
prism sync my-board --wait
prism release build my-board --commit abc123 --wait
prism compare my-board v1.1.0 v1.2.0 --wait --json > comparison.json
prism events --follow
prism job 0f3c... --wait
```

## Regenerate

The API document and the generated client are committed. After changing a
route or its models, run from the repository root:

```bash
cd backend
python scripts/export_openapi.py          # writes sdk/python/openapi.json
cd ..
python sdk/python/tools/generate.py       # writes sdk/python/prism/_generated.py
python -m unittest discover -s sdk/python/tests
```

`backend/tests/test_sdk_drift.py` fails when either committed file is stale, and
it runs the SDK's own tests under the backend's Python.

## Version

The version lives in `sdk/python/prism/_version.py` and starts at `0.0.0+dev`.
At release time it is rewritten from the release tag (`v4.0.0` becomes
`4.0.0`) before the wheel is built; the release archive ships that wheel. The
package is designed to be published (Apache-2.0, its own package metadata and
tests) but publishing is a separate decision.
