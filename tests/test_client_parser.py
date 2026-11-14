"""Standalone tests for this package (fixtures under tests/fixtures)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock

from lithium_systemgroup import (
    AmbiguousBomMatch,
    NoBomMatch,
    SystemGroupClient,
    SystemGroupClientError,
    SystemGroupParseError,
    bi_to_hex,
    bom_edit_direct_url,
    bom_edit_url,
    bom_list_direct_url,
    bom_list_url,
    build_hashed_password,
    customer_orders_report_url,
    deterministic_randbelow,
    digit_size_for_modulus,
    encrypted_string,
    extract_bom_id_from_url,
    extract_export_url_base,
    list_revisions_for_part,
    load_bom_list_entries,
    match_bom_list_entries,
    normalize_crm_decimal,
    normalize_order_quantity,
    parse_bom_document,
    parse_bom_list_document,
    parse_bom_list_pager,
    parse_customer_orders_document,
    parse_login_form,
    resolve_bom_from_list,
    split_bom_code,
    unicode_correct_invalid_characters,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class LoginParseTests(unittest.TestCase):
    def test_parse_login_form_extracts_aspnet_fields(self):
        html = (FIXTURES / "login.aspx.html").read_text(encoding="utf-8")
        form = parse_login_form(html)
        self.assertEqual(form.username_name, "txtUsername")
        self.assertEqual(form.password_name, "txtPassword")
        self.assertIn("__VIEWSTATE", form.hidden)
        self.assertTrue(form.uses_rsa_password)
        self.assertEqual(form.rsa_exponent_hex, "010001")
        self.assertEqual(form.session_id, "fixture-session-001")

    def test_parse_login_form_missing_password_raises(self):
        with self.assertRaises(SystemGroupParseError):
            parse_login_form("<html><form><input name='u' type='text'></form></html>")


class RsaLoginTests(unittest.TestCase):
    _N_HEX = (
        "cc175d0a4536caa7ce8b05bb30b8b2da836d9fc82e7bfbd31daadbbbb816b40bcc8fac18"
        "4dbdd85c6ccd763a0c3df10b77a8d6fb54507d13a62b1ddc68420f8d1929551c03485646"
        "bddb24010d37eb92b8280c74eee3e1ad2b1bfb12a4df8733cc6590829619a3ae76ea76ed"
        "627084a6642d91141008b7336fbb59abcb9ccf65"
    )
    _D_HEX = (
        "8848b5ad6d08a88d23e43be0832962ed7dc40f658a7cea6d50ab4e6ded00e41bd8eebbb9"
        "c2f819e9a6121177b3c30d1a5e3e55c8a0c5daafeb4f717244009106b2a7d09fe23625af"
        "55661274814b25cc25d245fb658bf6720cbef7480b3df8b832e23f4bd594ceaee8e4a0d1"
        "84f0d4dc51a25a28a10f9229e953ea1711e34621"
    )
    _E_HEX = "010001"

    def test_unicode_correct_arabic_yeh_keh(self):
        self.assertEqual(
            unicode_correct_invalid_characters("\u064a\u0643"),
            "\u06cc\u06a9",
        )

    def test_encrypted_string_roundtrip_pkcs1(self):
        plaintext = "fixture-session-001--s3cret"
        ct_hex = encrypted_string(
            plaintext,
            exponent_hex=self._E_HEX,
            modulus_hex=self._N_HEX,
            randbelow=deterministic_randbelow(42),
        )
        c_int = int(ct_hex.split()[0], 16)
        n = int(self._N_HEX, 16)
        d = int(self._D_HEX, 16)
        em = pow(c_int, d, n).to_bytes(digit_size_for_modulus(n), "big")
        sep = em.index(0x00, 2)
        self.assertEqual(em[sep + 1 :].decode("utf-8"), plaintext)

    def test_build_hashed_password_uses_session_separator(self):
        ct = build_hashed_password(
            "pw",
            session_id="SID",
            exponent_hex=self._E_HEX,
            modulus_hex=self._N_HEX,
            randbelow=deterministic_randbelow(7),
        )
        c_int = int(ct.split()[0], 16)
        n = int(self._N_HEX, 16)
        d = int(self._D_HEX, 16)
        em = pow(c_int, d, n).to_bytes(digit_size_for_modulus(n), "big")
        sep = em.index(0x00, 2)
        self.assertEqual(em[sep + 1 :].decode("utf-8"), "SID--pw")

    def test_bi_to_hex_groups(self):
        self.assertEqual(bi_to_hex(0), "0000")
        self.assertEqual(bi_to_hex(0xABCD), "abcd")


class BomParseTests(unittest.TestCase):
    def test_parse_html_fixture(self):
        html = (FIXTURES / "bom_edit.html").read_text(encoding="utf-8")
        bom = parse_bom_document(html, content_type="text/html")
        self.assertEqual(bom.bom_id, "39477")
        self.assertEqual(bom.parent_part_number, "500-9001-00")
        self.assertEqual(len(bom.lines), 3)
        self.assertEqual(bom.lines[0].part_number, "200-3333-00")

    def test_parse_json_fixture(self):
        raw = (FIXTURES / "bom_edit.json").read_text(encoding="utf-8")
        bom = parse_bom_document(raw, content_type="application/json")
        self.assertEqual(bom.bom_id, "39477")
        self.assertEqual(len(bom.lines), 2)

    def test_parse_telerik_edit_html_eds_materials(self):
        html = (FIXTURES / "bom_edit_telerik.html").read_text(encoding="utf-8")
        bom = parse_bom_document(html, content_type="text/html")
        self.assertEqual(bom.source, "html-eds-materials")
        self.assertEqual(bom.parent_part_number, "1155F229")
        self.assertEqual(bom.parent_revision, "A2*10-C")
        self.assertEqual(len(bom.lines), 3)

    def test_normalize_crm_decimal(self):
        self.assertEqual(normalize_crm_decimal("70/000000000000000"), "70")
        self.assertEqual(normalize_crm_decimal("10/500"), "10.5")

    def test_empty_document_raises(self):
        with self.assertRaises(SystemGroupParseError):
            parse_bom_document("   ")


class BomListParseTests(unittest.TestCase):
    def test_split_bom_code(self):
        self.assertEqual(split_bom_code("1155F229/A2*10-C"), ("1155F229", "A2*10-C"))
        self.assertEqual(split_bom_code("CGM5883"), ("CGM5883", ""))

    def test_parse_list_html_fixture(self):
        html = (FIXTURES / "bom_list.html").read_text(encoding="utf-8")
        entries = parse_bom_list_document(html, content_type="text/html")
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[0].bom_id, "39477")

    def test_parse_telerik_list_json_fixture(self):
        raw = (FIXTURES / "bom_list_telerik.json").read_text(encoding="utf-8")
        entries = parse_bom_list_document(raw, content_type="application/json")
        self.assertEqual(entries[0].part_number, "1155F229")
        self.assertEqual(entries[0].revision, "A2*10-C")

    def test_match_narrows_ambiguous_pn_with_revision(self):
        entries = parse_bom_list_document(
            (FIXTURES / "bom_list.html").read_text(encoding="utf-8")
        )
        hits = match_bom_list_entries(
            entries, part_number="500-9001-00", revision="1"
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].bom_id, "39477")

    def test_resolve_no_match(self):
        with self.assertRaises(NoBomMatch):
            resolve_bom_from_list(
                part_number="999-0000-00",
                list_path=FIXTURES / "bom_list.json",
            )

    def test_resolve_ambiguous(self):
        with self.assertRaises(AmbiguousBomMatch) as ctx:
            resolve_bom_from_list(
                part_number="500-9001-00",
                list_path=FIXTURES / "bom_list.html",
            )
        self.assertEqual(len(ctx.exception.matches), 2)

    def test_list_revisions_for_part(self):
        rows = list_revisions_for_part(
            "500-9001-00", list_path=FIXTURES / "bom_list.json"
        )
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual({r.part_number for r in rows}, {"500-9001-00"})


class ClientTests(unittest.TestCase):
    def test_build_login_payload(self):
        html = (FIXTURES / "login.aspx.html").read_text(encoding="utf-8")
        form = parse_login_form(html)
        client = SystemGroupClient(
            base_url="http://192.168.3.193/",
            username="alice",
            password="secret",
        )
        payload = client.build_login_payload(form)
        self.assertEqual(payload["txtUsername"], "alice")
        self.assertEqual(payload["txtPassword"], "")
        self.assertTrue(payload["hashedPassword"])
        self.assertRegex(payload["hashedPassword"], r"^[0-9a-f]+( [0-9a-f]+)*$")

    def test_urls(self):
        self.assertIn("Shell.aspx", bom_edit_url("http://crm/", 39477))
        self.assertIn("id=39477", bom_edit_direct_url("http://crm/", 39477))
        self.assertIn("EntityName=Bom", bom_list_direct_url("http://crm/"))
        self.assertIn("Shell.aspx", bom_list_url("http://crm/"))

    def test_extract_bom_id_from_url(self):
        self.assertEqual(
            extract_bom_id_from_url(
                "http://crm/Shell.aspx?page=PAC/Engineering/BomPages/Edit.aspx?id=39477"
            ),
            "39477",
        )
        self.assertIsNone(extract_bom_id_from_url("http://example/no-id"))

    def test_credentials_from_env_missing(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemGroupClientError):
                SystemGroupClient()

    def test_login_and_fetch_bom_with_session_mock(self):
        login_html = (FIXTURES / "login.aspx.html").read_text(encoding="utf-8")
        bom_html = (FIXTURES / "bom_edit.html").read_text(encoding="utf-8")
        session = mock.Mock()
        login_get = mock.Mock(
            status_code=200, text=login_html, url="http://crm/Authentication/Login.aspx"
        )
        login_get.raise_for_status = mock.Mock()
        login_post = mock.Mock(
            status_code=200, text="<html>ok</html>", url="http://crm/Default.aspx"
        )
        login_post.raise_for_status = mock.Mock()
        bom_get = mock.Mock(
            status_code=200,
            text=bom_html,
            url="http://crm/Edit.aspx",
            headers={"Content-Type": "text/html"},
        )
        bom_get.raise_for_status = mock.Mock()
        session.get.side_effect = [login_get, bom_get]
        session.post.return_value = login_post
        client = SystemGroupClient(
            base_url="http://crm/", username="u", password="p", session=session
        )
        body, ctype = client.fetch_bom(39477)
        self.assertIn("BomLinesGrid", body)
        self.assertIn("text/html", ctype)

    def test_parse_bom_list_pager_page1(self):
        html = (FIXTURES / "bom_list_page1.html").read_text(encoding="utf-8")
        pager = parse_bom_list_pager(html)
        self.assertIsNotNone(pager)
        assert pager is not None
        self.assertEqual(pager.current_page, 1)
        self.assertEqual(pager.page_count, 2)
        self.assertTrue(pager.next_enabled)
        self.assertEqual(
            pager.pager_target,
            "lstEntities$Grid$MasterTableView$ctl03$ctl02$Pager",
        )
        self.assertEqual(pager.next_argument, "3")
        self.assertEqual(pager.fields.get("__EVENTVALIDATION"), "ev-page1")

    def test_parse_pager_next_argument_from_itemdata(self):
        from lithium_systemgroup import parse_pager_next_argument

        html = (
            '<div class="EntityListPaging"></div>'
            r'<script>$create({"itemData":[{"value":"First"},{"value":"Previous"},'
            r'{"value":"Pages","templated":true},{"value":"Next"},{"value":"Last"}]});'
            "</script>"
        )
        self.assertEqual(parse_pager_next_argument(html), "3")

    def test_strip_clientstate_updated(self):
        from lithium_systemgroup import strip_clientstate_updated

        fields = {
            "a": '{"x":1,"__Updated":true}',
            "b": '{"__Updated":true}',
            "c": "plain",
        }
        out = strip_clientstate_updated(fields)
        self.assertEqual(out["a"], '{"x":1}')
        self.assertEqual(out["b"], "{}")
        self.assertEqual(out["c"], "plain")

    def test_apply_bom_list_async_delta_keeps_pager_target(self):
        from lithium_systemgroup import apply_bom_list_async_delta

        page1 = (FIXTURES / "bom_list_page1.html").read_text(encoding="utf-8")
        panel = (
            '<span id="lstEntities"><div class="EntityListPaging">'
            '<input id="x_cboPage_Input" value="صفحه2" />'
            '<ul class="rcbList"><li class="rcbItem ">صفحه1</li>'
            '<li class="rcbItem ">صفحه2</li></ul>'
            '<li class="rtbItem rtbBtn"><a title="بعدی" href="#"></a></li>'
            "</div>"
            '<table class="rgMasterTable"><tr class="rgRow"><td>1</td></tr></table>'
            "</span>"
        )
        delta = (
            "1|#||4|"
            f"{len(panel)}|updatePanel|innerUpdatePanel|{panel}"
            "|0|hiddenField|__EVENTTARGET|"
            "|0|hiddenField|__EVENTARGUMENT|"
            "|0|hiddenField|__VIEWSTATE|"
            "|11|hiddenField|__EVENTVALIDATION|ev-page2-xx"
        )
        merged = apply_bom_list_async_delta(page1, delta)
        pager = parse_bom_list_pager(merged)
        self.assertIsNotNone(pager)
        assert pager is not None
        self.assertEqual(pager.current_page, 2)
        self.assertEqual(
            pager.pager_target,
            "lstEntities$Grid$MasterTableView$ctl03$ctl02$Pager",
        )
        self.assertEqual(pager.fields.get("__EVENTVALIDATION"), "ev-page2-xx")

    def test_parse_bom_list_pager_unicode_escaped_quotes(self):
        # Live CRM $create blobs use \u0027 instead of raw quotes.
        html = (
            '<form action="./List.aspx?EntityName=Bom">'
            '<input type="hidden" name="__EVENTVALIDATION" value="ev" />'
            '<div class="EntityListPaging">'
            '<input id="x_cboPage_Input" value="صفحه1" />'
            '<ul class="rcbList"><li class="rcbItem ">صفحه1</li>'
            '<li class="rcbItem ">صفحه3</li></ul>'
            '<li class="rtbItem rtbBtn"><a title="بعدی" href="#"></a></li>'
            "</div>"
            r'<script>$create({_rawPostBackReference:"WebForm_DoPostBackWithOptions('
            r"new WebForm_PostBackOptions(\u0027lstEntities$Grid$MasterTableView$"
            r"ctl03$ctl02$Pager\u0027, \u0027{0}\u0027, true, \u0027{1}\u0027, "
            r'\u0027\u0027, false, true))"});</script>'
            "</form>"
        )
        pager = parse_bom_list_pager(html)
        self.assertIsNotNone(pager)
        assert pager is not None
        self.assertEqual(
            pager.pager_target,
            "lstEntities$Grid$MasterTableView$ctl03$ctl02$Pager",
        )
        self.assertEqual(pager.page_count, 3)

    def test_parse_bom_list_pager_page2_next_disabled(self):
        html = (FIXTURES / "bom_list_page2.html").read_text(encoding="utf-8")
        pager = parse_bom_list_pager(html)
        self.assertIsNotNone(pager)
        assert pager is not None
        self.assertEqual(pager.current_page, 2)
        self.assertEqual(pager.page_count, 2)
        self.assertFalse(pager.next_enabled)

    def test_iter_bom_list_pages_walks_next_postback(self):
        login_html = (FIXTURES / "login.aspx.html").read_text(encoding="utf-8")
        page1 = (FIXTURES / "bom_list_page1.html").read_text(encoding="utf-8")
        page2 = (FIXTURES / "bom_list_page2.html").read_text(encoding="utf-8")
        session = mock.Mock()
        login_get = mock.Mock(
            status_code=200, text=login_html, url="http://crm/Authentication/Login.aspx"
        )
        login_get.raise_for_status = mock.Mock()
        login_post = mock.Mock(
            status_code=200, text="<html>ok</html>", url="http://crm/Default.aspx"
        )
        login_post.raise_for_status = mock.Mock()
        list_get = mock.Mock(
            status_code=200,
            text=page1,
            url="http://crm/List.aspx?ComponentName=SystemGroup.PAC.Engineering&EntityName=Bom",
            headers={"Content-Type": "text/html"},
        )
        list_get.raise_for_status = mock.Mock()
        next_post = mock.Mock(
            status_code=200,
            text=page2,
            url="http://crm/List.aspx?ComponentName=SystemGroup.PAC.Engineering&EntityName=Bom",
            headers={"Content-Type": "text/html"},
        )
        next_post.raise_for_status = mock.Mock()
        session.get.side_effect = [login_get, list_get]
        session.post.side_effect = [login_post, next_post]
        client = SystemGroupClient(
            base_url="http://crm/", username="u", password="p", session=session
        )
        pages = list(client.iter_bom_list_pages())
        self.assertEqual(len(pages), 2)
        self.assertIn("صفحه1", pages[0][0])
        self.assertIn("صفحه2", pages[1][0])
        # Second session.post is the pager Next (after login post).
        next_call = session.post.call_args_list[1]
        posted = next_call.kwargs.get("data") or next_call.args[1]
        headers = next_call.kwargs.get("headers") or {}
        self.assertEqual(
            posted["__EVENTTARGET"],
            "lstEntities$Grid$MasterTableView$ctl03$ctl02$Pager",
        )
        # Live CRM RadToolBar posts the Next item index, not the string "Next".
        self.assertEqual(posted["__EVENTARGUMENT"], "3")
        self.assertEqual(posted["__ASYNCPOST"], "true")
        self.assertIn("innerUpdatePanel|", posted["scriptManager"])
        self.assertEqual(posted["__EVENTVALIDATION"], "ev-page1")
        self.assertEqual(headers.get("X-MicrosoftAjax"), "Delta=true")

    def test_load_bom_list_entries_merges_pages(self):
        login_html = (FIXTURES / "login.aspx.html").read_text(encoding="utf-8")
        page1 = (FIXTURES / "bom_list_page1.html").read_text(encoding="utf-8")
        page2 = (FIXTURES / "bom_list_page2.html").read_text(encoding="utf-8")
        session = mock.Mock()
        login_get = mock.Mock(
            status_code=200, text=login_html, url="http://crm/Authentication/Login.aspx"
        )
        login_get.raise_for_status = mock.Mock()
        login_post = mock.Mock(
            status_code=200, text="<html>ok</html>", url="http://crm/Default.aspx"
        )
        login_post.raise_for_status = mock.Mock()
        list_get = mock.Mock(
            status_code=200,
            text=page1,
            url="http://crm/List.aspx",
            headers={"Content-Type": "text/html"},
        )
        list_get.raise_for_status = mock.Mock()
        next_post = mock.Mock(
            status_code=200,
            text=page2,
            url="http://crm/List.aspx",
            headers={"Content-Type": "text/html"},
        )
        next_post.raise_for_status = mock.Mock()
        session.get.side_effect = [login_get, list_get]
        session.post.side_effect = [login_post, next_post]
        client = SystemGroupClient(
            base_url="http://crm/", username="u", password="p", session=session
        )
        entries = load_bom_list_entries(client=client)
        ids = [e.bom_id for e in entries]
        self.assertEqual(ids, ["39477", "40001", "41000", "42000"])

    def test_list_revisions_stops_after_part_cluster(self):
        """Load revisions must not walk pages after the part-id hit cluster ends."""
        login_html = (FIXTURES / "login.aspx.html").read_text(encoding="utf-8")
        page1 = (FIXTURES / "bom_list_page1.html").read_text(encoding="utf-8")
        # Mid page: no 500-9001-00 hits, but Next still enabled (3-page catalog).
        # Without early-exit, iter would keep posting through page3+.
        page2 = (
            (FIXTURES / "bom_list_page2.html")
            .read_text(encoding="utf-8")
            .replace("صفحه2", "صفحه2")
            .replace(
                '<ul class="rcbList"><li class="rcbItem ">صفحه1</li>'
                '<li class="rcbItem ">صفحه2</li></ul>',
                '<ul class="rcbList"><li class="rcbItem ">صفحه1</li>'
                '<li class="rcbItem ">صفحه2</li>'
                '<li class="rcbItem ">صفحه3</li></ul>',
            )
            .replace(
                '<li class="rtbItem rtbBtn rtbDisabled">'
                '<a title="بعدی" class="rtbWrap" href="#"><span>Next</span></a></li>',
                '<li class="rtbItem rtbBtn">'
                '<a title="بعدی" class="rtbWrap" href="#"><span>Next</span></a></li>',
            )
            .replace(
                '<li class="rtbItem rtbBtn rtbDisabled">'
                '<a title="انتها" class="rtbWrap" href="#"><span>Last</span></a></li>',
                '<li class="rtbItem rtbBtn">'
                '<a title="انتها" class="rtbWrap" href="#"><span>Last</span></a></li>',
            )
        )
        page3 = page2.replace("صفحه2", "صفحه3").replace("ev-page2", "ev-page3")
        page3 = page3.replace("41,000", "51,000").replace("41000", "51000")
        page3 = page3.replace("42,000", "52,000").replace("42000", "52000")
        session = mock.Mock()
        login_get = mock.Mock(
            status_code=200, text=login_html, url="http://crm/Authentication/Login.aspx"
        )
        login_get.raise_for_status = mock.Mock()
        login_post = mock.Mock(
            status_code=200, text="<html>ok</html>", url="http://crm/Default.aspx"
        )
        login_post.raise_for_status = mock.Mock()
        list_get = mock.Mock(
            status_code=200,
            text=page1,
            url="http://crm/List.aspx",
            headers={"Content-Type": "text/html"},
        )
        list_get.raise_for_status = mock.Mock()
        next_post = mock.Mock(
            status_code=200,
            text=page2,
            url="http://crm/List.aspx",
            headers={"Content-Type": "text/html"},
        )
        next_post.raise_for_status = mock.Mock()
        page3_post = mock.Mock(
            status_code=200,
            text=page3,
            url="http://crm/List.aspx",
            headers={"Content-Type": "text/html"},
        )
        page3_post.raise_for_status = mock.Mock()
        session.get.side_effect = [login_get, list_get]
        session.post.side_effect = [login_post, next_post, page3_post]
        client = SystemGroupClient(
            base_url="http://crm/", username="u", password="p", session=session
        )
        rows = list_revisions_for_part("500-9001-00", client=client)
        self.assertEqual({r.bom_id for r in rows}, {"39477", "40001"})
        # login POST + one Next (page2 miss after page1 hits) — never page3.
        self.assertEqual(session.post.call_count, 2)

    def test_customer_orders_report_url_uses_base(self):
        url = customer_orders_report_url("http://crm.example/", "ReportViewer.aspx?x=1")
        self.assertTrue(url.startswith("http://crm.example/"))
        self.assertIn("ReportViewer.aspx", url)

    def test_extract_export_url_base(self):
        html = (FIXTURES / "customer_orders_report.html").read_text(encoding="utf-8")
        base = extract_export_url_base(html)
        self.assertIsNotNone(base)
        self.assertIn("ReportSession=fixtureSession001", base)
        self.assertIn("OpType=Export", base)
        self.assertTrue(base.endswith("Format="))

    def test_fetch_customer_orders_csv_mock(self):
        login_html = (FIXTURES / "login.aspx.html").read_text(encoding="utf-8")
        report_html = (FIXTURES / "customer_orders_report.html").read_text(
            encoding="utf-8"
        )
        csv_text = (FIXTURES / "customer_orders.csv").read_text(encoding="utf-8")
        session = mock.Mock()
        login_get = mock.Mock(
            status_code=200, text=login_html, url="http://crm/Authentication/Login.aspx"
        )
        login_get.raise_for_status = mock.Mock()
        login_post = mock.Mock(
            status_code=200, text="<html>ok</html>", url="http://crm/Default.aspx"
        )
        login_post.raise_for_status = mock.Mock()
        report_get = mock.Mock(
            status_code=200,
            text=report_html,
            url="http://crm/ReportViewer.aspx",
            headers={"Content-Type": "text/html"},
        )
        report_get.raise_for_status = mock.Mock()
        export_get = mock.Mock(
            status_code=200,
            content=csv_text.encode("utf-8-sig"),
            text=csv_text,
            url="http://crm/Reserved.ReportViewerWebControl.axd",
            headers={"Content-Type": "text/csv; charset=utf-8"},
        )
        export_get.raise_for_status = mock.Mock()
        session.get.side_effect = [login_get, report_get, export_get]
        session.post.return_value = login_post
        client = SystemGroupClient(
            base_url="http://crm/", username="u", password="p", session=session
        )
        body, ctype = client.fetch_customer_orders()
        self.assertIn("شماره_حواله", body)
        self.assertIn("csv", ctype.lower())


class CustomerOrdersParseTests(unittest.TestCase):
    def test_normalize_order_quantity(self):
        self.assertEqual(normalize_order_quantity("1,000"), "1000")
        self.assertEqual(normalize_order_quantity("15,240"), "15240")
        self.assertEqual(normalize_order_quantity("50"), "50")

    def test_parse_csv_fixture(self):
        raw = (FIXTURES / "customer_orders.csv").read_text(encoding="utf-8")
        rows = parse_customer_orders_document(raw, content_type="text/csv")
        self.assertEqual(len(rows), 3)
        first = rows[0]
        self.assertEqual(first.delivery_note_number, "00006607")
        self.assertEqual(first.product_code, "RMJ/001")
        self.assertEqual(first.remaining_quantity, "2")
        self.assertIn("فخار", first.customer_name)
        # Aggregate footer columns must not overwrite line quantities.
        self.assertEqual(first.note_quantity, "50")
        second = rows[1]
        self.assertEqual(second.note_quantity, "1000")
        self.assertEqual(second.remaining_quantity, "980")

    def test_empty_orders_raise(self):
        with self.assertRaises(SystemGroupParseError):
            parse_customer_orders_document("   ")


if __name__ == "__main__":
    unittest.main()
