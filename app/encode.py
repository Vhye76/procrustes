import logging
import os

log = logging.getLogger("encode")

PASSTHROUGH_CODECS = ("hevc", "av1")
SD_DISPLAY_HEIGHT = 720

SDR_PRIMARIES_SD = "smpte170m"
SDR_PRIMARIES_HD = "bt709"
X265_SDR_COLOUR = "colorprim=%s:transfer=%s:colormatrix=%s:range=limited"
X265_HDR_COLOUR = "colorprim=%s:transfer=%s:colormatrix=%s:range=limited:hdr10=1"
X265_DV_VBV_KBPS = 40000
CHROMATICITY_UNIT = 50000
LUMINANCE_UNIT = 10000

X265_PRESET = "slow"
SVTAV1_PRESET = "4"
SVTAV1_CRF = 24
SVTAV1_PARAMS = "tune=0:film-grain=8"

QSV_PRESET = "veryslow"
QSV_GLOBAL_QUALITY = 26

RENDER_NODE = os.environ.get("RENDER_NODE", "/dev/dri/renderD128")

PASSTHROUGH = "passthrough"
ENCODE = "encode"

LIBX265 = "libx265"
LIBSVTAV1 = "libsvtav1"
AV1_QSV = "av1_qsv"

CPU = "cpu"
GPU = "gpu"

DEVICE_BY_ENCODER = {LIBX265: CPU, LIBSVTAV1: CPU, AV1_QSV: GPU}


#----- The routing decision
#----- Field chains;  telecine removes one frame in five, interlaced keeps the count.
FIELD_FILTERS = {
    "telecine": "fieldmatch,yadif=deint=interlaced,decimate",
    "interlaced": "bwdif=mode=send_frame",
    "progressive": None,
}
TELECINE_FRAME_RATIO = 4.0 / 5.0


class Decision:
    def __init__(self, action, gate, reason, encoder=None, grain=None, notes=None, fields=None,
                 params=None):
        self.action = action
        self.gate = gate
        self.reason = reason
        self.encoder = encoder
        self.device = DEVICE_BY_ENCODER.get(encoder)
        self.grain = grain
        self.fields = fields
        self.notes = list(notes or [])
        self.params = dict(params) if params else None

    @property
    def is_passthrough(self):
        return self.action == PASSTHROUGH

    def as_dict(self):
        return {
            "action": self.action,
            "gate": self.gate,
            "reason": self.reason,
            "encoder": self.encoder,
            "device": self.device,
            "grain": self.grain,
            "fields": self.fields,
            "notes": self.notes,
            "params": self.params,
        }

    def __repr__(self):
        return "<Decision %s gate=%d encoder=%s device=%s>" % (
            self.action,
            self.gate,
            self.encoder,
            self.device,
        )


#----- Thread allocation
def _threads(params):
    value = (params or {}).get("threads_per_job")
    log.debug("encoder thread figure resolved to %s", value)
    if value:
        return max(1, int(value))
    return max(1, os.cpu_count() or 1)


def is_sd(video, floor=SD_DISPLAY_HEIGHT):
    return int(video.get("display_height") or 0) < int(floor or SD_DISPLAY_HEIGHT)


#----- The encoder subset of a profile;  stored with the decision at ROUTED, so a queued title keeps it.
PARAM_KEYS = (
    "x265_preset", "x265_crf", "x265_aq_mode", "x265_aq_mode_film", "x265_tune_film",
    "x265_psy_rd", "x265_psy_rdoq", "x265_deblock", "x265_pix_fmt", "x265_extra_params",
    "x265_dv_vbv_kbps", "svtav1_preset", "svtav1_crf", "svtav1_params", "svtav1_pix_fmt",
    "qsv_preset", "qsv_global_quality", "sd_display_height", "threads_per_job",
)


def encoder_params(profile, render_node=None):
    out = {key: profile.get(key) for key in PARAM_KEYS}
    out["render_node"] = render_node or RENDER_NODE
    return out


#----- The router
def select(video, kind, profile, grain=None, gpu_available=True, override=None):
    decision = _select(video, kind, profile, grain, gpu_available, override)
    log.debug(
        "router gate %s: %s, %s",
        decision.gate, decision.encoder or "passthrough", decision.reason,
    )
    return decision


def describe(decision):
    if decision.is_passthrough:
        return "gate %s: passthrough, %s" % (decision.gate, decision.reason)
    return "gate %s: %s, %s" % (decision.gate, decision.encoder, decision.reason)


def _select(video, kind, profile, grain=None, gpu_available=True, override=None):
    override = override or {}
    notes = []
    log.debug(
        "routing %s %s, grain=%s, gpu_available=%s, override=%s",
        kind, video.get("codec"), grain, gpu_available, override,
    )

    codec = (video.get("codec") or "").lower()
    if codec in (profile.get("passthrough_codecs") or PASSTHROUGH_CODECS):
        return Decision(
            PASSTHROUGH,
            1,
            "source video is already %s" % codec,
            grain=grain,
        )

    sd_floor = profile.get("sd_display_height") or SD_DISPLAY_HEIGHT
    if is_sd(video, sd_floor) and not profile.get("encode_sd"):
        return Decision(
            PASSTHROUGH,
            2,
            "SD %s, display height %s is below %d and SD encoding is off"
            % ("television" if kind == "tv" else "source", video.get("display_height"), sd_floor),
            grain=grain,
        )

    if "film" in override:
        grain = bool(override["film"])
        notes.append("grain forced to %s by encode.job" % grain)

    if video.get("dolby_vision"):
        return Decision(
            ENCODE,
            3,
            "Dolby Vision RPU present, an AV1 re-encode would discard it",
            encoder=LIBX265,
            grain=grain,
            notes=notes,
        )

    codec_target = (override.get("output_codec") or profile.get("output_codec") or "hevc").lower()

    if codec_target == "av1":
        if grain:
            return Decision(
                ENCODE,
                4,
                "AV1 requested and source is grainy, film-grain synthesis needs the CPU encoder",
                encoder=LIBSVTAV1,
                grain=grain,
                notes=notes,
            )
        if not gpu_available:
            notes.append("GPU unavailable, av1_qsv fell back to libsvtav1")
            return Decision(
                ENCODE,
                5,
                "AV1 requested but the GPU is unavailable",
                encoder=LIBSVTAV1,
                grain=grain,
                notes=notes,
            )
        return Decision(
            ENCODE,
            5,
            "AV1 requested and source is not grainy",
            encoder=AV1_QSV,
            grain=grain,
            notes=notes,
        )

    if grain:
        return Decision(
            ENCODE,
            6,
            "grainy source, x265 grain tune",
            encoder=LIBX265,
            grain=grain,
            notes=notes,
        )
    return Decision(
        ENCODE,
        7,
        "clean source, x265 default",
        encoder=LIBX265,
        grain=grain,
        notes=notes,
    )


#----- Command fragments
def sdr_stamp_primaries(video, sd_floor=SD_DISPLAY_HEIGHT):
    if video.get("hdr") or video.get("colour_tagged"):
        return None
    return SDR_PRIMARIES_SD if is_sd(video, sd_floor) else SDR_PRIMARIES_HD


def _map_args():
    return [
        "-map", "0:v:0",
        "-map", "0:a",
        "-map", "0:s?",
        "-map", "0:t?",
        "-map_chapters", "0",
    ]


def _tail_args():
    return ["-c:a", "copy", "-c:s", "copy", "-map_metadata", "0"]


def _sdr_ffmpeg_colour_args(primaries):
    return _ffmpeg_colour_args(primaries, primaries, primaries)


def _ffmpeg_colour_args(primaries, transfer, matrix):
    return [
        "-color_primaries", primaries,
        "-color_trc", transfer,
        "-colorspace", matrix,
        "-color_range", "tv",
    ]


def hdr_colour(video):
    if not video.get("hdr"):
        return None
    prim = (video.get("color_primaries") or "").lower()
    trc = (video.get("color_transfer") or "").lower()
    spc = (video.get("color_space") or "").lower()
    if not (prim and trc and spc):
        return None
    return prim, trc, spc


def _master_display(md):
    def c(value):
        return int(round(float(value or 0.0) * CHROMATICITY_UNIT))

    def l(value):
        return int(round(float(value or 0.0) * LUMINANCE_UNIT))

    return "G(%d,%d)B(%d,%d)R(%d,%d)WP(%d,%d)L(%d,%d)" % (
        c(md.get("green_x")), c(md.get("green_y")),
        c(md.get("blue_x")), c(md.get("blue_y")),
        c(md.get("red_x")), c(md.get("red_y")),
        c(md.get("white_point_x")), c(md.get("white_point_y")),
        l(md.get("max_luminance")), l(md.get("min_luminance")),
    )


def x265_hdr_params(video, dv_vbv_kbps=X265_DV_VBV_KBPS):
    colour = hdr_colour(video)
    if colour is None:
        return None
    parts = [X265_HDR_COLOUR % colour]
    #----- the bitstream figures win;  the container's are the fallback.
    md = video.get("mastering_bitstream") or video.get("mastering_display")
    if md:
        parts.append("master-display=%s" % _master_display(md))
    cl = video.get("content_light_bitstream") or video.get("content_light")
    if cl:
        parts.append("max-cll=%d,%d" % (int(cl.get("max_content") or 0), int(cl.get("max_average") or 0)))
    if video.get("dolby_vision"):
        parts.append("vbv-maxrate=%d:vbv-bufsize=%d" % (int(dv_vbv_kbps), int(dv_vbv_kbps)))
    return ":".join(parts)


#----- The head of the x265 params string, before colour, extra params and pools
def x265_tuning(params, grain):
    if grain:
        parts = ["aq-mode=%d" % int(params.get("x265_aq_mode_film", 4))]
        tune = str(params.get("x265_tune_film") or "grain").lower()
        if tune != "none":
            parts.append("tune=%s" % tune)
    else:
        parts = ["aq-mode=%d" % int(params.get("x265_aq_mode", 3))]
    parts.append("psy-rd=%s" % params.get("x265_psy_rd", 2.0))
    parts.append("psy-rdoq=%s" % params.get("x265_psy_rdoq", 1.0))
    parts.append("deblock=%s" % (params.get("x265_deblock") or "-1,-1"))
    return ":".join(parts)


#----- Command builders
def build_command(decision, src, dst, video, params, crop=None, crf=None):
    if decision.is_passthrough:
        raise ValueError("build_command called on a passthrough decision")
    if video.get("dolby_vision") and decision.encoder != LIBX265:
        raise ValueError(
            "Dolby Vision RPU cannot be carried by %s, only libx265 preserves it"
            % decision.encoder
        )
    params = params or {}

    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1"]

    if decision.encoder == AV1_QSV:
        node = params.get("render_node") or RENDER_NODE
        args += ["-init_hw_device", "qsv=hw:%s" % node, "-filter_hw_device", "hw"]

    args += ["-i", str(src)]
    args += _map_args()

    filters = []
    #----- fields are matched on the full stored frame, so the field chain precedes the crop.
    field_filter = FIELD_FILTERS.get(decision.fields or "progressive")
    if field_filter:
        filters.append(field_filter)
    if crop:
        filters.append(crop)
    if decision.encoder == AV1_QSV:
        filters += ["format=p010le", "hwupload=extra_hw_frames=64"]
    if filters:
        args += ["-vf", ",".join(filters)]

    stamp = sdr_stamp_primaries(video, params.get("sd_display_height") or SD_DISPLAY_HEIGHT)
    hdr = hdr_colour(video)

    if decision.encoder == LIBX265:
        x265 = x265_tuning(params, decision.grain)
        if stamp:
            x265 = "%s:%s" % (x265, X265_SDR_COLOUR % (stamp, stamp, stamp))
        elif hdr:
            x265 = "%s:%s" % (x265, x265_hdr_params(video, params.get("x265_dv_vbv_kbps") or X265_DV_VBV_KBPS))
        #----- the operator's string goes after the built values and before pools, so it can override any of them.
        extra = (params.get("x265_extra_params") or "").strip(":")
        if extra:
            x265 = "%s:%s" % (x265, extra)
        x265 = "%s:pools=%d" % (x265, _threads(params))
        args += [
            "-c:v", "libx265",
            "-preset", str(params.get("x265_preset") or X265_PRESET),
            "-crf", str(crf if crf is not None else params.get("x265_crf", 18)),
            "-pix_fmt", str(params.get("x265_pix_fmt") or "yuv420p10le"),
        ]
        if video.get("dolby_vision"):
            args += ["-dolbyvision", "1"]
        args += [
            #----- colour travels inside the params string on this path, not as ffmpeg flags.
            "-x265-params", x265,
        ]
        if stamp or hdr:
            args += ["-color_range", "tv"]

    elif decision.encoder == LIBSVTAV1:
        svt = (params.get("svtav1_params") or SVTAV1_PARAMS).strip(":")
        args += [
            "-c:v", "libsvtav1",
            "-preset", str(params.get("svtav1_preset", SVTAV1_PRESET)),
            "-crf", str(crf if crf is not None else params.get("svtav1_crf", SVTAV1_CRF)),
            "-pix_fmt", str(params.get("svtav1_pix_fmt") or "yuv420p10le"),
            "-svtav1-params", "%s:lp=%d" % (svt, _threads(params)) if svt else "lp=%d" % _threads(params),
        ]
        if stamp:
            args += _sdr_ffmpeg_colour_args(stamp)
        elif hdr:
            args += _ffmpeg_colour_args(*hdr)

    elif decision.encoder == AV1_QSV:
        args += [
            "-c:v", "av1_qsv",
            "-preset", str(params.get("qsv_preset") or QSV_PRESET),
            "-global_quality", str(crf if crf is not None else params.get("qsv_global_quality", QSV_GLOBAL_QUALITY)),
        ]
        if stamp:
            args += _sdr_ffmpeg_colour_args(stamp)
        elif hdr:
            args += _ffmpeg_colour_args(*hdr)

    else:
        raise ValueError("unknown encoder %r" % decision.encoder)

    args += _tail_args()
    args += ["-f", "matroska", str(dst)]
    log.debug("encode argv: %s", " ".join(str(a) for a in args))
    return args
