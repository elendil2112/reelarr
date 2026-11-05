# Third-party notices

Reelarr is licensed under the GNU GPL v3.0 (see [`LICENSE`](LICENSE)). It
includes data from, and is built on, the projects below. Their licences are
all compatible with GPL-3.0.

## Data bundled in Reelarr

| File | Source | Licence |
|---|---|---|
| `app/data/places.tsv.gz` | [GeoNames](https://www.geonames.org) `cities1000` (cities of 1,000+ people), rebuilt with `tools/build_places.py` | © GeoNames, [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| `app/data/countries.tsv` | ISO 3166 country names via [pycountry](https://github.com/pycountry/pycountry) / Debian iso-codes, with everyday spellings and aliases added by Reelarr | LGPL-2.1-or-later |

The Reelarr name, reel mark and app icon are original to this project.
The interface uses your system's own fonts; no fonts, icons or images from
other projects are bundled.

## Python packages

Installed from PyPI by `requirements.txt` (and their dependencies):

| Package | Licence |
|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | MIT |
| [Starlette](https://github.com/Kludex/starlette) | BSD-3-Clause |
| [Pydantic](https://github.com/pydantic/pydantic) / pydantic-core | MIT |
| [Uvicorn](https://uvicorn.dev) (with httptools, uvloop, watchfiles, websockets, python-dotenv, PyYAML, h11, click) | BSD-3-Clause; MIT; Apache-2.0 / MIT |
| [APScheduler](https://github.com/agronholm/apscheduler) 3.x (with tzlocal) | MIT |
| [Mutagen](https://github.com/quodlibet/mutagen) | GPL-2.0-or-later |
| anyio, sniffio, idna, annotated-types, annotated-doc, typing-extensions, typing-inspection | MIT; Apache-2.0 / MIT; BSD-3-Clause; PSF-2.0 |

## Programs in the Docker image

The image is based on the official `python` slim image (Debian) and adds
these Debian packages, which Reelarr runs as separate programs:

| Program | Used for | Licence |
|---|---|---|
| [FFmpeg](https://ffmpeg.org) | reading and converting audio | LGPL-2.1-or-later / GPL-2.0-or-later (Debian build) |
| [7-Zip](https://www.7-zip.org) (Debian `p7zip-full`) | unpacking .7z and other archives | LGPL-2.1-or-later, with the unRAR licence restriction for RAR code |
| ca-certificates, tzdata | secure connections, time zones | MPL-2.0; public domain |

## Services Reelarr can talk to

Reelarr isn't affiliated with or endorsed by these services. Each has its own
terms, which apply when you use it through Reelarr:
[setlist.fm API](https://api.setlist.fm/docs/1.0/index.html) (needs your own
free key) · [Internet Archive](https://archive.org/about/terms.php) ·
[MusicBrainz](https://musicbrainz.org/doc/About/Data_License).

To regenerate the package list after a dependency change:

```sh
pip install pip-licenses
pip-licenses --from=mixed --format=markdown
```
