# procrustes

An automatic media import pipeline in a container.  Drop a title into 'import/', collect it from 'complete/'.

It identifies the title against a provider, checks it against a minimum standard, compares it against whatever the library already holds, remuxes to Matroska, strips non-English tracks, writes the Matroska tag hierarchy, encodes, verifies the result, and puts the finished file where you can pick it up.

It never writes to a media library.  Moving finished titles in stays a manual step, on purpose.

## The chain

```
import/  ->  probe  ->  standards  ->  identify  ->  compare  ->  remux
         ->  tag  ->  readiness  ->  encode  ->  verify  ->  complete/
```

Assessment runs ahead of encoding.  The assessment workers, three by default, take every title through probe, standards, identification, comparison and routing within minutes of a drop, so every gate failure is in the held queue long before the first encode finishes;  one thread per encoder, plus one for passthrough, then takes titles in queue order, and the queue order is yours to drag on the dashboard.  Anything that fails a gate goes to 'hold/' with a written reason and waits for a decision in the web UI.  A transient failure, such as a provider lookup that could not reach the network, holds with an exponential backoff and retries on its own:  five retries at 120, 240, 480, 960 and 1920 seconds, roughly 62 minutes in all, before it stops and waits for a person.

Nothing is ever deleted.  Sources are retired to 'complete/.quarantine' after the title completes.  A folder under 'import/' that is left empty by that move is removed, so a title dropped in as a whole folder does not leave its shell behind.

## Stages

Every title carries a stage, shown in the Stage column of the dashboard.  These are the values you will see there and what each one means.

| Stage | Shown as | Meaning |
| --- | --- | --- |
| DETECTED | queued | Seen in 'import/', size stable across two polls and untouched for the quiet window, 30 seconds by default.  Waiting for a free worker, at the back of the queue.  A title returns here, again at the back and with its file back under 'import/', when you press Retry or Force through, and when a retry backoff expires. |
| PROBED | probed | One ffprobe pass done.  Classified as a movie or as television. |
| SCREENED | screened | Passed the minimum standards gate. |
| IDENTIFIED | identified | Provider IDs resolved and verified.  The canonical name is settled from here on. |
| COMPARED | compared | Checked against whatever 'complete/' and the library already hold, in that order.  Also the value recorded when there is no incumbent in either, and when the incumbent could not be read.  A second arrival of a title still in the pipeline holds here naming the first, with its own table against the incumbent and a second against the first arrival. |
| ROUTED | waiting for encoder | The encoder is chosen and the title waits for its pool, CPU, GPU or passthrough, which takes titles in queue order.  Assessment is done;  everything from here runs on the pool's thread when one is free. |
| STAGED | copying | Copying the source into the encode work area.  A multi-gigabyte title sits here for minutes. |
| REMUXED | remuxed | Converted to Matroska if needed, non-English tracks dropped, track flags corrected. |
| TAGGED | tagged | Matroska tag block and segment title written. |
| READY | ready | Passed the readiness gate, or was forced past it, and is queued for an encoder slot. |
| ENCODING | encoding | An encoder is running.  Frames done of the total and the estimated time remaining appear beside it;  the count comes from the encoder itself, so a sparse subtitle track cannot make a running encode look stuck. |
| ENCODED | encoded | The encoder finished, or the router chose passthrough and no re-encode was needed. |
| VERIFIED | verified | Duration, packet count, track statistics, tag structure and HDR declarations checked on the finished file, every failure collected before it holds. |
| PUBLISHED | ready to promote | **The file is in 'complete/' and is yours to collect.**  This is the end of the pipeline as far as you are concerned.  Move the file out of 'complete/' and the title leaves this column on the next poll:  its record is closed if nothing of it remains, or counted under quarantined files while its retired source is still in '.quarantine'. |
| CLEANUP | ready to promote | Housekeeping after publishing:  the source is retired to quarantine, the folder it leaves empty under 'import/' is removed, and the work area is wiped.  It touches nothing you collect, so it reads the same as PUBLISHED. |

Three further values sit outside the pipeline.

| Stage | Shown as | Meaning |
| --- | --- | --- |
| HELD | needs a decision | A gate failed and the title is waiting for you;  its file has moved to 'hold/', at the same path it had under 'import/', and moves back to 'import/' on Retry, Force through or Identify, or to quarantine on Discard, so 'hold/' is empty once every decision is made.  Every reason is listed, not only the first:  the standards, identification and comparison checks all run before a title holds, so one Force through is an informed decision rather than a guess repeated until the title moves.  The decision queue offers Retry, Force through and Discard. |
| QUARANTINED | rejected | Refused, beaten by the incumbent, or superseded in 'complete/' by a later arrival that beat it.  The file is in 'complete/.quarantine'.  Remove it from there and the record closes on the next poll. |
| FAILED | failed | A mechanical failure:  an unreadable probe, a remux or encoder that exited non-zero, or a publish that could not write.  Nothing was moved or deleted, and no output exists, so Force through cannot apply and is not offered;  Retry and Discard are. |

A row marked "files gone" refers to a title whose files you have since removed by hand.  The watcher closes such a record on its next poll;  the Forget button does the same at once.  Forget only removes a database row;  it never deletes a file.

## Layout

```
app/            the pipeline: one module per concern
  main.py         supervisor, startup, signals
  config.py       the environment interface
  paths.py        mount contract, write guards, atomic publish
  locks.py        single-instance lock and encode job ownership
  orchestrator.py the state machine, the assessment workers, the encoder pools and the queue order
  encode.py       the encoder router and command builders
  gpu.py          the runtime GPU probe
  media.py        remux, language strip, flag repair, cropdetect, grain probe
  tags.py         Matroska tag hierarchy and the readiness gate
  probe.py        one ffprobe pass, the attribute set everything else consumes
  standards.py    the minimum-standards gate
  compare.py      new versus incumbent
  titles.py       the filename transform and naming rules
  episodes.py     episode matching, ranges, part markers
  provider.py     Wikidata, TMDB and TVDB lookups
  state.py        SQLite store, one row per title, carrying the queue order
  webui.py        JSON API, the session gate and the pages
  auth.py         passwords, TOTP, sessions, the lockout and the account rules
  settings.py     the settings registry
  audit.py        the background library sweep
  check_names.py  the third source check
  static/         the dashboard, the settings pages and the login form
  data/           FileBot's release-group and media-source lists, CC0, vendored
Dockerfile      alpine:3.24 plus ffmpeg, mkvtoolnix and the Intel media stack
entrypoint.sh   drops to PUID/PGID, joins RENDER_GID for /dev/dri, takes ownership of the writable mount points
media/          the container icon and dashboard favicon, a placeholder
```

## Mount contract

The pipeline is import, then encode, then complete.  One mount is required and everything else derives from it.  'import', 'complete', 'complete/.quarantine' and 'hold' are always subdirectories of the root and are not configurable, because a move between them is then a rename rather than a copy.

| Container path | Env var | Mode | Required | Purpose |
|---|---|---|---|---|
| /media | MEDIA_ROOT | rw | yes | the one required mount, everything derives from it |
| /encode | MEDIA_ENCODE | rw | no | per-title work area, mount separately for fast storage |
| /config | MEDIA_CONFIG | rw | no | state.db, instance lock, provider cache, logs |
| /library/movies | LIBRARY_MOVIES | ro | no | incumbent comparison |
| /library/tv | LIBRARY_TV | ro | no | incumbent comparison |
| /certs | CERT_DIR | ro | yes | TLS certificate and key |

Quarantine is not a mount.  Retired sources and rejected files go to '/media/complete/.quarantine'.

Without a library mount the incumbent comparison is skipped and every title is treated as new, which is logged at startup and shown in the UI.  A missing root is a startup error naming the variable.

The whole per-title work area lives on the encode mount, not just the encode.  A title is copied in once, then remuxed, tagged, encoded and verified there, and the finished file is moved out once.  That is two crossings of the slow filesystem in exchange for keeping three or four full-file rewrites on fast storage.  Startup compares the filesystem of the encode and complete mounts and warns when they match, so a fast disk that silently landed on the same filesystem is visible rather than mysterious.

## Naming

The Matroska tag carries the provider's title verbatim, character for character, punctuation and all.  The filename is that same title with only the unsafe characters removed, plus the control range:

```
/   \   :   *   ?   "   <   >   |
```

Nothing is re-worded, abbreviated, reordered or truncated.  Those nine are what SMB forbids, and the libraries are served over SMB, so SMB is the binding constraint rather than the filesystem.

A colon is REMOVED rather than turned into a dash, so 'Avengers: Endgame' is filed as 'Avengers Endgame'.  An em dash or en dash becomes ' - ', and a forward slash inside a title becomes '-'.  Everything else is kept, including parentheses, brackets, commas, apostrophes, ampersands and accented letters.  The parentheses and brackets are structural:  '(Year)' appears in every movie name, and '[tmdbid-N]', '[imdbid-ttN]' and '[tvdbid-N]' are what the incumbent lookup keys on.

```
Title (Year) [tmdbid-N] [imdbid-ttN]/Title (Year).mkv
Show Name (Year) [tvdbid-N] [tmdbid-N]/Season NN/Show Name - SNNENN - Episode Title.mkv
```

A file covering two episodes takes the range form, 'Show - S05E01-E02 - Kidnapping.mkv', because naming it as only the first makes Jellyfin report the second as missing;  the range is read from the source name, anchored on the episode the title match settled, and published only when the catalogue lists every episode in it, under the shared base title or, for two unrelated episodes, the first one's.  A multi-part episode uses ', Part 1' rather than whichever marker the provider happened to use.

The longer form that repeats the whole folder name inside the filename is for editions and nothing else:

```
Alien 3 (1992) [tmdbid-8077] [imdbid-tt0103644]/Alien 3 (1992) [tmdbid-8077] [imdbid-tt0103644] - Assembly Cut.mkv
```

The edition is read from the arrival's name, only after the year or at the end of the name, and only from the words that name a cut:  Director's Cut, Director's Definitive Cut, Final Cut, Assembly Cut, Alternative Cut, Extended, Theatrical, Unrated, Uncut, Uncensored, Special Edition, Fan Edit and Festival.  Remastered, Restored, Criterion, IMAX, Collector, Limited, Deluxe and Ultimate describe a transfer or a box, not a cut, and are stripped without becoming a label.  A claimed edition is then checked against the folder's plain file:  the same frame count, or a runtime within 'edition_runtime_tolerance_s', means the same cut, so the label is dropped and the pair compared as one title;  a different runtime keeps the label and the folder counts as no incumbent.  Only a claimed edition is checked this way;  a plain arrival is never compared against an edition.

The transform runs one way.  A filename can always be derived from a tag;  a tag can never be derived from a filename, because the information needed has already been discarded.  Where a tag and a filename differ by unsafe characters alone, that is expected and is not a defect.

## Environment

| Name | Default | Purpose |
|---|---|---|
| MEDIA_ROOT | /media | the one mount everything derives from;  defaulted rather than demanded, then validated to be a mounted directory |
| MEDIA_ENCODE | <root>/encode | optional, per-title work area on faster storage |
| MEDIA_CONFIG | <root>/config | optional, state.db, lock, cache, logs |
| LIBRARY_MOVIES | unset | ro movie library |
| LIBRARY_TV | unset | ro tv library |
| CERT_DIR | /certs | ro, holds the TLS certificate and key |
| TLS_CERT_FILE | fullchain.pem | certificate name within CERT_DIR |
| TLS_KEY_FILE | privkey.pem | private key name within CERT_DIR |
| PUID / PGID | required | identity the supervisor drops to;  enforced by 'entrypoint.sh', which refuses to start without both |
| RENDER_GID | unset | supplementary group for /dev/dri, GPU off if unset |
| RENDER_NODE | /dev/dri/renderD128 | render node the GPU probe and QSV encoder use |
| WEB_PORT | 443 | HTTPS only, there is no HTTP listener |
| DRY_RUN | 0 | 1 logs every intended action and performs none |
| LOCK_WAIT_TIMEOUT | 0 | seconds to wait for the instance lock, 0 waits indefinitely |
| LOCK_WAIT_INTERVAL | 15 | how often to retry the instance lock |
| LOG_LEVEL | info | 'info' records what happened, 'debug' adds why |
| AUDIT_INTERVAL | 2 | seconds between library files audited;  0 disables the sweep |
| AUDIT_SWEEP_INTERVAL | 3600 | seconds between passes over the libraries |

Every one of these is echoed into the log and onto /api/status at startup, so what the container thinks it was configured with is always visible without exec-ing into it.

The environment is the deployment surface only.  OUTPUT_CODEC, CRF, TV_ENCODE_SD, MAX_JOBS, GPU_SLOTS, CPU_SLOTS, ENCODE_HEADROOM, ENCODE_THREADS, POLL_INTERVAL, MTIME_QUIET and GRAIN_THRESHOLD are not read from the environment;  each is a setting in the section below, and one left in the environment is logged as ignored at startup.

Every module logs what it does.  At the default 'info' the log records one line per meaningful action, naming the title, the stage and the outcome, and says why when something fails or degrades.  Set 'LOG_LEVEL=debug' to add the detail behind each of those lines:  command lines, per-gate comparisons, measured figures against their thresholds.  Logs go to stdout and to 'config/logs/procrustes.log', and the tail is served at /api/logs.

Six more are read directly by the modules that use them and are neither validated nor reported.  They exist to substitute a binary, not to configure the service:  FFPROBE, FFMPEG, MKVMERGE, MKVPROPEDIT, MKVEXTRACT and VAINFO.

Two host-side values are consumed by the compose file rather than the container, and set the kernel cgroup limits:

| Name | Default | Purpose |
|---|---|---|
| CONTAINER_CPUS | 8 | CPU quota;  the encoder thread count autodetects from it |
| CONTAINER_MEM | 16g | memory ceiling;  nothing derives from it, it just has to be enough |

Get RENDER_GID from the host that will run the container:

```
stat -c %g /dev/dri/renderD128
```

## Settings

Everything that is not a deployment detail is a setting:  stored in a 'settings' table in 'state.db', edited on the Application Settings page under the menu at the top right of the dashboard (the Access group on the User Settings page instead), read by the pipeline at the point of use, and echoed into the log at startup with a mark on every stored value.  A setting nobody has changed is its default, and the defaults are the standards this pipeline was built on, so a fresh 'state.db' runs exactly as the reference configuration does.

Every setting under Encoding except the SD height, the passthrough codec list and the Dolby Vision VBV figure has one value for movies and one for television, and so do the grain threshold and the standards floors.  The rest are global.

| Group | Setting | Default | Meaning |
|---|---|---|---|
| Pipeline | max_jobs | 3 | assessment workers:  probe, screen, identify, compare and route, ahead of any encode |
| Pipeline | gpu_slots | 1 | GPU encode threads |
| Pipeline | cpu_slots | 1 | CPU encode threads, each at the encoder thread count divided by this;  two gain little except on SD |
| Pipeline | encode_headroom | 3.0 | multiple of source size required to admit a job |
| Pipeline | encode_threads | 0 | 0 autodetects from the cgroup CPU quota |
| Pipeline | poll_interval | 15 | import watch interval in seconds;  the page refuses a value at or above mtime_quiet |
| Pipeline | mtime_quiet | 30 | seconds a file must be untouched before it counts as stable |
| Pipeline | retry_max_attempts | 6 | transient failures retried this many times before the title holds |
| Pipeline | retry_base_delay | 120 | seconds before the first retry, doubling each time |
| Encoding | output_codec | hevc / hevc | hevc or av1 |
| Encoding | encode_sd | off / off | on sends an SD source to the encoder instead of passing it through |
| Encoding | sd_display_height | 720 | display height below which a source is SD |
| Encoding | passthrough_codecs | hevc, av1 | source codecs never re-encoded |
| Encoding | x265_preset, x265_crf | slow, 18 | libx265 speed and quality |
| Encoding | x265_aq_mode, x265_aq_mode_film, x265_tune_film | 3, 4, grain | adaptive quantisation on a clean and on a grainy source, and the tune on a grainy one |
| Encoding | x265_psy_rd, x265_psy_rdoq, x265_deblock | 2.0, 1.0, -1,-1 | the rest of the x265 params string |
| Encoding | x265_pix_fmt | yuv420p10le | 10-bit output |
| Encoding | x265_extra_params | empty | appended verbatim to the params string |
| Encoding | x265_dv_vbv_kbps | 40000 | VBV pair on a Dolby Vision encode |
| Encoding | svtav1_preset, svtav1_crf, svtav1_params, svtav1_pix_fmt | 4, 24, tune=0:film-grain=8, yuv420p10le | libsvtav1 |
| Encoding | qsv_preset, qsv_global_quality | veryslow, 26 | av1_qsv |
| Probes | grain_threshold | 0.18 / 0.18 | denoise delta above which a source counts as grainy |
| Probes | grain_sample_seconds, grain_sample_position | 20, 0.45 | the sample the grain and field probes decode |
| Probes | grain_probe_crf, grain_probe_preset | 20, ultrafast | the two sample encodes the grain probe compares |
| Probes | field_telecine_share | 0.10 | share of repeated fields at which a source is telecined |
| Probes | crop_sample_count, crop_sample_seconds, crop_sample_attempts | 6, 2, 12 | cropdetect sampling |
| Probes | crop_black_level_factor, crop_black_level_cap | 1.5, 0.13 | the cropdetect limit from the measured black |
| Probes | crop_secondary_share | 0.06 | share of samples at which a second geometry is a variable aspect |
| Standards | min_display_width, min_display_height | 1920x800 / 0x0 | resolution floor, 0 for none |
| Standards | min_runtime_min | 40 / 15 | runtime floor in minutes |
| Standards | letterbox_bars_px | 20 | bars at or above this are cropped, and above it fail the standards |
| Standards | pal_speedup_check | on | fail a 25 fps source at a PAL height |
| Standards | keep_langs | eng, en, und | audio and subtitle languages kept at ingest |
| Comparison | pixel_tolerance, bitrate_tolerance | 0.05, 0.25 | below these differences gates 2, 3 and 6 cast no vote |
| Comparison | codec_efficiency | h264 1.0, hevc 1.7, av1 2.2, vc1 0.9, mpeg4 0.7, mpeg2video 0.45 | bitrate weighting per codec |
| Comparison | edition_runtime_tolerance_s | 30 | runtime difference under which a name-claimed edition is the same cut |
| Matching | title_cutoff, contained_score | 0.82, 0.9 | the episode and search matcher's scores |
| Matching | max_range_span | 3 | widest 'E01-E03' range read as a range |
| Matching | range_duration_ratio | 1.8 | a single-numbered library episode this many times its season's median, with no next episode beside it, is listed as two episodes in one file |
| Matching | candidate_limit | 8 | candidates listed per source on an identification hold |
| Matching | provider_throttle_s, provider_timeout_s | 3.0, 30 | spacing and timeout of provider requests |
| Access | auth_enabled | on | a login in front of every page and API route;  off opens both to anyone who can reach the port.  Written only from User Settings |
| Access | session_hours | 168 | hours a login stays valid;  signing out ends it sooner |

A change applies to the next title that reaches the stage reading it.  A title already routed keeps the encoder parameters it was routed with, stored in its decision and shown on its detail, so a queue does not change shape under a running configuration;  Retry re-assesses a title under the current settings.  A change to max_jobs, gpu_slots or cpu_slots resizes the pools live:  a pool grows at once, and a pool that shrinks lets its surplus thread finish the title it is on before it exits.

The page posts every changed value in one request and nothing is written unless every value is valid;  a rejected value is named beside its field.  'Reset' beside a stored value, or on a whole group, returns it to the default.

## The encoder router

Evaluated in order, first match wins.

```
1  source codec is in passthrough_codecs   PASSTHROUGH
2  SD source and encode_sd is off          PASSTHROUGH
3  Dolby Vision RPU present                libx265    on any setting
4  output_codec av1 and grainy             libsvtav1  CPU
5  output_codec av1                        av1_qsv    GPU
6  grainy                                  libx265 aq-mode=4:tune=grain
7  otherwise                               libx265 aq-mode=3
```

x265 is the default because no Apple TV decodes AV1 in hardware.  AV1 is fully built and selectable per kind on the settings page, but the calibration batch has not been run, so the AV1 rate control values are starting points rather than settled ones.

Gate 3 exists because an AV1 re-encode discards the Dolby Vision RPU.  Those titles always take the x265 path, on any setting.

An HDR source that reaches an encoder carries its colour, mastering display and content light level into the x265 params, and a Dolby Vision source carries its RPU through with ffmpeg's native '-dolbyvision' and a VBV pair, no extraction tool involved.  In practice neither happens:  HDR and Dolby Vision material is HEVC, and gate 1 passes it through untouched.

Gate 5 is the one that can produce an encoder the table does not name.  At startup 'vainfo' must report VAProfileAV1Profile0 with the encode entrypoint;  a failed probe does not crash the container, it marks the GPU degraded, and gate 5 then routes to libsvtav1 on the CPU instead of av1_qsv.  At the hevc default a degraded GPU changes nothing at all, which is the point:  the GPU cannot break the pipeline.  Every encode logs which encoder actually ran, so a GPU that has quietly stopped being used is visible rather than silent.

SD is decided on display height, computed from width times SAR over height, so an anamorphic PAL DVD rip is classified on what it actually displays rather than on its stored dimensions.

PASSTHROUGH MEANS NO VIDEO RE-ENCODE.  It does not mean no processing.  A passthrough title is still remuxed to Matroska, language stripped, flag corrected, tagged and given track statistics.  An SD AVI rip arriving in 'complete/' still as an .avi would be a bug.

Grain is detected automatically, by encoding a 20 second sample twice, once clean and once through a light denoise, and comparing the two sizes.  The ratio is logged for every title so a bad threshold is visible rather than silent.  An 'encode.job' sidecar overrides it.  It is read from the directory holding the source rather than from a per-title path, so one file governs every source alongside it:  convenient for a season, surprising for a mixed drop.

```
film=1                  force the grain path, film=0 forces the clean path
codec=av1               per-title output codec
crf=17                  per-title quality target
crop=1920:804:0:138     skip cropdetect and use this
fields=telecine         skip the field probe;  progressive, interlaced or telecine
```

Every encoder-bound title is also classified as progressive, interlaced or telecined from an 'idet' pass over the same sample, and the command carries 'bwdif' or an inverse-telecine chain ahead of the crop when it needs one.  Cropdetect takes six samples across the file, measures the black level, rejects implausible samples and records a second aspect ratio when a film changes shape.

## Comparison gates

Before any encode, an arrival is compared against whatever 'complete/' and the library already hold, 'complete/' first because what is waiting to be promoted is the best copy known.  Every incumbent found is compared, and the arrival must win against each.  Seven gates, in order:

```
1  HDR or Dolby Vision present    losing it is never an upgrade
2  display pixel count            width * SAR / height, never stored dimensions
3  baked-in letterbox             larger real picture area wins
4  audio maximum channel count    5.1 beats 2.0
5  bit depth                      10-bit beats 8-bit
6  video bitrate                  weighted for codec efficiency
7  source pedigree                remux > encode > web, tiebreak only
```

EVERY GATE IS EVALUATED AND THE VOTES ARE TALLIED.  The first difference does not decide.  Every vote a win and the title proceeds to the encode.  Every vote a loss and it goes to quarantine with no encode spent on it.  Votes in both directions go to hold, behind the Compare button, and so does a pair on which no gate voted at all.  Nothing resolves a split verdict automatically, because there is no correct automatic answer for a file that is better in one respect and worse in another.  The two exits from that hold are Force through and Discard, and both are yours.

Gate 1 is asymmetric and is the only short circuit.  An arrival that LACKS HDR or Dolby Vision the incumbent carries is an immediate loss and no further gate runs.  An arrival that GAINS it casts an ordinary win vote and can be contradicted into review.

Gate 2 defers to gate 3 whenever either side carries baked-in bars, because display pixel count counts black as picture.  A correctly cropped 1920x800 arrival and an incumbent stored 1920x1080 with 280 px of bars hold identical real picture, and gate 2 on its own would decide that on a margin that is entirely black.

Gate 6 is comparative only.  There is no minimum bitrate anywhere in this pipeline and none is to be added.  Raw figures are weighted by codec first, h264 at 1.0, HEVC at 1.7 and AV1 at 2.2, because without the weighting the gate systematically favours the less efficient codec:  a surviving h264 source would rate above the HEVC this pipeline produced from it, and the pipeline would quarantine its own output.

Gate 7 is a tiebreak rather than a vote.  Pedigree is inferred from release naming, which is exactly the kind of signal the rest of this pipeline distrusts, so it is consulted only when no measurable gate voted and it is marked as a weak signal when it decides.

Gates 2, 3 and 6 need both sides to be measurable.  Where one side is missing, the gate casts no vote and the skip is recorded rather than counted as a tie.  The tolerances are 5 percent on pixel count and 25 percent on bitrate;  the bitrate figure is a starting point and has not been calibrated against this library.

The table behind the Compare button carries every attribute the pipeline measured, not only the seven it gates on.  A row that differs with no gate against it is marked as such, and that is the row worth looking at:  it is where the pipeline saw a difference and had no rule for it.

A library incumbent is read, compared against and left alone.  Nothing in the container writes to a library.  An incumbent in 'complete/' that loses is retired to 'complete/.quarantine' when the winner publishes, its record marked as superseded by the winner, so one folder never holds two copies of one cut.

Two arrivals of one title in the same batch never both encode.  The later one still compares against the incumbent, then against the earlier arrival's own file, and holds at COMPARED naming the earlier, its stage and the pair verdict;  its detail carries Compare for the incumbent table and Compare sibling for the pair.  The pair verdict decides nothing on its own.  Discard one, Retry the other, and the survivor runs against the incumbent alone, or against the other's published output in 'complete/' if it went through.

HDR is compared on presence at gate 1, and on declaration in the table.  A Matroska file states its mastering display and content light level twice, in the bitstream as SEI and in the container's Colour element, and the two can disagree:  eight HDR titles in one library all carried the metadata in the bitstream while three declared none of it in the container.  The pipeline probes both surfaces, repairs a container that under-declares its own bitstream with a header edit on the way through, and holds any title whose output declares less than its source carried.  A content light level of zero and zero, an encoder's way of saying not indicated, is written like any other and read back through mkvmerge, because ffprobe reports a Matroska content light element only when both values are non-zero.  The declaration rows appear in the Compare table without a gate number, so a difference there is one of the marked rows worth looking at rather than a vote.

## Identification and the internet

A provider ID is never guessed.  Resolution goes through Wikidata and then verifies against the TMDB or TVDB page before an ID is written anywhere, because Wikidata's provider IDs can be flat wrong.  Movies use tmdbid and imdbid;  television uses tvdbid and tmdbid, since TVDB governs episode titles and numbering.  Requests are spaced about three seconds apart, and every answer is cached on disk under 'config/cache', which is consulted before any request is made.

A name with no year that resolves to two verified entities at the same score ('Space Battleship Yamato', 1977 and 2010) holds with both listed rather than taking the first;  a year in the name settles it.  Every probed file also records its OpenSubtitles hash, for a lookup by hand;  the pipeline does not query the service.

A file that has already been through this pipeline, or that came back out of a library, states what it is:  embedded tags, ids in the filename, ids in the folder, ids in the library folder a repair copy came from, the segment title and the cleaned filename are every one consulted.  Television runs the same ladder in the same shape, with the COLLECTION block, the show folder and the origin folder beside the show name.  A fresh disc rip has none of those, so for that case the filename is all there is.  Every source that resolves to a complete identity casts a vote:  one vote is enough, agreement is recorded, and sources that name different films hold the title with each one listed for the operator to pick from.  A source that resolves to an entity with no provider ids, a segment title a release group filled with its own name for instance, is listed but never outvotes a source that resolved properly.

An id found on any of those rungs is a pointer, not an identity.  It is looked up on Wikidata, and the entity supplies the title, the year and the other id;  the TMDB or TVDB page is then checked by its own title and year against the entity's label and aliases.  Nothing on disk becomes a tag:  a folder written before the naming rules changed, or a tag block written by an earlier tool in filename form, is corrected to the provider's title on the way through.  An identity that is still missing a field holds with the field named rather than publishing a folder with 'None' in it.

A filename has already lost the provider's punctuation, and Wikidata's prefix search stops at a colon, so a search is matched under the naming rules rather than by string:  every candidate's label is put through the same transform the filename went through, a full-text search covers the entities the prefix search cannot reach, and a candidate whose release year is more than a year from the name's is skipped.  'Star Wars Episode IV A New Hope' and 'Futurama Bender's Game' both resolve from their filename form.

Episodes are matched by title against the provider's list and the SNNENN is derived from the match, never read out of the source filename.  Release groups renumber when they collapse a two-part episode into one file, and everything after it silently shifts.  The release tag and group suffix are stripped first ('Terra Nova (1080p x265 10bit Joy)' matches 'Terra Nova'), a bare 'Part 2' lands on its own half of a two-parter, a catalogue title that starts the file's title matches by containment, the segment title is tried when the file name fails, and a fuzzy fallback covers the typos scene filenames carry.  A file matching neither exactly nor fuzzily falls back to source numbering with a warning, taking the episode title from the provider's entry for that number rather than from the file name, and a title that cannot be identified at all holds.

Every answer is cached without expiry, an empty search result included, so a title held for an unresolvable name would hold again identically on any requeue.  Retry on a held title therefore asks the providers again, bypassing the cache for that one identification;  Force through does not, and carries the title on without an ID.

THERE IS NO OFFLINE MODE, and this is the one that reads as a hang.  A container with no outbound access cannot identify anything, so every title holds on the backoff described above and then waits for a person.  That is the intended behaviour rather than a fault, but it is worth knowing before pointing this at an isolated network.

A title that cannot be identified holds, and Force through carries it on without an ID rather than guessing one.  A forced unidentified title keeps the name it arrived with, extension changed to '.mkv', carries a tag block holding TITLE only, and lands flat in 'complete/' instead of in a provider-named folder, so it is visibly unlike finished work.

Cover art is a by-product of the same lookup.  The poster comes off the TMDB page already fetched to verify the title, with TVDB as the fallback for a show that resolved without a TMDB id, and there is no API key involved.  Posters are cached under 'config/cache/posters' and served from '/api/poster/<hash>', keyed on the artwork URL rather than the title so a long browser cache header stays honest across a store wipe, and the browser never contacts an image CDN and the dashboard renders on a LAN with no internet once a poster is cached.  A fetch that fails serves 404 and the tile falls back to text;  artwork is never allowed to become a failure the pipeline notices.

## Library audit

A background sweep over the mounted libraries, looking for every deviation the passthrough path already corrects and nothing that needs an encode:  a container that is not Matroska, foreign tracks, wrong default flags, a missing or flattened tag block, a folder or file name that differs from what the tag block would produce, a path component that breaks a naming rule, a wrong segment title, missing statistics, and an HDR declaration short of the bitstream.  The names are built from the tag block by the same functions the publish step uses, so a folder that predates the current transform, a show folder carrying the wrong ids, an unpadded season folder and a mis-numbered file are all findings;  a tag that is itself wrong, with names that agree with it, is not, because the audit never consults a provider.  Resolution, bit depth, codec and letterbox are never findings.  One row is a listing rather than a defect:  a single-numbered episode with no next episode beside it and a video duration at least 'range_duration_ratio' times its season's median is reported as two episodes in one file, with the range name the pipeline would give it, and carries no Import action.

It is throttled at AUDIT_INTERVAL seconds per file and skips files whose size and modification time it has already seen, so a first pass over a few thousand files takes a couple of hours and a repeat pass takes seconds.  That skip is what keeps the hourly pass cheap, and it also means a change to the checks never reaches a file that has not changed on disk:  'Rescan entire library' in the findings dialog wipes the findings and runs a first pass again.  It never starts when no library is mounted.

Repair is by running the file through the pipeline.  Each finding carries an Import action that copies the library file into 'import/', after which the ordinary chain remuxes, strips, repairs, tags and verifies it and leaves the result in 'complete/' for you to move into the library by hand.  A copied title skips the comparison against the file it came from and nothing else.  The copy refuses when the root lacks the space, when the name is already in 'import/', or while a title for that file is in the pipeline, which includes a published copy you have not yet moved into the library.  The copy runs in the background with GB copied of the total under the finding's buttons, and once it is in 'import/' the button reads In Pipeline, clickable through to the title once the watcher has picked it up, until the repaired file is in the library and the next audit pass clears the finding.

## One instance at a time

The container takes an exclusive 'flock' on MEDIA_CONFIG/procrustes.lock at startup.  A second instance pointed at the same mounts waits for the first to exit rather than running alongside it, because two instances would sweep each other's encode area and could publish the same title twice.  The kernel releases the lock if the holder is killed, so a hard kill needs no manual cleanup.

Encode job directories record their owning PID.  The startup sweep reclaims only directories whose owner is gone, and leaves a live job alone.

## Web UI

HTTPS only, on WEB_PORT, which defaults to 443.  There is no HTTP listener and no redirect.  The certificate and key come from the '/certs' mount and are never generated:  if they are missing or unreadable the container logs the reason and exits rather than starting without TLS.

A login in front of everything.  The first visit to a fresh store asks 'Require Authentication?'.  No opens the dashboard and the API to anyone who can reach the port, marked by an 'AUTH OFF' pill in the header and a warning at startup;  Yes takes a username and an authentication method and creates the first account.  Three methods per user:  password only, OTP only (a Google Authenticator or any RFC 6238 app, enrolled from a QR code or the secret typed by hand), or MFA with both.  Passwords are argon2id;  a code is accepted once;  five failures in fifteen minutes lock the username and the address for the rest of the window.  The switch, the session lifetime, the login mode, the password, the authenticator and the user list are all on the User Settings page under the menu.  Without a session every '/api/' route but the four below answers 401 and every page shows the login form;  the session cookie is Secure, HttpOnly and SameSite=Strict.

```
GET  /                            dashboard, or the login form without a session
GET  /settings                    Application Settings, likewise
GET  /account                     User Settings, likewise
GET  /api/health                  {ok, version}, the only status readable without a session;  the Docker healthcheck
GET  /api/login                   {enabled, setup}:  whether authentication is on and whether the first account is still to be created
POST /api/setup                   first run only:  {"require": false} switches authentication off;
                                  {"require": true, username, mode, password, secret, code} creates the first account and signs in
POST /api/setup/totp              first run only:  {username} returns {secret, uri, qr} for the enrolment
POST /api/login                   {username, password, code};  401 on any failure, 429 with Retry-After when locked out
POST /api/logout                  ends the session
GET  /api/account                 {enabled, username, mode, has_password, totp_enrolled, totp_pending, modes, users}
POST /api/account/auth            {enabled, password | code};  the only writer of auth_enabled, a factor needed to turn it off
POST /api/account/mode            {mode, password | code}
POST /api/account/password        {current | code, new};  other sessions are signed out
POST /api/account/totp/enrol      returns {secret, uri, qr};  nothing is enabled until confirmed
POST /api/account/totp/confirm    {code}
POST /api/account/totp/disable    {password | code};  refused while the login mode needs it
POST /api/users                   {username, password} adds a user in password mode
POST /api/users/<name>/delete     refuses the caller's own account, so one account always remains
GET  /api/status                  version, config, GPU state, encode space, stage counts, the depth of
                                  the assessment queue and each encoder pool's queue, active
                                  threads per pool, uptime, audit status, whether authentication is
                                  on and who is signed in, and for every running encode its frame,
                                  total_frames, fps and eta_s
GET  /api/titles                  every title, each with its queue_position, locked and slot
POST /api/queue                   {"order": [{"id": n} | {"show": name}, ...]}, the whole unlocked queue in the
                                  order wanted;  400 when it omits a queued title, repeats one, or names
                                  one a pool thread is working
GET  /api/titles/<id>             one title with its stage history and comparison table
GET  /api/held                    the decision queue, held and failed titles together
GET  /api/poster/<hash>           cached cover art by the 'poster' hash on a title, 404 when there is none
GET  /api/logs                    log tail
GET  /api/settings                every setting with its value, default and source, grouped, plus the pool targets
POST /api/settings                {"set": {key: value}} and/or {"reset": [keys]};  400 with {"errors": {key: why}} writes nothing
GET  /api/audit                   sweep status and every library finding
POST /api/audit                   start a sweep now;  {"rescan": true} wipes the findings first
POST /api/audit/<id>/import       copy that finding's file into import/ for repair, in the background
POST /api/held/<id>/decision      {"action": "retry" | "override" | "discard" | "forget"}
```

A menu at the top right, behind a hamburger, carries the output codec per kind, the GPU state, free space in the encode area and whether a library is mounted, then Application Settings, User Settings and Sign out;  each page's menu links the other two.  A DRY_RUN badge stays in the header itself, and an AUTH OFF badge beside it while authentication is off.

User Settings opens with the Access group:  the authentication switch, which asks for the current password or a fresh code before it turns off, and the session lifetime.  Below it, for the signed-in account:  the login mode as three options with any option the account cannot satisfy yet greyed out and the reason beside it, a password change, the authenticator with Enrol (a QR code and the secret as text, confirmed by the first code) or Remove, and the user list with Add and Remove.

The dashboard is a pipeline rather than a table.  Queue on the left, Encoding and Held as the two parallel paths out of it, Ready to promote on the right, and counters in the lower right:  library findings, with a scanning line beneath it while a pass runs, then quarantined files and failed jobs.  Each title is a cover art tile;  a title that has not been identified yet, or that was held before identification, shows its filename on the same footprint instead.  A season of television collapses to one tile per show with an episode count, and clicking it lists the episodes.

The Queue reads in processing order, left to right then down.  A title a pool thread is already working sits first, locked, labelled with its stage in the corner of its art and carrying the encode's progress;  with two encoder slots that is the first two tiles.  Every other tile carries its position number, a show's tile the range its episodes occupy, and can be dragged into a new place;  the order is saved when the tile is dropped and every worker takes its next title from it.  A show moves as a block and its queued episodes stay together.  A new arrival, and a title returning through Retry or Force through, joins the back.

Clicking any tile opens its detail:  stage, provider ids, every reason it stopped where it did, both paths, and the full stage history.  A held title's detail carries Retry, Force through and Discard;  a failed title's carries Retry and Discard, with a line saying why Force cannot apply.  The three counters are clickable and list what is in them:  library findings, quarantined files and failed jobs, the first with a Details table per file, an Import action per row, and Sweep now and Rescan entire library in its header.

A television tile opens the episode list instead.  Every row carries the same decisions as a tile, and the header carries them for the whole season at once, so clearing a held season is one action rather than one per episode.  A season action closes the list, because there is nothing left in it to show.

Where a title was compared against an incumbent, the detail shows a Compare button, and where it was held behind an earlier arrival of the same title a Compare sibling button beside it, the same table with the other arrival in the second column.  It opens a table of every attribute the pipeline measured on both files, side by side, with the published output as a third column once it exists.  Rows that a comparison gate acted on carry their gate number, every row whose gate cast a vote is marked, and a row that differs without any gate acting on it is marked too:  that is the case worth looking at, because the pipeline saw a difference and had no rule for it.

## Build and validate

While writing code, validate the code itself:

```
python3 -m compileall -q app
python3 -c "import app.main"
python3 -m app.check_names
```

That is the whole of local validation.  The second needs 'argon2-cffi' and 'qrcode' importable on the workstation, the two modules 'app/auth.py' imports;  a scratch venv with 'pip install argon2-cffi==25.1.0 qrcode==8.2' is enough.  The third reports any name a function loads that its module never defines, which the first two cannot see.  None of the commands executes a pipeline stage, touches a file or opens a socket.  There is no local test suite:  a workstation and this container are different environments, so functionality is validated in the container and nowhere else.

```
docker build -t procrustes:local .
```

The image build fails if ffmpeg lacks libx265, libsvtav1 or av1_qsv, if its libx265 wrapper has no '-dolbyvision' option, or if 'argon2' or 'qrcode' does not import.  Those checks are deliberate:  they stop the image shipping while claiming encoders or capabilities it does not have.  If one ever fails, change where ffmpeg comes from rather than deleting the check.  The escalation order is av1_vaapi, then a pinned ffmpeg from Alpine's edge community repository.

The image is Alpine 3.24, everything from Alpine's own repositories, including the two Python modules the login uses, 'py3-argon2-cffi' and 'py3-qrcode';  the build gate asserts both import.  Those two are the only third-party Python code in the image, and the CI validate job installs the same two from PyPI so 'import app.main' runs on a bare runner.  Every build increments the version in 'app/__init__.py', and the image is tagged with it.

Functionality is validated against the built container by hand, measuring outcome:  files, filenames, tag blocks, track lists, API responses, exit codes and health state.

Run it against a throwaway tree first:

```
docker run --rm -e PUID=1000 -e PGID=1000 -e DRY_RUN=1 \
  -e LOG_LEVEL=info \
  -v /tmp/testtree:/media \
  -v /srv/certs:/certs:ro \
  -p 443:443 procrustes:local
```

ONE ROOT MOUNT, DELIBERATELY.  'import', 'complete', 'complete/.quarantine' and 'hold' are created underneath it at startup, and a move between two of them is then a rename rather than a copy.  Mounting them individually turns every one of those moves into a copy at best;  at worst 'rename' refuses outright with EXDEV, because Linux will not rename across two mount points even when both sides are the same device.

DRY_RUN logs every intended move, encode and quarantine and performs none of them.

## Deploying

Copy '.env.example' to '.env', fill in the host paths, the certificate directory and RENDER_GID, then:

```
docker compose up -d
```

CI does not build on push.  The workflow is manual only, started from the Actions tab, so a commit or a tag publishes nothing on its own.

## Version

Current version 0.13.2, defined once in 'app/__init__.py' and consumed by the provider User-Agent, the startup log, '/api/status' and the image tag.  Every build increments it.

'x.0.0' is a release, '0.x.0' is a minor update or bug fix, and '0.0.x' is a pre-release.  The repository carries no git tags;  the version on the image and its label is the record.  Builds are manual runs of the workflow and nothing else triggers one.

## Licence

GPL-3.0.  See LICENSE.
