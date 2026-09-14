import difflib
import logging
import os
import re

from . import titles

log = logging.getLogger("episodes")

FUZZY_CUTOFF = 0.82
MAX_RANGE_SPAN = 3

RANGE_PATTERNS = (
    re.compile(r"(?:^|[^a-z0-9])s(\d{1,2})[\s._-]*e(\d{1,3})[\s._-]*e(\d{1,3})", re.I),
    #----- a bare second number counts only when it sits against the separator;  " - 99" is a title.
    re.compile(r"(?:^|[^a-z0-9])s(\d{1,2})[\s._-]*e(\d{1,3})(?:\s*[-~+&]\s*e|[-~+&]|\s*(?:to|and)\s*e?)(\d{1,3})", re.I),
    re.compile(r"(?:^|[^a-z0-9])(\d{1,2})x(\d{1,3})\s*(?:[-~+&]|to|and)\s*(?:\d{1,2}x)?(\d{1,3})", re.I),
)

SINGLE_PATTERNS = (
    re.compile(r"(?:^|[^a-z0-9])s(\d{1,2})[\s._-]*e(\d{1,3})", re.I),
    re.compile(r"(?:^|[^a-z0-9])(\d{1,2})x(\d{1,3})", re.I),
    re.compile(r"(?:^|[^a-z0-9])season[\s._-]*(\d{1,2})[\s._-]*episode[\s._-]*(\d{1,3})", re.I),
)

#----- "3 of 6" and "Part 3 of 6" name an episode only under a season folder.
OF_PATTERN = re.compile(r"(?:^|[^a-z0-9])(?:part[\s._-]*)?(\d{1,3})[\s._-]*of[\s._-]*(\d{1,3})(?:[^0-9]|$)", re.I)
SEASON_FOLDER = re.compile(r"^(?:season|series|staffel|saison|temporada)[\s._-]*(\d{1,2})$", re.I)

PART_MARKERS = (
    re.compile(r"\s*\(\s*part\s+(one|two|three|four|five|six)\s*\)\s*$", re.I),
    re.compile(r"\s*\(\s*part\s+(\d+)\s*\)\s*$", re.I),
    re.compile(r"\s*\(\s*(\d+)\s*\)\s*$", re.I),
    #----- the bare forms, "Part 2" and ", Part Two", which release names carry without parentheses.
    re.compile(r"\s*[-,:]?\s*(?:part|pt)[\s._]+(one|two|three|four|five|six)\s*$", re.I),
    re.compile(r"\s*[-,:]?\s*(?:part|pt)[\s._]+(\d+)\s*$", re.I),
)

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
}

MOVIE_YEAR = re.compile(r"(?:^|[^0-9])(19\d{2}|20\d{2})(?:[^0-9]|$)")


#----- Parsing source names
def classify(path):
    name = os.path.basename(str(path))
    parent = os.path.basename(os.path.dirname(str(path)))
    for candidate in (name, parent):
        if parse_filename(candidate) is not None:
            return "tv"
    if parse_path(path) is not None:
        return "tv"
    return "movie"


def parse_filename(name, default_season=None):
    text = str(name)
    for pattern in RANGE_PATTERNS:
        m = pattern.search(text)
        if m:
            season, first, last = (int(g) for g in m.groups())
            if last >= first:
                #----- a range wider than a two-parter is a mislabel, not a file holding a season.
                if last - first + 1 > MAX_RANGE_SPAN:
                    log.warning(
                        "range E%02d-E%02d in %s spans %d episodes, more than %d, reading it as E%02d alone",
                        first, last, text, last - first + 1, MAX_RANGE_SPAN, first,
                    )
                    return {"season": season, "first": first, "last": first}
                return {"season": season, "first": first, "last": last}
    for pattern in SINGLE_PATTERNS:
        m = pattern.search(text)
        if m:
            season, first = (int(g) for g in m.groups())
            return {"season": season, "first": first, "last": first}
    if default_season is not None:
        m = re.search(r"(?:^|[^0-9])e(\d{1,3})(?:[^0-9]|$)", text, re.I)
        if m:
            episode = int(m.group(1))
            return {"season": default_season, "first": episode, "last": episode}
        m = OF_PATTERN.search(text)
        if m:
            episode = int(m.group(1))
            return {"season": default_season, "first": episode, "last": episode}
        m = re.match(r"\s*(\d{1,3})(?=[\s._-]|$)", text)
        if m:
            episode = int(m.group(1))
            return {"season": default_season, "first": episode, "last": episode}
    return None


def season_from_folder(path):
    parent = os.path.basename(os.path.dirname(str(path)))
    m = SEASON_FOLDER.match(parent.strip())
    return int(m.group(1)) if m else None


def parse_path(path):
    name = os.path.basename(str(path))
    return parse_filename(name, default_season=season_from_folder(path))


def parse_marker(title):
    text = str(title)
    for pattern in PART_MARKERS:
        m = pattern.search(text)
        if m:
            token = m.group(1).lower()
            number = WORD_NUMBERS.get(token)
            if number is None:
                try:
                    number = int(token)
                except ValueError:
                    continue
            return pattern.sub("", text).strip(), number
    return text, None


def to_part_suffix(title):
    base, number = parse_marker(title)
    if number is None:
        return str(title)
    return "%s, Part %d" % (base, number)


def title_from_filename(name):
    stem = os.path.splitext(os.path.basename(str(name)))[0]
    stem = titles.strip_release_group(stem)
    parts = stem.split(" - ")
    if len(parts) >= 3:
        return titles.strip_release_tag(" - ".join(parts[2:]).strip())
    end = _marker_end(stem)
    if end is not None:
        rest = re.sub(r"^[\s._-]+", "", stem[end:]).strip()
        rest = titles.strip_release_tag(rest)
        if rest:
            return rest
    return titles.strip_release_tag(stem)


def _marker_end(text):
    for pattern in RANGE_PATTERNS + SINGLE_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.end()
    return None


#----- Matching against the provider catalogue
def _index(catalogue):
    exact = {}
    base = {}
    parts = {}
    keys = []
    for entry in catalogue:
        key = titles.normalise_for_match(entry["title"])
        exact.setdefault(key, entry)
        keys.append(key)
        plain, number = parse_marker(entry["title"])
        stripped = titles.normalise_for_match(plain)
        current = base.get(stripped)
        if current is None or _order(entry) < _order(current):
            base[stripped] = entry
        if number is not None:
            parts.setdefault((stripped, number), entry)
    return exact, base, parts, keys


#----- anchored at the start on whole words, longest hit wins;  a tag cannot supply a hit and 'Babel' cannot claim 'Babel One'.
def _contained(key, base):
    words = key.split()
    hits = []
    for candidate in base:
        cwords = candidate.split()
        if cwords and words[: len(cwords)] == cwords:
            hits.append(candidate)
    if not hits:
        return None, 0.0
    best = max(hits, key=len)
    return best, len(best.split()) / float(len(words))


#----- ordering resolves an unmarked title to the first episode of a marked pair.
def _order(entry):
    return (entry["season"], entry["episode"])


def match_episode(name, catalogue, cutoff=FUZZY_CUTOFF, extra=None):
    entry, method, score = _match_episode(name, catalogue, cutoff)
    if entry is None and extra:
        entry, method, score = _match_episode(extra, catalogue, cutoff, probe=str(extra))
        if entry is not None:
            log.info("episode matched on the segment title %r rather than the file name", extra)
    if method == "fuzzy":
        log.info("episode matched by fuzzy title on %s at %.2f", os.path.basename(str(name)), score)
    elif method == "contains":
        log.info(
            "episode matched by contained title on %s, %r at %.2f",
            os.path.basename(str(name)), entry["title"], score,
        )
    elif method == "none":
        log.info("no title match for %s", os.path.basename(str(name)))
    else:
        log.debug("episode matched exactly on %s", os.path.basename(str(name)))
    return entry, method, score


#----- the four exact rungs:  whole title, own part, base as first of a pair, marker-stripped.
def _exact_rungs(text, exact, base, parts):
    key = titles.normalise_for_match(text)
    plain, number = parse_marker(text)
    stripped = titles.normalise_for_match(plain)
    if key in exact:
        return exact[key], key, stripped, number
    if number is not None and (stripped, number) in parts:
        return parts[(stripped, number)], key, stripped, number
    if key in base:
        return base[key], key, stripped, number
    if stripped in exact:
        return exact[stripped], key, stripped, number
    #----- a probe carrying a part number the catalogue lacks must not land on the first part.
    if number is None and stripped in base:
        return base[stripped], key, stripped, number
    return None, key, stripped, number


#----- rungs in order:  exact rungs on the raw probe, the same on the token-stripped probe, containment, then difflib.
def _match_episode(name, catalogue, cutoff=FUZZY_CUTOFF, probe=None):
    exact, base, parts, keys = _index(catalogue)
    probe_title = title_from_filename(name) if probe is None else titles.strip_release_tag(probe)

    entry, key, _stripped, _number = _exact_rungs(probe_title, exact, base, parts)
    if entry is not None:
        return entry, "exact", 1.0

    #----- release tokens out, then the marker is read again:  "Darkness.Rising.Part.3.1080p.BluRay" is part 3.
    cleaned_text = titles.RELEASE_TOKENS.sub(" ", probe_title).strip(" ._-")
    entry, cleaned_key, cleaned_base, number = _exact_rungs(cleaned_text, exact, base, parts)
    if entry is not None:
        return entry, "exact", 1.0

    hit, share = _contained(cleaned_base if number is not None else cleaned_key, base)
    if hit:
        if number is not None:
            if (hit, number) in parts:
                return parts[(hit, number)], "contains", share
            log.info("contained title %r has no part %d in the catalogue", hit, number)
        else:
            return base[hit], "contains", share

    #----- difflib would pair "part 4" with "part 1" at 0.95;  a marked probe with no part is numbering's to settle.
    if number is not None:
        log.info("no catalogue entry for part %d of %r, leaving it to numbering", number, cleaned_base)
        return None, "none", 0.0

    close = difflib.get_close_matches(cleaned_key, keys, n=3, cutoff=cutoff)
    log.debug("fuzzy candidates for %r: %s", cleaned_key, close)
    if close:
        return exact[close[0]], "fuzzy", difflib.SequenceMatcher(None, cleaned_key, close[0]).ratio()

    return None, "none", 0.0


#----- Assignment and ranges
def assign(files, catalogue):
    results = []
    claimed = set()

    for path in files:
        entry, how, score = match_episode(path, catalogue)
        if entry is not None:
            key = (entry["season"], entry["episode"])
            if key not in claimed:
                claimed.add(key)
                results.append(
                    {
                        "path": path,
                        "season": entry["season"],
                        "first": entry["episode"],
                        "last": entry["episode"],
                        "title": to_part_suffix(entry["title"]),
                        "method": how,
                        "score": score,
                    }
                )
                continue

        parsed = parse_filename(os.path.basename(str(path)))
        if parsed is None:
            results.append({"path": path, "method": "unmatched", "score": 0.0})
            continue
        results.append(
            {
                "path": path,
                "season": parsed["season"],
                "first": parsed["first"],
                "last": parsed["last"],
                "title": None,
                "method": "fallback-numbering",
                "score": 0.0,
            }
        )

    return results


def extend_ranges(assignments, catalogue):
    by_key = {(e["season"], e["episode"]): e for e in catalogue}
    claimed = {
        (a["season"], n)
        for a in assignments
        if a.get("season") is not None
        for n in range(a["first"], a.get("last", a["first"]) + 1)
    }
    for a in assignments:
        if a.get("season") is None or a["method"] == "unmatched":
            continue
        nxt = (a["season"], a["last"] + 1)
        if nxt in claimed or nxt not in by_key:
            continue
        this_base = parse_marker(by_key.get((a["season"], a["first"]), {}).get("title", ""))[0]
        next_base = parse_marker(by_key[nxt]["title"])[0]
        if this_base and this_base == next_base:
            a["last"] = nxt[1]
            a["title"] = this_base
            claimed.add(nxt)
    return assignments


def season_counts(catalogue):
    counts = {}
    for entry in catalogue:
        counts[entry["season"]] = counts.get(entry["season"], 0) + 1
    return counts


def disk_counts(assignments):
    counts = {}
    for a in assignments:
        season = a.get("season")
        if season is None:
            continue
        span = a.get("last", a.get("first")) - a.get("first") + 1
        counts[season] = counts.get(season, 0) + span
    return counts
