"""ASP.NET forms-auth client for SystemGroup PAC Engineering BoM pages."""

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


def credentials_from_env() -> tuple[str, str, str]:
    """Return (base_url, username, password) from CRM_* env vars."""
    base = (os.environ.get("CRM_BASE_URL") or "").strip()
    user = (os.environ.get("CRM_USERNAME") or "").strip()
    password = os.environ.get("CRM_PASSWORD") or ""
    missing = [
        name
        for name, val in (
            ("CRM_BASE_URL", base),
            ("CRM_USERNAME", user),
            ("CRM_PASSWORD", password),
        )
        if not val
    ]
    if missing:
        raise SystemGroupClientError(
            "Missing required env vars: "
            + ", ".join(missing)
            + ". Set them in the environment or a gitignored env file; never commit secrets."
        )
    if not base.endswith("/"):
        base += "/"
    return base, user, password


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
    ) -> None:
        if base_url is None or username is None or password is None:
            env_base, env_user, env_pass = credentials_from_env()
            base_url = base_url or env_base
            username = username or env_user
            password = password if password is not None else env_pass
        if not base_url.endswith("/"):
            base_url += "/"
        self.base_url = base_url
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
