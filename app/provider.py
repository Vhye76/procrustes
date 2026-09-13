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

USER_AGENT = "mediaimport/%s (+https://github.com/Vhye76/mediaImport)" % VERSION
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

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY = "https://www.wikidata.org/wiki/Special:EntityData/%s.json"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
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
CANDIDATE_LIMIT = 8

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
    def __init__(self, cache_dir, throttle=THROTTLE_SECONDS):
        self.cache_dir = str(cache_dir)
        self.throttle = throttle
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

    def _wait(self):
        elapsed = time.time() - self._last
        if elapsed < self.throttle:
            log.debug("throttling %.1fs before the next request", self.throttle - elapsed)
            time.sleep(self.throttle - elapsed)
        self._last = time.time()

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
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
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
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
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
    def __init__(self, client, import_root=None):
        self.client = client
        self.import_root = os.path.normpath(str(import_root)) if import_root else None
        self._tvdb_posters = {}
        self._local = threading.local()

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
        return _page_confirms(body, names, year, "tvdb %s" % tvdb_id)

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
        ok, _note = _page_confirms(body, names, year, "tvdb %s" % found["tvdb"])
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
                embedded.get("title"),
                embedded.get("year"),
            ))

        for label, name in (("filename ids", stem), ("folder ids", parent),
                            ("origin folder ids", origin_parent)):
            if not name:
                continue
            ids = titles.ids_from_name(name)
            if ids["tmdb"] or ids["imdb"]:
                rungs.append((label, ids, titles.title_before_ids(name), ids["year"]))

        segment = (container or {}).get("segment_title")
        if segment:
            rungs.append(("segment title", {}, segment, None))

        guess, year = _clean_movie_name(stem)
        if guess:
            rungs.append(("filename", {}, guess, year))

        directory = os.path.normpath(os.path.dirname(source))
        if self.import_root and directory == self.import_root:
            log.debug("file sits directly in the watched root, parent folder rung skipped")
        else:
            parent_guess, parent_year = _clean_movie_name(parent)
            if parent_guess and parent_guess.lower() != (guess or "").lower():
                rungs.append(("parent folder", {}, parent_guess, parent_year))

        log.debug("identity ladder: %s", [r[0] for r in rungs])
        return rungs

    def identify_movie(self, source, container, origin=None, pinned=None):
        return self._identify_from(self.movie_candidates(source, container, origin), "movie", pinned)

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
                embedded.get("title"),
                None,
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
                rungs.append((label, ids, titles.title_before_ids(name), ids["year"]))

        guess = _clean_show_name(stem, parent)
        if guess:
            rungs.append(("filename", {}, guess, _show_year(stem, parent)))

        log.debug("show identity ladder: %s", [r[0] for r in rungs])
        return rungs

    def identify_show(self, source, origin=None, pinned=None):
        return self._identify_from(self.show_candidates(source, origin), "tv", pinned)

    #----- The walk, shared by both kinds
    def _identify_from(self, rungs, kind, pinned=None):
        self._local.scored = []
        self._local.searched = None
        if pinned:
            resolved = self._resolve_operator(pinned, kind)
            if resolved is not None:
                resolved["identified_from"] = "operator"
                resolved["missing"] = _missing(kind, resolved)
                log.info(
                    "identified by the operator: %s (%s) %s",
                    resolved.get("title") or resolved.get("show"),
                    resolved.get("year") or resolved.get("show_year"),
                    " ".join("%s=%s" % (f, resolved.get(f)) for f, _p in ID_PROPERTIES[kind]),
                )
                return resolved
        for rung, ids, name, year in rungs:
            pinned = {
                field: (ids or {}).get(field)
                for field, _prop in ID_PROPERTIES[kind]
                if (ids or {}).get(field)
            }
            if pinned:
                resolved = self._resolve_by_ids(pinned, name, year, kind)
            elif name:
                if self._local.searched is None:
                    self._local.searched = {"rung": rung, "name": name, "year": year}
                resolved = self._resolve_by_search(name, year, kind)
            else:
                resolved = None
            if resolved is None:
                log.debug("rung %s did not resolve", rung)
                continue
            resolved["identified_from"] = rung
            resolved["missing"] = _missing(kind, resolved)
            log.info(
                "identified from %s: %s (%s) %s",
                rung, resolved.get("title") or resolved.get("show"),
                resolved.get("year") or resolved.get("show_year"),
                " ".join("%s=%s" % (f, resolved.get(f)) for f, _p in ID_PROPERTIES[kind]),
            )
            return resolved
        return None

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
    def _resolve_by_search(self, title, year, kind):
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
            )
            best = _prefer(best, (score, resolved), kind)
            if resolved is not None and not _missing(kind, resolved):
                return resolved
        qids = [q for q in self.search_text(title) if q not in seen]
        if qids:
            score, resolved = self._resolve_from(
                "text:%s" % title, self.labels(qids), title, wanted, year, seen, kind,
                cutoff=TITLE_CUTOFF,
            )
            best = _prefer(best, (score, resolved), kind)
        return best[1]

    def _resolve_from(self, term, candidates, title, wanted, year, seen, kind, cutoff, pinned=None):
        scored = []
        for candidate in candidates:
            if candidate["id"] in seen:
                continue
            seen.add(candidate["id"])
            score = _candidate_score(title, wanted, candidate)
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
        for score, candidate in sorted(scored, key=lambda pair: -pair[0]):
            resolved = accept(candidate, title, year, pinned)
            self._remember(candidate)
            if resolved is None:
                continue
            if _missing(kind, resolved):
                best = _prefer(best, (score, resolved), kind)
                continue
            if best[1] is not None and score < best[0]:
                break
            if score < 1.0 and not pinned:
                log.info(
                    "title %r matched %r by similarity %.3f on search %r",
                    title, resolved.get("title") or resolved.get("show"), score, term,
                )
            return score, resolved
        return best

    def _remember(self, candidate):
        scored = getattr(self._local, "scored", None)
        if scored is not None:
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
        entity = self.entity(qid) if qid else {}
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

    #----- Candidates per source for a hold the operator has to settle
    def hold_candidates(self, kind):
        scored = list(getattr(self._local, "scored", None) or [])
        searched = getattr(self._local, "searched", None) or {}
        entities, seen = [], set()
        for c in sorted(scored, key=lambda c: -(c.get("score") or 0.0)):
            if c["id"] in seen:
                continue
            seen.add(c["id"])
            ids = c.get("ids") or {}
            entities.append({
                "qid": c["id"],
                "label": c.get("label"),
                "year": ids.get("year"),
                "tvdb": ids.get("tvdb"),
                "tmdb": ids.get("tmdb"),
                "imdb": ids.get("imdb"),
                "score": round(c.get("score") or 0.0, 3),
                "outcome": c.get("outcome") or "not evaluated",
                "term": c.get("term"),
            })
        entities = entities[:CANDIDATE_LIMIT]
        out = {"searched": searched, "wikidata": entities}
        name = searched.get("name")
        try:
            if kind == "tv":
                out["tvdb"] = self._tvdb_candidates(entities)
                out["tmdb"] = self._tmdb_candidates("tv", name, entities, P_TMDB_TV)
            else:
                out["tmdb"] = self._tmdb_candidates("movie", name, entities, P_TMDB)
                out["imdb"] = [
                    {"id": e["imdb"], "name": e["label"], "year": e["year"],
                     "origin": "%s %s" % (e["qid"], P_IMDB), "note": None}
                    for e in entities if e.get("imdb")
                ]
        except ProviderError as exc:
            log.info("candidate lists are incomplete, a provider could not be reached: %s", exc)
            out["error"] = str(exc)
        return out

    def _tvdb_candidates(self, entities):
        rows, seen = [], set()

        def add(tvdb, origin, name_hint, year_hint):
            key = str(tvdb)
            if key in seen or len(rows) >= CANDIDATE_LIMIT:
                return
            seen.add(key)
            row = {"id": key, "origin": origin, "name": name_hint, "year": year_hint,
                   "imdb": None, "tmdb": None, "note": None}
            body = self.tvdb_page(key)
            if body is None:
                row["note"] = "no series page"
            else:
                title, _year = _page_identity(body)
                row["name"] = title or name_hint
                m = re.search(r"imdb\.com/title/(tt\d+)", body)
                row["imdb"] = m.group(1) if m else None
                m = re.search(r"themoviedb\.org/tv/(\d+)", body)
                row["tmdb"] = m.group(1) if m else None
                row["note"] = "page found"
            rows.append(row)

        for e in entities:
            if e.get("tvdb"):
                add(e["tvdb"], "%s %s" % (e["qid"], P_TVDB), e.get("label"), e.get("year"))
        for e in entities:
            if e.get("tvdb") or not e.get("imdb"):
                continue
            found = self.tvdb_by_imdb(e["imdb"])
            if found:
                add(found["tvdb"], "imdb %s through the TVDB remote-id lookup" % e["imdb"],
                    found.get("name") or e.get("label"), found.get("year"))
        return rows

    def _tmdb_candidates(self, kind, name, entities, prop):
        rows = {}
        if name:
            status, body = self.client.fetch(TMDB_SEARCH % (kind, urllib.parse.quote_plus(name)))
            if status == 200:
                for tmdb, title, date in TMDB_SEARCH_HIT.findall(body):
                    if tmdb in rows or len(rows) >= CANDIDATE_LIMIT:
                        continue
                    rows[tmdb] = {
                        "id": tmdb,
                        "name": html.unescape(re.sub(r"<[^>]+>", "", title)).strip(),
                        "year": _year(date),
                        "origin": "TMDB search",
                        "note": None,
                    }
        for e in entities:
            tmdb = str(e.get("tmdb") or "")
            if not tmdb:
                continue
            origin = "%s %s" % (e["qid"], prop)
            if tmdb in rows:
                rows[tmdb]["origin"] += ", " + origin
                continue
            if len(rows) >= CANDIDATE_LIMIT:
                break
            body = self.tmdb_page(kind, tmdb)
            title, year = _page_identity(body) if body else (None, None)
            rows[tmdb] = {"id": tmdb, "name": title or e.get("label"), "year": year,
                          "origin": origin, "note": None if body else "no page"}
        return list(rows.values())

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

        slug = self.series_slug(resolved["tvdb"])
        catalogue = self.episodes_for_order(slug, "official")
        if not catalogue:
            return None

        entry, how, score = episodemod.match_episode(source, catalogue)
        if entry is None:
            parsed = episodemod.parse_filename(os.path.basename(source))
            if parsed is None:
                return None
            log.warning(
                "no title match for %s, falling back to source numbering", os.path.basename(source)
            )
            entry = {
                "season": parsed["season"],
                "episode": parsed["first"],
                "title": episodemod.title_from_filename(source),
            }
            how = "fallback-numbering"

        warnings = self._order_warning(slug, catalogue)
        return {
            "title": episodemod.to_part_suffix(entry["title"]),
            "show": resolved["show"],
            "show_year": resolved["show_year"],
            "season": entry["season"],
            "episode": entry["episode"],
            "tvdb": resolved["tvdb"],
            "tvdb_from": resolved.get("tvdb_from"),
            "tmdb": resolved["tmdb"],
            "qid": resolved.get("qid"),
            "notes": resolved.get("notes") or [],
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
YEAR_IN_NAME = re.compile(r"[.\s(\[_-](19\d{2}|20\d{2})(?=[)\].\s_-]|$)")
JUNK = re.compile(
    r"\b(1080p|720p|2160p|4k|bluray|blu-ray|bdrip|brrip|webrip|web-?dl|hdtv|remux|"
    r"x26[45]|h\.?26[45]|hevc|avc|xvid|divx|aac|ac3|dts(?:-hd)?|truehd|atmos|"
    r"ma|5\.1|7\.1|2\.0|10bit|8bit|hdr10?|dovi|dv|proper|repack|extended|"
    r"uncut|remastered|imax|multi|dual|complete)\b",
    re.I,
)


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


def _page_confirms(body, names, year, what, earlier_ok=False):
    names = [n for n in names if n]
    if not names:
        return False, None
    page_title, page_year = _page_identity(body)
    best = 0.0
    if page_title:
        wanted = titles.normalise_for_match(page_title)
        best = max(_name_score(page_title, wanted, n) for n in names)
        log.debug("%s page title %r scored %.3f against %s", what, page_title, best, names[0])
    if year and page_year and abs(int(year) - page_year) > 1:
        if earlier_ok and page_year < int(year) and best >= 1.0:
            note = "%s is titled %r and first aired %s on its page against %s on Wikidata" % (
                what, page_title, page_year, year)
            log.info(note)
            return True, note
        log.info("%s is titled %r from %s, not %s", what, page_title, page_year, year)
        return False, None
    if best >= TITLE_CUTOFF:
        return True, None
    #----- The body fallback uses the label alone;  a short alias like 'TFA' is found somewhere in any page.
    needle = re.sub(r"[^a-z0-9]+", "", names[0].lower())
    haystack = re.sub(r"[^a-z0-9]+", "", body.lower())
    return needle in haystack, None


def _missing(kind, resolved):
    return [f for f in REQUIRED[kind] if not (resolved or {}).get(f)]


def _prefer(best, other, kind):
    if other[1] is None:
        return best
    if best[1] is None:
        return other
    if other[0] != best[0]:
        return other if other[0] > best[0] else best
    if _missing(kind, best[1]) and not _missing(kind, other[1]):
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


def _name_score(title, wanted, name):
    if titles.matches(name, title):
        return 1.0
    other = titles.normalise_for_match(name)
    if not other or not wanted:
        return 0.0
    if other == wanted:
        return 1.0
    #----- 'Return of the Jedi' inside 'Star Wars: Episode VI – Return of the Jedi'.
    if " %s " % wanted in " %s " % other:
        return CONTAINED_SCORE
    return difflib.SequenceMatcher(None, wanted, other).ratio()


def _candidate_score(title, wanted, candidate):
    names = [candidate.get("label")]
    names.extend(candidate.get("aliases") or [])
    match = candidate.get("match") or {}
    if match.get("text"):
        names.append(match["text"])
    return max((_name_score(title, wanted, n) for n in names if n), default=0.0)


def _clean_movie_name(name):
    year = None
    matches = list(YEAR_IN_NAME.finditer(name))
    if matches:
        m = matches[-1]
        year = int(m.group(1))
        name = name[: m.start()]
    name = name.replace(".", " ").replace("_", " ")
    name = JUNK.sub(" ", name)
    name = re.sub(r"[-\[\(].*$", "", name)
    return re.sub(r"\s+", " ", name).strip(), year


def _show_year(name, parent):
    for candidate in (name, parent):
        found = titles.YEAR_IN_PARENS.findall(candidate or "")
        if found:
            return int(found[-1])
    return None


def _clean_show_name(name, parent):
    for candidate in (name, parent):
        cleaned = re.split(
            r"(?:^|[^a-z0-9])s\d{1,2}[\s._-]*e\d{1,3}", candidate, flags=re.I
        )[0]
        cleaned = re.split(r"(?:^|[^a-z0-9])\d{1,2}x\d{1,3}", cleaned, flags=re.I)[0]
        cleaned = cleaned.replace(".", " ").replace("_", " ")
        cleaned = JUNK.sub(" ", cleaned)
        cleaned = YEAR_IN_NAME.sub(" ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -[](){}")
        if cleaned:
            return cleaned
    return name
