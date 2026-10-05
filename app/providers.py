"""Optional providers: add-ons that bring shows INTO the watch folder.

Reelarr's core knows nothing about any particular store or downloader. A
provider is a separate Python package that, when installed, plugs in:

  * a settings group (with its secret fields masked like core's)
  * API routes, mounted under /api/providers/<name>/
  * a scheduler hook, called whenever the schedule is rebuilt
  * a status() for the dashboard
  * UI: a static folder (served at /providers/<name>/static/) whose script
    adds its own pages through window.Reelarr (see static/app.js)

What a provider must NOT do is file shows itself. It drops each show into
the watch folder — optionally with a .reelarr.json provenance sidecar — and
the normal pipeline takes it from there, with dry-run, trash and undo intact.

Discovery: every installed package that declares an entry point in the
"reelarr.providers" group, plus any module named in REELARR_PROVIDERS
(comma-separated). Each must expose a module-level `provider`.
A provider that isn't installed simply doesn't exist: no menu entry, no
settings, no routes.
"""
import importlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ENTRY_POINT_GROUP = "reelarr.providers"


@dataclass
class Provider:
    name: str                                   # url-safe id, e.g. "archive"
    label: str                                  # shown in the menu
    settings_defaults: dict = field(default_factory=dict)
    secret_fields: tuple = ()
    router: object = None                       # fastapi.APIRouter
    static_dir: Optional[Path] = None
    script: str = ""                            # file in static_dir
    views: list = field(default_factory=list)   # [{"id": "...", "label": "..."}]
    settings_layout: list = field(default_factory=list)   # same shape as app.js SETTINGS_LAYOUT items
    status: Callable[[], dict] = lambda: {}
    schedule: Callable[[object], None] = lambda scheduler: None
    on_startup: Callable[[], None] = lambda: None

    def describe(self) -> dict:
        return {"name": self.name, "label": self.label, "views": self.views,
                "settings_layout": self.settings_layout,
                "script": f"/providers/{self.name}/static/{self.script}" if self.script else ""}


_loaded: dict = {}
_errors: dict = {}


def load() -> dict:
    """Import every configured provider once. Safe to call repeatedly."""
    if _loaded or _errors:
        return _loaded
    from . import config
    names = [n.strip() for n in os.environ.get("REELARR_PROVIDERS", "").split(",") if n.strip()]
    try:
        from importlib.metadata import entry_points
        names += [ep.value.split(":")[0] for ep in entry_points(group=ENTRY_POINT_GROUP)]
    except Exception:
        pass
    for mod_name in dict.fromkeys(names):
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError as e:
            if e.name == mod_name:
                continue                      # not installed: that's normal
            _errors[mod_name] = f"missing dependency: {e.name}"
            continue
        except Exception as e:
            _errors[mod_name] = str(e)
            continue
        p = getattr(mod, "provider", None)
        if not isinstance(p, Provider):
            _errors[mod_name] = "has no `provider` object"
            continue
        config.register_defaults(p.name, p.settings_defaults, p.secret_fields)
        _loaded[p.name] = p
    return _loaded


def all() -> list:
    return list(load().values())


def errors() -> dict:
    return dict(_errors)


def mount(app):
    """Attach provider routes and static files to the FastAPI app."""
    from fastapi.staticfiles import StaticFiles
    for p in all():
        if p.router is not None:
            app.include_router(p.router, prefix=f"/api/providers/{p.name}")
        if p.static_dir and Path(p.static_dir).is_dir():
            app.mount(f"/providers/{p.name}/static",
                      StaticFiles(directory=str(p.static_dir)),
                      name=f"provider-{p.name}")


def statuses() -> dict:
    out = {}
    for p in all():
        try:
            out[p.name] = p.status()
        except Exception as e:
            out[p.name] = {"error": str(e)}
    return out


def schedule_all(scheduler):
    from . import database as db
    for p in all():
        try:
            p.schedule(scheduler)
        except Exception as e:
            db.log("error", "scheduler", f"provider {p.name}: could not schedule: {e}")


def startup_all():
    from . import database as db
    for name, err in _errors.items():
        db.log("warn", "system", f"provider {name} is installed but could not load: {err}")
    for p in all():
        try:
            p.on_startup()
        except Exception as e:
            db.log("error", "system", f"provider {p.name} startup failed: {e}")
