"""ASP.NET forms-auth client for SystemGroup PAC Engineering BoM and report pages."""

from __future__ import annotations

import os
import re
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
        self.session = session or requests.Session()
        self._logged_in = False

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
        """Return (body_text, content_type) for the BoM list page.

        Prefers direct List.aspx (has SgGridView/Telerik rows). Shell.aspx is
        chrome-only on this CRM and yields no ``rgRow`` grid.
        """
        if not self._logged_in:
            self.login()

        def _looks_like_list(body: str, _ctype: str) -> bool:
            lower = (body or "").lower()
            if "_$isthisshellpage" in lower or "isthisshellpage" in lower:
                return False
            return "rgrow" in lower or "mastertableview" in lower or "کد bom" in lower

        return self._fetch_first_ok(
            [
                bom_list_direct_url(self.base_url),
                bom_list_url(self.base_url),
            ],
            "BoM list page",
            prefer_predicate=_looks_like_list,
        )

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
