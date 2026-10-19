# Changelog

All notable changes to Reelarr. Versions follow [Semantic Versioning](https://semver.org/);
until 1.0, minor versions may change behaviour, and every upgrade backs up your
databases first.

## Unreleased

### Review: type track titles yourself
- Every title in **Review → Track titles** can be edited. Type over one and
  press Enter to move to the next; clear it to go back to the setlist's
  title. Your titles are kept with the show and written to the tags when you
  File it.
- Once every file has a title, the show can be filed even when the file count
  and the setlist never lined up — you've looked at all of it.
- Titles you type are learned as songs for that artist, like an approved setlist.

### Track titles: more songs than files
- When the setlist has more songs than there are files, Reelarr no longer
  just holds the show. It files it when every file is accounted for:
  - a file that names its songs ("Scarlet Begonias > Fire on the Mountain")
    is matched to them in the setlist, in order, and titled with all of them;
  - songs the recording simply doesn't have are left out (noted in Activity);
  - segues marked in an info file ("Scarlet Begonias >") or by setlist.fm are
    grouped onto one track when that gives exactly one group per file.
  Anything still unexplained waits in Review as before. Shows with as many
  files as songs, or more, are handled exactly as before.

### Matching — fixes from a 1,311-show audit report
**The learning tool (Maintenance → Learn from your corrections)**
- **Fixed: shows attached to another artist's show of the same date.** When a
  filed show had been moved or deleted, the tool took any folder with that date
  as the show — so toad1992-03-08 became the Grateful Dead's 1992-03-08 — and
  then learned "Toad the Wet Sprocket → Grateful Dead" as a rule for every
  future show. It now only follows a show to a folder that is plainly the same
  show (same artist folder, or a near-identical name).
- Artist rules are only learned for respellings of the same act
  ("Motorhead" → "Motörhead"), never between unrelated names. Venue rules are
  only learned when their city and state are a real place, and never overrule
  the city/state from your own album tags, a nugs sidecar or setlist.fm (the
  "The Fillmore → Benmont Tench – piano…, backing vocals" rule).
- **On upgrade**, learned rules that fail these checks are removed (after a
  backup of library_index.db) and each one is listed in Activity; records the
  old tool attached to another artist's show are pointed out in Activity too.

**Artist**
- Info-file lines that aren't band names are no longer taken as the artist:
  labels ("faults: none"), sentences ("Banned tracks removed."), places
  ("Santa Monica, California."), dated or filename lines, sharing boilerplate,
  and songs from the show's own setlist. A trailing date is trimmed
  ("TOAD THE WET SPROCKET 3/8/92").
- When the folder name and the info file name different, unrelated artists,
  the one in your library wins; if that can't decide it, the show waits in Review.
- HTML info files (archive.org descriptions) and UTF-16 info files are read properly.
- setlist.fm's search by date and city only ever fills in a missing artist;
  with no venue to match it needs two matching songs.
- If the folder's own prefix (toad, mmj) belongs to a different artist in your
  library than the one identified, the show waits in Review.

**Date**
- A full date in the folder name beats one from an info file or loose tags,
  unless setlist.fm confirms the other. Transfer/upload/"generated on" dates
  and future dates in info files are ignored.

**Venue, city, state**
- Venues lose leading dates ("75-06-06", "06/14/08 (Sat)"), SHNIDs, format
  words and a repeated city ("House Of Blues - Chicago"); a venue that's only
  "SBD" or "FM" is dropped. ALL-CAPS venues and cities are title-cased,
  keeping call signs and acronyms (WXRT, BBC).
- New layouts: "City, ST @ Venue - Festival - Stage", "City, ST -- Venue",
  "Recorded live at Venue, City (ST), …".
- Folder names: a city after the date ("Isaak1991-09-22.Toronto"), dotted
  venue and city ("De Doelen.Rotterdam,NL"), SHNIDs before the place, CamelCase
  and dotted artists ("EmmylouHarris", "Frank.Zappa"), dotted junk after the state.
- Pre-tagged album strings are only trusted when their place is real and they
  agree with the folder name's date and SHNID.
- "Boston, Boston" → MA (was United Kingdom), "Chicago, Chicago" → IL,
  "MI rm" → MI, "BE.VB" → Belgium, "Basel, BS" → Switzerland,
  "New York, NY" keeps its city.
- A venue, city or state that still can't be right is dropped, so the show
  waits in Review rather than being filed with it.

**Source and SHNID**
- The SHNID in the folder name beats one an info file cites; "matrix"/"mtx"
  in the folder name beats "SBD" in the info file.

**Titles**
- "Track 01"-style titles in pre-tagged files no longer beat a real setlist.
- An album title from untrusted tags is never used as the venue; a show whose
  only clue is an album title ("Orgasmatron (Expanded Edition)") waits in
  Review as a likely official album.

### Track titles
- **Fixed: titles one track off when a show starts with a tuning track.**
  setlists don't list tuning, crowd or break tracks, so a show with one more
  file than songs had every title shifted. Reelarr now sets those files aside
  before pairing titles with files: a file whose own title or name says so
  ("Tuning", "Crowd", "Set Break"…), or a short file (under 90 s) at the start,
  the end, or between discs. They're titled by where they sit — Tuning, Set
  Break, Encore Break, Crowd — or keep the label they already had.
- When it can't tell which file isn't a song, the show waits in Review
  instead of being filed with shifted titles. This now applies to setlist.fm
  setlists too, which used to skip the check.
- **Review → Track titles** lists every file with its length and the title
  it would get, with a *not a song* tick box; File it writes exactly what's shown.
- archive.org's per-file titles (which include tuning tracks) are used when
  they cover every file and the setlist doesn't.
- Fixed: when counts didn't match, track numbers were read from any digits in
  a file name — the `1` of `d1t05` gave the last file the first song's title.
- Tuning, Crowd and the like are no longer learned as songs.

### Notes
- A new **Notes** page for your own notes — tapes to chase, trades in
  progress, which source you liked. Saves as you type, when you switch notes
  and when you leave the page. Pin notes to the top, search them, and undo a
  delete. If the same note is edited in two windows, neither silently
  overwrites the other: you choose which version to keep.
- Notes live in `reelarr.db`, so they're in your backups. Deleted notes are
  kept for 30 days, then cleared.

### Queue
- Every Queue table — Wanted, Downloading, Coming in, Recently grabbed, Passed
  over — now sorts by any column: click a heading, click again to reverse.
  Your choice is remembered per table.
- A search box filters all five tables at once. Every word has to match
  somewhere in the row (artist, show, date, source, status, reason…); Esc clears it.
- Recently grabbed shows each show's status, progress and ETA from the
  download client, and keeps up to 100 grabs instead of 15.

## 0.1.0 — first public release

### Identify, tag and file
- Reads everything a live recording comes with — etree-style folder names, info
  files, embedded tags, SHNIDs — and checks it against setlist.fm and archive.org,
  keeping the provenance of every value.
- Works out the source: SBD, AUD, MTX, FM, front-of-board; optional crowd-noise
  analysis when nothing says.
- Names and tags every show the same way: `Artist/YYYY-MM-DD Venue, City, ST [SOURCE]`.
- **Places:** a built-in world city list (GeoNames). US states and Canadian
  provinces are two-letter codes; everywhere else the country is written in full
  in place of the state (`Paradiso, Amsterdam, Netherlands`). Codes are read
  against the city — `Berlin, DE` is Germany, `Dover, DE` is Delaware — and a
  missing state is filled when the city is unambiguous. Country spellings are
  yours to change.
- **Early and late shows** of one night are filed as one show: both folders kept
  inside it, the early show as disc 1 and the late as disc 2, and one album whose
  bracket carries both sources (`[SBD 082964 TinyDancer & AUD Miller]`).
- Converts SHN and WAV to FLAC without losing bit depth or sample rate.
- Anything uncertain waits in **Review**; corrections become rules for future shows,
  and your existing library teaches it your artists, venues and song titles.
- Official releases (the word "Official", plus any labels you name in Settings)
  are tagged as such and never replaced by fan recordings.

### An *arr for live music
- **Artists:** follow an artist and Reelarr watches the Live Music Archive for new
  uploads you don't have, in the sources and formats you'd accept — asking you
  first or grabbing automatically. Look back 30 days, a year, or everything;
  every check explains itself; stream-only items are recognised and skipped.
- **Discover:** search the Live Music Archive and grab shows.
- **Queue:** wanted releases, what was passed over and why, and a sortable
  download table (client order, progress, speed, ETA, seeds, ratio…).
- **Download clients:** Deluge, qBittorrent and Transmission, with a path-mapping
  helper; finished downloads come back through the watch folder automatically.

### Safe by design
- Starts in **dry run**: every change is planned and shown before any is made.
- **Nothing is deleted outright** — removed files go to a trash folder for 30 days —
  and every change can be undone from Activity.
- Database migrations with automatic backups; refuses to start on a database
  from a newer version.

### Security
- Login by default (scrypt, HttpOnly SameSite cookies, throttling), API key for
  other apps, secrets masked in the UI and redacted from logs.
- Strict Content-Security-Policy and related headers; the folder browser can't
  see the config folder; hardened `.torrent` parsing; archive extraction with
  size and zip-bomb limits, and programs and links removed from downloads.
- The container drops root and runs as `PUID`/`PGID`.

### Install
- Docker with no `.env` required, multi-arch image (amd64/arm64), an unRAID
  template, or bare metal with Python 3.10+.
- A setup wizard for the login, folders, download client, setlist.fm and start-up
  mode — every setting is in the UI.
- Add-ons ("providers") can bring shows in from other sources; see `docs/providers.md`.
