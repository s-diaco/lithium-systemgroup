"""ASP.NET forms-auth client for SystemGroup PAC Engineering BoM and report pages."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from html import escape as html_escape
from html import unescape
from typing import Iterator
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse, urlunparse

import requests

from .parser import LoginFormFields, SystemGroupParseError, parse_login_form
from .rsa_login import build_hashed_password, unicode_correct_invalid_characters


class SystemGroupClientError(RuntimeError):
    """Network / auth failure talking to SystemGroup CRM."""


DEFAULT_LOGIN_PATH = "/Authentication/Login.aspx"
DEFAULT_BOM_EDIT_PAGE = "PAC/Engineering/BomPages/Edit.aspx"
DEFAULT_BOM_LIST_PAGE = (
    "List.aspx?ComponentName=SystemGroup.PAC.Engineering&EntityName=Bom"
)
# SgGridView EntityListPaging defaults to 50 rows; CRM list often spans many pages.
DEFAULT_BOM_LIST_MAX_PAGES = 500
# CRM ScriptManager enables partial rendering only for real browser UAs.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Shell iframe List.aspx keys server-side ViewState via PageID/TimeStamp.
DEFAULT_BOM_LIST_PAGE_ID = "1"
DEFAULT_BOM_LIST_PAGE_WIDTH = "1050"
DEFAULT_BOM_LIST_PAGE_HEIGHT = "670"
_BOM_LIST_UPDATE_PANEL_ID = "innerUpdatePanel"
# Store report: sales delivery notes with remaining balance (path only; host from env).
DEFAULT_CUSTOMER_ORDERS_REPORT_PATH = (
    "ReportViewer.aspx?ShowParameterPanel=true&ReportPath="
    "%2FCustomerReports%2FStore%2F"
    "%DA%AF%D8%B2%D8%A7%D8%B1%D8%B4+%D8%AE%D9%84%D8%A7%D8%B5%D9%87+"
    "%D8%AE%D8%B1%D9%88%D8%AC%DB%8C+%D9%87%D8%A7%DB%8C+"
    "%D8%AD%D9%88%D8%A7%D9%84%D9%87+%D9%81%D8%B1%D9%88%D8%B4"
    "%28%D8%A8%D8%A7+%D9%85%D8%A7%D9%86%D8%AF%D9%87+%29"
)

_EXPORT_URL_BASE = re.compile(
    r'ExportUrlBase["\']?\s*:\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)

_BOM_ID_IN_URL = re.compile(
    r"(?:[?&]id=|/id/)(\d+)",
    re.IGNORECASE,
)

_HIDDEN_INPUT = re.compile(
    r"<input\b[^>]*\btype\s*=\s*[\"']hidden[\"'][^>]*>",
    re.IGNORECASE,
)
_INPUT_NAME = re.compile(r"\bname\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_INPUT_VALUE = re.compile(r"\bvalue\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
_FORM_ACTION = re.compile(
    r"<form\b[^>]*\baction\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
# EntityListPaging toolbar: PostBackOptions('…Pager', '{0}', …) — not the page combo.
# Live CRM scripts often emit quotes as \u0027 inside $create JSON blobs.
_PAGER_POSTBACK_TARGET = re.compile(
    r"WebForm_PostBackOptions\(\s*(?:\\u0027|[\"'])"
    r"([^\"'\\]+Pager)"
    r"(?:\\u0027|[\"'])\s*,\s*(?:\\u0027|[\"'])\{0\}(?:\\u0027|[\"'])",
    re.IGNORECASE,
)
_PAGE_LABEL = re.compile(r"صفحه\s*(\d+)", re.IGNORECASE)
_CBO_PAGE_VALUE = re.compile(
    r'id="[^"]*cboPage_Input"[^>]*\bvalue="([^"]*)"',
    re.IGNORECASE,
)
_CBO_PAGE_VALUE_ALT = re.compile(
    r'\bname="[^"]*cboPage"[^>]*\bvalue="([^"]*)"',
    re.IGNORECASE,
)
_PAGE_DROPDOWN_ITEM = re.compile(
    r'<li\b[^>]*class="[^"]*rcbItem[^"]*"[^>]*>\s*(صفحه\s*\d+)\s*</li>',
    re.IGNORECASE,
)
_NEXT_BUTTON = re.compile(
    r'<li\b([^>]*)>\s*<a\b[^>]*title="بعدی"',
    re.IGNORECASE,
)
_INPUT_TAG = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_PAGER_ITEM_DATA = re.compile(
    r'"itemData"\s*:\s*(\[[^\]]*\{[^]]*Next[^]]*\}[^\]]*\])',
    re.IGNORECASE,
)
_DELTA_HIDDEN_FIELD = re.compile(
    r"\|(\d+)\|hiddenField\|([^|]*)\|",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BomListPager:
    """Pagination controls from a CRM BoM list (SgGridView EntityListPaging)."""

    action: str
    fields: dict[str, str]
    pager_target: str | None
    current_page: int
    page_count: int
    next_enabled: bool
    # RadToolBar posts the Next item's index (not the string "Next").
    next_argument: str = "3"


def extract_aspnet_form_fields(html: str) -> tuple[str, dict[str, str]]:
    """Return (form action, hidden input map) from the first ASP.NET form."""
    action_match = _FORM_ACTION.search(html or "")
    action = unescape(action_match.group(1)) if action_match else ""
    fields: dict[str, str] = {}
    for tag in _HIDDEN_INPUT.findall(html or ""):
        name_match = _INPUT_NAME.search(tag)
        if not name_match:
            continue
        value_match = _INPUT_VALUE.search(tag)
        fields[name_match.group(1)] = (
            unescape(value_match.group(1)) if value_match else ""
        )
    return action, fields


def extract_aspnet_post_fields(html: str) -> tuple[str, dict[str, str]]:
    """Return (form action, successful control map) like a browser form POST.

    Includes hidden and visible text inputs (cboPage, filters, etc.). Checkboxes /
    radios are only included when marked checked. Submit/button/image/file skipped.
    """
    action_match = _FORM_ACTION.search(html or "")
    action = unescape(action_match.group(1)) if action_match else ""
    fields: dict[str, str] = {}
    for tag in _INPUT_TAG.findall(html or ""):
        name_match = _INPUT_NAME.search(tag)
        if not name_match:
            continue
        type_match = re.search(
            r'\btype\s*=\s*["\']([^"\']+)["\']', tag, re.IGNORECASE
        )
        itype = (type_match.group(1) if type_match else "text").lower()
        if itype in ("submit", "button", "image", "file"):
            continue
        if itype in ("checkbox", "radio") and "checked" not in tag.lower():
            continue
        value_match = _INPUT_VALUE.search(tag)
        fields[name_match.group(1)] = (
            unescape(value_match.group(1)) if value_match else ""
        )
    return action, fields


def strip_clientstate_updated(fields: dict[str, str]) -> dict[str, str]:
    """Drop reserved ``__Updated`` markers from Sg ClientState JSON values."""
    out: dict[str, str] = {}
    for key, value in fields.items():
        if (
            isinstance(value, str)
            and value.startswith("{")
            and '"__Updated"' in value
        ):
            try:
                obj = json.loads(value)
            except json.JSONDecodeError:
                out[key] = value
                continue
            if isinstance(obj, dict) and "__Updated" in obj:
                obj = dict(obj)
                obj.pop("__Updated", None)
                value = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        out[key] = value
    return out


def parse_pager_next_argument(html: str) -> str:
    """Return RadToolBar item index for Next (live CRM posts this, not ``Next``)."""
    match = _PAGER_ITEM_DATA.search(html or "")
    if match:
        raw = match.group(1)
        try:
            items = json.loads(raw.encode("utf-8").decode("unicode_escape"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            try:
                items = json.loads(raw)
            except json.JSONDecodeError:
                items = None
        if isinstance(items, list):
            for index, item in enumerate(items):
                if isinstance(item, dict) and item.get("value") == "Next":
                    return str(index)
    return "3"


def parse_bom_list_pager(html: str) -> BomListPager | None:
    """Parse EntityListPaging state from BoM list HTML, or None if absent."""
    if not html:
        return None
    has_pager_chrome = (
        "EntityListPaging" in html
        or "cboPage" in html
        or _PAGER_POSTBACK_TARGET.search(html) is not None
    )
    if not has_pager_chrome:
        return None

    action, fields = extract_aspnet_post_fields(html)
    pager_target: str | None = None
    for match in _PAGER_POSTBACK_TARGET.finditer(html):
        target = match.group(1)
        if "cboPage" in target:
            continue
        pager_target = target
        break

    page_numbers = [
        int(m.group(1))
        for m in _PAGE_LABEL.finditer(" ".join(_PAGE_DROPDOWN_ITEM.findall(html)))
    ]
    if not page_numbers:
        # Fallback: any صفحهN labels (combo input + dropdown).
        page_numbers = [int(m.group(1)) for m in _PAGE_LABEL.finditer(html)]
    page_count = max(page_numbers) if page_numbers else 1

    current_raw = ""
    for pattern in (_CBO_PAGE_VALUE, _CBO_PAGE_VALUE_ALT):
        match = pattern.search(html)
        if match:
            current_raw = match.group(1)
            break
    current_match = _PAGE_LABEL.search(current_raw) if current_raw else None
    current_page = int(current_match.group(1)) if current_match else 1

    next_enabled = False
    next_match = _NEXT_BUTTON.search(html)
    if next_match:
        next_enabled = "rtbDisabled" not in (next_match.group(1) or "")
    elif pager_target and current_page < page_count:
        next_enabled = True

    if not pager_target and page_count <= 1:
        return None

    return BomListPager(
        action=action,
        fields=fields,
        pager_target=pager_target,
        current_page=current_page,
        page_count=max(page_count, current_page, 1),
        next_enabled=next_enabled,
        next_argument=parse_pager_next_argument(html),
    )


def extract_async_update_panel(
    delta: str, panel_id: str = _BOM_LIST_UPDATE_PANEL_ID
) -> str | None:
    """Pull ``updatePanel`` HTML out of an ASP.NET async postback delta."""
    marker = f"|updatePanel|{panel_id}|"
    start = (delta or "").find(marker)
    if start < 0:
        return None
    content_start = start + len(marker)
    # Panel HTML ends where hiddenField nodes begin (CRM List.aspx shape).
    end = delta.find("|0|hiddenField|", content_start)
    if end < 0:
        end = delta.find("|hiddenField|", content_start)
    if end < 0:
        return delta[content_start:]
    return delta[content_start:end]


def extract_async_hidden_fields(delta: str) -> dict[str, str]:
    """Parse ``hiddenField`` nodes from an ASP.NET async postback delta."""
    fields: dict[str, str] = {}
    text = delta or ""
    for match in _DELTA_HIDDEN_FIELD.finditer(text):
        length = int(match.group(1))
        name = match.group(2)
        content_start = match.end()
        fields[name] = text[content_start : content_start + length]
    return fields


_PAGER_SCRIPT = re.compile(
    r"<script\b[^>]*>[\s\S]*?PostBackOptions\([\s\S]*?Pager[\s\S]*?</script>",
    re.IGNORECASE,
)


def apply_bom_list_async_delta(html: str, delta: str) -> str:
    """Merge an async Next delta into list HTML for the next postback round."""
    panel = extract_async_update_panel(delta)
    if not panel:
        raise SystemGroupClientError(
            "BoM list Next postback returned no updatePanel delta."
        )
    action, fields = extract_aspnet_post_fields(html)
    fields.update(extract_async_hidden_fields(delta))
    fields.update(extract_aspnet_post_fields(panel)[1])
    # Drop event target leftovers from the previous post.
    fields["__EVENTTARGET"] = ""
    fields["__EVENTARGUMENT"] = ""
    fields.pop("__ASYNCPOST", None)
    if "scriptManager" in fields and "|" in (fields.get("scriptManager") or ""):
        fields["scriptManager"] = ""
    hidden = "".join(
        f'<input type="hidden" name="{html_escape(name, quote=True)}" '
        f'value="{html_escape(value, quote=True)}" />'
        for name, value in fields.items()
    )
    # updatePanel HTML omits the pager $create blob; keep it from the prior page
    # so parse_bom_list_pager still finds pager_target / Next item index.
    pager_scripts = "".join(_PAGER_SCRIPT.findall(html or ""))
    action_attr = html_escape(action or "", quote=True)
    return (
        f'<form method="post" action="{action_attr}">'
        f"{hidden}{panel}{pager_scripts}</form>"
    )


def extract_bom_id_from_url(url: str) -> str | None:
    """Pull BoM id from a Shell/Edit CRM URL (handles nested page= query)."""
    if not url or not str(url).strip():
        return None
    raw = str(url).strip()
    # Nested page=PAC/...Edit.aspx?id=N may be percent-encoded once or twice.
    candidates = [raw, unquote(raw), unquote(unquote(raw))]
    for candidate in candidates:
        parsed = urlparse(candidate)
        qs = parse_qs(parsed.query)
        if "id" in qs and qs["id"]:
            return qs["id"][0]
        if "page" in qs and qs["page"]:
            page_val = qs["page"][0]
            match = _BOM_ID_IN_URL.search(page_val)
            if match:
                return match.group(1)
        match = _BOM_ID_IN_URL.search(candidate)
        if match:
            return match.group(1)
    return None


def _normalize_base_url(base: str) -> str:
    base = (base or "").strip()
    if base and not base.endswith("/"):
        base += "/"
    return base


def credentials_from_env() -> tuple[str, str, str]:
    """Return (base_url, username, password) from CRM_* env vars.

    Falls back to REPORT_* when CRM_* is unset (same host/credentials overlap).
    """
    base = (os.environ.get("CRM_BASE_URL") or os.environ.get("REPORT_BASE_URL") or "").strip()
    user = (os.environ.get("CRM_USERNAME") or os.environ.get("REPORT_USERNAME") or "").strip()
    password = os.environ.get("CRM_PASSWORD") or os.environ.get("REPORT_PASSWORD") or ""
    missing = []
    if not base:
        missing.append("CRM_BASE_URL (or REPORT_BASE_URL)")
    if not user:
        missing.append("CRM_USERNAME (or REPORT_USERNAME)")
    if not password:
        missing.append("CRM_PASSWORD (or REPORT_PASSWORD)")
    if missing:
        raise SystemGroupClientError(
            "Missing required env vars: "
            + ", ".join(missing)
            + ". Set them in the environment or a gitignored env file; never commit secrets."
        )
    return _normalize_base_url(base), user, password


def report_credentials_from_env() -> tuple[str, str, str]:
    """Return (base_url, username, password) from REPORT_* env vars.

    Falls back to CRM_* when REPORT_* is unset.
    """
    base = (os.environ.get("REPORT_BASE_URL") or os.environ.get("CRM_BASE_URL") or "").strip()
    user = (os.environ.get("REPORT_USERNAME") or os.environ.get("CRM_USERNAME") or "").strip()
    password = os.environ.get("REPORT_PASSWORD") or os.environ.get("CRM_PASSWORD") or ""
    missing = []
    if not base:
        missing.append("REPORT_BASE_URL (or CRM_BASE_URL)")
    if not user:
        missing.append("REPORT_USERNAME (or CRM_USERNAME)")
    if not password:
        missing.append("REPORT_PASSWORD (or CRM_PASSWORD)")
    if missing:
        raise SystemGroupClientError(
            "Missing required env vars: "
            + ", ".join(missing)
            + ". Set them in the environment or a gitignored env file; never commit secrets."
        )
    return _normalize_base_url(base), user, password


def customer_orders_report_path_from_env() -> str:
    """Report page path (no host) from REPORT_ORDERS_PATH, else package default."""
    path = (os.environ.get("REPORT_ORDERS_PATH") or "").strip()
    return path.lstrip("/") if path else DEFAULT_CUSTOMER_ORDERS_REPORT_PATH


def customer_orders_report_url(
    base_url: str, report_path: str | None = None
) -> str:
    """Build absolute ReportViewer URL under base_url."""
    path = (report_path or customer_orders_report_path_from_env()).lstrip("/")
    return urljoin(_normalize_base_url(base_url), path)


def extract_export_url_base(html: str) -> str | None:
    """Pull ReportViewer ``ExportUrlBase`` (ends before Format= value)."""
    match = _EXPORT_URL_BASE.search(html or "")
    if not match:
        return None
    # JS string may contain \\u0026 for &
    raw = match.group(1)
    try:
        return raw.encode("utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        return raw.replace("\\u0026", "&").replace("\\/", "/")


def bom_edit_url(base_url: str, bom_id: str | int) -> str:
    """Build Shell.aspx?page=...Edit.aspx?id=<id> under base_url."""
    page = f"{DEFAULT_BOM_EDIT_PAGE}?id={bom_id}"
    # Shell wraps the real page; some installs also accept the page URL directly.
    query = urlencode({"page": page})
    return urljoin(base_url, f"Shell.aspx?{query}")


def bom_edit_direct_url(base_url: str, bom_id: str | int) -> str:
    return urljoin(base_url, f"{DEFAULT_BOM_EDIT_PAGE}?id={bom_id}")


def bom_list_url(base_url: str) -> str:
    """Build Shell.aspx?page=List.aspx?ComponentName=…&EntityName=Bom."""
    query = urlencode({"page": DEFAULT_BOM_LIST_PAGE})
    return urljoin(base_url, f"Shell.aspx?{query}")


def bom_list_direct_url(base_url: str) -> str:
    return urljoin(base_url, DEFAULT_BOM_LIST_PAGE)


def bom_list_frame_url(
    base_url: str,
    *,
    page_id: str = DEFAULT_BOM_LIST_PAGE_ID,
    timestamp_ms: int | None = None,
    page_width: str = DEFAULT_BOM_LIST_PAGE_WIDTH,
    page_height: str = DEFAULT_BOM_LIST_PAGE_HEIGHT,
) -> str:
    """List.aspx URL as loaded inside Shell (PageID/TimeStamp key ViewState)."""
    ts = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    return urljoin(
        base_url,
        f"{DEFAULT_BOM_LIST_PAGE}"
        f"&PageID={page_id}&TimeStamp={ts}"
        f"&PageWidth={page_width}&PageHeight={page_height}",
    )


class SystemGroupClient:
    """Session-backed forms-auth client."""

    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 60.0,
        *,
        prefer_report_env: bool = False,
    ) -> None:
        if base_url is None or username is None or password is None:
            loader = report_credentials_from_env if prefer_report_env else credentials_from_env
            env_base, env_user, env_pass = loader()
            base_url = base_url or env_base
            username = username or env_user
            password = password if password is not None else env_pass
        self.base_url = _normalize_base_url(base_url or "")
        self.username = username
        self.password = password
        self.timeout = timeout
        owned_session = session is None
        self.session = session or requests.Session()
        # requests' default UA makes CRM ScriptManager set
        # SupportsPartialRendering=false; use a browser UA for owned sessions.
        if owned_session:
            self.session.headers["User-Agent"] = DEFAULT_USER_AGENT
        self._logged_in = False
        self._bom_list_frame_url: str | None = None

    def login_url(self) -> str:
        return urljoin(self.base_url, DEFAULT_LOGIN_PATH.lstrip("/"))

    def build_login_payload(self, form: LoginFormFields) -> dict[str, str]:
        """Build POST body matching Login.aspx / beforeLogin() when RSA fields exist."""
        payload = dict(form.hidden)
        username = unicode_correct_invalid_characters(self.username) or ""
        password = unicode_correct_invalid_characters(self.password) or ""
        payload[form.username_name] = username

        if form.uses_rsa_password:
            # Login.js: clear txtPassword; set hashedPassword = encrypt(sessionid + "--" + password)
            payload[form.password_name] = ""
            try:
                payload[form.hashed_password_name] = build_hashed_password(
                    password,
                    session_id=form.session_id or "",
                    exponent_hex=form.rsa_exponent_hex or "",
                    modulus_hex=form.rsa_modulus_hex or "",
                )
            except (ValueError, TypeError) as exc:
                raise SystemGroupClientError(
                    f"Failed to build RSA hashedPassword: {exc}"
                ) from exc
            # beforeLogin clicks #btnHidSubmit (not the visible btnSubmit)
            if form.hid_submit_name:
                payload[form.hid_submit_name] = form.hid_submit_value or ""
                if form.submit_name and form.submit_name != form.hid_submit_name:
                    payload.pop(form.submit_name, None)
            elif form.submit_name:
                payload[form.submit_name] = form.submit_value or "Login"
        else:
            payload[form.password_name] = password
            if form.submit_name:
                payload[form.submit_name] = form.submit_value or "Login"
        return payload

    def login(self, login_html: str | None = None) -> None:
        """GET login page (unless login_html provided), POST credentials, keep cookies."""
        login_url = self.login_url()
        if login_html is None:
            try:
                resp = self.session.get(login_url, timeout=self.timeout)
                resp.raise_for_status()
            except requests.RequestException as exc:
                raise SystemGroupClientError(
                    "Could not reach the SystemGroup CRM login page "
                    f"({login_url}): {exc}"
                ) from exc
            login_html = resp.text

        try:
            form = parse_login_form(login_html)
        except SystemGroupParseError as exc:
            raise SystemGroupClientError(
                f"CRM login page was not usable: {exc}"
            ) from exc
        action_url = urljoin(login_url, form.action) if form.action else login_url
        payload = self.build_login_payload(form)
        try:
            resp = self.session.post(
                action_url,
                data=payload,
                timeout=self.timeout,
                allow_redirects=True,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise SystemGroupClientError(
                f"Could not complete SystemGroup CRM login ({action_url}): {exc}"
            ) from exc

        # Heuristic: still on login page with password field → failed auth
        lower = resp.text.lower()
        if "login.aspx" in (resp.url or "").lower() and 'type="password"' in lower:
            raise SystemGroupClientError(
                "Login appears to have failed (still on Login.aspx). "
                "Check CRM_USERNAME / CRM_PASSWORD."
            )
        self._logged_in = True

    def _fetch_first_ok(
        self,
        urls: list[str],
        label: str,
        *,
        prefer_predicate=None,
    ) -> tuple[str, str]:
        """GET urls in order; return the first usable response.

        If ``prefer_predicate(body, ctype)`` is set, keep scanning after a
        successful GET that fails the predicate (e.g. Shell chrome with no
        BoM grid) and only fall back to that body if nothing better is found.
        """
        errors: list[str] = []
        fallback: tuple[str, str] | None = None
        for url in urls:
            try:
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                resp.raise_for_status()
            except requests.RequestException as exc:
                errors.append(f"{url}: {exc}")
                continue
            if "login.aspx" in (resp.url or "").lower():
                errors.append(f"{url}: redirected to login")
                continue
            ctype = resp.headers.get("Content-Type", "text/html")
            body = resp.text
            if prefer_predicate is not None and not prefer_predicate(body, ctype):
                errors.append(f"{url}: response did not look like usable {label}")
                if fallback is None:
                    fallback = (body, ctype)
                continue
            return body, ctype
        if fallback is not None:
            return fallback
        raise SystemGroupClientError(
            f"Could not fetch {label}. Tried:\n" + "\n".join(errors)
        )

    def fetch_bom(self, bom_id: str | int) -> tuple[str, str]:
        """Return (body_text, content_type) for the BoM Edit page.

        Prefers direct Edit.aspx. Shell.aspx is chrome-only on this CRM
        (no BoM line tables) — same pattern as the BoM list.
        """
        if not self._logged_in:
            self.login()

        def _looks_like_edit(body: str, _ctype: str) -> bool:
            lower = (body or "").lower()
            # Shell sets this; Edit pages contain grids / line markup.
            if "_$isthisshellpage" in lower or "isthisshellpage" in lower:
                return False
            return (
                "<table" in lower
                or "rgrow" in lower
                or "bomlines" in lower
                or "mastertableview" in lower
            )

        return self._fetch_first_ok(
            [
                bom_edit_direct_url(self.base_url, bom_id),
                bom_edit_url(self.base_url, bom_id),
            ],
            "BoM Edit page",
            prefer_predicate=_looks_like_edit,
        )

    def fetch_bom_list(self) -> tuple[str, str]:
        """Return (body_text, content_type) for the first BoM list page.

        Prefers the Shell-framed List.aspx URL (PageID/TimeStamp) so pager
        postbacks keep a valid server ViewState. Falls back to bare List.aspx
        for one-shot row reads when the framed URL is unavailable.

        For the full catalog (every pager page), use ``iter_bom_list_pages``.
        """
        if not self._logged_in:
            self.login()

        def _looks_like_list(body: str, _ctype: str) -> bool:
            lower = (body or "").lower()
            if "_$isthisshellpage" in lower or "isthisshellpage" in lower:
                return False
            return "rgrow" in lower or "mastertableview" in lower or "کد bom" in lower

        frame_url = bom_list_frame_url(self.base_url)
        try:
            resp = self.session.get(
                frame_url, timeout=self.timeout, allow_redirects=True
            )
            resp.raise_for_status()
            ctype = resp.headers.get("Content-Type", "text/html")
            if (
                "login.aspx" not in (resp.url or "").lower()
                and _looks_like_list(resp.text, ctype)
            ):
                action, _ = extract_aspnet_form_fields(resp.text)
                if action and "PageID=" in action:
                    self._bom_list_frame_url = urljoin(
                        self.base_url, action.lstrip("./")
                    )
                else:
                    self._bom_list_frame_url = resp.url or frame_url
                return resp.text, ctype
        except requests.RequestException:
            pass

        body, ctype = self._fetch_first_ok(
            [
                bom_list_direct_url(self.base_url),
                bom_list_url(self.base_url),
            ],
            "BoM list page",
            prefer_predicate=_looks_like_list,
        )
        return body, ctype

    def _bom_list_post_url(self, action: str) -> str:
        """Resolve list form action against the CRM base URL."""
        if self._bom_list_frame_url:
            return self._bom_list_frame_url
        if not action:
            return bom_list_frame_url(self.base_url)
        return urljoin(self.base_url, action.lstrip("./"))

    def _post_bom_list_next(
        self, html: str, pager: BomListPager
    ) -> tuple[str, str]:
        """Async-post EntityListPaging Next; return merged next-page HTML."""
        if not pager.pager_target:
            raise SystemGroupClientError(
                "BoM list pager has no postback target for Next."
            )
        _, fields = extract_aspnet_post_fields(html)
        payload = strip_clientstate_updated(fields)
        payload["__EVENTTARGET"] = pager.pager_target
        payload["__EVENTARGUMENT"] = pager.next_argument or "3"
        payload["__ASYNCPOST"] = "true"
        payload["scriptManager"] = (
            f"{_BOM_LIST_UPDATE_PANEL_ID}|{pager.pager_target}"
        )
        url = self._bom_list_post_url(pager.action)
        headers = {
            "Referer": url,
            "X-MicrosoftAjax": "Delta=true",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        try:
            resp = self.session.post(
                url,
                data=payload,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=True,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise SystemGroupClientError(
                f"Could not fetch next BoM list page ({url.split('?', 1)[0]}): {exc}"
            ) from exc
        if "login.aspx" in (resp.url or "").lower():
            raise SystemGroupClientError(
                "BoM list page postback redirected to login; check credentials."
            )
        delta = resp.text
        if "view state is expired" in delta.lower():
            raise SystemGroupClientError(
                "BoM list Next postback failed: page ViewState expired. "
                "Use the Shell-framed List URL (PageID/TimeStamp)."
            )
        if "|error|" in delta or (
            "updatePanel" not in delta and "EntityListPaging" not in delta
        ):
            raise SystemGroupClientError(
                "BoM list Next postback failed "
                f"({url.split('?', 1)[0]}): CRM returned an error delta."
            )
        try:
            merged = apply_bom_list_async_delta(html, delta)
        except SystemGroupClientError:
            # Full HTML response (non-async) — rare but usable.
            if "EntityListPaging" in delta or "rgRow" in delta:
                return delta, resp.headers.get("Content-Type", "text/html")
            raise
        return merged, "text/html"

    def iter_bom_list_pages(
        self, *, max_pages: int | None = None
    ) -> Iterator[tuple[str, str]]:
        """Yield (body, content_type) for every EntityListPaging page.

        Starts with a GET of the Shell-framed first page, then ASP.NET async
        postbacks with the pager toolbar item index for Next until Next is
        disabled, the page index stops advancing, or ``max_pages`` is hit.
        """
        if not self._logged_in:
            self.login()
        limit = DEFAULT_BOM_LIST_MAX_PAGES if max_pages is None else max(1, max_pages)
        body, ctype = self.fetch_bom_list()
        yield body, ctype

        seen_pages: set[int] = set()
        for _ in range(limit - 1):
            pager = parse_bom_list_pager(body)
            if pager is None or not pager.next_enabled or not pager.pager_target:
                break
            if pager.current_page in seen_pages:
                break
            seen_pages.add(pager.current_page)
            if pager.current_page >= pager.page_count:
                break
            body, ctype = self._post_bom_list_next(body, pager)
            next_pager = parse_bom_list_pager(body)
            if next_pager is not None and next_pager.current_page == pager.current_page:
                # Postback did not advance — stop to avoid an infinite loop.
                break
            yield body, ctype

    def fetch_customer_orders_report(
        self, report_path: str | None = None
    ) -> tuple[str, str]:
        """GET the customer-orders ReportViewer page; return (html, content_type)."""
        if not self._logged_in:
            self.login()
        url = customer_orders_report_url(self.base_url, report_path)
        try:
            resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise SystemGroupClientError(
                f"Could not fetch customer orders report ({url.split('?', 1)[0]}): {exc}"
            ) from exc
        if "login.aspx" in (resp.url or "").lower():
            raise SystemGroupClientError(
                "Customer orders report redirected to login; check credentials."
            )
        return resp.text, resp.headers.get("Content-Type", "text/html")

    def fetch_customer_orders(
        self,
        report_path: str | None = None,
        *,
        export_format: str = "CSV",
    ) -> tuple[str, str]:
        """Open the store orders report and download an export (default CSV).

        ReportViewer keeps a ``ReportSession`` on first GET; exports use
        ``ExportUrlBase`` + format (CSV / XML / EXCELOPENXML). Shell chrome is
        not required — same preference as direct BoM pages.
        """
        if not self._logged_in:
            self.login()
        html, _ctype = self.fetch_customer_orders_report(report_path)
        export_base = extract_export_url_base(html)
        if not export_base:
            raise SystemGroupClientError(
                "Customer orders report HTML had no ExportUrlBase. "
                "Save ReportViewer HTML and update extract_export_url_base()."
            )
        fmt = (export_format or "CSV").strip().upper() or "CSV"
        export_url = urljoin(self.base_url, export_base.lstrip("/") + fmt)
        try:
            resp = self.session.get(
                export_url, timeout=self.timeout, allow_redirects=True
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise SystemGroupClientError(
                f"Could not export customer orders ({fmt}): {exc}"
            ) from exc
        if "login.aspx" in (resp.url or "").lower():
            raise SystemGroupClientError(
                "Customer orders export redirected to login; check credentials."
            )
        # Decode text exports with BOM; leave binary callers to use content bytes via raw session.
        ctype = resp.headers.get("Content-Type", "application/octet-stream")
        if "csv" in ctype.lower() or fmt == "CSV" or "xml" in ctype.lower() or fmt == "XML":
            text = resp.content.decode("utf-8-sig")
            return text, ctype
        return resp.text, ctype


def rewrite_url_host(url: str, base_url: str) -> str:
    """Utility for tests: swap host of url to match base_url."""
    base = urlparse(base_url)
    parsed = urlparse(url)
    return urlunparse(
        (
            base.scheme or parsed.scheme,
            base.netloc or parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )
