import logging
import os
import re

from . import episodes, probe as probemod

log = logging.getLogger("standards")

MOVIE_MIN_DISPLAY_WIDTH = 1920
MOVIE_MIN_DISPLAY_HEIGHT = 800
MOVIE_MIN_RUNTIME_S = 40 * 60
TV_MIN_RUNTIME_S = 15 * 60

LETTERBOX_MAX_BARS_PX = 20

KEEP_LANGS = ("eng", "en", "und")

PAL_HEIGHTS = (576, 288)
PAL_RATE_TOLERANCE = 0.01

LETTERBOX_CANDIDATE_ASPECTS = ((16.0 / 9.0), (4.0 / 3.0))
ASPECT_TOLERANCE = 0.02


class Verdict:
    def __init__(self, ok, problems=None, warnings=None):
        self.ok = ok
        self.problems = list(problems or [])
        self.warnings = list(warnings or [])

    def as_dict(self):
        return {"ok": self.ok, "problems": self.problems, "warnings": self.warnings}

    def __repr__(self):
        return "<Verdict ok=%s problems=%r>" % (self.ok, self.problems)


#----- Extras vocabulary:  the first set flags anywhere, the second only in a trailing segment or as the folder.
EXTRAS_WORDS = (
    "sample", "trailer", "trailers", "featurette", "featurettes", "deleted scene",
    "deleted scenes", "behind the scenes", "making of", "gag reel", "bloopers", "outtakes",
)
EXTRAS_SEGMENT_WORDS = (
    "proof", "bonus", "extra", "extras", "interview", "interviews", "short", "shorts",
)


def _phrase(word):
    return r"[\s._-]+".join(re.escape(w) for w in word.split())


EXTRAS_PATTERNS = tuple(
    (re.compile(r"(?:^|[^a-z0-9])%s(?:$|[^a-z0-9])" % _phrase(word), re.I), word)
    for word in EXTRAS_WORDS
)
EXTRAS_SEGMENT_PATTERNS = tuple(
    (re.compile(r"(?:^|\)\s*|[\s._]-[\s._]|\s-\s)%s\s*$" % _phrase(word), re.I), word)
    for word in EXTRAS_SEGMENT_WORDS
)
EXTRAS_FOLDERS = tuple(
    (re.compile(r"^%s$" % _phrase(word), re.I), word)
    for word in EXTRAS_WORDS + EXTRAS_SEGMENT_WORDS
)


#----- Individual checks
def looks_like_extra(path):
    name = os.path.splitext(os.path.basename(str(path)))[0]
    for pattern, word in EXTRAS_PATTERNS:
        if pattern.search(name):
            return "file name carries '%s'" % word
    #----- the text after an episode marker is the title, so "S01E02 - Proof" is an episode.
    if episodes.parse_filename(name) is None:
        for pattern, word in EXTRAS_SEGMENT_PATTERNS:
            if pattern.search(name):
                return "file name ends in the segment '%s'" % word
    parent = os.path.basename(os.path.dirname(str(path)))
    for pattern, word in EXTRAS_FOLDERS:
        if pattern.match(parent.strip()):
            return "sits in a '%s' folder" % parent.strip()
    return None


def is_pal_speedup_suspect(video):
    rate = float(video.get("frame_rate") or 0)
    if abs(rate - 25.0) > PAL_RATE_TOLERANCE:
        return False
    return int(video.get("height") or 0) in PAL_HEIGHTS


def is_letterbox_candidate(video):
    aspect = float(video.get("display_aspect") or 0)
    if not aspect:
        return False
    return any(abs(aspect - a) < ASPECT_TOLERANCE for a in LETTERBOX_CANDIDATE_ASPECTS)


def _human_runtime(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    return "%d min" % (seconds // 60)


def runtime_seconds(container):
    return probemod.usable_duration(container.get("video") or {}, container)


#----- The gate
#----- The per-kind floors, 0 meaning none;  the module constants are the defaults
def floors(kind, profile=None):
    profile = profile or {}
    if kind == "movie":
        defaults = (MOVIE_MIN_DISPLAY_WIDTH, MOVIE_MIN_DISPLAY_HEIGHT, MOVIE_MIN_RUNTIME_S)
    else:
        defaults = (0, 0, TV_MIN_RUNTIME_S)
    width = profile.get("min_display_width")
    height = profile.get("min_display_height")
    runtime_min = profile.get("min_runtime_min")
    return (
        int(defaults[0] if width is None else width),
        int(defaults[1] if height is None else height),
        int(defaults[2] if runtime_min is None else runtime_min * 60),
    )


def screen(container, kind, path=None, crop=None, profile=None):
    problems = []
    warnings = []
    profile = profile or {}
    keep_langs = tuple(profile.get("keep_langs") or KEEP_LANGS)
    bars_limit = int(profile.get("letterbox_bars_px") or LETTERBOX_MAX_BARS_PX)
    pal_check = profile.get("pal_speedup_check", True)
    log.debug("screening as %s, crop=%s, path=%s", kind, crop, path)

    video = container.get("video") or {}
    audio = container.get("audio") or []

    extra = looks_like_extra(path) if path else None
    if extra:
        problems.append("looks like a sample or extras file, %s: %s" % (extra, os.path.basename(str(path))))

    if not audio:
        problems.append("no audio streams")
    elif not any((a.get("language") or "und").lower() in keep_langs for a in audio):
        problems.append(
            "no audio track tagged %s, found %s"
            % (" or ".join(keep_langs), ", ".join(sorted({(a.get("language") or "und") for a in audio})))
        )

    if pal_check and is_pal_speedup_suspect(video):
        problems.append(
            "25 fps at %dx%d, likely a PAL speed-up of film material"
            % (video.get("width") or 0, video.get("height") or 0)
        )

    if crop:
        bars = crop.get("bars_px") or 0
        if bars > bars_limit:
            problems.append(
                "baked-in letterbox of %d px exceeds the %d px limit"
                % (bars, bars_limit)
            )
    elif is_letterbox_candidate(video):
        warnings.append(
            "display aspect %.3f can hide baked-in bars, cropdetect required"
            % float(video.get("display_aspect") or 0)
        )

    runtime = runtime_seconds(container)

    if kind in ("movie", "tv"):
        min_width, min_height, min_runtime = floors(kind, profile)
        noun = "movie" if kind == "movie" else "episode"
        dw = int(video.get("display_width") or 0)
        dh = int(video.get("display_height") or 0)
        #----- a zero floor is no floor, which is how television carries no resolution floor.
        if (min_width and dw < min_width) or (min_height and dh < min_height):
            problems.append(
                "%s display resolution %dx%d is below the %dx%d floor"
                % (noun, dw, dh, min_width, min_height)
            )
        if runtime and min_runtime and runtime < min_runtime:
            problems.append(
                "%s runtime %s is below the %d min floor"
                % (noun, _human_runtime(runtime), min_runtime / 60)
            )
    else:
        problems.append("unknown kind %r" % kind)

    if not runtime:
        warnings.append("no usable duration reported, runtime floor not applied")

    verdict = Verdict(not problems, problems, warnings)
    if problems:
        log.info("standards failed: %s", "; ".join(problems))
    else:
        log.info("standards passed%s", ", warnings: " + "; ".join(warnings) if warnings else "")
    log.debug(
        "runtime %ss, display %sx%s, %d audio track(s)",
        runtime, video.get("display_width"), video.get("display_height"), len(audio),
    )
    return verdict
