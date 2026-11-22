#!/usr/bin/env python3
"""LAN probe: dump + try QuantitativeReview انبار (Store) filter.

Run on a host that can reach CRM with CRM_* / REPORT_* set:

  set -a && source /path/to/.env && set +a
  export CRM_BASE_URL="${CRM_BASE_URL:-$REPORT_BASE_URL}"
  export CRM_USERNAME="${CRM_USERNAME:-$REPORT_USERNAME}"
  export CRM_PASSWORD="${CRM_PASSWORD:-$REPORT_PASSWORD}"
  python scripts/probe_quantitative_review_store_filter.py [--code 2001] [--out /tmp/qr-probe]

Writes review.html, filter-fields.json, snippets.txt, and when --code is set
attempts fetch_quantitative_review_lines(warehouse_code=…) and saves
filtered-page1.html plus apply-error.txt on failure.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path

from lithium_systemgroup import (
    SystemGroupClient,
    discover_quantitative_review_store_filter_fields,
    quantitative_review_frame_url,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/tmp/qr-probe", help="Output directory")
    parser.add_argument(
        "--code",
        default="",
        help="انبار Number to apply (e.g. 2001). Empty = dump only.",
    )
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    client = SystemGroupClient()
    client.login()
    url = quantitative_review_frame_url(client.base_url)
    resp = client.session.get(url, timeout=client.timeout, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text
    (out / "review.html").write_text(html, encoding="utf-8")
    print("wrote", out / "review.html", "bytes", len(html), "url", resp.url)

    discovered = discover_quantitative_review_store_filter_fields(html)
    (out / "discovered.json").write_text(
        json.dumps(discovered, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("discovered:", discovered)

    fields = []
    for m in re.finditer(r"<(?:input|select|textarea)\b([^>]*)>", html, re.I):
        tag = m.group(0)
        name = re.search(r'\bname="([^"]+)"', tag)
        eid = re.search(r'\bid="([^"]+)"', tag)
        val = re.search(r'\bvalue="([^"]*)"', tag)
        n = name.group(1) if name else ""
        i = eid.group(1) if eid else ""
        blob = f"{n} {i}".lower()
        if any(
            k in blob
            for k in (
                "filter",
                "store",
                "anbar",
                "warehouse",
                "inventory",
                "condition",
                "clientstate",
                "reviewheader",
                "selected",
                "entity",
            )
        ):
            fields.append(
                {
                    "name": n,
                    "id": i,
                    "value": (val.group(1)[:200] if val else ""),
                }
            )
    (out / "filter-fields.json").write_text(
        json.dumps(fields, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("filter-like fields:", len(fields))

    snippets = []
    for key in (
        "انبار",
        "Store",
        "Filter",
        "ReviewHeader",
        "RefrenceMunti",
        "Selected",
        "SgTextBox_Number",
        "EntityList",
    ):
        idx = 0
        found = 0
        while found < 8:
            i = html.find(key, idx)
            if i < 0:
                break
            snippets.append(f"--- {key} @{i} ---\n{html[max(0, i - 120) : i + 280]}\n")
            idx = i + len(key)
            found += 1
    (out / "snippets.txt").write_text("\n".join(snippets), encoding="utf-8")
    print("wrote", out / "snippets.txt")

    code = (args.code or "").strip()
    if not code:
        print("No --code; dump only.")
        return

    try:
        lines = client.fetch_quantitative_review_lines(
            max_pages=1, warehouse_code=code
        )
        (out / "filtered-lines.json").write_text(
            json.dumps(
                [
                    {
                        "part_code": r.part_code,
                        "title": r.title,
                        "quantity": r.quantity,
                    }
                    for r in lines[:50]
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"warehouse_code={code}: {len(lines)} lines on page 1 (capped dump 50)")
    except Exception as exc:  # noqa: BLE001
        (out / "apply-error.txt").write_text(
            f"{exc}\n\n{traceback.format_exc()}", encoding="utf-8"
        )
        print("filter apply failed:", exc, file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc
