#----- Resolution classes and the default standards
SD = "SD"
HD720 = "720"
HD1080 = "1080"
UHD = "2160"
CLASS_ORDER = (UHD, HD1080, HD720)
CLASS_BOUNDS = {HD720: (1100, 700), HD1080: (1600, 900), UHD: (3200, 1800)}
CLASS_KEYS = tuple(
    "class_%s_%s" % (cls, side) for cls in (HD720, HD1080, UHD) for side in ("width", "height")
)

MIN_KBPS = {SD: 1500, HD720: 3000, HD1080: 6000, UHD: 20000}
MAX_SIZE_GB = 10
STATS_RATIO_MIN = 0.5
STATS_RATIO_MAX = 1.5

AT_LEAST = "at least"
AT_MOST = "at most"
ABOVE = "above"
EXACTLY = "exactly"
IS = "is"
ABSENT = "absent"
PRESENT = "present"
PASSES = "passes"

NUMBER = "number"
TEXT = "text"
BOOL = "bool"


#----- A file is in the highest class whose width OR height it reaches, so scope classes by width and 4:3 by height.
def bounds(profile=None):
    profile = profile or {}
    out = {}
    for cls, (width, height) in CLASS_BOUNDS.items():
        out[cls] = (
            int(profile.get("class_%s_width" % cls) or width),
            int(profile.get("class_%s_height" % cls) or height),
        )
    return out


def resolution_class(width, height, profile=None):
    limits = bounds(profile)
    width = int(width or 0)
    height = int(height or 0)
    for cls in CLASS_ORDER:
        min_width, min_height = limits[cls]
        if width >= min_width or height >= min_height:
            return cls
    return SD


def kbps_floor(cls, profile=None):
    profile = profile or {}
    key = "min_kbps_%s" % str(cls).lower()
    value = profile.get(key)
    return MIN_KBPS.get(cls) if value is None else value


#----- The report columns, in display order:  title first, then the comparison gates in gate order
class Column:
    def __init__(self, key, label, type, unit=None, gate=None):
        self.key = key
        self.label = label
        self.type = type
        self.unit = unit
        self.gate = gate

    def as_dict(self):
        return {"key": self.key, "label": self.label, "type": self.type, "unit": self.unit, "gate": self.gate}


COLUMNS = (
    Column("title", "title", TEXT),
    Column("kind", "kind", TEXT),
    Column("broken", "broken rules", TEXT),
    Column("dolby_vision", "Dolby Vision", BOOL, gate=1),
    Column("hdr", "HDR", BOOL, gate=1),
    Column("hdr_format", "HDR format", TEXT, gate=1),
    Column("display_pixels", "display pixels", NUMBER, gate=2),
    Column("display_width", "display width", NUMBER, gate=2),
    Column("display_height", "display height", NUMBER, gate=2),
    Column("picture_pixels", "picture pixels after bars", NUMBER, gate=3),
    Column("letterbox_px", "baked-in letterbox", NUMBER, "px", gate=3),
    Column("audio_channels_max", "audio channels", NUMBER, gate=4),
    Column("bit_depth", "bit depth", NUMBER, gate=5),
    Column("codec", "video codec", TEXT, gate=6),
    Column("kbps", "video bitrate", NUMBER, "kbps", gate=6),
    Column("weighted_kbps", "weighted bitrate", NUMBER, "kbps", gate=6),
    Column("bpp", "bits per pixel", NUMBER, gate=6),
    Column("res_class", "resolution class", TEXT, gate=6),
    Column("kbps_floor", "bitrate floor", NUMBER, "kbps", gate=6),
    Column("subtitle_forced_tracks", "forced subtitle tracks", NUMBER, gate=7),
    Column("pedigree", "source pedigree", TEXT, gate=8),
    Column("runtime_min", "runtime", NUMBER, "min"),
    Column("gb", "file size", NUMBER, "GB"),
    Column("pal_speedup", "PAL speed-up", BOOL),
    Column("kept_audio", "kept-language audio", BOOL),
    Column("extras", "extras name", TEXT),
    Column("container_format", "container", TEXT),
    Column("foreign_tracks", "foreign language tracks", TEXT),
    Column("audio_default_count", "default audio tracks", NUMBER),
    Column("subtitle_default_count", "default non-forced subtitles", NUMBER),
    Column("subtitle_forced_default_count", "default forced subtitles", NUMBER),
    Column("subtitle_named_forced", "subtitles named forced without the flag", NUMBER),
    Column("video_language", "video track language", TEXT),
    Column("tag_structure", "tag structure", TEXT),
    Column("names", "folder and file names", TEXT),
    Column("component_rules", "path component rules", TEXT),
    Column("segment_title", "segment title", TEXT),
    Column("statistics_ratio", "statistics byte-sum ratio", NUMBER),
    Column("hdr_declaration_gap", "HDR declared short of the bitstream", TEXT),
    Column("dv_record_gap", "Dolby Vision RPU without its record", BOOL),
    Column("episodes_in_file", "episodes in file", TEXT),
    Column("profile", "video profile", TEXT),
    Column("mastering_display", "mastering display declared", TEXT),
    Column("content_light", "content light level declared", TEXT),
    Column("display_aspect", "display aspect", NUMBER),
    Column("stored_width", "stored width", NUMBER),
    Column("stored_height", "stored height", NUMBER),
    Column("sar", "sample aspect", NUMBER),
    Column("variable_aspect", "variable aspect", TEXT),
    Column("audio_codecs", "audio codecs", TEXT),
    Column("audio_tracks", "audio tracks", NUMBER),
    Column("pix_fmt", "pixel format", TEXT),
    Column("frame_rate", "frame rate", NUMBER),
    Column("frame_count", "frame count", NUMBER),
    Column("colour_primaries", "colour primaries", TEXT),
    Column("colour_transfer", "colour transfer", TEXT),
    Column("colour_space", "colour space", TEXT),
    Column("colour_tagged", "colour tagged", BOOL),
    Column("subtitle_codecs", "subtitle codecs", TEXT),
    Column("subtitle_tracks", "subtitle tracks", NUMBER),
    Column("chapters", "chapters", NUMBER),
    Column("cover_art", "cover art tracks", NUMBER),
    Column("path", "path", TEXT),
)

COLUMN_BY_KEY = {c.key: c for c in COLUMNS}
#----- identity and derived columns no rule can sensibly judge;  every other column carries a rule.
UNRULED = ("title", "kind", "broken", "path", "kbps_floor")


#----- The rules
class Rule:
    def __init__(self, key, label, help, column, method, value_key=None, kind=None, klass=None,
                 repairable=False, gates=True, checks=None, fixed=None, ignored=False, generated=False):
        self.key = key
        self.label = label
        self.help = help
        self.column = column
        self.method = method
        self.value_key = value_key
        self.kind = kind
        self.klass = klass
        self.repairable = repairable
        #----- a repairable rule is fixed on the way through, and a check rule needs the library, so neither holds an arrival.
        self.gates = gates and not repairable and not checks
        self.checks = tuple(checks or ())
        self.fixed = fixed
        self.ignored = ignored
        self.generated = generated

    @property
    def ignore_key(self):
        return "ignore_" + self.key

    def as_dict(self):
        return {
            "key": self.key,
            "label": self.label,
            "help": self.help,
            "column": self.column,
            "method": self.method,
            "value_key": self.value_key,
            "kind": self.kind,
            "class": self.klass,
            "repairable": self.repairable,
            "gates": self.gates,
            "library_only": bool(self.checks),
            "fixed": self.fixed,
            "ignore_key": self.ignore_key,
            "generated": self.generated,
        }


def _bitrate_rule(cls, label):
    return Rule(
        "min_kbps_%s" % cls.lower(), "%s video bitrate" % label,
        "Video bitrate weighted by codec efficiency to h264 terms, for a file in this resolution class.",
        "weighted_kbps", AT_LEAST, value_key="min_kbps_%s" % cls.lower(), klass=cls,
    )


EXPLICIT = (
    Rule("movie_display_width", "Movie display width",
         "Display width, stored width times sample aspect;  0 applies no floor.",
         "display_width", AT_LEAST, value_key="min_display_width", kind="movie"),
    Rule("movie_display_height", "Movie display height",
         "Display height;  800 admits a 2.40:1 scope master.",
         "display_height", AT_LEAST, value_key="min_display_height", kind="movie"),
    Rule("episode_display_width", "Episode display width",
         "Display width of an episode;  0 applies no floor.",
         "display_width", AT_LEAST, value_key="min_display_width", kind="tv"),
    Rule("episode_display_height", "Episode display height",
         "Display height of an episode;  0 applies no floor.",
         "display_height", AT_LEAST, value_key="min_display_height", kind="tv"),
    Rule("movie_runtime", "Movie runtime",
         "Video stream running time in minutes;  0 applies no floor.",
         "runtime_min", AT_LEAST, value_key="min_runtime_min", kind="movie"),
    Rule("episode_runtime", "Episode runtime",
         "Video stream running time in minutes;  0 applies no floor.",
         "runtime_min", AT_LEAST, value_key="min_runtime_min", kind="tv"),
    Rule("letterbox_bars", "Letterbox bars",
         "Baked-in bars measured by cropdetect on 16:9 and 4:3 frames;  also the least bar height cropdetect reports.",
         "letterbox_px", AT_MOST, value_key="letterbox_bars_px"),
    Rule("pal_speedup", "PAL speed-up",
         "25 fps at a PAL stored height, film material running 4 percent fast.",
         "pal_speedup", ABSENT),
    Rule("kept_audio", "Kept-language audio",
         "At least one audio track tagged with a kept language.",
         "kept_audio", PRESENT),
    Rule("extras_name", "Extras name",
         "A sample, trailer, featurette or other extras word in the file or folder name.",
         "extras", ABSENT),
    _bitrate_rule(SD, "SD"),
    _bitrate_rule(HD720, "720p"),
    _bitrate_rule(HD1080, "1080p"),
    _bitrate_rule(UHD, "2160p"),
    Rule("max_size_gb", "File size",
         "A guide rather than a limit:  a prompt to check whether the grain tune was missed.",
         "gb", AT_MOST, value_key="max_size_gb", ignored=True),
    Rule("episodes_in_file", "Episodes in file",
         "A single-numbered episode whose length says two episodes, with no next episode in its season folder.",
         "episodes_in_file", ABSENT, checks=("episodes in file",)),
    Rule("dv_record", "Dolby Vision record",
         "A Dolby Vision RPU in the bitstream needs the container's configuration record;  a header edit cannot write it.",
         "dv_record_gap", ABSENT, gates=False),
    Rule("container", "Container",
         "Matroska, reached by a remux.",
         "container_format", PASSES, repairable=True, checks=("container",), fixed="matroska"),
    Rule("foreign_tracks", "Foreign language tracks",
         "Audio and subtitle tracks outside the kept languages, dropped by the language strip.",
         "foreign_tracks", PASSES, repairable=True, checks=("foreign tracks",), fixed="none"),
    Rule("default_audio", "Default audio tracks",
         "Exactly one audio track flagged default.",
         "audio_default_count", PASSES, repairable=True, checks=("audio default count",), fixed="1"),
    Rule("default_subtitles", "Default non-forced subtitles",
         "No non-forced subtitle track flagged default.",
         "subtitle_default_count", PASSES, repairable=True, checks=("subtitle defaults",), fixed="0"),
    Rule("forced_default", "Default forced subtitles",
         "One forced track flagged default when any exists, none otherwise.",
         "subtitle_forced_default_count", PASSES, repairable=True, checks=("forced subtitle default",),
         fixed="1 when forced tracks exist, else 0"),
    Rule("named_forced", "Subtitles named forced",
         "A kept-language track named forced carries the forced flag.",
         "subtitle_named_forced", PASSES, repairable=True, checks=("forced by name only",), fixed="0"),
    Rule("video_language", "Video track language",
         "The video track is tagged eng;  cover art stays und.",
         "video_language", PASSES, repairable=True, checks=("video language",), fixed="eng"),
    Rule("tag_structure", "Tag structure",
         "Every expected target on its own Tag, no slash-named Simple.",
         "tag_structure", PASSES, repairable=True, checks=("tag structure",), fixed="targeted"),
    Rule("names", "Folder and file names",
         "Names equal those the tag block would publish.",
         "names", PASSES, repairable=True,
         checks=("folder name", "file name", "show folder", "season folder"), fixed="match the tag"),
    Rule("component_rules", "Path component rules",
         "No unsafe character, trailing period or space, or reserved device name.",
         "component_rules", PASSES, repairable=True, checks=("component rules",), fixed="none broken"),
    Rule("segment_title", "Segment title",
         "The segment Info title equals the tag TITLE.",
         "segment_title", PASSES, repairable=True, checks=("segment title",), fixed="equals tag TITLE"),
    Rule("stats_ratio_min", "Statistics ratio floor",
         "Summed NUMBER_OF_BYTES against the file size;  near zero means missing statistics.",
         "statistics_ratio", ABOVE, value_key="stats_ratio_min", repairable=True),
    Rule("stats_ratio_max", "Statistics ratio ceiling",
         "Above roughly 1.5 the statistics are stale rather than compressed subtitles.",
         "statistics_ratio", AT_MOST, value_key="stats_ratio_max", repairable=True),
    Rule("hdr_declaration", "HDR declaration gap",
         "The container declares every HDR figure the bitstream carries.",
         "hdr_declaration_gap", PASSES, repairable=True,
         checks=("mastering display", "content light level"), fixed="none"),
)

#----- one rule per remaining column, shipped ignored with an empty standard until the operator sets one.
_ruled = {r.column for r in EXPLICIT}
GENERATED = tuple(
    Rule(
        "col_" + c.key, c.label[:1].upper() + c.label[1:],
        "Off until a standard is set;  switched on, it gates imports.",
        c.key,
        AT_LEAST if c.type == NUMBER else IS,
        value_key="std_" + c.key,
        ignored=True,
        generated=True,
    )
    for c in COLUMNS
    if c.key not in _ruled and c.key not in UNRULED
)
del _ruled

RULES = EXPLICIT + GENERATED
RULE_BY_KEY = {r.key: r for r in RULES}


#----- Evaluation
def _number(value):
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _yes(value):
    return str(value).strip().lower() in ("yes", "true", "1", "on")


def passes(method, value, standard, column_type=NUMBER):
    if method == ABSENT:
        return not value
    if method == PRESENT:
        return bool(value)
    if column_type == BOOL:
        return bool(value) == _yes(standard)
    if column_type == TEXT:
        if method == IS:
            return str(value).strip().lower() == str(standard or "").strip().lower()
        return True
    number = _number(value)
    target = _number(standard)
    #----- a figure or a standard that is not a number cannot break a numeric rule.
    if number is None or target is None:
        return True
    if method == AT_LEAST:
        return number >= target
    if method == AT_MOST:
        return number <= target
    if method == ABOVE:
        return number > target
    if method in (EXACTLY, IS):
        return number == target
    return True


def _show(value, unit=None):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return "%s%s" % (value, " " + unit if unit else "")


def _reason(rule, value, standard):
    column = COLUMN_BY_KEY.get(rule.column)
    unit = column.unit if column else None
    if rule.method == ABSENT:
        return "%s:  %s" % (rule.label, _show(value, unit) if not isinstance(value, bool) else "present")
    if rule.method == PRESENT:
        return "%s:  none" % rule.label
    return "%s %s, standard %s %s" % (rule.label, _show(value, unit), rule.method, _show(standard, unit))


def _broken(rule, reason):
    return {
        "rule": rule.key,
        "label": rule.label,
        "column": rule.column,
        "reason": reason,
        "repairable": rule.repairable,
    }


def evaluate(values, kind, profile, checks=None, gating_only=False):
    profile = profile or {}
    broken = []
    for rule in RULES:
        if rule.kind and rule.kind != kind:
            continue
        if profile.get(rule.ignore_key, rule.ignored):
            continue
        if gating_only and not rule.gates:
            continue
        #----- a check rule is read off the audit rows;  an arrival has none, so it is skipped rather than passed.
        if rule.checks:
            if checks is None:
                continue
            failed = [c for c in checks if c.get("check") in rule.checks and not c.get("ok")]
            if failed:
                broken.append(_broken(rule, "%s:  %s" % (
                    rule.label, "; ".join("%s" % c.get("actual") for c in failed),
                )))
            continue
        if rule.klass and values.get("res_class") != rule.klass:
            continue
        value = values.get(rule.column)
        #----- a column with no figure on this file breaks nothing.
        if value is None:
            continue
        standard = profile.get(rule.value_key) if rule.value_key else None
        column = COLUMN_BY_KEY.get(rule.column)
        if not passes(rule.method, value, standard, column.type if column else NUMBER):
            broken.append(_broken(rule, _reason(rule, value, standard)))
    return broken


def severity(broken):
    if any(b["repairable"] for b in broken):
        return "repair"
    if broken:
        return "below"
    return None


def flagged(broken):
    out = {}
    for b in broken:
        mark = "repair" if b["repairable"] else "below"
        if out.get(b["column"]) != "repair":
            out[b["column"]] = mark
    return out
