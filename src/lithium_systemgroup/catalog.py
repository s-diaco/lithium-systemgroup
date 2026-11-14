"""BoM list loading and match helpers (CRM only; no app models)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .client import SystemGroupClient, SystemGroupClientError, parse_bom_list_pager
from .parser import (
    BomListEntry,
    SystemGroupParseError,
    match_bom_list_entries,
    parse_bom_list_document,
)

# Default cap for Part ID / Load revisions searches (org setting overrides in apps).
DEFAULT_MAX_RESULTS = 10


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


@dataclass
class BomSearchResult:
    """Capped Part ID search result (CRM Number StartsWith)."""

    entries: list[BomListEntry] = field(default_factory=list)
    truncated: bool = False
    query: str = ""
    max_results: int | None = None

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


def entry_bom_number(entry: BomListEntry) -> str:
    """CRM ``Number`` / کد BOM value reconstructed from a list row."""
    pn = (entry.part_number or "").strip()
    rev = (entry.revision or "").strip()
    if rev:
        return f"{pn}/{rev}"
    return pn


def match_number_startswith(
    entries: list[BomListEntry], prefix: str
) -> list[BomListEntry]:
    """Keep rows whose CRM Number starts with ``prefix`` (case-insensitive)."""
    needle = (prefix or "").strip().casefold()
    if not needle:
        return list(entries)
    out: list[BomListEntry] = []
    for entry in entries:
        number = entry_bom_number(entry).casefold()
        if number.startswith(needle):
            out.append(entry)
    return out


def load_bom_list_entries(
    *,
    client: SystemGroupClient | None = None,
    list_html: str | None = None,
    list_json: str | None = None,
    list_path: str | Path | None = None,
    stop_part_number: str | None = None,
    stop_revision: str | None = None,
    filter_prefix: str | None = None,
    max_results: int | None = None,
) -> list[BomListEntry]:
    """Load BoM list rows from offline content/path or live CRM fetch.

    Live fetch shortcuts (avoid walking the full catalog):

    - ``filter_prefix``: Number header ``StartsWith`` filter postback (Part ID
      search). Stops paging once ``max_results`` is reached when set.
    - ``stop_part_number`` alone (legacy): same Number StartsWith path when
      ``filter_prefix`` is unset.
    - ``stop_part_number`` + ``stop_revision``: stop on the first page that
      contains that exact BoM code (unique PN/rev resolve).
    """
    result = _load_bom_list_entries(
        client=client,
        list_html=list_html,
        list_json=list_json,
        list_path=list_path,
        stop_part_number=stop_part_number,
        stop_revision=stop_revision,
        filter_prefix=filter_prefix,
        max_results=max_results,
    )
    return result.entries


def _load_bom_list_entries(
    *,
    client: SystemGroupClient | None = None,
    list_html: str | None = None,
    list_json: str | None = None,
    list_path: str | Path | None = None,
    stop_part_number: str | None = None,
    stop_revision: str | None = None,
    filter_prefix: str | None = None,
    max_results: int | None = None,
) -> BomSearchResult:
    """Internal loader that tracks truncation against ``max_results``."""
    query = (filter_prefix or stop_part_number or "").strip()
    cap = max_results if max_results is None else max(1, int(max_results))
    try:
        if list_json is not None:
            rows = parse_bom_list_document(list_json, content_type="application/json")
            return _cap_offline(rows, query=query, max_results=cap)
        if list_html is not None:
            rows = parse_bom_list_document(list_html, content_type="text/html")
            return _cap_offline(rows, query=query, max_results=cap)
        if list_path is not None:
            content = Path(list_path).read_text(encoding="utf-8")
            ctype = (
                "application/json"
                if str(list_path).lower().endswith(".json")
                or content.lstrip()[:1] in "{["
                else "text/html"
            )
            rows = parse_bom_list_document(content, content_type=ctype)
            return _cap_offline(rows, query=query, max_results=cap)
        sg = client or SystemGroupClient()
        stop_pn = (stop_part_number or "").strip() or None
        stop_rev = (stop_revision or "").strip() or None
        prefix = (filter_prefix or "").strip() or None
        if not prefix and stop_pn and not stop_rev:
            prefix = stop_pn

        # Prefer CRM Number StartsWith for Part ID loads.
        if prefix and not stop_rev:
            try:
                return _load_via_number_startswith(
                    sg, prefix=prefix, max_results=cap
                )
            except (SystemGroupClientError, SystemGroupParseError):
                # Fall through to paginated walk + local StartsWith + cap.
                pass

        merged: list[BomListEntry] = []
        seen: set[str] = set()
        truncated = False
        seen_prefix_hits = False
        for body, ctype in sg.iter_bom_list_pages():
            page_entries = parse_bom_list_document(body, content_type=ctype)
            if prefix and not stop_rev:
                page_hits = match_number_startswith(page_entries, prefix)
                for entry in page_hits:
                    if entry.bom_id in seen:
                        continue
                    if cap is not None and len(merged) >= cap:
                        truncated = True
                        break
                    seen.add(entry.bom_id)
                    merged.append(entry)
                if truncated:
                    break
                if page_hits:
                    seen_prefix_hits = True
                elif seen_prefix_hits:
                    # Hit cluster ended (ID-desc list); stop paging.
                    break
                continue
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
            if stop_rev and hits:
                break
        if not merged:
            raise SystemGroupParseError(
                "No BoM list rows found across paginated list pages."
            )
        return BomSearchResult(
            entries=merged,
            truncated=truncated,
            query=prefix or query,
            max_results=cap,
        )
    except (SystemGroupClientError, SystemGroupParseError, OSError) as exc:
        raise SystemGroupError(str(exc)) from exc


def _cap_offline(
    rows: list[BomListEntry],
    *,
    query: str,
    max_results: int | None,
) -> BomSearchResult:
    matched = match_number_startswith(rows, query) if query else list(rows)
    if max_results is None or len(matched) <= max_results:
        return BomSearchResult(
            entries=matched,
            truncated=False,
            query=query,
            max_results=max_results,
        )
    return BomSearchResult(
        entries=matched[:max_results],
        truncated=True,
        query=query,
        max_results=max_results,
    )


def _load_via_number_startswith(
    sg: SystemGroupClient,
    *,
    prefix: str,
    max_results: int | None,
) -> BomSearchResult:
    """Filter by Number StartsWith, page until cap, report truncation."""
    if not sg._logged_in:
        sg.login()
    body, ctype = sg.fetch_bom_list()
    body, ctype = sg._post_bom_list_part_code_filter(
        body, prefix, filter_function="StartsWith", column="Number"
    )
    merged: list[BomListEntry] = []
    seen: set[str] = set()
    truncated = False
    current = body
    current_ctype = ctype
    while True:
        page_entries = parse_bom_list_document(current, content_type=current_ctype)
        # CRM already filtered; still apply local StartsWith for safety.
        page_hits = match_number_startswith(page_entries, prefix)
        for entry in page_hits:
            if entry.bom_id in seen:
                continue
            if max_results is not None and len(merged) >= max_results:
                truncated = True
                break
            seen.add(entry.bom_id)
            merged.append(entry)
        if truncated:
            break
        pager = parse_bom_list_pager(current)
        if pager is None or not pager.next_enabled or not pager.pager_target:
            break
        if max_results is not None and len(merged) >= max_results:
            truncated = True
            break
        current, current_ctype = sg._post_bom_list_next(current, pager)
    if not merged:
        raise SystemGroupParseError(
            f"No BoM list rows matched Number StartsWith '{prefix}'."
        )
    return BomSearchResult(
        entries=merged,
        truncated=truncated,
        query=prefix,
        max_results=max_results,
    )


def list_revisions_for_part(
    part_number: str,
    *,
    client: SystemGroupClient | None = None,
    list_html: str | None = None,
    list_json: str | None = None,
    list_path: str | Path | None = None,
    entries: list[BomListEntry] | None = None,
    max_results: int | None = DEFAULT_MAX_RESULTS,
) -> BomSearchResult:
    """Return CRM BoM list rows whose Number StartsWith the Part ID query.

    Pass the user input as-is (including ``PN/rev`` prefixes). Does not strip
    at ``/``. Caps at ``max_results`` (default 10); ``truncated`` is set when
    more matches exist. Raises ``NoBomMatch`` when nothing matches.
    """
    query = (part_number or "").strip()
    if not query:
        raise SystemGroupError("Part ID is required to load revisions.")
    if entries is not None:
        result = _cap_offline(entries, query=query, max_results=max_results)
    else:
        result = _load_bom_list_entries(
            client=client,
            list_html=list_html,
            list_json=list_json,
            list_path=list_path,
            filter_prefix=query,
            max_results=max_results,
        )
    if not result.entries:
        raise NoBomMatch(query)
    sorted_entries = sorted(
        result.entries,
        key=lambda e: ((e.revision or "").casefold(), e.bom_id),
    )
    return BomSearchResult(
        entries=sorted_entries,
        truncated=result.truncated,
        query=query,
        max_results=result.max_results,
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
