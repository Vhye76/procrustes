import difflib
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import VERSION
from . import episodes as episodemod
from . import tags as tagsmod
from . import titles

log = logging.getLogger("provider")

USER_AGENT = "procrustes/%s (+https://github.com/Vhye76/procrustes)" % VERSION
THROTTLE_SECONDS = 0.25
TIMEOUT = 30
TITLE_CUTOFF = episodemod.FUZZY_CUTOFF
CONTAINED_SCORE = 0.9
TEXT_SEARCH_LIMIT = 10

ID_FIELDS = {
    "movie": ("tmdb", "imdb"),
    "tv": ("tvdb", "tmdb"),
}
REQUIRED = {
    "movie": ("title", "year", "tmdb", "imdb"),
    "tv": ("show", "show_year", "tvdb", "tmdb"),
}
MANUAL_REQUIRED = {
    "movie": ("title", "year"),
    "tv": ("show", "show_year", "season", "episode", "episode_title"),
}
ABBREVIATION_DROPPED = "abbreviation dropped"
TRAILING_DROPPED = "trailing words dropped"
SUBTITLE_SPLIT = re.compile(r"\s*:\s*|\s+-\s+|\s*[–—]\s*")

TMDB_API = "https://api.themoviedb.org/3"
TMDB_HOST = "api.themoviedb.org"
TMDB_IMAGE = "https://image.tmdb.org/t/p/original"
TMDB_LANGUAGE = "en-US"
TMDB_KIND = {"movie": "movie", "tv": "tv"}
API_KEY_V3 = re.compile(r"^[0-9a-f]{32}$", re.I)
ALIAS_COUNTRIES = ("US", "GB")
FIND_SOURCES = {"imdb": "imdb_id", "tvdb": "tvdb_id"}
FIND_RESULTS = {"movie": "movie_results", "tv": "tv_results"}


class ProviderError(RuntimeError):
    pass


class RateLimited(ProviderError):
    pass


def _authorised(url, key):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if key and urllib.parse.urlsplit(url).netloc == TMDB_HOST:
        if API_KEY_V3.match(key):
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"api_key": key})
        else:
            headers["Authorization"] = "Bearer %s" % key
    return urllib.request.Request(url, headers=headers)


def check_key(key, timeout=TIMEOUT):
    key = str(key or "").strip()
    if not key:
        return None
    try:
        with urllib.request.urlopen(_authorised(TMDB_API + "/authentication", key), timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return "TMDb rejected the key"
        return "TMDb answered HTTP %d" % exc.code
    except urllib.error.URLError as exc:
        return "TMDb could not be reached: %s" % exc.reason
    except (OSError, ValueError) as exc:
        return "TMDb could not be reached: %s" % exc
    if not body.get("success"):
        return "TMDb rejected the key"
    return None


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
            log.debug("throttling %.2fs before the next request", throttle - elapsed)
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
        request = _authorised(url, str(self._figure("tmdb_api_key", "") or "").strip())
        try:
            with urllib.request.urlopen(request, timeout=self._timeout()) as response:
                status = response.getcode()
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read().decode("utf-8", "replace") if exc.fp else ""
            if status == 401:
                raise ProviderError("TMDb rejected the API key on %s" % url)
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


def _year(value):
    m = re.search(r"(\d{4})", str(value or ""))
    return int(m.group(1)) if m else None


#----- Resolution
class Provider:
    def __init__(self, client, roots=(), settings=None):
        self.client = client
        self.roots = {os.path.normpath(str(root)) for root in roots if root}
        self.settings = settings
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

    def _api(self, path, **params):
        params.setdefault("language", TMDB_LANGUAGE)
        url = "%s%s?%s" % (TMDB_API, path, urllib.parse.urlencode(params))
        status, body = self.client.fetch(url)
        if status == 404:
            return None
        if status != 200:
            raise ProviderError("%s returned HTTP %d" % (url, status))
        try:
            return json.loads(body)
        except ValueError as exc:
            raise ProviderError("%s did not return JSON: %s" % (url, exc))

    def search(self, kind, term):
        data = self._api("/search/%s" % TMDB_KIND[kind], query=term, include_adult="false") or {}
        found = []
        for hit in data.get("results") or []:
            tmdb = str(hit.get("id") or "")
            if tmdb and tmdb not in found:
                found.append(tmdb)
        return found[:TEXT_SEARCH_LIMIT]

    def details(self, kind, tmdb_id):
        return self._api(
            "/%s/%s" % (TMDB_KIND[kind], tmdb_id),
            append_to_response="alternative_titles,external_ids",
        )

    def find(self, kind, field, value):
        data = self._api(
            "/find/%s" % urllib.parse.quote(str(value)), external_source=FIND_SOURCES[field],
        ) or {}
        return [str(hit["id"]) for hit in data.get(FIND_RESULTS[kind]) or [] if hit.get("id")]

    def season(self, tmdb_id, number):
        return self._api("/tv/%s/season/%s" % (tmdb_id, number))

    def candidate(self, kind, tmdb_id):
        found = self.details(kind, tmdb_id)
        if not found or not found.get("id"):
            log.info("tmdb %s %s does not exist", kind, tmdb_id)
            return None
        return {
            "id": str(found["id"]),
            "label": _label(kind, found),
            "aliases": _aliases(kind, found),
            "details": found,
        }

    def poster(self, kind, tmdb_id):
        if not tmdb_id:
            return None
        try:
            found = self.details(kind, tmdb_id)
        except ProviderError as exc:
            log.debug("poster lookup failed for tmdb %s %s: %s", kind, tmdb_id, exc)
            return None
        path = (found or {}).get("poster_path")
        if not path:
            log.debug("tmdb %s %s has no poster", kind, tmdb_id)
            return None
        return TMDB_IMAGE + path

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
            resolved["edition"] = _edition_of(
                source, origin, removed=resolved.get("reading") == EDITION_REMOVED)
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

        readings = _clean_show_name(
            stem, "" if self._in_root(source) else parent, episodemod.season_from_folder(source),
        )
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
        self._local.resolved_tmdb = (resolved or {}).get("tmdb")
        self._local.hold_entries = list((resolved or {}).get("disagree") or (resolved or {}).get("tied") or [])
        return resolved

    def _identify_walk(self, rungs, kind, pinned=None):
        self._local.scored = []
        self._local.searched = None
        self._local.resolved_tmdb = None
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
                    " ".join("%s=%s" % (f, resolved.get(f)) for f in ID_FIELDS[kind]),
                )
                return resolved
        results = []
        id_rungs = set()
        for rung, ids, readings in rungs:
            pinned = {
                field: (ids or {}).get(field)
                for field in ID_FIELDS[kind]
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
                log.debug("rung %s resolved tmdb %s lacking %s, no vote", rung,
                          resolved.get("tmdb"), ", ".join(resolved["missing"]))
            elif resolved.get("tied"):
                log.debug("rung %s tied between %s", rung,
                          ", ".join(t.get("tmdb") or "?" for t in resolved["tied"]))
            else:
                log.debug("rung %s resolved tmdb %s", rung, resolved.get("tmdb"))
            results.append((rung, resolved))

        #----- (rung, tmdb, the tied entry or None, resolved)
        votes = []
        for rung, resolved in results:
            if resolved["missing"]:
                continue
            if resolved.get("tied"):
                votes.extend((rung, t["tmdb"], t, resolved) for t in resolved["tied"])
            else:
                votes.append((rung, resolved.get("tmdb"), None, resolved))
        id_votes = [v for v in votes if v[0] in id_rungs]
        if id_votes and len({v[1] for v in id_votes}) == 1:
            for rung, tmdb, _entry, _resolved in votes:
                if rung not in id_rungs and tmdb != id_votes[0][1]:
                    log.debug("rung %s reached tmdb %s, the id rungs settle on %s", rung, tmdb, id_votes[0][1])
            votes = id_votes
        distinct = []
        for _rung, tmdb, _entry, _resolved in votes:
            if tmdb not in distinct:
                distinct.append(tmdb)

        if not distinct:
            for rung, resolved in results:
                log.info(
                    "identified from %s: %s (%s) %s, lacking %s",
                    rung, resolved.get("title") or resolved.get("show"),
                    resolved.get("year") or resolved.get("show_year"),
                    " ".join("%s=%s" % (f, resolved.get(f)) for f in ID_FIELDS[kind]),
                    ", ".join(resolved["missing"]),
                )
                return resolved
            return None

        if len(distinct) == 1:
            rungs_agreeing = []
            for rung, _tmdb, _entry, _resolved in votes:
                if rung not in rungs_agreeing:
                    rungs_agreeing.append(rung)
            resolved = votes[0][3]
            resolved["identified_from"] = ", ".join(rungs_agreeing)
            log.info(
                "identified from %s: %s (%s) %s",
                resolved["identified_from"], resolved.get("title") or resolved.get("show"),
                resolved.get("year") or resolved.get("show_year"),
                " ".join("%s=%s" % (f, resolved.get(f)) for f in ID_FIELDS[kind]),
            )
            return resolved

        disagree = []
        for rung, tmdb, entry, resolved in votes:
            if entry is None:
                entry = _hold_entry(resolved, kind)
            disagree.append(dict(entry, rung=rung))
        for candidate in getattr(self._local, "scored", None) or []:
            for entry in disagree:
                if candidate["id"] == entry["tmdb"] and (candidate.get("outcome") or "").startswith("accepted"):
                    candidate["outcome"] = "resolved from %s, disagrees with %s" % (
                        entry["rung"],
                        ", ".join("tmdb %s" % e["tmdb"] for e in disagree if e["tmdb"] != entry["tmdb"]),
                    )
                    break
        log.info(
            "the rungs disagree: %s",
            "; ".join("%s resolves to %s (tmdb %s, %s)" % (e["rung"], e["label"], e["tmdb"], e["year"] or "no date")
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
        retry = self._abbreviations_dropped(readings, kind, rung)
        if retry is None:
            retry = self._trailing_dropped(readings, kind, rung)
        return retry if retry is not None else resolved

    def _abbreviations_dropped(self, readings, kind, rung=None):
        dropped = []
        for name, year, _label in readings:
            short, removed = _drop_abbreviations(name)
            if short and (short, year) not in [(d[0], d[1]) for d in dropped]:
                dropped.append((short, year, ABBREVIATION_DROPPED, removed))
        if not dropped:
            return None
        self._local.searched = _searched_record(
            self._local.searched, rung, [(n, y, l) for n, y, l, _r in dropped]
        )
        log.info(
            "no complete identity from the name as read, searching again without %s",
            ", ".join("%r" % r for _n, _y, _l, r in dropped),
        )
        retry = self._best_reading([(n, y, l) for n, y, l, _r in dropped], kind, subtitles=True)
        return _dropped_outcome(retry, dropped, kind, "searched as %r with %r dropped as an abbreviation")

    def _trailing_dropped(self, readings, kind, rung=None):
        if kind != "movie":
            return None
        by_drop = {}
        seen = set()
        for name, year, _label in readings:
            #----- the year check is the only guard against a shorter title's namesake.
            if not year:
                continue
            words = name.split()
            for keep in range(len(words) - 1, max(1, -(-len(words) // 2)) - 1, -1):
                short = " ".join(words[:keep])
                if (short.lower(), year) in seen or _article_only(short):
                    continue
                seen.add((short.lower(), year))
                by_drop.setdefault(len(words) - keep, []).append(
                    (short, year, TRAILING_DROPPED, " ".join(words[keep:]))
                )
        for drop in sorted(by_drop):
            group = by_drop[drop]
            searched = [(n, y, l) for n, y, l, _r in group]
            self._local.searched = _searched_record(self._local.searched, rung, searched)
            log.info(
                "no complete identity from the name as read, searching again without its last %d word(s): %s",
                drop, ", ".join("%r" % n for n, _y, _l in searched),
            )
            retry = _dropped_outcome(
                self._best_reading(searched, kind), group, kind, "searched as %r with %r dropped from the end"
            )
            if retry is not None:
                return retry
        return None

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
            entries.append(dict(_hold_entry(resolved, kind), reading=resolved.get("reading")))
        for entry in entries:
            others = ", ".join(
                "tmdb %s (%s)" % (e["tmdb"], e["reading"]) for e in entries if e is not entry
            )
            for candidate in getattr(self._local, "scored", None) or []:
                if candidate["id"] == entry["tmdb"] and candidate.get("reading") == entry["reading"]:
                    candidate["outcome"] = "tied at %.2f with %s" % (score, others)
        log.info(
            "the name resolves to %d entities at score %.2f across its readings: %s",
            len(entries), score,
            "; ".join("tmdb %s (%s, %s)" % (e["tmdb"], e["year"], e["reading"]) for e in entries),
        )
        tied = dict(best[1])
        tied["tied"] = entries
        return tied

    def _resolve_by_ids(self, pinned, name, year, kind):
        found = []
        if pinned.get("tmdb"):
            found.append(str(pinned["tmdb"]))
        for field in ID_FIELDS[kind]:
            if field == "tmdb" or not pinned.get(field):
                continue
            for tmdb in self.find(kind, field, pinned[field]):
                if tmdb not in found:
                    found.append(tmdb)
        candidates = [c for c in (self.candidate(kind, tmdb) for tmdb in found) if c]
        if not candidates:
            log.info("TMDb has no %s carrying %s", kind, _describe(pinned))
            return None
        name = name or ""
        _score, resolved = self._resolve_from(
            "ids:%s" % _describe(pinned), candidates, name,
            titles.normalise_for_match(name), year, set(), kind, cutoff=0.0, pinned=pinned,
        )
        return resolved

    def _resolve_by_search(self, title, year, kind, subtitles=False):
        candidates = [c for c in (self.candidate(kind, tmdb) for tmdb in self.search(kind, title)) if c]
        return self._resolve_from(
            "search:%s" % title, candidates, title, titles.normalise_for_match(title), year, set(), kind,
            cutoff=self.title_cutoff(), subtitles=subtitles,
        )

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
                "search %r: tmdb %s %r scored %.3f", term, candidate["id"],
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
                    tie_score, ", ".join("tmdb %s" % c["id"] for _s, _r, c in complete if c is not tie_candidate))
                tied.append(_hold_entry(tie_resolved, kind))
            log.info(
                "title %r resolves to %d entities at score %.2f on search %r: %s",
                title, len(complete), score, term,
                "; ".join("tmdb %s (%s)" % (t["tmdb"], t["year"]) for t in tied),
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
        ids = _ids_of("movie", candidate["details"])
        candidate["ids"] = ids
        if not _carries(ids, pinned, candidate):
            candidate["outcome"] = "does not carry %s" % _describe(pinned)
            return None
        #----- A pinned id outranks a year read off the disk.
        if not pinned and year and ids["year"] and abs(int(ids["year"]) - int(year)) > 1:
            log.info(
                "tmdb %s %r is a %s release, not %s, skipping",
                candidate["id"], candidate.get("label"), ids["year"], year,
            )
            candidate["outcome"] = "released %s, not %s" % (ids["year"], year)
            return None
        notes = _filled(ids, pinned, "movie")
        candidate["outcome"] = _accepted_outcome("movie", ids)
        return {
            "title": candidate.get("label") or title,
            "year": ids["year"],
            "tmdb": ids["tmdb"],
            "imdb": ids["imdb"],
            "notes": notes,
        }

    def _accept_show_hit(self, candidate, name, year, pinned=None):
        ids = _ids_of("tv", candidate["details"])
        candidate["ids"] = ids
        if not _carries(ids, pinned, candidate):
            candidate["outcome"] = "does not carry %s" % _describe(pinned)
            return None
        if not pinned and year and ids["year"] and abs(int(ids["year"]) - int(year)) > 1:
            log.info(
                "tmdb %s %r started in %s, not %s, skipping",
                candidate["id"], candidate.get("label"), ids["year"], year,
            )
            candidate["outcome"] = "started in %s, not %s" % (ids["year"], year)
            return None
        tvdb_from = "tmdb external ids" if ids["tvdb"] else None
        notes = _filled(ids, pinned, "tv")
        if not tvdb_from and ids["tvdb"]:
            tvdb_from = "the rung's own id"
        candidate["outcome"] = _accepted_outcome("tv", ids)
        return {
            "show": candidate.get("label") or name,
            "show_year": ids["year"],
            "tvdb": ids["tvdb"],
            "tvdb_from": tvdb_from,
            "tmdb": ids["tmdb"],
            "notes": notes,
        }

    #----- The operator's choice, one selection per source
    def _resolve_operator(self, chosen, kind):
        match = str(chosen.get("match") or "").strip()
        if not match:
            return _manual_identity(chosen, kind)
        candidate = self.candidate(kind, match)
        found = (candidate or {}).get("details") or {}
        ids = _ids_of(kind, found) if found else {f: None for f in ID_FIELDS[kind]}
        label = (candidate or {}).get("label") or chosen.get("name")
        if not label:
            log.info("operator choice tmdb %s names no entity and no title", match)
            return None
        year = chosen.get("year") or ids.get("year")
        notes = ["identity chosen by the operator: %s" % _describe(
            {k: v for k, v in chosen.items() if v and k not in ("apply_to",)})]
        result = {"notes": notes}
        for field in ID_FIELDS[kind]:
            value = chosen.get(field) or ids.get(field)
            result[field] = str(value) if value else None
            if chosen.get(field) and ids.get(field) and str(chosen.get(field)) != str(ids.get(field)):
                notes.append("%s %s supplied by the operator, TMDb carries %s" % (field, value, ids.get(field)))
        if kind == "movie":
            result.update({"title": label, "year": year})
        else:
            result.update({"show": label, "show_year": year,
                           "tvdb_from": "operator" if chosen.get("tvdb") else ("tmdb external ids" if ids.get("tvdb") else None)})
        return result

    #----- The best match for a hold the operator has to settle
    def hold_candidates(self, kind):
        scored = list(getattr(self._local, "scored", None) or [])
        searched = getattr(self._local, "searched", None) or {}
        resolved_tmdb = getattr(self._local, "resolved_tmdb", None)
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
                    known[candidate["id"]] = _seed_identity(candidate, kind)
            if resolved_tmdb and _complete_identity(known.get(resolved_tmdb), kind):
                out["guess"] = [_guess_entry(known[resolved_tmdb], kind)]
                return out
            for name in _reading_names(searched):
                for tmdb in self.search(kind, name):
                    if tmdb in known:
                        continue
                    candidate = self.candidate(kind, tmdb)
                    if candidate:
                        known[tmdb] = _seed_identity(candidate, kind)
            out["guess"] = self._best_guess(list(known.values()), searched, kind)
        except ProviderError as exc:
            log.info("the best match is incomplete, a provider could not be reached: %s", exc)
            out["error"] = str(exc)
        return out

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

    #----- Television catalogue
    def episode_catalogue(self, tmdb_id):
        found = self.details("tv", tmdb_id) or {}
        numbers = sorted({
            int(s["season_number"]) for s in found.get("seasons") or []
            if s.get("season_number") is not None
        })
        catalogue = []
        for number in numbers:
            season = self.season(tmdb_id, number) or {}
            for episode in season.get("episodes") or []:
                if episode.get("episode_number") is None:
                    continue
                catalogue.append({
                    "season": int(episode.get("season_number", number)),
                    "episode": int(episode["episode_number"]),
                    "title": str(episode.get("name") or "").strip(),
                })
        log.debug("tmdb %s: %d episode(s) across %d season(s)", tmdb_id, len(catalogue), len(numbers))
        return catalogue

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
                "notes": list(resolved.get("notes") or []),
                "match_method": "manual",
                "match_score": None,
                "order_warnings": [],
                "identified_from": "manual",
                "manual": True,
                "missing": [],
            }

        catalogue = self.episode_catalogue(resolved["tmdb"])
        if not catalogue:
            return None

        segment = (container or {}).get("segment_title") if isinstance(container, dict) else None
        entry, how, score = episodemod.match_episode(
            source, catalogue, cutoff=self.title_cutoff(), extra=segment, show=resolved["show"],
        )
        max_range_span = self._figure("max_range_span", episodemod.MAX_RANGE_SPAN)
        if entry is None:
            parsed = episodemod.parse_path(source, max_range_span=max_range_span)
            if parsed is None:
                return None
            listed = _catalogue_entry(catalogue, parsed["season"], parsed["first"])
            if listed is None and parsed.get("weak"):
                log.warning(
                    "no title match for %s, and its bare number S%02dE%02d is not in the catalogue",
                    os.path.basename(source), parsed["season"], parsed["first"],
                )
                return {
                    "show": resolved["show"],
                    "tmdb": resolved["tmdb"],
                    "unlisted": "S%02dE%02d" % (parsed["season"], parsed["first"]),
                }
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
            "notes": notes,
            "match_method": how,
            "match_score": score,
            "order_warnings": [],
            "identified_from": resolved.get("identified_from"),
            "missing": [],
        }


#----- the trailing delimiter is a lookahead so two adjacent years both match.
YEAR_IN_NAME = re.compile(r"(?:^|[.\s(\[_-])(19\d{2}|20\d{2})(?=[)\].\s_-]|$)")
JUNK = titles.RELEASE_TOKENS
RELEASE_YEAR = "release year"
TITLE_WORD = "title word"
EDITION_REMOVED = "edition words removed"


#----- Name cleaning
def _catalogue_entry(catalogue, season, episode):
    for entry in catalogue:
        if entry["season"] == season and entry["episode"] == episode:
            return entry
    return None


#----- Title matching under the section 9 rules
def _describe(ids):
    return " ".join("%s=%s" % (k, v) for k, v in ids.items())


def _label(kind, found):
    if kind == "movie":
        return found.get("title") or found.get("original_title")
    return found.get("name") or found.get("original_name")


def _aliases(kind, found):
    label = _label(kind, found)
    original = found.get("original_title" if kind == "movie" else "original_name")
    alternative = (found.get("alternative_titles") or {}).get("titles" if kind == "movie" else "results") or []
    out = []
    for name in [original] + [a.get("title") for a in alternative if a.get("iso_3166_1") in ALIAS_COUNTRIES]:
        if name and name != label and name not in out:
            out.append(name)
    return out


def _ids_of(kind, found):
    external = found.get("external_ids") or {}
    if kind == "movie":
        return {
            "tmdb": str(found["id"]),
            "imdb": found.get("imdb_id") or external.get("imdb_id") or None,
            "year": _year(found.get("release_date")),
        }
    tvdb = external.get("tvdb_id")
    return {
        "tmdb": str(found["id"]),
        "tvdb": str(tvdb) if tvdb else None,
        "year": _year(found.get("first_air_date")),
    }


def _filled(ids, pinned, kind):
    notes = []
    for field, value in (pinned or {}).items():
        if field in ID_FIELDS[kind] and not ids.get(field):
            ids[field] = str(value)
            notes.append("%s %s from the rung, TMDb lists none" % (field, value))
    return notes


def _hold_entry(resolved, kind):
    return {
        "tmdb": resolved.get("tmdb"),
        "label": resolved.get("title") or resolved.get("show"),
        "year": resolved.get("year") or resolved.get("show_year"),
        "ids": {f: resolved.get(f) for f in ID_FIELDS[kind]},
    }


def _seed_identity(candidate, kind):
    ids = _ids_of(kind, candidate["details"])
    identity = {
        "title": candidate.get("label"),
        "year": ids.get("year"),
        "names": [n for n in [candidate.get("label")] + list(candidate.get("aliases") or []) if n],
    }
    for field in ID_FIELDS[kind]:
        identity[field] = ids.get(field)
    return identity


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
    return best[1].get("tmdb") != other[1].get("tmdb")


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


#----- a bare article scores as contained in nearly every title.
ARTICLES = {"the", "a", "an"}


def _article_only(name):
    return titles.normalise_for_match(name) in ARTICLES


def _dropped_outcome(retry, dropped, kind, note):
    if retry is None or _missing(kind, retry):
        return None
    searched_name = retry.pop("searched_name", None)
    for name, _year, _label, removed in dropped:
        if name == searched_name:
            retry["notes"] = list(retry.get("notes") or []) + [note % (name, removed)]
            break
    return retry


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
    lacking = [f for f in ID_FIELDS[kind] if not ids.get(f)]
    if not ids.get("year"):
        lacking.append("year")
    return "accepted" if not lacking else "accepted, lacks %s" % ", ".join(lacking)


def _carries(ids, pinned, candidate):
    for field, value in (pinned or {}).items():
        if ids.get(field) and str(ids[field]) != str(value):
            log.info(
                "%s %r carries %s=%s, not %s, skipping",
                candidate["id"], candidate.get("label"), field, ids.get(field), value,
            )
            return False
    return True


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
    return all(identity.get(field) for field in ID_FIELDS[kind])


def _entry_identity(entry, kind):
    ids = entry.get("ids") or {}
    identity = {"title": entry.get("label"), "year": entry.get("year")}
    for field in ID_FIELDS[kind]:
        identity[field] = ids.get(field)
    return identity


def _guess_entry(identity, kind):
    entry = {"title": identity.get("title"), "year": identity.get("year")}
    for field in ID_FIELDS[kind]:
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
            "manual": True, "notes": notes,
            "title": name, "year": year,
            "tmdb": chosen.get("tmdb") or None, "imdb": chosen.get("imdb") or None,
        }
    return {
        "manual": True, "notes": notes,
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
def _edition_of(source, origin=None, removed=False):
    stem = os.path.splitext(os.path.basename(source))[0]
    parent = os.path.basename(os.path.dirname(source))
    candidates = [stem, parent]
    if origin:
        candidates.append(os.path.splitext(os.path.basename(origin))[0])
    for candidate in candidates:
        label, _matched = titles.edition_from_name(candidate)
        if not label and removed:
            _rewritten, label = titles.strip_edition_phrases(candidate)
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
    text = titles.strip_release_tag(titles.strip_release_group(
        titles.strip_edition(titles.strip_leading_fields(name))))
    readings = _readings(text)
    #----- the full-name readings stay first, so an equal score for one record keeps the full name and no label.
    rewritten, _label = titles.strip_edition_phrases(text)
    if rewritten:
        seen = {r[0].lower() for r in readings}
        for found, year, label in _readings(rewritten):
            if label == TITLE_WORD or not found or found.lower() in seen or _article_only(found):
                continue
            seen.add(found.lower())
            readings.append((found, year, EDITION_REMOVED))
    return readings


def _clean_show_name(name, parent, season=None):
    parens = None
    for candidate in (name, parent):
        found = titles.YEAR_IN_PARENS.findall(candidate or "")
        if found:
            parens = int(found[-1])
            break
    for index, candidate in enumerate((name, parent)):
        if not candidate or (index == 1 and episodemod.SEASON_FOLDER.match(candidate.strip())):
            continue
        cleaned = titles.strip_release_group(candidate)
        parsed = episodemod.parse_filename(cleaned, default_season=season if index == 0 else None)
        if parsed is not None and parsed.get("weak"):
            cleaned = cleaned[: parsed["start"]]
        cleaned = titles.EPISODE_MARK.split(cleaned)[0]
        cleaned = re.split(r"(?:^|[^a-z0-9])\d{1,2}x\d{1,3}", cleaned, flags=re.I)[0]
        readings = _readings(cleaned)
        if readings:
            if parens:
                readings = [(n, y or parens, l or RELEASE_YEAR) for n, y, l in readings]
            return readings
    return []
