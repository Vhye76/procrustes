import logging
import os
import statistics
import threading
import time

import re

from . import compare, episodes, media, probe as probemod, standards, tags, titles

log = logging.getLogger("audit")

REPAIR_REMUX = "remux"
REPAIR_STRIP = "language strip"
REPAIR_FLAGS = "flag repair"
REPAIR_TAGS = "tag rewrite"
REPAIR_SEGMENT = "segment title"
REPAIR_STATS = "statistics refresh"
REPAIR_HDR = "hdr declaration"
REPAIR_NAMING = "republish"
REPAIR_NONE = None

TAG_INCOMPLETE = "tag incomplete"
EPISODES_IN_FILE = "episodes in file"
RANGE_DURATION_RATIO = 1.8
RANGE_MEDIAN_FLOOR = 3

HDR_ROWS = (
    ("mastering_display", "mastering display"),
    ("content_light", "content light level"),
    ("dolby_vision", "Dolby Vision record"),
)


#----- Rows of the detail table
def _row(check, expected, actual, ok, repair, surface=None):
    return {
        "check": check,
        "expected": expected,
        "actual": actual,
        "ok": bool(ok),
        "repair": repair if not ok else None,
        "surface": surface,
    }


def _stem(path):
    return os.path.splitext(os.path.basename(str(path)))[0]


def _text(simples, name):
    return (simples.get(name) or "").strip() or None


def _number(simples, name):
    value = _text(simples, name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _year_of(value):
    match = re.search(r"(\d{4})", value or "")
    return int(match.group(1)) if match else None


#----- Reading the identity out of the tag block
def _tag_identity(root, kind):
    found = {
        "title": None, "show": None, "present": set(), "flattened": [],
        "tmdb": None, "imdb": None, "tvdb": None, "year": None,
        "season": None, "episode": None,
    }
    if root is None:
        return found
    found["flattened"] = tags.slash_named_simples(root)
    found["present"] = tags.target_types_present(root)
    for tag in root.findall("Tag"):
        target = tags._target_type(tag)
        simples = tags._simples(tag)
        if kind == "movie" and target == tags.MOVIE:
            found["title"] = _text(simples, "TITLE")
            found["tmdb"] = _text(simples, "TMDB")
            found["imdb"] = _text(simples, "IMDB")
            found["year"] = _year_of(_text(simples, "DATE_RELEASED"))
        elif kind == "tv" and target == tags.EPISODE:
            found["title"] = _text(simples, "TITLE")
            found["episode"] = _number(simples, "PART_NUMBER")
        elif kind == "tv" and target == tags.SEASON:
            found["season"] = _number(simples, "PART_NUMBER")
        elif kind == "tv" and target == tags.COLLECTION:
            found["show"] = _text(simples, "TITLE")
            found["tvdb"] = _text(simples, "TVDB")
            found["tmdb"] = _text(simples, "TMDB")
    return found


#----- Naming rows, built by the same functions the publish step uses
def _build(fn, *args):
    try:
        return fn(*args)
    except titles.TitleError as exc:
        return "unbuildable (%s)" % exc


def _naming_row(check, expected, actual):
    return _row(check, expected, actual, expected == actual, REPAIR_NAMING)


def _movie_naming_rows(path, found):
    name = os.path.basename(path)
    stem = _stem(path)
    parent = os.path.basename(os.path.dirname(path))
    if not all(found.get(k) for k in ("title", "year")):
        return [
            _row("folder name", "(from tag)", TAG_INCOMPLETE, False, REPAIR_NAMING),
            _row("file name", "(from tag)", TAG_INCOMPLETE, False, REPAIR_NAMING),
        ]
    #----- a block without both ids is named in the manual form, absent ids dropped.
    manual = not (found.get("tmdb") and found.get("imdb"))
    folder = _build(titles.movie_folder, found["title"], found["year"], found["tmdb"], found["imdb"], manual)
    rows = [_naming_row("folder name", folder, parent)]
    plain = _build(titles.movie_filename, found["title"], found["year"])
    edition_prefix = folder + " - "
    if stem.startswith(edition_prefix):
        label = stem[len(edition_prefix):]
        rows.append(_naming_row("file name", _build(titles.movie_filename, found["title"], found["year"], label, folder), name))
    elif stem.startswith(parent + " - "):
        label = stem[len(parent) + 3:]
        rows.append(_naming_row("file name", _build(titles.movie_filename, found["title"], found["year"], label, folder), name))
    else:
        rows.append(_naming_row("file name", plain, name))
    return rows


def _tv_naming_rows(path, found, max_range_span=None):
    name = os.path.basename(path)
    stem = _stem(path)
    season_dir = os.path.basename(os.path.dirname(path))
    show_dir = os.path.basename(os.path.dirname(os.path.dirname(path)))
    complete = (
        found.get("show") and found.get("tvdb") and found.get("tmdb")
        and found.get("season") is not None and found.get("episode") is not None
        and found.get("title")
    )
    if not complete:
        return [
            _row("show folder", "(from tag)", TAG_INCOMPLETE, False, REPAIR_NAMING),
            _row("season folder", "(from tag)", TAG_INCOMPLETE, False, REPAIR_NAMING),
            _row("file name", "(from tag)", TAG_INCOMPLETE, False, REPAIR_NAMING),
        ]
    #----- the COLLECTION block carries no year, so the folder's own is used and everything else is checked.
    years = titles.YEAR_IN_PARENS.findall(show_dir)
    show_year = years[-1] if years else "YYYY"
    show = _build(titles.show_folder, found["show"], show_year, found["tvdb"], found["tmdb"])
    season = titles.season_folder(found["season"])
    parsed = episodes.parse_filename(stem, max_range_span=max_range_span)
    last = None
    if parsed and parsed["season"] == found["season"] and parsed["first"] == found["episode"]:
        last = parsed["last"]
    file_expected = _build(
        titles.episode_filename, found["show"], found["season"], found["episode"], found["title"], last,
    )
    return [
        _naming_row("show folder", show, show_dir),
        _naming_row("season folder", season, season_dir),
        _naming_row("file name", file_expected, name),
    ]


def _component_rows(path, kind):
    components = [os.path.basename(path), os.path.basename(os.path.dirname(path))]
    if kind == "tv":
        components.append(os.path.basename(os.path.dirname(os.path.dirname(path))))
    problems = []
    for component in components:
        for problem in titles.validate_component(component):
            problems.append("%s: %s" % (component, problem))
    return [_row("component rules", "none", "; ".join(problems) or "none", not problems, REPAIR_NAMING)]


def _hdr_rows(video):
    rows = []
    gap = set(video.get("hdr_declaration_gap") or [])
    for tag in ("color_primaries", "color_transfer", "color_space"):
        rows.append(_row(tag.replace("_", " "), video.get(tag), video.get(tag), True, REPAIR_NONE, "container"))
    for key, label in HDR_ROWS:
        if key == "dolby_vision":
            declared = "present" if video.get("dolby_vision") else "absent"
            carried = "present" if (video.get("dolby_vision") or video.get("dv_in_bitstream")) else "absent"
            repair = REPAIR_NONE
        else:
            declared = compare._mastering_summary(video.get(key)) if key == "mastering_display" \
                else compare._content_light_summary(video.get(key))
            bitstream = video.get(key + "_bitstream")
            carried = compare._mastering_summary(bitstream) if key == "mastering_display" \
                else compare._content_light_summary(bitstream)
            repair = REPAIR_HDR
        rows.append(_row(label, carried or "absent", declared or "absent", key not in gap, repair, "bitstream"))
    return rows


#----- Assessing one library file
def assess(path, kind, profile=None):
    profile = profile or {}
    container = probemod.probe(path, keep_langs=profile.get("keep_langs")).container
    video = container.get("video") or {}
    rows = []

    fmt = (container.get("format_name") or "").lower()
    rows.append(_row("container", "matroska", fmt or "unknown", "matroska" in fmt, REPAIR_REMUX))

    foreign = container.get("foreign_tracks") or []
    rows.append(_row("foreign tracks", "none", ", ".join(foreign) or "none", not foreign, REPAIR_STRIP))

    selectors, _ = probemod.track_selectors(path)
    audio_defaults = int(container.get("audio_default_count") or 0)
    rows.append(_row("audio default count", 1, audio_defaults, audio_defaults == 1, REPAIR_FLAGS))
    sub_defaults = int(container.get("subtitle_default_count") or 0)
    rows.append(_row("subtitle defaults", 0, sub_defaults, sub_defaults == 0, REPAIR_FLAGS))
    keep_langs = tuple(profile.get("keep_langs") or probemod.KEEP_LANGS)
    subtitles = container.get("subtitles") or []
    forced_wanted = 1 if any(probemod.forced_subtitle(s, keep_langs) for s in subtitles) else 0
    forced_defaults = int(container.get("subtitle_forced_default_count") or 0)
    rows.append(_row(
        "forced subtitle default", forced_wanted, forced_defaults,
        forced_defaults == forced_wanted, REPAIR_FLAGS,
    ))
    named_forced = sum(
        1 for s in subtitles
        if not s.get("forced") and probemod.forced_subtitle(s, keep_langs)
    )
    rows.append(_row("forced by name only", 0, named_forced, named_forced == 0, REPAIR_FLAGS))
    video_lang = next(
        (r["language"] for r in selectors
         if r["type"] == "video" and "V_MJPEG" not in (r["codec_id"] or "").upper()),
        "und",
    )
    rows.append(_row("video language", "eng", video_lang, video_lang == "eng", REPAIR_FLAGS))

    root = tags.read_tags(path)
    found = _tag_identity(root, kind)
    if kind == "movie":
        wanted = {tags.MOVIE}
        expected_structure = "MOVIE target"
    else:
        wanted = {tags.COLLECTION, tags.SEASON, tags.EPISODE}
        expected_structure = "COLLECTION, SEASON and EPISODE targets"
    present = sorted(t for t in found["present"] if t in wanted)
    structure_ok = wanted <= found["present"] and not found["flattened"]
    actual_structure = ", ".join(present) if present else "none"
    if found["flattened"]:
        actual_structure += ", flattened"
    rows.append(_row("tag structure", expected_structure, actual_structure, structure_ok, REPAIR_TAGS))

    if kind == "movie":
        rows += _movie_naming_rows(path, found)
    else:
        rows += _tv_naming_rows(path, found, profile.get("max_range_span"))
    rows += _component_rows(path, kind)

    tag_title = found["title"]
    segment = container.get("segment_title")
    rows.append(_row(
        "segment title", tag_title or "(tag TITLE)", segment or "unset",
        bool(tag_title) and segment == tag_title, REPAIR_SEGMENT,
    ))

    ratio = round(tags.byte_sum_ratio(path), 4)
    rows.append(_row(
        "statistics ratio", "> %s" % tags.STATS_RATIO_FLOOR, ratio,
        ratio > tags.STATS_RATIO_FLOOR, REPAIR_STATS,
    ))

    if video.get("hdr"):
        rows += _hdr_rows(video)

    failed = [r["check"] for r in rows if not r["ok"]]
    measured = {
        "rows": rows,
        "size_bytes": int(container.get("size_bytes") or 0),
        "hdr_format": video.get("hdr_format"),
        "codec": video.get("codec"),
        "duration_s": probemod.usable_duration(video, container),
        "tag": _tag_fields(found),
        "container": container,
        "crop": _crop(path, video, container, profile),
        "tag_structure": ("flattened" if found["flattened"] else "targeted") if root is not None else None,
        "statistics_ratio": ratio,
    }
    return failed, measured, _summary(rows)


def _crop(path, video, container, profile):
    if "crop_sample_count" not in profile or not standards.is_letterbox_candidate(video):
        return None
    try:
        found = media.detect_crop_for(path, video, container, profile)
    except Exception as exc:
        log.warning("audit cropdetect failed on %s: %s", os.path.basename(path), exc)
        return None
    #----- detect_crop returns None under the bar floor;  a measured file with no bars records 0, not blank.
    if found is None:
        return {"bars_px": 0, "picture_pixels": int(video.get("display_pixels") or 0) or None}
    return found


def _summary(rows):
    return "; ".join(
        "%s: %s" % (r["check"], r["actual"]) for r in rows if not r["ok"]
    ) or "meets the standard"


def _tag_fields(found):
    return {k: found.get(k) for k in ("show", "season", "episode", "title", "year")}


#----- The two-episode listing
def _season_number(folder):
    m = episodes.SEASON_FOLDER.match(os.path.basename(folder).strip())
    return int(m.group(1)) if m else None


def _range_candidates(rows, ratio, max_range_span=None):
    coded = []
    for row in rows:
        parsed = episodes.parse_filename(_stem(row["path"]), max_range_span=max_range_span)
        if parsed is not None:
            coded.append((row, parsed))
    claimed = {n for _row, p in coded for n in range(p["first"], p["last"] + 1)}
    found = []
    for row, parsed in coded:
        duration = row.get("duration_s")
        if duration is None or parsed["first"] != parsed["last"]:
            continue
        others = [r.get("duration_s") for r, _p in coded if r is not row and r.get("duration_s")]
        if len(others) < RANGE_MEDIAN_FLOOR:
            continue
        median = statistics.median(others)
        if parsed["first"] + 1 in claimed or duration < ratio * median:
            continue
        found.append((row, parsed, duration, median))
    return found


def _range_row(row, parsed, duration, median):
    tag = (row.get("measured") or {}).get("tag") or {}
    title = tag.get("title") or "TITLE"
    title = episodes.parse_marker(title)[0] or title
    proposed = _build(
        titles.episode_filename, tag.get("show") or "SHOW", parsed["season"], parsed["first"],
        title, parsed["first"] + 1,
    )
    detail = "%.1f min against a season median of %.1f min (%.2fx), no S%02dE%02d file in the folder" % (
        duration / 60.0, median / 60.0, duration / median, parsed["season"], parsed["first"] + 1,
    )
    return _row(EPISODES_IN_FILE, proposed, os.path.basename(row["path"]), False, REPAIR_NONE), detail


def _with_range_row(measured, row, detail):
    rows = [r for r in (measured.get("rows") or []) if r.get("check") != EPISODES_IN_FILE]
    if row is not None:
        row = dict(row, detail=detail)
        rows.append(row)
    measured = dict(measured, rows=rows)
    failed = [r["check"] for r in rows if not r["ok"]]
    return failed, measured, _summary(rows)


#----- The sweep
class Auditor:
    def __init__(self, cfg, layout, store, stop_event, settings=None):
        self.cfg = cfg
        self.settings = settings
        self.layout = layout
        self.store = store
        self.stop_event = stop_event
        self.thread = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._restart = threading.Event()
        self._status = {
            "running": False,
            "done": 0,
            "total": 0,
            "started_at": None,
            "finished_at": None,
            "next_at": None,
            "current": None,
        }

    def start(self):
        if not self.cfg.audit_enabled:
            log.info("library audit disabled, AUDIT_INTERVAL=%d", self.cfg.audit_interval)
            return
        self.thread = threading.Thread(target=self._run, name="auditor", daemon=True)
        self.thread.start()
        log.info(
            "library audit started, %ds between files, passes %ds apart",
            self.cfg.audit_interval, self.cfg.audit_sweep_interval,
        )

    def sweep_now(self):
        self._wake.set()

    def rescan(self):
        removed = self.store.audit_forget_all()
        log.info("library audit findings wiped, %d row(s), full rescan requested", removed)
        self._restart.set()
        self._wake.set()

    def status(self):
        with self._lock:
            snapshot = dict(self._status)
        snapshot.update(self.store.audit_totals())
        snapshot["enabled"] = bool(self.cfg.audit_enabled)
        return snapshot

    def _set(self, **fields):
        with self._lock:
            self._status.update(fields)

    def _run(self):
        while not self.stop_event.is_set():
            self._wake.clear()
            try:
                self._sweep()
            except Exception:
                log.exception("library audit pass failed")
            next_at = time.time() + self.cfg.audit_sweep_interval
            self._set(next_at=next_at)
            while not self.stop_event.is_set() and time.time() < next_at:
                if self._wake.wait(timeout=5):
                    break

    def _files(self):
        for kind, root in sorted(self.layout.libraries.items()):
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for name in sorted(filenames):
                    if name.startswith("."):
                        continue
                    if not name.lower().endswith(probemod.VIDEO_EXTENSIONS):
                        continue
                    yield kind, os.path.join(dirpath, name)

    def _sweep(self):
        self._restart.clear()
        files = list(self._files())
        self._set(running=True, done=0, total=len(files), started_at=time.time(), finished_at=None)
        log.info("library audit pass over %d file(s)", len(files))
        seen = []
        assessed = 0
        for kind, path in files:
            if self.stop_event.is_set() or self._restart.is_set():
                break
            seen.append(path)
            try:
                st = os.stat(path)
            except OSError as exc:
                log.debug("audit skipped %s: %s", path, exc)
                self._set(done=len(seen))
                continue
            previous = self.store.audit_seen(path)
            #----- a row without a stored container summary is assessed again, whatever its size and mtime.
            if previous == (st.st_size, st.st_mtime, True):
                self._set(done=len(seen))
                continue
            self._set(current=os.path.basename(path))
            try:
                failed, measured, summary = assess(
                    path, kind, self.settings.profile(kind) if self.settings else None,
                )
            except Exception as exc:
                log.warning("audit could not assess %s: %s", os.path.basename(path), exc)
                #----- a None container still counts as measured, so an unreadable file is stat-skipped like any other.
                failed, measured, summary = ["unreadable"], {"rows": [], "container": None}, str(exc)
            self.store.audit_record(
                path, kind, st.st_size, st.st_mtime, failed, measured, summary,
                duration_s=measured.get("duration_s"),
            )
            assessed += 1
            if failed:
                log.info("audit finding on %s: %s", os.path.basename(path), summary)
            self._set(done=len(seen), current=None)
            if self.stop_event.wait(self.cfg.audit_interval):
                break
        #----- an interrupted pass has not seen every file, so it must not prune the ones it missed.
        interrupted = self.stop_event.is_set() or self._restart.is_set()
        removed = self.store.audit_forget_missing(seen) if not interrupted else 0
        listed = self._folder_phase() if not interrupted else 0
        self._set(running=False, finished_at=time.time(), current=None)
        totals = self.store.audit_totals()
        log.info(
            "library audit pass finished: %d assessed, %d unchanged, %d forgotten, %d finding(s),"
            " %d file(s) listed as two episodes",
            assessed, len(seen) - assessed, removed, totals["findings"], listed,
        )

    def _folder_phase(self):
        profile = self.settings.profile("tv") if self.settings else {}
        ratio = float(profile.get("range_duration_ratio") or RANGE_DURATION_RATIO)
        max_range_span = profile.get("max_range_span")
        folders = {}
        for row in self.store.audit_rows("tv"):
            folders.setdefault(os.path.dirname(row["path"]), []).append(row)
        listed = 0
        for folder, rows in sorted(folders.items()):
            if self.stop_event.is_set() or self._restart.is_set():
                break
            season = _season_number(folder)
            #----- 0 is season 0, exempt because specials vary in length;  None is not a season folder at all.
            if not season:
                continue
            for row in rows:
                if row.get("duration_s") is None and not self._measure(row):
                    break
            flagged = {}
            for row, parsed, duration, median in _range_candidates(rows, ratio, max_range_span):
                flagged[row["path"]] = _range_row(row, parsed, duration, median)
            for row in rows:
                measured = row.get("measured") or {}
                has = any(r.get("check") == EPISODES_IN_FILE for r in measured.get("rows") or [])
                entry = flagged.get(row["path"])
                if entry is None and not has:
                    continue
                check, detail = entry if entry else (None, None)
                failed, measured, summary = _with_range_row(measured, check, detail)
                self.store.audit_update(row["path"], failed, measured, summary)
                if entry:
                    listed += 1
                    log.info("audit lists %s as two episodes: %s", os.path.basename(row["path"]), detail)
        return listed

    def _measure(self, row):
        self._set(current=os.path.basename(row["path"]))
        try:
            container = probemod.probe(row["path"]).container
            duration = probemod.usable_duration(container.get("video") or {}, container)
            measured = dict(row.get("measured") or {}, duration_s=duration)
            if "tag" not in measured:
                measured["tag"] = _tag_fields(_tag_identity(tags.read_tags(row["path"]), "tv"))
            row["duration_s"] = duration
            row["measured"] = measured
            self.store.audit_update(row["path"], row.get("checks") or [], measured, row.get("summary"), duration_s=duration)
        except Exception as exc:
            log.warning("audit could not measure %s: %s", os.path.basename(row["path"]), exc)
        self._set(current=None)
        #----- the wait is the per-file throttle, and a stop during it ends the folder.
        return not self.stop_event.wait(self.cfg.audit_interval)
