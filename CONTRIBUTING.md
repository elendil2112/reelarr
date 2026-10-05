# Contributing

Thanks for helping. Reelarr is a small project for a community that cares
about getting things right, so the bar is: **never lose or damage anyone's
recordings**.

## Running it

```sh
pip install -r requirements.txt pytest httpx ruff
python -m app                 # http://127.0.0.1:8189
python -m pytest tests -q     # needs ffmpeg on PATH
ruff check .
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

## Pull requests

Small and focused is easiest to review. Describe what changes and why, and
update `CHANGELOG.md` if users will notice. CI runs lint, the tests and an
image build on every pull request.

## Add-ons

Integrations with particular stores or services usually belong in an add-on
rather than the core — see [`docs/providers.md`](docs/providers.md).
