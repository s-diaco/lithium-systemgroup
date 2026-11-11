"""Parse SystemGroup login forms, BoM Edit, and BoM List HTML/JSON.

Edit: prefer ``window.eds_BomMaterials_updatedData`` (PartCode/Quantity);
rgRow cells are often empty. Header via ``nrgNumber`` / ``sltBomPart``.
List: Telerik ``MasterTableView_Header`` + ``rgRow``. Fixtures:
``bom_edit_telerik.html``, ``bom_edit_materials.json``, ``bom_list_telerik.*``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlparse, parse_qs


class SystemGroupParseError(ValueError):
    """Raised when login or BoM markup cannot be parsed."""


@dataclass
class LoginFormFields:
    action: str
    method: str
    hidden: dict[str, str]
    username_name: str
    password_name: str
    submit_name: str | None = None
    submit_value: str | None = None
    # SystemGroup RSA login (Login.js beforeLogin); absent on plain forms.
    rsa_exponent_hex: str | None = None
    rsa_modulus_hex: str | None = None
    session_id: str | None = None
    hashed_password_name: str | None = None
    hid_submit_name: str | None = None
    hid_submit_value: str | None = None

    @property
    def uses_rsa_password(self) -> bool:
        return bool(
            self.rsa_exponent_hex
            and self.rsa_modulus_hex
            and self.session_id is not None
            and self.hashed_password_name
        )


@dataclass
class BomListEntry:
    """One row from the CRM BoM list (List.aspx EntityName=Bom)."""

    bom_id: str
    part_number: str = ""
    description: str = ""
    revision: str = ""
    edit_url: str = ""

    def label(self) -> str:
        bits = [f"id={self.bom_id}"]
        if self.part_number:
            bits.append(self.part_number)
        if self.revision:
            bits.append(f"rev {self.revision}")
        if self.description:
            bits.append(self.description)
        return " — ".join(bits)


@dataclass
class BomLine:
    part_number: str = ""
    quantity: str = "1"
    revision: str = ""
    description: str = ""
    reference: str = ""
    manufacturer_name: str = ""
    manufacturer_part_number: str = ""
    dnp: str = ""
    level: int = 1

    def as_csv_row(self) -> dict[str, str]:
        return {
            "level": str(self.level),
            "part_number": self.part_number.strip(),
            "quantity": self.quantity.strip() or "1",
            "revision": self.revision.strip(),
            "description": self.description.strip() or self.part_number.strip() or "Imported from SystemGroup",
            "reference": self.reference.strip(),
            "manufacturer_name": self.manufacturer_name.strip(),
            "manufacturer_part_number": self.manufacturer_part_number.strip(),
            "dnp": self.dnp.strip(),
        }


@dataclass
class ParsedBom:
    bom_id: str | None = None
    parent_part_number: str = ""
    parent_revision: str = ""
    parent_description: str = ""
    lines: list[BomLine] = field(default_factory=list)
    source: str = "unknown"


# --- Login form ---------------------------------------------------------------

_USER_NAME_HINTS = ("user", "login", "email", "uid", "account")
_PASS_NAME_HINTS = ("pass", "pwd")
_SUBMIT_HINTS = ("login", "submit", "signin", "logon")


class _LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k.lower(): (v or "") for k, v in attrs}
        if tag == "form":
            self._current = {
                "action": ad.get("action", ""),
                "method": (ad.get("method") or "post").lower(),
                "inputs": [],
            }
        elif tag == "input" and self._current is not None:
            self._current["inputs"].append(
                {
                    "type": (ad.get("type") or "text").lower(),
                    "name": ad.get("name", ""),
                    "value": ad.get("value", ""),
                    "id": ad.get("id", ""),
                }
            )

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def parse_login_form(html: str) -> LoginFormFields:
    """Extract ASP.NET forms-auth fields from a Login.aspx HTML body."""
    parser = _LoginFormParser()
    parser.feed(html)
    if not parser.forms:
        raise SystemGroupParseError("No <form> found in login HTML.")

    # Prefer the form that has both a password input and a text/email username.
    chosen = None
    for form in parser.forms:
        types = {i["type"] for i in form["inputs"]}
        if "password" in types and types & {"text", "email", ""}:
            chosen = form
            break
    if chosen is None:
        chosen = parser.forms[0]

    hidden: dict[str, str] = {}
    username_name = ""
    password_name = ""
    submit_name = None
    submit_value = None

    for inp in chosen["inputs"]:
        name = inp["name"]
        if not name:
            continue
        itype = inp["type"]
        if itype == "hidden":
            hidden[name] = inp["value"]
        elif itype == "password" and not password_name:
            password_name = name
        elif itype in ("text", "email") and not username_name:
            lname = name.lower()
            if any(h in lname for h in _USER_NAME_HINTS) or not username_name:
                username_name = name
        elif itype in ("submit", "button", "image"):
            lname = name.lower()
            lval = (inp["value"] or "").lower()
            if any(h in lname or h in lval for h in _SUBMIT_HINTS) or submit_name is None:
                submit_name = name
                submit_value = inp["value"] or "Login"

    # Fallback: first non-hidden text-like input as username
    if not username_name:
        for inp in chosen["inputs"]:
            if inp["type"] in ("text", "email") and inp["name"]:
                username_name = inp["name"]
                break

    if not username_name or not password_name:
        raise SystemGroupParseError(
            "Could not locate username/password fields on login form. "
            "Save Login.aspx HTML and update parse_login_form()."
        )

    # Prefer password-hinted name if we grabbed a non-user text field first
    if not any(h in username_name.lower() for h in _USER_NAME_HINTS):
        for inp in chosen["inputs"]:
            if inp["type"] in ("text", "email") and any(
                h in inp["name"].lower() for h in _USER_NAME_HINTS
            ):
                username_name = inp["name"]
                break

    if password_name and any(h in password_name.lower() for h in _PASS_NAME_HINTS):
        pass  # good
    else:
        for inp in chosen["inputs"]:
            if inp["type"] == "password" and inp["name"]:
                password_name = inp["name"]
                break

    # Prefer hidden programmatic submit used by beforeLogin → btnHidSubmit.click()
    hid_submit_name = None
    hid_submit_value = None
    for inp in chosen["inputs"]:
        name = inp["name"]
        if not name:
            continue
        if name.lower() in ("btnhidsubmit", "hidsubmit") or "hidsubmit" in name.lower():
            hid_submit_name = name
            hid_submit_value = inp["value"] or ""
            break

    rsa_e = hidden.get("rsa_e") or None
    rsa_m = hidden.get("rsa_m") or None
    session_id = hidden.get("sessionid")
    if session_id is None and "sessionid" in {i["name"].lower() for i in chosen["inputs"]}:
        session_id = ""
    hashed_name = None
    for inp in chosen["inputs"]:
        if inp["name"] and inp["name"].lower() == "hashedpassword":
            hashed_name = inp["name"]
            break
    if hashed_name is None and "hashedPassword" in hidden:
        hashed_name = "hashedPassword"

    return LoginFormFields(
        action=chosen["action"],
        method=chosen["method"] or "post",
        hidden=hidden,
        username_name=username_name,
        password_name=password_name,
        submit_name=submit_name,
        submit_value=submit_value,
        rsa_exponent_hex=rsa_e,
        rsa_modulus_hex=rsa_m,
        session_id=session_id,
        hashed_password_name=hashed_name,
        hid_submit_name=hid_submit_name,
        hid_submit_value=hid_submit_value,
    )


# --- BoM document -------------------------------------------------------------

_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "part_number": (
        "part_number",
        "part number",
        "part no",
        "partno",
        "partcode",
        "part_code",
        "code",
        "code_material",
        "item",
        "item code",
        "شماره قطعه",
        "کد کالا",
        "کدکالا",
    ),
    "quantity": (
        "quantity",
        "qty",
        "count",
        "qty.",
        "تعداد",
        "مصرف استاندارد",
        "مصرفاستاندارد",
    ),
    "revision": ("revision", "rev", "rev.", "نسخه"),
    "description": (
        "description",
        "desc",
        "name",
        "name_material",
        "partname",
        "part_name",
        "عنوان کالا",
        "عنوانکالا",
        "شرح",
        "توضیحات",
    ),
    "reference": ("reference", "references", "designator", "designators", "ref", "مرجع"),
    "manufacturer_name": ("manufacturer", "manufacturer_name", "mfr", "mfg", "سازنده"),
    "manufacturer_part_number": (
        "manufacturer_part_number",
        "mpn",
        "mfg_part_number",
        "manufacturer part number",
        "کد سازنده",
    ),
    "dnp": ("dnp", "dnl", "do_not_load", "do not load", "do not populate"),
    "level": ("level", "lvl", "indent"),
}

# Live Edit.aspx embeds materials as: window.eds_BomMaterials_updatedData="[{\"PartCode\":...}]";
_EDS_MATERIALS_UPDATED = re.compile(
    r"window\.eds_BomMaterials_updatedData\s*=\s*\"((?:\\.|[^\"\\])*)\"",
    re.DOTALL,
)
_CRM_DECIMAL = re.compile(r"^(\d+)/(\d+)$")
_BIDI_AND_FMT = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_THOUSAND_SEP = re.compile(r"[, ،]")


def _normalize_header(raw: str) -> str | None:
    key = re.sub(r"\s+", " ", (raw or "").strip().lower())
    key = key.replace("-", " ").replace("_", " ")
    compact = key.replace(" ", "")
    for field_name, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            a = alias.lower().replace("_", " ")
            if key == a or compact == a.replace(" ", ""):
                return field_name
    return None


def normalize_crm_decimal(raw: str) -> str:
    """Normalize CRM quantity text (Persian ``70/000`` decimal slash, bidi marks)."""
    text = _BIDI_AND_FMT.sub("", raw or "").strip()
    if not text:
        return ""
    match = _CRM_DECIMAL.fullmatch(text.replace(",", "").replace("،", ""))
    if match:
        whole, frac = match.group(1), match.group(2).rstrip("0")
        return f"{whole}.{frac}" if frac else whole
    return text


def _line_from_mapping(data: dict[str, Any], default_level: int = 1) -> BomLine:
    normalized: dict[str, str] = {}
    for k, v in data.items():
        field_name = _normalize_header(str(k))
        if field_name:
            normalized[field_name] = "" if v is None else str(v).strip()
    # Explicit CRM materials keys (eds_BomMaterials_updatedData).
    if not normalized.get("part_number"):
        for key in ("PartCode", "partCode", "Part_Code"):
            if data.get(key) not in (None, ""):
                normalized["part_number"] = str(data[key]).strip()
                break
    if not normalized.get("description"):
        for key in ("PartName", "partName", "Title"):
            if data.get(key) not in (None, ""):
                normalized["description"] = str(data[key]).strip()
                break
    if not normalized.get("quantity") and data.get("Quantity") not in (None, ""):
        normalized["quantity"] = str(data["Quantity"]).strip()
    if normalized.get("quantity"):
        normalized["quantity"] = normalize_crm_decimal(normalized["quantity"]) or "1"
    level_raw = normalized.get("level", "")
    try:
        level = int(float(level_raw)) if level_raw else default_level
    except ValueError:
        level = default_level
    return BomLine(
        part_number=normalized.get("part_number", ""),
        quantity=normalized.get("quantity", "1") or "1",
        revision=normalized.get("revision", ""),
        description=normalized.get("description", ""),
        reference=normalized.get("reference", ""),
        manufacturer_name=normalized.get("manufacturer_name", ""),
        manufacturer_part_number=normalized.get("manufacturer_part_number", ""),
        dnp=normalized.get("dnp", ""),
        level=level,
    )


class _BomTableParser(HTMLParser):
    """Collect tables; caller picks the best BoM-looking one.

    Stacked tables so Telerik nested header/filter widgets are not dropped.
    """

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[dict[str, Any]] = []
        self.meta: dict[str, str] = {}
        self._table_stack: list[dict[str, Any]] = []
        self._row_stack: list[list[tuple[str, str]]] = []
        self._in_cell = False
        self._cell_tag = ""
        self._cell_text: list[str] = []
        self._capture_meta_name: str | None = None
        self._meta_buf: list[str] = []

    @property
    def _current_table(self) -> dict[str, Any] | None:
        return self._table_stack[-1] if self._table_stack else None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k.lower(): (v or "") for k, v in attrs}
        if tag == "table":
            self._table_stack.append(
                {
                    "id": ad.get("id", ""),
                    "class": ad.get("class", ""),
                    "rows": [],
                }
            )
        elif tag == "tr" and self._current_table is not None:
            self._row_stack.append([])
        elif (
            tag in ("td", "th")
            and self._current_table is not None
            and self._row_stack
            and not self._in_cell
        ):
            self._in_cell = True
            self._cell_tag = tag
            self._cell_text = []
        elif tag in ("span", "div", "td", "input") and ad.get("data-bom-field"):
            self._capture_meta_name = ad["data-bom-field"]
            self._meta_buf = []
            if tag == "input" and ad.get("value"):
                self.meta[ad["data-bom-field"]] = ad["value"]
                self._capture_meta_name = None

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell:
            text = re.sub(r"\s+", " ", "".join(self._cell_text)).strip()
            if self._row_stack:
                self._row_stack[-1].append((self._cell_tag, text))
            self._in_cell = False
        elif tag == "tr" and self._row_stack:
            cells = self._row_stack.pop()
            if cells and self._current_table is not None:
                self._current_table["rows"].append(cells)
        elif tag == "table" and self._table_stack:
            self.tables.append(self._table_stack.pop())
        if self._capture_meta_name and tag in ("span", "div", "td"):
            self.meta[self._capture_meta_name] = re.sub(
                r"\s+", " ", "".join(self._meta_buf)
            ).strip()
            self._capture_meta_name = None

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_text.append(data)
        if self._capture_meta_name:
            self._meta_buf.append(data)


_JSON_PATTERNS = (
    re.compile(
        r"(?:var\s+|window\.)?(?:bomData|bomLines|BomLines|gridData)\s*=\s*(\{.*?\}|\[.*?\])\s*;",
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(
        r'<script[^>]*type=["\']application/json["\'][^>]*id=["\']bom-data["\'][^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    ),
)


def _parse_bom_json_blob(text: str) -> ParsedBom | None:
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _parsed_bom_from_json(data, source="json")


def _parsed_bom_from_json(data: Any, source: str = "json") -> ParsedBom:
    # Live CRM materials arrays use PartCode/PartName/Quantity (+ BomRef).
    if (
        isinstance(data, list)
        and data
        and isinstance(data[0], dict)
        and any(k in data[0] for k in ("PartCode", "partCode", "PartName"))
    ):
        return _parsed_bom_from_materials(
            [row for row in data if isinstance(row, dict)], source=source
        )

    bom = ParsedBom(source=source)
    lines_raw: list[Any] = []
    if isinstance(data, list):
        lines_raw = data
    elif isinstance(data, dict):
        bom.bom_id = (
            str(data["bom_id"]) if data.get("bom_id") is not None else None
        ) or (str(data["id"]) if data.get("id") is not None else None)
        parent = data.get("parent") or data.get("header") or {}
        if isinstance(parent, dict):
            bom.parent_part_number = str(
                parent.get("part_number")
                or parent.get("partNumber")
                or parent.get("code")
                or ""
            )
            bom.parent_revision = str(parent.get("revision") or parent.get("rev") or "")
            bom.parent_description = str(
                parent.get("description") or parent.get("name") or ""
            )
        else:
            bom.parent_part_number = str(
                data.get("parent_part_number") or data.get("part_number") or ""
            )
            bom.parent_revision = str(data.get("parent_revision") or data.get("revision") or "")
            bom.parent_description = str(
                data.get("parent_description") or data.get("description") or ""
            )
        for key in ("lines", "items", "rows", "data", "BomLines"):
            if isinstance(data.get(key), list):
                lines_raw = data[key]
                break
    for item in lines_raw:
        if not isinstance(item, dict):
            continue
        line = _line_from_mapping(item)
        if line.part_number or line.manufacturer_part_number:
            bom.lines.append(line)
        if not bom.bom_id and item.get("BomRef") not in (None, ""):
            bom.bom_id = normalize_crm_id(str(item["BomRef"]))
    if not bom.lines:
        raise SystemGroupParseError("JSON BoM contained no usable line items.")
    return bom


def _score_table(table: dict[str, Any]) -> int:
    score = 0
    tid = (table.get("id") or "").lower()
    tclass = (table.get("class") or "").lower()
    if "header" in tid:
        return -1
    if "grdmaterials" in tid or "bommaterials" in tid:
        score += 15
    if any(x in tid or x in tclass for x in ("bom", "grid", "lines", "items", "material")):
        score += 5
    rows = table.get("rows") or []
    if not rows:
        return score
    header_cells = [c[1] for c in rows[0]]
    mapped = sum(1 for h in header_cells if _normalize_header(h))
    score += mapped * 2
    if mapped >= 2:
        score += 3
    # Prefer tables that actually have non-empty data cells.
    dataish = 0
    for row in rows[1:]:
        if any((c[1] or "").strip() for c in row):
            dataish += 1
    score += min(dataish, 20)
    score += min(len(rows), 20)
    return score


def _unescape_js_json_string(raw: str) -> Any:
    """Decode a JS string literal that holds JSON (``\\\"`` → ``"``)."""
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw.replace(r"\"", '"').replace(r"\/", "/").replace(r"\\", "\\")


def _materials_from_eds_updated_data(html: str) -> list[dict[str, Any]] | None:
    match = _EDS_MATERIALS_UPDATED.search(html or "")
    if not match:
        return None
    decoded = _unescape_js_json_string(match.group(1))
    if isinstance(decoded, str):
        try:
            decoded = json.loads(decoded)
        except json.JSONDecodeError:
            return None
    if isinstance(decoded, list) and decoded:
        return [row for row in decoded if isinstance(row, dict)]
    return None


def _form_field_map(html: str) -> dict[str, str]:
    """Collect input name/id → value pairs (Edit header controls)."""
    fields: dict[str, str] = {}
    for m in re.finditer(
        r"""<(?:input|textarea)\b([^>]*)>""",
        html or "",
        re.IGNORECASE,
    ):
        attrs = m.group(1)
        name_m = re.search(r"""(?:name|id)\s*=\s*["']([^"']+)["']""", attrs, re.I)
        val_m = re.search(r"""\bvalue\s*=\s*["']([^"']*)["']""", attrs, re.I)
        if name_m and val_m:
            fields[name_m.group(1)] = val_m.group(1)
    return fields


def _apply_edit_header_fields(bom: ParsedBom, html: str) -> None:
    fields = _form_field_map(html)
    part = (fields.get("sltBomPart") or "").strip()
    number = (fields.get("nrgNumber") or "").strip()
    title = (fields.get("txtTitle") or fields.get("txtBomPartName") or "").strip()
    if part and not bom.parent_part_number:
        bom.parent_part_number = part
    if number:
        code_pn, code_rev = split_bom_code(number)
        if code_pn and not bom.parent_part_number:
            bom.parent_part_number = code_pn
        if code_rev and not bom.parent_revision:
            bom.parent_revision = code_rev
        # When Number is PN/rev, prefer PN from Number over bare selector if empty.
        if code_pn and code_rev:
            bom.parent_part_number = code_pn
            bom.parent_revision = code_rev
    if title and not bom.parent_description:
        bom.parent_description = title
    if not bom.bom_id:
        # Prefer BomRef from materials blob later; id= in Edit URL as fallback.
        id_m = re.search(r"[?&]id=(\d+)", html or "", re.I)
        if id_m:
            bom.bom_id = id_m.group(1)


def _parsed_bom_from_materials(
    rows: list[dict[str, Any]], *, source: str
) -> ParsedBom:
    bom = ParsedBom(source=source)
    for item in rows:
        line = _line_from_mapping(item)
        if line.part_number or line.manufacturer_part_number:
            bom.lines.append(line)
        if not bom.bom_id and item.get("BomRef") not in (None, ""):
            bom.bom_id = normalize_crm_id(str(item["BomRef"]))
    if not bom.lines:
        raise SystemGroupParseError("BoM materials JSON contained no usable line items.")
    return bom


def _parse_bom_html(html: str) -> ParsedBom:
    # Prefer live CRM EntityDataSource materials JSON (grid rows are often empty).
    materials = _materials_from_eds_updated_data(html)
    if materials:
        bom = _parsed_bom_from_materials(materials, source="html-eds-materials")
        _apply_edit_header_fields(bom, html)
        return bom

    # Prefer embedded JSON if present (common for ASP.NET grids / SPA shells).
    for pattern in _JSON_PATTERNS:
        match = pattern.search(html)
        if match:
            blob = match.group(1)
            parsed = _parse_bom_json_blob(blob)
            if parsed and parsed.lines:
                parsed.source = "html-embedded-json"
                _apply_edit_header_fields(parsed, html)
                return parsed

    parser = _BomTableParser()
    parser.feed(html)
    if not parser.tables:
        raise SystemGroupParseError(
            "No BoM table or embedded JSON found. Save Edit.aspx HTML and update parser."
        )

    best = max(parser.tables, key=_score_table)
    rows = best["rows"]
    if len(rows) < 2:
        raise SystemGroupParseError("BoM table has no data rows.")

    header_map: list[str | None] = [_normalize_header(c[1]) for c in rows[0]]
    if sum(1 for h in header_map if h) < 2:
        raise SystemGroupParseError(
            "BoM table headers not recognized. Expected columns like "
            "part_number, quantity, description. Update _HEADER_ALIASES or fixture."
        )

    bom = ParsedBom(source="html-table")
    if parser.meta.get("bom_id"):
        bom.bom_id = parser.meta["bom_id"]
    bom.parent_part_number = parser.meta.get("parent_part_number", "")
    bom.parent_revision = parser.meta.get("parent_revision", "")
    bom.parent_description = parser.meta.get("parent_description", "")
    _apply_edit_header_fields(bom, html)

    for row in rows[1:]:
        cells = [c[1] for c in row]
        mapping: dict[str, str] = {}
        for idx, field_name in enumerate(header_map):
            if field_name and idx < len(cells):
                mapping[field_name] = cells[idx]
        line = _line_from_mapping(mapping)
        if line.part_number or line.manufacturer_part_number:
            bom.lines.append(line)

    if not bom.lines:
        raise SystemGroupParseError("BoM table parsed but no line items extracted.")
    return bom


def parse_bom_document(content: str, content_type: str | None = None) -> ParsedBom:
    """Parse a BoM Edit HTML page or JSON payload into ParsedBom."""
    stripped = (content or "").strip()
    if not stripped:
        raise SystemGroupParseError("Empty BoM document.")

    ctype = (content_type or "").lower()
    looks_json = stripped[0] in "{[" or "json" in ctype
    if looks_json:
        try:
            return _parsed_bom_from_json(json.loads(stripped), source="json")
        except json.JSONDecodeError:
            if "json" in ctype:
                raise SystemGroupParseError("Invalid BoM JSON.") from None
            # fall through to HTML

    return _parse_bom_html(stripped)


# --- BoM list -----------------------------------------------------------------

_LIST_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "bom_id": ("id", "bom id", "bom_id", "bomid", "entity id", "شناسه"),
    # CRM list column «کد BOM» holds parent_part_number/revision (e.g. 500-9001-00/1).
    "bom_code": (
        "bom code",
        "bom_code",
        "number",  # clientDataKeyNames / JSON field for «کد BOM»
        "کد bom",
        "کدbom",
        "code bom",
    ),
    "part_number": (
        "part_number",
        "part number",
        "part no",
        "partno",
        "code",
        "item",
        "item code",
        "شماره قطعه",
    ),
    # «عنوان BOM» is the list title; do not map bare «title» alone before this.
    "description": (
        "description",
        "desc",
        "name",
        "title",
        "bom title",
        "عنوان bom",
        "عنوانbom",
        "شرح",
        "توضیحات",
    ),
    "revision": ("revision", "rev", "rev.", "نسخه"),
    "edit_url": ("edit", "url", "link", "edit url", "ویرایش"),
}

# Telerik/ASP.NET often wraps digits with LTR marks and thousand separators.
_RG_HEADER_TH = re.compile(
    r"<th\b[^>]*\brgHeader\b[^>]*>(.*?)</th>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")

_BOM_ID_IN_HREF = re.compile(
    r"(?:BomPages/Edit\.aspx\?id=|[?&]id=)(\d+)",
    re.IGNORECASE,
)


def _normalize_list_header(raw: str) -> str | None:
    key = re.sub(r"\s+", " ", (raw or "").strip().lower())
    key = key.replace("-", " ").replace("_", " ")
    compact = key.replace(" ", "")
    for field_name, aliases in _LIST_HEADER_ALIASES.items():
        for alias in aliases:
            a = alias.lower().replace("_", " ")
            if key == a or compact == a.replace(" ", ""):
                return field_name
    return None


def normalize_crm_id(raw: str) -> str:
    """Strip bidi marks / thousand separators from a CRM شناسه cell (e.g. ‎39,485‏)."""
    text = _BIDI_AND_FMT.sub("", raw or "")
    text = _THOUSAND_SEP.sub("", text)
    return text.strip()


def _strip_tags(html_fragment: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", html_fragment or "")).strip()


def _headers_from_rg_header_ths(html: str) -> list[str | None]:
    """Extract column map from Telerik ``th.rgHeader`` cells (static header table)."""
    headers: list[str | None] = []
    for match in _RG_HEADER_TH.finditer(html or ""):
        headers.append(_normalize_list_header(_strip_tags(match.group(1))))
    return headers


def _bom_id_from_href(href: str) -> str | None:
    if not href:
        return None
    candidates = [href, unquote(href), unquote(unquote(href))]
    for candidate in candidates:
        match = _BOM_ID_IN_HREF.search(candidate)
        if match:
            return match.group(1)
        parsed = urlparse(candidate)
        qs = parse_qs(parsed.query)
        if "id" in qs and qs["id"] and str(qs["id"][0]).isdigit():
            return str(qs["id"][0])
        if "page" in qs and qs["page"]:
            match = _BOM_ID_IN_HREF.search(qs["page"][0])
            if match:
                return match.group(1)
    return None


class _BomListTableParser(HTMLParser):
    """Collect list tables and anchor hrefs that look like BoM Edit links.

    Uses a stack so Telerik nested tables (static header + filter widgets)
    are not dropped when an inner ``<table>`` opens.
    """

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[dict[str, Any]] = []
        self.orphan_links: list[tuple[str, str]] = []  # (href, text)
        self._table_stack: list[dict[str, Any]] = []
        self._row_stack: list[dict[str, Any]] = []
        self._in_cell = False
        self._cell_tag = ""
        self._cell_text: list[str] = []
        self._cell_href: str | None = None
        self._in_a = False
        self._a_href = ""
        self._a_text: list[str] = []

    @property
    def _current_table(self) -> dict[str, Any] | None:
        return self._table_stack[-1] if self._table_stack else None

    @property
    def _current_row(self) -> dict[str, Any] | None:
        return self._row_stack[-1] if self._row_stack else None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k.lower(): (v or "") for k, v in attrs}
        if tag == "table":
            self._table_stack.append(
                {
                    "id": ad.get("id", ""),
                    "class": ad.get("class", ""),
                    "rows": [],
                }
            )
        elif tag == "tr" and self._current_table is not None:
            self._row_stack.append(
                {
                    "attrs": {
                        k: v
                        for k, v in ad.items()
                        if k.startswith("data-") or k in ("id", "class")
                    },
                    "cells": [],
                }
            )
        elif (
            tag in ("td", "th")
            and self._current_table is not None
            and self._current_row is not None
            and not self._in_cell
        ):
            self._in_cell = True
            self._cell_tag = tag
            self._cell_text = []
            self._cell_href = None
        elif tag == "a":
            self._in_a = True
            self._a_href = ad.get("href", "")
            self._a_text = []
            if self._in_cell and self._a_href:
                self._cell_href = self._a_href

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_a:
            text = re.sub(r"\s+", " ", "".join(self._a_text)).strip()
            if self._a_href and _bom_id_from_href(self._a_href):
                if not self._table_stack:
                    self.orphan_links.append((self._a_href, text))
            self._in_a = False
        elif tag in ("td", "th") and self._in_cell:
            text = re.sub(r"\s+", " ", "".join(self._cell_text)).strip()
            row = self._current_row
            if row is not None:
                row["cells"].append(
                    {
                        "tag": self._cell_tag,
                        "text": text,
                        "href": self._cell_href or "",
                    }
                )
            self._in_cell = False
        elif tag == "tr" and self._row_stack:
            row = self._row_stack.pop()
            table = self._current_table
            if table is not None and row["cells"]:
                table["rows"].append(
                    {"attrs": dict(row["attrs"]), "cells": list(row["cells"])}
                )
        elif tag == "table" and self._table_stack:
            finished = self._table_stack.pop()
            self.tables.append(finished)

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_text.append(data)
        if self._in_a:
            self._a_text.append(data)


def _row_class(row: dict[str, Any]) -> str:
    attrs = row.get("attrs") or {}
    return (attrs.get("class") or "").lower()


def _is_telerik_data_row(row: dict[str, Any]) -> bool:
    cls = _row_class(row)
    return "rgrow" in cls.replace(" ", "") or "rgaltrow" in cls.replace(" ", "")


def _header_map_from_row(row: dict[str, Any]) -> list[str | None]:
    return [_normalize_list_header(c["text"]) for c in row.get("cells") or []]


def _best_header_map(
    tables: list[dict[str, Any]], html: str, data_col_count: int
) -> list[str | None]:
    """Pick a column map for Telerik grids (static header table or rgHeader ths)."""
    best: list[str | None] = []
    best_mapped = 0
    for table in tables:
        tid = (table.get("id") or "").lower()
        for row in table.get("rows") or []:
            header_map = _header_map_from_row(row)
            mapped = sum(1 for h in header_map if h)
            if mapped < 2:
                continue
            # Prefer Header sibling / same width as the data table.
            score = mapped
            if "header" in tid:
                score += 5
            if data_col_count and len(header_map) == data_col_count:
                score += 5
            if score > best_mapped or (
                score == best_mapped and len(header_map) > len(best)
            ):
                best_mapped = score
                best = header_map
    if best_mapped >= 2:
        return best
    rg_headers = _headers_from_rg_header_ths(html)
    if sum(1 for h in rg_headers if h) >= 2:
        return rg_headers
    return best


def _score_list_table(table: dict[str, Any]) -> int:
    score = 0
    tid = (table.get("id") or "").lower()
    tclass = (table.get("class") or "").lower()
    if "header" in tid:
        return -1  # never treat the static header table as the data grid
    if any(x in tid or x in tclass for x in ("bom", "list", "grid", "entity", "master")):
        score += 5
    if "mastertableview" in tid and "header" not in tid:
        score += 10
    rows = table.get("rows") or []
    if not rows:
        return score
    rg_rows = sum(1 for r in rows if _is_telerik_data_row(r))
    if rg_rows:
        score += 20 + min(rg_rows, 40)
    header_cells = [c["text"] for c in rows[0]["cells"]]
    mapped = sum(1 for h in header_cells if _normalize_list_header(h))
    score += mapped * 2
    if mapped >= 2:
        score += 3
    # Prefer tables that contain Edit.aspx links
    for row in rows[1:]:
        for cell in row["cells"]:
            if _bom_id_from_href(cell.get("href") or ""):
                score += 2
                break
    # Prefer tables whose cells look like CRM ids (‎39,485‏)
    idish = 0
    for row in rows:
        cells = row.get("cells") or []
        if cells and re.fullmatch(r"\d+", normalize_crm_id(cells[0].get("text") or "")):
            idish += 1
    score += min(idish, 30)
    score += min(len(rows), 30)
    return score


def split_bom_code(raw: str) -> tuple[str, str]:
    """Split CRM «کد BOM» (field ``Number``) → (part_number, revision).

    When a ``/`` is present (e.g. ``1155F229/A2*10-C``), the left side is the
    part number and the remainder is revision. Values without a slash
    (e.g. ``CGM5883``, ``2183S914*2-C``) are the whole part number with an
    empty revision.
    """
    text = (raw or "").strip()
    if not text:
        return "", ""
    if "/" not in text:
        return text, ""
    pn, rev = text.split("/", 1)
    return pn.strip(), rev.strip()


def _entry_from_list_mapping(data: dict[str, Any]) -> BomListEntry | None:
    bom_id = normalize_crm_id(
        str(data.get("bom_id") or data.get("id") or data.get("ID") or "")
    )
    edit_url = str(data.get("edit_url") or data.get("url") or data.get("href") or "").strip()
    if not bom_id and edit_url:
        bom_id = _bom_id_from_href(edit_url) or ""
    if not bom_id:
        return None
    if not bom_id.isdigit():
        # Reject non-id leftovers; CRM ids are numeric.
        bom_id = normalize_crm_id(bom_id)
        if not bom_id.isdigit():
            return None

    part_number = str(
        data.get("part_number")
        or data.get("partNumber")
        or data.get("code")
        or ""
    ).strip()
    revision = str(data.get("revision") or data.get("rev") or "").strip()

    bom_code = str(
        data.get("bom_code")
        or data.get("bomCode")
        or data.get("Number")
        or data.get("کد BOM")
        or data.get("کد bom")
        or ""
    ).strip()
    if bom_code:
        code_pn, code_rev = split_bom_code(bom_code)
        # «کد BOM» / Number is the CRM canonical PN[/rev]; prefer it over PartCode.
        if code_pn:
            part_number = code_pn
        if code_rev:
            revision = code_rev
        elif not revision:
            revision = ""

    description = str(
        data.get("description")
        or data.get("name")
        or data.get("title")
        or data.get("Title")
        or ""
    ).strip()

    return BomListEntry(
        bom_id=bom_id,
        part_number=part_number,
        description=description,
        revision=revision,
        edit_url=edit_url,
    )


def _parsed_bom_list_from_json(data: Any) -> list[BomListEntry]:
    rows_raw: list[Any] = []
    if isinstance(data, list):
        rows_raw = data
    elif isinstance(data, dict):
        for key in ("rows", "items", "data", "BomList", "results"):
            if isinstance(data.get(key), list):
                rows_raw = data[key]
                break
        if not rows_raw and any(k in data for k in ("id", "bom_id", "part_number")):
            rows_raw = [data]
    entries: list[BomListEntry] = []
    for item in rows_raw:
        if not isinstance(item, dict):
            continue
        # Normalize alias keys through list header map where useful
        mapped: dict[str, Any] = {}
        for k, v in item.items():
            field_name = _normalize_list_header(str(k))
            if field_name:
                mapped[field_name] = v
            else:
                mapped[str(k)] = v
        entry = _entry_from_list_mapping({**item, **mapped})
        if entry:
            entries.append(entry)
    if not entries:
        raise SystemGroupParseError("BoM list JSON contained no usable rows.")
    return entries


def _iter_list_data_rows(table: dict[str, Any]) -> list[dict[str, Any]]:
    rows = table.get("rows") or []
    telerik = [r for r in rows if _is_telerik_data_row(r)]
    if telerik:
        return telerik
    # Simple fixture tables: skip the header row when it maps.
    if len(rows) >= 2 and sum(1 for h in _header_map_from_row(rows[0]) if h) >= 1:
        return rows[1:]
    return [
        r
        for r in rows
        if any(normalize_crm_id(c.get("text") or "").isdigit() for c in r.get("cells") or [])
    ]


def _parse_bom_list_html(html: str) -> list[BomListEntry]:
    # Prefer embedded JSON blobs shaped like list payloads.
    for pattern in _JSON_PATTERNS:
        match = pattern.search(html)
        if match:
            try:
                data = json.loads(match.group(1))
                return _parsed_bom_list_from_json(data)
            except (json.JSONDecodeError, SystemGroupParseError):
                pass

    parser = _BomListTableParser()
    parser.feed(html)
    entries: list[BomListEntry] = []

    if parser.tables:
        best = max(parser.tables, key=_score_list_table)
        data_rows = _iter_list_data_rows(best)
        sample_cols = len(data_rows[0]["cells"]) if data_rows else 0
        header_map = _best_header_map(parser.tables, html, sample_cols)
        if sum(1 for h in header_map if h) < 2 and best["rows"]:
            # Fall back to first row of the chosen table (simple fixtures).
            header_map = _header_map_from_row(best["rows"][0])

        for row in data_rows:
            cells = row["cells"]
            mapping: dict[str, str] = {}
            edit_href = ""
            for idx, field_name in enumerate(header_map):
                if idx >= len(cells):
                    break
                cell = cells[idx]
                if field_name:
                    mapping[field_name] = cell["text"]
                if cell.get("href") and _bom_id_from_href(cell["href"]):
                    edit_href = cell["href"]
            if not edit_href:
                for cell in cells:
                    if cell.get("href") and _bom_id_from_href(cell["href"]):
                        edit_href = cell["href"]
                        break
            if edit_href:
                mapping.setdefault("edit_url", edit_href)
                mapping.setdefault("bom_id", _bom_id_from_href(edit_href) or "")
            attrs = row.get("attrs") or {}
            if attrs.get("data-bom-id"):
                mapping.setdefault("bom_id", attrs["data-bom-id"])
            if not mapping.get("bom_id"):
                for cell in cells:
                    cand = normalize_crm_id(cell.get("text") or "")
                    if cand.isdigit():
                        mapping["bom_id"] = cand
                        break
            entry = _entry_from_list_mapping(mapping)
            if entry:
                entries.append(entry)

    if not entries and parser.orphan_links:
        for href, text in parser.orphan_links:
            bom_id = _bom_id_from_href(href)
            if bom_id:
                entries.append(
                    BomListEntry(bom_id=bom_id, description=text, edit_url=href)
                )

    if not entries:
        raise SystemGroupParseError(
            "No BoM list rows found. Save List.aspx HTML (EntityName=Bom) and "
            "update parse_bom_list_document()."
        )
    return entries


def parse_bom_list_document(
    content: str, content_type: str | None = None
) -> list[BomListEntry]:
    """Parse a CRM BoM list HTML page or JSON payload into BomListEntry rows."""
    stripped = (content or "").strip()
    if not stripped:
        raise SystemGroupParseError("Empty BoM list document.")

    ctype = (content_type or "").lower()
    looks_json = stripped[0] in "{[" or "json" in ctype
    if looks_json:
        try:
            return _parsed_bom_list_from_json(json.loads(stripped))
        except json.JSONDecodeError:
            if "json" in ctype:
                raise SystemGroupParseError("Invalid BoM list JSON.") from None

    return _parse_bom_list_html(stripped)


def match_bom_list_entries(
    entries: list[BomListEntry],
    *,
    part_number: str | None = None,
    description: str | None = None,
    revision: str | None = None,
) -> list[BomListEntry]:
    """Match list rows by parent part number (from «کد BOM» / ``Number``).

    Preference:
    1. Exact part-number matches (case-insensitive) — the PN side of
       ``Number`` when it contains ``/``, else the whole ``Number``.
    2. If the query looks like a full ``Number`` (``PN/rev``), match that
       exact PN+revision pair.
    3. If multiple and ``revision`` is set, narrow by exact revision.
    4. Else if multiple and ``description`` is set, narrow by exact description.
    5. If no PN hits, fall back to exact description matches.
    """
    raw_pn = (part_number or "").strip()
    pn = raw_pn.casefold()
    desc = (description or "").strip().casefold()
    rev = (revision or "").strip().casefold()
    if not pn and not desc:
        return list(entries)

    # Allow callers to pass a full «کد BOM» value (e.g. 1155F229/A2*10-C).
    query_pn, query_rev = split_bom_code(raw_pn)
    if query_rev and not rev:
        pn = query_pn.casefold()
        rev = query_rev.casefold()

    pn_hits = [
        e
        for e in entries
        if pn and (e.part_number or "").strip().casefold() == pn
    ]
    if pn_hits:
        if rev and len(pn_hits) > 1:
            by_rev = [
                e
                for e in pn_hits
                if (e.revision or "").strip().casefold() == rev
            ]
            if by_rev:
                return by_rev
        if desc and len(pn_hits) > 1:
            narrowed = [
                e
                for e in pn_hits
                if (e.description or "").strip().casefold() == desc
            ]
            if narrowed:
                return narrowed
        return pn_hits

    if desc:
        return [
            e
            for e in entries
            if (e.description or "").strip().casefold() == desc
        ]
    return []

