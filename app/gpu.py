import logging
import os
import subprocess

log = logging.getLogger("gpu")

RENDER_NODE = os.environ.get("RENDER_NODE", "/dev/dri/renderD128")
VAINFO = os.environ.get("VAINFO", "vainfo")

REQUIRED_PROFILE = "VAProfileAV1Profile0"
HEVC_PROFILE = "VAProfileHEVCMain10"
REQUIRED_ENTRYPOINT = "VAEntrypointEncSlice"


class GpuStatus:
    def __init__(self, available, reason, render_node=None, profiles=None, hevc_available=False):
        self.available = available
        self.reason = reason
        self.render_node = render_node
        self.profiles = profiles or []
        self.hevc_available = hevc_available

    @property
    def degraded(self):
        return not self.available

    def as_dict(self):
        return {
            "available": self.available,
            "hevc_available": self.hevc_available,
            "degraded": self.degraded,
            "reason": self.reason,
            "render_node": self.render_node,
            "av1_encode_profiles": [p for p in self.profiles if REQUIRED_PROFILE in p],
            "hevc_encode_profiles": [p for p in self.profiles if HEVC_PROFILE in p],
        }

    def __repr__(self):
        return "<GpuStatus available=%s hevc_available=%s reason=%r>" % (
            self.available, self.hevc_available, self.reason,
        )


#----- vainfo
def _vainfo(render_node, timeout=30):
    log.debug("running %s --display drm --device %s", VAINFO, render_node)
    return subprocess.run(
        [VAINFO, "--display", "drm", "--device", render_node],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def parse_vainfo(text):
    profiles = []
    for line in (text or "").splitlines():
        line = line.strip()
        #----- AV1 decides 'available';  the HEVC Main 10 line is recorded beside it for gates 6 and 7.
        if REQUIRED_ENTRYPOINT in line and (REQUIRED_PROFILE in line or HEVC_PROFILE in line):
            profiles.append(" ".join(line.split()))
    return profiles


#----- The probe
def probe(cfg=None, render_node=None):
    status = _probe(cfg, render_node)
    if status.available:
        log.info("gpu available on %s: %s", status.render_node, status.reason)
    else:
        log.warning("gpu degraded on %s: %s", status.render_node, status.reason)
    return status


def _probe(cfg=None, render_node=None):
    node = render_node or getattr(cfg, "render_node", None) or RENDER_NODE
    log.debug("probing render node %s", node)

    if cfg is not None and not cfg.gpu_enabled:
        return GpuStatus(False, "RENDER_GID is unset, GPU support is not configured", node)

    if not os.path.exists(node):
        return GpuStatus(False, "render node %s does not exist" % node, node)

    if not os.access(node, os.R_OK | os.W_OK):
        return GpuStatus(
            False,
            "render node %s is not readable and writable by this process, check RENDER_GID" % node,
            node,
        )

    try:
        proc = _vainfo(node)
    except FileNotFoundError:
        return GpuStatus(False, "vainfo is not installed in this image", node)
    except subprocess.TimeoutExpired:
        return GpuStatus(False, "vainfo timed out against %s" % node, node)

    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return GpuStatus(
            False,
            "vainfo exited %d: %s" % (proc.returncode, combined.strip()[-300:]),
            node,
        )

    profiles = parse_vainfo(combined)
    log.debug("vainfo returned %d line(s), %d matching profile(s)",
              len(combined.splitlines()), len(profiles))
    for line in profiles:
        log.debug("profile %s", line)
    av1 = any(REQUIRED_PROFILE in p for p in profiles)
    hevc = any(HEVC_PROFILE in p for p in profiles)
    if not av1:
        return GpuStatus(
            False,
            "%s with %s not reported by the driver, AV1 hardware encode is unavailable%s"
            % (REQUIRED_PROFILE, REQUIRED_ENTRYPOINT,
               "; HEVC hardware encode available" if hevc else ""),
            node, profiles, hevc_available=hevc,
        )

    return GpuStatus(
        True,
        "AV1 hardware encode available%s" % (", HEVC hardware encode available" if hevc else ""),
        node, profiles, hevc_available=hevc,
    )
