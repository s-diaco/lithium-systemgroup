"""SystemGroup CRM client and BoM parsers for Lithium and other Python projects."""

from .catalog import (
    AmbiguousBomMatch,
    NoBomMatch,
    SystemGroupError,
    list_revisions_for_part,
    load_bom_list_entries,
    resolve_bom_from_list,
)
from .client import (
    SystemGroupClient,
    SystemGroupClientError,
    bom_edit_direct_url,
    bom_edit_url,
    bom_list_direct_url,
    bom_list_url,
    credentials_from_env,
    extract_bom_id_from_url,
)
from .parser import (
    BomLine,
    BomListEntry,
    LoginFormFields,
    ParsedBom,
    SystemGroupParseError,
    match_bom_list_entries,
    normalize_crm_decimal,
    parse_bom_document,
    parse_bom_list_document,
    parse_login_form,
    split_bom_code,
)
from .rsa_login import (
    bi_to_hex,
    build_hashed_password,
    deterministic_randbelow,
    digit_size_for_modulus,
    encrypted_string,
    unicode_correct_invalid_characters,
)

__all__ = [
    "AmbiguousBomMatch",
    "BomLine",
    "BomListEntry",
    "LoginFormFields",
    "NoBomMatch",
    "ParsedBom",
    "SystemGroupClient",
    "SystemGroupClientError",
    "SystemGroupError",
    "SystemGroupParseError",
    "bi_to_hex",
    "bom_edit_direct_url",
    "bom_edit_url",
    "bom_list_direct_url",
    "bom_list_url",
    "build_hashed_password",
    "credentials_from_env",
    "deterministic_randbelow",
    "digit_size_for_modulus",
    "encrypted_string",
    "extract_bom_id_from_url",
    "list_revisions_for_part",
    "load_bom_list_entries",
    "match_bom_list_entries",
    "normalize_crm_decimal",
    "parse_bom_document",
    "parse_bom_list_document",
    "parse_login_form",
    "resolve_bom_from_list",
    "split_bom_code",
    "unicode_correct_invalid_characters",
]

__version__ = "0.1.0"
