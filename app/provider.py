import difflib
import hashlib
import html
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import VERSION
from . import episodes as episodemod
from . import tags as tagsmod
from . import titles

log = logging.getLogger("provider")

USER_AGENT = "procrustes/%s (+https://github.com/Vhye76/procrustes)" % VERSION
THROTTLE_SECONDS = 3.0
TIMEOUT = 30
TITLE_CUTOFF = episodemod.FUZZY_CUTOFF
CONTAINED_SCORE = 0.9
TEXT_SEARCH_LIMIT = 10

P_TMDB = "P4947"
P_IMDB = "P345"
P_TVDB = "P4835"
P_TMDB_TV = "P4983"
P_RELEASE = "P577"
P_START = "P580"
ID_PROPERTIES = {
    "movie": (("tmdb", P_TMDB), ("imdb", P_IMDB)),
    "tv": (("tvdb", P_TVDB), ("tmdb", P_TMDB_TV)),
}
#----- A show's tmdb is required for its folder name but is not a condition of accepting the entity.
REQUIRED = {
    "movie": ("title", "year", "tmdb", "imdb"),
    "tv": ("show", "show_year", "tvdb", "tmdb"),
}
MANUAL_REQUIRED = {
    "movie": ("title", "year"),
    "tv": ("show", "show_year", "season", "episode", "episode_title"),
}
ABBREVIATION_DROPPED = "abbreviation dropped"
SUBTITLE_SPLIT = re.compile(r"\s*:\s*|\s+-\s+|\s*[–—]\s*")

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY = "https://www.wikidata.org/wiki/Special:EntityData/%s.json"
TMDB_MOVIE = "https://www.themoviedb.org/movie/%s"
TMDB_TV = "https://www.themoviedb.org/tv/%s"

TVDB_POSTER = re.compile(
    r"https://artworks\.thetvdb\.com/banners/posters/[^\"'\s>]+"
)

OG_IMAGE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.I,
)
TVDB_ROOT = "https://thetvdb.com/"
TVDB_DEREFERRER = "https://thetvdb.com/dereferrer/series/%s"
TVDB_SERIES = "https://thetvdb.com/series/%s"
TVDB_SEASONS = "https://thetvdb.com/series/%s/allseasons/%s"
TVDB_SPECIALS = "https://thetvdb.com/series/%s/seasons/%s/0"
TVDB_REMOTE_ID = "https://thetvdb.com/api/GetSeriesByRemoteID.php?imdbid=%s"
TMDB_SEARCH = "https://www.themoviedb.org/search/%s?query=%s"
IMDB_LINK = "imdb.com/title/%s"

PAGE_TITLE = re.compile(r"<title>\s*(.*?)\s*</title>", re.I | re.S)
#----- TMDB titles pages 'Name (2015)' for a film and 'Name (TV Series 1984)' for a show;  TVDB 'Name (2003)' or bare.
PAGE_YEAR = re.compile(r"\s*\((?:TV Series\s+)?(\d{4})\)\s*$")
#----- TMDB's separator arrives as '&#8212;', an em dash once unescaped;  TVDB's is a hyphen.
SITE_SUFFIX = re.compile(r"\s+(?:\u2014|-)\s+(?:The Movie Database \(TMDB\)|TheTVDB\.com)\s*$")
TVDB_ENG_TITLE = re.compile(
    r'class="change_translation_text"[^>]*data-language="eng"[^>]*data-title="([^"]*)"',
    re.I,
)
TVDB_ORIGINAL_LANGUAGE = re.compile(
    r"<strong>\s*Original Language\s*</strong>\s*<span>\s*([^<]*)", re.I
)
TMDB_SEARCH_HIT = re.compile(
    r'data-media-type="(?:tv|movie)"[^>]*href="/(?:tv|movie)/(\d+)[^"]*"[^>]*>\s*'
    r"<h2[^>]*>\s*(?:<span>)?\s*(.*?)\s*(?:</span>)?\s*</h2>\s*</a>"
    r'(?:\s*<span class="release_date[^"]*">\s*([^<]*))?',
    re.I | re.S,
)

EPISODE_LABEL = re.compile(
    r'episode-label">S(\d{1,2})E(\d{1,3})</span>.*?<a[^>]*href="([^"]*)"[^>]*>\s*(.*?)\s*</a>',
    re.I | re.S,
)
SPECIAL_ROW = re.compile(
    r'<td>\s*S(\d{1,2})E(\d{1,3})\s*</td>\s*<td>\s*<a[^>]*href="([^"]*)"[^>]*>\s*(.*?)\s*</a>',
    re.I | re.S,
)


class ProviderError(RuntimeError):
    pass


class RateLimited(ProviderError):
    pass


#----- The HTTP client, throttled and cached
class Client:
    def __init__(self, cache_dir, throttle=THROTTLE_SECONDS, settings=None):
        self.cache_dir = str(cache_dir)
        self.throttle = throttle
        self.settings = settings
        self._last = 0.0
        self._local = threading.local()
        os.makedirs(self.cache_dir, exist_ok=True)

    #----- Thread-local, so a bypass on one assessment worker leaves the others on the cache.
    class _Fresh:
        def __init__(self, client):
            self.client = client

        def __enter__(self):
            self.client._local.fresh = True
            return self

        def __exit__(self, *exc):
            self.client._local.fresh = False
            return False

    def fresh(self):
        return Client._Fresh(self)

    def _bypassing(self):
        return bool(getattr(self._local, "fresh", False))

    def _cache_path(self, url):
        return os.path.join(self.cache_dir, hashlib.sha256(url.encode()).hexdigest() + ".json")

    def _cache_get(self, url):
        path = self._cache_path(url)
        if not os.path.isfile(path):
            return None
        try:
            with open(path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _cache_put(self, url, status, body):
        try:
            with open(self._cache_path(url), "w") as fh:
                json.dump({"url": url, "status": status, "body": body}, fh)
        except OSError:
            pass

    #----- Figures come from the settings object per request;  the constructor default serves a direct call.
    def _figure(self, key, default):
        if self.settings is None:
            return default
        return self.settings.get(key)

    def _wait(self):
        throttle = float(self._figure("provider_throttle_s", self.throttle))
        elapsed = time.time() - self._last
        if elapsed < throttle:
            log.debug("throttling %.1fs before the next request", throttle - elapsed)
            time.sleep(throttle - elapsed)
        self._last = time.time()

    def _timeout(self):
        return float(self._figure("provider_timeout_s", TIMEOUT))

    def fetch(self, url, use_cache=True):
        if use_cache and not self._bypassing():
            cached = self._cache_get(url)
            if cached is not None:
                log.debug("cache hit for %s", url)
                return cached["status"], cached["body"]

        self._wait()
        log.debug("GET %s", url)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout()) as response:
                status = response.getcode()
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read().decode("utf-8", "replace") if exc.fp else ""
            if status == 429:
                raise RateLimited("rate limited by %s" % url)
            if status >= 500:
                raise ProviderError("%s returned HTTP %d" % (url, status))
        except urllib.error.URLError as exc:
            raise ProviderError("could not reach %s: %s" % (url, exc))

        log.debug("HTTP %s from %s", status, url)
        if status == 200:
            self._cache_put(url, status, body)
        return status, body

    def fetch_json(self, url):
        status, body = self.fetch(url)
        if status != 200:
            raise ProviderError("%s returned HTTP %d" % (url, status))
        try:
            return json.loads(body)
        except ValueError as exc:
            raise ProviderError("%s did not return JSON: %s" % (url, exc))

    def final_url(self, url):
        self._wait()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=self._timeout()) as response:
            return response.geturl()


#----- Wikidata claim readers
def _claims(entity, prop):
    values = []
    for claim in (entity.get("claims") or {}).get(prop, []):
        snak = (claim.get("mainsnak") or {}).get("datavalue") or {}
        value = snak.get("value")
        if isinstance(value, dict):
            value = value.get("time") or value.get("id")
        if value is not None:
            values.append(value)
    return values


RANK_ORDER = {"preferred": 2, "normal": 1}


def _dated_claims(entity, prop):
    rows = []
    for claim in (entity.get("claims") or {}).get(prop, []):
        if claim.get("rank") == "deprecated":
            continue
        value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if not isinstance(value, dict) or not value.get("time"):
            continue
        rows.append(
            {
                "time": value["time"],
                "precision": int(value.get("precision") or 0),
                "rank": RANK_ORDER.get(claim.get("rank"), 1),
            }
        )
    return rows


def _best_date(entity, prop):
    rows = _dated_claims(entity, prop)
    if not rows:
        return None
    top_rank = max(r["rank"] for r in rows)
    rows = [r for r in rows if r["rank"] == top_rank]
    top_precision = max(r["precision"] for r in rows)
    candidates = [r for r in rows if r["precision"] == top_precision]
    chosen = sorted(candidates, key=lambda r: r["time"])[0]
    log.debug(
        "%s: chose %s from %d claim(s) at rank %d precision %d",
        prop, chosen["time"], len(_dated_claims(entity, prop)),
        chosen["rank"], chosen["precision"],
    )
    return chosen["time"]


def _year(value):
    m = re.search(r"(\d{4})", str(value or ""))
    return int(m.group(1)) if m else None


#----- Resolution
class Provider:
    def __init__(self, client, roots=(), settings=None):
        self.client = client
        self.roots = {os.path.normpath(str(root)) for root in roots if root}
        self.settings = settings
        self._tvdb_posters = {}
        self._local = threading.local()

    #----- Matching figures, read per call so a settings change reaches the next identification
    def _figure(self, key, default):
        if self.settings is None:
            return default
        return self.settings.get(key)

    def title_cutoff(self):
        return float(self._figure("title_cutoff", TITLE_CUTOFF))

    def contained_score(self):
        return float(self._figure("contained_score", CONTAINED_SCORE))

    def search_entities(self, term, limit=20):
        url = "%s?%s" % (
            WIKIDATA_API,
            urllib.parse.urlencode(
                {
                    "action": "wbsearchentities",
                    "search": term,
                    "language": "en",
                    "format": "json",
                    "limit": limit,
                }
            ),
        )
        return self.client.fetch_json(url).get("search") or []

    def search_text(self, term, limit=TEXT_SEARCH_LIMIT):
        url = "%s?%s" % (
            WIKIDATA_API,
            urllib.parse.urlencode(
                {
                    "action": "query",
                    "list": "search",
                    "srsearch": term,
                    "srlimit": limit,
                    "format": "json",
                }
            ),
        )
        hits = (self.client.fetch_json(url).get("query") or {}).get("search") or []
        return [h["title"] for h in hits if str(h.get("title", "")).startswith("Q")]

    def labels(self, qids):
        if not qids:
            return []
        url = "%s?%s" % (
            WIKIDATA_API,
            urllib.parse.urlencode(
                {
                    "action": "wbgetentities",
                    "ids": "|".join(qids),
                    "props": "labels|aliases",
                    "languages": "en",
                    "format": "json",
                }
            ),
        )
        entities = self.client.fetch_json(url).get("entities") or {}
        out = []
        for qid in qids:
            entity = entities.get(qid) or {}
            label = ((entity.get("labels") or {}).get("en") or {}).get("value")
            aliases = [a.get("value") for a in (entity.get("aliases") or {}).get("en") or []]
            out.append({"id": qid, "label": label, "aliases": [a for a in aliases if a]})
        return out

    def entity(self, qid):
        data = self.client.fetch_json(WIKIDATA_ENTITY % qid)
        return (data.get("entities") or {}).get(qid) or {}

    #----- P4983 is TMDB's series id, not a TVDB id;  TVDB comes from P4835 alone.
    def ids_from_entity(self, entity, kind="movie"):
        tmdb = _claims(entity, P_TMDB if kind == "movie" else P_TMDB_TV)
        imdb = _claims(entity, P_IMDB)
        tvdb = _claims(entity, P_TVDB)
        released = _best_date(entity, P_RELEASE) or _best_date(entity, P_START)
        return {
            "tmdb": tmdb[0] if tmdb else None,
            "imdb": imdb[0] if imdb else None,
            "tvdb": tvdb[0] if tvdb else None,
            "year": _year(released) if released else None,
        }

    def tmdb_page(self, kind, tmdb_id):
        url = (TMDB_MOVIE if kind == "movie" else TMDB_TV) % tmdb_id
        status, body = self.client.fetch(url)
        return body if status == 200 else None

    def verify_tmdb(self, kind, tmdb_id, names, year=None, own_claim=False):
        body = self.tmdb_page(kind, tmdb_id)
        if body is None:
            return False, None
        return _page_confirms(
            body, names, year, "tmdb %s" % tmdb_id,
            earlier_ok=bool(own_claim and kind == "tv"),
            cutoff=self.title_cutoff(), contained=self.contained_score(),
        )

    def tvdb_page(self, tvdb_id):
        try:
            status, body = self.client.fetch(TVDB_SERIES % self.series_slug(tvdb_id))
        except urllib.error.HTTPError as exc:
            log.info("tvdb %s does not dereference: HTTP %s", tvdb_id, exc.code)
            return None
        except urllib.error.URLError as exc:
            raise ProviderError("could not reach thetvdb for %s: %s" % (tvdb_id, exc))
        except ProviderError as exc:
            log.info("tvdb %s does not lead to a series page: %s", tvdb_id, exc)
            return None
        return body if status == 200 else None

    def verify_tvdb(self, tvdb_id, names, year=None):
        body = self.tvdb_page(tvdb_id)
        if body is None:
            return False, None
        return _page_confirms(
            body, names, year, "tvdb %s" % tvdb_id,
            cutoff=self.title_cutoff(), contained=self.contained_score(),
        )

    def tvdb_by_imdb(self, imdb_id):
        status, body = self.client.fetch(TVDB_REMOTE_ID % imdb_id)
        if status != 200 or not body.strip():
            return None
        try:
            root = ET.fromstring(body.strip())
        except ET.ParseError as exc:
            log.debug("tvdb remote-id lookup for %s did not parse: %s", imdb_id, exc)
            return None
        series = root.find("Series")
        if series is None:
            return None
        tvdb = (series.findtext("seriesid") or "").strip()
        if not tvdb:
            return None
        return {
            "tvdb": tvdb,
            "name": (series.findtext("SeriesName") or "").strip() or None,
            "year": _year(series.findtext("FirstAired")),
        }

    def tvdb_from_imdb(self, imdb_id, names, year=None):
        found = self.tvdb_by_imdb(imdb_id)
        if not found:
            log.info("tvdb has no series for imdb %s", imdb_id)
            return None
        body = self.tvdb_page(found["tvdb"])
        if body is None:
            return None
        if (IMDB_LINK % imdb_id) not in body:
            log.info("tvdb %s from imdb %s does not link that id back, skipping", found["tvdb"], imdb_id)
            return None
        ok, _note = _page_confirms(
            body, names, year, "tvdb %s" % found["tvdb"],
            cutoff=self.title_cutoff(), contained=self.contained_score(),
        )
        if not ok:
            log.info("tvdb %s from imdb %s did not confirm %r, skipping", found["tvdb"], imdb_id, names[0] if names else None)
            return None
        return found["tvdb"]

    def tmdb_poster(self, kind, tmdb_id):
        if not tmdb_id:
            return None
        url = (TMDB_MOVIE if kind == "movie" else TMDB_TV) % tmdb_id
        try:
            status, body = self.client.fetch(url)
        except ProviderError as exc:
            log.debug("poster lookup failed for %s: %s", url, exc)
            return None
        if status != 200:
            return None
        match = OG_IMAGE.search(body)
        if not match:
            log.debug("no og:image on %s", url)
            return None
        return match.group(1)

    def tvdb_poster(self, tvdb_id):
        if not tvdb_id:
            return None
        key = str(tvdb_id)
        if key in self._tvdb_posters:
            return self._tvdb_posters[key]
        url = None
        try:
            status, body = self.client.fetch(TVDB_SERIES % self.series_slug(key))
            if status == 200:
                match = TVDB_POSTER.search(body)
                url = match.group(0) if match else None
                if url is None:
                    log.debug("no poster artwork on the tvdb page for %s", key)
        except Exception as exc:
            log.debug("tvdb poster lookup failed for %s: %s", key, exc)
        self._tvdb_posters[key] = url
        return url

    #----- The movie identity ladder
    def movie_candidates(self, source, container, origin=None):
        stem = os.path.splitext(os.path.basename(source))[0]
        parent = os.path.basename(os.path.dirname(source))
        origin_parent = os.path.basename(os.path.dirname(origin)) if origin else ""
        rungs = []

        embedded = tagsmod.movie_identity(source)
        if embedded:
            rungs.append((
                "embedded tag",
                {"tmdb": embedded.get("tmdb"), "imdb": embedded.get("imdb")},
                _single(embedded.get("title"), embedded.get("year")),
            ))

        for label, name in (("filename ids", stem), ("folder ids", parent),
                            ("origin folder ids", origin_parent)):
            if not name:
                continue
            ids = titles.ids_from_name(name)
            if ids["tmdb"] or ids["imdb"]:
                rungs.append((label, ids, _single(titles.title_before_ids(name), ids["year"])))

        segment = (container or {}).get("segment_title")
        if segment:
            rungs.append(("segment title", {}, _single(segment, None)))

        readings = _clean_movie_name(stem)
        if readings:
            rungs.append(("filename", {}, readings))

        if self._in_root(source):
            log.debug("file sits directly in a pipeline root, parent folder rung skipped")
        else:
            searched = {name.lower() for name, _year, _label in readings}
            parent_readings = [
                r for r in _clean_movie_name(parent) if r[0].lower() not in searched
            ]
            if parent_readings:
                rungs.append(("parent folder", {}, parent_readings))

        log.debug("identity ladder: %s", [r[0] for r in rungs])
        return rungs

    def identify_movie(self, source, container, origin=None, pinned=None):
        resolved = self._identify_from(self.movie_candidates(source, container, origin), "movie", pinned)
        if resolved is not None and resolved.get("manual"):
            resolved["edition"] = None
        elif resolved is not None:
            resolved["edition"] = _edition_of(source, origin)
            if resolved["edition"]:
                log.info("edition %r read from the arrival name", resolved["edition"])
        return resolved

    #----- The show identity ladder
    def show_candidates(self, source, origin=None):
        stem = os.path.splitext(os.path.basename(source))[0]
        parent = os.path.basename(os.path.dirname(source))
        grandparent = os.path.basename(os.path.dirname(os.path.dirname(source)))
        rungs = []

        embedded = tagsmod.show_identity(source)
        if embedded:
            rungs.append((
                "embedded tag",
                {"tvdb": embedded.get("tvdb"), "tmdb": embedded.get("tmdb")},
                _single(embedded.get("title"), None),
            ))

        folders = [("folder ids", parent), ("folder ids", grandparent)]
        if origin:
            origin_dir = os.path.dirname(origin)
            folders.append(("origin folder ids", os.path.basename(origin_dir)))
            folders.append(("origin folder ids", os.path.basename(os.path.dirname(origin_dir))))
        for label, name in folders:
            if not name:
                continue
            ids = titles.ids_from_name(name)
            if ids["tvdb"] or ids["tmdb"]:
                rungs.append((label, ids, _single(titles.title_before_ids(name), ids["year"])))

        readings = _clean_show_name(stem, "" if self._in_root(source) else parent)
        if readings:
            rungs.append(("filename", {}, readings))

        log.debug("show identity ladder: %s", [r[0] for r in rungs])
        return rungs

    def identify_show(self, source, origin=None, pinned=None):
        return self._identify_from(self.show_candidates(source, origin), "tv", pinned)

    def _in_root(self, source):
        return os.path.normpath(os.path.dirname(source)) in self.roots

    #----- The walk, shared by both kinds
    def _identify_from(self, rungs, kind, pinned=None):
        resolved = self._identify_walk(rungs, kind, pinned)
        #----- 'hold_candidates' preselects this entity.
        self._local.resolved_qid = (resolved or {}).get("qid")
        self._local.hold_entries = list((resolved or {}).get("disagree") or (resolved or {}).get("tied") or [])
        return resolved

    def _identify_walk(self, rungs, kind, pinned=None):
        self._local.scored = []
        self._local.searched = None
        self._local.resolved_qid = None
        self._local.hold_entries = []
        if pinned:
            resolved = self._resolve_operator(pinned, kind)
            if resolved is not None:
                resolved["identified_from"] = "manual" if resolved.get("manual") else "operator"
                resolved["missing"] = _missing(kind, resolved)
                log.info(
                    "identified by the operator: %s (%s) %s",
                    resolved.get("title") or resolved.get("show"),
                    resolved.get("year") or resolved.get("show_year"),
                    " ".join("%s=%s" % (f, resolved.get(f)) for f, _p in ID_PROPERTIES[kind]),
                )
                return resolved
        results = []
        id_rungs = set()
        for rung, ids, readings in rungs:
            pinned = {
                field: (ids or {}).get(field)
                for field, _prop in ID_PROPERTIES[kind]
                if (ids or {}).get(field)
            }
            readings = [r for r in readings if r[0]]
            if pinned:
                id_rungs.add(rung)
                name, year = (readings[0][0], readings[0][1]) if readings else ("", None)
                resolved = self._resolve_by_ids(pinned, name, year, kind)
            elif readings:
                self._local.searched = _searched_record(self._local.searched, rung, readings)
                resolved = self._search_readings(readings, kind, rung)
            else:
                resolved = None
            if resolved is None:
                log.debug("rung %s did not resolve", rung)
                continue
            resolved["identified_from"] = rung
            resolved["missing"] = _missing(kind, resolved)
            if resolved["missing"]:
                log.debug("rung %s resolved %s lacking %s, no vote", rung,
                          resolved.get("qid"), ", ".join(resolved["missing"]))
            elif resolved.get("tied"):
                log.debug("rung %s tied between %s", rung,
                          ", ".join(t.get("qid") or "?" for t in resolved["tied"]))
            else:
                log.debug("rung %s resolved %s", rung, resolved.get("qid"))
            results.append((rung, resolved))

        #----- (rung, qid, the tied entry or None, resolved)
        votes = []
        for rung, resolved in results:
            if resolved["missing"]:
                continue
            if resolved.get("tied"):
                votes.extend((rung, t["qid"], t, resolved) for t in resolved["tied"])
            else:
                votes.append((rung, resolved.get("qid"), None, resolved))
        id_votes = [v for v in votes if v[0] in id_rungs]
        if id_votes and len({v[1] for v in id_votes}) == 1:
            for rung, qid, _entry, _resolved in votes:
                if rung not in id_rungs and qid != id_votes[0][1]:
                    log.debug("rung %s reached %s, the id rungs settle on %s", rung, qid, id_votes[0][1])
            votes = id_votes
        distinct = []
        for _rung, qid, _entry, _resolved in votes:
            if qid not in distinct:
                distinct.append(qid)

        if not distinct:
            for rung, resolved in results:
                log.info(
                    "identified from %s: %s (%s) %s, lacking %s",
                    rung, resolved.get("title") or resolved.get("show"),
                    resolved.get("year") or resolved.get("show_year"),
                    " ".join("%s=%s" % (f, resolved.get(f)) for f, _p in ID_PROPERTIES[kind]),
                    ", ".join(resolved["missing"]),
                )
                return resolved
            return None

        if len(distinct) == 1:
            rungs_agreeing = []
            for rung, _qid, _entry, _resolved in votes:
                if rung not in rungs_agreeing:
                    rungs_agreeing.append(rung)
            resolved = votes[0][3]
            resolved["identified_from"] = ", ".join(rungs_agreeing)
            log.info(
                "identified from %s: %s (%s) %s",
                resolved["identified_from"], resolved.get("title") or resolved.get("show"),
                resolved.get("year") or resolved.get("show_year"),
                " ".join("%s=%s" % (f, resolved.get(f)) for f, _p in ID_PROPERTIES[kind]),
            )
            return resolved

        disagree = []
        for rung, qid, entry, resolved in votes:
            if entry is None:
                entry = {
                    "qid": qid,
                    "label": resolved.get("title") or resolved.get("show"),
                    "year": resolved.get("year") or resolved.get("show_year"),
                    "ids": {f: resolved.get(f) for f, _p in ID_PROPERTIES[kind]},
                }
            disagree.append(dict(entry, rung=rung))
        for candidate in getattr(self._local, "scored", None) or []:
            for entry in disagree:
                if candidate["id"] == entry["qid"] and (candidate.get("outcome") or "").startswith("accepted"):
                    candidate["outcome"] = "resolved from %s, disagrees with %s" % (
                        entry["rung"],
                        ", ".join(e["qid"] for e in disagree if e["qid"] != entry["qid"]),
                    )
                    break
        log.info(
            "the rungs disagree: %s",
            "; ".join("%s resolves to %s (%s, %s)" % (e["rung"], e["label"], e["qid"], e["year"] or "no date")
                      for e in disagree),
        )
        resolved = dict(votes[0][3])
        resolved.pop("tied", None)
        resolved["disagree"] = disagree
        return resolved

    #----- Only a name that finds nothing complete is searched again without its leading abbreviations;
    #----- subtitle equality applies to that second search alone.
    def _search_readings(self, readings, kind, rung=None):
        resolved = self._best_reading(readings, kind)
        if resolved is not None:
            resolved.pop("searched_name", None)
        if resolved is not None and not _missing(kind, resolved):
            return resolved
        dropped = []
        for name, year, _label in readings:
            short, removed = _drop_abbreviations(name)
            if short and (short, year) not in [(d[0], d[1]) for d in dropped]:
                dropped.append((short, year, ABBREVIATION_DROPPED, removed))
        if not dropped:
            return resolved
        self._local.searched = _searched_record(
            self._local.searched, rung, [(n, y, l) for n, y, l, _r in dropped]
        )
        log.info(
            "no complete identity from the name as read, searching again without %s",
            ", ".join("%r" % r for _n, _y, _l, r in dropped),
        )
        retry = self._best_reading([(n, y, l) for n, y, l, _r in dropped], kind, subtitles=True)
        if retry is None or _missing(kind, retry):
            return resolved
        searched_name = retry.pop("searched_name", None)
        for name, _year, _label, removed in dropped:
            if name == searched_name:
                retry["notes"] = list(retry.get("notes") or []) + [
                    "searched as %r with %r dropped as an abbreviation" % (name, removed)
                ]
                break
        return retry

    def _best_reading(self, readings, kind, subtitles=False):
        results = []
        for name, year, label in readings:
            self._local.reading = label
            try:
                score, resolved = self._resolve_by_search(name, year, kind, subtitles=subtitles)
            finally:
                self._local.reading = None
            if resolved is None:
                continue
            resolved["reading"] = label
            resolved["searched_name"] = name
            results.append((score, resolved))
        if not results:
            return None
        best = results[0]
        for other in results[1:]:
            if _readings_tie(best, other, kind):
                best = (best[0], self._tie_readings(best, other, kind))
                continue
            best = _prefer(best, other, kind)
        return best[1]

    def _tie_readings(self, best, other, kind):
        score = best[0]
        entries = []
        for _score, resolved in (best, other):
            entries.append({
                "qid": resolved.get("qid"),
                "label": resolved.get("title") or resolved.get("show"),
                "year": resolved.get("year") or resolved.get("show_year"),
                "ids": {f: resolved.get(f) for f, _p in ID_PROPERTIES[kind]},
                "reading": resolved.get("reading"),
            })
        for entry in entries:
            others = ", ".join(
                "%s (%s)" % (e["qid"], e["reading"]) for e in entries if e is not entry
            )
            for candidate in getattr(self._local, "scored", None) or []:
                if candidate["id"] == entry["qid"] and candidate.get("reading") == entry["reading"]:
                    candidate["outcome"] = "tied at %.2f with %s" % (score, others)
        log.info(
            "the name resolves to %d entities at score %.2f across its readings: %s",
            len(entries), score,
            "; ".join("%s (%s, %s)" % (e["qid"], e["year"], e["reading"]) for e in entries),
        )
        tied = dict(best[1])
        tied["tied"] = entries
        return tied

    #----- Wikidata search paths
    def _resolve_by_ids(self, pinned, name, year, kind):
        qids = []
        for field, prop in ID_PROPERTIES[kind]:
            if not pinned.get(field):
                continue
            #----- CirrusSearch matches a statement directly, so an id finds its entity without a name.
            for qid in self.search_text("haswbstatement:%s=%s" % (prop, pinned[field])):
                if qid not in qids:
                    qids.append(qid)
        if not qids:
            log.info("no Wikidata entity carries %s", _describe(pinned))
            return None
        name = name or ""
        _score, resolved = self._resolve_from(
            "ids:%s" % _describe(pinned), self.labels(qids), name,
            titles.normalise_for_match(name), year, set(), kind, cutoff=0.0, pinned=pinned,
        )
        return resolved

    #----- Prefix hits already matched the whole string, so they are ordered by score but not cut;
    #----- full-text hits matched on any word and must clear the cutoff.
    def _resolve_by_search(self, title, year, kind, subtitles=False):
        if kind == "movie":
            terms = _movie_search_terms(title, year)
        else:
            terms = ("%s (TV series)" % title, title)
        wanted = titles.normalise_for_match(title)
        seen = set()
        best = (None, None)
        for term in terms:
            score, resolved = self._resolve_from(
                term, self.search_entities(term), title, wanted, year, seen, kind, cutoff=0.0,
                subtitles=subtitles,
            )
            best = _prefer(best, (score, resolved), kind)
            if resolved is not None and not _missing(kind, resolved) and not resolved.get("tied"):
                return score, resolved
        qids = [q for q in self.search_text(title) if q not in seen]
        if qids:
            score, resolved = self._resolve_from(
                "text:%s" % title, self.labels(qids), title, wanted, year, seen, kind,
                cutoff=self.title_cutoff(), subtitles=subtitles,
            )
            best = _prefer(best, (score, resolved), kind)
        return best

    def _resolve_from(self, term, candidates, title, wanted, year, seen, kind, cutoff, pinned=None,
                      subtitles=False):
        scored = []
        for candidate in candidates:
            if candidate["id"] in seen:
                continue
            seen.add(candidate["id"])
            score = _candidate_score(
                title, wanted, candidate, contained=self.contained_score(), subtitles=subtitles,
            )
            log.debug(
                "search %r: %s %r scored %.3f", term, candidate["id"],
                candidate.get("label"), score,
            )
            candidate["score"] = score
            candidate["term"] = term
            if score < cutoff:
                candidate["outcome"] = "below the %.2f cutoff" % cutoff
                self._remember(candidate)
                continue
            scored.append((score, candidate))
        accept = self._accept_movie_hit if kind == "movie" else self._accept_show_hit
        best = (None, None)
        complete = []
        for score, candidate in sorted(scored, key=lambda pair: -pair[0]):
            if complete and score < complete[0][0]:
                break
            resolved = accept(candidate, title, year, pinned)
            self._remember(candidate)
            if resolved is None:
                continue
            if _missing(kind, resolved):
                best = _prefer(best, (score, resolved), kind)
                continue
            if best[1] is not None and score < best[0]:
                break
            complete.append((score, resolved, candidate))
            if pinned:
                break
        if not complete:
            return best
        score, resolved, _candidate = complete[0]
        #----- two complete hits at one score on a name search is a tie, and a tie holds.
        if len(complete) > 1:
            tied = []
            for tie_score, tie_resolved, tie_candidate in complete:
                tie_candidate["outcome"] = "tied at %.2f with %s" % (
                    tie_score, ", ".join(c["id"] for _s, _r, c in complete if c is not tie_candidate))
                tied.append({
                    "qid": tie_resolved.get("qid"),
                    "label": tie_resolved.get("title") or tie_resolved.get("show"),
                    "year": tie_resolved.get("year") or tie_resolved.get("show_year"),
                    "ids": {f: tie_resolved.get(f) for f, _p in ID_PROPERTIES[kind]},
                })
            log.info(
                "title %r resolves to %d entities at score %.2f on search %r: %s",
                title, len(complete), score, term,
                "; ".join("%s (%s)" % (t["qid"], t["year"]) for t in tied),
            )
            resolved = dict(resolved)
            resolved["tied"] = tied
            return score, resolved
        if score < 1.0 and not pinned:
            log.info(
                "title %r matched %r by similarity %.3f on search %r",
                title, resolved.get("title") or resolved.get("show"), score, term,
            )
        return score, resolved

    def _remember(self, candidate):
        scored = getattr(self._local, "scored", None)
        if scored is not None:
            reading = getattr(self._local, "reading", None)
            if reading and not candidate.get("reading"):
                candidate["reading"] = reading
            scored.append(candidate)

    def _accept_movie_hit(self, candidate, title, year, pinned=None):
        entity = self.entity(candidate["id"])
        ids = self.ids_from_entity(entity)
        candidate["ids"] = ids
        if not _carries(ids, pinned, candidate):
            candidate["outcome"] = "does not carry %s" % _describe(pinned)
            return None
        #----- A pinned id outranks a year read off the disk;  the page check still applies.
        if not pinned and year and ids["year"] and abs(int(ids["year"]) - int(year)) > 1:
            log.info(
                "%s %r is a %s release, not %s, skipping",
                candidate["id"], candidate.get("label"), ids["year"], year,
            )
            candidate["outcome"] = "released %s, not %s" % (ids["year"], year)
            return None
        label = _label(entity) or title
        names = _names(entity, label)
        notes = []
        if ids["tmdb"]:
            ok, note = self.verify_tmdb("movie", ids["tmdb"], names, ids["year"])
            if not ok:
                log.info("tmdb %s did not confirm %r, skipping", ids["tmdb"], label)
                candidate["outcome"] = "tmdb %s did not confirm the title" % ids["tmdb"]
                return None
            if note:
                notes.append(note)
        candidate["outcome"] = _accepted_outcome("movie", ids)
        return {
            "title": label,
            "year": ids["year"] or year,
            "tmdb": ids["tmdb"],
            "imdb": ids["imdb"],
            "qid": candidate["id"],
            "notes": notes,
        }

    def _accept_show_hit(self, candidate, name, year, pinned=None):
        entity = self.entity(candidate["id"])
        ids = self.ids_from_entity(entity, kind="tv")
        candidate["ids"] = ids
        if not _carries(ids, pinned, candidate):
            candidate["outcome"] = "does not carry %s" % _describe(pinned)
            return None
        if not pinned and year and ids["year"] and abs(int(ids["year"]) - int(year)) > 1:
            log.info(
                "%s %r started in %s, not %s, skipping",
                candidate["id"], candidate.get("label"), ids["year"], year,
            )
            candidate["outcome"] = "started in %s, not %s" % (ids["year"], year)
            return None
        label = _label(entity) or name
        names = _names(entity, label)
        notes = []
        tvdb = ids["tvdb"]
        tvdb_from = "wikidata %s" % P_TVDB if tvdb else None
        if tvdb:
            ok, note = self.verify_tvdb(tvdb, names, ids["year"])
            if not ok:
                log.info("tvdb %s did not confirm show %r, skipping", tvdb, label)
                candidate["outcome"] = "tvdb %s did not confirm the show" % tvdb
                return None
            if note:
                notes.append(note)
        elif ids["imdb"]:
            tvdb = self.tvdb_from_imdb(ids["imdb"], names, ids["year"])
            if tvdb:
                tvdb_from = "imdb %s through the TVDB remote-id lookup" % ids["imdb"]
                notes.append("tvdb %s found through %s" % (tvdb, tvdb_from))
                candidate["ids"] = dict(ids, tvdb=tvdb)
        tmdb = ids["tmdb"]
        if tmdb:
            ok, note = self.verify_tmdb("tv", tmdb, names, ids["year"], own_claim=True)
            if not ok:
                log.info("tmdb %s did not confirm show %r, dropped", tmdb, label)
                notes.append("tmdb %s did not confirm the show and was dropped" % tmdb)
                tmdb = None
            elif note:
                notes.append(note)
        candidate["outcome"] = _accepted_outcome("tv", dict(ids, tvdb=tvdb, tmdb=tmdb))
        return {
            "show": label,
            "show_year": ids["year"] or year,
            "tvdb": tvdb,
            "tvdb_from": tvdb_from,
            "tmdb": tmdb,
            "qid": candidate["id"],
            "notes": notes,
        }


    #----- The operator's choice, one selection per source
    def _resolve_operator(self, chosen, kind):
        qid = str(chosen.get("qid") or "").strip()
        if not qid:
            return _manual_identity(chosen, kind)
        entity = self.entity(qid)
        ids = self.ids_from_entity(entity, kind=kind)
        label = _label(entity) or chosen.get("name")
        if not label:
            log.info("operator choice %s names no entity and no title", qid or "(none)")
            return None
        names = _names(entity, label) or [label]
        year = chosen.get("year") or ids["year"]
        notes = ["identity chosen by the operator: %s" % _describe(
            {k: v for k, v in chosen.items() if v and k not in ("apply_to",)})]
        result = {"qid": qid or None, "notes": notes}
        for field, prop in ID_PROPERTIES[kind]:
            value = chosen.get(field) or ids.get(field)
            result[field] = str(value) if value else None
            if not value:
                continue
            if chosen.get(field) and str(chosen.get(field)) != str(ids.get(field)):
                notes.append("%s %s supplied by the operator, the entity carries %s" % (field, value, ids.get(field)))
            if field == "imdb":
                continue
            if field == "tvdb":
                ok, _note = self.verify_tvdb(value, names, None)
            else:
                ok, _note = self.verify_tmdb(kind, value, names, None)
            notes.append("%s %s %s by its page" % (field, value, "confirmed" if ok else "not confirmed"))
        if kind == "movie":
            result.update({"title": label, "year": year})
        else:
            result.update({"show": label, "show_year": year,
                           "tvdb_from": "operator" if chosen.get("tvdb") else ("wikidata %s" % P_TVDB if ids["tvdb"] else None)})
        return result

    #----- The best match for a hold the operator has to settle
    def hold_candidates(self, kind):
        scored = list(getattr(self._local, "scored", None) or [])
        searched = getattr(self._local, "searched", None) or {}
        resolved_qid = getattr(self._local, "resolved_qid", None)
        entries = list(getattr(self._local, "hold_entries", None) or [])
        out = {"searched": searched, "guess": []}
        try:
            if entries:
                out["guess"] = [
                    _guess_entry(i, kind) for i in (_entry_identity(e, kind) for e in entries)
                    if _complete_identity(i, kind)
                ]
                return out
            known = {}
            for candidate in scored:
                if candidate["id"] not in known:
                    known[candidate["id"]] = self._seed_identity(candidate["id"], kind)
            if resolved_qid and _complete_identity(known.get(resolved_qid), kind):
                out["guess"] = [_guess_entry(known[resolved_qid], kind)]
                return out
            prop = P_TMDB if kind == "movie" else P_TMDB_TV
            carried = {str(i["tmdb"]) for i in known.values() if i.get("tmdb")}
            for tmdb in self._tmdb_search(kind, _reading_names(searched)):
                if tmdb in carried:
                    continue
                carried.add(tmdb)
                for qid in self.search_text("haswbstatement:%s=%s" % (prop, tmdb)):
                    if qid not in known:
                        known[qid] = self._seed_identity(qid, kind)
            out["guess"] = self._best_guess(list(known.values()), searched, kind)
        except ProviderError as exc:
            log.info("the best match is incomplete, a provider could not be reached: %s", exc)
            out["error"] = str(exc)
        return out

    def _seed_identity(self, qid, kind):
        entity = self.entity(qid)
        ids = self.ids_from_entity(entity, kind=kind)
        label = _label(entity)
        identity = {
            "qid": qid,
            "title": label,
            "year": ids.get("year"),
            "names": _names(entity, label) if label else [],
            "tmdb": ids.get("tmdb"),
        }
        if kind == "movie":
            identity["imdb"] = ids.get("imdb")
        else:
            tvdb = ids.get("tvdb")
            if not tvdb and ids.get("imdb"):
                tvdb = (self.tvdb_by_imdb(ids["imdb"]) or {}).get("tvdb")
            identity["tvdb"] = tvdb
        return identity

    def _best_guess(self, identities, searched, kind):
        readings = [r for r in (searched.get("readings") or [searched]) if r.get("name")]
        ranked = []
        for identity in identities:
            if not _complete_identity(identity, kind):
                continue
            best = None
            for reading in readings:
                year = reading.get("year")
                if year and abs(int(identity["year"]) - int(year)) > 1:
                    continue
                score = _candidate_score(
                    reading["name"], titles.normalise_for_match(reading["name"]),
                    {"label": identity["title"], "aliases": identity.get("names") or []},
                    contained=self.contained_score(),
                    subtitles=reading.get("label") == ABBREVIATION_DROPPED,
                )
                best = score if best is None else max(best, score)
            if best is not None:
                ranked.append((best, identity))
        if not ranked:
            return []
        top = max(score for score, _identity in ranked)
        return [_guess_entry(identity, kind) for score, identity in ranked if score == top]

    def _tmdb_search(self, kind, names):
        found = []
        for name in names:
            status, body = self.client.fetch(TMDB_SEARCH % (kind, urllib.parse.quote_plus(name)))
            if status != 200:
                continue
            for tmdb, _title, _date in TMDB_SEARCH_HIT.findall(body):
                if tmdb not in found:
                    found.append(tmdb)
        return found

    #----- Television catalogue
    def series_slug(self, tvdb_id):
        final = self.client.final_url(TVDB_DEREFERRER % tvdb_id)
        m = re.search(r"/series/([^/?#]+)", final)
        if not m:
            raise ProviderError("could not resolve a slug for tvdbid %s" % tvdb_id)
        return m.group(1)

    def episodes_for_order(self, slug, order="official"):
        status, body = self.client.fetch(TVDB_SEASONS % (slug, order))
        if status != 200:
            return []
        found = _catalogue_entries(EPISODE_LABEL.findall(body))
        #----- allseasons omits season 0;  the specials sit on their own page as a plain table.
        if not any(e["season"] == 0 for e in found):
            status, body = self.client.fetch(TVDB_SPECIALS % (slug, order))
            if status == 200:
                specials = _catalogue_entries(SPECIAL_ROW.findall(body))
                if specials:
                    log.debug("%s %s: %d special(s) from the season 0 page", slug, order, len(specials))
                found.extend(specials)
        if found and not self._english_original(slug):
            self._translate_titles(slug, order, found)
        for entry in found:
            entry.pop("href", None)
        return found

    def _english_original(self, slug):
        status, body = self.client.fetch(TVDB_SERIES % slug)
        if status != 200:
            return True
        m = TVDB_ORIGINAL_LANGUAGE.search(body)
        if not m:
            return True
        return html.unescape(m.group(1)).strip().lower().startswith("english")

    def _translate_titles(self, slug, order, entries):
        translated = 0
        for entry in entries:
            href = entry.get("href")
            if not href:
                continue
            status, body = self.client.fetch(urllib.parse.urljoin(TVDB_ROOT, href))
            if status != 200:
                continue
            m = TVDB_ENG_TITLE.search(body)
            title = html.unescape(m.group(1)).strip() if m else ""
            if title:
                entry["title"] = title
                translated += 1
        log.info(
            "%s %s: %d of %d episode titles read from the English translation",
            slug, order, translated, len(entries),
        )

    def all_orders(self, slug):
        orders = {}
        for order in ("official", "dvd", "absolute", "alternate"):
            found = self.episodes_for_order(slug, order)
            if found:
                orders[order] = found
        return orders

    def identify(self, row, container, kind):
        source = row["source_path"]
        pinned = row.get("pinned") or None
        if kind == "movie":
            return self.identify_movie(source, container, row.get("origin_path"), pinned)

        resolved = self.identify_show(source, row.get("origin_path"), pinned)
        if resolved is None or resolved["missing"]:
            return resolved
        if resolved.get("manual"):
            return {
                "title": resolved["episode_title"],
                "show": resolved["show"],
                "show_year": resolved["show_year"],
                "season": resolved["season"],
                "episode": resolved["episode"],
                "episode_last": None,
                "tvdb": resolved["tvdb"],
                "tvdb_from": resolved.get("tvdb_from"),
                "tmdb": resolved["tmdb"],
                "qid": None,
                "notes": list(resolved.get("notes") or []),
                "match_method": "manual",
                "match_score": None,
                "order_warnings": [],
                "identified_from": "manual",
                "manual": True,
                "missing": [],
            }

        slug = self.series_slug(resolved["tvdb"])
        catalogue = self.episodes_for_order(slug, "official")
        if not catalogue:
            return None

        segment = (container or {}).get("segment_title") if isinstance(container, dict) else None
        entry, how, score = episodemod.match_episode(
            source, catalogue, cutoff=self.title_cutoff(), extra=segment,
        )
        max_range_span = self._figure("max_range_span", episodemod.MAX_RANGE_SPAN)
        if entry is None:
            parsed = episodemod.parse_path(source, max_range_span=max_range_span)
            if parsed is None:
                return None
            listed = _catalogue_entry(catalogue, parsed["season"], parsed["first"])
            if listed is not None:
                log.warning(
                    "no title match for %s, falling back to source numbering S%02dE%02d,"
                    " title '%s' from the catalogue",
                    os.path.basename(source), parsed["season"], parsed["first"], listed["title"],
                )
                entry = listed
            else:
                log.warning(
                    "no title match for %s, falling back to source numbering S%02dE%02d,"
                    " which the catalogue does not list; title kept from the file name",
                    os.path.basename(source), parsed["season"], parsed["first"],
                )
                entry = {
                    "season": parsed["season"],
                    "episode": parsed["first"],
                    "title": episodemod.title_from_filename(source),
                }
            how = "fallback-numbering"

        warnings = self._order_warning(slug, catalogue)
        episode_last, episode_title, shared = episodemod.episode_range(source, entry, catalogue, max_range_span)
        notes = list(resolved.get("notes") or [])
        if not shared:
            notes.append(
                "covers S%02dE%02d-E%02d, episodes with differing titles, named for the first"
                % (entry["season"], entry["episode"], episode_last)
            )
        return {
            "title": episode_title,
            "show": resolved["show"],
            "show_year": resolved["show_year"],
            "season": entry["season"],
            "episode": entry["episode"],
            "episode_last": episode_last,
            "tvdb": resolved["tvdb"],
            "tvdb_from": resolved.get("tvdb_from"),
            "tmdb": resolved["tmdb"],
            "qid": resolved.get("qid"),
            "notes": notes,
            "match_method": how,
            "match_score": score,
            "order_warnings": warnings,
            "identified_from": resolved.get("identified_from"),
            "missing": [],
        }

    def _order_warning(self, slug, aired):
        try:
            orders = self.all_orders(slug)
        except ProviderError:
            return []
        aired_counts = episodemod.season_counts(aired)
        notes = []
        for order, catalogue in orders.items():
            if order == "official":
                continue
            counts = episodemod.season_counts(catalogue)
            if counts != aired_counts:
                notes.append(
                    "%s order has per-season counts %s against aired %s"
                    % (order, counts, aired_counts)
                )
        return notes


#----- the trailing delimiter is a lookahead so two adjacent years both match.
YEAR_IN_NAME = re.compile(r"(?:^|[.\s(\[_-])(19\d{2}|20\d{2})(?=[)\].\s_-]|$)")
JUNK = titles.RELEASE_TOKENS
RELEASE_YEAR = "release year"
TITLE_WORD = "title word"


#----- Name cleaning
def _catalogue_entries(rows):
    return [
        {
            "season": int(season),
            "episode": int(episode),
            "href": href,
            "title": html.unescape(re.sub(r"<[^>]+>", "", title)).strip(),
        }
        for season, episode, href, title in rows
    ]


def _catalogue_entry(catalogue, season, episode):
    for entry in catalogue:
        if entry["season"] == season and entry["episode"] == episode:
            return entry
    return None


#----- Title matching under the section 9 rules
def _describe(ids):
    return " ".join("%s=%s" % (k, v) for k, v in ids.items())


def _label(entity):
    return ((entity.get("labels") or {}).get("en") or {}).get("value")


def _names(entity, label):
    aliases = [a.get("value") for a in (entity.get("aliases") or {}).get("en") or []]
    return [n for n in [label] + aliases if n]


def _page_identity(body):
    m = PAGE_TITLE.search(body)
    text = SITE_SUFFIX.sub("", html.unescape(m.group(1))) if m else ""
    year = None
    ym = PAGE_YEAR.search(text)
    if ym:
        year = int(ym.group(1))
        text = text[: ym.start()]
    eng = TVDB_ENG_TITLE.search(body)
    if eng and html.unescape(eng.group(1)).strip():
        text = html.unescape(eng.group(1))
    return text.strip() or None, year


def _page_confirms(body, names, year, what, earlier_ok=False, cutoff=TITLE_CUTOFF, contained=CONTAINED_SCORE):
    names = [n for n in names if n]
    if not names:
        return False, None
    page_title, page_year = _page_identity(body)
    best = 0.0
    if page_title:
        wanted = titles.normalise_for_match(page_title)
        best = max(_name_score(page_title, wanted, n, contained) for n in names)
        log.debug("%s page title %r scored %.3f against %s", what, page_title, best, names[0])
    if year and page_year and abs(int(year) - page_year) > 1:
        if earlier_ok and page_year < int(year) and best >= 1.0:
            note = "%s is titled %r and first aired %s on its page against %s on Wikidata" % (
                what, page_title, page_year, year)
            log.info(note)
            return True, note
        log.info("%s is titled %r from %s, not %s", what, page_title, page_year, year)
        return False, None
    if best >= cutoff:
        return True, None
    #----- The body fallback uses the label alone;  a short alias like 'TFA' is found somewhere in any page.
    needle = re.sub(r"[^a-z0-9]+", "", names[0].lower())
    haystack = re.sub(r"[^a-z0-9]+", "", body.lower())
    return needle in haystack, None


def _missing(kind, resolved):
    resolved = resolved or {}
    required = MANUAL_REQUIRED[kind] if resolved.get("manual") else REQUIRED[kind]
    #----- season 0 and episode 0 are values, so only an absent field is missing.
    return [f for f in required if resolved.get(f) is None or resolved.get(f) == ""]


def _readings_tie(best, other, kind):
    if best[1] is None or other[1] is None or best[0] != other[0]:
        return False
    if best[1].get("tied") or other[1].get("tied"):
        return False
    if _missing(kind, best[1]) or _missing(kind, other[1]):
        return False
    return best[1].get("qid") != other[1].get("qid")


def _searched_record(record, rung, readings):
    entries = [{"name": n, "year": y, "label": l, "rung": rung} for n, y, l in readings]
    if record is None:
        return {
            "rung": rung,
            "name": readings[0][0],
            "year": readings[0][1],
            "readings": entries,
        }
    record = dict(record)
    record["readings"] = list(record.get("readings") or []) + entries
    return record


def _single(name, year):
    return [(name, year, None)]


def _prefer(best, other, kind):
    if other[1] is None:
        return best
    if best[1] is None:
        return other
    if other[0] != best[0]:
        return other if other[0] > best[0] else best
    if _missing(kind, best[1]) and not _missing(kind, other[1]):
        return other
    if best[1].get("tied") and not other[1].get("tied") and not _missing(kind, other[1]):
        return other
    return best


def _accepted_outcome(kind, ids):
    lacking = [f for f, _p in ID_PROPERTIES[kind] if not ids.get(f)]
    if not ids.get("year"):
        lacking.append("year")
    return "accepted" if not lacking else "accepted, lacks %s" % ", ".join(lacking)


def _carries(ids, pinned, candidate):
    if not pinned:
        return True
    for field, value in pinned.items():
        if str(ids.get(field)) != str(value):
            log.info(
                "%s %r carries %s=%s, not %s, skipping",
                candidate["id"], candidate.get("label"), field, ids.get(field), value,
            )
            return False
    return True


def _movie_search_terms(title, year):
    terms = []
    if year:
        terms.append("%s (%s film)" % (title, year))
    terms.append(title)
    return terms


def _name_score(title, wanted, name, contained=CONTAINED_SCORE):
    if titles.matches(name, title):
        return 1.0
    other = titles.normalise_for_match(name)
    if not other or not wanted:
        return 0.0
    if other == wanted:
        return 1.0
    #----- 'Return of the Jedi' inside 'Star Wars: Episode VI – Return of the Jedi'.
    if " %s " % wanted in " %s " % other:
        return contained
    return difflib.SequenceMatcher(None, wanted, other).ratio()


def _candidate_score(title, wanted, candidate, contained=CONTAINED_SCORE, subtitles=False):
    names = [candidate.get("label")]
    names.extend(candidate.get("aliases") or [])
    match = candidate.get("match") or {}
    if match.get("text"):
        names.append(match["text"])
    names = [n for n in names if n]
    if subtitles:
        names += [part for n in names for part in _subtitles(n)]
    best = max((_name_score(title, wanted, n, contained) for n in names), default=0.0)
    for name in names:
        expanded = _expand_initials(wanted, name)
        if expanded:
            best = max(best, _name_score(expanded, expanded, name, contained))
    return best


def _complete_identity(identity, kind):
    if not identity or not identity.get("title") or not identity.get("year"):
        return False
    return all(identity.get(field) for field, _prop in ID_PROPERTIES[kind])


def _entry_identity(entry, kind):
    ids = entry.get("ids") or {}
    identity = {"qid": entry.get("qid"), "title": entry.get("label"), "year": entry.get("year")}
    for field, _prop in ID_PROPERTIES[kind]:
        identity[field] = ids.get(field)
    return identity


def _guess_entry(identity, kind):
    entry = {"qid": identity.get("qid"), "title": identity.get("title"), "year": identity.get("year")}
    for field, _prop in ID_PROPERTIES[kind]:
        entry[field] = identity.get(field)
    return entry


def _reading_names(searched):
    names = []
    for reading in searched.get("readings") or [searched]:
        name = reading.get("name")
        if name and name not in names:
            names.append(name)
    return names


#----- Every value of a manual identity is the operator's;  nothing is read from the provider or the file.
def _manual_identity(chosen, kind):
    name = str(chosen.get("name") or "").strip() or None
    year = int(chosen["year"]) if chosen.get("year") else None
    entered = {k: v for k, v in chosen.items() if v not in (None, "")}
    notes = ["identity entered by the operator: %s" % _describe(entered)]
    if kind == "movie":
        return {
            "qid": None, "manual": True, "notes": notes,
            "title": name, "year": year,
            "tmdb": chosen.get("tmdb") or None, "imdb": chosen.get("imdb") or None,
        }
    return {
        "qid": None, "manual": True, "notes": notes,
        "show": name, "show_year": year,
        "tvdb": chosen.get("tvdb") or None, "tmdb": chosen.get("tmdb") or None,
        "tvdb_from": "operator" if chosen.get("tvdb") else None,
        "season": chosen.get("season"), "episode": chosen.get("episode"),
        "episode_title": chosen.get("episode_title") or None,
    }


def _subtitles(name):
    return [name[m.end():].strip() for m in SUBTITLE_SPLIT.finditer(name) if name[m.end():].strip()]


#----- 'lotr' against 'lord of the rings the return of the king' reads as the run of words it spells.
def _expand_initials(wanted, name):
    words = (wanted or "").split()
    other = titles.normalise_for_match(name).split()
    if not words or not other:
        return None
    changed = False
    out = []
    for word in words:
        run = None
        if word not in other and titles.is_abbreviation(word):
            size = len(word)
            for start in range(0, len(other) - size + 1):
                window = other[start:start + size]
                if "".join(w[0] for w in window) == word:
                    run = window
                    break
        if run:
            out.extend(run)
            changed = True
        else:
            out.append(word)
    return " ".join(out) if changed else None


def _drop_abbreviations(name):
    words = str(name or "").split()
    count = 0
    while count < len(words) and titles.is_abbreviation(words[count]):
        count += 1
    if count == 0 or count == len(words):
        return None, None
    return " ".join(words[count:]), " ".join(words[:count])


#----- the edition comes from the arrival name, the parent folder, or a repair copy's origin name.
def _edition_of(source, origin=None):
    stem = os.path.splitext(os.path.basename(source))[0]
    parent = os.path.basename(os.path.dirname(source))
    candidates = [stem, parent]
    if origin:
        candidates.append(os.path.splitext(os.path.basename(origin))[0])
    for candidate in candidates:
        label, _matched = titles.edition_from_name(candidate)
        if label:
            return label
    return None


def _tidy(text):
    text = re.sub(r"(?<=\s)-|-(?=\s)|^-|-$", " ", text)
    text = re.sub(r"[(\[]\s*[)\]]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" ._-")


def _cut_title(text):
    text = text.replace(".", " ").replace("_", " ")
    for m in JUNK.finditer(text):
        head = text[: m.start()].strip(" ._-()[]")
        if head:
            text = text[: m.start()]
            break
        if re.search(r"\d", m.group(0)):
            text = ""
            break
    return _tidy(text)


def _delimited(text, m):
    return (
        m.start() < m.start(1)
        and text[m.start()] in "(["
        and text[m.end():m.end() + 1] in (")", "]")
    )


def _readings(text):
    matches = list(YEAR_IN_NAME.finditer(text))
    if not matches:
        title = _cut_title(text)
        return [(title, None, None)] if title else []
    m = matches[-1]
    year = int(m.group(1))
    delimited = _delimited(text, m)
    before = _cut_title(text[: m.start()])
    release = before or _cut_title(text[m.end() + (1 if delimited else 0):])
    if len(matches) > 1 or delimited:
        return [(release, year, RELEASE_YEAR)] if release else []
    word = _cut_title(text) if before else ("%s %s" % (m.group(1), release)).strip()
    readings = []
    if release:
        readings.append((release, year, RELEASE_YEAR))
    if word and word.lower() != release.lower():
        readings.append((word, None, TITLE_WORD))
    return readings


def _clean_movie_name(name):
    text = titles.strip_release_tag(titles.strip_release_group(titles.strip_edition(name)))
    return _readings(text)


def _clean_show_name(name, parent):
    parens = None
    for candidate in (name, parent):
        found = titles.YEAR_IN_PARENS.findall(candidate or "")
        if found:
            parens = int(found[-1])
            break
    for candidate in (name, parent):
        if not candidate:
            continue
        cleaned = titles.strip_release_group(candidate)
        cleaned = titles.EPISODE_MARK.split(cleaned)[0]
        cleaned = re.split(r"(?:^|[^a-z0-9])\d{1,2}x\d{1,3}", cleaned, flags=re.I)[0]
        readings = _readings(cleaned)
        if readings:
            if parens:
                readings = [(n, y or parens, l or RELEASE_YEAR) for n, y, l in readings]
            return readings
    return []
