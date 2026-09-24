import logging
import os
import re

from . import compare, episodes, probe as probemod, rules

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


def runtime_seconds(container):
    return probemod.usable_duration(container.get("video") or {}, container)


#----- The gate
#----- Every report column that the probe alone can supply;  the gate and the report both read these.
def values(container, kind, path=None, profile=None, crop=None, tag_structure=None, statistics_ratio=None):
    profile = profile or {}
    keep_langs = tuple(profile.get("keep_langs") or KEEP_LANGS)
    video = container.get("video") or {}
    audio = container.get("audio") or []
    out = compare.attributes(
        container, path, crop=crop, tag_structure=tag_structure,
        statistics_ratio=statistics_ratio, keep_langs=keep_langs,
    )
    bitrate = video.get("bitrate")
    weighted = compare.normalised_bitrate(bitrate, video.get("codec"), profile.get("codec_efficiency"))
    pixels = out.get("display_pixels") or 0
    rate = out.get("frame_rate") or 0
    runtime = runtime_seconds(container)
    cls = rules.resolution_class(video.get("display_width"), video.get("display_height"), profile)
    out.update({
        "kind": kind,
        "kbps": round(float(bitrate) / 1000.0) if bitrate else None,
        "weighted_kbps": round(weighted / 1000.0) if weighted else None,
        "bpp": round(float(bitrate) / (pixels * rate), 3) if bitrate and pixels and rate else None,
        "res_class": cls,
        "kbps_floor": rules.kbps_floor(cls, profile),
        "runtime_min": round(runtime / 60.0, 1) if runtime else None,
        "gb": round(out.get("size_bytes") / 1e9, 2) if out.get("size_bytes") else None,
        "pal_speedup": is_pal_speedup_suspect(video),
        "kept_audio": any((a.get("language") or "und").lower() in keep_langs for a in audio),
        "extras": looks_like_extra(path) if path else None,
        "dv_record_gap": bool(video.get("dv_in_bitstream") and not video.get("dolby_vision")),
        "container_format": container.get("format_name"),
        "video_language": video.get("language"),
        "subtitle_forced_default_count": int(container.get("subtitle_forced_default_count") or 0),
    })
    return out


def screen(container, kind, path=None, crop=None, profile=None):
    warnings = []
    profile = profile or {}
    log.debug("screening as %s, crop=%s, path=%s", kind, crop, path)

    video = container.get("video") or {}
    audio = container.get("audio") or []
    problems = []
    if kind not in ("movie", "tv"):
        problems.append("unknown kind %r" % kind)

    found = values(container, kind, path=path, profile=profile, crop=crop)
    #----- no crop here, so the letterbox rule has no figure and fires in the report only.
    problems += [b["reason"] for b in rules.evaluate(found, kind, profile, gating_only=True)]

    if not crop and is_letterbox_candidate(video):
        warnings.append(
            "display aspect %.3f can hide baked-in bars, cropdetect required"
            % float(video.get("display_aspect") or 0)
        )
    runtime = runtime_seconds(container)
    if not runtime:
        warnings.append("no usable duration reported, runtime floor not applied")

    verdict = Verdict(not problems, problems, warnings)
    if problems:
        log.info("standards failed: %s", "; ".join(problems))
    else:
        log.info("standards passed%s", ", warnings: " + "; ".join(warnings) if warnings else "")
    log.debug(
        "runtime %ss, display %sx%s, class %s, %s kbps weighted, %d audio track(s)",
        runtime, video.get("display_width"), video.get("display_height"),
        found.get("res_class"), found.get("weighted_kbps"), len(audio),
    )
    return verdict
