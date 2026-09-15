# procrustes - operating ruleset for this repository

## 1.  What this file is

THIS FILE IS THE SOURCE OF TRUTH FOR THIS REPOSITORY.  It is the operating ruleset for the container:  what it must do, what it must never do, and why each rule exists.

It is scoped deliberately.  The full media library standards, covering the workstation scripts, the NAS layout, library-wide audits and the music library, live in a separate CLAUDE.md alongside those tools.  Where a rule here is derived from that document it is restated in full rather than cross-referenced, because this repository has to be readable on its own.

## 2.  Hard boundaries

THE CONTAINER NEVER WRITES TO A MEDIA LIBRARY.  Libraries are mounted read only at the container boundary, which is a stronger guarantee than any check in Python.  The guards in 'app/paths.py' stay anyway as defence in depth.

PROMOTION INTO THE LIBRARIES IS MANUAL, ALWAYS.  The pipeline ends at 'complete/'.  No code in this repository moves a finished title into a library, and none should be added.

NOTHING THAT MATTERS IS EVER DELETED.  A failure holds.  A rejected file goes to quarantine.  A completed source is retired to quarantine.  Nothing in a library, nothing incoming and nothing in 'complete/' is ever removed, and no code doing so should be added.  Two exceptions.  The encode area:  intermediates there are removed once their successor exists, and a job directory is wiped after its title retires.  And an empty folder under 'import/':  when a source moves out to quarantine, 'paths.prune_empty_folders' removes its parent folders upward while each is empty, stopping at 'import/' itself or at the first folder with anything left in it.  Added 2026-09-12 after six library folders were dropped in whole and every one left its shell behind once its file retired.  The climb passes the write guard at every step, so it can never reach a library, and it removes directories only, never a file.

A PROVIDER ID IS NEVER GUESSED.  If it cannot be resolved, the title holds until an operator forces it.  Guessed numeric IDs have historically returned a Russian district, a Polish village, a bank and an unrelated film, each confidently formatted and entirely wrong.  A forced unidentified title carries no provider ID at all:  it keeps the name it arrived with, its tag block holds TITLE only, and it lands flat in 'complete/' rather than in a provider-named folder, so it cannot be mistaken for finished work.

## 3.  Repository layout

README.md carries the module tree.  Two things it does not say:  'app/static/' is vanilla JS with no framework, and 'app/data/' holds FileBot's release-group and media-source lists, CC0, vendored with SOURCES.

'app/' is a single Python package with no third-party dependencies.  The standard library is sufficient and the HTTP client is hand-rolled on urllib.  KEEP IT THAT WAY.  No requests, no npm, no framework, no CDN.  A dependency-free image is trivially auditable and never breaks on a transitive upgrade.

One process holds everything:  the assessment workers, one pool of threads per encoder plus one for passthrough, the import watcher, the library auditor and the web UI.  That is deliberate.  The UI needs live orchestrator state, and every encode is a subprocess call, so the GIL is not a constraint.

## 4.  Mount contract

ONE REQUIRED MOUNT.  Host paths are a deployment detail and appear only in the reference compose file;  nothing in the image knows what they are.  README.md carries the mount table.

FOUR DIRECTORIES ARE DERIVED FROM THE ROOT AND ARE NOT CONFIGURABLE:

```
<root>/import
<root>/complete
<root>/complete/.quarantine
<root>/hold
```

THAT IS DELIBERATE AND IT IS LOAD BEARING.  A title moves between those four, and a move between two paths under one mount is a rename:  instant, no data copied, whatever the file size.  Split them onto separate mounts and every one of those moves becomes a copy at best and fails outright at worst, as section 21 records.

ONLY ENCODE AND CONFIG ARE OVERRIDABLE, because they are the two that benefit from faster storage and neither is a rename target.  Left unset they are subdirectories of the root and everything is one mount.  Pointed elsewhere, the crossing is paid once at publish, which is a copy either way when the encode area is on a different pool.

THE PIPELINE IS IMPORT, ENCODE, COMPLETE.  'hold/' and 'config/' are not stages;  they are where a title goes when it cannot proceed, and where the machinery keeps its state.

```
import/       drop zone, the only watched entry point
encode/       the entire per-title work area, one directory per running job
complete/     TERMINAL, collected by hand
complete/.quarantine/   retired sources and rejected incoming files
hold/         needs a decision
config/       state.db, the instance lock, provider cache, cached posters, logs
```

QUARANTINE LIVES UNDER 'complete/', not on its own mount.  It is a dotted directory so it sits beside finished work without being mistaken for it.  Nothing in the container ever scans 'complete/';  the orchestrator only joins paths to write into it, so a dotted sibling costs nothing.

A MISSING ROOT IS A STARTUP ERROR, not a directory quietly created in the wrong place.  'MEDIA_ENCODE' and 'MEDIA_CONFIG' are validated only when they are set explicitly;  unset, 'Layout.ensure' creates them under the root.  'config/' has to be persistent wherever it lands:  it holds the SQLite store, so losing it means re-processing everything, and it holds the flock that section 19's single-instance guarantee depends on, which only works if two containers can see the same file.

THE ENTRYPOINT TAKES OWNERSHIP OF THE WRITABLE MOUNT POINTS BEFORE DROPPING PRIVILEGES.  Docker creates a missing '-v' host directory as root:root, so the process running as PUID:PGID cannot create anything under it;  measured 2026-09-13 on a from-scratch deployment, 'Layout.ensure' failed on 'config/logs' with 'Permission denied' and the restart policy looped the container.  The entrypoint now compares the owner of 'MEDIA_ROOT', 'MEDIA_ENCODE' and 'MEDIA_CONFIG' against PUID:PGID and chowns the mount point itself, never recursively:  everything below is created by the process as that identity, a recursive pass over the root would walk the whole share on every start, and the libraries and '/certs' are read-only mounts on which a chown fails.  The bind mount puts the change on the host directory, so it lands once per fresh directory and is inert afterwards.

DEGRADE, DO NOT FAIL, APPLIES TO THE LIBRARIES ONLY.  Without a library mount the incumbent comparison is skipped and every title is treated as new.  Logged at startup and shown in the UI, because silently skipping a gate is worse than not having one.

THE WHOLE PER-TITLE WORK AREA LIVES ON THE ENCODE MOUNT, NOT JUST THE ENCODE ITSELF.  An earlier design used fast storage only for encoder output, which optimised the one stage that does not need it:  an x265 encode is CPU bound, and the expensive I/O is on either side, each pass rewriting the whole file:

```
mp4 to mkv or avi to mkv remux     full read + full write
language strip                     full read + full write
encode                             full read + full write
packet count verification          full read of the source and of the output
statistics and tag writes          header rewrites plus a full re-read on re-add
```

Three or four complete passes per title, so copy in once, do everything on the encode mount, move out once:  two crossings of the slow filesystem, and no staging directory on the array at all.

Startup compares 'os.stat().st_dev' of the encode mount against the complete mount and warns when they match, so a fast disk that silently landed on the same filesystem is visible rather than mysterious.

STAGING HAPPENS BEHIND THE ENCODER SLOT, SO COPIES IN THE ENCODE AREA ARE BOUNDED BY THE SLOT COUNTS.  A title is staged by the pool thread that will encode it, after it has been dequeued, so at most 'cpu_slots + gpu_slots + 1' job directories exist at once whatever 'max_jobs' says;  section 6 records the measurement behind the split.

Admission control requires roughly 'encode_headroom' times the source size free before a job starts, read at admission time so it self-adjusts as the pool changes.  A job that runs out of space mid encode wastes the whole encode, so if the pool is small, lower the slot counts rather than the multiplier.  There is no equivalent guard for memory, considered and declined:  the prediction needs a constant nobody has measured and frame area dominates it, so sizing CONTAINER_MEM is the operator's call and an OOM kill mid encode is an accepted failure mode.

## 5.  Configuration

TWO SURFACES, BY WHAT THEY DESCRIBE.  The environment is the deployment surface:  mounts, libraries, the certificate, the identity, the render node, the port, the lock timings, DRY_RUN, LOG_LEVEL and the audit intervals.  Every variable below is read once at startup, validated, and echoed into the log and onto '/api/status'.  No config file, no host assumptions.  Everything else, which is everything the pipeline decides with, is a setting:  stored in the 'settings' table in 'state.db', edited on the Application Settings page, read at the point of use, and echoed into the log at startup with a mark on every stored value.

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

Adding a variable means adding it to 'Config.as_dict()' as well, or it silently vanishes from the startup banner and the status endpoint, which is where anyone debugging looks first.  NOTHING ENFORCES THAT TABLE.  A variable added to the code and not to this table drifts silently until someone reads both.  TESTPLAN.md case T-01 compares the two by hand at startup.

ELEVEN VARIABLES ARE NOT READ FROM THE ENVIRONMENT AT ALL.  'config.REMOVED' names them:  OUTPUT_CODEC, CRF, TV_ENCODE_SD, MAX_JOBS, GPU_SLOTS, CPU_SLOTS, ENCODE_HEADROOM, ENCODE_THREADS, POLL_INTERVAL, MTIME_QUIET, GRAIN_THRESHOLD.  One present in the environment is listed in 'Config.ignored' and 'main' logs a warning naming it and the settings page;  its value is never consulted.  Nothing seeds a setting from the environment, so a docker run that still carries the line and a settings page that says otherwise cannot disagree about which one is in force.

### Settings

'app/settings.py' IS THE ONE REGISTRY.  'SETTINGS' declares every setting once:  key, group, label, one sentence of help, type (int, float, bool, str, choice, list, table), default, whether it is per kind, and its bounds, choices or pattern.  The default of a setting that replaced a module constant is that constant, so the module keeps its value for a direct call and the registry cannot drift from it.  README.md carries the table of settings with their defaults.

A PER-KIND SETTING HAS ONE VALUE FOR MOVIES AND ONE FOR TELEVISION, stored as 'key.movie' and 'key.tv'.  Everything under Encoding is per kind except 'sd_display_height', 'passthrough_codecs' and 'x265_dv_vbv_kbps', and so are the grain threshold and the standards floors.  'Settings.profile(kind)' resolves every setting for one kind into a flat dict, adds the derived 'threads_per_job', and is the object the orchestrator hands to 'encode', 'standards', 'compare', 'media', 'probe' and 'tags'.  Those functions take the figures as parameters and stay pure in their inputs;  none of them reads the settings object, so each remains testable on a dict.  'provider.Provider' and 'provider.Client' hold the settings object and read their figures per call, because their call chains are deep and the values are global.

A CHANGE APPLIES AT THE NEXT READ, AND A ROUTED TITLE KEEPS ITS PARAMETERS.  'Settings.update' validates every value in the batch, runs the cross-key rules ('poll_interval' below 'mtime_quiet', the two slot counts not both zero) against the merged result, and writes the whole batch in one transaction or nothing.  The watcher reads its intervals per poll, admission reads the headroom per job, every gate reads its figures when it runs.  '_route' snapshots 'encode.encoder_params(profile)' into the decision as 'params', stored in 'decision_json', and '_encode' builds the command from that snapshot;  so a title waiting on a pool encodes under the configuration it was routed with, whatever changed since, and Retry is what re-assesses it.  A decision without 'params' goes back to assessment through the same branch as a decision that is missing.  The sidecar 'encode.job' still wins per title.

THE POOLS RESIZE LIVE.  'orchestrator.Pools' carries a target per pool and a thread registry;  a worker runs while its index is below its pool's target and checks that between titles, so a shrink never interrupts a running encode and the surplus thread exits after its current title.  '_ensure_workers' starts a thread for every index below the target with no live thread;  'apply_settings' calls it when 'max_jobs', 'gpu_slots' or 'cpu_slots' changed.  'Pools.snapshot' carries the targets beside the live counts, so a shrink in progress is visible on the settings page.

THE ENDPOINTS.  'GET /api/settings' returns 'Settings.describe()' with the pool snapshot;  'POST /api/settings' takes '{"set": {key: value}}' and '{"reset": [keys]}', returns the same body with 'changed' on success, and 400 with '{"errors": {key: why}}' having written nothing.  Every change is logged with its old and new value.  The page is 'app/static/settings.html', served at '/settings', vanilla JS like the dashboard.

### Read outside Config

These are read at import time in the module named, are NOT validated, and do NOT appear in the banner or on '/api/status'.  They exist as seams for substituting a binary, not as configuration.

```
FFPROBE      ffprobe       probe.py
FFMPEG       ffmpeg        media.py
MKVMERGE     mkvmerge      probe.py, media.py
MKVPROPEDIT  mkvpropedit   media.py, tags.py
MKVEXTRACT   mkvextract    tags.py
VAINFO       vainfo        gpu.py
```

ONE VARIABLE EXISTS IN BOTH PLACES.  'RENDER_NODE' has a module-level fallback at 'gpu.py' and 'encode.py';  the Config value wins whenever a cfg is passed, and the module constant only serves a direct call that passes none.  This is deliberate and it is not drift, but a change has to be made in both places.

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

ASSESSMENT AND ENCODING ARE TWO HALVES ON SEPARATE THREADS.  'max_jobs' assessment workers take a title from DETECTED through ROUTED:  probe, screen, identify, compare and route, which chooses the encoder and stores the decision with the encoder parameters it was routed under.  The title is then queued for one of three pools by that decision, 'cpu' with 'cpu_slots' threads, 'gpu' with 'gpu_slots', or 'passthrough' with one, and the pool thread does everything from staging to cleanup.  Measured 2026-09-10 before the split:  81 titles at DETECTED, one encoding, two staged and blocked on the single CPU slot, held queue empty, because a title was assessed only when an encode completed;  after it the same batch is assessed within minutes, every gate failure is held before the first encode finishes, and a passthrough title publishes while an encode runs.  Resume places a title by its stage:  anything up to COMPARED goes back to assessment, ROUTED and later go to the pool its stored decision names, and a later stage with no stored decision is re-assessed.

THE THREE ASSESSMENT STAGES RUN THROUGH BEFORE ANYTHING HOLDS.  SCREENED, IDENTIFIED and COMPARED are read-only assessments over the probed container, so a failure in one records its reasons and the next still runs, and the title holds once with everything the three of them found.  A clear comparison loss is the exception and quarantines immediately, because that verdict is definitive.  The working stages are not run speculatively:  STAGED, REMUXED and ENCODING transform the file and cost hours and disk, so a title can still hold a second time at READY or VERIFIED with whatever those find after the work is done.

PUBLISHED IS THE END OF THE PIPELINE FOR A PERSON;  CLEANUP IS HOUSEKEEPING.  Everything after PUBLISHED operates on the pipeline's own working areas and touches nothing the operator collects, so a CLEANUP failure must never present a published title as failed.  Section 21's EXDEV incident did exactly that while CLEANUP was the terminus:  the move fallback fixed the cause, and PUBLISHED being terminal fixes the shape.  'state.COMPLETE' is the pair, and the dashboard reads it as one figure.

A TITLE IS IN THE PIPELINE UNTIL IT IS PROMOTED, AND THE WATCHER NEVER RECYCLES A ROW THAT IS.  The file in 'complete/' is still waiting on the manual move, and a held or failed title on a decision.  'state.in_pipeline' is that rule, true for every stage but QUARANTINED, and it has three readers:  the findings link in 'webui.annotate_finding', the copy refusal in 'orchestrator.import_finding', and the reappearance branch in 'orchestrator.scan'.  Measured 2026-09-12 on Barbarella:  CLEANUP counted as terminal, so the finding offered Import again, the second copy landed at the same 'import/' path, and 'scan' recycled the CLEANUP row for it, 'output_path' included;  the published file lost its row and the second run held on the destination collision.  Only a QUARANTINED row is reset for a fresh arrival;  a path whose row is published and not yet promoted is skipped with one log line, and Forget is what reopens it.

A ROW LEAVES READY TO PROMOTE WHEN ITS OUTPUT LEAVES 'complete/', AND LEAVES QUARANTINED WHEN ITS FILE LEAVES '.quarantine'.  A stopped row's stage says where its file is, and a row with no file has nothing to say.  Measured 2026-09-12:  twelve titles at CLEANUP, seven with no file anywhere, output promoted and quarantine emptied, still counted and tiled as ready to promote, because the only exit was Forget.  'orchestrator._close_collected' runs on every watcher poll:  a PUBLISHED or CLEANUP row whose 'output_path' is gone moves to QUARANTINED when its retired source is still there, since that is then where its only file is, and is forgotten when no file remains;  a QUARANTINED row whose file is gone, with nothing at its source or output path either, is forgotten.  The three-file test is 'state.files_present', the same one 'webui.annotate' reports as 'files_gone', so the sweep and the Forget button never disagree.  Nothing on disk is touched;  a copied output stays in 'complete/' with its row, since the pipeline cannot tell a copy from an unpromoted file.  Skipped under DRY_RUN, where no output is ever written.

PROGRESS IS COUNTED IN ITS OWN UNIT, NEVER A PERCENTAGE FROM A PROXY.  Measured 2026-09-12 on Star Wars Episode II:  the tile sat at 3.7 percent for two hours with a climbing ETA while the output grew 5 MB every 30 seconds and ffmpeg ran at 765 percent CPU.  Since ffmpeg 7.0 the progress 'out_time' is the lowest last-muxed timestamp across the output streams, and that output copied a forced subrip track with a handful of cues, so the figure parked at the last cue at 315 s and 'orchestrator._progress_handler' had derived everything from it.  It now reads 'frame=', the encoder's own count, against 'probe.total_frames', the video track's 'NUMBER_OF_FRAMES' or duration times frame rate, and the row carries 'frame', 'total_frames', 'fps' and 'eta_s';  the tile reads 'x / y frames' with the bar sized from the two, frames alone with no bar when the total is unknown.  The repair copy reports 'copied_bytes' against 'total_bytes' and the finding row reads 'x / y GB';  the audit already counts files.

THE TAG BLOCK IS WRITTEN TWICE, BEFORE AND AFTER THE ENCODE.  ffmpeg's Matroska demuxer renders a targeted tag into the global metadata dict as 'TARGETTYPE/NAME' and the muxer writes it back untargeted, so '-map_metadata 0' on the encode flattens a correct block into exactly the defect section 12 describes.  The pre-encode write and its readiness gate stay, because they are what stops a title before it costs encoder time.  The post-encode write is what makes the published file correct, and readiness runs a second time inside VERIFIED against the file that actually ships.  The carry-forward set is read from the pre-encode file:  'tags.carry_forward' skips any name containing a slash, so reading the flattened output would silently drop the scraped ACTOR, DIRECTOR, GENRE and SYNOPSIS keys.

TWO ORDERINGS ARE LOAD BEARING.

IDENTIFICATION HAPPENS EARLY, before any encode, so the canonical name is settled and a title that cannot be identified costs no encoder time.  The comparison runs before the encode for the same reason:  a clear loser is quarantined without an encode being spent on it.

THE LANGUAGE STRIP RUNS BEFORE THE ENCODE.  The encoder maps every audio track and would otherwise carry foreign audio into the final file.

'/media/import' is the only watched entry point.  Nothing else is mounted, so nothing enters the chain that was not deliberately placed there.

A file must be size-stable across two polls AND untouched for 'mtime_quiet' seconds before it is detected.  Files ending in '.part' and files beginning with a dot are ignored outright.

THOSE TWO CONDITIONS SET THE DETECTION WINDOW AND THEIR VALUES INTERACT.  'mtime_quiet' is the floor and the poll adds up to one interval on top of it, so 120 and 60 meant a finished copy stayed invisible for 120 to 180 seconds.  At 30 and 15 the window is 30 to 45.  Lowered 2026-09-09 because the original figures were sized for a stall that does not happen on a LAN copy;  the residual risk, a transfer paused longer than 'mtime_quiet', is bounded because a truncated file fails ffprobe or the minimum standards gate and holds.

'poll_interval' IS BELOW 'mtime_quiet', ENFORCED.  The size condition compares against a size recorded by a PREVIOUS poll, so the quiet window has to contain at least one poll for the pair to fire together.  Invert them and the first qualifying poll finds no previous size, returns false, and detection costs an extra interval.  'Settings.update' refuses a batch that would put the poll at or above the quiet window.

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

THE FLOORS ARE PER-KIND SETTINGS, AND THE FIGURES ABOVE ARE THEIR DEFAULTS.  'min_display_width', 'min_display_height' and 'min_runtime_min' on the Standards group, 0 meaning no floor, which is how television carries no resolution floor;  'letterbox_bars_px', 'pal_speedup_check' and 'keep_langs' are the global ones.  'standards.screen' takes them on a profile and its module constants are the defaults.

THE HEIGHT FLOOR IS 800, NOT 1080, AND THE WIDTH FLOOR IS WHAT REJECTS SD.  A 2.40:1 scope master is 1920x800 and a 2.35:1 is 1920x818;  neither has bars and neither is 1080 tall.  Measured 2026-09-08:  a correctly cropped Blade Runner 2049 and a Return of the Jedi at 1920x816 were both held as "below the 1920x1080 floor", so the floor as written rewarded the file that wasted a quarter of every frame on black.  The width floor of 1920 is what continues to reject 720p, NTSC and PAL DVD, all of which display far narrower.

The 40 minute movie floor exists because a 10 minute bonus featurette once qualified as a disc's main feature and produced two wrong rips.  The floor is the guard, and section 26 records the ARM setting that was the actual cause.

THE EXTRAS CHECK IS A VOCABULARY IN TWO STRENGTHS, KEYED ON POSITION.  'standards.EXTRAS_WORDS' (sample, trailer, featurette, deleted scenes, behind the scenes, making of, gag reel, bloopers, outtakes) flag anywhere in the file name as whole tokens, because none of them is a plausible title.  'EXTRAS_SEGMENT_WORDS' (proof, bonus, extra, extras, interview, short) are ordinary words, so they flag only as a trailing segment after the year or a hyphen ('Movie (2001) - Extras'), as the whole stem, or as the parent folder's name ('Extras/', 'Featurettes/');  an episode's title portion after the marker is exempt, so 'S01E02 - Proof' is an episode.  Re-implemented 2026-09-13 from the 'other' vocabulary in guessit's release grammar;  the reason names the token or folder that fired.

Failures go to 'hold/' with a written reason, never silently to quarantine.  The UI carries a per-title override that forces a title through anyway.

UNTIL 2026-09-13 A HOLD WAS A ROW STATE ONLY.  97 titles sat at HELD with 'hold/' empty and every file still in 'import/';  'layout.held' had no reader.  'orchestrator._hold' is now the one route into HELD, for gate failures and transient retries alike, and it moves the source under 'hold/' at its path relative to 'import/', so the folders the identity ladder reads travel with it, reserves the name through the same exclusive-create loop as quarantine, prunes the vacated 'import/' folders, and rewrites 'source_path' so every later decision follows the file.  A Retry or Force re-runs the title from 'hold/';  the watcher never scans it, and a title that publishes is retired from there to quarantine.  Under DRY_RUN nothing moves.

THE OVERRIDE CLEARS EVERY GATE IT CAN REACH, AND A HELD TITLE STATES EVERY REASON IT WAS HELD.  'overridden' was originally read only in '_screen', so Force through on a comparison hold requeued the title and '_compare' held it again on the same verdict;  Blade Runner 2049 did that six times in one session.  A title held at readiness or verification had no instrument at all, and because 'process()' raised on the first objection a title held for one reason could have three more behind it, discovered one requeue at a time.  Both are fixed by the rule section 8 applies to the comparison:  every gate evaluated and the verdicts collected.

The flag is now read at seven gates:

```
_screen     standards bypassed
_compare    comparison bypassed, including a clear LOSS
_identify   proceeds with no provider ID, section 2
_ready      proceeds, the readiness problems recorded in the stage history
_verify     proceeds, the verification problems recorded in the stage history
_publish    a destination collision publishes beside it under a unique name, never over it
_remux      the mp4-path duration drift is a recorded note, as the avi path already was
```

FOUR STOP POINTS STAY OUT OF REACH, AND THAT IS PHYSICAL RATHER THAN POLICY.  An unreadable probe, a non-zero ffmpeg or mkvmerge in the remux, a non-zero encoder, and an I/O failure at publish each mean no output file exists, so there is nothing to carry forward.  Those land in FAILED, and FAILED is exactly the set force cannot apply to:  the decision endpoint refuses 'override' on a FAILED title and the UI offers Retry instead of Force.  HELD is the judgement set, and every HELD title is forceable.

A held title's reasons are stored as a list on the row and rendered as a list in the detail dialog, one entry per gate that objected, with the one-line 'reason' kept as the joined summary for tiles and logs.  Every gate a forced title bypassed is written into its stage history at the point it was bypassed, so a forced file is recorded as unverified rather than silently unverified.

The consequence is deliberate:  a clear LOSS is forced through as well as an ambiguous verdict, and a file that failed truncation or packet-count checks can be forced to 'complete/'.  Nothing writes to a library and promotion stays manual, which is what keeps that safe.

The letterbox check is cheap-first:  only a frame whose display aspect is 16:9 or 4:3 can hide baked in bars, so a warning is raised on those and the expensive cropdetect runs later, on candidates only.  Bars under about 20 px are not worth acting on.

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

Every vote a win proceeds.  Every vote a loss goes to quarantine with no encode spent.  VOTES IN BOTH DIRECTIONS GO TO HELD with a side-by-side attribute table in the UI, and so does a pair on which no gate voted at all.

A held contradictory verdict has two exits:  the section 7 override, which bypasses this gate entirely, or Discard, which quarantines the arrival.  Nothing resolves it automatically, because there is no correct automatic answer to a pair that is better in one respect and worse in another.

FIRST-DIFFERENCE-WINS WAS THE BEHAVIOUR AND IT WAS NEVER WHAT THIS SECTION SAID.  'compare.compare' returned at the first differing gate, so "level or contradictory goes to held" only ever fired for level and a split verdict was settled silently by whichever gate sat earliest.  Found 2026-09-08 while adding gate 6:  Blade Runner 2049 arrived at 9.024 Mbps h264 against a 1.326 Mbps HEVC incumbent, losing gate 5 on bit depth and winning gate 6 roughly fourfold once weighted.  It now holds for review.

GATE 1 IS ASYMMETRIC AND IT IS THE ONLY SHORT CIRCUIT.  An incoming file that LACKS HDR or Dolby Vision the incumbent carries is an immediate loss and no further gate is evaluated, because losing it is never an upgrade.  An incoming file that GAINS it casts an ordinary win vote and can be contradicted into review.

Gate 7 is last on purpose and is a tiebreak rather than a vote.  Source pedigree is inferred from release naming, which is exactly the kind of signal this project distrusts everywhere else, so it is consulted ONLY when no measurable gate voted, and it is flagged as a weak signal when it decides.

Gates 2, 3 and 6 need both sides to be measurable.  When one side is missing the gate casts no vote and the skip is recorded in the notes rather than silently treated as a tie.  A deferred gate 2 casts no vote either.

THE COMPARISON CARRIES EVERY ATTRIBUTE THE PIPELINE MEASURES, NOT ONLY THE SIX IT GATES ON.  Anything measured and dropped is a defect rather than an omission.  'compare.MEASURED' is the list and it is what the UI renders;  a row that differs with no gate against it is marked as such, because that is the case where the pipeline saw something and had no rule for it.  This was written after a title won on audio channel count while the incumbent's real disqualifier, a 25 fps PAL speed-up, was invisible to all six gates.

AN INCONCLUSIVE HOLD NAMES THE INCUMBENT, AND SAYS WHEN ITS TITLE DIFFERS.  Measured 2026-09-12:  a TNG repair copy of '11001001' matched by title to TVDB's S01E15, the library numbers that season one behind TVDB because Encounter at Farpoint is one file, so the incumbent found at S01E15 was 'Too Short a Season', a different episode with an identical encode, and the hold read only "no gate produced a clear difference".  'orchestrator._ambiguous_detail' now puts the incumbent's file name on the hold reason, the COMPARED record and the log line, and says first when the incumbent's segment title differs from the incoming identity's title under the section 9 transform.  The verdict is unchanged;  a shifted season is still a decision for a person.

'picture_pixels' had been hardcoded to None since the gate was written, so gate 3 had never fired.  Cropdetect now runs at COMPARED on both sides, on letterbox candidates only, per section 7.

GATE 2 DEFERS TO GATE 3 WHENEVER EITHER SIDE CARRIES BAKED-IN BARS.  Display pixel count counts black bars as picture and gate 3 exists to discount them.  Measured 2026-09-08:  an incoming Blade Runner 2049 at a correctly cropped 1920x800 was quarantined against an incumbent stored 1920x1080 carrying 280 px of bars, an identical 1,536,000 px of real picture on both sides, with cropdetect's 280 px measured at COMPARED and discarded.  Gate 2 now records a defer note and gate 3 decides.  On that title the verdict stays a loss and moves to gate 5, bit depth 8 against 10, which is the true disqualifier.

THE DEFER CONDITION HAS NO AMBIGUOUS MIDDLE, BY CONSTRUCTION.  One setting, 'letterbox_bars_px', is the floor cropdetect reports a crop at and the limit the standards gate fails above, so 'letterbox_px' is only ever 0 or that figure and above;  'media.CROP_MIN_BARS_PX' and 'standards.LETTERBOX_MAX_BARS_PX' are its default on each side.

A 24-file cropdetect sample of the movie library, taken with the bit-depth-scaled limit from section 16, found the 10 to 19 px band empty and three titles at or above 20 px, two of them the same shape as the Blade Runner incumbent:  Suicide Squad at 1920x1080 with 280 px, and Guardians of the Galaxy Vol. 3 at 1920x1016 with 212 px.

### Video bitrate, gate 6

COMPARATIVE ONLY.  There is no minimum bitrate and none is to be added:  section 7 does not screen on it, and section 14's rule that size figures do not measure an encode is unchanged.

THE FIGURE IS THE VIDEO TRACK'S OWN BPS, ON BOTH SIDES.  'probe._video_bitrate' reads the 'BPS' or 'BPS-eng' stream tag first, falls back to NUMBER_OF_BYTES times 8 over the track's own DURATION, and finally to ffprobe's stream 'bit_rate', which Matroska frequently omits.  Verified 2026-09-08:  both sides of the Blade Runner pair carry a BPS tag, so the first rung answers.

Track against track, each over its own duration, keeps section 17's whole-file bitrate trap out of scope, and a whole-file figure would fold in audio and subtitle tracks, which differ between candidates for reasons that have nothing to do with picture.

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

STARTING POINTS, NOT SETTLED VALUES, in the sense section 14 uses for the AV1 parameters, and a setting on the Comparison group of the settings page.  No calibration batch has been run against this library.  The av1 figure derives from section 25's 25 to 30 percent applied to the hevc figure.  Anything unlisted is treated as 1.0.

Without the weighting the gate favours less efficient codecs, self-defeatingly:  re-importing a title whose h264 source still exists would rate that source above the HEVC this pipeline produced from it, and quarantine its own output.

'bitrate_tolerance' is 0.25 against 'pixel_tolerance' at 0.05, both settings, and it is also unmeasured.  Legitimate encodes of one title vary far more in bitrate than in pixel count, so the 5 percent figure would make almost every pair cast a vote.

NO BITS-PER-PIXEL NORMALISATION.  A differing resolution is gates 2 and 3's business, and a resolution vote that contradicts a bitrate vote lands in review under the tally.

A PAL SPEED-UP IS DETECTED FROM THE PAIR, NOT FROM ONE FILE.  Equal frame counts within one frame at different frame rates means one side is speed-adjusted and the slower rate is correct.  That works at any resolution.  The single-file check in 'standards' keys on stored height and could not see a 1080p file at 25 fps carrying a 23.976 master's frame count.

The container never touches the incumbent.  It is read, compared against, and left alone.

## 9.  Title authority and the filename transform

THE TAG IS THE PROVIDER'S TITLE, VERBATIM.  Character for character, including ':', '?', '"' and any other punctuation.  Nothing is ever stripped from a tag.

THE FILENAME IS THAT SAME TITLE MINUS ONLY UNSAFE CHARACTERS.  No re-wording, no abbreviation, no reordering, no truncation.  The transform is mechanical and strictly one way:  a filename can always be derived from a tag, a tag can never be derived from a filename.

THE UNSAFE SET is exactly these nine, plus control characters 0x00 to 0x1F:

```
/   \   :   *   ?   "   <   >   |
```

Two further rules apply to the filename component rather than to characters:  it must not end in a period or a space, and it must not be one of the reserved device names CON, PRN, AUX, NUL, COM1 to COM9, LPT1 to LPT9.

Why that set and no more:  XFS and Btrfs forbid only '/' and NUL, while NTFS and SMB forbid all nine.  The libraries are served over SMB, so SMB is the binding constraint.  That is why '?' is removed while '!' is kept:  one is reserved, the other is merely punctuation.

EVERYTHING ELSE IS SAFE AND IS RETAINED:

```
(  )  [  ]  {  }  ,  .  '  -  !  &  #  %  $  ~  +  =  @  ^
and all accented and non-ASCII letters
```

Parentheses and square brackets are structural:  '(Year)' appears in every movie name and '[tvdbid-N]', '[tmdbid-N]', '[imdbid-ttN]' carry provider IDs.  A rule stripping them would contradict every folder in the library.

This keep list is not a preference.  Across 2934 library title portions there are 239 commas, 219 periods, 202 apostrophes, 82 hyphens, 26 exclamation marks and 21 ampersands.  A rule of "strip punctuation from filenames" would condemn about 570 correct files.

REMOVING A COLON.  A colon is REMOVED.  There is exactly one form and it is not a per-title choice.  'Avengers: Endgame' is filed as 'Avengers Endgame'.  This applies identically to movies and television.  Adopted 2026-09-06, superseding an earlier rule that accepted both ' - ' and removal.  The dash form is now non-conforming.

An em dash or en dash becomes ' - '.  A forward slash inside a title becomes '-'.

DIRECTION OF THE TRANSFORM:

```
tag          Futurama: Bender's Game
folder       Futurama Bender's Game (2008) [tmdbid-13253] [imdbid-tt1054486]
file name    Futurama Bender's Game (2008).mkv
```

Three rules follow:

- Never derive a tag from a folder or file name.  The information needed is already discarded.  The section 11 ladder obeys this by construction, since an id read from a folder leads to a provider entity and the entity supplies the title;  Star Wars IV, 2026-09-11, is the incident.
- Never assert that a tag equals a filename.  Apply the transform to the tag, then compare results.
- Where a tag and a filename differ by unsafe characters alone, that is expected and is not a defect.  The colon specifically must differ by REMOVAL, not by ' - '.

'app/titles.py' implements this and is removal-only by construction.  It has no permissive mode, because the library migration to the removal form is complete.

## 10.  Naming

### Movies

```
Title (Year) [tmdbid-N] [imdbid-ttN]/Title (Year).mkv
```

Editions, and ONLY editions, repeat the exact folder name plus ' - Label', in the form README shows, because Jellyfin does not support Plex's {edition-Name} syntax.  Applying the long form to ordinary single-version films once produced 21 wrong filenames.  Do not generalise it.

AN EDITION IS READ FROM THE ARRIVAL NAME, NEVER INFERRED FROM THE FILE.  Until 2026-09-13 nothing read one:  '_publish' always built the plain name, so an arriving 'Extended' cut would have landed beside the theatrical file under a collision suffix and been compared against the wrong cut.  'titles.EDITIONS' is the vocabulary, re-implemented from guessit's edition list plus the library's own 'Assembly Cut' and 'Final Cut':  Director's Cut, Director's Definitive Cut, Extended, Theatrical, Unrated, Uncut, Uncensored, Remastered, Restored, Criterion, IMAX, Collector, Limited, Deluxe, Ultimate, Special Edition, Alternative Cut, Fan Edit, Festival.  'titles.edition_from_name' looks only after the last year in the name, or at the end of the stem when the name carries no year, so 'The Extended Family (2010)' is a title and 'Alien 3 (1992) Assembly Cut' is an edition.  'provider.identify_movie' reads it from the file name, the parent folder and a repair copy's origin name, the cleaners drop the matched text before the search, and the identity carries 'edition'.  '_publish' then builds the long form;  the MOVIE tag block is unchanged, since the edition is a filename convention.  Runtime is never consulted:  a cut is what its name says, and a wrong name is a wrong file, not a wrong inference.

### Television

```
Show Name (Year) [tvdbid-N] [tmdbid-N]/Season NN/Show Name - SNNENN - Episode Title.mkv

Standard episode        Show - S03E07 - Recluse.mkv
Multi-part episode      Show - S03E02 - Quarantine, Part 1.mkv
Two episodes, one file  Show - S05E01-E02 - Kidnapping.mkv
```

Multi-part episodes use ', Part 1' rather than the provider's marker.  TVDB uses three marker forms and all three must be handled:  '(1)', '(Part 1)' and '(Part One)'.  All three appeared in a single batch, and a converter handling only '(1)' silently leaves the other two intact.  The conversion applies to the Matroska EPISODE title and the segment Info title as well as the filename.

A file covering two episodes takes the range form and drops the part marker entirely.  Naming it as only the first makes Jellyfin report the second as missing.  Its EPISODE PART_NUMBER is the first episode of the range.

## 11.  Provider IDs and episode matching

Movies use tmdbid and imdbid.  Television uses tvdbid and tmdbid, because TVDB governs episode titles and numbering.

Resolution goes through Wikidata, then verification:  'wbsearchentities', then 'Special:EntityData/<QID>.json', reading P4947 for a film's TMDB id, P4983 for a series' TMDB id, P345 for IMDb, P4835 for TVDB and P577 for release date.  THE TWO TMDB PROPERTIES ARE NOT INTERCHANGEABLE AND NEITHER IS A TVDB ID.  Until 2026-09-11 the code read only P4947 for TMDB and fell back to P4983 as a TVDB id, so every show resolved with 'tmdb' None and wrote '[tmdbid-None]' into its folder, Murder, She Wrote and Star Trek among them.  'ids_from_entity' now takes the kind and reads the matching property, and TVDB comes from P4835 alone.  Requests are spaced about 3 seconds apart.  CHECK THE HTTP STATUS:  a 429 body fails JSON parsing and looks identical to "not found".

Cross-check before writing an ID.  Fetch the TMDB page and confirm it describes the entity.  Wikidata provider IDs can be flat wrong:  P4983 for one show held the TMDB movie id of an unrelated 1984 Italian comedy.

THE PAGE IS CONFIRMED BY ITS OWN TITLE AND YEAR, AGAINST THE ENTITY'S LABEL AND ALIASES.  Until 2026-09-11 the check was whether the rung's string appeared anywhere in the page body.  Star Wars VII held on every rung:  its tag TITLE was the filename form 'Star Wars Episode VII The Force Awakens', TMDB titles the film 'Star Wars: The Force Awakens', and the Wikidata label 'Star Wars: Episode VII – The Force Awakens' is not on the page either.  TMDB spells titles its own way;  id 11 is plain 'Star Wars' there.  '_page_confirms' now reads the page's '<title>', 'Name (YYYY)' on TMDB, 'Name (TV Series YYYY)' for a show, 'Name (YYYY)' or bare on TVDB, and scores that name against the entity's label and every alias under the section 9 scoring, accepting at 'title_cutoff';  the label-in-body test remains as the fallback.  A page year more than one off the entity's rejects regardless, which is what catches a wrong id pointing at a sibling:  an Episode V entity against page 11 scores 0.9 on containment and is rejected on 1977 against 1980.  The TMDB HTML carries no IMDb or TVDB cross-links, checked, so the title and year are all there is.

Searching a bare franchise name returns the franchise entity rather than the film.  Search 'Title (YYYY film)'.

### The search is matched under section 9's own rules

'wbsearchentities' IS A PREFIX MATCH OVER LABELS AND ALIASES, AND IT STOPS AT PUNCTUATION.  A filename has already had the colon removed and the en dash turned into ' - ', so searching it for an entity whose only labels carry the colon finds nothing.  Measured 2026-09-11 with the pipeline's User-Agent:  'Star Wars Episode IV A New Hope' and 'Star Wars Episode IV A New Hope (1977 film)' both return HTTP 200 with zero hits, while 'Star Wars Episode V The Empire Strikes Back' resolves only because Q181795 happens to carry a colon-free alias.  So whether a film resolved from its filename depended on which aliases a volunteer had entered.

'provider._resolve_by_search', the name path for both kinds, does three things, in order:

- The two prefix searches as before, then a FULL-TEXT FALLBACK:  'action=query&list=search' is CirrusSearch, tolerant of punctuation, and it ranks the film first for both colon titles measured.  The hits' labels and aliases come back in one 'wbgetentities' call, so the fallback costs two requests rather than one per hit.
- EVERY CANDIDATE IS SCORED AGAINST THE TITLE THE WAY SECTION 9 SAYS TO COMPARE:  apply 'titles.to_filename' to the label and compare with the name, then 'normalise_for_match' on both sides, then containment as whole words, then difflib.  Full-text hits must clear 'title_cutoff', the one setting the episode matcher and the search share, 0.82 by default;  prefix hits are ordered by score but not cut, because Wikidata already matched the whole string.  Verified 2026-09-11 on A New Hope, Bender's Game, Return of the Jedi and Blade Runner 2049;  'Return of the Jedi' scores 1.0 on the Episode VI label through the containment rule.
- A CANDIDATE WHOSE RELEASE YEAR IS MORE THAN A YEAR FROM THE NAME'S IS SKIPPED with a log line.  'RoboCop' returns six entities labelled 'RoboCop';  the 1987 film sat ahead of the 2014 one and the old first-verified-wins would have taken it.

The TMDB page verification still gates every acceptance;  the scoring makes the search stricter, not looser.

A TIE AT THE TOP SCORE IS A HOLD, NOT A PICK.  Measured 2026-09-13:  'Space Battleship Yamato.mp4', no year, no tags, no folder, resolved to the 1977 anime film because Q3693349 preceded Q1191847, the 2010 live-action film, in the Wikidata response;  both carry the exact label, scored 1.0 and verified on their TMDB pages, and 'first that verifies' took the first.  '_resolve_from' on a name search now walks every candidate at the top score and collects each one that verifies complete;  two or more come back as an identity carrying 'tied', each candidate's outcome reads 'tied at 1.00 with Q…', and the orchestrator holds with both named and the missing year stated.  The candidate lists and the operator selector then apply as for any identification hold.  A year in the name settles it before the tie is measured, so '(2010)' resolves without a hold;  the id rungs keep first-that-verifies, since every candidate there carries the same id.  Cost:  one extra page fetch per tied candidate.

A YEAR INSIDE A TITLE IS NOT THE RELEASE YEAR.  'Blade Runner 2049 (2017)' carries two year-shaped numbers and the first one is part of the name.  Take the LAST match, not the first, and do not let the pattern consume its trailing delimiter:  in 'Blade.Runner.2049.2017.1080p' the dot after 2049 is also the dot before 2017, so a consuming pattern finds only one match and last equals first.  Both forms resolve correctly with a lookahead.  Titles that are only a year, 1917 and 2012, are unaffected, because the pattern needs a leading delimiter and there is none at position zero.

### The identity ladder

A FILENAME IS THE LAST RESORT, NOT THE FIRST.  A file that has been through this pipeline, or that came back out of a library, already states what it is.  'provider.movie_candidates' and 'provider.show_candidates' build an ordered list of rungs and 'provider._identify_from' walks it, the same walk for both kinds;  the first rung that yields a verified identity wins.

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

Rung 6 is skipped for a file sitting directly in 'import/', because the parent is then the mount itself and 'import' is not a film.

A RUNG SUPPLIES IDS AND A NAME;  THE PROVIDER SUPPLIES THE IDENTITY.  A rung carrying an id fetches the Wikidata entities that carry that id statement, 'haswbstatement:P4947=11' through the same full-text search the name fallback uses, then reads the entity:  label, year, and the other ids.  A rung with no id searches by name as before.  Either way every candidate goes through one acceptance per kind, '_accept_movie_hit' or '_accept_show_hit':  ids from the entity, the year check on a name search, the TMDB or TVDB page confirmed against the entity's label and aliases, and the LABEL as the title.  For an id rung the entity must carry the rung's id or it is a different title and is skipped;  two entities on one id, TVDB 73545 is both Battlestar Galactica and its miniseries, are ordered by the section 9 score against the rung's name and the first that verifies wins.  Nothing from the disk enters the identity except the ids that led to it.  An id Wikidata does not know falls to the next rung with a log line;  it never falls back to the disk text.

WHY THE DISK TEXT IS NEVER THE TITLE.  Until 2026-09-11 every id rung used the on-disk text as the identity's title and took the rung as final however incomplete it was.  Four shapes, all measured that day on repair copies:

- Star Wars IV had no tag block, fell to 'origin folder ids', and wrote the folder text 'Star Wars Episode IV A New Hope' into the MOVIE tag as TITLE:  a filename wearing a tag, which the audit could never catch because folder, file and tag agreed.
- Three Murder, She Wrote copies written by the P4947 defect above resolved from the embedded block with 'tmdb=None' while the folder beside them said '[tmdbid-484]'.
- Every show resolved from an embedded block carried 'show_year None', because the COLLECTION block has no year, and published into 'Brooklyn Nine-Nine (None) [tvdbid-269586] [tmdbid-48891]'.
- Star Wars VII held on every rung because its tag TITLE was the filename form, per the page confirmation above.

Measured 2026-09-11 with the pipeline's User-Agent:  'haswbstatement:P4947=11' returns Q17738, 'haswbstatement:P345=tt0076759' the same, 'haswbstatement:P4835=78049' Q833322, 'haswbstatement:P4835=73545' two entities.  One request per rung, cached like every other 200.  All four shapes resolve:  IV to 'Star Wars: Episode IV – A New Hope' from its origin folder, VII to its label from its tag, Murder, She Wrote to tmdb 484 and 1984 from a TVDB-only block, Battlestar to the series over the miniseries.

Consequences:  every identification touches Wikidata, rung 1 included;  a tag TITLE that differs from the label is rewritten to the label on the way through;  a movie whose folder carries ids Wikidata does not know falls to the name search rather than short-circuiting on the folder text.

AN INCOMPLETE IDENTITY HOLDS, IT DOES NOT PUBLISH.  A movie needs title, year, TMDB and IMDB;  a show needs show, year, TVDB and TMDB.  Neither acceptance rejects an entity for lacking an id, because that would let a lower-scored entity, a different show, win the search;  the identity comes back with a 'missing' list instead and the orchestrator holds with the field named:  'resolved tvdb 78049 (Murder, She Wrote) but tmdb could not be determined from the Wikidata entity; an ID is never guessed'.  Force takes it through unidentified per section 2.  Behind that, 'titles.movie_folder', 'movie_filename', 'show_folder' and 'episode_filename' raise 'TitleError' on a None, so '(None)' and '[tmdbid-None]' can never be formatted into a name again.

THAT RULE HELD FOR P4983 ONLY UNTIL 2026-09-13, AND THE OTHER HALF LET A REMAKE WIN.  '_accept_show_hit' rejected an entity with no P4835 outright, and '_accept_movie_hit' did the same for a missing P4947 or P345.  Measured that day on 77 Star Blazers episodes:  Q16741126 'Star Blazers', the 1979 US series, carries P580 1979, P4983 7768 and P345 tt0078692 but no P4835, and scored 1.0;  Q4292 'Space Battleship Yamato' scored 1.0 through an alias and failed verification on a dead P4835 85230;  Q2068751 'Star Blazers 2199', the 2012 remake, scored 0.9 by containment, carried a TVDB id, and was accepted;  only the remake's missing date, holding every episode on 'show_year', stood between the 77 files and 'Star Blazers 2199 (YYYY) [tvdbid-259675]'.  Both acceptances now return the incomplete identity, and '_resolve_from' and '_resolve_by_search' carry a score with every hit:  a complete hit displaces an incomplete one at the same score, never at a lower one.

A MISSING TVDB ID IS LOOKED UP THROUGH THE IMDb ID, AND CONFIRMED BY THE ROUND TRIP.  No Wikidata entity carries TVDB 77773, the 'star-blazers' series, titled '宇宙戦艦ヤマト', original country Japan, 77 episodes over three seasons.  'https://thetvdb.com/api/GetSeriesByRemoteID.php?imdbid=tt0078692' answers with no key:  'seriesid 77773', 'SeriesName Star Blazers', 'IMDB_ID tt0078692'.  'provider.tvdb_from_imdb' takes that id only when the dereferenced series page carries the same IMDb id back and confirms the entity's names, and the identity records the route as 'tvdb_from';  the id was read from a provider and the provider's page links the id it was found by, which is a lookup rather than a guess.  The endpoint is TVDB's legacy v1 API and is unsupported, and there is no keyless TVDB name search:  'GetSeries.php?seriesname=' returns an empty document and the HTML search page is script-driven.  Should the endpoint stop answering, the show holds incomplete for tvdb and the operator selection below is the route.

A TVDB PAGE IS NAMED BY ITS ENGLISH TRANSLATION, NOT ITS '<title>'.  The series page's '<title>' is the original-language name, '宇宙戦艦ヤマト' on 77773, which scores nothing against any English label;  the page also carries a 'change_translation_text' element per language, and 'data-language="eng"' on 77773 reads 'Star Blazers'.  '_page_identity' reads that element first and falls back to '<title>'.  Before this the page confirmed only through the body-substring fallback, which passed because the slug 'star-blazers' appears in the page's own links.

THE EPISODE CATALOGUE IS READ IN ENGLISH FOR A NON-ENGLISH-ORIGINAL SERIES.  The 'allseasons' listing serves original-language titles under every header, cookie and URL parameter tried ('ヤマト あの太陽を撃て!' for S03E25), while each episode page carries the same 'eng' translation element ('Star Force, Shoot That Sun!').  'episodes_for_order' reads the series page's 'Original Language' field, a cache hit since verification fetched the page, and when it is not English fetches each episode page named in the listing for its English title, keeping the listing's title where the page has none.  One request per episode, about four minutes for 77 episodes, once, then cached;  the other orders share the same episode pages.  Without this every episode of such a show fell to source numbering with the filename as its title.

A SHOW'S TMDB PAGE MAY DATE IT BY ITS ORIGINAL, AND THAT IS ACCEPTED IN ONE DIRECTION.  TMDB titles 7768 'Star Blazers (TV Series 1974)':  the exact title, first aired five years before Wikidata's 1979, because TMDB dates a dubbed adaptation by the original's air date.  An adaptation cannot air before its original, so in this class the page year is always EARLIER than the entity's;  a wrong id pointing at a same-titled remake runs the other way.  '_page_confirms' therefore accepts a show's own P4983 id when the page title scores 1.0 against the label and the page year is earlier than the entity's, by any amount, and writes the discrepancy into the IDENTIFIED detail;  later by more than one rejects as before, and a name-search hit is unaffected.  The folder year stays Wikidata's.  Residual risk, accepted:  a wrong P4983 pointing at a same-titled EARLIER show passes, and in that case the folder name, the TVDB id and the catalogue are still right and only the '[tmdbid-N]' component is wrong.

A stale or hand-edited tag cannot inject a wrong ID:  the id leads to an entity, and the entity's page has to confirm it.  A rung that fails verification falls through to the next rung rather than failing the title.

The rung that produced an identity is recorded in the stage detail, so a wrong match can be traced to its source instead of guessed at.

Measured 2026-09-07:  a fresh ARM rip carries no tags, no segment title and no folder ids, so only rungs 5 and 6 apply to it;  the ladder improves re-imports and library-shaped files and does nothing for a disc rip.

RUNG 3a EXISTS BECAUSE THE AUDIT COPY LANDS FLAT.  Section 27 copies a library file directly into 'import/', so its provider-id folder is left behind and rung 3 sees the watched root.  Measured 2026-09-11 on the library copy of A New Hope:  nothing for rungs 1 to 4, and the filename search failed on the colon.  The row stores 'origin_path', so the ladder reads the ids out of its parent folder.

'verify_tvdb' dereferences the id to its series page;  a 404 from the dereferrer is "not confirmed" and a network failure is transient.  A TMDB id on a show entity that its page does not confirm is dropped rather than rejecting the entity, because TVDB governs television;  the identity then holds as incomplete.

### Operator selection, one selector per source

THE BACKSTOP FOR EVERY CASE THE RUNGS CANNOT SETTLE.  When identification holds, the row carries the candidates from each source and the detail dialog shows one list per source, so the operator confirms every component of the identity rather than one entity whose claims are then trusted.  For a show the sources are the Wikidata entity, which supplies the title and year, the TVDB series, which supplies the tvdb id and the catalogue, and the TMDB series;  for a movie the entity, the TMDB movie and IMDb.

WHAT EACH SOURCE CAN ENUMERATE WITHOUT A KEY, MEASURED 2026-09-13.  Wikidata:  every entity '_resolve_from' scored, with its label, year, ids, score and the outcome in the words of the log line ('accepted, lacks tvdb', 'tvdb 85230 did not confirm the show', 'started in 1974, not 1979', 'below the 0.82 cutoff').  TVDB:  every P4835 on those entities plus the remote-id lookup for every entity carrying P345, each dereferenced to its series page for the English name and the IMDb and TMDB cross-links the page carries.  TMDB:  'https://www.themoviedb.org/search/tv?query=<name>' and '/search/movie?query=<name>' are server-rendered, each hit an anchor carrying the id, the title and a date;  six hits for 'Star Blazers', 7768 first.  Those are merged with every P4983 or P4947 on the entities.  IMDb:  every P345 on the entities;  there is no keyless search.  Every source has a free-text id field for the case where no listed candidate is right.  Lists are capped at 'candidate_limit', eight per source by default.

THE LISTS ARE BUILT WHEN THE HOLD IS WRITTEN, on the assessment worker that produced it, through 'provider.hold_candidates', and stored on the row as 'candidates_json' together with the rung and name that were searched.  The cost is bounded:  one TMDB search, one series page per distinct TVDB candidate, under a minute.  A provider that cannot be reached leaves the lists incomplete with the error recorded, never fails the hold.

THE PIPELINE'S OWN CHOICE IS PRESELECTED WHERE IT HAD ONE.  The highest-scored accepted entity is checked, and the ids it carries or that the IMDb route produced are checked in the other lists;  changing the entity re-derives them.  Rejected rows show why.  'Identify' is enabled once the entity and every id source have a selection or a typed id.  A checkbox applies the decision to every held title of the same kind whose search name matches, so a season is one decision.

THE DECISION IS 'action: "identify"' ON THE DECISION ENDPOINT, with 'qid', the id fields for the kind, an optional 'year', and 'apply_to' of 'title' or 'same-search'.  The choice is stored on each row as 'pinned_json', the history records it, and the row requeues to DETECTED.  A new top rung 'operator' fires on a pinned row:  the entity is fetched by qid and supplies the title and year, the ids come from the operator's selections, each page is still fetched and its confirmation written into the IDENTIFIED detail, but an operator-selected id is not rejected by the page check or the year check.  Every id in the result was read from a provider or chosen by a person from a list of provider-confirmed ids, and the record says which;  Force still means unidentified.

TVDB'S 'allseasons' PAGE OMITS SEASON 0.  Measured 2026-09-11 on Murder, She Wrote:  264 episode labels on 'allseasons/official' and not one 'S00', while '/seasons/official/0' lists six specials in a plain table.  So no special had ever been in the catalogue, every special fell back to source numbering with the filename as its title, and section 17's season 0 note was hiding it.  'episodes_for_order' now reads the season 0 page as well when the order carries no specials.  A special TVDB does not list still falls back, with the warning, which is the correct outcome for a crossover episode numbered by hand.

### Finding the incumbent in the library

A MOVIE INCUMBENT IS THE SAME CUT, OR THERE IS NONE.  'orchestrator._movie_file' picks the file inside a matched folder by the identity's edition:  the long-form name for an edition, the plain name for a plain arrival.  A folder holding only the other kind returns no incumbent with the reason written into the COMPARED detail ('holds no 'Extended' edition, only …; a different cut is not compared'), and the title is treated as new.  Two cuts differ in runtime and bitrate for reasons no gate models, so they are never compared.

THE LOOKUP KEYS ON THE PROVIDER ID, NOT ON THE TITLE.  Section 10 puts '[tmdbid-N]', '[imdbid-ttN]' and '[tvdbid-N]' into every library folder name, so an ID match is exact and survives any drift between a stored folder name and what the current transform emits.  The transformed-name prefix match stays as a fallback, and the route that matched is recorded in the stage detail so a name-only match is visible rather than assumed.

Measured 2026-09-08:  Return of the Jedi resolved to 'Star Wars: Episode VI – Return of the Jedi', rendered 'Star Wars Episode VI - Return of the Jedi' by the section 9 transform, while the library folder, which predates the transform, is 'Star Wars Episode VI Return of the Jedi (1983) [tmdbid-1892] [imdbid-tt0086190]';  the prefix match failed, the title took a full encode slot compared against nothing, and both sides carried tmdbid 1892.

A MISS AND A GENUINELY NEW TITLE MUST NOT LOG THE SAME SENTENCE.  'no incumbent, treated as new' read identically whether the library held nothing or the lookup had failed, which is the shape section 21 warns about when selection rests on a single fallible query.  The COMPARED detail now separates four outcomes:  no library mounted, a folder count scanned with nothing matched, a match by provider ID, and a match by folder name.

### Scraped text carries HTML entities

DECODE THEM, AND DO IT AFTER STRIPPING TAGS.  'episodes_for_order' stripped tags out of the TVDB page and never decoded entities, so the catalogue carried the transport form:  'Let&#039;s Give the Boy a Hand', 'S&iacute; Se Puede', 'In the Shadow of Z&#039;ha&#039;dum'.  Measured 2026-09-09:  2 of 110 Babylon 5 episodes and 5 of 96 Dexter episodes.

THE QUIET FAILURE IS THE DANGEROUS ONE.  'titles.normalise_for_match' expands '&' to ' and ' before stripping punctuation, so '&#039;' becomes ' and 039 '.  A title with TWO entities falls to 0.758, misses the 0.82 cutoff, and drops to source numbering with a warning you can see.  A title with ONE still scores 0.86, matches, and writes the corrupt string into the store, the Matroska EPISODE tag, the segment Info title and the filename.  Dexter S01E04 did exactly that and was caught only because it was a self-comparison.  Section 9 says the tag is the provider's title verbatim;  verbatim means the decoded title, not its transport encoding.

'html.unescape' from the standard library, applied AFTER the tag strip.  Reversed, an escaped '&lt;i&gt;' becomes a real tag and the tag strip eats literal text.  Verified 2026-09-09:  both failing titles went from 0.758 and 0.86 to an exact 1.000 match, and zero entities remain across 206 episodes.

THE SCOPE IS THAT ONE CALL SITE, CHECKED:  it is the only place a title string is extracted from scraped HTML.  'verify_tmdb' does a substring test, the poster patterns extract URLs, and Wikidata is JSON.

### Cover art

THE POSTER COMES OFF THE PAGE 'verify_tmdb' ALREADY FETCHES.  Every identification requests 'https://www.themoviedb.org/movie/<id>' to confirm the title, and that page carries the poster in its 'og:image' meta tag.  'provider.tmdb_poster' re-requests the same URL, which is a cache hit, and reads the first 'og:image' out of the body.  The SECOND one is the landscape backdrop, not the poster.

NO TMDB API KEY, DELIBERATELY.  A key would add an environment variable and a secret to reach an image already present in a response the pipeline holds.  The library offers nothing to reuse:  checked 2026-09-08, every movie folder holds the .mkv alone, because Jellyfin keeps artwork in its own metadata cache.

TWO TRAPS IN SERVING IT, BOTH IN 'webui' NOW.

'ProviderClient.fetch' CANNOT CARRY IMAGE BYTES.  It ends in 'response.read().decode("utf-8", "replace")', which destroys a JPEG.  Posters use 'webui.fetch_poster_bytes', which is a separate binary path.

THE POSTER FETCH MUST NOT SHARE THE PROVIDER THROTTLE.  'fetch' calls '_wait' against one shared timestamp, so twenty uncached posters through it would serialise a dashboard load into a minute of blocking and queue image requests ahead of identification.  Posters come from an image CDN, not from the hosts the spacing is polite to.

TELEVISION FALLS BACK TO TVDB, BECAUSE A SHOW OFTEN RESOLVES WITHOUT A TMDB ID.  Measured 2026-09-09:  a Babylon 5 season resolved tvdb 70726 with tmdb null on all 22 episodes, so none had artwork.  'provider.tvdb_poster' fetches 'https://thetvdb.com/series/<slug>' from 'series_slug' and takes the first URL under a '/banners/posters/' path segment;  the page also carries fanart, backgrounds, graphical and person art, and person art would put an actor's headshot on the tile.

THE TVDB PAGE HAS NO 'og:image' AND NO PRIMARY MARKER.  Babylon 5's carries 13 unique posters, so "first" means first in document order, correct on that show and unverified on any other.  Accepted, because a wrong poster is cosmetic and touches no pipeline decision.

MEMOISE THE RESULT PER SHOW, INCLUDING THE FAILURES.  'Client.final_url' is not cached and calls '_wait', and '_poster_url' runs once per title, so without the memo a 22 episode season resolves the same slug 22 times and adds over a minute of throttle.  'tvdb_poster' caches misses as well as hits and catches broadly:  'final_url' lets urllib's HTTPError escape unwrapped, so a narrow except would skip the memo write and retry a failed lookup on every episode.

Posters are cached under 'config/cache/posters' keyed by a hash of the URL, and served from '/api/poster/<hash>' under that same key with a long cache header.  THE URL IS THE HASH, NOT THE TITLE ID, AND THAT IS WHAT THE CACHE HEADER DEPENDS ON.  Measured 2026-09-11:  the endpoint was '/api/poster/<title_id>' with 'max-age=604800, immutable', the store was wiped for a build, the ids were reissued from 1, and the dashboard showed Blade Runner 2049 on A New Hope from the browser's cache;  a hard reload did not clear it, because the tiles are inserted by 'refresh()' after the reload.  'immutable' is only honest on a URL whose content can never change;  a row id is not that after a store wipe, the hash of the artwork URL is, and it is the key the on-disk cache already used.  The row carries it as 'poster' beside 'poster_url'.  The browser never contacts TMDB, so section 3's no-CDN rule holds and the dashboard renders on a LAN with no internet once a poster is cached.  A fetch failure serves 404 and the UI falls back to a text tile;  artwork is never allowed to be a failure the pipeline notices.

### Choosing the release year

P577 IS NOT A SINGLE VALUE.  A film routinely carries several release claims, one per country or event, and Wikidata returns them in no meaningful order.  Taking the first is wrong.

Q1259032, Futurama: Into the Wild Green Yonder, as returned:

```
+2008-01-01  precision 9   rank normal   no country
+2009-02-24  precision 11  rank normal   United States
+2009-03-20  precision 11  rank normal   Germany
```

The first is a year-only placeholder and the film is a 2009 release;  taking claim zero wrote 2008 into the folder and the file name.  Blade Runner carries the same shape.

'provider._best_date' selects instead:  drop deprecated rank, prefer preferred rank when present, then the highest precision, then the earliest date.  Verified against four entities.

Lookups are cached on disk under 'config/cache', and the cache is consulted before any request is made.  EVERY HTTP 200 IS CACHED WITHOUT EXPIRY, AND AN EMPTY SEARCH RESULT IS A 200.  So a title held for "could not be resolved" would re-run the ladder against the cached empty answers on every requeue and hold again identically.  The operator's Retry therefore marks the title for a cache-bypassing identification:  'Client.fresh' is a thread-local context the orchestrator enters around 'provider.identify' for a retried title, and the fresh answers overwrite the cache.  Force does not;  it takes the title through unidentified.  INTERNET ACCESS IS REQUIRED.  Identification is mandatory under section 2, so there is no offline mode:  a provider that cannot be reached raises ProviderError, the orchestrator treats it as transient, retries five times over roughly 62 minutes with exponential backoff, and holds with the reason written, which is the intended outcome.

### Episode order

AIRED ORDER, AUTOMATICALLY, FOR EVERY SHOW.  No per-show decision and no held gate.  This is a deliberate choice and it is an accepted risk, not a covered one:  a show whose disk content is genuinely in DVD order will be numbered as aired and nothing will stop it.  Where per-season counts disagree with aired order the show is still processed and the mismatch is surfaced as a warning.

### Matching

MATCH EPISODES BY TITLE, NEVER BY THE NUMBERING IN SOURCE FILENAMES.  Release groups renumber when they collapse a two-part episode into one file, and everything after silently shifts.  One Voyager season numbered Caretaker as a single s01e01, putting every following file one behind TVDB.  Match normalised episode titles against the provider list, then derive SNNENN from the match.

THE TITLE IS WHATEVER FOLLOWS THE EPISODE MARKER.  'episodes.title_from_filename' read only the library form 'Show - SxxEyy - Title', three parts on ' - ', and returned the whole stem for anything else, so an arrival named 'Show SxxEyy - Title' could never match by title and fell to source numbering with the stem as its title.  Measured 2026-09-13 on 'Star Blazers S03E25 - Star Force, Shoot that Sun!.avi'.  The three-part form is still read first;  otherwise the title is the text after the range or single episode marker with leading separators stripped, and the bare stem only when no marker is found.  All eight held names then matched their English catalogue titles, seven exactly and one fuzzy on a dropped 'The'.

Fuzzy fallback at a difflib cutoff of 0.82.  Release filenames carry typos:  one show alone had six ('No Sequitur', 'Persisitence of Vision', 'Dreadnaught', 'Darklin', 'Worse Case Scenario', 'Vis a Vis').  Fall back to source numbering only when exact and fuzzy both fail, and log every fallback.

WHEN NUMBERING DECIDES, THE TITLE IS STILL THE CATALOGUE'S.  Measured 2026-09-13 on twelve Star Trek: Enterprise episodes:  the release tag in the filename defeated the title match, numbering supplied the episode, and the identity title was the filename text, 'Terra Nova (1080p x265 10bit Joy)', which a forced publish would have written into the filename, the EPISODE tag and the segment title.  'provider.identify' now reads the catalogue entry for the parsed season and episode and takes its title;  the filename text is used only when the catalogue has no entry for that number, and the warning line says which.  'match_method' stays 'fallback-numbering' and the score 0.0, so the record still shows that the number came from the file.

THE RELEASE TAG IS STRIPPED BEFORE THE TITLE IS MATCHED, AND THE MATCHER HAS A CONTAINMENT RUNG.  The same Enterprise batch:  'title_from_filename' returned 'Terra Nova (1080p x265 10bit Joy)', difflib scored it 0.49 against 'Terra Nova', and 'Shockwave Part 2' carried its part marker without the parentheses 'PART_MARKERS' knew.  Four changes, all in 'episodes':

- 'titles.strip_release_tag' removes a trailing '(…)' or '[…]' group whose contents carry a release token or a release group name, repeatedly;  'Storm Front (2)' is untouched because '2' is neither.  'titles.strip_release_group' removes a '-GROUP' suffix at the end of a stem.  Both run inside 'title_from_filename'.
- 'PART_MARKERS' recognise the bare forms 'Part 2', 'Part Two', ', Part 2' and '- Part 2' at the end, and '_index' maps '(base, part)' so a marked probe lands on its own part rather than the first of the pair.  The unmarked-probe rule is unchanged.
- A containment rung between the exact rungs and difflib, rung 5 below:  anchoring at the start keeps a one-word title ('Home', 'Dawn') from hitting inside a release tag, and longest-wins keeps 'Babel One' from a hypothetical 'Babel'.  Before it, an exact rung on the probe with release tokens removed, so 'Show.S03E02.Homefront.1080p.WEB-DL.x264-Joy' is exact rather than contained.
- The segment title is a second probe:  'match_episode' takes 'extra' and 'provider.identify' passes the container's segment title.

THE RUNGS, IN ORDER, AS 'episodes._match_episode' RUNS THEM, every comparison on 'titles.normalise_for_match' of both sides.  The exact rungs run twice, first on that probe and then on the probe with release tokens removed and its part marker read again, because a dotted release name carries the marker inside the junk:  'Darkness.Rising.Part.3.1080p.BluRay.x264' is part 3 only once '1080p.BluRay.x264' is gone.

```
1  exact        the whole probe equals a catalogue title                          method exact, 1.0
2  own part     the probe's base plus its part number equals a marked entry       method exact, 1.0
3  base         the whole probe equals an entry's base, first of a marked pair    method exact, 1.0
4  stripped     an unmarked probe minus separators equals a title or a base       method exact, 1.0
   (1 to 4 again on the token-stripped probe)
5  containment  an entry's base starts the probe on whole words, longest wins     method contains, share of probe words
6  difflib      closest catalogue title at or above 0.82, unmarked probes only    method fuzzy, the ratio
```

A PROBE CARRYING A PART NUMBER THE CATALOGUE LACKS NEVER LANDS ON THE FIRST PART.  Measured 2026-09-13 on the first 0.9.1 retry:  'Transformers.Prime.S01E03.Darkness.Rising.Part.3.1080p.BluRay.x264-DEiMOS' identified as 'Darkness Rising, Part 1', S01E01, method 'contains' at 0.29, because the marker was still buried in the tokens when containment ran and the base map resolves an unmarked probe to the first part.  Now a marked probe whose '(base, part)' is absent skips rung 3, skips containment's base fallback and skips difflib, which would otherwise pair 'part 4' with 'part 1' at 0.95, and falls to numbering with the catalogue title for that number.

The file name is tried first through all six, then the segment title when the file name found nothing;  a miss on both falls to source numbering with the catalogue title for that number.  Verified 2026-09-13 on the eight Enterprise shapes, 'Home' against 'Homefront', a dotted release name, a typo and the numeric title;  'Dawning of Joy' against 'Dawn' did not match.

THE RELEASE VOCABULARY IS FILEBOT'S DATA, VENDORED.  'app/data/release-groups.txt' (5,460 group names plus two regex lines) and 'app/data/media-sources.txt' (seventeen source-type patterns) are byte-for-byte copies from 'github.com/filebot/data', CC0-1.0, commit and date in 'app/data/SOURCES', refreshed by hand and never fetched at runtime.  'titles' loads them once at import:  every line compiles under Python's 're' except the WEB-DL source row, whose variable-width look-behind is replaced by a fixed-width equivalent in code ('titles.WEB_DL_FIXED'), and a row that fails to compile is logged and skipped rather than failing the import.  'RELEASE_TOKENS' is the hand list joined with the source patterns;  'RELEASE_GROUPS' is consulted only by position, a '-GROUP' suffix or a token inside a trailing bracket group, because the corpus holds ordinary words ('JOY', 'WAR', 'LIFE') that must never be removed from the middle of a title.  'extended', 'uncut' and 'remastered' left the token list, since section 10's edition vocabulary claims them.

EPISODE FORMS, FROM GUESSIT'S GRAMMAR.  Range separators '~', 'to' and 'and' join '-', '+' and '&', still tight against the separator for a symbol and unambiguous as a word.  'MAX_RANGE_SPAN' is 3:  a range wider than that is read as its first episode with a warning, so 'E01-E99' cannot claim a season.  'Part 3 of 6' and '3 of 6' parse as episode 3 when the parent folder is 'Season NN' ('episodes.season_from_folder', 'parse_path'), as does a leading bare number ('Season 02/07 - Title.mkv').  Air-date names are not read:  the catalogue carries no air dates to match them against.

THE OPENSUBTITLES HASH IS RECORDED, NOT LOOKED UP.  'probe.opensubtitles_hash' is the 64-bit sum of the first and last 64 KiB plus the size, standard library only, verified against an independent implementation;  it lands on the container summary as 'oshash' and in the detail dialog.  The lookup needs an OpenSubtitles API key and a registered user agent, the shape the cover-art paragraph declined, and is a separate decision.  The hash costs one 128 KiB read per probe.

A title carrying no part marker that matches a marked pair resolves to the FIRST episode of the pair, not an arbitrary one.  Measured 2026-09-07:  'Caretaker' tied against 'Caretaker (1)' and 'Caretaker (2)' and fuzzy matching picked part 2.  Range extension then relies on this.

Episode ranges appear in source filenames as 'e17-18', 'e01-2', 'e16-18', 'e44+45', 'E01E02' and 'e15&16'.  A regex handling one form silently reports episodes missing that are present.

A BARE SECOND NUMBER IS A RANGE END ONLY WHEN IT SITS AGAINST ITS SEPARATOR.  Every form above has the bare number tight against '-', '+' or '&';  ' - <title>' is the same characters with whitespace around them, and a title that starts with digits then reads as a range end.  Measured 2026-09-12 on the library:  'S01E14 - 11001001' parsed as 14 to 110, and every numeric-titled episode was a false 'file name' finding, since the audit carries the parsed range into the expected name.  'episodes.RANGE_PATTERNS' now takes a second number without its own 'e' only with no whitespace on either side of the separator;  with whitespace the second number needs the 'e'.  All six forms above still parse, and so do 'S01E01-E02' and 'e03 - e04'.

Only collapse into a range when the source really is one file covering two episodes.  Assign every file to a distinct episode first, then extend a file to a range only if the following episode is still unclaimed and shares the same base title.

## 12.  Matroska metadata

Three separate title fields exist and are easily confused.

SEGMENT INFO TITLE, set with 'mkvpropedit FILE --edit info --set title='.  The plain title, no year, real punctuation preserved.

GLOBAL TAGS TITLE, set with 'mkvpropedit FILE --tags global:file.xml'.  Must agree with the segment title.  ffprobe reports this one in preference when present.

TRACK NAME, set with 'mkvpropedit FILE --edit track:v1 --set name='.  For tracks the property is 'name', NOT 'title'.  '--delete title' on a track is a parse error and mkvpropedit aborts the entire command, so every other edit in that invocation silently fails too.

### Movie tag shape

A single MOVIE-targeted Tag carrying exactly TITLE, TMDB, IMDB and DATE_RELEASED.  TITLE equals the segment Info title.  mkvpropedit omits '<TargetTypeValue>50</TargetTypeValue>', so VERIFY BY TargetType NAME, NEVER BY GREPPING FOR THE NUMBER.

Some files carry a large untargeted block of scraped metadata instead:  ACTOR, DIRECTOR, GENRE, PRODUCER, SYNOPSIS, LAW_RATING and so on.  THESE ARE LEGITIMATE AND MUST BE CARRIED FORWARD.  '--tags global:' replaces untargeted tags and would destroy every one of those fields.  When rewriting a movie tag, carry forward any untargeted key that is not part of the canonical set.

Only ENCODER, COMMENT, MAJOR_BRAND, MINOR_VERSION, COMPATIBLE_BRANDS, HANDLER_NAME, VENDOR_ID, CREATION_TIME and SOFTWARE are safe to drop.  Those are remux leftovers.

### Television tag hierarchy

Three levels in addition to the segment title.  Getting them inverted is an easy and previously made mistake.

```
Target 70, COLLECTION   TITLE = show name, plus TVDB and TMDB ids
Target 60, SEASON       TITLE = 'Season N', PART_NUMBER = N
Target 50, EPISODE      TITLE = episode title, PART_NUMBER = episode number
```

The segment Info title is the EPISODE title, matching the title portion of the filename.  For a range file the EPISODE PART_NUMBER is the first episode of the range.

### Flattened tag blocks

A subtler failure of the same hierarchy.  Every value lands inside ONE untargeted Tag whose Simple names carry the level as a slash path:  'COLLECTION/TITLE', 'SEASON/PART_NUMBER', 'EPISODE/TITLE'.  Jellyfin does not read that as a hierarchy.

It defeats every cheap check.  A grep for COLLECTION passes, all strings are present and correct, and an XML parse succeeds.  THE ONLY RELIABLE TEST IS STRUCTURAL:

- assert all expected TargetType names appear on their own Tag elements
- assert no Simple Name anywhere contains a slash

An audit of 2276 episodes found 329 affected across four shows, and 51 of 213 movies carried the same defect as 'MOVIE/TITLE'.  The data in a flattened block is normally correct and only the shape is wrong, so the repair is structural, not a re-derivation.

### HDR declarations

A Matroska file declares its HDR metadata twice, and the two can disagree.  The bitstream carries mastering display colour volume (ST 2086) and content light level (MaxCLL, MaxFALL) as SEI messages, which ffprobe shows as frame side data.  The container carries the same figures in the track's Colour and MasteringMetadata elements, which ffprobe shows as stream side data.  A player, and every tool that reads only '-show_streams', sees the container.

MEASURED 2026-09-10 ACROSS THE LIBRARY:  215 movies hold 8 HDR titles, all HEVC Main 10, PQ, bt2020.  Every one carries ST 2086 and CLL in the bitstream.  Three declare no ST 2086 in the container and four declare no CLL, and Saving Private Ryan declares nothing at all while its SEI carries everything.  ffmpeg's own x265 output from a source with no side data, a filter graph for instance, produces exactly that shape:  full SEI, empty container.

THE PROBE READS BOTH SURFACES.  'probe.probe' keeps the container's mastering display and content light level from the stream side data, and for any stream '_is_hdr' accepts it runs a second, narrow ffprobe over the first twelve frames for the SEI.  'hdr_declaration_gap' names every declaration the bitstream carries that the container lacks or states differently.  The bitstream is authoritative:  a container that disagrees with its own SEI is repaired to match, silently, and the change is recorded in the stage history.

A ZERO CONTENT LIGHT PAIR IS WRITTEN LIKE ANY OTHER, AND READ BACK THROUGH MKVMERGE.  Saving Private Ryan and Forrest Gump carry a content light SEI of MaxCLL 0, MaxFALL 0, the encoder's "not indicated".  The repair wrote the pair faithfully, and both titles then held at READY and again at VERIFIED with 'container lacks content_light', because libavformat surfaces a Matroska content light element only when both values are non-zero.  Measured 2026-09-11 on the workstation:  0/0 and 1000/0 written by mkvpropedit are invisible to ffprobe, 1000/400 is visible, and 'mkvmerge -J' reports all three as 'max_content_light' and 'max_frame_light'.  So for an HDR Matroska stream on which ffprobe reports no content light, 'probe.probe' reads those two properties from 'mkvmerge -J', one header read, and a present element is a declaration whatever its numbers.  Nothing keys on the value.  The two forced copies in 'complete/' from before the read carry the element and match their bitstream.

THE REPAIR IS A HEADER EDIT ON THE REMUX PATH.  'media.repair_hdr_declaration' runs beside 'fix_flags_and_language', through mkvpropedit, and writes the ST 2086 chromaticities and luminances and the two light levels from the bitstream figures.  mkvpropedit takes floats and spells the properties 'color-', not 'colour-';  ffprobe reports the same figures back as rationals, so 0.68 reads back as 11408507/16777216 and 0.0001 as 209800/2098000053.  A file with no mastering metadata on either surface is left alone;  HLG in particular is legitimate without it, and nothing is ever manufactured.  The DOVI configuration record is out of reach for a header edit, so an RPU in the bitstream with no container record is detected and held, never repaired.

NEITHER REMUX PATH LOSES THE DECLARATION AND NEITHER CREATES ONE.  Measured in the image:  mkvmerge as 'strip_foreign' uses it and 'ffmpeg -c copy' as the container conversion uses it both preserve the Colour element, and both leave an absent one absent.  The passthrough path is therefore the only place the repair is needed;  an encode heals the container by itself, because ffmpeg's HEVC decoder exports the SEI as side data and the muxer writes the container element from it.

THE GATE FAILS ON LOSS, NEVER ON ABSENCE.  'tags.check_hdr' runs inside 'readiness' at READY and again at VERIFIED against the file that ships, with the PROBED probe as its baseline.  A declaration the source carried on either surface that the output's container lacks is a hold, and so is a colour tag that changed.  A declaration the output gained is fine;  an encode legitimately declares more than an under-declaring source did.

### Statistics

Every file carries per-track BPS, DURATION, NUMBER_OF_FRAMES and NUMBER_OF_BYTES, written with '--add-track-statistics-tags'.

THESE ARE READ BACK, NOT ONLY WRITTEN.  Comparison gate 6 reads the video track's BPS, per section 8, so a file whose statistics were destroyed loses a comparison gate as well as its own accuracy.

USE '--tags global:' AND NEVER '--tags all:'.  The all: form replaces every tag in the file and destroys per-track statistics.

The global: form replaces only untargeted tags, so track-targeted statistics survive, BUT NOT UNIVERSALLY:  some files store them as global tags and those ARE destroyed.  One film lost every statistics tag to exactly this while its sibling survived the identical command in the same batch.  ALWAYS re-check the byte-sum ratio after writing global tags and re-run '--add-track-statistics-tags' where it comes back zero.  'tags.refresh_statistics' does this and returns the ratio.

A trailing '--add-track-statistics-tags' is REQUIRED after any container conversion, after mkvpropedit-only edits, and after an encode.

## 13.  Tracks:  flags and language policy

### Flags

Audio tracks:  EXACTLY ONE default, and it must be the primary track rather than a commentary.  Assert the count is exactly one rather than merely testing that some default exists.  Files with several audio tracks and no default let the player choose arbitrarily, which on one release meant landing on the director's commentary.  More than one default is the common scene-release failure:  one release flagged both its TrueHD Atmos and AC3 track as default.

Subtitle tracks:  no default, in any language.

Forced subtitle tracks are exempt and keep their default.  Key that exception off the actual forced_track property and NEVER off the track name, since names like 'SDH' and 'Force' are inconsistent scene conventions.

### Language

English only.  Non-English audio and subtitle tracks are dropped at ingest.

```
keep_langs = eng en und
```

THE LIST IS ONE SETTING WITH FOUR READERS.  'probe.probe' counts foreign tracks with it, 'standards.screen' requires an audio track in it, 'media.strip_foreign' drops what is not in it, and 'tags.readiness' fails a track outside it;  each takes 'keep_langs' as a parameter and the orchestrator and the auditor pass the same value, so a library file is judged by the list an arrival is.  The module constants are its default.

VIDEO TRACK LANGUAGE IS 'eng', not 'und'.  The one exception is cover art:  a V_MJPEG track is a poster, not video, and stays 'und'.

'und' is kept and always will be, because an untagged track must never be assumed foreign.

THE PURPOSE IS CONVENIENCE, NOT DISK SPACE.  A clean single-language track list means Jellyfin presents one audio option rather than a menu of thirty.  Space reclaimed is incidental and is NOT a measure of success.  Do not report bytes saved.  Report track outcomes:  foreign tracks gone, exactly one default audio, no subtitle default, nothing truncated.

TRAP:  an untagged foreign track survives, because untagged maps to 'und' and 'und' is kept.  A subtitle named 'Chinese (Cantonese)' with no language tag would be kept and the run would report success.

TRAP:  the policy covers audio and subtitles equally.  Selecting on foreign audio alone leaves every file whose audio is clean English but whose subtitles are not.  That mistake once left 269 files untouched after a run that reported 199 of 199 successful.  'media.strip_foreign' keys on track type and language, so both are treated identically by construction.

A LANGUAGE STRIP CAN MAKE A FILE LARGER.  ffmpeg '-c copy' writes zlib-compressed PGS subtitles back uncompressed, so surviving tracks expand.  One film grew 136 MB despite dropping six tracks.  A file that grew is NOT a failed remux and is not a finding.

## 14.  Encoding and the encoder router

### The router

Evaluated in order, first match wins.  Implemented in 'encode.select', which is a pure function of the probed attributes so it is fully testable without media.

```
1  source codec is hevc or av1          PASSTHROUGH
2  television and the source is SD      PASSTHROUGH
3  Dolby Vision RPU present             libx265    on any setting
4  output_codec av1 and grainy          libsvtav1  CPU
5  output_codec av1                     av1_qsv    GPU
6  grainy                               libx265 aq-mode=4:tune=grain
7  otherwise                            libx265 aq-mode=3
```

GATE ORDER IS LOAD BEARING AND BREAKS SILENTLY IF DISTURBED.  TESTPLAN.md cases T-31 to T-38 exercise one title per gate and read the resolved gate back from the API;  run them after touching this function.

Gate 1:  an already-AV1 file is never transcoded back to HEVC.

Gate 2:  an SD source is never re-encoded by default.  An SD source has little to gain and a generation of quality to lose;  the field handling below removes the interlace half of that loss, and the default is unchanged.  SD means display height below 'sd_display_height', 720, computed from width times SAR over height, so an anamorphic PAL DVD rip is classified on what it actually displays.  'encode_sd' re-enables it per kind;  for movies it is reachable only once the standards floor admits an SD source, since the floor rejects one first.

Gate 3:  an AV1 re-encode discards the Dolby Vision RPU, because AV1 Dolby Vision is profile 10 and effectively nothing plays it, so DV titles always take the x265 path and the x265 path can never be retired.  GATE 3 IS UNREACHABLE FOR REAL MATERIAL.  Every Dolby Vision profile is HEVC or AV1, so gate 1 passes every DV title through and is what protects the RPU;  gate 3 is the backstop for a DV source in a third codec, which does not exist, and 'build_command' refuses any encoder but libx265 for a DV title regardless, so a change to gate 1 cannot silently drop an RPU.

PASSTHROUGH MEANS NO VIDEO RE-ENCODE, NOT NO PROCESSING.  A passthrough title is still remuxed to Matroska, language stripped, flag corrected, tagged and given statistics.  An SD AVI rip arriving in 'complete/' still as an .avi is a bug.

### x265 parameters

Settled on a nine-film batch, 2026-08-27 and 2026-08-28.  THESE ARE THE DEFAULTS, AND THE DEFAULTS ARE NOT RETUNED IN CODE.  Every figure below is a per-kind setting on the Encoding group ('x265_preset', 'x265_crf', 'x265_aq_mode', 'x265_aq_mode_film', 'x265_tune_film', 'x265_psy_rd', 'x265_psy_rdoq', 'x265_deblock', 'x265_pix_fmt', and 'x265_extra_params' appended verbatim), so a deviation is made on the settings page, is marked as stored in the banner and on the page, and is carried in every routed title's decision.  The registry's defaults are the constants in 'encode.py'.

```
libx265, preset slow, crf 18, pix_fmt yuv420p10le
-x265-params <aq>:psy-rd=2.0:psy-rdoq=1.0:deblock=-1,-1
aq = aq-mode=4:tune=grain on film sources, aq-mode=3 otherwise
```

HDR SIGNALLING TRAVELS INSIDE THE PARAMS STRING TOO.  For an HDR source 'x265_hdr_params' appends 'colorprim', 'transfer', 'colormatrix', 'range=limited', 'hdr10=1', 'master-display' from the ST 2086 figures and 'max-cll' from the light levels, taking the bitstream figures first and the container's second.  Measured in the image on 2026-09-10, on ffmpeg 7.1.5 and again on 8.1.2:  the libx265 wrapper carries all of that through on its own, from either surface, so the params are a guard against a wrapper that stops doing so rather than the mechanism.  'hdr10-opt' is deliberately NOT passed:  it changes chroma QP offsets and is a tuning decision, not signalling.

DOLBY VISION THROUGH AN ENCODE IS THREE PARAMETERS, NOT A NEW BINARY.  Measured on a Barbarella segment:  '-dolbyvision auto', which is what an unmodified argv gets, silently drops the RPU with no error and no DOVI record in the output.  '-dolbyvision 1' without VBV fails with "Dolby Vision requires VBV settings to enable HRD".  '-dolbyvision 1' with 'vbv-maxrate' and 'vbv-bufsize' carries the RPU intact, container record and per-frame data both.  So for a DV title 'build_command' passes '-dolbyvision 1' and 'x265_dv_vbv_kbps', default 'X265_DV_VBV_KBPS', for both VBV figures, and the Dockerfile build gate asserts the wrapper has the option.

KNOWN ISSUE:  'x265_dv_vbv_kbps' IS AN UNMEASURED RATE CAP, AND IT IS THE ONLY CAP IN THE PIPELINE.  x265 will not encode a Dolby Vision profile without HRD, HRD needs VBV, so the DV path alone carries 'vbv-maxrate' and 'vbv-bufsize', both set from 'x265_dv_vbv_kbps', default 40000.  The number controls two things:

- **Rate control.**  Under VBV, x265 raises QP wherever the bitrate over the buffer window would exceed the cap, so quality is surrendered in exactly the highest-complexity scenes whenever the cap binds.  40000 was chosen to sit above the peaks a 1080p CRF 18 slow encode produces, an expectation rather than a measurement.
- **The signalled level and tier.**  x265 derives the HEVC level and tier from resolution, frame rate and the VBV maxrate.  Measured on the Barbarella segment:  the plain CRF 18 encode signals Level 4, Main tier;  the same encode with the VBV pair signals Level 4.1, High tier, because 40000 kbps exceeds Main tier's 20000 at that level.  High tier is a different compatibility surface, and some hardware decoders that accept Main tier at 4.1 do not accept High.  Every DV encode from this pipeline currently carries that tier.

'vbv-bufsize' equal to 'vbv-maxrate' is a one-second window.  A larger buffer averages peaks over a longer window and binds less often at the same maxrate;  it is a second knob and it is also unmeasured.

Unmeasured because gate 1 lets no DV title reach an encoder.  The value has been exercised once, on a ten-second 1920x816 segment, where it did not bind:  12.17 Mbps with the pair against 11.86 without, the difference being HRD's own decisions.  The first place the expectation would be tested is a UHD DV title after a change to gate 1.  Section 29 carries the settlement procedure;  until then the constant is a starting point in the sense the AV1 parameters below are.

THE THREADING FIGURE IS NOT PART OF THAT TUNING.  'pools=' is appended to the same '-x265-params' string, and 'lp=' to '-svtav1-params', from 'encode_threads' divided by 'cpu_slots', autodetected from the cgroup quota when the setting is 0.  The thread count differs between deployments, so a built command that carries 'pools=' has not been retuned.

USE tune=grain ON FILM SOURCES.  This is the single setting that separated the batch.  The two grain-tuned jobs produced the lowest and third-lowest bitrates at identical CRF.  The one grain-heavy 35mm source that ran plain 'aq-mode=3' produced the highest at 9,697k, nearly double another title on the same CRF and aq-mode.  Without the grain tune, x265 reads film grain as detail worth preserving and pays for it frame by frame.

JUDGING FROM RELEASE YEAR IS NOT RELIABLE.  One of the grain-heavy sources is a 2015 title shot on 35mm.  Check the source, not the date.

TEN GIGABYTES IS A GUIDE, NOT A LIMIT.  Nothing fails for going over;  treat it as a prompt to check whether the source is film and the grain tune was missed.  Raising CRF to pull a file under the number is the WRONG move:  the size is a preference, the quality target is not, and size figures are the wrong measure of whether a run succeeded.

### AV1 parameters

STARTING POINTS, NOT SETTLED VALUES.  The calibration batch has not been run.

```
av1_qsv     -preset veryslow -global_quality 26, p010le via hwupload
libsvtav1   -preset 4 -crf 24 -pix_fmt yuv420p10le -svtav1-params tune=0:film-grain=8
```

'tune=0' is the subjective mode; the default 'tune=1' targets PSNR and is wrong here.  'film-grain' is the AV1 answer to 'tune=grain':  grain is synthesised at decode rather than paid for frame by frame.  Its 'film-grain-denoise' companion is a calibration variable, not a settled value.

BEFORE FLIPPING THE AV1 DEFAULT, run a calibration batch and score against the SOURCE, not against the x265 table.  The reference films are already HEVC, so re-encoding them measures a second generation rather than the encoder.  Score with the 'ssim' filter, built into ffmpeg.  Bitrate alone settles nothing, since a smaller AV1 file is the expected outcome;  the question is size at equal SSIM.

### Automatic grain detection

The grain signal cannot come from a hand-written sidecar, because in an automatic chain nobody writes one and every film source would silently lose the largest measured lever in the project.

Measured instead:  a 20 second sample from the middle of the file, encoded twice at a fixed CRF, once clean and once through a light 'hqdn3d' denoise.  Grain is expensive to encode, so a grainy source shows a large size delta and a clean digital source shows almost none.  The ratio is logged for every title so a bad threshold is visible rather than silent.  'grain_threshold' tunes it, per kind;  the sample length, position and the probe's own CRF and preset are settings on the Probes group.

THE PROBE RUNS AT ROUTING, ON THE SOURCE, ONE AT A TIME.  Routing is on the assessment side so the title can be queued for the right pool, and the probe reads its 20 seconds from the source on the array.  Its scratch directory is 'encode/.probe/<title_id>', removed afterwards and swept at startup as ownerless if a crash leaves it.  Probes are serialised on a semaphore of one, so three assessment workers cannot take three cores from the running encoders;  the bound is roughly one core for roughly ten seconds per title.  A sidecar 'film=' skips it, as does DRY_RUN.

An 'encode.job' sidecar beside the source overrides the probe and wins:

```
film=1                  force the grain path, film=0 forces the clean path
codec=av1               per-title output codec
crf=17                  per-title quality target
crop=1920:804:0:138     skip cropdetect and use this
fields=telecine         skip the field probe;  progressive, interlaced or telecine
```

### Field handling

THE ENCODER IS NEVER HANDED FIELDS AS FRAMES.  Until 2026-09-13 the command mapped the video straight into the encoder, so a 480i source or a telecined DVD, reachable with 'encode_sd' on or from an interlaced HD source, would have been encoded with the combing and the repeated fields baked in.  Now every encoder-bound title is classified at routing and the command carries a filter when it needs one.

THE PROBE IS 'idet' OVER THE GRAIN SAMPLE.  'media.field_probe' decodes the same 20 s at 45 percent the grain probe uses, video only, through 'idet', and reads the multi-frame counts and the repeated-field counts from the summary:

```
telecine     repeated fields (top + bottom) at 10 percent or more of frames
interlaced   otherwise, TFF + BFF greater than progressive
progressive  otherwise
```

It runs in '_route' under the grain probe's semaphore, for non-passthrough decisions only, so a passthrough title pays nothing.  'ffprobe' 'field_order' is recorded on the video summary for the record and not consulted, because DVD headers routinely say 'progressive' over telecined content.  The decision carries 'fields', stored with it and rebuilt at ENCODING.  A sidecar 'fields=' wins over the probe.

Measured 2026-09-13:  Airplane! (1080p film), a Barney Miller DVD episode at 29.97 and Babylon 5 at 23.976 all classify progressive with zero interlaced frames;  a synthetic 3:2 pulldown classifies telecine at 240 repeated fields in 600;  a synthetic 60i classifies interlaced at 600 of 600.  ffmpeg's 'testsrc2' pattern misclassifies as interlaced on its moving edges and is not a valid fixture for the progressive case.

THE THREE CHAINS, AHEAD OF THE CROP AND AHEAD OF THE QSV UPLOAD:

```
telecine     fieldmatch,yadif=deint=interlaced,decimate    23.976 out of 29.97, one frame in five removed
interlaced   bwdif=mode=send_frame                         frame count kept
progressive  nothing
```

Fields are matched on the full stored frame, so the field filter precedes any crop;  on the QSV path the software filters run before 'format=p010le,hwupload'.

VERIFICATION KNOWS ABOUT THE FIFTH FRAME.  '_verify' compares video packet counts as an equality;  for a 'telecine' decision the expected count is four fifths of the source within one percent and the note reads 'telecine removed n of m frames'.  The duration check is unchanged, the running time is preserved.  'total_frames' for the progress bar is scaled the same way.

ACCEPTED LIMITATION:  a mixed episode classifies on one 20 s sample.  'fieldmatch' with 'yadif=deint=interlaced' behind it handles interlaced stretches inside a telecined episode, but a sample landing on a video-only stretch of a film-and-video show (DS9:  shot on film, finished on video, effects composited at 480i) classifies it interlaced and encodes every frame deinterlaced at 29.97.  The x265 parameters remain unmeasured at SD, and 'encode_sd' defaults to off.

### Two traps in the command shapes

MAPPING.  Explicit maps, never '-map 0'.  A bare '-map 0' hands a V_MJPEG cover-art track to the video encoder, which re-encodes a poster as video.  Cover-art mjpeg is legitimate and must be carried, not encoded.  Use '-map 0:v:0 -map 0:a -map 0:s? -map 0:t? -map_chapters 0'.

THE SDR STAMP IS A VALUE, NOT A FLAG.  An untagged SDR source is stamped smpte170m only when it is SD;  an untagged HD source is stamped bt709.  The original rule was written for untagged NTSC DVD rips and the code applied it at any resolution, which put a 601 matrix on a 1080p master and shifted every saturated colour.  SD is 'encode.is_sd', the same predicate router gate 2 uses, and it is defined once.

COLOUR FLAGS, AND THE FIX THAT MUST NOT BE GENERALISED.  On the x265 path, ffmpeg's '-color_primaries', '-color_trc' and '-colorspace' suppress what '-x265-params' sets, so only the matrix lands.  Those three flags must NOT be passed on the x265 path; colour goes inside '-x265-params' instead, with '-color_range tv' alongside.

THAT FIX IS SPECIFIC TO THE X265 PATH.  Neither av1_qsv nor libsvtav1 accepts colour properties through a params string, so on both AV1 paths those same three ffmpeg flags are the correct and only mechanism for the smpte170m stamping of untagged NTSC DVD rips.  Generalising the removal would silently drop the stamping.  TESTPLAN.md case T-58 checks the colour properties on the output file for a tagged source, an untagged NTSC rip and an HDR source, which is where this mistake would surface.

DO NOT APPLY SDR COLOUR ASSUMPTIONS TO HDR MATERIAL.  The smpte170m stamping is correct for untagged NTSC DVD rips and is WRONG for anything bt2020.

## 15.  Container remuxing

NEVER use 'ffmpeg -map 0' when remuxing MP4 to MKV.  Many HandBrake MP4 rips carry a 'bin_data' QuickTime chapter stream that Matroska rejects outright, failing the remux; 135 of one show's 242 files had one.  Use '-map 0:v -map 0:a -map 0:s? -map_chapters 0'.  Dropping bin_data is lossless because the chapters live in the MP4 chapter atom and ffmpeg carries them into Matroska natively.  ALWAYS ASSERT THE CHAPTER COUNT IS PRESERVED;  'media._mp4_to_mkv' fails the remux when it changes, and TESTPLAN.md case T-41 records both counts.

Old Xvid and DivX AVI rips often need '-fflags +genpts -bsf:v mpeg4_unpack_bframes'.  Allow about 2 seconds of duration tolerance, not 1:  genpts recomputes timestamps and routinely lands a second either side with every packet preserved.  Genuinely damaged sources drifted 14 to 21 seconds.

Compare packet counts to distinguish a damaged source from a bad remux.  Two files drifted 14 to 21 seconds while nb_read_packets was identical in and out; the AVI header over-claimed frames by exactly the drift at 25 fps.  The remux was faithful.

ffmpeg cannot infer a container from a temp extension like 'file.mkv.part' and fails at muxer init.  ALWAYS PASS '-f matroska' EXPLICITLY when writing to a temp name.  Without it the remux silently falls through to a fallback, so the intended code path never runs while per-file verification still passes.

mkvmerge, unlike ffmpeg, detects Matroska by content, so it works fine on a '.part' temp name.

mkvmerge silently makes a muxed-in sidecar subtitle the default track.  After any sidecar mux, clear the flag explicitly.

A REMUX INHERITS THE SOURCE CONTAINER'S TITLE.  ffmpeg copies the input's title metadata into the output, so a mux from a scene release arrives carrying a segment title like 'Grease.2.1982.PROPER.1080p.BluRay.H264.AAC-RARBG'.  Set the segment Info title explicitly after any remux rather than assuming the new file has none.

ffmpeg does NOT compute track statistics.  It carries forward whatever the source held.  A container conversion from a source holding no Matroska statistics writes NONE at all:  measured byte-sum ratio 0.0000 on both AVI to MKV and MP4 to MKV.

## 16.  Verification and quality signals

```
Encode completed        absence of .part files
Nothing truncated       compare VIDEO STREAM duration
Stream fidelity         compare packet counts in versus out
Corruption              full decode with 'ffmpeg -f null'
Statistics accuracy     sum NUMBER_OF_BYTES, compare against file size
Letterboxing            metadata first, cropdetect only on real candidates
```

TRAP:  COMPARE VIDEO STREAM DURATION, NOT CONTAINER DURATION.  After a language strip the container figure can drop by several minutes with nothing lost, because the longest stream was a subtitle track that got removed.

WHAT 'orchestrator._verify' ACTUALLY RUNS:  video stream duration, video packet count in against out, the statistics byte-sum ratio, and the readiness check, which carries the structural tag test and the HDR invariant from section 12.  The table above is the standard;  the full decode scan is deliberately NOT implemented, because it costs a complete read of the output per title and the packet count was judged to carry enough of the signal.  Measured 2026-09-08 on the first published title, an 89 minute encode preserved 128,424 video packets exactly, which is why the check is an equality rather than a tolerance.

TRAP:  a decode scan does NOT catch dropped audio.  Surviving packets are valid and there is simply a hole in the timeline.  The reference defect, 193 gaps and roughly 150 seconds of missing audio, passed a clean decode.

TRAP:  A CONTAINER-LEVEL PROBE IS NOT EVIDENCE OF ABSENCE.  '-show_streams' sees only the Colour element;  section 12 records the library files whose SEI carries what their element lacks, and the two-surface probe this forced.

'_verify' COLLECTS, IT DOES NOT RAISE ON THE FIRST OBJECTION.  Duration drift, the packet-count equality, the statistics floor and readiness are all evaluated and the title holds once with every problem named, or is forced past all of them together with each recorded in the stage history.

### Cropdetect

TRAP, AND IT SILENTLY INVALIDATES WHOLE AUDITS:  cropdetect's 'limit' is read in the source's NATIVE BIT DEPTH, not normalised to 8-bit.  The usual limit=24 is correct for yuv420p, but against a 10-bit source it means 24 of 1023, an 8-bit equivalent of about 6, below video black at 16, so bars are never detected and every 10-bit file reports full-frame.

```
limit = 24 * 2^(depth - 8)      so 96 for 10-bit, 384 for 12-bit
```

Read pix_fmt per file and set the limit from it.  This defect had already corrupted a full 2456 file audit, silently passing 596 high-bit-depth files as clean.  'probe.cropdetect_limit' implements the scaling; use it rather than a constant.

WHEN A DETECTOR AND A DECODED FRAME DISAGREE, BELIEVE THE FRAME.

SAMPLING, BLACK LEVEL AND PLAUSIBILITY, RE-IMPLEMENTED 2026-09-13 FROM TINYMEDIAMANAGER'S DETECTOR.  Three 40-frame samples with the tallest picture winning could not see an asymmetric misdetection, a dark sample that reported nothing, or a variable-aspect film.  'media.detect_crop' now:

- takes six 2-second samples evenly spaced between 2 and 92 percent of the usable duration, the spacing capped at 900 s, and advances 1.4 spacings past a rejected sample so a dark scene is not re-sampled, stopping after twelve attempts;
- measures the black level first, 'signalstats' YMIN over the first sample, and sets 'limit' to 1.5 times it, never below the depth-scaled floor above and never above 13 percent of the bit-depth range, logging all three;
- rejects a sample whose bars differ left against right by more than 1.5 percent of the width or top against bottom by more than 2 percent of the height, or whose crop keeps less than half the width or 60 percent of the height;
- takes the crop geometry seen in the most plausible samples as primary, and records a second geometry seen in at least 6 percent of samples whose display aspect differs by 0.15 or more as 'secondary' with its share.  The filter uses the primary;  'compare.MEASURED' carries the secondary as a 'variable aspect' row that no gate votes on.

Verified on a synthetic file:  a 1920x800 letterboxed section, a 1440x1080 pillarboxed section and an asymmetric section gave the primary 1920x800 in four of five plausible samples, the secondary 1440x1080 at 20 percent, and both asymmetric samples rejected as 'bars uneven top 60 bottom 220'.  The result is returned with no filter when the bars are under 20 px, and still carries 'secondary' when there is one.

Anamorphic storage defeats a pure geometry check.  A scope film can be stored 1920x1080 with a non-square sample aspect, giving full 1080 rows of picture and no bars.  Compute display aspect from width times SAR over height; never infer it from stored dimensions alone.

### Quality signals

ONE AUDIO TRACK LABELLED STEREO likely means a bonus feature rather than the main title.  Correct feature rips carry 2 to 5 audio tracks including a 5.1.

A SCOPE FILM STORED AT 1920x1080 means baked-in letterbox.  The real picture may be only 800 or 818 lines, with roughly a quarter of every frame encoded black.

A 4:3 FRAME WHOSE CONTENT FILLS IT is correct, an Academy-ratio or open-matte transfer.  A 4:3 FRAME WITH WIDE CONTENT INSIDE IT is a genuine defect, windowboxed on a modern display.

A LIBRARY ENTRY CAN BE A PAL DVD RIP WEARING AN HD LABEL.  One folder held 720x576 at 25 fps with SAR 64:45 where the DVD had 5.1 audio.  That runs 4 percent fast with audio pitched about a semitone sharp.  Check frame rate and stored dimensions before assuming a folder holds what its name implies.  This is why the PAL speed-up check is in the minimum standards.

## 17.  Known false positives

Do not report these as defects.

CLAIMED BYTES EXCEED FILE SIZE.  Caused by zlib-compressed PGS subtitle tracks:  mkvpropedit reports uncompressed logical bytes while Matroska stores them compressed.  Only a large overshoot, above roughly 1.5x, means genuinely stale tags.

VIDEO BPS EXCEEDS WHOLE-FILE BITRATE.  BPS is computed over the track's own duration, not the container's, so a video track shorter than its container inflates the figure.  Comparison gate 6 is not affected, since it reads video-track BPS on both sides.

A STREAM WITH NO STATISTICS TAGS.  Cover-art mjpeg streams are never tagged.

RUNTIME 8 MINUTES OVER THE PROVIDER'S.  Usually long end credits, or a legitimately different cut.

DUPLICATE NUMBER_OF_BYTES AND NUMBER_OF_BYTES-eng TAGS.  Caused by setting a language tag on a file that already had statistics tags.  Re-run '--add-track-statistics-tags' to normalise.

A DATE_RELEASED THAT IS A FULL DATE RATHER THAN A YEAR.  Scraped-metadata files carry ISO dates such as '1977-05-25' where the canonical shape carries '1977'.  The ISO form is richer, not wrong.

A COLLECTION TITLE EQUAL TO THE EPISODE TITLE.  Legitimate whenever a pilot or TV movie shares the series name.  Test the hierarchy structurally instead.

A TITLE THAT APPEARS TO DIFFER FROM ITS FILENAME.  If the title contains a comma, 'ffprobe -of csv=p=0' wraps the value in literal quotes, so a shell comparison fails on 23 episodes that were correct.  Use an XML parser or a JSON output format.

A TAG TITLE THAT DIFFERS FROM ITS FILENAME BY AN UNSAFE CHARACTER.  The tag keeps the provider's punctuation while the filename omits the nine unsafe characters.  Apply the transform to the tag before comparing.  Comparing mkvextract's raw output against a decoded filename also falsely flags 'Black, White & Brown', because '&amp;' is legitimate XML encoding.

SEASON 00 WITH GAPS IN ITS NUMBERING.  Specials are numbered per TVDB and a library normally holds only some.  Any gap check must exempt season 0.

A FILE THAT GREW AFTER A LANGUAGE STRIP.  See section 13.

## 18.  Dolby Vision and HDR

Files carrying Dolby Vision survive a '-c copy' remux intact, including the DOVI configuration record, dv_profile, dv_level, rpu_present_flag, bl_present_flag and any mastering display metadata.  Verified before and after a language strip on two Profile 8 Level 3 titles.

A DV title is NEVER AV1-encoded, and in practice is never encoded at all;  section 14 records gate 1 passing it through, gate 3 as the backstop, and the '-dolbyvision 1' mechanism should one reach the encoder.  No extraction tool is in the image and none is needed.

Detection is from the v:0 side data list, reading dv_profile and rpu_present_flag.  That is the CONTAINER's DOVI configuration record.  The probe also notes an RPU in the bitstream, and a file carrying one with no container record is held rather than repaired, because a header edit cannot write that record.  Plain HDR10 with no RPU is not Dolby Vision and encodes normally; AV1 carries HDR10 static metadata correctly.

## 19.  Single instance, locking and concurrency

ONE INSTANCE AT A TIME, ENFORCED.  The supervisor takes an exclusive 'flock' on 'MEDIA_CONFIG/procrustes.lock' before doing anything else.  A second instance pointed at the same mounts WAITS for the first to exit rather than running alongside it.

Why this is not optional:  every other guard in the process is 'threading.Semaphore' or 'threading.RLock', which are process local.  Two supervisors sharing the mounts would sweep each other's encode area and could publish the same title twice.

flock rather than a PID file, because the kernel releases it when the holder dies, so a hard kill needs no stale-lock reasoning.  The PID is written into the file for diagnostics only.

The wait is interruptible.  SIGNAL HANDLERS ARE INSTALLED BEFORE THE LOCK IS ATTEMPTED.  An earlier version registered them after the orchestrator was constructed, so a stop during startup had no handler and would have sat until SIGKILL.

ENCODE JOB DIRECTORIES RECORD THEIR OWNING PID.  The startup sweep reclaims only directories whose owner is no longer alive and leaves a live job alone.  An unconditional sweep would delete an in-flight encode's working directory.

ENCODES RUN UNDER A TRACKED SUBPROCESS AND ARE TERMINATED ON SHUTDOWN.  Without this the supervisor exits while ffmpeg keeps running, reparented to the init process and still writing into the encode area, where the next instance's sweep will find it, which made the two-instance problem reachable from an ordinary restart.

PUBLISHING IS ATOMIC.  'paths.publish_file' reserves the destination with 'O_CREAT | O_EXCL', copies into it, then replaces.  The previous check-then-act with 'os.path.exists' followed by 'shutil.move' raced between the three concurrent workers:  the same film dropped in twice under different release names resolves to one provider ID, so one folder and one filename, and the second move overwrote the first.  Quarantine naming uses the same exclusive-create loop.

### Pools

```
GPU encode threads   1     the A310 has one media engine, queueing more at it gains nothing
CPU encode threads   1     x265 preset slow and svt-av1 preset 4 both saturate the allotted cores
passthrough threads  1     fixed;  the I/O-only path, bounded by the disk rather than a core
```

Each pool has exactly as many threads as the device has slots and pulls from its own queue, so there is no semaphore to wait on and no device sits idle while a thread blocks on the other one.  'max_jobs' is the assessment pool and has nothing to do with encoding capacity.  All three resize live per section 5:  a raised target starts threads at once, a lowered one lets the surplus thread finish its title before it exits, and a running encode is never interrupted by a settings change.

TWO CPU ENCODERS GAIN LITTLE, AND ONLY AT LOW RESOLUTION.  'cpu_slots' at 2 runs two encodes at 'pools=4' each on the same eight cores.  x265's wavefront parallelism is bounded by CTU rows, about 11 at 720p and 17 at 1080p, so one eight-thread encode leaves threads idle at low resolution and two four-thread encodes recover some of it.  Expect a modest aggregate gain on SD and 720p, near zero at 1080p and above, doubled per-title latency and doubled staged copies.  The setting is legitimate for an SD-heavy batch;  it is not a lever on throughput generally, and the GPU is the only thing that adds capacity rather than dividing it.

The GPU is shared with whatever else uses it on the host;  at the HEVC default the pipeline does not touch it, so contention is a constraint on switching 'output_codec' to av1 rather than a problem now.

## 20.  Logging

EVERY MODULE LOGS WHAT IT DOES, and that is not shaped by testing.

Two tiers, switched by 'LOG_LEVEL':

- 'info', the default.  What happened.  One line per meaningful action naming the title, the stage and the outcome.  When a stage fails or degrades the line says why in plain terms.  No command lines, no arithmetic, no per-gate working, no chatter for steps that cannot fail.
- 'debug'.  Why.  Full command lines, per-gate comparisons, candidate scoring, measured figures against their thresholds, derived geometry, raw tool output.

'LOG_LEVEL' is a container variable, so raising verbosity is a deployment setting rather than a code change.

Logs go to stdout and to 'config/logs/procrustes.log', and the tail is served at '/api/logs'.

Every log line carries the title id where one exists, so a single title's path can be extracted from a run with several jobs in flight.

A POLL THAT FINDS NOTHING NEW IS NOT AN ACTION.  The watcher's 'already claims this path' line fires once per in-flight title per poll and accounted for 223 of 328 lines in a three-title run, while the 108 minute encode that completed inside that same run logged nothing at all.  It is a debug line.  ENCODED and VERIFIED each emit an info line carrying the same outcome text they already write into the stage history, because an encode finishing is the most meaningful event the pipeline has.

CONFIG RESOLUTION IS LOGGED BY 'main', NOT BY 'config'.  'Config' is constructed before logging is set up, so it records where each variable came from and 'main' prints that at debug once handlers exist.  Anything logging from inside 'Config' is discarded.

THE SETTINGS BANNER FOLLOWS THE CONFIG BANNER, AT INFO.  One 'setting' line per storage key with a '*' on a stored value, so a run's configuration is readable from its log alone;  every operator change logs the key with its old and new value, and 'pools retargeted' names the three targets after a slot change.

## 21.  Scripting rules and traps

NEVER GATE A JOB ON A COMMAND-LINE STRING MATCH.  Command lines are a shared, unowned namespace:  'pgrep -f "libx26[5]"' once matched an unrelated 'grep libx265' and logged "waiting for CPU" against a job that did not exist, and the bracket trick only stops the pattern matching its own shell.  Under containers a shared PID namespace sees every process on the host and a private one sees nothing and starts a second encode on top of a running one.  Gate on something the job OWNS, per section 19.

mkvpropedit REPORTS ITS ERRORS ON STDOUT, NOT STDERR.  A wrapper capturing only stderr gets an empty string and reports a blank failure; one ignoring the exit code as well reports success over work it never did.  Capture both streams and key pass or fail on the EXIT CODE.

MKVPROPEDIT TRACK SELECTORS ARE TYPE-RELATIVE; MKVMERGE TRACK IDS ARE GLOBAL.  In a video plus audio plus subtitle file, mkvmerge -J reports the first audio track as id 1, but its selector is 'a1', not 'a2'.  Deriving the selector by adding one to the global id fails loudly on a single-audio file and fails SILENTLY on a multi-audio file, where 'a2' is a real track and the edit lands on the wrong one.  Count the position among tracks of that type.  'probe.track_selectors' does this.

PARSE FFPROBE OUTPUT FROM STDOUT ALONE.  A helper returning stdout plus stderr broke on files emitting harmless decode warnings, so float() threw and files were falsely failed.  Worse, when the source parsed as None the comparison was skipped entirely rather than failing.

VERIFY TAG CONTENT, NOT JUST PRESENCE, AND ASSERT ON STRUCTURE.  A grep confirming a target existed would have passed all 380 files that had the hierarchy inverted, and section 12 records why an XML parse alone passes a flattened block.

IDEMPOTENCY GUARDS MUST KEY ON THE DESTINATION, NOT THE SOURCE.  Asking "is the source file gone?" defers forever on remuxed files, since a remux leaves its source in place.

DELETE OR RETIRE SOURCES ONLY AFTER THE OUTPUT PASSES VERIFICATION.

RE-DERIVE THE FILE LIST AT APPLY TIME rather than reusing one captured during an earlier scan.  Selection, action and verification must EACH derive their own list.

SELECTING THE WORK AND VERIFYING THE WORK MUST USE DIFFERENT CODE PATHS.  Reusing one query for both is how a run reports total success over an incomplete list.

'glob.glob' treats '[i_c]' in a real folder name as a character class and silently matches nothing.  USE os.walk for paths containing brackets, which every '[tvdbid-N]' folder does.

RENAME FAILS ACROSS MOUNT POINTS EVEN ON THE SAME DEVICE.  Linux refuses 'rename(2)' when the two paths are on different mounts, regardless of whether they share a filesystem.  Measured 2026-09-08 while retiring a source after a successful publish:

```
/media/import    dev=42
/media/complete  dev=42
os.replace(...)  OSError [Errno 18] Invalid cross-device link
```

Same device number on both sides and EXDEV anyway, because they were two separate bind mounts of one filesystem.  The encode, the verification and the publish had all completed;  only the final move failed, and the title landed in FAILED with the work already done.

TWO CONSEQUENCES, BOTH IN THE CODE NOW.  The layout collapsed to one root so the pipeline directories are siblings again, and 'paths.move_file' tries 'rename' first and falls back to copy-then-remove only on EXDEV.  Never assume a move within the container is cheap or expensive;  let the kernel answer.

A FAILED MOVE MUST RELEASE ITS OWN RESERVATION.  'unique_path' reserves the destination with 'O_CREAT | O_EXCL' before any data moves, so the failure above left a zero-byte file sitting in quarantine.  'move_file' removes the reservation on every failure path.

DOCKER IGNORE PATTERNS ARE PATH-PREFIX MATCHED FROM THE CONTEXT ROOT, NOT gitignore SEMANTICS.  A '.dockerignore' line of '__pycache__/' excludes only './__pycache__/' and does nothing about 'app/__pycache__/'.  Found 2026-09-07 when a build shipped 18 stale .pyc files into the image.  Use '**/__pycache__/' and '**/*.py[cod]'.

## 22.  Image and build

Base 'alpine:3.24'.  Moved from 'debian:trixie-slim' on 2026-09-10.  An earlier note that Alpine lacked the GNU coreutils behaviour the tooling relies on was true of busybox, not of Alpine:  the image installs GNU 'coreutils', 'findutils' and 'bash', so nothing in the tooling meets busybox.  The image went from 711 MB to 282 MB.

Packages:  ffmpeg, mkvtoolnix, python3, bash, coreutils, findutils, jq, ca-certificates, tini, su-exec, shadow, libcap, libcap-utils, libva, libva-utils, intel-media-driver, libvpl, onevpl-intel-gpu.  All from Alpine's main and community repositories;  nothing non-free, nothing from a third-party repository, nothing static.

Three substitutions against the Debian set, measured 2026-09-10:  'su-exec' for 'gosu', the same 'user:group command' shape;  'shadow' for 'useradd', 'groupadd' and 'usermod', which busybox's 'adduser' does not match;  'libva-utils' for 'vainfo'.  'intel-gpu-tools' has no Alpine package and nothing in the code referenced it.  '/usr/sbin/nologin' is a symlink the build creates, because Alpine's lives at '/sbin/nologin' and the entrypoint names the Debian path.

VERSIONS THAT MOVED WITH THE BASE:  ffmpeg 7.1.5 to 8.1.2, mkvtoolnix v92 to v99, python 3.13 to 3.14, x265 4.1 unchanged.  Every measurement in sections 12, 14 and 18 was repeated in the Alpine image and reproduced exactly, 'yuv420p10le' output confirmed Main 10, and the container ran end to end under podman:  tini, the privilege drop, the instance lock, the layout, the GPU probe, the TLS listener, the dashboard, plain HTTP refused, and a clean SIGTERM to exit 0.

'libcap-utils' provides 'setcap' and is required at build time, not run time.  'openssl' is deliberately ABSENT, so that nothing in the image can generate a certificate.

musl rather than glibc.  Nothing in the package touches it:  the application is standard-library Python and every heavy operation is a subprocess call into ffmpeg, x265 or mkvtoolnix, all of which Alpine builds against musl routinely.  DNS goes through musl's resolver, which since 1.2.4 falls back to TCP for large answers, so the provider lookups are not affected.

BINDING 443 AS A NON-ROOT PROCESS.  The entrypoint drops to PUID, and ports below 1024 need a capability.  The build applies 'cap_net_bind_service' to the resolved python binary, not to '/usr/bin/python3', because that is a symlink and 'setcap' does not follow symlinks.  Docker's default capability set already carries CAP_NET_BIND_SERVICE, so the compose file needs no 'cap_add'.  File capabilities live in extended attributes and are easy to lose silently, so the build verifies with 'getcap' immediately after setting it rather than assuming.

Firmware and the i915 binding come from the host kernel.  The image ships userspace only.

### The build gate

The build FAILS if ffmpeg lacks libx265, libsvtav1 or av1_qsv.

DO NOT DELETE THIS CHECK IF IT FAILS.  It exists so the image cannot ship claiming encoders it does not have.  The escalation order is av1_vaapi, which is the same hardware through VAAPI rather than oneVPL, then a pinned ffmpeg from Alpine's edge community repository.  Pick one at build time and record which in an image label; never let the runtime choose.

THE GATE ALSO ASSERTS THE LIBX265 WRAPPER HAS '-dolbyvision'.  Without it a Dolby Vision RPU cannot be carried through an encode, per section 14, and the build must not ship an ffmpeg older than 7.1 claiming otherwise.

VERIFIED 2026-09-07 on Debian trixie and again 2026-09-10 on Alpine 3.24:  ffmpeg carries libx265, libsvtav1, av1_qsv AND av1_vaapi, and the wrapper has '-dolbyvision'.  No fallback is needed today.

### Runtime GPU probe

At startup, 'vainfo' must report VAProfileAV1Profile0 with VAEntrypointEncSlice.  A failed probe does NOT crash:  it marks the GPU degraded and routes av1_qsv titles to libsvtav1, and at the HEVC default it changes nothing, so the GPU cannot break the pipeline.

Every encode logs which encoder actually ran, so a GPU that has quietly stopped being used is visible rather than silent.

## 23.  Versioning and release tags

'x.0.0' is a release.  '0.x.0' is the implementation of new features.  '0.0.x' is a bug fix.  The current version is 0.10.0.

EVERY BUILD INCREMENTS THE VERSION.  Adopted 2026-09-10, applying from the build after 0.0.12.  A build whose 'VERSION' equals the one before it is a build that cannot be told apart from it, on the provider User-Agent, on the image label, or in a bug report.  NOTHING ENFORCES IT.  The workflow reads 'VERSION' from 'app/__init__.py', tags the image with it and stamps 'org.opencontainers.image.version' from it;  a build on an unincremented version publishes an image whose version tag overwrites the previous one on GHCR, and that is the whole consequence.  The repository does not use git tags, and since 2026-09-12 the workflow no longer looks at them.

'VERSION' IN 'app/__init__.py' IS THE SINGLE DEFINITION.  A version duplicated into a format string rots silently, which is the defect that produced the placeholder User-Agent this replaced.  Three consumers:  the provider User-Agent, built as 'procrustes/<VERSION> (+<repo url>)', the startup log line, and the 'version' field on '/api/status', added 2026-09-13 so the running version is readable without the log.  Wikimedia rejects generic and browser-imitating agents with 403, and Wikidata is the first host every identification touches, so a browser User-Agent is not an option.

THE WORKFLOW IS 'workflow_dispatch' ONLY.  Images are published by a manual run from the Actions tab and by nothing else.

## 24.  Validation and testing

THERE IS NO LOCAL TEST SUITE AND THERE SHOULD NOT BE ONE.  A workstation and this container are different environments;  a result from one says nothing about the other, so functionality is validated in the container and nowhere else.

While code is being written, the only validation performed is validation of the code itself:

```
python3 -m compileall -q app          every module parses
python3 -c "import app.main"          the package imports, no circular imports
python3 -m app.check_names            every name a function loads is defined in its module
```

All three are properties of the source.  None executes a pipeline stage, touches a file or opens a socket.  The CI workflow runs exactly these three as its 'validate' job, and the image job depends on it, so a syntactically broken tree cannot produce an image.

THE THIRD CHECK EXISTS BECAUSE THE FIRST TWO CANNOT SEE A MISSING CONSTANT.  Measured 2026-09-13 on 0.9.0:  an edit to 'standards.py' replaced a block by slicing between two anchors and the slice took six module definitions with it ('KEEP_LANGS', 'PAL_HEIGHTS', 'PAL_RATE_TOLERANCE', 'LETTERBOX_CANDIDATE_ASPECTS', 'ASPECT_TOLERANCE', 'Verdict').  'compileall' and the import both passed, because a name inside a function body is resolved when the function runs, and the first title to reach SCREENED failed with 'NameError'.  'app/check_names.py' is a standard-library AST pass:  for every function it collects the names visible to it, the module's bindings, its own and its enclosing functions' locals, and the builtins, and reports any name loaded that is in none of them.  Run against the broken file it names all six;  against the tree it is silent.  No third-party checker is used, per section 3.

THE TEST INSTANCE IS WIPED FOR EVERY BUILD, RIGHT NOW.  'state.db' and the queues, 'import/', 'encode/', 'complete/' and '.quarantine/', are cleared before each test build is run, so title ids restart from 1, the findings table is rebuilt from empty, and everything in 'complete/' is a test artefact that is never promoted.  Section 11's poster cache incident is what an id reissue looks like to anything that assumed otherwise.  Live state is for diagnosis;  nothing in it is repaired, collected or promoted, and the only edits that persist are the ones made to the library itself on the workstation.

FUNCTIONALITY IS VALIDATED AGAINST A BUILT CONTAINER, PER 'TESTPLAN.md'.  That plan measures outcome:  every case asserts on a file, a filename, a tag block, a track list, an API response, an exit code or a health state.  No case reads a log line and no case depends on a log level.  Every case is repeatable from a stated starting state.

THE ROUTER CASES ARE THE HIGHEST RISK.  A misrouted title produces a valid file with the wrong tradeoff and nothing fails, so a case confirming an output file exists proves nothing.  TESTPLAN.md states the expected gate and encoder per case and reads both back from '/api/titles/<id>', corroborated against the output file's codec.

Colour flags, per section 14, are checked on the output file's properties rather than on the command that produced them.

The instance lock, the handover between instances and the SIGTERM path are container cases:  container behaviour is verified by running the container, not by reasoning about it.

## 25.  Decisions and their rationale

### x265 is the default output codec, not AV1

AV1 is 25 to 30 percent more efficient and is where this library should end up.  The client fleet is not there.  The current Apple TV 4K is A15-based and has no AV1 hardware decoder, and there is no AV1-capable Apple TV on the market, so this is not a wait-a-few-months problem.

Jellyfin transcodes AV1 to h264 on the fly for a client that cannot decode it.  That is a real mitigation when the transcode is hardware accelerated, but it spends at delivery time some of the quality the AV1 encode paid for, and it contends for the same media engine.  Direct play has no failure mode.

So AV1 is fully built, tested and selectable, and 'output_codec' defaults to hevc for both kinds.  Flipping it is a settings change plus a calibration batch, not a code change.  That is the entire reason encoder selection is a router rather than a hardcoded ffmpeg line.

Consequence:  four of the seven router paths are dormant at the default, which means they are least exercised at exactly the moment they get switched on.  TESTPLAN.md cases T-35 and T-36 exist to exercise them before that switch is flipped in anger.

### The manual review gate became an automatic readiness check

The old pipeline stopped for a human before encoding.  That gate was load bearing, but it was written as an aid to a person who was going to look anyway.  It is now automatic and holds on failure.  DRY_RUN and the held queue exist to buy back the confidence that gate provided.

### Television is processed automatically in aired order

Chosen over a per-show held decision.  The protection is section 11's title match, not the order:  the number is derived from the match, so release-group renumbering cannot propagate.

### No third-party Python dependencies

Section 3's rule, stated here as a decision so it does not erode:  every dependency added is a supply chain, an upgrade treadmill and an audit burden on an image that otherwise consists of Alpine packages and this repository.

### The web UI is unauthenticated, over HTTPS only

Intended for a trusted LAN.  Anyone who can reach the port can force a held title through, quarantine an incoming file and change every setting.  That is a deliberate tradeoff for a home service, and it is the reason the port should be published deliberately rather than broadly.

TLS DOES NOT CHANGE THAT.  The listener is HTTPS on 443 with no HTTP listener and no redirect;  what TLS buys is that the traffic is not readable in transit, nothing more.

THE CERTIFICATE IS SUPPLIED, NEVER GENERATED.  '/certs' is mounted read only, 'openssl' is deliberately absent from the image, and nothing in the container can create a certificate.  A missing, unreadable or malformed pair is logged and the supervisor exits non-zero before any listener is created, because a silent downgrade to HTTP would defeat the requirement.  With 'restart: unless-stopped' that is a restart loop rather than a running container with a broken UI, and that is the intended behaviour.

THE CONTAINER DOES NOT VALIDATE THE CERTIFICATE IT IS GIVEN.  It does not inspect the issuer and does not verify the chain.  Do not add those checks:  the operator owns what is mounted, and chain validation would reject a private-CA certificate as a side effect while proving very little.

The healthcheck probes '127.0.0.1' with verification disabled.  That is not an accommodation for a weak certificate;  a certificate issued for the service hostname fails hostname verification against a loopback address no matter which CA signed it, and the probe is testing liveness rather than identity.

## 26.  ARM configuration, upstream

Automatic Ripping Machine produces most of what arrives in 'import/'.  It is not part of this container and this repository does not configure it, but its settings determine what the container is handed, so they are recorded here rather than only in the workstation standards.

```
MINLENGTH        2400     titles under 40 minutes are ineligible
SKIP_TRANSCODE   true     ARM keeps MakeMKV output, largest file assumed main feature
RIPMETHOD        mkv      MakeMKV direct
PREVENT_99       false    setting true ejects the disc and rips nothing
MAINFEATURE      inert    HandBrake option;  with SKIP_TRANSCODE true, HandBrake never runs
```

MINLENGTH IS THE SETTING THAT MATTERS.  At the previous value of 600 a 10 minute bonus featurette qualified as a disc's main feature and produced the wrong Cars and Chicken Little rips.  2400 is what makes the 40 minute movie floor in section 7 a second line of defence rather than the only one.

WRONG-TITLE RIPS WERE CAUSED BY MINLENGTH, NOT DRM.  The Cars disc reported 8 titles, not 99, so PREVENT_99 was never in play.  Every 'HB_*' setting does nothing while SKIP_TRANSCODE is true, because HandBrake never runs.

## 27.  The library audit

A BACKGROUND SWEEP FOR EVERY DEVIATION THE PASSTHROUGH PATH CAN CORRECT, AND NOTHING THAT NEEDS AN ENCODE.  'audit.Auditor' is a thread beside the watcher.  It walks the mounted libraries with 'os.walk', never glob, opens files read only, and assesses each against exactly the set the passthrough path repairs on every title:

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
```

Resolution, bit depth, codec, letterbox and PAL speed-up are never findings.  They need an encode, and an encode of already-encoded media is the loss gate 1 exists to prevent.

THE NAMING CHECK NEEDS NO PROVIDER.  Section 9's rule is that every name is derived from the tag block, so the audit builds the names the pipeline would publish, through the same 'titles.movie_folder', 'movie_filename', 'show_folder', 'season_folder' and 'episode_filename' the publish step uses, and compares them with what is on disk:  the containing folder and the file for a movie, the show folder, the season folder and the file for an episode.  It cannot check the reverse and does not try:  a tag that is itself wrong, and a folder and file that agree with it, pass.  That is only caught when the file goes through the pipeline and rung 1's verification rewrites the identity.

UNTIL 2026-09-11 ONLY THE FILE'S TITLE PORTION WAS CHECKED.  The folder was never read, so the Return of the Jedi folder in section 11's old form, and a show folder written '[tmdbid-None]' by the P4947 defect, passed;  measured that day, the two Star Wars entries with no tag block were flagged and the third, with a folder in the old form, was not.  The rows now compare whole names, so the year, the ids and the season padding are covered as well as the title.

Three consequences shape the findings list:

- A NAMING ROW NEEDS A COMPLETE TAG BLOCK.  A block missing any field the name needs reports 'tag incomplete' once per row and compares nothing;  the 'tag structure' row already carries that finding, and a missing block must not fan out into four more.  A show whose COLLECTION block holds no TMDB is incomplete by this rule, which is what the 0.2.0 outputs look like.
- FINDINGS ARE PER FILE.  A wrong show folder is one finding on every episode in it;  there is no folder-level record.
- THE SHOW FOLDER'S YEAR COMES FROM THE FOLDER.  The COLLECTION block carries no year, so the expected show folder is built with the year parsed out of the actual one, 'YYYY' when it has none, and the row checks everything else.

Repairing a naming finding is the same Import as any other:  rung 1 of the identity ladder reads the embedded block, verifies it, and the published folder and file are regenerated from it.  For a folder-only defect that copies every file in the folder through the pipeline;  renaming the folder by hand is the cheaper route and remains the operator's, per section 2.

IT IS THROTTLED AND IT SKIPS WHAT HAS NOT CHANGED.  'AUDIT_INTERVAL' seconds between files, default 2, so a first pass over 2934 files takes about two hours and never competes with a staging copy for the array.  A 'findings' table in 'state.db' records path, size and mtime for every file assessed, so a repeat pass is mostly 'stat' calls.  A pass restarts 'AUDIT_SWEEP_INTERVAL' seconds after the last one finished, default 3600.  'AUDIT_INTERVAL=0' disables it, and it never starts when no library is mounted.

THE SKIP KEYS ON SIZE AND MTIME ONLY, SO A RULE CHANGE NEEDS A WIPE.  A file assessed under one set of checks and untouched since is never re-assessed under a later set;  0.4.0's naming rows reached the whole library only because the findings table was empty at startup.  'Rescan entire library' in the findings dialog, 'POST /api/audit' with '{"rescan": true}', runs 'Auditor.rescan':  the findings table is deleted, a running pass is stopped through the '_restart' event so it does not leave a stat-skippable tail, and the next pass is a first pass by construction, the full 1.9 hours at the default interval.  The wipe takes the repair links with it, so a finding whose import is in the pipeline shows Import again until the finding is re-created;  the title rows, and the self-comparison skip they carry, are untouched, and a second Import of the same file is still refused on the name collision in 'import/'.

REPAIR IS BY RUNNING THE FILE THROUGH THE PIPELINE.  Each finding carries a copy action, 'POST /api/audit/<id>/import', which copies the library file into 'import/' and lets the ordinary chain do the rest.  That is a read of the library and a write under the writable root, so section 2 gains no exception.  The copy lands as '<name>.part' and is renamed, because the watcher ignores '.part';  it refuses when the root lacks the space, when the name is already in 'import/', or while a title for that file is in the pipeline, which is every stage but quarantine and includes a published copy not yet promoted, per section 6.  The loop still ends by hand:  the repaired file lands in 'complete/' and the operator moves it into the library.

A COPIED TITLE MUST NOT COMPARE AGAINST ITSELF.  It resolves to the same provider ID as the file it came from, so '_find_incumbent' would match that file, every gate would read equal, and section 8 would hold it as a no-vote pair.  The finding records the import path, the watcher links the new title to it and stores 'origin_path' on the row, and '_find_incumbent' skips that one realpath.  Only the comparison is skipped;  screening and readiness still apply.  This does not use 'overridden', which section 7 makes far broader than this needs.

The three HDR titles needing repair on 2026-09-10 are 5.6, 6.3 and 9.8 GiB, each copied to 'import/', staged and moved to 'complete/', roughly 65 GiB of I/O and a 29 GiB free-space floor for the largest, to correct a few hundred bytes of Colour header;  what that buys is a file verified by the same 'readiness' check as any other title, which a direct header edit is not.

The dashboard's 'Library findings' counter and its dialog are as README describes;  the Details table per finding is in the shape of the Compare dialog, container beside bitstream for the HDR rows, expected beside actual for the rest, the differing rows marked, and only correctable checks appear.

## 28.  Out of scope

Not in this repository, and adding them needs a decision rather than a commit:

- Any write path into a media library.  See section 2.
- YouTube.  Those rips are organised by hand and no naming, tagging or encoding standard applies.
- Music.  No standards are defined for it anywhere yet.
- Host-specific packaging.  No Unraid Community Applications template, no Docker Hub mirror.  The deliverable is the image plus a reference compose file that runs anywhere with Docker and a render node.
- The workstation scripts.  They live in their own tree and continue to run there unchanged.

ONE EXCEPTION TO THE PACKAGING RULE, ADDED DELIBERATELY.  The Dockerfile carries 'net.unraid.docker.icon'.  It is Unraid-specific and inert on every other host.  It is there because a container with no icon makes the Unraid Docker page request a placeholder that does not exist on that build, and the page auto-refreshes:  measured 2026-09-08, that filled the 128 MB '/var/log' tmpfs to 100 percent with 66 MB of syslog and 61 MB of nginx errors.  The container wrote none of it.  The icon lives at 'media/procrustes.png' and is served from the repository, matching what every other container on that host does.  It is a placeholder and is expected to be replaced.  THE SAME FILE IS THE DASHBOARD FAVICON.  The Dockerfile copies it into 'app/static/' and 'webui' serves it at '/favicon.ico' as 'image/png' with a day of cache;  the page declares it with a 'link rel=icon'.  One file in the repository, two consumers, so replacing the placeholder replaces both.

## 29.  To do

Open items, all deferred by the developer.  Remove an entry when it is done or dropped;  do not let this list describe finished work.

- **AV1 calibration.**  Section 14's AV1 parameters have no calibration behind them.  The first AV1 batch ran 2026-09-10;  score the outputs against their sources with the 'ssim' filter, per section 14.
- **'x265_dv_vbv_kbps'.**  The known issue in section 14.  Settle it with one full-length 1080p DV encode and one UHD at the current cap, reading the x265 log for VBV adjustments and the per-frame QP curve and comparing the bitrate curve against an uncapped CRF 18 encode of the same source;  if the cap never binds at 1080p keep it there, and decide the UHD figure separately and by tier.  Inert under gate 1 until then.
- **TESTPLAN cases written 2026-09-10 and not yet executed against the container:**  T-60e to T-60h (HDR declarations), T-94 to T-100 (reasons and force), T-101 to T-107 (the library audit), T-108 to T-115 (assessment ahead of encoding), T-116 to T-118 (audit copy feedback), T-119 to T-125 (origin ids, transform-aware search, Retry, the show ladder and specials), T-126 to T-132 (folder and file name alignment), T-133 to T-135 (the full rescan), T-136 to T-142 (the ladder, page confirmation, the None guard, the zero content light pair), T-143 to T-150 (in the pipeline until promoted, the numeric title, the inconclusive reason, the emptied import folder, rows closing with their files, frame-counted progress), T-151 to T-157 (the incomplete-identity rule on both kinds, the IMDb route to TVDB, the English catalogue, the TMDB adaptation date, candidates per source, the operator selection, the title after the marker), T-158 and T-159 (the catalogue title under numbering, the held file under 'hold/'), T-160 to T-169 (the matcher's tag strip, part index and containment rung, the tie hold, the field probe and its three chains, the release corpus, the file hash, cropdetect sampling, editions, episode forms, extras words), T-174 to T-181 (the settings page and menu, per-kind CRF, a routed title's frozen parameters, live pool resize, a rejected batch, ignored environment variables, reset, the kept languages), the last nine groups written 2026-09-11 to 2026-09-14.  Each was exercised in a scratch tree on the workstation;  the plan is run by hand against the built image.
- **Review the library audit's first pass.**  Expected findings on the current library:  the three titles under-declaring ST 2086 and Forrest Gump's missing CLL, per section 12;  the last is a zero pair the container genuinely lacks, and its repair copy passes now that the probe reads the element through mkvmerge.  Anything else it reports is either a real defect or a check that needs correcting, and the edition false positive fixed on 2026-09-10 is the reference for the second kind.
- **The grain probe against the 9,697k reference.**  Section 14's grain-heavy 35mm title encoded at 'aq-mode=3' before automatic detection existed.  Run the probe on that source and confirm the ratio clears 'grain_threshold', so the tune that separated the nine-film batch is what an automatic run would choose.
- **Hardware decode on the QSV path.**  'build_command' decodes in software and uploads with 'hwupload';  on the A310 the media engine sat at 47 percent with the decode block near idle.  '-hwaccel qsv -hwaccel_output_format qsv' ahead of '-i' keeps frames on the device.  Needs a measurement and a software fallback for sources the hardware decoder does not accept.  Not planned;  noted as the next lever on the GPU path.
