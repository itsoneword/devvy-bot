"""William Russell "Submit Claim Online" (Gravity Forms #gform_21), dv-5a25.

Field ids come from a live dump of the public form (2026-11-10). Scope is
what was verified against it: Medical
Treatment, 1 to 6 bills, reimbursement by bank transfer to the account used
before (field 46 Yes + last four digits). Maternity, new bank details and card
payout are :class:`Unsupported`, not improvised.

Claim data keys (all strings unless noted)::

    claim_type      "medical"
    name, address, dob (dd/mm/yyyy), policy, member, email, phone
    previous_claim  optional claim reference this one relates to
    symptoms_from (dd/mm/yyyy), symptoms, consult_date (optional), diagnosis
    prior_condition optional: when you had it before (dd/mm/yyyy or text)
    accident        optional: accident and injuries
    bills           list of 1..6 {date, invoice, amount, currency, notes?}
    payout          {previous_account: true, last4: "1234", currency?}
    other_insurance optional: name, address, policy number of other cover
    files           paths of invoices / receipts (inside the workspace)
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Tuple

from . import (
    CHECK,
    FILE,
    RADIO,
    SELECT,
    TEXT,
    BadData,
    FieldDef,
    FillPlan,
    FormDef,
    PageDef,
    Unsupported,
)

_F = 21  # Gravity Forms form id
MAX_BILLS = 6
PHYSICIAN_THRESHOLD = Decimal("500")
# Reimbursement currency select (field 89): ISO code -> option label.
PAYOUT_CURRENCIES = {"USD": "US dollars", "GBP": "Pounds sterling", "EUR": "Euros"}
_YES_NO = {"No": "No", "Yes": "Yes"}
_DATE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def _inp(n: Any) -> str:
    return f"#input_{_F}_{n}"


def _radio(n: int, *labels: str) -> Dict[str, str]:
    return {label: f"#choice_{_F}_{n}_{i}" for i, label in enumerate(labels)}


def _text(key: str, n: int, escape: bool = False) -> FieldDef:
    return FieldDef(key, TEXT, _inp(n), escape=escape)


# (date, invoice, amount, notes, "add another" select) per bill; the 6th
# bill has no "add another".
_BILL_IDS: Tuple[Tuple[int, int, int, int, int], ...] = (
    (20, 21, 22, 75, 23),
    (24, 25, 26, 76, 27),
    (28, 29, 30, 77, 31),
    (32, 33, 34, 78, 35),
    (38, 37, 36, 79, 39),
    (42, 41, 40, 80, 0),
)


def _bill_fields() -> Tuple[FieldDef, ...]:
    out: List[FieldDef] = []
    for i, (date, invoice, amount, notes, more) in enumerate(_BILL_IDS, start=1):
        out += [
            _text(f"bill{i}_date", date, escape=True),
            _text(f"bill{i}_invoice", invoice),
            FieldDef(f"bill{i}_currency", TEXT, _inp(amount), part=(0, 2)),
            FieldDef(f"bill{i}_amount", TEXT, _inp(amount), part=(1, 2)),
            _text(f"bill{i}_notes", notes),
        ]
        if more:
            out.append(FieldDef(f"bill{i}_more", SELECT, _inp(more), options=_YES_NO))
    return tuple(out)


PAGES = (
    PageDef(
        1,
        f"#gform_page_{_F}_1",
        (
            FieldDef(
                "claim_type",
                RADIO,
                options=_radio(96, "Medical Treatment", "Maternity Care"),
            ),
            _text("name", 3),
            _text("address", 67),
            _text("dob", 6, escape=True),
            _text("policy", 7),
            _text("member", 74),
            _text("email", 8),
            _text("phone", 9),
            FieldDef("previous_claim", RADIO, options=_radio(93, "No", "Yes")),
            _text("previous_claim_ref", 95),
        ),
    ),
    PageDef(
        2,
        f"#gform_page_{_F}_2",
        (
            _text("symptoms_from", 11, escape=True),
            _text("symptoms", 84),
            _text("consult_date", 12, escape=True),
            _text("diagnosis", 13),
            FieldDef("prior_condition", SELECT, _inp(68), options=_YES_NO),
            _text("prior_condition_when", 16),
            FieldDef("accident", RADIO, options=_radio(14, "No", "Yes")),
            _text("accident_details", 17),
        ),
    ),
    PageDef(3, f"#gform_page_{_F}_3", _bill_fields()),
    PageDef(
        4,
        f"#gform_page_{_F}_4",
        (
            FieldDef(
                "payout_method",
                SELECT,
                _inp(64),
                options={"Bank transfer": "Bank transfer"},
            ),
            FieldDef(
                "payout_currency",
                SELECT,
                _inp(89),
                options=PAYOUT_CURRENCIES,
            ),
            FieldDef("previous_account", RADIO, options=_radio(46, "Yes", "No")),
            _text("last4", 90),
        ),
    ),
    PageDef(
        5,
        f"#gform_page_{_F}_5",
        (
            FieldDef("other_insurance", RADIO, options=_radio(58, "Yes", "No")),
            _text("other_insurance_details", 59),
            FieldDef("consent", CHECK, f"#input_{_F}_60_1"),
            _text("declaration_name", 62),
            FieldDef("documents", FILE, f"#gform_page_{_F}_5 input[type=file]"),
        ),
    ),
)


def _str(data: Dict[str, Any], key: str, required: bool = True) -> str:
    value = data.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise BadData(key, f"{key} is required")
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise BadData(key, f"{key} must be text")
    return str(value).strip()


def _date(data: Dict[str, Any], key: str, required: bool = True) -> str:
    value = _str(data, key, required)
    if value and not _DATE.match(value):
        raise BadData(key, f"{key} must be dd/mm/yyyy, got {value!r}")
    return value


def plan(data: Dict[str, Any]) -> FillPlan:
    """Claim data -> per-page values; raises before any page is touched."""
    if not isinstance(data, dict):
        raise BadData("data", "data must be an object")
    claim_type = str(data.get("claim_type") or "medical").strip().lower()
    if claim_type != "medical":
        raise Unsupported(
            f"claim_type {claim_type!r} is not supported (only medical treatment)"
        )
    bills = data.get("bills")
    if not isinstance(bills, list) or not bills:
        raise Unsupported("at least one bill is required")
    if len(bills) > MAX_BILLS:
        raise Unsupported(
            f"the form takes at most {MAX_BILLS} bills; file the rest as a second claim"
        )
    payout = data.get("payout")
    if not isinstance(payout, dict) or payout.get("previous_account") is not True:
        raise Unsupported(
            "only bank transfer to the account used before is supported "
            "(payout.previous_account: true + last4)"
        )
    last4 = str(payout.get("last4") or "").strip()
    if not re.fullmatch(r"\d{4}", last4):
        raise Unsupported(
            "payout.last4 (last four digits of the bank account) is required"
        )

    warnings: List[str] = []
    p1: List[Tuple[str, Any]] = [("claim_type", "Medical Treatment")]
    for key in ("name", "address"):
        p1.append((key, _str(data, key)))
    p1.append(("dob", _date(data, "dob")))
    for key in ("policy", "member", "email", "phone"):
        p1.append((key, _str(data, key)))
    previous = _str(data, "previous_claim", required=False)
    if previous:
        p1 += [("previous_claim", "Yes"), ("previous_claim_ref", previous)]
    else:
        p1.append(("previous_claim", "No"))

    p2: List[Tuple[str, Any]] = [
        ("symptoms_from", _date(data, "symptoms_from")),
        ("symptoms", _str(data, "symptoms")),
    ]
    consult = _date(data, "consult_date", required=False)
    if consult:
        p2.append(("consult_date", consult))
    p2.append(("diagnosis", _str(data, "diagnosis")))
    prior = _str(data, "prior_condition", required=False)
    if prior:
        p2 += [("prior_condition", "Yes"), ("prior_condition_when", prior)]
    else:
        p2.append(("prior_condition", "No"))
    accident = _str(data, "accident", required=False)
    if accident:
        p2 += [("accident", "Yes"), ("accident_details", accident)]
    else:
        p2.append(("accident", "No"))

    p3: List[Tuple[str, Any]] = []
    total = Decimal(0)
    currencies = set()
    for i, bill in enumerate(bills, start=1):
        if not isinstance(bill, dict):
            raise BadData(f"bills[{i}]", f"bill {i} must be an object")
        amount = _str(bill, "amount")
        try:
            total += Decimal(amount)
        except InvalidOperation:
            raise BadData(f"bill{i}_amount", f"bill {i} amount must be a number")
        currency = _str(bill, "currency").upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise BadData(f"bill{i}_currency", f"bill {i} currency must be an ISO code")
        currencies.add(currency)
        p3 += [
            (f"bill{i}_date", _str(bill, "date")),
            (f"bill{i}_invoice", _str(bill, "invoice")),
            (f"bill{i}_currency", currency),
            (f"bill{i}_amount", amount),
        ]
        notes = _str(bill, "notes", required=False)
        if notes:
            p3.append((f"bill{i}_notes", notes))
        if i < MAX_BILLS:
            p3.append((f"bill{i}_more", "Yes" if i < len(bills) else "No"))
    if total > PHYSICIAN_THRESHOLD:
        warnings.append(
            f"bills total {total} {'/'.join(sorted(currencies))}: over US$500 the "
            "claim needs section B of the PDF claim form signed by the physician; "
            "attach it to files"
        )

    payout_currency = str(payout.get("currency") or sorted(currencies)[0]).upper()
    if payout_currency not in PAYOUT_CURRENCIES:
        raise Unsupported(
            f"reimbursement currency {payout_currency} is not supported "
            f"(one of {', '.join(PAYOUT_CURRENCIES)}; set payout.currency)"
        )
    p4: List[Tuple[str, Any]] = [
        ("payout_method", "Bank transfer"),
        ("payout_currency", payout_currency),
        ("previous_account", "Yes"),
        ("last4", last4),
    ]

    other = _str(data, "other_insurance", required=False)
    p5: List[Tuple[str, Any]] = (
        [("other_insurance", "Yes"), ("other_insurance_details", other)]
        if other
        else [("other_insurance", "No")]
    )
    p5 += [("consent", True), ("declaration_name", _str(data, "name"))]

    files = data.get("files") or []
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise BadData("files", "files must be a list of paths")
    if not files:
        warnings.append("no supporting documents attached: the form requires one")
    return FillPlan(
        values={1: p1, 2: p2, 3: p3, 4: p4, 5: p5},
        upload=(5, "documents"),
        files=list(files),
        warnings=warnings,
    )


FORM = FormDef(
    id="william_russell_claim",
    title="William Russell medical claim",
    domain="william-russell.com",
    url="https://www.william-russell.com/members/claims/",
    pages=PAGES,
    plan=plan,
    form_selector=f"#gform_wrapper_{_F}",
    next_selector=".gform_next_button",
    submit_selector=f"#gform_submit_button_{_F}",
    overlay_selectors=(".cky-overlay", ".cky-consent-container"),
    preview_keys=("name", "policy", "member", "symptoms_from", "diagnosis"),
)
