"""BoM list loading and match helpers (CRM only; no app models)."""

from __future__ import annotations

from pathlib import Path

from .client import SystemGroupClient, SystemGroupClientError
from .parser import (
    BomListEntry,
    SystemGroupParseError,
    match_bom_list_entries,
    parse_bom_list_document,
    split_bom_code,
)


class SystemGroupError(Exception):
    """User-facing failure talking to or interpreting SystemGroup CRM data."""


class AmbiguousBomMatch(SystemGroupError):
    """Multiple CRM BoM list rows matched the query."""

    def __init__(self, matches: list[BomListEntry], query: str):
        self.matches = matches
        self.query = query
        labels = "; ".join(m.label() for m in matches[:20])
        more = "" if len(matches) <= 20 else f" (+{len(matches) - 20} more)"
        super().__init__(
            f"Multiple CRM BoMs matched '{query}': {labels}{more}. "
            "Pick one BoM id (GUI pick list, --bom-id, or CRM URL)."
        )


class NoBomMatch(SystemGroupError):
    """No CRM BoM list row matched the query."""

    def __init__(self, query: str):
        self.query = query
        super().__init__(
            f"No CRM BoM on the list matched '{query}'. "
            "Check the parent part number/description, or enter a CRM BoM id / Edit URL."
        )


def load_bom_list_entries(
    *,
    client: SystemGroupClient | None = None,
    list_html: str | None = None,
    list_json: str | None = None,
    list_path: str | Path | None = None,
    stop_part_number: str | None = None,
    stop_revision: str | None = None,
) -> list[BomListEntry]:
    """Load BoM list rows from offline content/path or live CRM fetch.

    Live fetch early-exit (avoids walking the full catalog):

    - ``stop_part_number`` + ``stop_revision``: stop on the first page that
      contains that exact BoM code (unique PN/rev resolve).
    - ``stop_part_number`` alone: stop after the first page with no hits that
      follows a page that did hit (part-id cluster; covers Load revisions and
      page-boundary straddles without requiring every later page).
    """
    try:
        if list_json is not None:
            return parse_bom_list_document(list_json, content_type="application/json")
        if list_html is not None:
            return parse_bom_list_document(list_html, content_type="text/html")
        if list_path is not None:
            content = Path(list_path).read_text(encoding="utf-8")
            ctype = (
                "application/json"
                if str(list_path).lower().endswith(".json")
                or content.lstrip()[:1] in "{["
                else "text/html"
            )
            return parse_bom_list_document(content, content_type=ctype)
        sg = client or SystemGroupClient()
        # Walk EntityListPaging pages (CRM defaults to 50 rows/page).
        merged: list[BomListEntry] = []
        seen: set[str] = set()
        stop_pn = (stop_part_number or "").strip() or None
        stop_rev = (stop_revision or "").strip() or None
        seen_part_hits = False
        for body, ctype in sg.iter_bom_list_pages():
            page_entries = parse_bom_list_document(body, content_type=ctype)
            for entry in page_entries:
                if entry.bom_id in seen:
                    continue
                seen.add(entry.bom_id)
                merged.append(entry)
            if not stop_pn:
                continue
            hits = match_bom_list_entries(
                page_entries, part_number=stop_pn, revision=stop_rev
            )
            if stop_rev:
                if hits:
                    break
                continue
            # Part-only: keep paging through the hit cluster, then one miss.
            if hits:
                seen_part_hits = True
            elif seen_part_hits:
                break
        if not merged:
            raise SystemGroupParseError(
                "No BoM list rows found across paginated list pages."
            )
        return merged
    except (SystemGroupClientError, SystemGroupParseError, OSError) as exc:
        raise SystemGroupError(str(exc)) from exc


def list_revisions_for_part(
    part_number: str,
    *,
    client: SystemGroupClient | None = None,
    list_html: str | None = None,
    list_json: str | None = None,
    list_path: str | Path | None = None,
    entries: list[BomListEntry] | None = None,
) -> list[BomListEntry]:
    """Return all CRM BoM list rows for a part number (any revision).

    Raises ``NoBomMatch`` when nothing matches. Does not raise
    ``AmbiguousBomMatch`` — callers always get the full revision set.

    Live CRM fetches stop paging once the part-id hit cluster ends (see
    ``load_bom_list_entries``), so Load revisions does not walk the catalog.
    """
    pn = (part_number or "").strip()
    if not pn:
        raise SystemGroupError("Part ID is required to load revisions.")
    code_pn, _code_rev = split_bom_code(pn)
    pn = code_pn or pn
    rows = entries if entries is not None else load_bom_list_entries(
        client=client,
        list_html=list_html,
        list_json=list_json,
        list_path=list_path,
        stop_part_number=pn,
    )
    matches = match_bom_list_entries(rows, part_number=pn, revision=None)
    if not matches:
        raise NoBomMatch(pn)
    return sorted(
        matches,
        key=lambda e: ((e.revision or "").casefold(), e.bom_id),
    )


def resolve_bom_from_list(
    *,
    part_number: str | None = None,
    description: str | None = None,
    revision: str | None = None,
    client: SystemGroupClient | None = None,
    list_html: str | None = None,
    list_json: str | None = None,
    list_path: str | Path | None = None,
    entries: list[BomListEntry] | None = None,
) -> BomListEntry:
    """Fetch/parse the CRM BoM list and resolve a single match.

    Raises ``NoBomMatch`` or ``AmbiguousBomMatch``. When neither part_number nor
    description is set, returns AmbiguousBomMatch with the full list (browse).
    """
    pn = (part_number or "").strip() or None
    desc = (description or "").strip() or None
    rev = (revision or "").strip() or None
    query = pn or desc or "(all)"
    if pn and rev:
        query = f"{pn}/{rev}"

    rows = entries if entries is not None else load_bom_list_entries(
        client=client,
        list_html=list_html,
        list_json=list_json,
        list_path=list_path,
        # Unique PN/rev resolve can stop once that row is on a fetched page.
        stop_part_number=pn if (pn and rev) else None,
        stop_revision=rev if (pn and rev) else None,
    )

    if not pn and not desc:
        raise AmbiguousBomMatch(rows, query)

    matches = match_bom_list_entries(
        rows, part_number=pn, description=desc, revision=rev
    )
    if not matches:
        raise NoBomMatch(query)
    if len(matches) > 1:
        raise AmbiguousBomMatch(matches, query)
    return matches[0]
