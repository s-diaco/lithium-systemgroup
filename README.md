# lithium-systemgroup

SystemGroup CRM client and BoM / customer-order parsers for Python projects.

Talks to the on-LAN SystemGroup PAC Engineering CRM (forms-auth login, BoM list,
BoM edit, store delivery-note report) and returns structured data.
Framework-agnostic; no ORM or app models.

## Install

```bash
pip install "lithium-systemgroup @ git+https://github.com/s-diaco/lithium-systemgroup.git"
```

Or pin a tag/commit:

```bash
pip install "lithium-systemgroup @ git+https://github.com/s-diaco/lithium-systemgroup.git@v0.1.0"
```

## Quick start

```python
import os
from lithium_systemgroup import (
    SystemGroupClient,
    parse_bom_document,
    parse_customer_orders_document,
    list_revisions_for_part,
)

# Host and credentials come from the environment — never hard-code the base URL.
client = SystemGroupClient(
    base_url=os.environ["CRM_BASE_URL"],
    username=os.environ["CRM_USERNAME"],
    password=os.environ["CRM_PASSWORD"],
)

# Part ID → revisions on the CRM BoM list
revs = list_revisions_for_part("1155F229", client=client)
for row in revs:
    print(row.bom_id, row.revision, row.description)

# Fetch and parse one BoM Edit page
body, ctype = client.fetch_bom(revs[0].bom_id)
bom = parse_bom_document(body, content_type=ctype)
print(bom.parent_part_number, bom.parent_revision, len(bom.lines))
for line in bom.lines:
    print(line.part_number, line.quantity, line.reference)

# Customer orders (store delivery notes with remaining balance)
orders_client = SystemGroupClient(prefer_report_env=True)
csv_body, csv_ctype = orders_client.fetch_customer_orders()
orders = parse_customer_orders_document(csv_body, content_type=csv_ctype)
for row in orders:
    print(row.delivery_note_number, row.customer_name, row.product_code, row.remaining_quantity)
```

## Environment variables

| Name | Role |
|------|------|
| `CRM_BASE_URL` | CRM host base URL (BoM). Falls back to `REPORT_BASE_URL`. |
| `CRM_USERNAME` / `CRM_PASSWORD` | Forms-auth credentials for CRM. Fall back to `REPORT_*`. |
| `REPORT_BASE_URL` | Report host base URL (orders report). Falls back to `CRM_BASE_URL`. |
| `REPORT_USERNAME` / `REPORT_PASSWORD` | Forms-auth for the report host. Fall back to `CRM_*`. |
| `REPORT_ORDERS_PATH` | Optional ReportViewer path (no host). Default: store delivery-note report under `/CustomerReports/Store/…`. |

Never commit credentials. Prefer a gitignored env file or the process environment.

## What this package includes

| Module | Role |
|--------|------|
| `client` | ASP.NET forms-auth session; fetch BoM list / edit (list paging uses Shell-framed List.aspx + async Next); export customer-orders CSV |
| `parser` | Login form, BoM edit/list HTML/JSON, customer-orders CSV |
| `rsa_login` | CRM RSA password hashing used by Login.aspx |
| `catalog` | List load + match helpers (`list_revisions_for_part`, `resolve_bom_from_list`) |

## What it does **not** include

Writing parts, revisions, assemblies, or orders into a downstream ERP app. That stays in the consuming application.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"   # or: pip install -e . pytest
pytest
```

Fixtures under `tests/fixtures/` are trimmed CRM / report samples (LAN-only host).

## License

GPL-3.0-or-later.
