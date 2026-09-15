import os


class ConfigError(RuntimeError):
    pass


#----- Config is built before logging exists, so sources are recorded rather than logged.
_SOURCES = {}


#----- Environment readers
def _source(name, from_env):
    _SOURCES[name] = "environment" if from_env else "default"


def _str(name, default=None, required=False):
    v = os.environ.get(name)
    _source(name, bool(v))
    if v is None or v == "":
        if required:
            raise ConfigError("%s is required and is not set" % name)
        return default
    return v


def _int(name, default):
    v = os.environ.get(name)
    _source(name, bool(v))
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        raise ConfigError("%s must be an integer, got %r" % (name, v))


def _bool(name, default=False):
    v = os.environ.get(name)
    _source(name, bool(v))
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


VALID_CODECS = ("hevc", "av1")

CPU_MAX_PATH = "/sys/fs/cgroup/cpu.max"

#----- Settings that left the environment;  present in the env they are listed as ignored, never read.
REMOVED = (
    "OUTPUT_CODEC", "CRF", "TV_ENCODE_SD", "MAX_JOBS", "GPU_SLOTS", "CPU_SLOTS",
    "ENCODE_HEADROOM", "ENCODE_THREADS", "POLL_INTERVAL", "MTIME_QUIET", "GRAIN_THRESHOLD",
)


#----- cgroup CPU quota
def parse_cpu_max(text):
    if not text:
        return None
    parts = text.split()
    if len(parts) < 2 or parts[0] == "max":
        return None
    try:
        quota, period = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, quota // period)


def detect_cpus(path=CPU_MAX_PATH):
    try:
        with open(path, encoding="utf-8") as fh:
            return parse_cpu_max(fh.read())
    except OSError:
        return None


#----- The configuration surface
class Config:
    def __init__(self, env=None):
        if env is not None:
            saved = dict(os.environ)
            os.environ.clear()
            os.environ.update(env)
            try:
                self._load()
            finally:
                os.environ.clear()
                os.environ.update(saved)
        else:
            self._load()

    def _load(self):
        _SOURCES.clear()
        self.media_root = _str("MEDIA_ROOT", "/media")
        encode_override = _str("MEDIA_ENCODE")
        config_override = _str("MEDIA_CONFIG")
        self.media_encode = encode_override or os.path.join(self.media_root, "encode")
        self.media_config = config_override or os.path.join(self.media_root, "config")
        self.encode_mounted = bool(encode_override)
        self.config_mounted = bool(config_override)
        self.library_movies = _str("LIBRARY_MOVIES")
        self.library_tv = _str("LIBRARY_TV")

        self.cert_dir = _str("CERT_DIR", "/certs")
        self.tls_cert_file = _str("TLS_CERT_FILE", "fullchain.pem")
        self.tls_key_file = _str("TLS_KEY_FILE", "privkey.pem")

        self.puid = _int("PUID", -1)
        self.pgid = _int("PGID", -1)
        self.render_gid = _int("RENDER_GID", -1)

        self.web_port = _int("WEB_PORT", 443)
        self.dry_run = _bool("DRY_RUN", False)
        self.log_level = _str("LOG_LEVEL", "info").lower()
        self.render_node = _str("RENDER_NODE", "/dev/dri/renderD128")
        self.lock_wait_timeout = _int("LOCK_WAIT_TIMEOUT", 0)
        self.lock_wait_interval = _int("LOCK_WAIT_INTERVAL", 15)
        self.audit_interval = _int("AUDIT_INTERVAL", 2)
        self.audit_sweep_interval = _int("AUDIT_SWEEP_INTERVAL", 3600)
        self.ignored = [name for name in REMOVED if os.environ.get(name)]

        for name, value in (
            ("AUDIT_INTERVAL", self.audit_interval),
            ("AUDIT_SWEEP_INTERVAL", self.audit_sweep_interval),
        ):
            if value < 0:
                raise ConfigError("%s must not be negative, got %d" % (name, value))
        if self.lock_wait_interval < 1:
            raise ConfigError("LOCK_WAIT_INTERVAL must be at least 1 second")
        if self.lock_wait_timeout < 0:
            raise ConfigError("LOCK_WAIT_TIMEOUT must not be negative")
        required = [("MEDIA_ROOT", self.media_root)]
        if self.encode_mounted:
            required.append(("MEDIA_ENCODE", self.media_encode))
        if self.config_mounted:
            required.append(("MEDIA_CONFIG", self.media_config))
        for name, path in required:
            if not os.path.isdir(path):
                raise ConfigError("%s points at %s which is not a mounted directory" % (name, path))

        if not 1 <= self.web_port <= 65535:
            raise ConfigError("WEB_PORT must be a valid port, got %d" % self.web_port)

        self.sources = dict(_SOURCES)

    #----- Derived values
    @property
    def tls_cert(self):
        return os.path.join(self.cert_dir, self.tls_cert_file)

    @property
    def tls_key(self):
        return os.path.join(self.cert_dir, self.tls_key_file)

    @property
    def gpu_enabled(self):
        return self.render_gid >= 0

    @property
    def libraries_mounted(self):
        return bool(self.library_movies or self.library_tv)

    @property
    def audit_enabled(self):
        return self.audit_interval > 0 and self.libraries_mounted

    #----- Reporting
    def as_dict(self):
        return {
            "MEDIA_ROOT": self.media_root,
            "MEDIA_ENCODE": self.media_encode,
            "MEDIA_CONFIG": self.media_config,
            "LIBRARY_MOVIES": self.library_movies,
            "LIBRARY_TV": self.library_tv,
            "CERT_DIR": self.cert_dir,
            "TLS_CERT_FILE": self.tls_cert_file,
            "TLS_KEY_FILE": self.tls_key_file,
            "PUID": self.puid,
            "PGID": self.pgid,
            "RENDER_GID": self.render_gid,
            "RENDER_NODE": self.render_node,
            "WEB_PORT": self.web_port,
            "DRY_RUN": self.dry_run,
            "LOG_LEVEL": self.log_level,
            "LOCK_WAIT_TIMEOUT": self.lock_wait_timeout,
            "LOCK_WAIT_INTERVAL": self.lock_wait_interval,
            "AUDIT_INTERVAL": self.audit_interval,
            "AUDIT_SWEEP_INTERVAL": self.audit_sweep_interval,
        }

    def banner(self):
        rows = self.as_dict()
        width = max(len(k) for k in rows)
        return "\n".join(
            "%-*s  %s" % (width, k, "(unset)" if v is None else v) for k, v in rows.items()
        )
