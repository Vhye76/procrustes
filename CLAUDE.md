# procrustes - operating ruleset for this repository

## 1.  What this file is

THIS FILE IS THE SOURCE OF TRUTH FOR THIS REPOSITORY:  what the container must do, what it must never do, and the constraint each rule rests on.

The full media library standards, covering the workstation scripts, the NAS layout, library-wide audits and the music library, live in a separate CLAUDE.md alongside those tools.

## 2.  Hard boundaries

THE CONTAINER NEVER WRITES TO A MEDIA LIBRARY.  Libraries are mounted read only at the container boundary, a stronger guarantee than any check in Python.  The guards in 'app/paths.py' stay anyway as defence in depth.

PROMOTION INTO THE LIBRARIES IS MANUAL, ALWAYS.  The pipeline ends at 'complete/'.  No code in this repository moves a finished title into a library, and none should be added.

NOTHING THAT MATTERS IS EVER DELETED.  A failure holds;  a rejected file and a completed source go to quarantine.  Nothing in a library, nothing incoming and nothing in 'complete/' is ever removed.  Two exceptions.  The encode area:  intermediates are removed once their successor exists, and a job directory is wiped after its title retires.  And an empty folder under 'import/':  when a source moves out, 'paths.prune_empty_folders' removes its parent folders upward while each is empty, stopping at 'import/' or at the first folder with anything left in it.  The climb passes the write guard at every step and removes directories only, never a file.

A PROVIDER ID IS NEVER GUESSED.  If it cannot be resolved, the title holds until an operator forces it.  A forced unidentified title carries no provider ID at all:  it keeps the name it arrived with, its tag block holds TITLE only, and it lands flat in 'complete/' rather than in a provider-named folder, so it cannot be mistaken for finished work.

## 3.  Repository layout

README.md carries the module tree.  Two things it does not say:  'app/static/' is vanilla JS with no framework, and 'app/data/' holds FileBot's release-group and media-source lists, CC0, vendored with SOURCES.

'app/' is a single Python package with two third-party dependencies and no more.  The standard library is sufficient for everything else and the HTTP client is hand-rolled on urllib.  KEEP IT THAT WAY.  No requests, no npm, no framework, no CDN.

THE TWO EXCEPTIONS ARE NAMED, AND EACH DOES ONE THING THE STANDARD LIBRARY CANNOT.  'argon2-cffi' is argon2id password hashing;  'hashlib' has no Argon2.  'qrcode' renders the authenticator enrolment as an SVG.  Both are Alpine community packages, 'py3-argon2-cffi' and 'py3-qrcode', never fetched from an index at build or run time;  the build gate in section 22 asserts both import, and the CI validate job installs the same two from PyPI because 'import app.main' runs there on a bare runner.  'app/auth.py' is the only module that imports either.

One process holds everything:  the assessment workers, one pool of threads per encoder plus one for passthrough, the import watcher, the library auditor and the web UI.

## 4.  Mount contract

ONE REQUIRED MOUNT.  Host paths are a deployment detail and appear only in the reference compose file.  README.md carries the mount table.

FOUR DIRECTORIES ARE DERIVED FROM THE ROOT AND ARE NOT CONFIGURABLE:

```
<root>/import
<root>/complete
<root>/complete/.quarantine
<root>/hold
```

THAT IS LOAD BEARING.  A move between two paths under one mount is a rename:  instant, no data copied.  Split them onto separate mounts and every move becomes a copy at best and fails outright at worst, per section 21.

ONLY ENCODE AND CONFIG ARE OVERRIDABLE, because they benefit from faster storage and neither is a rename target.  Left unset they are subdirectories of the root.

THE PIPELINE IS IMPORT, ENCODE, COMPLETE.  'hold/' and 'config/' are not stages.

```
import/       drop zone, the only watched entry point
encode/       the entire per-title work area, one directory per running job
complete/     TERMINAL, collected by hand
complete/.quarantine/   retired sources and rejected incoming files
hold/         needs a decision
config/       state.db, the instance lock, provider cache, cached posters, logs
```

QUARANTINE LIVES UNDER 'complete/', a dotted directory beside finished work.  Nothing in the container ever scans 'complete/', so a dotted sibling costs nothing.

A MISSING ROOT IS A STARTUP ERROR.  'MEDIA_ENCODE' and 'MEDIA_CONFIG' are validated only when set explicitly;  unset, 'Layout.ensure' creates them under the root.  'config/' has to be persistent wherever it lands:  it holds the SQLite store and the flock that section 19's single-instance guarantee depends on.

THE ENTRYPOINT TAKES OWNERSHIP OF THE WRITABLE MOUNT POINTS BEFORE DROPPING PRIVILEGES, because Docker creates a missing '-v' host directory as root:root.  It compares the owner of 'MEDIA_ROOT', 'MEDIA_ENCODE' and 'MEDIA_CONFIG' against PUID:PGID and chowns the mount point itself, never recursively:  a recursive pass would walk the whole share, and the libraries and '/certs' are read-only mounts on which a chown fails.

DEGRADE, DO NOT FAIL, APPLIES TO THE LIBRARIES ONLY.  Without a library mount the incumbent comparison is skipped and every title is treated as new, logged at startup and shown in the UI.

THE WHOLE PER-TITLE WORK AREA LIVES ON THE ENCODE MOUNT, NOT JUST THE ENCODE ITSELF.  An x265 encode is CPU bound;  the expensive I/O is on either side, each pass rewriting the whole file:

```
mp4 to mkv or avi to mkv remux     full read + full write
language strip                     full read + full write
encode                             full read + full write
packet count verification          full read of the source and of the output
statistics and tag writes          header rewrites plus a full re-read on re-add
```

So copy in once, do everything on the encode mount, move out once.  Startup compares 'os.stat().st_dev' of the encode mount against the complete mount and warns when they match.

STAGING HAPPENS BEHIND THE ENCODER SLOT.  A title is staged by the pool thread that will encode it, so at most 'cpu_slots + gpu_slots + 1' job directories exist at once whatever 'max_jobs' says.

Admission control requires roughly 'encode_headroom' times the source size free before a job starts.  There is no guard for memory:  sizing CONTAINER_MEM is the operator's call and an OOM kill mid encode is an accepted failure mode.

## 5.  Configuration

TWO SURFACES, BY WHAT THEY DESCRIBE.  The environment is the deployment surface, read once at startup, validated, and echoed into the log and onto '/api/status'.  Everything the pipeline decides with is a setting:  stored in the 'settings' table in 'state.db', edited on the Application Settings page, read at the point of use, and echoed into the log at startup with a mark on every stored value.

```
MEDIA_ROOT           /media            required rw, the one mount everything derives from
MEDIA_ENCODE         <root>/encode     optional, per-title work area on faster storage
MEDIA_CONFIG         <root>/config     optional, state.db, lock, cache, logs
LIBRARY_MOVIES       unset             ro movie library
LIBRARY_TV           unset             ro tv library
CERT_DIR             /certs            ro, holds the TLS certificate and key
TLS_CERT_FILE        fullchain.pem     certificate name within CERT_DIR
TLS_KEY_FILE         privkey.pem       private key name within CERT_DIR
PUID / PGID          required          identity the supervisor drops to
RENDER_GID           unset             supplementary group for /dev/dri, GPU off if unset
RENDER_NODE          /dev/dri/renderD128  render node the GPU probe and QSV encoder use
WEB_PORT             443               HTTPS only, there is no HTTP listener
DRY_RUN              0                 1 logs every intended action and performs none
LOCK_WAIT_TIMEOUT    0                 seconds to wait for the instance lock, 0 waits forever
LOCK_WAIT_INTERVAL   15
LOG_LEVEL            info              info or debug, see section 20
AUDIT_INTERVAL       2                 seconds between library files audited, 0 disables the sweep
AUDIT_SWEEP_INTERVAL 3600              seconds between passes over the libraries
```

Adding a variable means adding it to 'Config.as_dict()' and to this table;  NOTHING ENFORCES THAT, and one missing from 'as_dict()' silently vanishes from the banner and the status endpoint.

ELEVEN VARIABLES ARE NOT READ FROM THE ENVIRONMENT AT ALL.  'config.REMOVED' names them:  OUTPUT_CODEC, CRF, TV_ENCODE_SD, MAX_JOBS, GPU_SLOTS, CPU_SLOTS, ENCODE_HEADROOM, ENCODE_THREADS, POLL_INTERVAL, MTIME_QUIET, GRAIN_THRESHOLD.  One present is listed in 'Config.ignored' and 'main' logs a warning naming it and the settings page.  Nothing seeds a setting from the environment.

### Settings

'app/settings.py' IS THE ONE REGISTRY.  'SETTINGS' declares every setting once:  key, group, label, one sentence of help, type (int, float, bool, str, choice, list, table), default, whether it is per kind, and its bounds, choices or pattern.  The default of a setting that replaced a module constant is that constant.  README.md carries the table.

A GROUP NAMES THE PAGE THAT RENDERS IT.  Each 'GROUPS' entry carries a page, 'settings' or 'account', and 'describe()' emits it.  The Access group ('auth_enabled', 'session_hours') renders on User Settings only.  'auth_enabled' has one write route, 'POST /api/account/auth';  'Settings.update' and 'reset' refuse it from '/api/settings' with 'set from the User Settings page', and 'Auth' writes it with 'internal=True'.

A PER-KIND SETTING HAS ONE VALUE FOR MOVIES AND ONE FOR TELEVISION, stored as 'key.movie' and 'key.tv'.  Everything under Encoding is per kind except 'sd_display_height', 'passthrough_codecs' and 'x265_dv_vbv_kbps', and so are the grain threshold and the standards floors.  'Settings.profile(kind)' resolves one kind into a flat dict, adds the derived 'threads_per_job', and is what the orchestrator hands to 'encode', 'standards', 'compare', 'media', 'probe' and 'tags', which take the figures as parameters and never read the settings object.  'provider.Provider' and 'provider.Client' hold the settings object and read per call.

A CHANGE APPLIES AT THE NEXT READ, AND A ROUTED TITLE KEEPS ITS PARAMETERS.  'Settings.update' validates every value in the batch, runs the cross-key rules ('poll_interval' below 'mtime_quiet', the two slot counts not both zero) against the merged result, and writes the whole batch in one transaction or nothing.  '_route' snapshots 'encode.encoder_params(profile)' into the decision as 'params', stored in 'decision_json', and '_encode' builds the command from that snapshot;  a decision without 'params' goes back to assessment.  The sidecar 'encode.job' still wins per title.

THE POOLS RESIZE LIVE.  'orchestrator.Pools' carries a target per pool;  a worker runs while its index is below its pool's target and checks that between titles, so a shrink never interrupts a running encode.  '_ensure_workers' starts a thread for every index below the target with no live thread, and 'apply_settings' calls it when 'max_jobs', 'gpu_slots' or 'cpu_slots' changed.  'Pools.snapshot' carries the targets beside the live counts.

THE ENDPOINTS.  'GET /api/settings' returns 'Settings.describe()' with the pool snapshot;  'POST /api/settings' takes '{"set": {key: value}}' and '{"reset": [keys]}', returns the same body with 'changed' on success, and 400 with '{"errors": {key: why}}' having written nothing.  The page is 'app/static/settings.html' at '/settings'.

### Read outside Config

Read at import time in the module named, NOT validated, absent from the banner and '/api/status';  seams for substituting a binary.

```
FFPROBE      ffprobe       probe.py
FFMPEG       ffmpeg        media.py
MKVMERGE     mkvmerge      probe.py, media.py
MKVPROPEDIT  mkvpropedit   media.py, tags.py
MKVEXTRACT   mkvextract    tags.py
VAINFO       vainfo        gpu.py
```

'RENDER_NODE' has a module-level fallback at 'gpu.py' and 'encode.py' as well;  the Config value wins whenever a cfg is passed, and a change has to be made in both places.

## 6.  The chain

```
DETECTED     size stable across two polls, mtime quiet
PROBED       one ffprobe pass, classify movie or tv
SCREENED     minimum standards           fail -> collected, see below
IDENTIFIED   provider ID resolution      fail -> collected; transient -> retry with backoff
COMPARED     against library incumbent   loss -> QUARANTINE, ambiguous -> collected
ROUTED       encoder chosen, waiting for a slot on its pool
STAGED       copy into the encode job directory
REMUXED      container conversion if needed, then language strip, then flag repair
TAGGED       movie MOVIE block, or the three-level TV hierarchy
READY        the readiness gate           fail -> HELD, forceable
ENCODING     the encoder is running, frames done of total and ETA on /api/status
ENCODED      route per section 14, or pass through
VERIFIED     duration, packets, statistics, readiness, HDR   fail -> HELD, forceable
PUBLISHED    move to complete/, TERMINAL as far as a user is concerned
CLEANUP      source to quarantine, encode job directory wiped
```

ASSESSMENT AND ENCODING ARE TWO HALVES ON SEPARATE THREADS.  'max_jobs' assessment workers take a title from DETECTED through ROUTED;  the title is then queued for one of three pools by its decision, 'cpu' with 'cpu_slots' threads, 'gpu' with 'gpu_slots', or 'passthrough' with one, and the pool thread does everything from staging to cleanup.  Resume places a title by its stage:  up to COMPARED goes back to assessment, ROUTED and later go to the pool its stored decision names, and a later stage with no stored decision is re-assessed.

THE THREE ASSESSMENT STAGES RUN THROUGH BEFORE ANYTHING HOLDS.  SCREENED, IDENTIFIED and COMPARED are read-only, so a failure in one records its reasons and the next still runs, and the title holds once with everything the three found;  a clear comparison loss quarantines immediately.  STAGED, REMUXED and ENCODING cost hours and disk, so they are not run speculatively, and a title can hold a second time at READY or VERIFIED.

PUBLISHED IS THE END OF THE PIPELINE FOR A PERSON;  CLEANUP IS HOUSEKEEPING, and a CLEANUP failure must never present a published title as failed.  'state.COMPLETE' is the pair, and the dashboard reads it as one figure.

A TITLE IS IN THE PIPELINE UNTIL IT IS PROMOTED, AND THE WATCHER NEVER RECYCLES A ROW THAT IS.  'state.in_pipeline' is true for every stage but QUARANTINED, with three readers:  'webui.annotate_finding', 'orchestrator.import_finding' and the reappearance branch in 'orchestrator.scan'.  A path whose row is published and not yet promoted is skipped with one log line;  Forget reopens it.

A ROW LEAVES READY TO PROMOTE WHEN ITS OUTPUT LEAVES 'complete/', AND LEAVES QUARANTINED WHEN ITS FILE LEAVES '.quarantine'.  'orchestrator._close_collected' runs on every watcher poll:  a PUBLISHED or CLEANUP row whose 'output_path' is gone moves to QUARANTINED when its retired source is still there and is forgotten when no file remains;  a QUARANTINED row with no file at any of its three paths is forgotten.  The test is 'state.files_present', the same one 'webui.annotate' reports as 'files_gone'.  Nothing on disk is touched.  Skipped under DRY_RUN.

PROGRESS IS COUNTED IN ITS OWN UNIT, NEVER A PERCENTAGE FROM A PROXY.  Since ffmpeg 7.0 the progress 'out_time' is the lowest last-muxed timestamp across the output streams, so a copied forced subtitle track parks it at its last cue.  'orchestrator._progress_handler' reads 'frame=' against 'probe.total_frames', the video track's 'NUMBER_OF_FRAMES' or duration times frame rate;  the row carries 'frame', 'total_frames', 'fps' and 'eta_s', and the tile reads 'x / y frames'.  The repair copy reports 'copied_bytes' against 'total_bytes'.

THE TAG BLOCK IS WRITTEN TWICE, BEFORE AND AFTER THE ENCODE.  ffmpeg's Matroska demuxer renders a targeted tag into the global metadata dict as 'TARGETTYPE/NAME' and the muxer writes it back untargeted, so '-map_metadata 0' flattens a correct block into the defect section 12 describes.  The pre-encode write and its readiness gate stop a title before it costs encoder time;  the post-encode write makes the published file correct, and readiness runs again inside VERIFIED.  'tags.carry_forward' reads the pre-encode file and skips any name containing a slash.

TWO ORDERINGS ARE LOAD BEARING.  IDENTIFICATION AND THE COMPARISON HAPPEN BEFORE ANY ENCODE, so a title that cannot be identified or that clearly loses costs no encoder time.  THE LANGUAGE STRIP RUNS BEFORE THE ENCODE, because the encoder maps every audio track.

'/media/import' is the only watched entry point.  A file must be size-stable across two polls AND untouched for 'mtime_quiet' seconds;  files ending in '.part' and files beginning with a dot are ignored.  At the defaults of 30 and 15 a finished copy is detected 30 to 45 seconds after its last write;  a transfer paused longer produces a truncated file that fails ffprobe or the standards gate and holds.

'poll_interval' IS BELOW 'mtime_quiet', ENFORCED.  The size condition compares against a size recorded by a PREVIOUS poll, so the quiet window has to contain at least one poll.  'Settings.update' refuses a batch that would put the poll at or above the quiet window.

## 7.  Minimum standards

The first gate.  It decides whether a file is worth any identification or encoding effort at all.

```
BOTH KINDS
  ffprobe readable, at least one video stream
  at least one audio track tagged eng or und
  not a 25 fps PAL speed-up of film material
  baked-in letterbox no worse than 20 px
  not a sample or extras file

MOVIES
  display resolution at least 1920x800, computed width * SAR / height
  runtime at least 40 min

TELEVISION
  SD accepted, since no HD master exists for much of the library
  runtime at least 15 min
```

THE FLOORS ARE PER-KIND SETTINGS, AND THE FIGURES ABOVE ARE THEIR DEFAULTS.  'min_display_width', 'min_display_height' and 'min_runtime_min' on the Standards group, 0 meaning no floor;  'letterbox_bars_px', 'pal_speedup_check' and 'keep_langs' are global.  'standards.screen' takes them on a profile and its module constants are the defaults.

THE HEIGHT FLOOR IS 800, NOT 1080, AND THE WIDTH FLOOR IS WHAT REJECTS SD.  A 2.40:1 scope master is 1920x800 and a 2.35:1 is 1920x818, so a 1080 floor rewards the file with bars.  The width floor of 1920 rejects 720p, NTSC and PAL DVD.

The 40 minute movie floor exists because a bonus featurette can qualify as a disc's main feature;  section 26 records the ARM setting that is the first line of defence.

THE EXTRAS CHECK IS A VOCABULARY IN TWO STRENGTHS, KEYED ON POSITION.  'standards.EXTRAS_WORDS' (sample, trailer, featurette, deleted scenes, behind the scenes, making of, gag reel, bloopers, outtakes) flag anywhere in the file name as whole tokens.  'EXTRAS_SEGMENT_WORDS' (proof, bonus, extra, extras, interview, short) are ordinary words, so they flag only as a trailing segment after the year or a hyphen, as the whole stem, or as the parent folder's name;  an episode's title portion after the marker is exempt.  The vocabulary is guessit's.

A HOLD MOVES THE FILE.  'orchestrator._hold' is the one route into HELD, for gate failures and transient retries alike.  It moves the source under 'hold/' at its path relative to 'import/', so the folders the identity ladder reads travel with it, reserves the name through the same exclusive-create loop as quarantine, prunes the vacated 'import/' folders, and rewrites 'source_path'.  Retry and Force re-run the title from 'hold/';  the watcher never scans it, and a title that publishes is retired from there to quarantine.  Under DRY_RUN nothing moves.

THE OVERRIDE CLEARS EVERY GATE IT CAN REACH, AND A HELD TITLE STATES EVERY REASON IT WAS HELD.  Every gate evaluates in full and collects its verdicts, and 'overridden' is read at seven gates:

```
_screen     standards bypassed
_compare    comparison bypassed, including a clear LOSS
_identify   proceeds with no provider ID, section 2
_ready      proceeds, the readiness problems recorded in the stage history
_verify     proceeds, the verification problems recorded in the stage history
_publish    a destination collision publishes beside it under a unique name, never over it
_remux      the mp4-path duration drift is a recorded note, as the avi path is
```

FOUR STOP POINTS STAY OUT OF REACH, AND THAT IS PHYSICAL RATHER THAN POLICY.  An unreadable probe, a non-zero ffmpeg or mkvmerge in the remux, a non-zero encoder, and an I/O failure at publish each mean no output file exists.  Those land in FAILED, which force cannot apply to:  the decision endpoint refuses 'override' on a FAILED title and the UI offers Retry instead of Force.  Every HELD title is forceable.

A held title's reasons are stored as a list on the row and rendered as a list in the detail dialog, with the one-line 'reason' as the joined summary.  Every gate a forced title bypassed is written into its stage history.  A clear LOSS and a failed verification are forceable alike;  nothing writes to a library and promotion stays manual.

The letterbox check is cheap-first:  only a 16:9 or 4:3 display aspect can hide baked in bars, so cropdetect runs on those candidates only.

## 8.  New versus incumbent comparison

Runs after identification and before any encode.  EVERY GATE IS EVALUATED AND THE VOTES ARE TALLIED.  The first difference does not decide.

```
1  HDR or Dolby Vision present    losing it is never an upgrade, asymmetric, see below
2  display pixel count            w * SAR / h, never stored dimensions
3  baked-in letterbox             larger real picture area wins, gate 2 defers to it
4  audio maximum channel count    5.1 beats 2.0
5  bit depth                      10-bit beats 8-bit
6  video bitrate                  weighted for codec efficiency, comparative only
7  source pedigree                remux > encode > web, tiebreak only
```

Every vote a win proceeds.  Every vote a loss goes to quarantine with no encode spent.  VOTES IN BOTH DIRECTIONS GO TO HELD with a side-by-side attribute table in the UI, and so does a pair on which no gate voted.  'compare.compare' returns only after every gate has voted;  returning at the first difference settles a split verdict silently by whichever gate sits earliest.

A held contradictory verdict has two exits:  the section 7 override, or Discard, which quarantines the arrival.

GATE 1 IS ASYMMETRIC AND IT IS THE ONLY SHORT CIRCUIT.  An incoming file that LACKS HDR or Dolby Vision the incumbent carries is an immediate loss.  One that GAINS it casts an ordinary win vote.

Gate 7 is a tiebreak rather than a vote.  Source pedigree is inferred from release naming, so it is consulted ONLY when no measurable gate voted, and flagged as a weak signal when it decides.

Gates 2, 3 and 6 need both sides to be measurable.  When one side is missing the gate casts no vote and the skip is recorded in the notes.  A deferred gate 2 casts no vote either.

THE COMPARISON CARRIES EVERY ATTRIBUTE THE PIPELINE MEASURES, NOT ONLY THE SIX IT GATES ON.  'compare.MEASURED' is the list and it is what the UI renders;  a row that differs with no gate against it is marked as such.

AN INCONCLUSIVE HOLD NAMES THE INCUMBENT, AND SAYS WHEN ITS TITLE DIFFERS, since a library numbered one behind the catalogue puts a different episode at the matched number.  'orchestrator._ambiguous_detail' puts the incumbent's file name on the hold reason, the COMPARED record and the log line, and says first when its segment title differs from the incoming title under the section 9 transform.

Cropdetect runs at COMPARED on both sides, on letterbox candidates only.  GATE 2 DEFERS TO GATE 3 WHENEVER EITHER SIDE CARRIES BAKED-IN BARS, because display pixel count counts black bars as picture;  gate 2 records a defer note.  THE DEFER CONDITION HAS NO AMBIGUOUS MIDDLE:  one setting, 'letterbox_bars_px', is the floor cropdetect reports a crop at and the limit the standards gate fails above, so 'letterbox_px' is only ever 0 or that figure and above;  'media.CROP_MIN_BARS_PX' and 'standards.LETTERBOX_MAX_BARS_PX' are its default on each side.

### Video bitrate, gate 6

COMPARATIVE ONLY.  There is no minimum bitrate and none is to be added:  section 7 does not screen on it, and section 14's rule that size figures do not measure an encode is unchanged.

THE FIGURE IS THE VIDEO TRACK'S OWN BPS, ON BOTH SIDES.  'probe._video_bitrate' reads the 'BPS' or 'BPS-eng' stream tag first, falls back to NUMBER_OF_BYTES times 8 over the track's own DURATION, and finally to ffprobe's stream 'bit_rate', which Matroska frequently omits.

RAW BITRATE ACROSS CODECS IS NOT A QUALITY COMPARISON, so the figures are weighted first:

```
codec_efficiency, relative to h264 = 1.0
  h264, avc      1.0
  hevc, h265     1.7
  av1            2.2
  vc1            0.9
  mpeg4          0.7
  mpeg2video     0.45
```

STARTING POINTS, NOT SETTLED VALUES, and a setting on the Comparison group.  The av1 figure derives from section 25's 25 to 30 percent applied to the hevc figure.  Anything unlisted is 1.0.  Without the weighting the gate would quarantine this pipeline's own HEVC output against its h264 source.

'bitrate_tolerance' is 0.25 against 'pixel_tolerance' at 0.05, both settings, and it is also unmeasured;  legitimate encodes of one title vary far more in bitrate than in pixel count.

NO BITS-PER-PIXEL NORMALISATION.  A differing resolution is gates 2 and 3's business, and a resolution vote that contradicts a bitrate vote lands in review under the tally.

A PAL SPEED-UP IS DETECTED FROM THE PAIR, NOT FROM ONE FILE.  Equal frame counts within one frame at different frame rates means one side is speed-adjusted and the slower rate is correct, at any resolution;  the single-file check in 'standards' keys on stored height.

The container never touches the incumbent.

## 9.  Title authority and the filename transform

THE TAG IS THE PROVIDER'S TITLE, VERBATIM.  Character for character, including ':', '?', '"' and any other punctuation.  Nothing is ever stripped from a tag.

THE FILENAME IS THAT SAME TITLE MINUS ONLY UNSAFE CHARACTERS.  No re-wording, no abbreviation, no reordering, no truncation.  The transform is mechanical and strictly one way:  a filename can always be derived from a tag, a tag can never be derived from a filename.

THE UNSAFE SET is exactly these nine, plus control characters 0x00 to 0x1F:

```
/   \   :   *   ?   "   <   >   |
```

Two further rules apply to the filename component:  it must not end in a period or a space, and it must not be one of the reserved device names CON, PRN, AUX, NUL, COM1 to COM9, LPT1 to LPT9.

XFS and Btrfs forbid only '/' and NUL, while NTFS and SMB forbid all nine, and the libraries are served over SMB;  that is why '?' is removed while '!' is kept.

EVERYTHING ELSE IS SAFE AND IS RETAINED:

```
(  )  [  ]  {  }  ,  .  '  -  !  &  #  %  $  ~  +  =  @  ^
and all accented and non-ASCII letters
```

Parentheses and square brackets are structural:  '(Year)' appears in every movie name and '[tvdbid-N]', '[tmdbid-N]', '[imdbid-ttN]' carry provider IDs.

REMOVING A COLON.  A colon is REMOVED.  There is exactly one form and it is not a per-title choice.  'Avengers: Endgame' is filed as 'Avengers Endgame', for movies and television alike;  the ' - ' form is non-conforming.  An em dash or en dash becomes ' - '.  A forward slash inside a title becomes '-'.

DIRECTION OF THE TRANSFORM:

```
tag          Futurama: Bender's Game
folder       Futurama Bender's Game (2008) [tmdbid-13253] [imdbid-tt1054486]
file name    Futurama Bender's Game (2008).mkv
```

Three rules follow:

- Never derive a tag from a folder or file name.  The section 11 ladder obeys this by construction:  an id read from a folder leads to a provider entity and the entity supplies the title.
- Never assert that a tag equals a filename.  Apply the transform to the tag, then compare results.
- Where a tag and a filename differ by unsafe characters alone, that is expected and is not a defect.  The colon specifically must differ by REMOVAL, not by ' - '.

'app/titles.py' implements this and is removal-only by construction.  It has no permissive mode.

## 10.  Naming

### Movies

```
Title (Year) [tmdbid-N] [imdbid-ttN]/Title (Year).mkv
```

Editions, and ONLY editions, repeat the exact folder name plus ' - Label', in the form README shows, because Jellyfin does not support Plex's {edition-Name} syntax.  Do not generalise the long form to single-version films.

AN EDITION IS READ FROM THE ARRIVAL NAME, NEVER INFERRED FROM THE FILE.  'titles.EDITIONS' is the vocabulary, guessit's edition list plus the library's own 'Assembly Cut' and 'Final Cut':  Director's Cut, Director's Definitive Cut, Extended, Theatrical, Unrated, Uncut, Uncensored, Remastered, Restored, Criterion, IMAX, Collector, Limited, Deluxe, Ultimate, Special Edition, Alternative Cut, Fan Edit, Festival.  'titles.edition_from_name' looks only after the last year in the name, or at the end of the stem when the name carries no year, so 'The Extended Family (2010)' is a title and 'Alien 3 (1992) Assembly Cut' is an edition.  'provider.identify_movie' reads it from the file name, the parent folder and a repair copy's origin name, and the identity carries 'edition';  '_publish' builds the long form and the MOVIE tag block is unchanged.  Runtime is never consulted.

### Television

```
Show Name (Year) [tvdbid-N] [tmdbid-N]/Season NN/Show Name - SNNENN - Episode Title.mkv

Standard episode        Show - S03E07 - Recluse.mkv
Multi-part episode      Show - S03E02 - Quarantine, Part 1.mkv
Two episodes, one file  Show - S05E01-E02 - Kidnapping.mkv
```

Multi-part episodes use ', Part 1' rather than the provider's marker.  TVDB uses three marker forms and all three must be handled:  '(1)', '(Part 1)' and '(Part One)'.  The conversion applies to the Matroska EPISODE title and the segment Info title as well as the filename.

A file covering two episodes takes the range form and drops the part marker;  naming it as only the first makes Jellyfin report the second as missing.  Its EPISODE PART_NUMBER is the first episode of the range.

## 11.  Provider IDs and episode matching

Movies use tmdbid and imdbid.  Television uses tvdbid and tmdbid, because TVDB governs episode titles and numbering.

Resolution goes through Wikidata, then verification:  'wbsearchentities', then 'Special:EntityData/<QID>.json', reading P4947 for a film's TMDB id, P4983 for a series' TMDB id, P345 for IMDb, P4835 for TVDB and P577 for release date.  THE TWO TMDB PROPERTIES ARE NOT INTERCHANGEABLE AND NEITHER IS A TVDB ID;  'ids_from_entity' takes the kind.  Requests are spaced about 3 seconds apart.  CHECK THE HTTP STATUS:  a 429 body fails JSON parsing and looks identical to "not found".

THE PAGE IS CONFIRMED BY ITS OWN TITLE AND YEAR, AGAINST THE ENTITY'S LABEL AND ALIASES, because a Wikidata provider id can be flat wrong and TMDB spells titles its own way.  '_page_confirms' reads the page's '<title>', 'Name (YYYY)' on TMDB, 'Name (TV Series YYYY)' for a show, 'Name (YYYY)' or bare on TVDB, and scores it against the label and every alias under the section 9 scoring, accepting at 'title_cutoff', with the label-in-body test as the fallback.  A page year more than one off the entity's rejects regardless.

Searching a bare franchise name returns the franchise entity.  Search 'Title (YYYY film)'.

### The search is matched under section 9's own rules

'wbsearchentities' IS A PREFIX MATCH THAT STOPS AT PUNCTUATION, so a filename with the colon removed finds nothing for an entity whose labels all carry it.  'provider._resolve_by_search' runs the two prefix searches, then a FULL-TEXT FALLBACK through 'action=query&list=search', with the hits' labels and aliases fetched in one 'wbgetentities' call.  EVERY CANDIDATE IS SCORED THE WAY SECTION 9 SAYS TO COMPARE:  'titles.to_filename' on the label against the name, then 'normalise_for_match' on both sides, then containment as whole words, then difflib.  Full-text hits must clear 'title_cutoff', the one setting the episode matcher and the search share, 0.82 by default;  prefix hits are ordered by score but not cut.  A CANDIDATE WHOSE RELEASE YEAR IS MORE THAN A YEAR FROM THE NAME'S IS SKIPPED.

A TIE AT THE TOP SCORE IS A HOLD, NOT A PICK.  '_resolve_from' on a name search collects every top-scored candidate that verifies complete;  two or more come back as an identity carrying 'tied', each outcome reading 'tied at 1.00 with Q…', and the orchestrator holds with both named.  A year in the name settles it first;  the id rungs keep first-that-verifies.

A YEAR INSIDE A TITLE IS NOT THE RELEASE YEAR:  'Blade Runner 2049 (2017)'.  Take the LAST match, with a lookahead rather than a consumed delimiter, since in 'Blade.Runner.2049.2017.1080p' one dot serves both.

### The identity ladder

A FILENAME IS THE LAST RESORT, NOT THE FIRST.  'provider.movie_candidates' and 'provider.show_candidates' build the rungs and 'provider._identify_from' walks them;  the first rung that yields a verified identity wins.

```
MOVIES
1  embedded tag     TMDB, IMDB, TITLE and DATE_RELEASED from the MOVIE-targeted block
2  filename ids     [tmdbid-N] or [imdbid-ttN] parsed out of the file name
3  folder ids       the same, parsed out of the containing folder
3a origin folder    the same, parsed out of the library folder a section 27 repair copy came from
4  segment title    the Matroska segment Info title
5  filename         the cleaned file name, the original behaviour
6  parent folder    the containing folder name, skipped when it is the watched root

SHOWS
1  embedded tag     TVDB and TMDB from the COLLECTION-targeted block, TITLE as the name
2  folder ids       [tvdbid-N] or [tmdbid-N] parsed out of the parent, then the grandparent
2a origin folder    the same on the library folders a section 27 repair copy came from
3  filename         the cleaned show name, then its parent folder, the original behaviour
```

Rung 6 is skipped for a file directly in 'import/' or 'hold/'.  RUNG 3a EXISTS BECAUSE THE AUDIT COPY LANDS FLAT in 'import/';  the row stores 'origin_path'.

A RUNG SUPPLIES IDS AND A NAME;  THE PROVIDER SUPPLIES THE IDENTITY.  An id rung fetches the entities carrying that id statement, 'haswbstatement:P4947=11' through the full-text search;  a name rung searches by name.  Every candidate goes through '_accept_movie_hit' or '_accept_show_hit':  ids from the entity, the year check on a name search, the page confirmed, and the LABEL as the title.  An id rung's entity must carry that id;  two entities on one id are ordered by the section 9 score.  Nothing from the disk enters the identity except the ids that led to it, because disk text as the title is a filename wearing a tag, which the audit cannot catch;  an id Wikidata does not know falls to the next rung.  So every identification touches Wikidata, rung 1 included, and a tag TITLE that differs from the label is rewritten to the label.

AN INCOMPLETE IDENTITY HOLDS, IT DOES NOT PUBLISH.  A movie needs title, year, TMDB and IMDB;  a show needs show, year, TVDB and TMDB.  Neither acceptance rejects an entity for lacking an id, because a lower-scored entity carrying the id would then win;  the identity carries a 'missing' list and the orchestrator holds with the field named.  A complete hit displaces an incomplete one at the same score, never at a lower one.  'titles.movie_folder', 'movie_filename', 'show_folder' and 'episode_filename' raise 'TitleError' on a None.

A MISSING TVDB ID IS LOOKED UP THROUGH THE IMDb ID, AND CONFIRMED BY THE ROUND TRIP.  'https://thetvdb.com/api/GetSeriesByRemoteID.php?imdbid=<id>' answers with no key.  'provider.tvdb_from_imdb' takes that id only when the dereferenced series page carries the same IMDb id back and confirms the entity's names, recorded as 'tvdb_from'.  The endpoint is TVDB's legacy v1 API;  should it stop answering, the show holds incomplete for the operator selection below.

A TVDB PAGE IS NAMED BY ITS ENGLISH TRANSLATION, NOT ITS '<title>':  '_page_identity' reads the 'change_translation_text' element with 'data-language="eng"' first.  THE EPISODE CATALOGUE IS READ IN ENGLISH FOR A NON-ENGLISH-ORIGINAL SERIES:  the 'allseasons' listing serves original-language titles whatever is sent, so 'episodes_for_order' reads the series page's 'Original Language' field and, when it is not English, fetches each episode page for its English title.

A SHOW'S TMDB PAGE MAY DATE IT BY ITS ORIGINAL, AND THAT IS ACCEPTED IN ONE DIRECTION.  TMDB dates a dubbed adaptation by the original's air date, and an adaptation cannot air before its original, so '_page_confirms' accepts a show's own P4983 id when the page title scores 1.0 against the label and the page year is earlier by any amount, writing the discrepancy into the IDENTIFIED detail;  later by more than one rejects.  The folder year stays Wikidata's.  Residual risk, accepted:  a wrong P4983 pointing at a same-titled EARLIER show passes, and only the '[tmdbid-N]' component is then wrong.

A stale or hand-edited tag cannot inject a wrong ID:  the entity's page has to confirm it.  The rung that produced an identity is recorded in the stage detail.  'verify_tvdb' dereferences the id to its series page;  a 404 is "not confirmed" and a network failure is transient.  A TMDB id on a show entity that its page does not confirm is dropped rather than rejecting the entity.

### Operator selection, one selector per source

THE BACKSTOP FOR EVERY CASE THE RUNGS CANNOT SETTLE.  When identification holds, the detail dialog shows one candidate list per source, so the operator confirms every component of the identity.  For a show the sources are the Wikidata entity (title and year), the TVDB series (tvdb id and catalogue) and the TMDB series;  for a movie the entity, the TMDB movie and IMDb.

WHAT EACH SOURCE CAN ENUMERATE WITHOUT A KEY.  Wikidata:  every entity '_resolve_from' scored.  TVDB:  every P4835 plus the remote-id lookup for every P345, each dereferenced to its series page.  TMDB:  'https://www.themoviedb.org/search/tv?query=<name>' and '/search/movie?query=<name>', server-rendered, merged with every P4983 or P4947.  IMDb:  every P345.  Every source has a free-text id field.  Lists are capped at 'candidate_limit', eight by default.

THE LISTS ARE BUILT WHEN THE HOLD IS WRITTEN, through 'provider.hold_candidates', stored as 'candidates_json' with the rung and name searched;  a provider that cannot be reached leaves the lists incomplete, never fails the hold.  The pipeline's own choice is preselected;  rejected rows show why.  A checkbox applies the decision to every held title of the same kind whose search name matches.

THE DECISION IS 'action: "identify"' ON THE DECISION ENDPOINT, with 'qid', the id fields for the kind, an optional 'year', and 'apply_to' of 'title' or 'same-search'.  The choice is stored as 'pinned_json', the history records it, and the row requeues to DETECTED.  A top rung 'operator' fires on a pinned row:  the entity is fetched by qid, the ids come from the selections, each page is still fetched and its confirmation written into the IDENTIFIED detail, but an operator-selected id is not rejected by the page or year check.

TVDB'S 'allseasons' PAGE OMITS SEASON 0;  'episodes_for_order' reads '/seasons/official/0' as well when the order carries no specials.  A special TVDB does not list falls back to source numbering, with the warning.

### Finding the incumbent in the library

A MOVIE INCUMBENT IS THE SAME CUT, OR THERE IS NONE.  'orchestrator._movie_file' picks the file inside a matched folder by the identity's edition;  a folder holding only the other kind returns no incumbent with the reason in the COMPARED detail.

THE LOOKUP KEYS ON THE PROVIDER ID, NOT ON THE TITLE.  Section 10 puts '[tmdbid-N]', '[imdbid-ttN]' and '[tvdbid-N]' into every library folder name, so an ID match is exact and survives any drift between a stored folder name and the current transform.  The transformed-name prefix match is the fallback, and the route that matched is recorded in the stage detail.

A MISS AND A GENUINELY NEW TITLE MUST NOT LOG THE SAME SENTENCE.  The COMPARED detail separates four outcomes:  no library mounted, a folder count scanned with nothing matched, a match by provider ID, and a match by folder name.

### Scraped text carries HTML entities

DECODE THEM, AND DO IT AFTER STRIPPING TAGS.  'titles.normalise_for_match' expands '&' to ' and ' before stripping punctuation, so '&#039;' becomes ' and 039 ', and a title with one entity still matches and writes the corrupt string into the store, the tag, the segment title and the filename.  'html.unescape', applied AFTER the tag strip;  reversed, an escaped '&lt;i&gt;' becomes a real tag.  The one call site is 'episodes_for_order'.

### Cover art

THE POSTER COMES OFF THE PAGE 'verify_tmdb' ALREADY FETCHES:  the first 'og:image' meta tag on 'https://www.themoviedb.org/movie/<id>', the SECOND being the backdrop.  'provider.tmdb_poster' re-requests the same URL, a cache hit.  NO TMDB API KEY, DELIBERATELY.

TWO TRAPS IN SERVING IT, BOTH IN 'webui'.  'ProviderClient.fetch' CANNOT CARRY IMAGE BYTES, since it ends in 'response.read().decode("utf-8", "replace")';  posters use 'webui.fetch_poster_bytes'.  THE POSTER FETCH MUST NOT SHARE THE PROVIDER THROTTLE:  'fetch' calls '_wait' against one shared timestamp, and posters come from an image CDN.

TELEVISION FALLS BACK TO TVDB.  'provider.tvdb_poster' fetches 'https://thetvdb.com/series/<slug>' from 'series_slug' and takes the first URL under '/banners/posters/';  a wrong poster is cosmetic.  MEMOISE THE RESULT PER SHOW, INCLUDING THE FAILURES:  'Client.final_url' is not cached and calls '_wait', '_poster_url' runs once per title, and 'tvdb_poster' catches broadly, since 'final_url' lets urllib's HTTPError escape unwrapped.

Posters are cached under 'config/cache/posters' keyed by a hash of the URL and served from '/api/poster/<hash>' with a long cache header.  THE URL IS THE HASH, NOT THE TITLE ID:  'immutable' is only honest on a URL whose content can never change, and a row id is not that after a store wipe.  The row carries it as 'poster' beside 'poster_url'.

### Choosing the release year

P577 IS NOT A SINGLE VALUE.  A film carries several release claims in no meaningful order, and a year-only placeholder can precede them.  'provider._best_date' selects:  drop deprecated rank, prefer preferred rank, then the highest precision, then the earliest date.

Lookups are cached on disk under 'config/cache'.  EVERY HTTP 200 IS CACHED WITHOUT EXPIRY, AND AN EMPTY SEARCH RESULT IS A 200, so a title held for "could not be resolved" would hold again identically.  The operator's Retry therefore runs a cache-bypassing identification:  'Client.fresh' is a thread-local context the orchestrator enters around 'provider.identify' for a retried title, and the fresh answers overwrite the cache.  INTERNET ACCESS IS REQUIRED:  a provider that cannot be reached raises ProviderError, treated as transient, retried five times over roughly 62 minutes with exponential backoff, then held with the reason written.

### Episode order

AIRED ORDER, AUTOMATICALLY, FOR EVERY SHOW.  No per-show decision and no held gate.  Accepted risk:  a show whose disk content is genuinely in DVD order will be numbered as aired.  Where per-season counts disagree with aired order the mismatch is surfaced as a warning.

### Matching

MATCH EPISODES BY TITLE, NEVER BY THE NUMBERING IN SOURCE FILENAMES, since release groups renumber when they collapse a two-part episode into one file.  Match normalised titles against the provider list, then derive SNNENN from the match.

THE TITLE IS WHATEVER FOLLOWS THE EPISODE MARKER.  'episodes.title_from_filename' reads 'Show - SxxEyy - Title' first;  otherwise the text after the episode marker with leading separators stripped, and the bare stem only when no marker is found.

Fuzzy fallback at a difflib cutoff of 0.82, because release filenames carry typos.  Fall back to source numbering only when exact and fuzzy both fail, and log every fallback.  WHEN NUMBERING DECIDES, THE TITLE IS STILL THE CATALOGUE'S:  'provider.identify' takes the catalogue entry for the parsed number, using the filename text only when there is none;  'match_method' stays 'fallback-numbering' and the score 0.0.

THE RELEASE TAG IS STRIPPED BEFORE THE TITLE IS MATCHED.  'titles.strip_release_tag' removes a trailing '(…)' or '[…]' group whose contents carry a release token or a release group name, repeatedly;  'Storm Front (2)' is untouched.  'titles.strip_release_group' removes a '-GROUP' suffix.  Both run inside 'title_from_filename'.  'PART_MARKERS' recognise the bare forms 'Part 2', 'Part Two', ', Part 2' and '- Part 2' at the end, and '_index' maps '(base, part)' so a marked probe lands on its own part.  The segment title is a second probe:  'match_episode' takes 'extra' and 'provider.identify' passes it.

THE RUNGS, AS 'episodes._match_episode' RUNS THEM, every comparison on 'titles.normalise_for_match' of both sides.  The exact rungs run twice, on the probe and then on the probe with release tokens removed and its part marker read again, because a dotted release name carries the marker inside the junk.

```
1  exact        the whole probe equals a catalogue title                          method exact, 1.0
2  own part     the probe's base plus its part number equals a marked entry       method exact, 1.0
3  base         the whole probe equals an entry's base, first of a marked pair    method exact, 1.0
4  stripped     an unmarked probe minus separators equals a title or a base       method exact, 1.0
   (1 to 4 again on the token-stripped probe)
5  containment  an entry's base starts the probe on whole words, longest wins     method contains, share of probe words
6  difflib      closest catalogue title at or above 0.82, unmarked probes only    method fuzzy, the ratio
```

A PROBE CARRYING A PART NUMBER THE CATALOGUE LACKS NEVER LANDS ON THE FIRST PART:  it skips rung 3, containment's base fallback and difflib, and falls to numbering.  The file name is tried first, then the segment title;  a miss on both falls to source numbering with the catalogue title.

THE RELEASE VOCABULARY IS FILEBOT'S DATA, VENDORED.  'app/data/release-groups.txt' and 'app/data/media-sources.txt' are byte-for-byte copies from 'github.com/filebot/data', CC0-1.0, commit and date in 'app/data/SOURCES', never fetched at runtime.  'titles' loads them once at import;  the WEB-DL row's variable-width look-behind is replaced by 'titles.WEB_DL_FIXED', and a row that fails to compile is logged and skipped.  'RELEASE_TOKENS' is the hand list joined with the source patterns;  'RELEASE_GROUPS' is consulted only by position, a '-GROUP' suffix or a token inside a trailing bracket group, because the corpus holds ordinary words ('JOY', 'WAR', 'LIFE').  'extended', 'uncut' and 'remastered' belong to section 10's edition vocabulary instead.

EPISODE FORMS, FROM GUESSIT'S GRAMMAR.  Range separators '~', 'to' and 'and' join '-', '+' and '&'.  'MAX_RANGE_SPAN' is 3, so 'E01-E99' cannot claim a season.  'Part 3 of 6' and '3 of 6' parse as episode 3 when the parent folder is 'Season NN' ('episodes.season_from_folder', 'parse_path'), as does a leading bare number ('Season 02/07 - Title.mkv').

THE OPENSUBTITLES HASH IS RECORDED, NOT LOOKED UP.  'probe.opensubtitles_hash' is the 64-bit sum of the first and last 64 KiB plus the size, on the container summary as 'oshash'.

A title carrying no part marker that matches a marked pair resolves to the FIRST episode of the pair.  Ranges appear as 'e17-18', 'e01-2', 'e16-18', 'e44+45', 'E01E02' and 'e15&16'.  A BARE SECOND NUMBER IS A RANGE END ONLY WHEN IT SITS AGAINST ITS SEPARATOR, since a numeric title after ' - ' otherwise reads as a range end;  'episodes.RANGE_PATTERNS' takes a second number without its own 'e' only with no whitespace on either side.

A RANGE COMES FROM THE SOURCE NAME AND IS ANCHORED ON THE MATCHED ENTRY.  'episodes.episode_range' reads the span from the name, applies it from the episode the title match settled on, and publishes the range only when the catalogue lists every episode in it;  a missing number publishes the first episode alone with a warning.  The title is the shared base when every entry in the range carries one ('Emissary (1)' and 'Emissary (2)' give 'Emissary'), and the first entry's title otherwise, with a note on the IDENTIFIED record;  the servers read the code, not the text.  The identity carries 'episode_last', the row stores it, 'episode_filename' takes it as 'last', and the EPISODE PART_NUMBER stays the first episode.  A file named as a single episode is never extended.

## 12.  Matroska metadata

Three separate title fields exist and are easily confused.

SEGMENT INFO TITLE, set with 'mkvpropedit FILE --edit info --set title='.  The plain title, no year, real punctuation preserved.

GLOBAL TAGS TITLE, set with 'mkvpropedit FILE --tags global:file.xml'.  Must agree with the segment title.  ffprobe reports this one in preference when present.

TRACK NAME, set with 'mkvpropedit FILE --edit track:v1 --set name='.  For tracks the property is 'name', NOT 'title';  '--delete title' on a track is a parse error and mkvpropedit aborts the entire command.

### Movie tag shape

A single MOVIE-targeted Tag carrying exactly TITLE, TMDB, IMDB and DATE_RELEASED.  TITLE equals the segment Info title.  mkvpropedit omits '<TargetTypeValue>50</TargetTypeValue>', so VERIFY BY TargetType NAME, NEVER BY GREPPING FOR THE NUMBER.

Some files carry a large untargeted block of scraped metadata:  ACTOR, DIRECTOR, GENRE, PRODUCER, SYNOPSIS, LAW_RATING and so on.  THESE ARE LEGITIMATE AND MUST BE CARRIED FORWARD, because '--tags global:' replaces untargeted tags.  Only ENCODER, COMMENT, MAJOR_BRAND, MINOR_VERSION, COMPATIBLE_BRANDS, HANDLER_NAME, VENDOR_ID, CREATION_TIME and SOFTWARE are safe to drop.

### Television tag hierarchy

```
Target 70, COLLECTION   TITLE = show name, plus TVDB and TMDB ids
Target 60, SEASON       TITLE = 'Season N', PART_NUMBER = N
Target 50, EPISODE      TITLE = episode title, PART_NUMBER = episode number
```

The segment Info title is the EPISODE title.  For a range file the EPISODE PART_NUMBER is the first episode of the range.

### Flattened tag blocks

Every value lands inside ONE untargeted Tag whose Simple names carry the level as a slash path:  'COLLECTION/TITLE', 'SEASON/PART_NUMBER', 'EPISODE/TITLE', or 'MOVIE/TITLE' on a film.  Jellyfin does not read that as a hierarchy, and a grep for COLLECTION passes and an XML parse succeeds.  THE ONLY RELIABLE TEST IS STRUCTURAL:

- assert all expected TargetType names appear on their own Tag elements
- assert no Simple Name anywhere contains a slash

The data in a flattened block is normally correct, so the repair is structural, not a re-derivation.

### HDR declarations

A Matroska file declares its HDR metadata twice:  the bitstream carries mastering display colour volume (ST 2086) and content light level (MaxCLL, MaxFALL) as SEI messages, ffprobe's frame side data;  the container carries the same figures in the track's Colour and MasteringMetadata elements, ffprobe's stream side data.  A player, and every tool that reads only '-show_streams', sees the container, and ffmpeg's own x265 output from a source with no side data produces full SEI and an empty container.

THE PROBE READS BOTH SURFACES.  'probe.probe' keeps the container's figures from the stream side data, and for any stream '_is_hdr' accepts it runs a second, narrow ffprobe over the first twelve frames for the SEI.  'hdr_declaration_gap' names every declaration the bitstream carries that the container lacks or states differently.  The bitstream is authoritative:  a container that disagrees with its own SEI is repaired to match, and the change is recorded in the stage history.

A ZERO CONTENT LIGHT PAIR IS WRITTEN LIKE ANY OTHER, AND READ BACK THROUGH MKVMERGE.  libavformat surfaces a Matroska content light element only when both values are non-zero, so a 0/0 pair is invisible to ffprobe while 'mkvmerge -J' reports it as 'max_content_light' and 'max_frame_light'.  For an HDR stream on which ffprobe reports no content light, 'probe.probe' reads those two properties from 'mkvmerge -J', and a present element is a declaration whatever its numbers.

THE REPAIR IS A HEADER EDIT ON THE REMUX PATH.  'media.repair_hdr_declaration' runs beside 'fix_flags_and_language', through mkvpropedit, and writes the ST 2086 chromaticities and luminances and the two light levels from the bitstream figures.  mkvpropedit takes floats and spells the properties 'color-', not 'colour-';  A file with no mastering metadata on either surface is left alone;  HLG is legitimate without it.  The DOVI configuration record is out of reach for a header edit, so an RPU in the bitstream with no container record is held, never repaired.

NEITHER REMUX PATH LOSES THE DECLARATION AND NEITHER CREATES ONE.  mkvmerge ('strip_foreign') and 'ffmpeg -c copy' (the container conversion) both preserve the Colour element and leave an absent one absent.  The passthrough path is therefore the only place the repair is needed;  an encode heals the container by itself, because ffmpeg's HEVC decoder exports the SEI as side data and the muxer writes the container element from it.

THE GATE FAILS ON LOSS, NEVER ON ABSENCE.  'tags.check_hdr' runs inside 'readiness' at READY and again at VERIFIED, with the PROBED probe as its baseline.  A declaration the source carried on either surface that the output's container lacks is a hold, and so is a colour tag that changed.  A declaration the output gained is fine.

### Statistics

Every file carries per-track BPS, DURATION, NUMBER_OF_FRAMES and NUMBER_OF_BYTES, written with '--add-track-statistics-tags' and READ BACK by comparison gate 6.

USE '--tags global:' AND NEVER '--tags all:', which replaces every tag and destroys per-track statistics.  The global: form replaces only untargeted tags, BUT NOT UNIVERSALLY:  some files store statistics as global tags and those ARE destroyed.  ALWAYS re-check the byte-sum ratio after writing global tags and re-run '--add-track-statistics-tags' where it comes back zero;  'tags.refresh_statistics' does this and returns the ratio.  A trailing '--add-track-statistics-tags' is REQUIRED after any container conversion, after mkvpropedit-only edits, and after an encode.

## 13.  Tracks:  flags and language policy

### Flags

Audio tracks:  EXACTLY ONE default, and it must be the primary track rather than a commentary.  Assert the count is exactly one rather than merely testing that some default exists.  No default lets the player choose arbitrarily;  more than one is the common scene-release failure.

Subtitle tracks:  no default, in any language.  Forced subtitle tracks are exempt and keep their default.  Key that exception off the actual forced_track property and NEVER off the track name, since names like 'SDH' and 'Force' are inconsistent scene conventions.

### Language

English only.  Non-English audio and subtitle tracks are dropped at ingest.

```
keep_langs = eng en und
```

THE LIST IS ONE SETTING WITH FOUR READERS.  'probe.probe' counts foreign tracks with it, 'standards.screen' requires an audio track in it, 'media.strip_foreign' drops what is not in it, and 'tags.readiness' fails a track outside it;  each takes 'keep_langs' as a parameter and the orchestrator and the auditor pass the same value.  The module constants are its default.

VIDEO TRACK LANGUAGE IS 'eng', not 'und'.  The one exception is cover art:  a V_MJPEG track is a poster, not video, and stays 'und'.

'und' is kept and always will be, because an untagged track must never be assumed foreign.

THE PURPOSE IS CONVENIENCE, NOT DISK SPACE.  Do not report bytes saved;  report track outcomes:  foreign tracks gone, exactly one default audio, no subtitle default, nothing truncated.

TRAP:  an untagged foreign track survives, because untagged maps to 'und' and 'und' is kept.

TRAP:  the policy covers audio and subtitles equally.  'media.strip_foreign' keys on track type and language, so both are treated identically by construction.

A LANGUAGE STRIP CAN MAKE A FILE LARGER.  ffmpeg '-c copy' writes zlib-compressed PGS subtitles back uncompressed.  A file that grew is NOT a failed remux and is not a finding.

## 14.  Encoding and the encoder router

### The router

Evaluated in order, first match wins.  'encode.select' is a pure function of the probed attributes.

```
1  source codec is hevc or av1          PASSTHROUGH
2  television and the source is SD      PASSTHROUGH
3  Dolby Vision RPU present             libx265    on any setting
4  output_codec av1 and grainy          libsvtav1  CPU
5  output_codec av1                     av1_qsv    GPU
6  grainy                               libx265 aq-mode=4:tune=grain
7  otherwise                            libx265 aq-mode=3
```

GATE ORDER IS LOAD BEARING AND BREAKS SILENTLY IF DISTURBED.  A misrouted title produces a valid file with the wrong tradeoff, so after touching this function exercise one title per gate and read the resolved gate back from '/api/titles/<id>', corroborated against the output file's codec.

Gate 1:  an already-AV1 file is never transcoded back to HEVC.

Gate 2:  an SD source has little to gain and a generation of quality to lose.  SD means display height below 'sd_display_height', 720, computed from width times SAR over height.  'encode_sd' re-enables it per kind;  for movies it is reachable only once the standards floor admits an SD source.

Gate 3:  AV1 Dolby Vision is profile 10 and effectively nothing plays it, so DV titles always take the x265 path and the x265 path can never be retired.  GATE 3 IS UNREACHABLE FOR REAL MATERIAL:  every Dolby Vision profile is HEVC or AV1, so gate 1 is what protects the RPU;  'build_command' refuses any encoder but libx265 for a DV title regardless.

PASSTHROUGH MEANS NO VIDEO RE-ENCODE, NOT NO PROCESSING.  A passthrough title is still remuxed to Matroska, language stripped, flag corrected, tagged and given statistics.

### x265 parameters

Settled on a calibration batch.  THE DEFAULTS ARE NOT RETUNED IN CODE.  Every figure below is a per-kind setting on the Encoding group ('x265_preset', 'x265_crf', 'x265_aq_mode', 'x265_aq_mode_film', 'x265_tune_film', 'x265_psy_rd', 'x265_psy_rdoq', 'x265_deblock', 'x265_pix_fmt', and 'x265_extra_params' appended verbatim), so a deviation is made on the settings page and carried in every routed title's decision.  The registry's defaults are the constants in 'encode.py'.

```
libx265, preset slow, crf 18, pix_fmt yuv420p10le
-x265-params <aq>:psy-rd=2.0:psy-rdoq=1.0:deblock=-1,-1
aq = aq-mode=4:tune=grain on film sources, aq-mode=3 otherwise
```

HDR SIGNALLING TRAVELS INSIDE THE PARAMS STRING TOO.  For an HDR source 'x265_hdr_params' appends 'colorprim', 'transfer', 'colormatrix', 'range=limited', 'hdr10=1', 'master-display' from the ST 2086 figures and 'max-cll' from the light levels, bitstream figures first.  The libx265 wrapper carries all of that through on its own;  the params guard against a wrapper that stops doing so.  'hdr10-opt' is NOT passed:  it changes chroma QP offsets and is a tuning decision.

DOLBY VISION THROUGH AN ENCODE IS THREE PARAMETERS.  '-dolbyvision auto', the default, silently drops the RPU;  '-dolbyvision 1' without VBV fails with "Dolby Vision requires VBV settings to enable HRD";  with 'vbv-maxrate' and 'vbv-bufsize' it carries the RPU intact.  So 'build_command' passes '-dolbyvision 1' and 'x265_dv_vbv_kbps', default 'X265_DV_VBV_KBPS', for both VBV figures, and the build gate asserts the wrapper has the option.

KNOWN ISSUE:  'x265_dv_vbv_kbps' IS AN UNMEASURED RATE CAP, THE ONLY CAP IN THE PIPELINE.  Default 40000, an expectation of sitting above a 1080p CRF 18 slow encode's peaks;  where it binds, x265 raises QP in the highest-complexity scenes.  x265 also derives the HEVC tier from the VBV maxrate, and 40000 kbps exceeds Main tier's 20000 at Level 4.1, so every DV encode signals High tier, which some hardware decoders do not accept.  Unmeasured because gate 1 lets no DV title reach an encoder.

THE THREADING FIGURE IS NOT PART OF THAT TUNING.  'pools=' is appended to '-x265-params', and 'lp=' to '-svtav1-params', from 'encode_threads' divided by 'cpu_slots', autodetected from the cgroup quota when the setting is 0.

USE tune=grain ON FILM SOURCES.  x265 reads film grain as detail worth preserving and pays for it frame by frame;  a grain-heavy 35mm source on plain 'aq-mode=3' produces nearly double the bitrate of a clean title at the same CRF.  JUDGING FROM RELEASE YEAR IS NOT RELIABLE;  check the source.

TEN GIGABYTES IS A GUIDE, NOT A LIMIT:  a prompt to check whether the grain tune was missed.  Raising CRF to pull a file under the number is the WRONG move;  the size is a preference, the quality target is not.

### AV1 parameters

STARTING POINTS, NOT SETTLED VALUES.  The calibration batch has not been scored.

```
av1_qsv     -preset veryslow -global_quality 26, p010le via hwupload
libsvtav1   -preset 4 -crf 24 -pix_fmt yuv420p10le -svtav1-params tune=0:film-grain=8
```

'tune=0' is the subjective mode; the default 'tune=1' targets PSNR and is wrong here.  'film-grain' is the AV1 answer to 'tune=grain':  grain is synthesised at decode.  Its 'film-grain-denoise' companion is a calibration variable.

BEFORE FLIPPING THE AV1 DEFAULT, run a calibration batch and score against the SOURCE, not the x265 table, since the reference films are already HEVC.  Score with the 'ssim' filter;  the question is size at equal SSIM.

### Automatic grain detection

The grain signal cannot come from a hand-written sidecar, because in an automatic chain nobody writes one.  Measured instead:  a 20 second sample from the middle of the file, encoded twice at a fixed CRF, once clean and once through a light 'hqdn3d' denoise;  a grainy source shows a large size delta, and the ratio is logged.  'grain_threshold' tunes it, per kind;  the sample length, position and the probe's own CRF and preset are settings on the Probes group.

THE PROBE RUNS AT ROUTING, ON THE SOURCE, ONE AT A TIME.  Its scratch directory is 'encode/.probe/<title_id>', removed afterwards and swept at startup as ownerless.  Probes are serialised on a semaphore of one.  A sidecar 'film=' skips it, as does DRY_RUN.

An 'encode.job' sidecar beside the source overrides the probe and wins:

```
film=1                  force the grain path, film=0 forces the clean path
codec=av1               per-title output codec
crf=17                  per-title quality target
crop=1920:804:0:138     skip cropdetect and use this
fields=telecine         skip the field probe;  progressive, interlaced or telecine
```

### Field handling

THE ENCODER IS NEVER HANDED FIELDS AS FRAMES.  Every encoder-bound title is classified at routing and the command carries a filter when it needs one.

THE PROBE IS 'idet' OVER THE GRAIN SAMPLE.  'media.field_probe' decodes the same 20 s at 45 percent, video only, through 'idet', and reads the multi-frame and repeated-field counts:

```
telecine     repeated fields (top + bottom) at 10 percent or more of frames
interlaced   otherwise, TFF + BFF greater than progressive
progressive  otherwise
```

It runs in '_route' under the grain probe's semaphore, for non-passthrough decisions only.  'ffprobe' 'field_order' is recorded and not consulted, because DVD headers routinely say 'progressive' over telecined content.  The decision carries 'fields', rebuilt at ENCODING.  A sidecar 'fields=' wins.

THE THREE CHAINS, AHEAD OF THE CROP AND AHEAD OF THE QSV UPLOAD:

```
telecine     fieldmatch,yadif=deint=interlaced,decimate    23.976 out of 29.97, one frame in five removed
interlaced   bwdif=mode=send_frame                         frame count kept
progressive  nothing
```

Fields are matched on the full stored frame, so the field filter precedes any crop;  on the QSV path the software filters run before 'format=p010le,hwupload'.

VERIFICATION KNOWS ABOUT THE FIFTH FRAME.  For a 'telecine' decision the expected packet count is four fifths of the source within one percent, noted 'telecine removed n of m frames', and 'total_frames' is scaled the same way.

ACCEPTED LIMITATION:  a mixed episode classifies on one 20 s sample, so a film-and-video show sampled on a video-only stretch classifies interlaced.  The x265 parameters are unmeasured at SD, and 'encode_sd' defaults to off.

### Two traps in the command shapes

MAPPING.  Explicit maps, never '-map 0', which hands a V_MJPEG cover-art track to the video encoder.  Use '-map 0:v:0 -map 0:a -map 0:s? -map 0:t? -map_chapters 0'.

THE SDR STAMP IS A VALUE, NOT A FLAG.  An untagged SDR source is stamped smpte170m only when it is SD;  an untagged HD source is stamped bt709, because a 601 matrix on a 1080p master shifts every saturated colour.  SD is 'encode.is_sd', the predicate router gate 2 uses.

COLOUR FLAGS, AND THE FIX THAT MUST NOT BE GENERALISED.  On the x265 path, ffmpeg's '-color_primaries', '-color_trc' and '-colorspace' suppress what '-x265-params' sets, so they are NOT passed there;  colour goes inside '-x265-params', with '-color_range tv' alongside.  Neither av1_qsv nor libsvtav1 accepts colour properties through a params string, so on both AV1 paths those three flags are the only mechanism.

DO NOT APPLY SDR COLOUR ASSUMPTIONS TO HDR MATERIAL.  The smpte170m stamping is WRONG for anything bt2020.

## 15.  Container remuxing

NEVER use 'ffmpeg -map 0' when remuxing MP4 to MKV.  Many HandBrake MP4 rips carry a 'bin_data' QuickTime chapter stream that Matroska rejects outright.  Use '-map 0:v -map 0:a -map 0:s? -map_chapters 0'.  Dropping bin_data is lossless because the chapters live in the MP4 chapter atom and ffmpeg carries them into Matroska natively.  ALWAYS ASSERT THE CHAPTER COUNT IS PRESERVED;  'media._mp4_to_mkv' fails the remux when it changes.

Old Xvid and DivX AVI rips often need '-fflags +genpts -bsf:v mpeg4_unpack_bframes'.  Allow about 2 seconds of duration tolerance, not 1:  genpts recomputes timestamps and routinely lands a second either side with every packet preserved.  Compare packet counts to distinguish a damaged source from a bad remux:  an AVI header that over-claims frames drifts by exactly that count while nb_read_packets is identical in and out.

ffmpeg cannot infer a container from a temp extension like 'file.mkv.part' and fails at muxer init.  ALWAYS PASS '-f matroska' EXPLICITLY when writing to a temp name;  without it the remux silently falls through to a fallback while per-file verification still passes.

mkvmerge silently makes a muxed-in sidecar subtitle the default track.  After any sidecar mux, clear the flag explicitly.

A REMUX INHERITS THE SOURCE CONTAINER'S TITLE.  Set the segment Info title explicitly after any remux.

ffmpeg does NOT compute track statistics.  It carries forward whatever the source held, and a container conversion from a source holding no Matroska statistics writes NONE at all.

## 16.  Verification and quality signals

```
Encode completed        absence of .part files
Nothing truncated       compare VIDEO STREAM duration
Stream fidelity         compare packet counts in versus out
Corruption              full decode with 'ffmpeg -f null'
Statistics accuracy     sum NUMBER_OF_BYTES, compare against file size
Letterboxing            metadata first, cropdetect only on real candidates
```

TRAP:  COMPARE VIDEO STREAM DURATION, NOT CONTAINER DURATION.  After a language strip the container figure can drop by minutes with nothing lost, because the longest stream was a removed subtitle track.

WHAT 'orchestrator._verify' ACTUALLY RUNS:  video stream duration, video packet count in against out, the statistics byte-sum ratio, and the readiness check, which carries the structural tag test and the HDR invariant from section 12.  The full decode scan is deliberately NOT implemented:  it costs a complete read of the output per title, and an encode preserves the video packet count exactly, which is why that check is an equality rather than a tolerance.

TRAP:  a decode scan does NOT catch dropped audio.  Surviving packets are valid and there is simply a hole in the timeline.

TRAP:  A CONTAINER-LEVEL PROBE IS NOT EVIDENCE OF ABSENCE.  '-show_streams' sees only the Colour element;  section 12 has the two-surface probe.

'_verify' COLLECTS, IT DOES NOT RAISE ON THE FIRST OBJECTION.  Every check is evaluated and the title holds once with every problem named, or is forced past all of them with each recorded in the stage history.

### Cropdetect

TRAP, AND IT SILENTLY INVALIDATES WHOLE AUDITS:  cropdetect's 'limit' is read in the source's NATIVE BIT DEPTH.  limit=24 is correct for yuv420p, but against a 10-bit source it means 24 of 1023, below video black, so bars are never detected.

```
limit = 24 * 2^(depth - 8)      so 96 for 10-bit, 384 for 12-bit
```

'probe.cropdetect_limit' implements the scaling.  WHEN A DETECTOR AND A DECODED FRAME DISAGREE, BELIEVE THE FRAME.

SAMPLING, BLACK LEVEL AND PLAUSIBILITY, FROM TINYMEDIAMANAGER'S DETECTOR.  'media.detect_crop':

- takes six 2-second samples between 2 and 92 percent of the usable duration, the spacing capped at 900 s, advancing 1.4 spacings past a rejected sample, at most twelve attempts;
- measures the black level first, 'signalstats' YMIN over the first sample, and sets 'limit' to 1.5 times it, never below the depth-scaled floor above and never above 13 percent of the bit-depth range;
- rejects a sample whose bars differ left against right by more than 1.5 percent of the width or top against bottom by more than 2 percent of the height, or whose crop keeps less than half the width or 60 percent of the height;
- takes the geometry seen in the most plausible samples as primary, and records a second geometry seen in at least 6 percent of samples whose display aspect differs by 0.15 or more as 'secondary' with its share.  The filter uses the primary;  'compare.MEASURED' carries the secondary as a 'variable aspect' row that no gate votes on.

The result carries no filter when the bars are under 20 px, and still carries 'secondary' when there is one.

Anamorphic storage defeats a pure geometry check.  Compute display aspect from width times SAR over height; never infer it from stored dimensions alone.

### Quality signals

ONE AUDIO TRACK LABELLED STEREO likely means a bonus feature;  feature rips carry 2 to 5 audio tracks including a 5.1.

A SCOPE FILM STORED AT 1920x1080 means baked-in letterbox.  The real picture may be only 800 or 818 lines.

A 4:3 FRAME WHOSE CONTENT FILLS IT is correct, an Academy-ratio or open-matte transfer.  A 4:3 FRAME WITH WIDE CONTENT INSIDE IT is a genuine defect.

A LIBRARY ENTRY CAN BE A PAL DVD RIP WEARING AN HD LABEL.  720x576 at 25 fps with SAR 64:45 runs 4 percent fast with audio pitched about a semitone sharp.  Check frame rate and stored dimensions before assuming a folder holds what its name implies.

## 17.  Known false positives

Do not report these as defects.

CLAIMED BYTES EXCEED FILE SIZE.  Caused by zlib-compressed PGS subtitle tracks:  mkvpropedit reports uncompressed logical bytes while Matroska stores them compressed.  Only a large overshoot, above roughly 1.5x, means genuinely stale tags.

VIDEO BPS EXCEEDS WHOLE-FILE BITRATE.  BPS is computed over the track's own duration, not the container's.  Comparison gate 6 is not affected, since it reads video-track BPS on both sides.

A STREAM WITH NO STATISTICS TAGS.  Cover-art mjpeg streams are never tagged.

RUNTIME 8 MINUTES OVER THE PROVIDER'S.  Usually long end credits, or a legitimately different cut.

DUPLICATE NUMBER_OF_BYTES AND NUMBER_OF_BYTES-eng TAGS.  Caused by setting a language tag on a file that already had statistics tags.  Re-run '--add-track-statistics-tags' to normalise.

A DATE_RELEASED THAT IS A FULL DATE RATHER THAN A YEAR.  Scraped-metadata files carry ISO dates such as '1977-05-25' where the canonical shape carries '1977'.

A COLLECTION TITLE EQUAL TO THE EPISODE TITLE.  Legitimate whenever a pilot or TV movie shares the series name.  Test the hierarchy structurally instead.

A TITLE THAT APPEARS TO DIFFER FROM ITS FILENAME.  If the title contains a comma, 'ffprobe -of csv=p=0' wraps the value in literal quotes.  Use an XML parser or a JSON output format.

A TAG TITLE THAT DIFFERS FROM ITS FILENAME BY AN UNSAFE CHARACTER.  Apply the transform to the tag before comparing.  Comparing mkvextract's raw output against a decoded filename also falsely flags an ampersand, because '&amp;' is legitimate XML encoding.

SEASON 00 WITH GAPS IN ITS NUMBERING.  Specials are numbered per TVDB and a library normally holds only some.  Any gap check must exempt season 0.

A FILE THAT GREW AFTER A LANGUAGE STRIP.  See section 13.

## 18.  Dolby Vision and HDR

Files carrying Dolby Vision survive a '-c copy' remux intact, including the DOVI configuration record, dv_profile, dv_level, rpu_present_flag, bl_present_flag and any mastering display metadata.

A DV title is NEVER AV1-encoded, and in practice is never encoded at all;  section 14 records gate 1 passing it through, gate 3 as the backstop, and the '-dolbyvision 1' mechanism should one reach the encoder.  No extraction tool is in the image and none is needed.

Detection is from the v:0 side data list, reading dv_profile and rpu_present_flag.  That is the CONTAINER's DOVI configuration record.  The probe also notes an RPU in the bitstream, and a file carrying one with no container record is held rather than repaired, because a header edit cannot write that record.  Plain HDR10 with no RPU is not Dolby Vision and encodes normally; AV1 carries HDR10 static metadata correctly.

## 19.  Single instance, locking and concurrency

ONE INSTANCE AT A TIME, ENFORCED.  The supervisor takes an exclusive 'flock' on 'MEDIA_CONFIG/procrustes.lock' before doing anything else.  A second instance pointed at the same mounts WAITS for the first to exit.  Every other guard in the process is 'threading.Semaphore' or 'threading.RLock', which are process local;  two supervisors sharing the mounts would sweep each other's encode area and could publish the same title twice.

flock rather than a PID file, because the kernel releases it when the holder dies.

The wait is interruptible.  SIGNAL HANDLERS ARE INSTALLED BEFORE THE LOCK IS ATTEMPTED;  registered later, a stop during startup has no handler and sits until SIGKILL.

ENCODE JOB DIRECTORIES RECORD THEIR OWNING PID.  The startup sweep reclaims only directories whose owner is no longer alive.

ENCODES RUN UNDER A TRACKED SUBPROCESS AND ARE TERMINATED ON SHUTDOWN.  Otherwise the supervisor exits while ffmpeg keeps running, reparented to the init process and still writing into the encode area.

PUBLISHING IS ATOMIC.  'paths.publish_file' reserves the destination with 'O_CREAT | O_EXCL', copies into it, then replaces;  a check-then-act with 'os.path.exists' and 'shutil.move' races when the same film arrives twice under different release names.  Quarantine naming uses the same exclusive-create loop.

### Pools

```
GPU encode threads   1     the A310 has one media engine, queueing more at it gains nothing
CPU encode threads   1     x265 preset slow and svt-av1 preset 4 both saturate the allotted cores
passthrough threads  1     fixed;  the I/O-only path, bounded by the disk rather than a core
```

Each pool has exactly as many threads as the device has slots and pulls from its own queue.  'max_jobs' is the assessment pool and has nothing to do with encoding capacity.  All three resize live per section 5.

TWO CPU ENCODERS GAIN LITTLE, AND ONLY AT LOW RESOLUTION.  x265's wavefront parallelism is bounded by CTU rows, about 11 at 720p and 17 at 1080p, so two four-thread encodes recover idle threads on SD and 720p, near zero at 1080p and above, at doubled per-title latency and doubled staged copies.

## 20.  Logging

EVERY MODULE LOGS WHAT IT DOES, and that is not shaped by testing.  Two tiers, switched by 'LOG_LEVEL':

- 'info', the default.  What happened.  One line per meaningful action naming the title, the stage and the outcome, and why when a stage fails or degrades.  No command lines, no arithmetic, no per-gate working, no chatter for steps that cannot fail.
- 'debug'.  Why.  Full command lines, per-gate comparisons, candidate scoring, measured figures against their thresholds, derived geometry, raw tool output.

Logs go to stdout and to 'config/logs/procrustes.log', and the tail is served at '/api/logs'.  Every log line carries the title id where one exists.

A POLL THAT FINDS NOTHING NEW IS NOT AN ACTION.  The watcher's 'already claims this path' line is a debug line.  ENCODED and VERIFIED each emit an info line carrying the same outcome text they write into the stage history.

CONFIG RESOLUTION IS LOGGED BY 'main', NOT BY 'config'.  'Config' is constructed before logging is set up, so it records where each variable came from and 'main' prints that at debug once handlers exist.

THE SETTINGS BANNER FOLLOWS THE CONFIG BANNER, AT INFO:  one 'setting' line per storage key with a '*' on a stored value.  Every operator change logs the key with its old and new value, and 'pools retargeted' names the three targets after a slot change.

## 21.  Scripting rules and traps

NEVER GATE A JOB ON A COMMAND-LINE STRING MATCH.  'pgrep -f "libx26[5]"' matches an unrelated 'grep libx265', a shared PID namespace sees every process on the host and a private one sees nothing.  Gate on something the job OWNS, per section 19.

mkvpropedit REPORTS ITS ERRORS ON STDOUT, NOT STDERR.  Capture both streams and key pass or fail on the EXIT CODE.

MKVPROPEDIT TRACK SELECTORS ARE TYPE-RELATIVE; MKVMERGE TRACK IDS ARE GLOBAL.  mkvmerge -J reports the first audio track as id 1, but its selector is 'a1', not 'a2';  adding one to the global id fails SILENTLY on a multi-audio file.  'probe.track_selectors' counts the position among tracks of that type.

PARSE FFPROBE OUTPUT FROM STDOUT ALONE.  Stderr carries harmless decode warnings that break float(), and a source that then parses as None must fail the comparison rather than skip it.

VERIFY TAG CONTENT, NOT JUST PRESENCE, AND ASSERT ON STRUCTURE.  A grep confirming a target exists passes an inverted hierarchy, and section 12 records why an XML parse alone passes a flattened block.

IDEMPOTENCY GUARDS MUST KEY ON THE DESTINATION, NOT THE SOURCE.  Asking "is the source file gone?" defers forever on remuxed files.

DELETE OR RETIRE SOURCES ONLY AFTER THE OUTPUT PASSES VERIFICATION.

RE-DERIVE THE FILE LIST AT APPLY TIME.  Selection, action and verification must EACH derive their own list, and SELECTING THE WORK AND VERIFYING THE WORK MUST USE DIFFERENT CODE PATHS;  reusing one query for both is how a run reports total success over an incomplete list.

'glob.glob' treats '[i_c]' in a real folder name as a character class and silently matches nothing.  USE os.walk for paths containing brackets, which every '[tvdbid-N]' folder does.

RENAME FAILS ACROSS MOUNT POINTS EVEN ON THE SAME DEVICE.  Linux refuses 'rename(2)' when the two paths are on different mounts:  two bind mounts of one filesystem report the same 'st_dev' and 'os.replace' raises EXDEV anyway.  So the layout is one root with the pipeline directories as siblings, and 'paths.move_file' tries 'rename' first and falls back to copy-then-remove only on EXDEV.

A FAILED MOVE MUST RELEASE ITS OWN RESERVATION.  'unique_path' reserves the destination with 'O_CREAT | O_EXCL' before any data moves, so a failure between the two leaves a zero-byte file in quarantine.  'move_file' removes the reservation on every failure path.

DOCKER IGNORE PATTERNS ARE PATH-PREFIX MATCHED FROM THE CONTEXT ROOT, NOT gitignore SEMANTICS.  A '.dockerignore' line of '__pycache__/' excludes only './__pycache__/'.  Use '**/__pycache__/' and '**/*.py[cod]'.

## 22.  Image and build

Base 'alpine:3.24'.  The image installs GNU 'coreutils', 'findutils' and 'bash', so nothing in the tooling meets busybox.

Packages:  ffmpeg, mkvtoolnix, python3, py3-argon2-cffi, py3-qrcode, bash, coreutils, findutils, jq, ca-certificates, tini, su-exec, shadow, libcap, libcap-utils, libva, libva-utils, intel-media-driver, libvpl, onevpl-intel-gpu.  All Alpine packages;  nothing from PyPI.  'py3-qrcode' depends on 'py3-pillow' at the package level;  the SVG path the code uses never imports it.

'su-exec' is the privilege drop;  'shadow' provides 'useradd', 'groupadd' and 'usermod';  'libva-utils' provides 'vainfo';  'libcap-utils' provides 'setcap' at build time.  '/usr/sbin/nologin' is a symlink the build creates, because Alpine's lives at '/sbin/nologin'.  'openssl' is deliberately ABSENT, so that nothing in the image can generate a certificate.

musl rather than glibc.  Every heavy operation is a subprocess call into ffmpeg, x265 or mkvtoolnix, all of which Alpine builds against musl routinely;  musl's resolver since 1.2.4 falls back to TCP for large answers, so the provider lookups are not affected.

BINDING 443 AS A NON-ROOT PROCESS.  The build applies 'cap_net_bind_service' to the resolved python binary, not to the '/usr/bin/python3' symlink, and verifies with 'getcap' immediately after, since file capabilities are easy to lose silently.  Docker's default capability set already carries CAP_NET_BIND_SERVICE, so the compose file needs no 'cap_add'.

### The build gate

The build FAILS if ffmpeg lacks libx265, libsvtav1 or av1_qsv.  DO NOT DELETE THIS CHECK IF IT FAILS.  The escalation order is av1_vaapi, the same hardware through VAAPI rather than oneVPL, then a pinned ffmpeg from Alpine's edge community repository.  Pick one at build time and record which in an image label; never let the runtime choose.  Alpine's ffmpeg carries all four, so no fallback is in use.

THE GATE ALSO ASSERTS THE LIBX265 WRAPPER HAS '-dolbyvision', per section 14, so the build cannot ship an ffmpeg older than 7.1 claiming otherwise.  AND THAT 'argon2' AND 'qrcode' IMPORT, with argon2id available:  'app/auth.py' imports both at module level, so without them 'import app.main' fails.

### Runtime GPU probe

At startup, 'vainfo' must report VAProfileAV1Profile0 with VAEntrypointEncSlice.  A failed probe does NOT crash:  it marks the GPU degraded and routes av1_qsv titles to libsvtav1, and at the HEVC default it changes nothing.  Every encode logs which encoder actually ran.

## 23.  Versioning and release tags

'x.0.0' is a release.  '0.x.0' is the implementation of new features.  '0.0.x' is a bug fix.  The current version is 0.11.2.

EVERY BUILD INCREMENTS THE VERSION.  A build whose 'VERSION' equals the one before it cannot be told apart from it.  NOTHING ENFORCES IT.  The workflow reads 'VERSION' from 'app/__init__.py', tags the image with it and stamps 'org.opencontainers.image.version' from it;  a build on an unincremented version publishes an image whose version tag overwrites the previous one on GHCR.  The repository does not use git tags.

'VERSION' IN 'app/__init__.py' IS THE SINGLE DEFINITION.  Three consumers:  the provider User-Agent, built as 'procrustes/<VERSION> (+<repo url>)', the startup log line, and the 'version' field on '/api/status'.  Wikimedia rejects generic and browser-imitating agents with 403, so a browser User-Agent is not an option.

THE WORKFLOW IS 'workflow_dispatch' ONLY.  Images are published by a manual run from the Actions tab and by nothing else.

## 24.  Validation and testing

THERE IS NO LOCAL TEST SUITE AND THERE SHOULD NOT BE ONE.  A workstation and this container are different environments, so functionality is validated in the container and nowhere else.

While code is being written, the only validation performed is validation of the code itself:

```
python3 -m compileall -q app          every module parses
python3 -c "import app.main"          the package imports, no circular imports
python3 -m app.check_names            every name a function loads is defined in its module
```

All three are properties of the source.  The CI workflow runs exactly these three as its 'validate' job, and the image job depends on it.

THE THIRD CHECK EXISTS BECAUSE THE FIRST TWO CANNOT SEE A MISSING CONSTANT.  A name inside a function body is resolved when the function runs, so a removed module-level constant passes 'compileall' and the import and fails with 'NameError' on the first title to reach that stage.  'app/check_names.py' is a standard-library AST pass:  for every function it collects the names visible to it and reports any name loaded that is in none of them.

THE TEST INSTANCE IS WIPED FOR EVERY BUILD.  'state.db' and the queues, 'import/', 'encode/', 'complete/' and '.quarantine/', are cleared before each test build is run, so title ids restart from 1 and everything in 'complete/' is a test artefact that is never promoted.

FUNCTIONALITY IS VALIDATED AGAINST A BUILT CONTAINER, BY HAND.  Every case asserts on a file, a filename, a tag block, a track list, an API response, an exit code or a health state;  no case reads a log line, and every case is repeatable from a stated starting state.

THE ROUTER CASES ARE THE HIGHEST RISK.  A misrouted title produces a valid file with the wrong tradeoff and nothing fails.  State the expected gate and encoder per case and read both back from '/api/titles/<id>', corroborated against the output file's codec.  Colour flags, per section 14, are checked on the output file's properties rather than on the command that produced them.

The instance lock, the handover between instances and the SIGTERM path are container cases.

## 25.  Decisions and their rationale

### x265 is the default output codec, not AV1

AV1 is 25 to 30 percent more efficient and is where this library should end up, but the current Apple TV 4K has no AV1 hardware decoder, and Jellyfin's on-the-fly transcode to h264 spends at delivery time some of the quality the AV1 encode paid for;  direct play has no failure mode.

So AV1 is fully built, tested and selectable, and 'output_codec' defaults to hevc for both kinds.  Flipping it is a settings change plus a calibration batch, which is the entire reason encoder selection is a router rather than a hardcoded ffmpeg line.

### The manual review gate became an automatic readiness check

The old pipeline stopped for a human before encoding.  It is automatic and holds on failure;  DRY_RUN and the held queue buy back the confidence that gate provided.

### Television is processed automatically in aired order

Chosen over a per-show held decision.  The protection is section 11's title match, not the order:  the number is derived from the match, so release-group renumbering cannot propagate.

### No third-party Python dependencies beyond the two named

Section 3's rule, stated here as a decision so it does not erode:  every dependency added is a supply chain, an upgrade treadmill and an audit burden.  A third needs the same case made:  a capability the standard library cannot supply, and an Alpine package that supplies it.

### The web UI requires a login, over HTTPS only

Section 30 carries the mechanism.  The login is in-app and optional:  the first visit to a fresh store asks whether to require authentication, the answer is a setting, and switching it off opens the dashboard and the API to anyone who can reach the port.  Off is a logged degradation with a header pill, in the shape of DRY_RUN.

TLS DOES NOT CHANGE WHO CAN USE THE API.  The listener is HTTPS on 443 with no HTTP listener and no redirect;  TLS buys that the traffic, the password and the session cookie are not readable in transit, and the cookie is marked Secure.

THE CERTIFICATE IS SUPPLIED, NEVER GENERATED.  A missing, unreadable or malformed pair is logged and the supervisor exits non-zero before any listener is created, because a silent downgrade to HTTP would defeat the requirement;  with 'restart: unless-stopped' that is a restart loop, and that is the intended behaviour.

THE CONTAINER DOES NOT VALIDATE THE CERTIFICATE IT IS GIVEN.  Do not add issuer or chain checks:  the operator owns what is mounted, and chain validation would reject a private-CA certificate.  The healthcheck probes '127.0.0.1' with verification disabled, because a certificate issued for the service hostname fails hostname verification against a loopback address, and the probe is testing liveness rather than identity.

## 26.  ARM configuration, upstream

Automatic Ripping Machine produces most of what arrives in 'import/'.  It is not part of this container, but its settings determine what the container is handed.

```
MINLENGTH        2400     titles under 40 minutes are ineligible
SKIP_TRANSCODE   true     ARM keeps MakeMKV output, largest file assumed main feature
RIPMETHOD        mkv      MakeMKV direct
PREVENT_99       false    setting true ejects the disc and rips nothing
MAINFEATURE      inert    HandBrake option;  with SKIP_TRANSCODE true, HandBrake never runs
```

MINLENGTH IS THE SETTING THAT MATTERS.  Below it a bonus featurette qualifies as a disc's main feature;  2400 is what makes the 40 minute movie floor in section 7 a second line of defence rather than the only one.  Every 'HB_*' setting does nothing while SKIP_TRANSCODE is true.

## 27.  The library audit

A BACKGROUND SWEEP FOR EVERY DEVIATION THE PASSTHROUGH PATH CAN CORRECT, AND NOTHING THAT NEEDS AN ENCODE.  'audit.Auditor' is a thread beside the watcher.  It walks the mounted libraries with 'os.walk', never glob, opens files read only, and assesses each against exactly the set the passthrough path repairs:

```
container not Matroska                    remux
foreign audio or subtitle tracks          language strip
audio default count not exactly 1         flag repair
non-forced subtitle marked default        flag repair
video track language not eng              flag repair
tag block missing, inverted or flattened  tag rewrite
folder and file names differ from the     republish through the pipeline
  names the tag block would produce
a path component breaks a section 9 rule  republish through the pipeline
segment title differs from the tag TITLE  mkvpropedit
statistics missing or stale               statistics refresh
HDR declaration short of the bitstream    mkvpropedit, section 12
a single-numbered file whose length says  listed with its range name, no repair
  two episodes
```

Resolution, bit depth, codec, letterbox and PAL speed-up are never findings.  They need an encode, which is the loss gate 1 exists to prevent.

THE NAMING CHECK NEEDS NO PROVIDER.  The audit builds the names the pipeline would publish, through the same 'titles.movie_folder', 'movie_filename', 'show_folder', 'season_folder' and 'episode_filename' the publish step uses, and compares whole names with what is on disk.  It cannot check the reverse:  a tag that is itself wrong, and names that agree with it, pass until the file goes through the pipeline and rung 1's verification rewrites the identity.

- A NAMING ROW NEEDS A COMPLETE TAG BLOCK.  A block missing any field the name needs reports 'tag incomplete' once per row and compares nothing;  the 'tag structure' row already carries that finding.
- FINDINGS ARE PER FILE.  A wrong show folder is one finding on every episode in it.
- THE SHOW FOLDER'S YEAR COMES FROM THE FOLDER, since the COLLECTION block carries no year;  'YYYY' when it has none.

THE TWO-EPISODE LISTING PROPOSES AND DOES NOT ACT.  A television file whose on-disk code is a single episode N is listed when no file in its season folder carries N+1 and its video duration is at least 'range_duration_ratio' times the median of the folder's other files, three others at least;  season 0 is exempt, as specials vary in length.  The rule is per folder while the sweep skips unchanged files by stat, so 'findings' keeps 'duration_s' per file and 'Auditor._folder_phase' re-derives the row from stored figures after every complete pass, probing a row recorded without a duration in place;  an N+1 file arriving later clears the row on the next pass.  The row carries the name section 10's form would give the file from its own tag block, 'S01E01-E02' with the part marker dropped, and 'repair' None:  a finding with no repairable row shows no Import button.  A double-length episode the catalogue numbers as one meets both conditions and is on the list with its figures;  the audit has no catalogue to tell it apart.  The threshold is unmeasured.

Repairing a naming finding is the same Import as any other:  rung 1 reads the embedded block, verifies it, and the published names are regenerated from it.  For a folder-only defect that copies every file in the folder through the pipeline;  renaming the folder by hand is the cheaper route and remains the operator's, per section 2.

IT IS THROTTLED AND IT SKIPS WHAT HAS NOT CHANGED.  'AUDIT_INTERVAL' seconds between files, default 2.  A 'findings' table in 'state.db' records path, size and mtime for every file assessed, so a repeat pass is mostly 'stat' calls.  A pass restarts 'AUDIT_SWEEP_INTERVAL' seconds after the last one finished, default 3600.  'AUDIT_INTERVAL=0' disables it, and it never starts when no library is mounted.

THE SKIP KEYS ON SIZE AND MTIME ONLY, SO A RULE CHANGE NEEDS A WIPE.  'Rescan entire library' in the findings dialog, 'POST /api/audit' with '{"rescan": true}', runs 'Auditor.rescan':  the findings table is deleted, a running pass is stopped through the '_restart' event so it does not leave a stat-skippable tail, and the next pass is a first pass by construction.  The wipe takes the repair links with it;  the title rows are untouched.

REPAIR IS BY RUNNING THE FILE THROUGH THE PIPELINE.  Each finding carries a copy action, 'POST /api/audit/<id>/import', which copies the library file into 'import/', a read of the library and a write under the writable root.  The copy lands as '<name>.part' and is renamed, because the watcher ignores '.part';  it refuses when the root lacks the space, when the name is already in 'import/', or while a title for that file is in the pipeline, per section 6.  The loop still ends by hand.

A COPIED TITLE MUST NOT COMPARE AGAINST ITSELF.  It resolves to the same provider ID as the file it came from, so section 8 would hold it as a no-vote pair.  The finding records the import path, the watcher stores 'origin_path' on the row, and '_find_incumbent' skips that one realpath.  Only the comparison is skipped;  screening and readiness still apply.  This does not use 'overridden', which section 7 makes far broader than this needs.

A header repair through the pipeline moves a whole file to correct a few hundred bytes of Colour header;  what that buys is a file verified by the same 'readiness' check as any other title.

The dashboard's 'Library findings' counter and its dialog are as README describes;  the Details table per finding is in the shape of the Compare dialog, container beside bitstream for the HDR rows, expected beside actual for the rest.

## 28.  Out of scope

Not in this repository, and adding them needs a decision rather than a commit:

- Any write path into a media library.  See section 2.
- YouTube.  Those rips are organised by hand and no naming, tagging or encoding standard applies.
- Music.  No standards are defined for it anywhere yet.
- Host-specific packaging.  No Unraid Community Applications template, no Docker Hub mirror.  The deliverable is the image plus a reference compose file that runs anywhere with Docker and a render node.
- The workstation scripts.  They live in their own tree and continue to run there unchanged.

ONE EXCEPTION TO THE PACKAGING RULE, ADDED DELIBERATELY.  The Dockerfile carries 'net.unraid.docker.icon', inert on every other host, because a container with no icon makes the auto-refreshing Unraid Docker page fill the host's '/var/log' tmpfs with errors.  The icon lives at 'media/procrustes.png', a placeholder expected to be replaced.  THE SAME FILE IS THE DASHBOARD FAVICON.  The Dockerfile copies it into 'app/static/' and 'webui' serves it at '/favicon.ico' as 'image/png' with a day of cache;  the page declares it with a 'link rel=icon'.

## 30.  Authentication

THE LOGIN IS IN FRONT OF EVERY PAGE AND EVERY API ROUTE BUT FIVE.  'GET /api/health' answers '{ok, version}' for the Docker healthcheck;  'GET /api/login' says whether authentication is on and whether the first account is still to be created;  'POST /api/setup' and 'POST /api/setup/totp' serve the first run;  'POST /api/login' takes credentials.  Everything else answers 401 '{"error": "login required"}' without a valid session, and the three pages serve 'login.html' in place of themselves.  'webui.Handler._user' is the one gate:  the session from the cookie, or the synthetic operator while authentication is off.

THE FIRST RUN IS ONE STATE, 'auth_enabled' ON WITH NO USER ROW.  The page asks 'Require Authentication?'.  No stores 'auth_enabled' off.  Yes takes a username and a method, Password, OTP/Google Authenticator or MFA with both, enrols the authenticator inline, and 'Auth.setup' verifies every factor the chosen mode needs before the row is written.  'user_create' checks the count inside the store lock, so two first-run posts cannot both succeed.  No account is seeded and no default password exists;  the window before the first answer is open to whoever reaches the port, accepted because a seeded credential would be a wider exposure.

THREE MODES PER USER, AND THE ROW'S MODE IS WHAT THE LOGIN CHECKS.  'password' needs a password hash, 'totp' needs a confirmed secret, 'both' needs both, and 'auth.mode_allowed' refuses a mode the row cannot satisfy.  The login form is one form for every mode, and the server ignores a factor the row does not require, so the page never reveals which factors a username needs.  Every failure answers 401 with the same body, 'login failed';  the reason goes to the log with the username and the address.  An unknown username is verified against a dummy hash so the two paths cost the same.

ARGON2ID AT THE LIBRARY DEFAULTS, RECORDED AND NOT RETUNED.  'argon2.PasswordHasher()':  argon2id, three passes, 64 MiB, four lanes, a 32-byte hash and a 16-byte salt.  A stored hash that no longer matches those parameters is rehashed on the next successful password login.

TOTP IS RFC 6238 OVER RFC 4226 IN THE STANDARD LIBRARY, AND A CODE IS ACCEPTED ONCE.  HMAC-SHA1, six digits, a 30 second step, a window of one step either side, a 160-bit secret from 'secrets.token_bytes' in unpadded base32.  'auth.verify_code' returns the step it accepted and refuses any step at or below the row's 'last_totp_step';  the comparison is 'hmac.compare_digest'.  The secret is stored in the clear in 'state.db', since the server has to compute the same code.

THE ENROLMENT TRAVELS INLINE, NEVER IN A URL.  'auth.enrolment' returns the secret, the otpauth URI and the QR as an SVG string in one JSON body;  a URL carrying the secret would land in the handler's debug log line.  The QR is 'qrcode''s 'SvgPathImage';  Pillow is never imported.

A SESSION IS A RANDOM TOKEN THE STORE KNOWS ONLY BY ITS HASH.  'secrets.token_urlsafe(32)' in the cookie, its SHA-256 in 'sessions', expiring 'session_hours' from login;  logout deletes it, a password change deletes the user's other sessions, and removing a user drops theirs.  The cookie is 'procrustes_session' with 'Path=/; Secure; HttpOnly; SameSite=Strict'.  'SameSite=Strict' plus JSON-only POST bodies is the CSRF guard;  nothing else is added.

FIVE FAILURES IN FIFTEEN MINUTES LOCK THE USERNAME AND THE ADDRESS.  'auth.Lockout' is in memory, keyed both ways, and answers 429 with 'Retry-After' for the rest of the window;  a correct login inside it is still refused.  A restart clears it, accepted:  the lockout bounds the argon2 cost and the guessing rate, not the store.

EVERY CREDENTIAL CHANGE RE-CHECKS A FACTOR IN THE SAME REQUEST.  Changing the mode, changing the password, removing the authenticator and switching authentication off each take the current password or a fresh code through 'Auth._confirm';  a fresh code advances 'last_totp_step'.  Enrolling an authenticator writes 'totp_pending' and nothing else until a code confirms it.  Removing the authenticator is refused while the mode needs it.  Any signed-in user adds and removes users;  a user cannot remove their own account, which keeps at least one in the table.

THE SWITCH HAS ONE ROUTE AND TWO DIRECTIONS.  'POST /api/account/auth' is the only writer of 'auth_enabled'.  Off needs a signed-in user and a factor and logs who did it at warning.  On has no precondition:  the response sets no cookie, and the next load is the login form with users present or the first-run page with none.  Sessions are not touched in either direction.  While off, the User Settings page shows the switch and nothing per user, every per-user and user-management endpoint answers 409, the header carries the 'AUTH OFF' pill, Sign out is hidden, and startup logs a warning.

THE DASHBOARD AND THE SETTINGS PAGE REACH THE LOGIN THROUGH A 401.  Every 'fetch' on both pages goes through one wrapper that reloads the page on 401, and the reload lands on 'login.html', which sends the browser back to the path it was on.  User Settings is a third page, 'account.html' at '/account'.
