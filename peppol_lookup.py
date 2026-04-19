import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ===================== Config =====================
BASE_URL = "https://directory.peppol.eu/search/1.0/json"
TIMEOUT_SECONDS = 20
SLEEP_BETWEEN_CALLS_SECONDS = 0.12
USER_AGENT = "PeppolDirectoryLookup/3.1 (Python requests)"

# Participant IDs we want to output
RE_PID_0208 = re.compile(r"\b0208:(\d{10})\b", re.IGNORECASE)
RE_PID_9925 = re.compile(r"\b9925:be(\d{10})\b", re.IGNORECASE)


# ===================== HTTP session =====================
def build_session() -> requests.Session:
    """
    Build a requests Session with retry logic and appropriate headers.
    """
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})

    retry = Retry(
        total=6,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ===================== Normalization =====================
def normalize_btw_be(btw_raw: Any) -> str:
    """
    Return exactly 10 digits for Belgian VAT/CBE (no 'BE').
    Handles Excel numeric mangling (9 digits -> pad left with 0).
    """
    if btw_raw is None or (isinstance(btw_raw, float) and pd.isna(btw_raw)):
        return ""
    s = str(btw_raw).strip().upper()
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if len(digits) == 9:
        digits = "0" + digits
    if len(digits) > 10:
        digits = digits[-10:]
    return digits if len(digits) == 10 else ""


# ===================== Directory queries =====================
def query_directory_by_participant(
    session: requests.Session, participant_value: str
) -> Dict[str, Any]:
    """
    Exact match lookup using participant parameter.
    participant_value must include scheme prefix, e.g.
    iso6523-actorid-upis::9925:be0473191833
    """
    params = {"participant": participant_value}
    r = session.get(BASE_URL, params=params, timeout=TIMEOUT_SECONDS)
    if r.status_code >= 400:
        raise RuntimeError(
            f"HTTP {r.status_code} for participant={participant_value}: {r.text[:200]}"
        )
    return r.json()


def query_directory_by_q(session: requests.Session, q_value: str) -> Dict[str, Any]:
    """
    UI-like general purpose search.
    """
    params = {"q": q_value, "resultPageIndex": 0, "resultPageCount": 20}
    r = session.get(BASE_URL, params=params, timeout=TIMEOUT_SECONDS)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} for q={q_value}: {r.text[:200]}")
    return r.json()


# ===================== Parsing helpers =====================
def extract_participant_ids(
    payload: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract 0208 and 9925 from the payload (string scan).
    Output normalized formats:
      - 0208:##########
      - 9925:BE##########
    """
    text = str(payload)

    m0208 = RE_PID_0208.search(text)
    m9925 = RE_PID_9925.search(text)

    id_0208 = f"0208:{m0208.group(1)}" if m0208 else None
    id_9925 = f"9925:BE{m9925.group(1)}" if m9925 else None
    return id_0208, id_9925


def _collect_result_items(obj: Any) -> List[Dict[str, Any]]:
    """
    Collect "result items" from the payload by scanning common container keys and recursing.
    """
    items: List[Dict[str, Any]] = []

    if isinstance(obj, dict):
        if any(
            k in obj
            for k in [
                "participant",
                "participantIdentifier",
                "participantID",
                "participantId",
            ]
        ):
            items.append(obj)

        for k in [
            "results",
            "resultList",
            "businessCards",
            "entities",
            "items",
            "matches",
        ]:
            v = obj.get(k)
            if isinstance(v, list):
                for it in v:
                    if isinstance(it, dict):
                        items.append(it)

        for v in obj.values():
            items.extend(_collect_result_items(v))

    elif isinstance(obj, list):
        for it in obj:
            items.extend(_collect_result_items(it))

    return items


def _get_participant_ids_from_item(
    item: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract participant IDs from a single item (prefer structured fields, fallback to str()).
    """
    candidates = [
        item.get("participantIdentifier"),
        item.get("participant"),
        item.get("participantID"),
        item.get("participantId"),
    ]
    blob = " ".join([str(c) for c in candidates if c is not None]) + " " + str(item)

    m0208 = RE_PID_0208.search(blob)
    m9925 = RE_PID_9925.search(blob)

    id_0208 = f"0208:{m0208.group(1)}" if m0208 else None
    id_9925 = f"9925:BE{m9925.group(1)}" if m9925 else None
    return id_0208, id_9925


def _iter_kv(obj: Any):
    """
    Yield (key, value) pairs for all dict nodes recursively.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _iter_kv(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _iter_kv(it)


def _looks_like_company_name(s: str) -> bool:
    """
    Heuristics to reject technical values and accept plausible entity names.
    """
    if not s:
        return False
    t = s.strip()
    if len(t) < 2:
        return False
    # Reject pure digits / IDs / URLs / schemes
    if re.fullmatch(r"\d+", t):
        return False
    if "iso6523" in t.lower():
        return False
    if re.search(
        r"\b(0208:|9925:|participant|actorid-upis|http|https)\b", t, re.IGNORECASE
    ):
        return False
    # Needs at least one letter
    if not re.search(r"[A-Za-zÀ-ÿ]", t):
        return False
    return True


def _get_entity_name_from_anywhere(obj: Any) -> Optional[str]:
    """
    Find first plausible entity name by scanning keys containing 'name' (case-insensitive),
    anywhere in the provided object.
    """
    preferred_key_patterns = [
        re.compile(r"entity\s*name", re.IGNORECASE),
        re.compile(r"registered\s*name", re.IGNORECASE),
        re.compile(r"legal\s*name", re.IGNORECASE),
        re.compile(r"entityName", re.IGNORECASE),
        re.compile(r"registeredName", re.IGNORECASE),
        re.compile(r"legalName", re.IGNORECASE),
        re.compile(r"^name$", re.IGNORECASE),
    ]

    preferred: List[str] = []
    other: List[str] = []

    for k, v in _iter_kv(obj):
        if not isinstance(k, str):
            continue
        if not isinstance(v, str):
            continue
        if not _looks_like_company_name(v):
            continue

        if any(p.search(k) for p in preferred_key_patterns):
            preferred.append(v.strip())
        elif "name" in k.lower():
            other.append(v.strip())

    if preferred:
        return preferred[0]
    if other:
        return other[0]
    return None


def extract_entity_name(
    payload: Dict[str, Any], target_0208: Optional[str], target_9925: Optional[str]
) -> Optional[str]:
    """
    Choose the best entity name:
      1) from a result item matching the found IDs
      2) otherwise anywhere in payload
    """
    items = _collect_result_items(payload)

    for it in items:
        it_0208, it_9925 = _get_participant_ids_from_item(it)
        if target_9925 and it_9925 == target_9925:
            nm = _get_entity_name_from_anywhere(it)
            if nm:
                return nm
        if target_0208 and it_0208 == target_0208:
            nm = _get_entity_name_from_anywhere(it)
            if nm:
                return nm

    return _get_entity_name_from_anywhere(payload)


# ===================== Lookup =====================
def lookup_peppol_ids_for_customer(
    session: requests.Session,
    btw_raw: Any,
    cache: Dict[str, Tuple[Optional[str], Optional[str], Optional[str]]],
    log_misses: bool = True,
    debug: bool = False,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Published-only lookup:
      - Try participant exact-match for 9925 and 0208
      - If 0208 not found: use q=digits10 (UI-like search) and extract 0208
      - Entity name: extracted best-effort from payload (prefer hit matching IDs)
    """
    digits10 = normalize_btw_be(btw_raw)
    if not digits10:
        if log_misses:
            print(f"Geen/ongeldig BTWnr: '{btw_raw}'")
        return None, None, None

    if digits10 in cache:
        return cache[digits10]

    found_0208: Optional[str] = None
    found_9925: Optional[str] = None
    entity_name: Optional[str] = None
    last_error: Optional[str] = None

    # 1) participant exact-match attempts
    participant_candidates = [
        f"iso6523-actorid-upis::9925:be{digits10}",
        f"iso6523-actorid-upis::9925:{digits10}",
        f"iso6523-actorid-upis::0208:{digits10}",
    ]

    for participant_value in participant_candidates:
        try:
            payload = query_directory_by_participant(session, participant_value)

            if debug:
                with open("debug_payload_participant.json", "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)

            id_0208, id_9925 = extract_participant_ids(payload)

            if id_0208 and not found_0208:
                found_0208 = id_0208
            if id_9925 and not found_9925:
                found_9925 = id_9925

            if not entity_name:
                entity_name = extract_entity_name(payload, found_0208, found_9925)

            if found_0208 and found_9925 and entity_name:
                break

        except Exception as e:
            last_error = str(e)

        time.sleep(SLEEP_BETWEEN_CALLS_SECONDS)

    # 2) If 0208 missing, use q-search like UI
    if not found_0208:
        try:
            payload_q = query_directory_by_q(session, digits10)

            if debug:
                with open("debug_payload_q.json", "w", encoding="utf-8") as f:
                    json.dump(payload_q, f, ensure_ascii=False, indent=2)

            items = _collect_result_items(payload_q)

            # Find exact 0208 match and take its name
            for it in items:
                it_0208, it_9925 = _get_participant_ids_from_item(it)
                if it_0208 == f"0208:{digits10}":
                    found_0208 = it_0208
                    if it_9925 and not found_9925:
                        found_9925 = it_9925

                    nm = _get_entity_name_from_anywhere(it)
                    if nm:
                        entity_name = entity_name or nm
                    break

            if not entity_name:
                entity_name = extract_entity_name(payload_q, found_0208, found_9925)

        except Exception as e:
            last_error = str(e)

        time.sleep(SLEEP_BETWEEN_CALLS_SECONDS)

    if log_misses and (not found_0208 and not found_9925):
        msg = f"Geen match voor BTW '{btw_raw}' -> digits='{digits10}'."
        if last_error:
            msg += f" Laatste fout: {last_error}"
        print(msg)

    result = (found_0208, found_9925, entity_name)
    cache[digits10] = result
    return result


# ===================== Main =====================
def main(
    input_xlsx_path: str, output_xlsx_path: str, sheet_name: str = "Blad1"
) -> None:
    """
    Main function to read input Excel, perform lookups, and write output Excel.
     - Input must have columns: Kltnr, Naam, BTWnr (Adres, PC, Plaats, Telefoon, Email are ignored)
     - Output will have columns: Kltnr, Naam, BTWnr, Peppol ID 0208, Peppol ID 9925, Entity name
     - BTWnr is normalized to 10 digits (Belgian VAT/CBE) before lookup
     - Lookups are cached in-memory for efficiency
     - Progress is printed every 50 records
     - Errors and misses are logged to console
     - Debug payloads can be saved for the first record if needed
     - Output is written without index column
     - Sheet name can be specified as an optional third argument (default "Blad1")
    """
    # Force BTWnr as text to avoid losing leading zeroes
    df = pd.read_excel(
        input_xlsx_path, sheet_name=sheet_name, dtype={"BTWnr": "string"}
    )

    required_in = ["Kltnr", "Naam", "BTWnr"]
    for col in required_in:
        if col not in df.columns:
            raise ValueError(
                f"Kolom ontbreekt in input: '{col}'. Gevonden: {list(df.columns)}"
            )

    session = build_session()
    cache: Dict[str, Tuple[Optional[str], Optional[str], Optional[str]]] = {}

    out_rows: List[Dict[str, Any]] = []

    for idx, row in df.iterrows():
        kltnr = row.get("Kltnr", "")
        naam = row.get("Naam", "")
        btw = row.get("BTWnr", "")

        # Set debug=True for first record if you need to inspect payloads:
        debug = False  # e.g. (idx == 0)

        peppol_0208, peppol_9925, entity_name = lookup_peppol_ids_for_customer(
            session, btw, cache, log_misses=True, debug=debug
        )

        out_rows.append(
            {
                "Kltnr": kltnr,
                "Naam": naam,
                "BTWnr": btw,
                "Peppol ID 0208": peppol_0208 or "",
                "Peppol ID 9925": peppol_9925 or "",
                "Entity name": entity_name or "",
            }
        )

        if (idx + 1) % 50 == 0:
            print(f"Verwerkt: {idx + 1}/{len(df)}")

    out_df = pd.DataFrame(
        out_rows,
        columns=[
            "Kltnr",
            "Naam",
            "BTWnr",
            "Peppol ID 0208",
            "Peppol ID 9925",
            "Entity name",
        ],
    )
    out_df.to_excel(output_xlsx_path, index=False)
    print(f"Klaar. Output geschreven naar: {output_xlsx_path}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Gebruik: python peppol_lookup.py <input.xlsx> <output.xlsx> [sheetnaam]")
        raise SystemExit(2)

    input_path = sys.argv[1]
    output_path = sys.argv[2]
    sheet = sys.argv[3] if len(sys.argv) >= 4 else "Blad1"

    main(input_path, output_path, sheet_name=sheet)
