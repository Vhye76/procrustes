# TESTPLAN

Execution plan for validating procrustes against a built container.  Run by hand.

## What this is

This plan measures OUTCOME.  Every case asserts on something the container produced or did:  a file at a path, a filename, a tag block, a track list, an API response, an exit code, a container health state.  No case reads a log line and no case depends on a log level.

Every case is repeatable.  Each states its starting state, its input and its command, so the same case run twice from the same starting state gives the same result.

Nothing here runs on a workstation.  The workstation and the container are different environments and a result from one says nothing about the other.

## Preconditions

Before any case runs:

```
image built                 docker build -t procrustes:local .
root mounted                MEDIA_ROOT, the one required writable mount
certificate mounted         /certs holds the certificate and key
RENDER_GID set              stat -c %g /dev/dri/renderD128 on the host

MEDIA_ENCODE and MEDIA_CONFIG are optional.  Cases that need them mounted separately say so.
```

'LOG_LEVEL' is not set by this plan and no case depends on it.

DRY_RUN is off unless a case says otherwise.

## Recording

Record the observed value for every case, not just pass or fail.  A run that records "PASS" against 13 groups tells you nothing when the next run differs.  A run that records the filename produced, the byte-sum ratio, the chapter count and the resolved gate can be compared against the run before it.

## Reset between cases

Unless a case says otherwise, reset by stopping the container, emptying import, encode, complete and hold, and leaving config in place.  Cases that require an empty state database say so.

---

## 1.  Startup

**T-01  The banner lists every setting.**

- **Start:**  container stopped.
- **Do:**  'docker compose up -d', then 'docker logs procrustes'.
- **Expect:**  the config banner lists every name in section 5 of CLAUDE.md.  Record any name present in one and absent from the other.

**T-02  Mounts resolve to the paths given.**

- **Start:**  container running.
- **Do:**  'curl -sk https://localhost/api/status | jq .config'.
- **Expect:**  the five MEDIA_ paths match the container paths in the compose file.

**T-03  The GPU probe reports a result.**

- **Start:**  container running with RENDER_GID set.
- **Do:**  'curl -sk https://localhost/api/status | jq .gpu'.
- **Expect:**  'available' true with a non-empty 'av1_encode_profiles', or 'degraded' true with a 'reason' naming the cause.  Both are valid outcomes;  record which.

**T-04  A missing root refuses to start.**

- **Start:**  container stopped, the MEDIA_ROOT volume removed from the compose file.
- **Do:**  'docker compose up', read the exit.
- **Expect:**  the container exits non-zero and the message names MEDIA_ROOT.

**T-04a  The optional mounts are genuinely optional.**

- **Start:**  container stopped, only MEDIA_ROOT and /certs mounted.
- **Do:**  'docker compose up -d', then read '/api/status'.
- **Expect:**  a clean start.  'encode' and 'config' are created as subdirectories of the root and the banner shows them there.

**T-04b  A retire is a rename when everything is under one root.**

- **Start:**  the collapsed layout from T-04a, a resolvable title in import.
- **Do:**  run it to CLEANUP, then compare the inode of the source before and of the quarantined file after.
- **Expect:**  the same inode.  A rename, not a copy.

**T-04c  A retire still works when encode is on another pool.**

- **Start:**  MEDIA_ENCODE mounted on separate storage.
- **Do:**  run a title to CLEANUP.
- **Expect:**  publish copies, retire renames, both succeed, and no zero-byte file is left in quarantine.

**T-05  A missing certificate refuses to start.**

- **Start:**  container stopped, '/certs' mounted empty.
- **Do:**  'docker compose up'.
- **Expect:**  the container exits non-zero.  No listener is bound on 443.  Confirm with 'curl -k https://localhost/' failing to connect.

**T-06  The instance lock is taken.**

- **Start:**  container running.
- **Do:**  'ls -l <config mount>/procrustes.lock'.
- **Expect:**  the file exists and contains a JSON object with the running container's pid.

**T-07  The healthcheck reaches healthy.**

- **Start:**  container started within the last two minutes.
- **Do:**  'docker inspect --format "{{.State.Health.Status}}" procrustes'.
- **Expect:**  'healthy'.  Not 'starting', not 'unhealthy'.

## 2.  Detection

**T-08  Partial files are ignored.**

- **Start:**  empty import.
- **Do:**  place 'Movie.mkv.part', wait two poll intervals.
- **Expect:**  '/api/titles' stays empty.

**T-09  Dotfiles are ignored.**

- **Start:**  empty import.
- **Do:**  place '.hidden.mkv', wait two poll intervals.
- **Expect:**  '/api/titles' stays empty.

**T-10  A stable file is detected on the second poll.**

- **Start:**  empty import, empty state.
- **Do:**  place a valid mkv, wait one poll interval, check '/api/titles', wait a second interval, check again.
- **Expect:**  absent after the first, present after the second.

**T-11  A growing file is not detected.**

- **Start:**  empty import.
- **Do:**  place a file, append to it between two polls.
- **Expect:**  '/api/titles' stays empty until it stops growing.

## 3.  Minimum standards

Each case:  place the described file, wait for it to reach a terminal state, then read '/api/held'.

- **T-12  An SD movie holds.**  A 720x480 movie.  Expect HELD with a reason naming the resolution floor.
- **T-13  A short movie holds.**  A 10 minute movie.  Expect HELD naming the 40 minute floor.
- **T-14  A short episode holds.**  A 5 minute episode.  Expect HELD naming the 15 minute floor.
- **T-15  Foreign-only audio holds.**  A file whose only audio is 'fra'.  Expect HELD naming the audio language.
- **T-16  A PAL speed-up holds.**  720x576 at 25 fps.  Expect HELD naming the PAL speed-up.
- **T-17  Undetermined audio is accepted.**  A file whose only audio is 'und'.  Expect it proceeds past SCREENED.
- **T-18  SD television is accepted.**  A 720x480 episode.  Expect it proceeds past SCREENED.
- **T-18a  The crop floor is 20 px.**  One source with bars between 10 and 19 px and one with bars above 20 px.  Expect the first published uncropped and reporting 'letterbox_px' 0, and the second cropped.  A 24-file library sample found the 10 to 19 px band empty, so the first file has to be constructed.

- **T-19a  The override clears a comparison hold.**  Take a title to HELD on a contradictory comparison, press Force through, and expect it to pass COMPARED with the detail naming the override and continue into the encoder rather than holding again.  Assert on the stage history.
- **T-19b  An overridden title skips the incumbent work.**  For the same run, assert no comparison object was recorded.  Its absence is what proves the gate was skipped rather than recomputed, which a verdict alone cannot show.
- **T-19c  The override does not survive a re-import.**  Drop the same source back into 'import/' afterwards and expect it gated normally on the fresh run.

**T-19  The override forces a held title through.**

- **Start:**  a title held by T-12.
- **Do:**  'curl -sk -X POST https://localhost/api/held/<id>/decision -d {"action":"override"}'.
- **Expect:**  the title leaves HELD and advances past SCREENED.

## 4.  Identification

**T-20  A resolvable title records its IDs.**

- **Start:**  a correctly named film in import.
- **Do:**  wait for IDENTIFIED, read '/api/titles/<id>'.
- **Expect:**  'tmdb' and 'imdb' populated, 'title' set to the provider's title.  Record all three.

**T-21  An unresolvable title holds rather than guessing.**

- **Start:**  a file named so no provider match exists.
- **Do:**  wait for a terminal state.
- **Expect:**  HELD.  No provider ID is recorded.  Record the reason.

**T-22  An unreachable provider retries then holds.**

- **Start:**  container running with outbound network blocked and an empty provider cache.
- **Do:**  place a valid file, wait.
- **Expect:**  the title retries, then reaches HELD after the retry ladder is exhausted.  Record the attempt count from '/api/titles/<id>'.

**T-76  An embedded MOVIE tag identifies without a Wikidata search.**

- **Start:**  empty import.
- **Do:**  copy a published library file into import.
- **Expect:**  resolved, and '/api/titles/<id>' stage detail reads "resolved <title> from embedded tag".

**T-77  A '[tmdbid-N]' folder identifies from the path.**

- **Start:**  empty import.
- **Do:**  place a file inside a folder named 'Title (Year) [tmdbid-N] [imdbid-ttN]', tags stripped.
- **Expect:**  resolved, detail reads "from folder ids".

**T-78  A segment title identifies when nothing else does.**

- **Start:**  empty import.
- **Do:**  place a file whose name is meaningless but whose segment Info title is the real title.
- **Expect:**  resolved, detail reads "from segment title".

**T-79  A nameless file still holds.**

- **Start:**  empty import.
- **Do:**  place a file named 'futurepack-cls.mkv', no tags, no segment title.
- **Expect:**  HELD, reason "provider ID could not be resolved and must never be guessed".

**T-80  A wrong embedded ID is rejected and the ladder continues.**

- **Start:**  empty import.
- **Do:**  place a file whose MOVIE tag carries a TMDB id belonging to a different film, with a correct filename.
- **Expect:**  the embedded rung is rejected, the filename rung resolves, detail reads "from filename".

**T-81  A file in the watched root does not use the parent folder.**

- **Start:**  empty import.
- **Do:**  place an unidentifiable file directly in 'import/'.
- **Expect:**  HELD.  No lookup is ever attempted for the term "import".

**T-82  The release year is the release, not the placeholder.**

- **Start:**  empty import, empty provider cache.
- **Do:**  identify Futurama: Into the Wild Green Yonder.
- **Expect:**  year 2009, not 2008.  Published folder reads '(2009)'.

**T-119  A library repair copy identifies from its origin folder.**

- **Start:**  a movie finding whose library file carries no tag block and no segment title, and whose folder carries '[tmdbid-N] [imdbid-ttN]'.
- **Do:**  press Import on the finding.
- **Expect:**  detail reads "from origin folder ids", the debug log showing one 'ids:' search per id and no name search, the published folder carrying the same two ids as the library folder, and the tag TITLE equal to the Wikidata label rather than the folder text.

**T-120  A colon title resolves from its filename form.**

- **Start:**  empty import, empty provider cache.
- **Do:**  place 'Star Wars Episode IV A New Hope (1977).mkv' directly in 'import/', no tags, no segment title.
- **Expect:**  resolved to tmdb 11, detail reads "from filename", and the debug log showing the two prefix searches empty and the 'text:' search scoring Q17738 at 1.000.

**T-121  A remake resolves to the right year.**

- **Start:**  empty import, empty provider cache.
- **Do:**  place 'Robocop (2014).mkv', no tags.
- **Expect:**  tmdb 97020, and an info line naming the 1987 entity as skipped for its year.

**T-122  Retry asks the provider again.**

- **Start:**  a title held for "could not be resolved" whose failing searches are in 'config/cache'.
- **Do:**  correct the cause upstream, then press Retry.  Note the cache file mtimes for the search URLs first.
- **Expect:**  an info line saying identification bypasses the provider cache, the search cache files rewritten, and the title resolving.  A second title held the same way and requeued by any path other than Retry keeps its cached answers.

**T-123  A show resolves with both ids.**

- **Start:**  empty import, empty provider cache.
- **Do:**  place 'Murder, She Wrote - S06E17 - Murder - According to Maggie.mkv' directly in 'import/', no tags.
- **Expect:**  tvdb 78049 and tmdb 484 on '/api/titles/<id>', the published folder reading '[tvdbid-78049] [tmdbid-484]', never '[tmdbid-None]'.

**T-124  A library repair copy of an episode identifies from its origin folders.**

- **Start:**  a TV finding whose library show folder carries '[tvdbid-N] [tmdbid-N]'.
- **Do:**  press Import.
- **Expect:**  detail reads "from origin folder ids", an 'ids:' search and no name search in the debug log, both ids and the show year on the row.

**T-125  A special matches by title.**

- **Start:**  empty import, empty provider cache.
- **Do:**  place 'Murder, She Wrote - S00E02 - South by Southwest.mkv'.
- **Expect:**  'match_method' exact on the identity, season 0 episode 2, no fallback-numbering warning in the log.

**T-83  DRY_RUN completes a title.**

- **Start:**  'DRY_RUN=1', a resolvable file in import.
- **Do:**  wait for a terminal stage.
- **Expect:**  CLEANUP, every stage logging intent, no file created anywhere, and no FAILED state.

**T-84  Discard on a missing source removes the row.**

- **Start:**  a held title whose source file has been renamed away.
- **Do:**  POST 'discard' against it.
- **Expect:**  the row is gone from '/api/titles', nothing written to quarantine, and the response action reads "forgotten".

## 5.  Comparison against the incumbent

Each case needs a prepared library file and a prepared incoming file that differ on exactly one gate.

- **T-23  Gate 1, losing HDR is a loss.**  Incumbent HDR, incoming SDR.  Expect QUARANTINED, incoming file present under 'complete/.quarantine'.
- **T-24  Gate 2, higher resolution wins.**  Incoming 2160p against 1080p incumbent.  Expect it proceeds.
- **T-25  Gate 3, larger picture area wins.**  Incoming without bars against a letterboxed incumbent.  Expect it proceeds.
- **T-25a  Gate 2 defers when either side carries bars.**  A correctly cropped 1920x800 incoming file against an incumbent stored 1920x1080 with 280 px of bars and a greater bit depth.  Expect QUARANTINED decided at gate 5 on bit depth, the comparison notes carrying the gate 2 defer, and the table showing 'letterbox_px' 0 against 280.  This is the Blade Runner 2049 pair from 2026-09-08.
- **T-25b  Gate 2 still decides when neither side carries bars.**  A 2160p incoming file against a 1080p incumbent, neither letterboxed.  Expect the verdict at gate 2 and no defer note.  Regression guard on T-25a.
- **T-25d  A contradictory pair holds instead of deciding.**  An incoming file better on bitrate and worse on bit depth.  Expect HELD, the comparison verdict 'ambiguous' with no single gate, and the votes list naming gate 5 for the incumbent and gate 6 for the incoming.  Expect nothing in quarantine.
- **T-25e  A unanimous pair still decides.**  An incoming file better on two gates and worse on none.  Expect the verdict recorded, the reason carrying the agreeing-gate count, and every voting row marked in the table.
- **T-25c  An equivalent pair behind bars holds rather than quarantining.**  The T-25a pair with both sides at the same bit depth.  Expect HELD as ambiguous, not QUARANTINED.  Before the defer this pair was discarded on black bars alone.
- **T-26  Gate 4, more audio channels wins.**  Incoming 5.1 against 2.0.  Expect it proceeds.
- **T-27  Gate 5, greater bit depth wins.**  Incoming 10-bit against 8-bit.  Expect it proceeds.
- **T-28  Gate 6, better pedigree breaks a tie.**  Identical but for release naming.  Expect it proceeds and the comparison notes record the weak signal.
- **T-29  A level pair holds.**  Two files identical on every gate.  Expect HELD, and '/api/titles/<id>' carries a comparison object.
- **T-29a  The incumbent is found by provider ID when the folder name has drifted.**  A library folder carrying the right '[tmdbid-N]' whose name does not match the current transform, for example an en dash title filed with no dash at all.  Expect the comparison to run and the COMPARED detail to name the ID route.  This is the Return of the Jedi case from 2026-09-08.
- **T-29b  The name fallback still works and is labelled.**  A library folder whose name matches the transform exactly and whose provider IDs are absent from the folder name.  Expect the comparison to run and the COMPARED detail to name the folder-name route.
- **T-29d  Gate 6 reads the BPS tag.**  Expect 'video_bitrate' populated on both sides of the comparison table, and the figure to match the video track's BPS tag read directly with ffprobe.
- **T-29e  Codec weighting applies.**  An HEVC incumbent against an h264 arrival whose raw bitrate is below it but above it once weighted at 1.7.  Expect gate 6 to vote for the arrival.
- **T-29f  An unmeasurable bitrate casts no vote.**  A source carrying neither a BPS tag nor NUMBER_OF_BYTES.  Expect gate 6 skipped with a note, and the verdict decided by the remaining gates rather than held.
- **T-29c  A genuine miss is distinguishable from a lookup failure.**  A title with no library counterpart.  Expect the COMPARED detail to report the number of folders scanned rather than a bare 'no incumbent' sentence.

**T-30  No library mounted skips the comparison.**

- **Start:**  compose without the library mounts.
- **Do:**  place a title that exists in the library.
- **Expect:**  it proceeds, and '/api/titles/<id>' records that the comparison was skipped.

## 6.  Routing

Each case:  place one title matching the gate, wait for ENCODED, then read the gate and encoder back from the ROUTED entry in the title's stage history on '/api/titles/<id>' and confirm the output file's codec.

```
T-31  gate 1   an hevc source           expect passthrough, output still hevc, not re-encoded
T-32  gate 1   an av1 source            expect passthrough, output still av1
T-33  gate 2   an SD episode            expect passthrough, output codec unchanged
T-34  gate 1   a Dolby Vision title     expect passthrough at gate 1, not gate 3, output carries the DOVI record
T-35  gate 4   OUTPUT_CODEC=av1, grainy expect libsvtav1, output av1
T-36  gate 5   OUTPUT_CODEC=av1, clean  expect av1_qsv, output av1
T-37  gate 6   a grainy source          expect libx265, output hevc
T-38  gate 7   a clean source           expect libx265, output hevc
```

**T-39  A passthrough title is still processed.**

- **Start:**  an SD AVI episode.
- **Do:**  wait for PUBLISHED.
- **Expect:**  the file in 'complete/' is '.mkv', not '.avi'.  It carries tags and track statistics.

**T-40  TV_ENCODE_SD re-enables SD encoding.**

- **Start:**  'TV_ENCODE_SD=1', an SD episode.
- **Expect:**  gate 2 does not fire;  the title routes to an encoder.

## 7.  Remuxing

- **T-41  An mp4 with bin_data remuxes.**  A HandBrake mp4 carrying a QuickTime chapter stream.  Expect PUBLISHED, and the chapter count in the output equals the chapter count in the source.  Record both.
- **T-42  An avi needing genpts remuxes.**  An Xvid avi.  Expect PUBLISHED, and the packet count in equals the packet count out.  Record both.
- **T-43  The segment title is set after remux.**  A scene-named mp4.  Expect the output's segment Info title is the provider title, not the release name.

## 8.  Language and flags

- **T-44  Foreign audio is dropped.**  A file with eng and fra audio.  Expect the output holds only eng.
- **T-45  Foreign subtitles are dropped.**  A file with eng and chi subtitles.  Expect the output holds only eng.
- **T-46  Undetermined tracks are kept.**  A file with an und audio track.  Expect it survives.
- **T-47  Exactly one default audio.**  A file with two audio tracks both flagged default.  Expect the output has exactly one.  Count it, do not test that one exists.
- **T-48  No subtitle default.**  A file with a defaulted subtitle track.  Expect the output has none.
- **T-49  A forced subtitle keeps its default.**  A file with a forced subtitle flagged default.  Expect it keeps the flag.
- **T-50  A file that grew is not a failure.**  A file with PGS subtitles that grows after the strip.  Expect PUBLISHED.  Record both sizes.

## 9.  Tagging

- **T-51  A movie carries a MOVIE block.**  Expect a MOVIE-targeted Tag with TITLE, TMDB, IMDB and DATE_RELEASED.  Verify by TargetType name, never by the number.
- **T-52  Television carries three levels.**  Expect COLLECTION, SEASON and EPISODE targets, each on its own Tag element.
- **T-53  No Simple Name contains a slash.**  Parse the tag XML from any published file.  Expect no Simple whose Name contains '/'.
- **T-54  Scraped metadata survives.**  A source carrying ACTOR, DIRECTOR and GENRE.  Expect those keys present in the output.
- **T-55  Statistics are non-zero.**  For every published file, sum NUMBER_OF_BYTES and divide by the file size.  Expect a ratio above zero.  Record it.

## 10.  Encode output

- **T-56  The output codec matches the routed encoder.**  Cross-check T-31 to T-38.
- **T-57  The output is 10-bit.**  Expect pix_fmt yuv420p10le on every encoded output.
- **T-58  Colour is correct for the source.**  Read the properties off the OUTPUT FILE, never off the command that produced it.  Four sources through the x265 path:  a tagged source keeps its own properties;  an untagged SD NTSC rip comes out smpte170m;  an untagged HD source comes out bt709;  an HDR source keeps bt2020.  Record all four.
- **T-58a  The colour mechanism differs by path and must not be generalised.**  Run the untagged HD source and the untagged SD rip through both AV1 paths.  Expect the same four outcomes as T-58, produced by the three ffmpeg colour flags rather than by the params string.  The x265 path must still pass none of those three flags.
- **T-59  Cover art is carried, not encoded.**  A source with an mjpeg poster.  Expect the output still carries an mjpeg attachment, not a second video stream.
- **T-60  Audio and subtitles pass through unchanged.**  Expect codec and channel count identical in and out.

- **T-60a  The published file carries a targeted tag block.**  Assert structurally on the file in 'complete/', not by grep:  every expected TargetType appears on its own Tag element, and no Simple Name anywhere contains a slash.  Run one movie and one episode.
- **T-60b  Scraped metadata survives the encode.**  A source carrying an untargeted block of ACTOR, DIRECTOR, GENRE and SYNOPSIS.  Expect every one of those keys present on the published file.
- **T-60c  Statistics survive the post-encode tag write.**  Record the byte-sum ratio on the published file.  Expect it above the floor and not zero.
- **T-60e  HDR declarations survive and are repaired.**  Three HDR sources through the passthrough path, built from one clip:  one whose container agrees with its bitstream, one whose container declares no mastering display or content light level, and one whose container declares different figures from its SEI.  Expect all three PUBLISHED with 'ffprobe -show_streams' on the output reporting mastering display and content light level matching the bitstream, the second and third with a REMUXED stage detail naming the repair, the first with none.  A fourth source carrying no mastering metadata on either surface must publish with nothing manufactured.
- **T-60f  A lost HDR declaration holds.**  Take an HDR title past REMUXED, strip the Colour element from the work file with mkvpropedit by hand, and let it continue.  Expect HELD at READY or VERIFIED naming the declaration the source carried and the output lacks.
- **T-60g  An HDR encode declares fully.**  An HDR source in a codec that is neither hevc nor av1, so it actually reaches libx265.  Expect the output to carry mastering display and content light level in both container and bitstream, matching the source's bitstream.
- **T-60h  Dolby Vision through a forced encode keeps its RPU.**  A DV title forced down the x265 path.  Expect '-dolbyvision 1' and the VBV pair in the debug argv, and the output carrying the DOVI configuration record with per-frame RPU data.  Then confirm the build gate:  an image whose libx265 wrapper lacks '-dolbyvision' must fail to build.
- **T-60d  Video packets are preserved through the encode.**  Expect the VERIFIED stage detail on '/api/titles/<id>' to carry a video packet figure equal to the source count.  Measured 2026-09-08:  an 89 minute encode preserved 128,424 packets exactly, so this is an equality and not a tolerance.  Then truncate an encoder output by hand before verification and expect the title HELD naming the packet mismatch rather than published.

## 11.  Publishing

- **T-61  A movie lands in the documented shape.**  Expect 'Title (Year) [tmdbid-N] [imdbid-ttN]/Title (Year).mkv'.
- **T-62  An episode lands in the documented shape.**  Expect 'Show (Year) [tvdbid-N] [tmdbid-N]/Season NN/Show - SNNENN - Title.mkv'.
- **T-63  A duplicate does not overwrite.**  Drop the same film twice under different release names.  Expect two files, the second suffixed, and the first byte-identical to what it was.
- **T-64  Quarantine naming does not collide.**  Quarantine two files of the same name.  Expect both present, the second suffixed.
- **T-65  The source is retired.**  After PUBLISHED, expect the original file gone from import and present under 'complete/.quarantine'.

## 12.  Concurrency

**T-66  A second container waits.**

- **Start:**  one container running.
- **Do:**  start a second against the same mounts.
- **Expect:**  the second does not process anything and reports waiting.  The first keeps its lock.

**T-67  SIGTERM terminates encodes.**

- **Start:**  a container with an encode in flight.
- **Do:**  'docker stop procrustes'.
- **Expect:**  no ffmpeg process survives.  Confirm on the host.

**T-68  An orphaned job directory is swept.**

- **Start:**  container stopped, a directory in the encode mount holding an owner file naming a dead pid.
- **Do:**  start the container.
- **Expect:**  the directory is gone.

**T-69  A live job directory is not swept.**

- **Start:**  container running with a job in flight.
- **Do:**  start a second container, let it wait, then stop the first.
- **Expect:**  the in-flight job's directory is intact when the second takes over.

## 12a.  Comparison measurement and the state record

- **T-70a  A speed-up is reported even though no gate acts on it.**  An incumbent that is a 25 fps speed-up of the same master as the incoming file.  Expect the comparison notes to name the differing frame rates at equal frame counts, and the UI row for frame rate to be marked as a difference no gate acted on.
- **T-70b  Gate 3 fires with real numbers.**  An incumbent that is genuinely letterboxed against a correctly cropped incoming file.  Expect picture pixels populated on BOTH sides and the gate to decide, rather than the 'cropdetect not run on both sides' note.
- **T-70c  The published output is the third column.**  Open the Compare dialog on a completed title.  Expect a Published column whose values match what ffprobe reports on the file in 'complete/'.
- **T-70d  The dialog renders untrusted text as text.**  A source filename containing '&', '<' and a double quote.  Expect the characters displayed literally and no broken markup.
- **T-70e  A removed file leaves a record that can be cleared.**  Take a title to CLEANUP, collect its published output out of 'complete/' by hand, delete its quarantined source by hand, and refresh.  All three of output, source and quarantine copy must be absent before the row counts as gone.  Expect the row marked 'files gone' and a Forget button.  Press it and expect the row to disappear.  Confirm no file was deleted by the container.
- **T-70i  A quarantined title is not reported as gone.**  Quarantine a title through a comparison loss.  Expect the row marked 'files quarantined' rather than 'files gone', no Forget button, the row not dimmed, and a forget POST refused with the message naming files still on disk.  Confirm the file is present under 'complete/.quarantine' at its full size.
- **T-70f  A re-imported source is processed, not skipped.**  After T-70e, drop the same source back into 'import/' under the same filename.  Expect it detected and processed rather than silently ignored.
- **T-70g  A source still in flight is not processed twice.**  While a title is at ENCODING, confirm '/api/titles' holds no second row for its path and that the original row keeps its stage.  The skip itself is a debug line and is not asserted on.
- **T-70h  The log records the encode and not the polls.**  Run one title to CLEANUP at the default 'info' level with a second title in flight.  Expect one ENCODED line and one VERIFIED line, each naming the title id, and no 'already claims this path' line at info.  This is the one case that reads the log, and it exists because the defect it guards was the log itself.

## 12d.  Assessment ahead of encoding

- **T-108  A batch is assessed before its first encode finishes.**  Drop a dozen mixed titles at once, several failing screening.  Expect every screening failure in HELD within the first minute, each with its full reason list, while the first encode is still running.
- **T-109  Passthrough does not wait behind an encode.**  With a CPU encode running, drop an hevc source.  Expect it ROUTED, staged, verified and PUBLISHED on the passthrough pool while the encode continues.
- **T-110  Staged copies are bounded by the pools.**  During T-108, count job directories in the encode area.  Expect at most 'CPU_SLOTS + GPU_SLOTS + 1' at any moment, never one per assessment worker.
- **T-111  Status reports the queues.**  'GET /api/status' carries 'queues' with 'assess', 'cpu', 'gpu' and 'passthrough' depths and 'slots' with the active count per pool.
- **T-112  Resume lands on the right queue.**  Restart mid-encode.  Expect the encoding title to resume on the CPU pool, a ROUTED passthrough on the passthrough pool, and a title interrupted before ROUTED to be re-assessed;  a title past ROUTED with no stored decision returns to DETECTED with a history entry saying so.
- **T-113  A degraded GPU routes to the CPU at assessment.**  'RENDER_GID' unset, 'OUTPUT_CODEC=av1', a clean source.  Expect ROUTED to name libsvtav1 on the CPU with the "GPU unavailable" note, and nothing left waiting on the GPU queue.
- **T-114  Grain probes are serialised.**  Drop three grain-heavy sources together with an encode running.  Expect the three probes to run one after another in the log, not overlapping.
- **T-115  'CPU_SLOTS=2' runs two CPU encodes.**  Expect two titles ENCODING at once, each with 'pools=4' in the debug argv, three staged copies at most, and the queue draining at the same rate as with one.

## 12b.  Reasons and force

- **T-94  Every assessment reason is collected.**  A file that fails the resolution floor and has an unresolvable name.  Expect one HELD with two reasons on '/api/titles/<id>', one from SCREENED and one from IDENTIFIED, and the detail dialog listing both.  One Force through, then expect it published.
- **T-95  A forced unidentified title keeps its name.**  Continuing T-94:  expect the output flat in 'complete/' as '<source stem>.mkv', a single MOVIE or EPISODE tag carrying TITLE only, the segment title matching, and no provider-named folder.
- **T-96  Verification collects, it does not stop at the first failure.**  Arrange an output failing three of the four VERIFIED checks.  Expect all three named on the title, not one.
- **T-97  Force records every gate it bypassed.**  Force a title past readiness or verification.  Expect the stage history to carry a 'forced past' entry for each, and the dashboard to show it.
- **T-98  Force publishes beside a collision, never over it.**  Force a title whose destination already exists.  Expect both files present and the new one under a unique name, with the PUBLISHED detail saying so.
- **T-99  Force cannot apply to FAILED.**  A file whose ffprobe fails outright.  Expect FAILED, 'forceable' false on '/api/titles/<id>', the detail dialog stating why, and POST 'override' refused with a 4xx.
- **T-100  A transient provider failure is not swept into a hold.**  Block outbound access and import a title.  Expect the backoff retries, not a batched HELD carrying the provider error as a reason.

## 12c.  The library audit

- **T-101  The sweep reproduces the known findings.**  With the libraries mounted, wait for a first pass.  Expect 'GET /api/audit' to list exactly the HDR titles whose container under-declares the bitstream and none whose container agrees, and every other check clean on files that meet the standard.
- **T-102  A repeat pass is cheap.**  Time the second pass.  Expect it to finish in seconds, with the log naming every file as unchanged.
- **T-103  The copy action feeds the pipeline.**  Import one finding.  Expect the file to appear in 'import/' under a '.part' name and be renamed, the watcher to detect it, the COMPARED detail to say the incumbent was its own origin and was skipped, and the published output to carry the repaired declaration.  The finding row must show the title as in the pipeline while it runs.
- **T-104  The copy refuses a duplicate.**  Import the same finding twice.  Expect the second refused with a 4xx naming the title in flight.
- **T-105  The copy refuses a full root.**  With the root near full, import a finding.  Expect a refusal before any bytes move and no '.part' file left behind.
- **T-106  The audit never starts without a library.**  Unset both library variables.  Expect no auditor thread, no counter on the dashboard, and 'audit.enabled' false on '/api/status'.
- **T-107  The dashboard renders the audit.**  Expect a third corner counter, the list dialog with the sweep status in its header, a Details table per finding with the differing rows marked, and an Import button that becomes an in-pipeline link once pressed.
- **T-116  The copy returns at once and reports progress.**  Import a finding of several GiB.  Expect 'POST /api/audit/<id>/import' to return within a second with 'copy started', the Import button greyed out, a bar beneath the buttons that advances while the '.part' in 'import/' grows, and 'copy.percent' on 'GET /api/audit' climbing to match.
- **T-117  The row reads In Pipeline through the detection gap.**  When the copy from T-116 completes, expect the button to read 'In Pipeline' and be disabled before the watcher has detected the file, then 'In Pipeline' and clickable to the title's detail dialog once detected, with '#N' and the stage name absent from the label.  After the title publishes and its source retires, expect the row still to read In Pipeline and 'POST /api/audit/<id>/import' to return 400 with 'already in the pipeline as title N at CLEANUP';  after the output is moved into the library and the next pass runs, expect the finding gone.
- **T-118  A failed copy is reported and retryable.**  Start a copy and fill the root or unmount the library while it runs.  Expect no '.part' left in 'import/', the finding row showing the failure text and an enabled Import button, and a second Import to start a fresh copy.
- **T-126  A folder without the transform's dash is a finding.**  A movie whose tag TITLE carries an en dash, filed in a folder and under a file name without the ' - '.  Expect 'folder name' and 'file name' rows failing with the dashed names as expected, every other row unchanged, and Import republishing under the dashed folder.
- **T-127  Folder ids that disagree with the tag are a finding.**  A movie folder carrying a different '[tmdbid-N]' from the tag's TMDB.  Expect the 'folder name' row failing and 'file name' passing.
- **T-128  An edition is not a finding.**  'Alien 3 (1992) [...]/Alien 3 (1992) [...] - Assembly Cut.mkv' with a correct tag.  Expect both naming rows passing.
- **T-129  A '[tmdbid-None]' show folder is a finding on every episode.**  A show folder written by 0.2.0 whose files carry TMDB in the COLLECTION block.  Expect the 'show folder' row failing on each file with the id in the expected name.
- **T-130  An unpadded season folder is a finding.**  'Season 6' holding a file whose SEASON PART_NUMBER is 6.  Expect 'season folder' failing with 'Season 06' expected.
- **T-131  A range file is compared as a range.**  'Show - S05E01-E02 - Title.mkv' with EPISODE PART_NUMBER 1.  Expect 'file name' passing;  the same file renamed 'S05E02-E03' fails.
- **T-132  A component breaking a section 9 rule is a finding.**  A season folder ending in a period.  Expect 'component rules' failing and naming that component.
- **T-133  Rescan wipes and re-assesses everything.**  On an already-swept library press 'Rescan entire library'.  Expect the findings counter at zero at once, a new pass over every file, and the finished log line reporting every file assessed and none unchanged;  'Sweep now' afterwards finishes in seconds.
- **T-134  Rescan during a pass restarts it from the top.**  Press it while 'Scanning' shows a pass part way through.  Expect the running pass to stop within one file interval and a fresh pass to start at the first file, with no stat-skipped files on it.
- **T-135  Rescan does not disturb an in-flight repair.**  With a finding's copy in the pipeline, rescan.  Expect the row to offer Import once the file is re-assessed, a second Import refused on the name in 'import/', the title unaffected, and its COMPARED detail still recording the origin skip.

## 13.  Web

- **T-70  HTTPS answers.**  'curl -sk https://localhost/api/status'.  Expect JSON.
- **T-71  Plain HTTP does not.**  'curl -s http://localhost:443/api/status'.  Expect a failure, not a redirect and not a served page.
- **T-136  A folder-carried id yields the provider's title, never the folder text.**  Import a movie with no tag block from a folder named in the pre-transform form, 'Star Wars Episode IV A New Hope (1977) [tmdbid-11] [imdbid-tt0076759]'.  Expect the MOVIE tag TITLE to read 'Star Wars: Episode IV – A New Hope', the published name 'Star Wars Episode IV - A New Hope (1977)', and the IDENTIFIED detail naming 'folder ids' or 'origin folder ids'.
- **T-137  A filename-form tag TITLE is corrected, not held.**  Import a movie whose MOVIE block carries a correct TMDB and a TITLE in filename form, 'Star Wars Episode VII The Force Awakens'.  Expect identification from 'embedded tag', the published tag TITLE equal to the Wikidata label, and no hold.
- **T-138  A TVDB-only COLLECTION block completes from the entity.**  Import an episode whose block carries TVDB and no TMDB.  Expect the identity to carry the TMDB and the show year from Wikidata, the published show folder '[tvdbid-N] [tmdbid-N]' with a four-digit year, and the IDENTIFIED detail naming 'embedded tag'.
- **T-139  A show never publishes into a '(None)' folder.**  Import an episode with a complete COLLECTION block.  Expect the published show folder to carry the year;  a '(None)' anywhere in 'complete/' fails the case.
- **T-140  A missing field holds with the field named.**  Import an episode whose show entity carries no P4983 and whose folders carry no tmdb.  Expect HELD at IDENTIFIED with a reason of the form 'resolved tvdb N (Show) but tmdb could not be determined from the Wikidata entity; an ID is never guessed', Force available, and nothing published.
- **T-141  An id Wikidata does not know falls to the next rung.**  Import a movie in a folder carrying a fabricated '[tmdbid-999999999]' and a resolvable file name.  Expect the log to record 'no Wikidata entity carries tmdb=999999999' and the identity to come from 'filename'.
- **T-142  A zero content light pair passes the gates.**  Import an HDR source whose SEI carries MaxCLL 0 and MaxFALL 0 with no container declaration.  Expect the REMUXED detail to record the content light repair, READY and VERIFIED to pass with no HDR problem, and 'mkvmerge -J' on the published file to report 'max_content_light' 0 and 'max_frame_light' 0.
- **T-143  A held or failed copy reads In Pipeline.**  Import a finding whose copy will hold, a comparison with no clear gate for instance.  Expect the row to read In Pipeline while the title is HELD, clickable to the title, and a second Import refused with the title named.
- **T-144  A published, unpromoted path is never recycled.**  Take a title to CLEANUP, then copy the library file into 'import/' by hand under the same name.  Expect one log line 'skipping ... published from this path and not yet promoted', the row unchanged with its 'output_path' intact, and the file left in 'import/'.  Forget the title after collecting its files and expect the same file detected and processed as new on the next poll.
- **T-145  A numeric episode title is not a range.**  A library episode named '... - S05E09 - 99.mkv' with a correct tag.  Expect no 'file name' finding.  Drop sources named with each of 'e17-18', 'e01-2', 'e16-18', 'e44+45', 'E01E02', 'e15&16' and 'S01E01-E02' and expect each parsed as the range it names.
- **T-146  An inconclusive hold names the incumbent.**  Import a copy that matches a library file at the same number with a different title, a season numbered one behind TVDB for instance.  Expect the hold reason, the COMPARED record and the log line to name the incumbent's file and read "carries a different title", with the two titles quoted, ahead of the gate summary.
- **T-147  An emptied import folder is removed.**  Drop a title into 'import/' as a folder holding one file, and a second folder holding two.  When the first title retires at CLEANUP, expect its folder gone from 'import/' and 'import/' itself still present;  when the first of the two retires, expect that folder still present with the other file in it, and gone once the second retires.  Discard a held title that arrived in a folder and expect the same removal on the quarantine path.  Expect one log line 'removed empty folder' per folder removed.
- **T-72  The dashboard is served.**  'curl -sk https://localhost/'.  Expect HTML.
- **T-76  A poster appears once a title is identified.**  Import a film with a resolvable tmdb id.  Expect '/api/titles/<id>' to carry a 'poster_url' after IDENTIFIED, a 'poster' hash beside it, and 'GET /api/poster/<hash>' to return image bytes with an image content type.
- **T-77  A title with no poster falls back to a text tile.**  A file held at the standards gate, which never identifies.  Expect 'poster_url' and 'poster' null, '/api/poster/<any unknown hash>' to answer 404, and the tile to show the filename on the same footprint rather than a blank.
- **T-78  Posters are served from the container, not from TMDB.**  After a poster has been fetched once, confirm a file exists under 'config/cache/posters', then block outbound internet and reload the dashboard.  Expect the poster still rendered.
- **T-79  A poster fetch never blocks identification.**  While a dashboard with uncached posters is loading, confirm a title still advances through IDENTIFIED at the normal rate.  The poster path must not sit behind the 3 second provider throttle.
- **T-80  Television collapses to one tile per show.**  Import a season.  Expect one tile carrying the series poster and the episode count, the box header counting titles rather than tiles, and clicking the tile to list the episodes.
- **T-81  A held title is actionable from its tile.**  Click a held tile.  Expect the detail dialog with Compare, Retry, Force through and Discard, and each button to reach '/api/held/<id>/decision'.
- **T-82  The counters are clickable and independent.**  With titles in both states, expect 'Quarantined Files' and 'Failed Jobs' to open separate lists, each showing the reason per title and a Compare button where a comparison exists.
- **T-84  A TVDB-only show gets artwork.**  Import a season of a show that resolves tvdb but not tmdb.  Expect 'poster_url' populated from artworks.thetvdb.com and the group tile to render it.
- **T-85  A season resolves its slug once.**  Time a fresh season import against a single-episode one.  Expect the difference to be the per-episode work only;  a season paying an extra 3 seconds per episode means the poster memo is not holding.
- **T-86  The group tile names the show.**  A television group with no artwork available.  Expect its placeholder to read the show name, not one episode's filename.
- **T-87  The episode list acts at both levels.**  Open a held season.  Expect per-row Retry, Force through and Discard, plus header actions for all of them, and expect a header action to close the list and move every episode back to Queue.
- **T-88  Quarantined rows offer no decisions.**  Open the Quarantined list.  Expect Details and Compare only, since the decision endpoint rejects a title that is not awaiting one.
- **T-89  Scraped titles carry no HTML entities.**  Import an episode whose provider title contains an apostrophe or an accent.  Expect the stored title decoded, for example "Let's Give the Boy a Hand" and not 'Let&#039;s Give the Boy a Hand', and the same on the published filename and the Matroska EPISODE tag.
- **T-90  An entity title matches exactly, not fuzzily.**  For that same episode, expect the match method to be exact.  A fuzzy match means the decode did not happen:  one entity still scores about 0.86 and passes silently, which is the failure this case exists to catch.
- **T-91  Detection lands inside the new window.**  Time a copy into 'import/' from completion to the 'detected' line.  Expect 30 to 45 seconds.  Repeat with a large file, where a slow write would show as a premature detection.
- **T-92  A partial copy is still refused.**  Interrupt a copy mid-transfer and leave it untouched past MTIME_QUIET.  Expect it detected and then held by ffprobe or the minimum standards gate, never encoded.
- **T-93  The queue holds three tiles per row.**  At 1920x1080 with the queue populated, expect three tiles across in Queue and two in every other box, with no page scrollbar.
- **T-83  The layout fits 1920x1080.**  Load the dashboard at that size with every box populated.  Expect no page scrollbar, and each box to scroll internally instead.
- **T-148  A promoted title leaves Ready to promote.**  Take two titles to CLEANUP.  Move the first's output out of 'complete/' with its retired source still in '.quarantine';  within one poll expect its tile gone, the row QUARANTINED with a reason naming the promotion, and Quarantined Files up by one.  Move the second's output out and remove its retired source;  within one poll expect the row absent from '/api/titles' and one 'title closed' log line.  Copy a third's output into the library instead of moving it and expect its tile unchanged.
- **T-149  A removed quarantine file closes its row.**  Remove a quarantined file from '.quarantine' by hand.  Within one poll expect the row gone and the counter down by one.  With DRY_RUN=1 expect neither sweep to run.
- **T-151  An exact entity lacking an id is not displaced by a weaker match.**  Drop 'Star Blazers S03E25 - Star Force, Shoot that Sun!.avi' flat in 'import/' with the TVDB remote-id lookup unreachable (a hosts entry for 'thetvdb.com' pointing nowhere, in a scratch container).  Expect no identity of 'Star Blazers 2199' anywhere in the log or the row, and, once the network is restored and the title retried, the IDENTIFIED detail to name Q16741126's label 'Star Blazers' with year 1979.  Repeat with a movie whose entity lacks P345 and expect a hold naming 'imdb' rather than a lower-scored entity.
- **T-152  A TVDB id is found through the IMDb id and the route is recorded.**  The same drop with the network intact.  Expect the IDENTIFIED detail to read 'tvdb 77773 found through imdb tt0078692 through the TVDB remote-id lookup', '/api/titles/<id>' to carry tvdb 77773, tmdb 7768 and show_year 1979, and the published path 'Star Blazers (1979) [tvdbid-77773] [tmdbid-7768]/Season 03/Star Blazers - S03E25 - Star Force, Shoot That Sun!.mkv'.
- **T-153  A non-English-original series publishes English episode titles.**  The same title.  Expect the log line '<slug> official: 77 of 77 episode titles read from the English translation', the episode matched by title with 'match_method' exact or fuzzy rather than 'fallback-numbering', and the EPISODE tag TITLE 'Star Force, Shoot That Sun!'.  On an English-original show expect no translation line and no episode-page requests in the debug log.
- **T-154  A show's TMDB page dated by its original is accepted, and the discrepancy recorded.**  The same title.  Expect tmdb 7768 kept, the IDENTIFIED detail carrying 'tmdb 7768 is titled 'Star Blazers' and first aired 1974 on its page against 1979 on Wikidata', and the folder year 1979.  Import a show whose P4983 page is dated LATER than the entity by more than one year and expect that tmdb dropped as before.
- **T-155  A held identification carries candidates per source.**  Hold a show at IDENTIFIED (a fabricated show name that scores against entities lacking TVDB ids, or T-151's network shape).  Expect '/api/titles/<id>' to carry 'candidates' with 'searched', 'wikidata', 'tvdb' and 'tmdb' lists, each Wikidata row carrying its score and an outcome in the log line's words, each TVDB row the English name and cross-links from its page, each TMDB row an origin of 'TMDB search' or 'Q… P4983' or both, and the detail dialog to render three tables with the pipeline's pick preselected and Identify disabled until every source has a selection.
- **T-156  The operator's selection identifies a whole season in one decision.**  With several episodes of one show held at IDENTIFIED, open one, select an entity and an id in each list, leave the same-search checkbox ticked, and click Identify.  Expect every held episode of that show to carry 'operator identified the title as qid=… tvdb=… tmdb=…' in its history, the IDENTIFIED detail to name 'operator' with each id's page confirmation noted, and the episodes to publish under the chosen ids.  Repeat with a typed id in one free-text field and expect the typed value in the record with 'supplied by the operator' noted.  POST 'identify' with a malformed id and expect HTTP 400 naming the field.
- **T-157  An arrival named without the library's three-part form matches by title.**  Drop 'Show S03E25 - Title.avi' and 'Show.S03E25.Title.mkv' for a resolvable show.  Expect 'match_method' exact or fuzzy on both, the EPISODE tag TITLE from the catalogue, and no 'falling back to source numbering' line.  Drop 'Show - S03E25 - Title.mkv' and expect the same result as before.
- **T-158  When numbering decides, the title is the catalogue's.**  Drop 'Star Trek Enterprise S01E06 Terra Nova (1080p x265 10bit Joy).mkv' flat in 'import/'.  Expect the log line 'falling back to source numbering S01E06, title 'Terra Nova' from the catalogue', '/api/titles/<id>' to carry 'match_method' 'fallback-numbering' with 'identity.title' 'Terra Nova', and a forced publish to name the file 'Star Trek Enterprise - S01E06 - Terra Nova.mkv' with that TITLE in the EPISODE tag and the segment title.  Drop the same name numbered S01E99 and expect the line ending 'which the catalogue does not list; title kept from the file name' and the title 'Nothing (1080p x265 Joy)'-shaped text from the file.
- **T-159  A held title's file is under 'hold/'.**  Drop a below-standard episode as 'import/Show (2001) [tvdbid-N]/Season 01/x.mkv'.  Once HELD, expect the file at 'hold/Show (2001) [tvdbid-N]/Season 01/x.mkv', 'import/Show (2001) [tvdbid-N]' gone, 'source_path' on '/api/titles/<id>' pointing under 'hold/', and a 'moved to hold/...' entry in the history ahead of the reasons.  Drop a second copy under the same name and expect it held beside the first as 'x.1.mkv'.  Force the first;  expect it to publish and its source to arrive in '.quarantine', with 'hold/' left without it.  With DRY_RUN=1 expect the file to stay in 'import/'.
- **T-160  The matcher sees through a release tag and a bare part marker.**  Drop 'Star Trek Enterprise S01E06 Terra Nova (1080p x265 10bit Joy).mkv', 'Star Trek Enterprise S02E01 Shockwave Part 2 (1080p x265 10bit Joy).mkv', "Star Trek Enterprise S04E09 Kir'shara (1080p x265 Joy).mkv" and 'Star.Trek.Enterprise.S03E02.Homefront.1080p.WEB-DL.x264-Joy.mkv'.  Expect 'match_method' exact on all four, never 'fallback-numbering', S02E01 to carry the title 'Shockwave, Part 2', and the published names 'Star Trek Enterprise - S01E06 - Terra Nova.mkv' and 'Star Trek Enterprise - S02E01 - Shockwave, Part 2.mkv'.  Drop a file whose segment title is the episode title and whose file name is random;  expect the log line 'episode matched on the segment title'.
- **T-161  A tie at the top score holds with both candidates.**  Drop 'Space Battleship Yamato.mp4' flat in 'import/'.  Expect HELD at IDENTIFIED with the reason naming Q3693349 (1977) and Q1191847 (2010), 'candidates.wikidata' carrying both with outcome 'tied at 1.00 with …', and the detail dialog offering both.  Rename the file 'Space Battleship Yamato (2010).mp4';  expect it to resolve to Q1191847 with no hold.  Drop 'Battlestar Galactica S01E01 33.mkv' with no year anywhere;  expect a hold between the 1978 and 2004 series.
- **T-162  The field probe classifies and the command carries the chain.**  With 'TV_ENCODE_SD=1', drop one progressive SD episode, one 480i episode and one 3:2 telecined DVD episode.  Expect the ROUTED detail and 'decision.fields' on '/api/titles/<id>' to read progressive, interlaced and telecine respectively, the debug argv to carry no field filter, 'bwdif=mode=send_frame' and 'fieldmatch,yadif=deint=interlaced,decimate' ahead of any crop, the telecine output at 23.976 fps with four fifths of the source's frames and a VERIFIED note 'telecine removed n of m frames', and the interlaced output at the source frame count with no combing on a frame grab.  A sidecar 'fields=progressive' beside the telecined episode must suppress the chain and the ROUTED line must say 'from the sidecar'.
- **T-163  A passthrough title pays no field probe.**  Drop an HEVC episode.  Expect no 'field probe' line for it and 'decision.fields' null.
- **T-164  The release corpus loads and strips by position only.**  At startup expect no 'skipped, pattern does not compile' line.  Drop 'Show.S01E01.Title.1080p.x265-Joy.mkv' and 'Show S01E02 - War of the Worlds.mkv' for a resolvable show whose S01E02 is 'War of the Worlds';  expect the first matched on 'Title' and the second matched exact with 'War' intact.
- **T-165  The OpenSubtitles hash is recorded.**  For any probed title expect 'probe.oshash' on '/api/titles/<id>' to equal the value of the reference algorithm run by hand on the same file (sum of the first and last 64 KiB as little-endian 64-bit words plus the size, modulo 2^64, sixteen hex digits), and the detail dialog to show it.
- **T-166  Cropdetect records a variable aspect and rejects an implausible sample.**  Encode a synthetic file built from three 'testsrc2' sections, 1920x800 padded to 1080 with equal bars, 1440x1080 padded to 1920, and 1920x800 padded with 60 px top and 220 px bottom.  Expect the crop record on the ENCODED history to carry the primary 'crop=1920:800:0:140', 'secondary' 1440x1080 with its share, and the debug log to show the asymmetric samples rejected as 'bars uneven';  the Compare table on a title with a secondary must show the 'variable aspect' row.
- **T-167  An edition publishes under the long form and is not compared against the plain cut.**  With a library holding 'Alien 3 (1992) [tmdbid-8077] [imdbid-tt0103644]/Alien 3 (1992).mkv', drop 'Alien 3 (1992) Assembly Cut.mkv'.  Expect the identity to carry 'edition' 'Assembly Cut', the COMPARED detail to read "holds no 'Assembly Cut' edition … a different cut is not compared", and the published file 'Alien 3 (1992) [tmdbid-8077] [imdbid-tt0103644]/Alien 3 (1992) [tmdbid-8077] [imdbid-tt0103644] - Assembly Cut.mkv'.  Drop 'The Extended Family (2010).mkv';  expect no edition.
- **T-168  Episode forms.**  Drop files named with 'e01~02', 'e01 to 02', 'e01 and 02', 'E01-E99' and 'Part 3 of 6' under a 'Season 02' folder, and 'Season 02/07 - Title.mkv'.  Expect ranges 1-2 on the first three, E01 alone with a 'spans 99 episodes' warning on the fourth, S02E03 on the fifth and S02E07 on the last.
- **T-169  Extras words hold by position.**  Drop 'Movie (2001) Featurette.mkv', 'Extras/Making Of.mkv', 'Movie (2001) - Extras.mkv' and 'Show S01E02 - Proof.mkv'.  Expect the first three held at screening with the reason naming the token or folder, and the fourth to proceed.
- **T-170  A leading year is the release year.**  Drop '2023.Transformers-.Rise.Of.The.Beasts.1920x804.BDRip.x264.TrueHD-Atmos.mkv', '2012.2009.1080p.BluRay.mkv' and 'Dual.2022.1080p.mkv'.  Expect the first to resolve to Q107174193 with year 2023, 'identified_from' 'filename' and 'reading' 'release year';  the second to search '2012' at 2009 and resolve to the 2009 film;  the third to search 'Dual' at 2022, and the debug log to show 'Dual 2022' scored as the second reading.
- **T-171  A hyphenated title resolves from a bare filename.**  Drop 'Spider-Man (2002).mkv', 'Ant-Man and the Wasp (2018).mkv', 'Star Wars Episode V - The Empire Strikes Back (1980).mkv', and the bare stems 'Spider-Man.mkv', 'Kick-Ass.mkv' and 'Ant-Man.mkv'.  Expect every one to resolve on the filename rung with the whole title searched, none truncated at the hyphen;  the debug log for the three bare stems must show the full name.  Drop 'Show.S01E01.Pilot-KILLERS.mkv' under a show folder;  expect the episode title matched as 'Pilot', the group still removed.
- **T-172  A bare year is searched both ways and the best fit wins.**  Drop 'Death Race 2000.1080p.mkv', '2001.A.Space.Odyssey.1080p.mkv' and '2010.1080p.mkv'.  Expect the 1975, 1968 and 1984 films, each with 'reading' 'title word' on the identity and the IDENTIFIED detail reading 'with the year read as the title word'.  Drop 'Transformers.2007.1080p.mkv';  expect the 2007 film with 'reading' 'release year', both readings scored in the debug log, and no tie although both readings reach the same entity.  Drop '2012.Doomsday.mkv';  expect a hold whose reason names both entities and reads 'the year in the name reads as either the release year or a title word', the Identify header listing both readings, each Wikidata candidate row carrying its reading, and the TMDB list built from both names.
- **T-173  A Retry from 'hold/' searches no folder name.**  Hold a title that sits directly in 'import/' at identification, confirm the file is under 'hold/', and POST 'retry'.  Expect the debug log to show the ladder without a 'parent folder' rung, and 'candidates.searched' never to carry 'hold' or 'import' as a name.
- **T-150  Encode progress counts frames.**  Encode a source carrying a forced subrip track with few cues.  Expect 'progress' on '/api/status' to carry 'frame', 'total_frames', 'fps' and 'eta_s', the frame figure to rise on every poll from the first, the tile to read 'x / y frames' with the bar sized from them, and 'eta_s' to fall.  On a source with no frame count and no duration expect 'total_frames' null, the frame figure alone and no bar.
- **T-73  A held decision requeues.**  POST 'retry' against a held title.  Expect it leaves HELD.
- **T-74  An unknown action is refused.**  POST 'nonsense'.  Expect a 4xx and no state change.
- **T-75  An unknown title is 404.**  GET '/api/titles/999999'.  Expect 404.

---

## Coverage note

Router cases T-31 to T-38 are the highest risk in this plan.  A misrouted title produces a valid file with the wrong tradeoff and nothing fails, so confirming an output file exists proves nothing.  Each router case states the expected gate and encoder and reads both back from '/api/titles/<id>', then corroborates against the output file's codec.

Four of the seven router paths are dormant at the HEVC default.  T-35 and T-36 require 'OUTPUT_CODEC=av1' and exist so those paths are exercised before that switch is ever flipped in anger.
