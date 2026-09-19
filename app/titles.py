import logging
import os
import re
import unicodedata

log = logging.getLogger("titles")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

UNSAFE = '/\\:*?"<>|'
CONTROL = "".join(chr(c) for c in range(0x00, 0x20))

RESERVED = {"CON", "PRN", "AUX", "NUL"}
RESERVED |= {"COM%d" % n for n in range(1, 10)}
RESERVED |= {"LPT%d" % n for n in range(1, 10)}

EM_DASH = "—"
EN_DASH = "–"

_WS = re.compile(r"\s+")


class TitleError(ValueError):
    pass


def _collapse(s):
    return _WS.sub(" ", s).strip()


TMDBID_IN_NAME = re.compile(r"\[tmdbid-(\d+)\]", re.I)
IMDBID_IN_NAME = re.compile(r"\[imdbid-(tt\d+)\]", re.I)
TVDBID_IN_NAME = re.compile(r"\[tvdbid-(\d+)\]", re.I)
YEAR_IN_PARENS = re.compile(r"\((19\d{2}|20\d{2})\)")


#----- Release vocabulary
HAND_TOKENS = (
    r"1080p|720p|2160p|4k|bluray|blu-ray|bdrip|brrip|webrip|web-?dl|hdtv|remux|"
    r"x26[45]|h\.?26[45]|hevc|avc|xvid|divx|aac|ac3|dts(?:-hd)?|truehd|atmos|"
    r"ma|5\.1|7\.1|2\.0|10bit|8bit|hdr10?|dovi|dv|proper|repack|imax|multi|dual|complete|"
    r"\d{3,4}x\d{3,4}"
)

#----- FileBot's WEB-DL row uses a variable-width look-behind that re rejects;  this is the fixed-width form.
WEB_DL_FIXED = (
    r"(?:(?:ABC|ATV|ATVP|AMC|AMZN|BBC|CBS|CC|CORE|CR|CRAV|CW|DCU|DSCP|DSNP|DSNY|Disney[+]|"
    r"DisneyPlus|FBWatch|FREE|FOX|GLBO|HBO|MAX|HMAX|HULU|iP|iT|LIFE|MA|MTV|NBC|NICK|NF|Netflix|"
    r"RED|TF1|STZ|STAN|PCOK|PLAY|PMTP|VICE|MY5|PPLUS|VIKI|AO|MUBI|SBT|NOW|BCORE|AUBC|SBS|7PLUS|"
    r"9NOW|TEN|TVNZ|3NOW|ABMA|APPS)[ .-])?(?:WEB.?DL|WEB.?DLRip|WEB.?Cap|WEB.?Rip|HC|HD.?Rip|VODR|"
    r"VODRip|PPV|PPVRip|iTunesHD|ithd|azuhd|AmazonHD|NetflixHD|NetflixUHD|"
    r"(?:(?<=\d{3}[p].)|(?<=\d{4}[p].))WEB|WEB(?=.[hx]\d{3}))"
)


def _load_media_sources():
    patterns = []
    path = os.path.join(DATA_DIR, "media-sources.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            rows = [line.rstrip("\n") for line in fh if line.strip()]
    except OSError as exc:
        log.warning("media sources not loaded from %s: %s", path, exc)
        return patterns
    for row in rows:
        label, _, pattern = row.partition("\t")
        if label == "WEB-DL":
            pattern = WEB_DL_FIXED
        try:
            re.compile(pattern)
        except re.error as exc:
            log.warning("media source %s skipped, pattern does not compile: %s", label, exc)
            continue
        patterns.append("(?:%s)" % pattern)
    return patterns


def _load_release_groups():
    names = set()
    patterns = []
    path = os.path.join(DATA_DIR, "release-groups.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [line.strip() for line in fh if line.strip()]
    except OSError as exc:
        log.warning("release groups not loaded from %s: %s", path, exc)
        return names, patterns
    for line in lines:
        if re.search(r"[()\[\]|?*+\\^$]", line):
            try:
                patterns.append(re.compile(r"^(?:%s)$" % line))
            except re.error as exc:
                log.warning("release group pattern skipped, does not compile: %r: %s", line, exc)
            continue
        names.add(line.lower())
    return names, patterns


MEDIA_SOURCES = _load_media_sources()
RELEASE_GROUPS, RELEASE_GROUP_PATTERNS = _load_release_groups()
RELEASE_TOKENS = re.compile(
    r"\b(?:%s)\b" % "|".join([HAND_TOKENS] + MEDIA_SOURCES), re.I
)

#----- Edition vocabulary;  an underscore in a pattern stands for any run of separators.
EDITIONS = (
    (r"director'?s_definitive_cut", "Director's Definitive Cut"),
    (r"director'?s_cut", "Director's Cut"),
    (r"final_cut", "Final Cut"),
    (r"assembly_cut", "Assembly Cut"),
    (r"alternat(?:e|ive)_cut", "Alternative Cut"),
    (r"extended(?:_(?:cut|edition|version))?", "Extended"),
    (r"theatrical(?:_(?:cut|edition|version))?", "Theatrical"),
    (r"unrated(?:_(?:cut|edition|version))?", "Unrated"),
    (r"uncut(?:_(?:edition|version))?", "Uncut"),
    (r"uncensored(?:_(?:edition|version))?", "Uncensored"),
    (r"special_edition", "Special Edition"),
    (r"fan_edit", "Fan Edit"),
    (r"festival(?:_(?:cut|edition|version))?", "Festival"),
)
#----- Transfer and packaging words:  stripped from a search name, never a label.
EDITION_NOISE = (
    (r"remastered(?:_(?:edition|version))?", "Remastered"),
    (r"restored(?:_(?:edition|version))?", "Restored"),
    (r"criterion(?:_(?:collection|edition))?", "Criterion"),
    (r"imax(?:_(?:edition|version))?", "IMAX"),
    (r"(?:ultimate_)?collector'?s?(?:_edition)?", "Collector"),
    (r"limited(?:_edition)?", "Limited"),
    (r"deluxe(?:_edition)?", "Deluxe"),
    (r"ultimate(?:_edition)?", "Ultimate"),
)
_SEP = r"[\s._-]+"


def _compile_editions(table):
    return tuple(
        (re.compile(r"(?:^|[\s._\-(\[])(%s)(?=$|[\s._\-)\]])" % pattern.replace("_", _SEP), re.I), label)
        for pattern, label in table
    )


_EDITION_PATTERNS = _compile_editions(EDITIONS)
_NOISE_PATTERNS = _compile_editions(EDITION_NOISE)
#----- the trailing delimiter is a lookahead so two adjacent years both match.
_LAST_YEAR = re.compile(r"(?:^|[.\s(\[_-])(?:19|20)\d{2}(?=[)\].\s_-]|$)")
EPISODE_MARK = re.compile(r"(?:^|[^a-z0-9])s\d{1,2}[\s._-]*e\d{1,3}", re.I)


#----- Release names
def is_release_group(token):
    token = str(token).strip()
    if not token:
        return False
    if token.lower() in RELEASE_GROUPS:
        return True
    return any(p.match(token) for p in RELEASE_GROUP_PATTERNS)


def strip_release_group(stem):
    text = str(stem).strip()
    #----- a group name is removed only in group position, after the last hyphen;  'War' mid-title stays.
    m = re.search(r"-([A-Za-z0-9_.]+)$", text)
    if m and is_release_group(m.group(1)) and _release_signal(text[: m.start()]):
        text = text[: m.start()].rstrip(" ._-")
    return text


def _release_signal(head):
    return bool(RELEASE_TOKENS.search(head) or _LAST_YEAR.search(head) or EPISODE_MARK.search(head))


def strip_release_tag(text):
    text = str(text).strip()
    while True:
        m = re.search(r"[\s._-]*[(\[]([^()\[\]]*)[)\]]\s*$", text)
        if not m:
            return text
        inside = m.group(1)
        words = [w for w in re.split(r"[\s._-]+", inside) if w]
        if not (RELEASE_TOKENS.search(inside) or any(is_release_group(w) for w in words)):
            return text
        text = text[: m.start()].rstrip(" ._-")


def _edition_match(name, patterns):
    text = os.path.splitext(str(name))[0] if re.search(r"\.[A-Za-z0-9]{2,4}$", str(name)) else str(name)
    years = list(_LAST_YEAR.finditer(text))
    #----- after the year an edition may sit anywhere;  without one it must end the stem, so a title word is never taken.
    if years:
        region = text[years[-1].end():]
        for pattern, label in patterns:
            m = pattern.search(region)
            if m:
                return label, m.group(1)
        return None, None
    tail = strip_release_tag(text)
    for pattern, label in patterns:
        m = pattern.search(tail)
        if m and not tail[m.end():].strip(" ._-)]"):
            return label, m.group(1)
    return None, None


def edition_from_name(name):
    return _edition_match(name, _EDITION_PATTERNS)


def strip_edition(name):
    text = str(name)
    for patterns in (_EDITION_PATTERNS, _NOISE_PATTERNS):
        _label, matched = _edition_match(text, patterns)
        if matched:
            text = re.sub(r"[\s._\-(\[]*%s[)\]]?" % re.escape(matched), " ", text, count=1, flags=re.I)
    return text


#----- Reading ids back out of a name
def ids_from_name(name):
    s = str(name)
    tmdb = TMDBID_IN_NAME.search(s)
    imdb = IMDBID_IN_NAME.search(s)
    tvdb = TVDBID_IN_NAME.search(s)
    year = YEAR_IN_PARENS.search(s)
    found = {
        "tmdb": tmdb.group(1) if tmdb else None,
        "imdb": imdb.group(1) if imdb else None,
        "tvdb": tvdb.group(1) if tvdb else None,
        "year": int(year.group(1)) if year else None,
    }
    if any(found.values()):
        log.debug("ids parsed from %r: %s", s, found)
    return found


def title_before_ids(name):
    s = str(name)
    s = TMDBID_IN_NAME.sub(" ", s)
    s = IMDBID_IN_NAME.sub(" ", s)
    s = TVDBID_IN_NAME.sub(" ", s)
    s = YEAR_IN_PARENS.sub(" ", s)
    return _collapse(s)


#----- The filename transform
def to_filename(title):
    s = str(title)
    s = s.replace(EM_DASH, " - ").replace(EN_DASH, " - ")
    s = s.replace("/", "-")
    s = s.replace(":", "")
    s = "".join(ch for ch in s if ch not in CONTROL and ch not in UNSAFE)
    out = _collapse(s)
    if out != str(title):
        log.debug("filename transform: %r -> %r", str(title), out)
    return out


def matches(tag_title, name):
    return to_filename(tag_title) == _collapse(str(name))


def is_reserved(name):
    stem = str(name).split(".")[0].strip().upper()
    return stem in RESERVED


#----- Validation
def validate_component(name):
    problems = []
    s = str(name)
    if any(ch in CONTROL for ch in s):
        problems.append("control-character")
    for ch in UNSAFE:
        if ch in s:
            problems.append("unsafe-%s" % ch)
    if s != s.rstrip(". "):
        problems.append("trailing-period-or-space")
    if is_reserved(s):
        problems.append("reserved-device-name")
    return problems


def assert_component(name):
    problems = validate_component(name)
    if problems:
        raise TitleError("%r is not a safe path component: %s" % (name, ", ".join(problems)))
    return name


def normalise_for_match(s):
    s = unicodedata.normalize("NFKC", str(s))
    s = s.replace(EM_DASH, " - ").replace(EN_DASH, " - ")
    s = s.replace("&", " and ")
    s = re.sub(r"[^0-9A-Za-z ]+", " ", s)
    return _collapse(s).lower()


#----- Naming
def _require(**fields):
    missing = [name for name, value in fields.items() if value is None or value == ""]
    if missing:
        raise TitleError("cannot build a name without %s" % ", ".join(missing))


def movie_folder(title, year, tmdb, imdb):
    _require(title=title, year=year, tmdb=tmdb, imdb=imdb)
    imdb = str(imdb)
    if not imdb.startswith("tt"):
        imdb = "tt%s" % imdb
    return assert_component(
        "%s (%s) [tmdbid-%s] [imdbid-%s]" % (to_filename(title), year, tmdb, imdb)
    )


def movie_filename(title, year, edition=None, folder=None):
    if edition:
        base = folder or ""
        if not base:
            raise TitleError("an edition filename requires the full folder name")
        return assert_component("%s - %s.mkv" % (base, to_filename(edition)))
    _require(title=title, year=year)
    return assert_component("%s (%s).mkv" % (to_filename(title), year))


def show_folder(show, year, tvdb, tmdb):
    _require(show=show, year=year, tvdb=tvdb, tmdb=tmdb)
    return assert_component(
        "%s (%s) [tvdbid-%s] [tmdbid-%s]" % (to_filename(show), year, tvdb, tmdb)
    )


def season_folder(season):
    return "Season %02d" % int(season)


def episode_code(season, first, last=None):
    season = int(season)
    first = int(first)
    if last is None or int(last) == first:
        return "S%02dE%02d" % (season, first)
    return "S%02dE%02d-E%02d" % (season, first, int(last))


def episode_filename(show, season, first, episode_title, last=None):
    _require(show=show, season=season, first=first, episode_title=episode_title)
    return assert_component(
        "%s - %s - %s.mkv"
        % (to_filename(show), episode_code(season, first, last), to_filename(episode_title))
    )
