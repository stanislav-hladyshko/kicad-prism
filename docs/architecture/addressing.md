# Addressing Prism

Two grammars name a place in Prism: the HTTPS project URL the browser reads,
and a `prism://` resource URI for tools and tracker links. Backend code builds
both in `backend/app/core/links.py`; do not spell either beside a caller.

- `project_url(public_base_url, project_id, **params)` builds a browser URL.
  `None` and empty parameters are dropped, the parameters keep the order they
  are given, and the project id is one quoted path segment.
- `resource_uri(project_id, commit, kind, key=None)` and
  `parse_resource_uri(uri)` write and read the resource grammar.

## Project URLs

The route is `/project/<projectId>` (`frontend/src/App.tsx`, mounting
`ProjectDetailPage`). The plural `/projects/...` matches no route, so the
catch-all redirects it to `/`.

Query parameters are read by the project page and the feature panels it
mounts. Parameters a section does not use are ignored, and every value is a
string.

| Parameter | Selects | Values |
| --- | --- | --- |
| `section` | The open project panel. | `overview` (default), `history`, `visualizers`, `workflows`, `release-studio`, `assets`, `documentation`, `simulation-studio` (experimental; only when Simulation Studio is enabled). Any other value opens `overview`. |
| `branch` | The branch the page and viewer show. | A branch ref (`origin/test` for a remote branch) or its name; absent means the repository's current checkout. |
| `commit` | The pinned revision, and the commit to highlight in History. | A full commit sha. |
| `tab` | The visualizer's initial tab. | `sch` (default), `pcb`, `3d`, `bom`, `stackup`, `assembly`. Read once when the visualizer opens. |
| `variant` | The design variant rendered from the revision's assembly catalog. | A variant name; absent means the default assembly. |
| `build` | The Release Studio build shown. | A build id; without it the panel follows the newest build. |
| `sim` | The open Simulation Studio simulation. | A simulation kind, currently `dc-drop`. |
| `setup` | The open simulation setup. | A setup id. |
| `view` | The simulation studio tab, when `sim` is set. | `results`, or the setup tab when absent. |

### Design comparison

These open a comparison over the History section; `section=history` must be
present (or written with them) for the page to show it.

| Parameter | Selects | Values |
| --- | --- | --- |
| `base` | The old side of the comparison. | A full commit sha. `base` and `compare` together open the comparison. |
| `compare` | The new side of the comparison. | A full commit sha. |
| `view` | Marks the link as a comparison. | `semantic`; any other value reads as absent. |
| `diff` | The comparison tab. | `sch` (default), `pcb`, `bom`, `stackup`, `fabrication`. |
| `presentation` | The reviewer's presentation override. | `composite`, `side-by-side`, `old-new`; absent lets the policy follow the selected change. |
| `item` | The focused semantic change. | A change id. |
| `group` | The focused review group. | A group id. |
| `ref` | The designator focused inside `group`. | A reference designator; absent focuses the whole group. |
| `document` | The document (schematic sheet or board) the focus is read in. | A document path. |
| `secondary` | Whether secondary changes are listed. | `1` shows them; absent hides them. |
| `layers` | Visible PCB layers. | Comma-separated layer names, for example `F.Cu,B.Cu`. |

Tracker issue links also carry `comment` and `side` (`side` is `base` or
`compare`); no frontend reader consumes either yet.

## Resource URIs

```
prism://project/<projectId>/rev/<commit>/<kind>[/<key>]
```

- `projectId` is one quoted path segment.
- `commit` is a full 40-character hexadecimal sha, stored lowercase.
- `kind` is one of `component`, `net`, `bom`, `netlist`, `stackup`, `sheet`,
  `comparison`, `release`.
- `key` names one item of that kind, for example a reference designator or a
  net name, and is quoted as one path segment. It is absent when the resource
  is whole-kind.

`parse_resource_uri` returns a frozen `ResourceRef(project_id, commit, kind,
key)` and raises `ValueError` for any other spelling.

## Not addressable yet

Selection, highlighted nets, insets and camera are not addressable yet (v5.2,
ADR scripting-and-mcp §3.9).
