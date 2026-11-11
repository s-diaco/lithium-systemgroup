# lithium-systemgroup

SystemGroup CRM client and BoM parsers for **Lithium** and other Python projects.

Talks to the on-LAN SystemGroup PAC Engineering CRM (forms-auth login, BoM list, BoM edit pages) and returns structured data. **No Django / Lithium models.**

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
from lithium_systemgroup import SystemGroupClient, parse_bom_document, list_revisions_for_part

client = SystemGroupClient(
    base_url="http://192.168.3.193/",
    username="crm-user",
    password="crm-pass",
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
```

CLI-style env credentials (optional): `CRM_BASE_URL`, `CRM_USERNAME`, `CRM_PASSWORD`.

## What this package includes

| Module | Role |
|--------|------|
| `client` | ASP.NET forms-auth session, fetch BoM list / edit |
| `parser` | Login form, BoM edit HTML/JSON, BoM list HTML/JSON |
| `rsa_login` | CRM RSA password hashing used by Login.aspx |
| `catalog` | List load + match helpers (`list_revisions_for_part`, `resolve_bom_from_list`) |

## What it does **not** include

Writing into Lithium (parts, revisions, assemblies). That stays in `django-bom` / Lithium.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"   # or: pip install -e . pytest
pytest
```

Fixtures under `tests/fixtures/` are trimmed CRM samples (LAN-only host).

## License

GPL-3.0-or-later (same family as django-bom / Lithium).
