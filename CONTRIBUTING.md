# Contributing

Thanks for helping. Reelarr is a small project for a community that cares
about getting things right, so the bar is: **never lose or damage anyone's
recordings**.

## Running it

```sh
pip install -r requirements-dev.txt
python -m app                 # http://127.0.0.1:8189
python -m pytest tests -q     # needs ffmpeg on PATH
ruff check .                  # lint
python -m mypy                # type check
```

## Ground rules

- **Every file change goes through `app/fileops.py`.** That's what gives
  Reelarr dry run, trash instead of delete, and undo. Code that calls
  `shutil.move`, `os.remove` or `rmtree` on someone's music won't be merged.
- **Respect dry run.** In dry run, plan; don't act.
- **Settings belong in the UI.** A new option needs a default in
  `app/config.py` and a line in the Settings page, not just a key in
  settings.json.
- **Database changes are migrations.** Add a step to `app/migrations.py`;
  never edit an old one.
- **Be polite to the sites Reelarr talks to.** Rate-limit, identify yourself
  honestly, and follow each site's rules on automated access.
- **Tests with the change.** Bugs get a test that fails without the fix.

## Finding your way around

- `app/pipeline.py` — a show from the watch folder to the library.
  `identify()` is an outline of named steps (read local → look up online →
  merge → corrections → titles → location → artist → source type → last
  look); start there.
- `app/metadata.py` — reading folder names, info files and tags.
- `app/align.py` — pairing setlist titles with files.
- `app/fileops.py` — every file change, with dry run and undo.
- **"Barbosa"** in the code is the private tool Reelarr grew out of. Those
  parts only matter to someone migrating an old Barbosa install, and are
  marked as legacy.

## Pull requests

Small and focused is easiest to review. Describe what changes and why, and
update `CHANGELOG.md` if users will notice. CI runs lint, the tests and an
image build on every pull request.

## Add-ons

Integrations with particular stores or services usually belong in an add-on
rather than the core — see [`docs/providers.md`](docs/providers.md).
