#!/usr/bin/env python3
import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup


ARCHIVE_BASE = "https://web.archive.org/web/"
OUR_CLINICS_URL = (
    "https://web.archive.org/web/20250708180027/https://www.myfootdr.com.au/our-clinics/"
)


@dataclass
class ClinicRecord:
    name: str
    address: str
    email: str
    phone: str
    services: str


def http_get(url: str, timeout: int = 30) -> requests.Response:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp


def find_region_urls() -> List[str]:
    resp = http_get(OUR_CLINICS_URL)
    soup = BeautifulSoup(resp.text, "lxml")
    region_urls: Set[str] = set()
    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if "/our-clinics/regions/" in href:
            # Wayback URLs might miss the timestamp for some anchors; ensure absolute archive URL
            if href.startswith("http"):
                region_urls.add(href)
            else:
                region_urls.add(ARCHIVE_BASE + href.lstrip("/"))

    # Also include Sunshine Coast page ID variant if present (menu-item is a page)
    # Already captured by the anchor scan above; no special-case needed.
    return sorted(region_urls)


def find_clinic_urls(region_url: str) -> List[str]:
    resp = http_get(region_url)
    soup = BeautifulSoup(resp.text, "lxml")
    clinic_urls: Set[str] = set()

    # Primary: links inside the regional clinics grid
    for a in soup.select(".regional-clinics a[href]"):
        href: str = a.get("href", "")
        if "/our-clinics/" in href:
            if href.startswith("http"):
                clinic_urls.add(href)
            else:
                clinic_urls.add(ARCHIVE_BASE + href.lstrip("/"))

    # Fallback: any anchor pointing to a clinic page under /our-clinics/
    if not clinic_urls:
        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            if "/our-clinics/" in href and not href.rstrip("/").endswith("/our-clinics"):
                if href.startswith("http"):
                    clinic_urls.add(href)
                else:
                    clinic_urls.add(ARCHIVE_BASE + href.lstrip("/"))

    return sorted(clinic_urls)


def extract_text(elem) -> str:
    if not elem:
        return ""
    return " ".join(elem.get_text(" ", strip=True).split())


def parse_json_ld(soup: BeautifulSoup) -> Optional[dict]:
    # Collect all JSON-LD blocks and find the LocalBusiness object
    for script in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        try:
            data = json.loads(script.string or script.text or "{}")
        except json.JSONDecodeError:
            # Some pages have multiple JSON objects concatenated or HTML comments; try to sanitize
            try:
                cleaned = (script.string or script.text or "").strip()
                # Attempt to load array-wrapped JSON-LD
                if cleaned.startswith("[") and cleaned.endswith("]"):
                    data = json.loads(cleaned)
                else:
                    continue
            except Exception:
                continue

        # JSON-LD may be a dict or a list
        candidates: List[dict] = []
        if isinstance(data, dict):
            candidates = [data]
        elif isinstance(data, list):
            candidates = [x for x in data if isinstance(x, dict)]

        for obj in candidates:
            obj_type = obj.get("@type")
            if not obj_type:
                continue
            # @type may be a string or a list
            types = {obj_type} if isinstance(obj_type, str) else set(obj_type)
            if any(t in ("LocalBusiness", "Organization") for t in types):
                # Prefer LocalBusiness over Organization if both present
                if "LocalBusiness" in types:
                    return obj
                # else keep as fallback
                lb = obj.copy()
                # do not return immediately; there may be a LocalBusiness ahead
                # store as fallback
                return lb
    return None


def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    # Remove spaces inside parentheses, keep formatting mostly
    return re.sub(r"\s+", " ", phone).strip()


def normalize_services(services: List[str]) -> str:
    # Deduplicate preserving order, title-case consistently but keep acronyms
    seen = set()
    ordered: List[str] = []
    for s in services:
        s_clean = re.sub(r"\s+", " ", s).strip()
        if not s_clean:
            continue
        key = s_clean.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(s_clean)
    return ", ".join(ordered)


def extract_services(soup: BeautifulSoup) -> List[str]:
    services: List[str] = []
    # Common section: list items linking to /our-services/
    for a in soup.select('a[href*="/our-services/"]'):
        text = extract_text(a)
        if text:
            services.append(text)

    # Fallback: JSON-LD makesOffer urls -> derive name from path segment
    ld = parse_json_ld(soup)
    if ld and isinstance(ld.get("makesOffer"), list):
        for offer in ld["makesOffer"]:
            url = offer.get("url") if isinstance(offer, dict) else None
            if not url:
                continue
            # derive last path segment as name
            m = re.search(r"/our-services/([^/?#]+)/?", url)
            if m:
                slug = m.group(1)
                slug = slug.replace("-", " ").strip()
                if slug:
                    services.append(slug.title())

    return services


def extract_clinic_fields(clinic_url: str) -> Optional[ClinicRecord]:
    try:
        resp = http_get(clinic_url)
    except Exception as e:
        sys.stderr.write(f"Failed to fetch clinic page: {clinic_url} -> {e}\n")
        return None

    soup = BeautifulSoup(resp.text, "lxml")

    # JSON-LD primary source
    ld = parse_json_ld(soup) or {}

    name = ld.get("name") or extract_text(soup.find("h1"))

    # Address
    address_text = ""
    addr = ld.get("address")
    if isinstance(addr, dict):
        parts = [
            addr.get("streetAddress") or "",
            addr.get("addressLocality") or "",
            addr.get("addressRegion") or "",
            addr.get("postalCode") or "",
        ]
        address_text = ", ".join([p for p in parts if p]).strip(", ")
    if not address_text:
        # Fallback: look for address blocks
        # Sometimes in a map/address container, but JSON-LD is usually present.
        address_candidate = soup.select_one(".clinic-address, .address, address")
        address_text = extract_text(address_candidate)

    # Phone
    phone = ld.get("telephone") or ""
    if not phone:
        tel_anchor = None
        for a in soup.select('a[href^="tel:"]'):
            tel = a.get("href", "")[4:]
            if tel and re.sub(r"\D", "", tel) != "1800366837":
                tel_anchor = a
                break
        if tel_anchor:
            phone = extract_text(tel_anchor)
    phone = normalize_phone(phone)

    # Email
    email = ld.get("email") or ""
    if not email:
        mail = soup.select_one('a[href^="mailto:"]')
        if mail:
            href = mail.get("href", "")
            email = href.split(":", 1)[-1]

    # Services
    services_list = extract_services(soup)
    services = normalize_services(services_list)

    return ClinicRecord(
        name=name or "",
        address=address_text or "",
        email=(email or "").strip(),
        phone=phone,
        services=services,
    )


def scrape_all() -> List[ClinicRecord]:
    region_urls = find_region_urls()
    if not region_urls:
        raise RuntimeError("No region URLs found.")

    # Gather unique clinic URLs from all regions
    clinic_url_set: Set[str] = set()
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_to_region = {pool.submit(find_clinic_urls, u): u for u in region_urls}
        for fut in as_completed(future_to_region):
            try:
                urls = fut.result()
                for cu in urls:
                    clinic_url_set.add(cu)
            except Exception as e:
                sys.stderr.write(f"Failed to parse region {future_to_region[fut]} -> {e}\n")

    clinic_urls = sorted(clinic_url_set)
    if not clinic_urls:
        raise RuntimeError("No clinic URLs found.")

    records: List[ClinicRecord] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        future_to_url = {pool.submit(extract_clinic_fields, url): url for url in clinic_urls}
        for fut in as_completed(future_to_url):
            url = future_to_url[fut]
            try:
                rec = fut.result()
                if rec and rec.name:
                    records.append(rec)
            except Exception as e:
                sys.stderr.write(f"Failed to extract clinic {url} -> {e}\n")

    # Sort by clinic name for determinism
    records.sort(key=lambda r: r.name.lower())
    return records


def write_csv(path: str, records: List[ClinicRecord]) -> None:
    fieldnames = [
        "Name of Clinic",
        "Address",
        "Email",
        "Phone",
        "Services",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            writer.writerow(
                {
                    "Name of Clinic": r.name,
                    "Address": r.address,
                    "Email": r.email,
                    "Phone": r.phone,
                    "Services": r.services,
                }
            )


def main():
    out_csv = "/workspace/myfootdr_clinics.csv"
    try:
        records = scrape_all()
    except Exception as e:
        sys.stderr.write(f"Error during scrape: {e}\n")
        sys.exit(1)

    write_csv(out_csv, records)
    print(f"Wrote {len(records)} clinics to {out_csv}")


if __name__ == "__main__":
    main()

