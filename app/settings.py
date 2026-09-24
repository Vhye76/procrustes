import json
import logging
import os
import re
import threading
import time

from . import audit, compare, config as configmod, encode, episodes, media, provider as providermod, rules, standards

log = logging.getLogger("settings")

KINDS = ("movie", "tv")

GROUPS = (
    ("pipeline", "Pipeline", "settings"),
    ("encoding", "Encoding", "settings"),
    ("probes", "Probes", "settings"),
    ("standards", "Minimum standards", "standards"),
    ("comparison", "Comparison", "settings"),
    ("matching", "Matching and provider", "settings"),
    ("access", "Access", "account"),
)

X265_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower",
    "veryslow", "placebo",
)
X265_TUNES = ("none", "grain", "animation", "psnr", "ssim", "fastdecode", "zerolatency")
PIX_FMTS = ("yuv420p", "yuv420p10le")
QSV_PRESETS = ("veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
HEVC_DEVICES = ("cpu", "gpu", "both")
#----- PARAMS admits an x265 or svt-av1 params string and nothing shell-like;  the value is one argv entry, never a shell word.
DEBLOCK = r"^-?\d+,-?\d+$"
PARAMS = r"^[A-Za-z0-9_=:.,\-]*$"
TOKEN = r"^[a-z0-9_]+$"


class SettingsError(ValueError):
    def __init__(self, errors):
        self.errors = dict(errors)
        super().__init__("; ".join("%s: %s" % item for item in sorted(self.errors.items())))


class Setting:
    def __init__(self, key, group, label, help, type, default, per_kind=False,
                 minimum=None, maximum=None, choices=None, pattern=None, step=None):
        self.key = key
        self.group = group
        self.label = label
        self.help = help
        self.type = type
        self.default = default
        self.per_kind = per_kind
        self.minimum = minimum
        self.maximum = maximum
        self.choices = choices
        self.pattern = pattern
        self.step = step

    def storage_keys(self):
        if self.per_kind:
            return ["%s.%s" % (self.key, kind) for kind in KINDS]
        return [self.key]

    def default_for(self, kind=None):
        if self.per_kind:
            return self.default[kind]
        return self.default

    def as_dict(self):
        return {
            "key": self.key,
            "group": self.group,
            "label": self.label,
            "help": self.help,
            "type": self.type,
            "default": self.default,
            "per_kind": self.per_kind,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": list(self.choices) if self.choices else None,
            "pattern": self.pattern,
            "step": self.step,
        }


def _kinds(value):
    return {"movie": value, "tv": value}


#----- The registry;  a default that replaced a module constant is that constant, so the two cannot drift.
SETTINGS = (
    Setting("max_jobs", "pipeline", "Assessment workers",
            "Threads taking a title through probe, standards, identification, comparison and routing.",
            "int", 3, minimum=1, maximum=64),
    Setting("gpu_slots", "pipeline", "GPU encode slots",
            "Encode threads on the GPU pool, av1_qsv and hevc_qsv;  the bound on GPU-side staged copies.",
            "int", 1, minimum=0, maximum=16),
    Setting("cpu_slots", "pipeline", "CPU encode slots",
            "Encode threads on the CPU pool, each at the encoder thread count divided by this.",
            "int", 1, minimum=0, maximum=16),
    Setting("encode_headroom", "pipeline", "Encode headroom",
            "Multiple of the source size that must be free in the encode area before a job is admitted.",
            "float", 3.0, minimum=1.0, maximum=20.0, step=0.1),
    Setting("encode_threads", "pipeline", "Encoder threads",
            "Threads available to the encoders in total;  0 autodetects from the cgroup CPU quota.",
            "int", 0, minimum=0, maximum=256),
    Setting("poll_interval", "pipeline", "Poll interval",
            "Seconds between scans of the import folder;  must stay below the quiet window.",
            "int", 15, minimum=1, maximum=3600),
    Setting("mtime_quiet", "pipeline", "Quiet window",
            "Seconds a file must be untouched before it counts as stable.",
            "int", 30, minimum=2, maximum=86400),
    Setting("job_reclaim_interval", "pipeline", "Job reclaim interval",
            "Seconds between passes that remove encode job directories no running title holds.",
            "int", 300, minimum=60, maximum=86400),
    Setting("retry_max_attempts", "pipeline", "Retry attempts",
            "Transient failures retried this many times before the title holds for a person.",
            "int", 6, minimum=1, maximum=50),
    Setting("retry_base_delay", "pipeline", "Retry base delay",
            "Seconds before the first retry;  each later retry doubles it.",
            "int", 120, minimum=5, maximum=86400),

    Setting("output_codec", "encoding", "Output codec",
            "Codec the encoder produces;  a source already in a passthrough codec is never re-encoded.",
            "choice", _kinds("hevc"), per_kind=True, choices=configmod.VALID_CODECS),
    Setting("encode_sd", "encoding", "Encode SD sources",
            "Off passes an SD source through untouched;  on sends it to the encoder.",
            "bool", _kinds(False), per_kind=True),
    Setting("passthrough_codecs", "encoding", "Passthrough codecs",
            "Source codecs that are never re-encoded, comma separated.",
            "list", list(encode.PASSTHROUGH_CODECS), pattern=TOKEN),
    Setting("x265_preset", "encoding", "x265 preset",
            "Speed against compression for libx265.",
            "choice", _kinds(encode.X265_PRESET), per_kind=True, choices=X265_PRESETS),
    Setting("x265_crf", "encoding", "x265 CRF",
            "Quality target for libx265;  lower is higher quality and larger.",
            "int", _kinds(18), per_kind=True, minimum=0, maximum=51),
    Setting("x265_aq_mode", "encoding", "x265 aq-mode, clean source",
            "Adaptive quantisation mode on a source the grain probe calls clean.",
            "int", _kinds(3), per_kind=True, minimum=0, maximum=4),
    Setting("x265_aq_mode_film", "encoding", "x265 aq-mode, grainy source",
            "Adaptive quantisation mode on a source the grain probe calls grainy.",
            "int", _kinds(4), per_kind=True, minimum=0, maximum=4),
    Setting("x265_tune_film", "encoding", "x265 tune, grainy source",
            "Tune applied on a grainy source;  none applies no tune.",
            "choice", _kinds("grain"), per_kind=True, choices=X265_TUNES),
    Setting("x265_psy_rd", "encoding", "x265 psy-rd",
            "Psychovisual rate-distortion strength.",
            "float", _kinds(2.0), per_kind=True, minimum=0.0, maximum=5.0, step=0.1),
    Setting("x265_psy_rdoq", "encoding", "x265 psy-rdoq",
            "Psychovisual strength in rate-distortion optimised quantisation.",
            "float", _kinds(1.0), per_kind=True, minimum=0.0, maximum=50.0, step=0.1),
    Setting("x265_deblock", "encoding", "x265 deblock",
            "Deblocking filter offsets as 'alpha,beta'.",
            "str", _kinds("-1,-1"), per_kind=True, pattern=DEBLOCK),
    Setting("x265_pix_fmt", "encoding", "x265 pixel format",
            "Output pixel format;  yuv420p10le is 10-bit Main 10.",
            "choice", _kinds("yuv420p10le"), per_kind=True, choices=PIX_FMTS),
    Setting("x265_extra_params", "encoding", "x265 extra params",
            "Appended verbatim to the x265 params string, colon separated, after the built values.",
            "str", _kinds(""), per_kind=True, pattern=PARAMS),
    Setting("x265_dv_vbv_kbps", "encoding", "x265 Dolby Vision VBV",
            "vbv-maxrate and vbv-bufsize in kbps on a Dolby Vision encode, which x265 requires.",
            "int", encode.X265_DV_VBV_KBPS, minimum=1000, maximum=400000),
    Setting("svtav1_preset", "encoding", "SVT-AV1 preset",
            "Speed against compression for libsvtav1, 0 slowest to 13 fastest.",
            "int", _kinds(int(encode.SVTAV1_PRESET)), per_kind=True, minimum=0, maximum=13),
    Setting("svtav1_crf", "encoding", "SVT-AV1 CRF",
            "Quality target for libsvtav1.",
            "int", _kinds(encode.SVTAV1_CRF), per_kind=True, minimum=0, maximum=63),
    Setting("svtav1_params", "encoding", "SVT-AV1 params",
            "The svtav1-params string ahead of the thread count.",
            "str", _kinds(encode.SVTAV1_PARAMS), per_kind=True, pattern=PARAMS),
    Setting("svtav1_pix_fmt", "encoding", "SVT-AV1 pixel format",
            "Output pixel format for libsvtav1.",
            "choice", _kinds("yuv420p10le"), per_kind=True, choices=PIX_FMTS),
    Setting("qsv_preset", "encoding", "QSV preset",
            "Speed against compression for av1_qsv.",
            "choice", _kinds(encode.QSV_PRESET), per_kind=True, choices=QSV_PRESETS),
    Setting("qsv_global_quality", "encoding", "QSV global quality",
            "Quality target for av1_qsv;  lower is higher quality.",
            "int", _kinds(encode.QSV_GLOBAL_QUALITY), per_kind=True, minimum=1, maximum=63),
    Setting("hevc_devices", "encoding", "HEVC devices",
            "Pools an HEVC encode may run on;  both lets whichever is free take the next title.",
            "choice", _kinds(encode.HEVC_DEVICES), per_kind=True, choices=HEVC_DEVICES),
    Setting("hevc_qsv_preset", "encoding", "HEVC QSV preset",
            "Speed against compression for hevc_qsv.",
            "choice", _kinds(encode.HEVC_QSV_PRESET), per_kind=True, choices=QSV_PRESETS),
    Setting("hevc_qsv_global_quality", "encoding", "HEVC QSV global quality",
            "Quality target for hevc_qsv;  lower is higher quality, and the scale is not x265's CRF.",
            "int", _kinds(encode.HEVC_QSV_GLOBAL_QUALITY), per_kind=True, minimum=1, maximum=51),

    Setting("grain_threshold", "probes", "Grain threshold",
            "Denoise delta above which a source counts as grainy.",
            "float", _kinds(media.GRAIN_THRESHOLD), per_kind=True, minimum=0.01, maximum=0.99, step=0.01),
    Setting("grain_sample_seconds", "probes", "Probe sample length",
            "Seconds the grain and field probes decode.",
            "int", media.GRAIN_SAMPLE_SECONDS, minimum=2, maximum=300),
    Setting("grain_sample_position", "probes", "Probe sample position",
            "Fraction of the running time at which the grain and field samples start.",
            "float", media.GRAIN_SAMPLE_POSITION, minimum=0.0, maximum=0.95, step=0.01),
    Setting("grain_probe_crf", "probes", "Grain probe CRF",
            "CRF of the two sample encodes the grain probe compares.",
            "int", int(media.GRAIN_PROBE_CRF), minimum=0, maximum=51),
    Setting("grain_probe_preset", "probes", "Grain probe preset",
            "x265 preset of the two sample encodes.",
            "choice", media.GRAIN_PROBE_PRESET, choices=X265_PRESETS),
    Setting("field_telecine_share", "probes", "Telecine share",
            "Share of frames carrying a repeated field at or above which the source is telecined.",
            "float", media.FIELD_TELECINE_SHARE, minimum=0.01, maximum=0.5, step=0.01),
    Setting("crop_sample_count", "probes", "Crop samples",
            "Plausible cropdetect samples collected across the running time.",
            "int", media.CROP_SAMPLE_COUNT, minimum=1, maximum=30),
    Setting("crop_sample_seconds", "probes", "Crop sample length",
            "Seconds each cropdetect sample decodes.",
            "int", media.CROP_SAMPLE_SECONDS, minimum=1, maximum=60),
    Setting("crop_sample_attempts", "probes", "Crop sample attempts",
            "Samples tried before cropdetect gives up, rejected ones included.",
            "int", media.CROP_SAMPLE_ATTEMPTS, minimum=1, maximum=60),
    Setting("crop_black_level_factor", "probes", "Crop black level factor",
            "cropdetect limit as a multiple of the measured black level.",
            "float", media.CROP_BLACK_LEVEL_FACTOR, minimum=1.0, maximum=5.0, step=0.1),
    Setting("crop_black_level_cap", "probes", "Crop black level cap",
            "Ceiling on the cropdetect limit as a fraction of the bit-depth range.",
            "float", media.CROP_BLACK_LEVEL_CAP, minimum=0.01, maximum=0.5, step=0.01),
    Setting("crop_secondary_share", "probes", "Variable aspect share",
            "Share of samples at which a second crop geometry is recorded as a variable aspect.",
            "float", media.CROP_SECONDARY_SHARE, minimum=0.01, maximum=0.5, step=0.01),

    Setting("min_display_width", "standards", "Minimum display width",
            "Display width below which a title fails the standards;  0 applies no floor.",
            "int", {"movie": standards.MOVIE_MIN_DISPLAY_WIDTH, "tv": 0}, per_kind=True,
            minimum=0, maximum=7680),
    Setting("min_display_height", "standards", "Minimum display height",
            "Display height below which a title fails the standards;  0 applies no floor.",
            "int", {"movie": standards.MOVIE_MIN_DISPLAY_HEIGHT, "tv": 0}, per_kind=True,
            minimum=0, maximum=4320),
    Setting("min_runtime_min", "standards", "Minimum runtime",
            "Minutes below which a title fails the standards;  0 applies no floor.",
            "int", {"movie": standards.MOVIE_MIN_RUNTIME_S // 60, "tv": standards.TV_MIN_RUNTIME_S // 60},
            per_kind=True, minimum=0, maximum=600),
    Setting("letterbox_bars_px", "standards", "Letterbox bars",
            "Baked-in bars at or above this many pixels fail the standards and are cropped by the encoder.",
            "int", standards.LETTERBOX_MAX_BARS_PX, minimum=1, maximum=1000),
    Setting("keep_langs", "standards", "Kept languages",
            "Audio and subtitle language tags kept at ingest, comma separated;  everything else is dropped.",
            "list", list(standards.KEEP_LANGS), pattern=TOKEN),
    Setting("class_720_width", "standards", "720p class width",
            "Display width at or above which a file is at least 720p;  below every class it is SD.",
            "int", rules.CLASS_BOUNDS[rules.HD720][0], minimum=1, maximum=7680),
    Setting("class_720_height", "standards", "720p class height",
            "Display height at or above which a file is at least 720p, whatever its width.",
            "int", rules.CLASS_BOUNDS[rules.HD720][1], minimum=1, maximum=4320),
    Setting("class_1080_width", "standards", "1080p class width",
            "Display width at or above which a file is at least 1080p.",
            "int", rules.CLASS_BOUNDS[rules.HD1080][0], minimum=1, maximum=7680),
    Setting("class_1080_height", "standards", "1080p class height",
            "Display height at or above which a file is at least 1080p, whatever its width.",
            "int", rules.CLASS_BOUNDS[rules.HD1080][1], minimum=1, maximum=4320),
    Setting("class_2160_width", "standards", "2160p class width",
            "Display width at or above which a file is 2160p.",
            "int", rules.CLASS_BOUNDS[rules.UHD][0], minimum=1, maximum=7680),
    Setting("class_2160_height", "standards", "2160p class height",
            "Display height at or above which a file is 2160p, whatever its width.",
            "int", rules.CLASS_BOUNDS[rules.UHD][1], minimum=1, maximum=4320),
    Setting("min_kbps_sd", "standards", "SD video bitrate",
            "Weighted video bitrate floor in kbps for an SD file.",
            "int", rules.MIN_KBPS[rules.SD], minimum=0, maximum=500000),
    Setting("min_kbps_720", "standards", "720p video bitrate",
            "Weighted video bitrate floor in kbps for a 720p file.",
            "int", rules.MIN_KBPS[rules.HD720], minimum=0, maximum=500000),
    Setting("min_kbps_1080", "standards", "1080p video bitrate",
            "Weighted video bitrate floor in kbps for a 1080p file.",
            "int", rules.MIN_KBPS[rules.HD1080], minimum=0, maximum=500000),
    Setting("min_kbps_2160", "standards", "2160p video bitrate",
            "Weighted video bitrate floor in kbps for a 2160p file.",
            "int", rules.MIN_KBPS[rules.UHD], minimum=0, maximum=500000),
    Setting("max_size_gb", "standards", "File size",
            "Size in GB above which a file is highlighted.",
            "float", float(rules.MAX_SIZE_GB), minimum=0.0, maximum=1000.0, step=0.1),
    Setting("stats_ratio_min", "standards", "Statistics ratio floor",
            "Summed NUMBER_OF_BYTES against the file size, at or below which statistics are missing.",
            "float", rules.STATS_RATIO_MIN, minimum=0.0, maximum=10.0, step=0.01),
    Setting("stats_ratio_max", "standards", "Statistics ratio ceiling",
            "Summed NUMBER_OF_BYTES against the file size, above which statistics are stale.",
            "float", rules.STATS_RATIO_MAX, minimum=0.0, maximum=10.0, step=0.01),

    Setting("pixel_tolerance", "comparison", "Pixel tolerance",
            "Relative difference in display or picture pixels below which gates 2 and 3 cast no vote.",
            "float", compare.PIXEL_TOLERANCE, minimum=0.0, maximum=1.0, step=0.01),
    Setting("bitrate_tolerance", "comparison", "Bitrate tolerance",
            "Relative difference in weighted video bitrate below which gate 6 casts no vote.",
            "float", compare.BITRATE_TOLERANCE, minimum=0.0, maximum=1.0, step=0.01),
    Setting("codec_efficiency", "comparison", "Codec efficiency",
            "Bitrate weighting per codec relative to h264 at 1.0;  an unlisted codec weighs 1.0.",
            "table", dict(compare.CODEC_EFFICIENCY)),
    Setting("edition_runtime_tolerance_s", "comparison", "Edition runtime tolerance",
            "Video runtime difference in seconds at or below which an edition read from the arrival name is the same cut as the folder's file;  the label is dropped and the pair compared.",
            "int", compare.EDITION_RUNTIME_TOLERANCE_S, minimum=0, maximum=3600),

    Setting("title_cutoff", "matching", "Title cutoff",
            "Fuzzy score at or above which an episode title or a full-text search hit matches.",
            "float", episodes.FUZZY_CUTOFF, minimum=0.5, maximum=1.0, step=0.01),
    Setting("contained_score", "matching", "Containment score",
            "Score given when the searched title sits whole inside an entity's label.",
            "float", providermod.CONTAINED_SCORE, minimum=0.5, maximum=1.0, step=0.01),
    Setting("max_range_span", "matching", "Maximum range span",
            "Episodes an 'E01-E03' range may cover;  wider reads as its first episode.",
            "int", episodes.MAX_RANGE_SPAN, minimum=1, maximum=10),
    Setting("range_duration_ratio", "matching", "Range duration ratio",
            "A single-numbered library episode this many times its season's median length, with no next episode beside it, is listed as two episodes in one file.",
            "float", audit.RANGE_DURATION_RATIO, minimum=1.2, maximum=3.0, step=0.1),
    Setting("candidate_limit", "matching", "Candidate limit",
            "Candidates listed per source on an identification hold.",
            "int", providermod.CANDIDATE_LIMIT, minimum=1, maximum=50),
    Setting("provider_throttle_s", "matching", "Provider throttle",
            "Seconds between requests to Wikidata, TMDB and TVDB.",
            "float", providermod.THROTTLE_SECONDS, minimum=0.0, maximum=60.0, step=0.5),
    Setting("provider_timeout_s", "matching", "Provider timeout",
            "Seconds a provider request may take before it counts as unreachable.",
            "int", providermod.TIMEOUT, minimum=5, maximum=300),

    Setting("auth_enabled", "access", "Require authentication",
            "Off opens the dashboard and the API to anyone who can reach the port.",
            "bool", True),
    Setting("session_hours", "access", "Session lifetime",
            "Hours a login stays valid;  signing out ends it sooner.",
            "int", 168, minimum=1, maximum=8760),
)


#----- Minimum Standards:  a standard for every generated rule, and an ignore flag for every rule
def _standard_setting(rule):
    column = rules.COLUMN_BY_KEY[rule.column]
    if column.type == rules.NUMBER:
        return Setting(rule.value_key, "standards", rule.label, rule.help, "float", 0.0, step=0.01)
    if column.type == rules.BOOL:
        return Setting(rule.value_key, "standards", rule.label, rule.help, "choice", "yes", choices=("yes", "no"))
    return Setting(rule.value_key, "standards", rule.label, rule.help, "str", "")


RULE_SETTINGS = tuple(_standard_setting(r) for r in rules.GENERATED) + tuple(
    Setting(r.ignore_key, "standards", "Ignore %s" % r.label,
            "Checked, the figure is still measured and shown but the rule neither highlights nor gates.",
            "bool", r.ignored)
    for r in rules.RULES
)
SETTINGS = SETTINGS + RULE_SETTINGS

BY_KEY = {s.key: s for s in SETTINGS}
POOL_KEYS = ("max_jobs", "gpu_slots", "cpu_slots")
#----- written only through the account endpoint, which checks a factor first;  the settings batch refuses them.
SWITCH_KEYS = ("auth_enabled",)
SWITCH_MESSAGE = "set from the User Settings page"
MIGRATED_PAL = "pal_speedup_check"


#----- Coercion and validation of one value, from JSON or from the form
def _coerce(setting, value):
    t = setting.type
    if t == "int":
        #----- bool is an int subclass, and a checkbox posted to an int field is a page defect, not a value.
        if isinstance(value, bool):
            raise ValueError("must be an integer")
        try:
            out = int(str(value).strip()) if not isinstance(value, int) else value
        except ValueError:
            raise ValueError("must be an integer")
    elif t == "float":
        if isinstance(value, bool):
            raise ValueError("must be a number")
        try:
            out = float(value)
        except (TypeError, ValueError):
            raise ValueError("must be a number")
    elif t == "bool":
        if isinstance(value, bool):
            out = value
        elif isinstance(value, (int, float)):
            out = bool(value)
        else:
            text = str(value).strip().lower()
            if text in ("1", "true", "yes", "on"):
                out = True
            elif text in ("0", "false", "no", "off", ""):
                out = False
            else:
                raise ValueError("must be true or false")
    elif t in ("str", "choice"):
        if not isinstance(value, str):
            raise ValueError("must be text")
        out = value.strip()
        if t == "choice":
            out = out.lower()
            if out not in setting.choices:
                raise ValueError("must be one of %s" % ", ".join(setting.choices))
    elif t == "list":
        if isinstance(value, str):
            items = value.split(",")
        elif isinstance(value, (list, tuple)):
            items = [str(v) for v in value]
        else:
            raise ValueError("must be a comma-separated list")
        out = []
        for item in items:
            item = item.strip().lower()
            if item and item not in out:
                out.append(item)
        if not out:
            raise ValueError("must list at least one value")
        if setting.pattern:
            for item in out:
                if not re.match(setting.pattern, item):
                    raise ValueError("%r is not a valid entry" % item)
    elif t == "table":
        if isinstance(value, dict):
            pairs = list(value.items())
        elif isinstance(value, (list, tuple)):
            pairs = [(row[0], row[1]) for row in value if isinstance(row, (list, tuple)) and len(row) == 2]
        else:
            raise ValueError("must be a table of codec and factor")
        out = {}
        for name, factor in pairs:
            name = str(name).strip().lower()
            if not name:
                continue
            if not re.match(TOKEN, name):
                raise ValueError("%r is not a valid codec name" % name)
            try:
                factor = float(factor)
            except (TypeError, ValueError):
                raise ValueError("factor for %s must be a number" % name)
            if factor <= 0:
                raise ValueError("factor for %s must be above 0" % name)
            out[name] = factor
        if not out:
            raise ValueError("must list at least one codec")
    else:
        raise ValueError("unknown setting type %s" % t)

    if t in ("int", "float"):
        if setting.minimum is not None and out < setting.minimum:
            raise ValueError("must be at least %s" % setting.minimum)
        if setting.maximum is not None and out > setting.maximum:
            raise ValueError("must be at most %s" % setting.maximum)
    if t == "str" and setting.pattern and not re.match(setting.pattern, out):
        raise ValueError("does not match the expected form")
    return out


#----- Rules that span keys, run on the merged view of a batch
def _cross_checks(values):
    errors = {}
    if values["poll_interval"] >= values["mtime_quiet"]:
        errors["poll_interval"] = "must be below the quiet window (%s)" % values["mtime_quiet"]
    if values["gpu_slots"] + values["cpu_slots"] < 1:
        errors["cpu_slots"] = "GPU and CPU slots must not both be zero"
    #----- each boundary list must ascend, or a class could never be reached.
    for side in ("width", "height"):
        keys = ["class_%s_%s" % (cls, side) for cls in (rules.HD720, rules.HD1080, rules.UHD)]
        for lower, upper in zip(keys, keys[1:]):
            if values[lower] >= values[upper]:
                errors[lower] = "must be below %s (%s)" % (upper, values[upper])
    return errors


def _split(storage_key):
    for kind in KINDS:
        suffix = "." + kind
        if storage_key.endswith(suffix):
            return storage_key[: -len(suffix)], kind
    return storage_key, None


#----- The live settings, one instance per process
class Settings:
    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._values = {}
        self._load()

    def _load(self):
        self._migrate()
        loaded = {}
        for storage_key, text in self.store.settings_all():
            key, kind = _split(storage_key)
            setting = BY_KEY.get(key)
            #----- a row from a version that scoped the key differently is dropped, not misread.
            if setting is None or bool(setting.per_kind) != (kind is not None):
                log.warning("stored setting %s is not in the registry and is ignored", storage_key)
                continue
            try:
                raw = json.loads(text)
                loaded[storage_key] = _coerce(setting, raw)
            except (ValueError, TypeError) as exc:
                log.warning("stored setting %s is malformed (%s), default applies", storage_key, exc)
        with self._lock:
            self._values = loaded
        log.info("settings loaded, %d stored value(s)", len(loaded))

    #----- a stored PAL check becomes the PAL rule's ignore flag, inverted, once.
    def _migrate(self):
        stored = dict(self.store.settings_all())
        if MIGRATED_PAL in stored:
            try:
                checking = bool(json.loads(stored[MIGRATED_PAL]))
            except ValueError:
                checking = True
            target = rules.RULE_BY_KEY["pal_speedup"].ignore_key
            if target not in stored:
                self.store.settings_put([(target, json.dumps(not checking), time.time())])
            self.store.settings_delete([MIGRATED_PAL])
            log.info("setting %s migrated to %s=%s", MIGRATED_PAL, target, not checking)

    def get(self, key, kind=None):
        setting = BY_KEY[key]
        if setting.per_kind:
            if kind not in KINDS:
                raise ValueError("setting %s needs a kind" % key)
            storage_key = "%s.%s" % (key, kind)
        else:
            storage_key = key
        with self._lock:
            if storage_key in self._values:
                return self._values[storage_key]
        return setting.default_for(kind)

    def _resolved(self, kind):
        out = {}
        for setting in SETTINGS:
            out[setting.key] = self.get(setting.key, kind if setting.per_kind else None)
        return out

    #----- Resolution
    def threads(self):
        configured = self.get("encode_threads")
        if configured:
            return configured, "settings"
        detected = configmod.detect_cpus()
        if detected:
            return detected, "cgroup cpu.max"
        return os.cpu_count() or 1, "os.cpu_count"

    def profile(self, kind):
        #----- a title with no kind yet reads the movie values;  only the global keys matter before classification.
        if kind not in KINDS:
            kind = "movie"
        out = self._resolved(kind)
        out["kind"] = kind
        threads, source = self.threads()
        out["encode_threads_resolved"] = threads
        out["thread_source"] = source
        out["threads_per_job"] = max(1, threads // max(1, out["cpu_slots"]))
        return out

    #----- Writing
    def update(self, changes, internal=False):
        errors = {}
        staged = {}
        for key, value in (changes or {}).items():
            setting = BY_KEY.get(key)
            if setting is None:
                errors[key] = "unknown setting"
                continue
            if key in SWITCH_KEYS and not internal:
                errors[key] = SWITCH_MESSAGE
                continue
            if setting.per_kind:
                if not isinstance(value, dict):
                    errors[key] = "needs a value per kind"
                    continue
                for kind, item in value.items():
                    if kind not in KINDS:
                        errors["%s.%s" % (key, kind)] = "unknown kind"
                        continue
                    try:
                        staged["%s.%s" % (key, kind)] = _coerce(setting, item)
                    except ValueError as exc:
                        errors["%s.%s" % (key, kind)] = str(exc)
            else:
                try:
                    staged[key] = _coerce(setting, value)
                except ValueError as exc:
                    errors[key] = str(exc)
        if errors:
            raise SettingsError(errors)

        with self._lock:
            #----- the cross-key rules see the batch as it would stand, both kinds resolved.
            merged = dict(self._values)
            merged.update(staged)
            for kind in KINDS:
                view = {}
                for setting in SETTINGS:
                    storage_key = "%s.%s" % (setting.key, kind) if setting.per_kind else setting.key
                    view[setting.key] = merged.get(storage_key, setting.default_for(kind))
                errors.update(_cross_checks(view))
            if errors:
                raise SettingsError(errors)

            changed = []
            rows = []
            now = time.time()
            for storage_key, value in staged.items():
                key, kind = _split(storage_key)
                old = self._values.get(storage_key, BY_KEY[key].default_for(kind))
                #----- a value equal to the default is still stored, so the page and the banner show it as chosen.
                if old == value and storage_key in self._values:
                    continue
                rows.append((storage_key, json.dumps(value), now))
                if old != value:
                    changed.append((storage_key, old, value))
            if rows:
                self.store.settings_put(rows)
                for storage_key, text, _at in rows:
                    self._values[storage_key] = json.loads(text)
        return changed

    def reset(self, keys):
        storage_keys = []
        for key in keys or ():
            base, kind = _split(str(key))
            setting = BY_KEY.get(base)
            if setting is None:
                raise SettingsError({key: "unknown setting"})
            if base in SWITCH_KEYS:
                raise SettingsError({key: SWITCH_MESSAGE})
            if kind is None:
                storage_keys.extend(setting.storage_keys())
            elif setting.per_kind:
                storage_keys.append(key)
            else:
                raise SettingsError({key: "not a per-kind setting"})
        changed = []
        with self._lock:
            present = [k for k in storage_keys if k in self._values]
            if present:
                self.store.settings_delete(present)
            for storage_key in present:
                base, kind = _split(storage_key)
                changed.append((storage_key, self._values.pop(storage_key), BY_KEY[base].default_for(kind)))
        return changed

    #----- Reporting
    def describe(self):
        groups = []
        with self._lock:
            stored = dict(self._values)
        for name, label, page in GROUPS:
            rows = []
            for setting in SETTINGS:
                if setting.group != name:
                    continue
                row = setting.as_dict()
                if setting.per_kind:
                    row["value"] = {
                        kind: stored.get("%s.%s" % (setting.key, kind), setting.default[kind])
                        for kind in KINDS
                    }
                    row["source"] = {
                        kind: "stored" if "%s.%s" % (setting.key, kind) in stored else "default"
                        for kind in KINDS
                    }
                else:
                    row["value"] = stored.get(setting.key, setting.default)
                    row["source"] = "stored" if setting.key in stored else "default"
                rows.append(row)
            groups.append({"name": name, "label": label, "page": page, "settings": rows})
        return {"groups": groups}

    def as_dict(self):
        out = {}
        for setting in SETTINGS:
            if setting.per_kind:
                out[setting.key] = {kind: self.get(setting.key, kind) for kind in KINDS}
            else:
                out[setting.key] = self.get(setting.key)
        return out

    def banner(self):
        #----- one line per storage key, a '*' marking a stored value against a default.
        lines = []
        with self._lock:
            stored = set(self._values)
        width = max(len(s.key) for s in SETTINGS) + 6
        for setting in SETTINGS:
            if setting.per_kind:
                for kind in KINDS:
                    storage_key = "%s.%s" % (setting.key, kind)
                    mark = "*" if storage_key in stored else " "
                    lines.append("%-*s %s %s" % (width, storage_key, mark, _fmt(self.get(setting.key, kind))))
            else:
                mark = "*" if setting.key in stored else " "
                lines.append("%-*s %s %s" % (width, setting.key, mark, _fmt(self.get(setting.key))))
        return "\n".join(lines)


def _fmt(value):
    if isinstance(value, dict):
        return ", ".join("%s=%s" % (k, v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if value == "":
        return "(empty)"
    return str(value)
