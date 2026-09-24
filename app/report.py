import os

from . import rules, standards, state

NAME_CHECKS = ("folder name", "file name", "show folder", "season folder")


#----- Building one row
def title_of(finding, measured):
    tag = measured.get("tag") or {}
    stem = os.path.splitext(os.path.basename(finding["path"]))[0]
    if finding.get("kind") == "tv":
        if tag.get("show") and tag.get("season") is not None and tag.get("episode") is not None:
            code = "S%02dE%02d" % (tag["season"], tag["episode"])
            return " - ".join(p for p in (tag["show"], code, tag.get("title")) if p)
        return stem
    if tag.get("title"):
        return "%s (%s)" % (tag["title"], tag["year"]) if tag.get("year") else tag["title"]
    return stem


def _check_values(checks):
    by_name = {}
    for check in checks:
        by_name.setdefault(check.get("check"), []).append(check)
    names = [c for name in NAME_CHECKS for c in by_name.get(name, [])]
    component = by_name.get("component rules")
    episodes = by_name.get("episodes in file")
    return {
        "names": ("differs" if any(not c.get("ok") for c in names) else "match") if names else None,
        "component_rules": component[0].get("actual") if component else None,
        "episodes_in_file": episodes[0].get("expected") if episodes else None,
    }


#----- a row audited before the container summary was stored carries only these until its next pass.
def _unmeasured_values(measured):
    size = measured.get("size_bytes")
    duration = measured.get("duration_s")
    return {
        "codec": measured.get("codec"),
        "hdr_format": measured.get("hdr_format"),
        "gb": round(size / 1e9, 2) if size else None,
        "runtime_min": round(duration / 60.0, 1) if duration else None,
    }


def import_state(finding, orchestrator, store, repairable):
    title_id = finding.get("imported_title_id")
    in_pipeline = None
    if title_id:
        row = store.get(title_id)
        if row and state.in_pipeline(row["stage"]):
            in_pipeline = {
                "id": row["id"],
                "stage": row["stage"],
                "display_stage": state.display_name(row["stage"]),
            }
    elif finding.get("import_path") and os.path.exists(finding["import_path"]):
        in_pipeline = {"id": None, "stage": state.DETECTED, "display_stage": "awaiting detection"}
    return {
        "repairable": repairable,
        "in_pipeline": in_pipeline,
        "copy": orchestrator.import_progress(finding["id"]),
    }


def build_row(finding, profile, orchestrator, store):
    measured = finding.get("measured") or {}
    kind = finding.get("kind")
    checks = measured.get("rows") or []
    container = measured.get("container")
    if container:
        found = standards.values(
            container, kind, path=finding["path"], profile=profile, crop=measured.get("crop"),
            tag_structure=measured.get("tag_structure"), statistics_ratio=measured.get("statistics_ratio"),
        )
    else:
        found = _unmeasured_values(measured)
    found.update(_check_values(checks))
    found["title"] = title_of(finding, measured)
    found["kind"] = kind
    found["path"] = finding["path"]
    broken = rules.evaluate(found, kind, profile, checks=checks)
    found["broken"] = "; ".join(b["label"] for b in broken) or None
    unreadable = "unreadable" in (finding.get("checks") or [])
    return {
        "id": finding["id"],
        "kind": kind,
        "path": finding["path"],
        "values": {c.key: found.get(c.key) for c in rules.COLUMNS},
        "broken": broken,
        "severity": rules.severity(broken),
        "flagged": rules.flagged(broken),
        "detail": checks,
        "import": import_state(finding, orchestrator, store, any(b["repairable"] for b in broken)),
        "measured": "container" in measured,
        "unreadable": finding.get("summary") if unreadable else None,
    }


#----- The rules as the pages render them, each with its current standard and ignore flag
def describe_rules(settings):
    profiles = {kind: settings.profile(kind) for kind in ("movie", "tv")}
    out = []
    for rule in rules.RULES:
        profile = profiles[rule.kind or "movie"]
        row = rule.as_dict()
        row["standard"] = profile.get(rule.value_key) if rule.value_key else rule.fixed
        row["ignored"] = bool(profile.get(rule.ignore_key, rule.ignored))
        out.append(row)
    return out


def build(store, settings, orchestrator):
    libraries = sorted(orchestrator.layout.libraries)
    profiles = {kind: settings.profile(kind) for kind in libraries}
    rows = []
    for kind in libraries:
        for finding in store.audit_rows(kind):
            rows.append(build_row(finding, profiles[kind], orchestrator, store))
    return {
        "status": orchestrator.auditor.status(),
        "libraries": libraries,
        "rules": describe_rules(settings),
        "columns": [c.as_dict() for c in rules.COLUMNS],
        "rows": rows,
    }
