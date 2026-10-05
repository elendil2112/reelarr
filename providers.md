# Writing a provider

A provider is an optional add-on that brings shows **into** Reelarr's watch
folder from somewhere else — a store you subscribe to, a private tracker, a
friend's server. Reelarr's core knows nothing about any of them; an installed
provider plugs itself in, and an absent one leaves no trace in the UI.

## The contract

1. **Deliver, don't file.** A provider puts each finished show in the watch
   folder as its own folder, and stops there. Reelarr identifies, tags and
   files it like anything else — so dry-run, trash and undo apply to provider
   shows exactly as they do to everything else.
2. **Stage out of sight.** Download into a folder whose name starts with `.`
   or `_` inside the watch folder (e.g. `.myprovider_staging/`). The watcher
   ignores those, so half-finished downloads are never picked up, and moving a
   finished show out is an instant same-drive rename.
3. **Say what you know.** Drop a `.reelarr.json` sidecar in the show folder.
   These keys are read (all optional):

   ```json
   {"official": "Official", "source_type": "SBD", "shnid": "12345",
    "source": "my-provider"}
   ```

   `official` marks the show as a commercial release — it scores higher, and
   a fan recording can never replace it. `source_type` is authoritative over
   anything parsed from folder names or info files. `shnid` carries a release
   or etree id into the album tag. `source` is recorded as where the show came
   from. Artist, date and venue still come from the files themselves (tags,
   folder name, info file), so name the folder and tag the audio well.

## Shape

A provider is an importable Python package with a module-level `provider`:

```python
from app.providers import Provider

provider = Provider(
    name="example",                    # URL-safe id; also its settings group
    label="Example Store",             # menu label
    settings_defaults={"enabled": False, "api_token": ""},
    secret_fields=("api_token",),      # masked in the UI like core's secrets
    router=router,                     # FastAPI APIRouter → /api/providers/example/…
    static_dir=Path(__file__).parent / "static",
    script="example.js",               # served at /providers/example/static/example.js
    settings_layout=[["enabled", "checkbox", "Turn it on"],
                     ["api_token", "password", "Your token"]],
    status=lambda: {...},              # appears as s.providers.example in /api/status
    schedule=lambda scheduler: ...,    # add APScheduler jobs; called on every reschedule
)
```

## Discovery

Reelarr loads every package that declares an entry point in the
`reelarr.providers` group:

```toml
[project.entry-points."reelarr.providers"]
example = "reelarr_example"
```

…plus any module named in the `REELARR_PROVIDERS` environment variable
(comma-separated), which is handy in a Docker image.

## UI

The provider's script runs after the core page loads and uses `window.Reelarr`:

| call | does |
|---|---|
| `Reelarr.addView(id, label, html, onShow)` | adds a menu entry and a page |
| `Reelarr.onStatus(fn)` | `fn(status)` on every status poll (~6 s) |
| `Reelarr.api.get/post/put(url, body)` | JSON fetch; a 401 sends the user to sign-in |
| `Reelarr.$`, `$$`, `esc`, `fmtTime`, `refreshStatus` | the core page's helpers |

Its settings appear automatically as their own group under Settings, from
`settings_layout`.

## Indexers

An indexer is something Reelarr can *search* — Discover and monitored
Artists use it. Implement `app.indexers.base.Indexer` (`search`,
`new_for_artist`, `check_downloadable`, `grab`), return `Release` objects,
and register it from your provider package at import time:

```python
from app import indexers
indexers.register(MyIndexer())
```

It then appears in Discover's indexer menu and in each artist's settings.
`grab` should put whatever the download client needs (a `.torrent`) into the
`.torrent` folder; Reelarr's torrent flow hands it on from there.
