#!/usr/bin/env python3
import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Optional, Set
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


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


def http_get_html(url: str, timeout: int = 30) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
    }
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            charset_match = re.search(r"charset=([\w-]+)", content_type, flags=re.I)
            charset = charset_match.group(1) if charset_match else "utf-8"
            return resp.read().decode(charset, errors="replace")
    except (HTTPError, URLError) as e:
        raise RuntimeError(f"HTTP error for {url}: {e}")


def find_region_urls() -> List[str]:
    html = http_get_html(OUR_CLINICS_URL)
    region_urls: Set[str] = set()
    for href in re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I):
        if "/our-clinics/regions/" in href:
            if href.startswith("http"):
                region_urls.add(href)
            elif href.startswith("/web/"):
                region_urls.add("https://web.archive.org" + href)
            else:
                # Fallback: assume original site path
                region_urls.add(ARCHIVE_BASE + href.lstrip("/"))
    return sorted(region_urls)


def find_clinic_urls(region_url: str) -> List[str]:
    html = http_get_html(region_url)
    clinic_urls: Set[str] = set()

    # Look for anchors within the regional clinics table rows first
    # Heuristic: capture all our-clinics page links on region page
    for href in re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I):
        if "/our-clinics/" in href and not href.rstrip("/").endswith("/our-clinics"):
            if href.startswith("http"):
                clinic_urls.add(href)
            elif href.startswith("/web/"):
                clinic_urls.add("https://web.archive.org" + href)
            else:
                clinic_urls.add(ARCHIVE_BASE + href.lstrip("/"))

    return sorted(clinic_urls)


def strip_tags(text: str) -> str:
    # Remove HTML tags and condense whitespace
    text = re.sub(r"<\s*br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def parse_json_ld(html: str) -> Optional[dict]:
    # Find all <script type="application/ld+json"> blocks and parse JSON
    json_ld_blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.I | re.S,
    )
    fallback: Optional[dict] = None
    for block in json_ld_blocks:
        content = block.strip()
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Try to fix common issues: stray comments or multiple objects
            content_clean = re.sub(r"/\*.*?\*/", "", content, flags=re.S)
            try:
                data = json.loads(content_clean)
            except json.JSONDecodeError:
                continue

        objs: List[dict] = []
        if isinstance(data, dict):
            objs = [data]
        elif isinstance(data, list):
            objs = [x for x in data if isinstance(x, dict)]

        for obj in objs:
            obj_type = obj.get("@type")
            if not obj_type:
                continue
            types = {obj_type} if isinstance(obj_type, str) else set(obj_type)
            if "LocalBusiness" in types:
                return obj
            if any(t in ("Organization",) for t in types):
                fallback = fallback or obj
    return fallback


def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    return re.sub(r"\s+", " ", phone).strip()


def normalize_services(services: List[str]) -> str:
    seen_lower: Set[str] = set()
    ordered: List[str] = []
    for s in services:
        s_clean = re.sub(r"\s+", " ", s).strip()
        if not s_clean:
            continue
        key = s_clean.lower()
        if key not in seen_lower:
            seen_lower.add(key)
            ordered.append(s_clean)
    return ", ".join(ordered)


def extract_services(html: str, ld: Optional[dict]) -> List[str]:
    services: List[str] = []
    # From anchors text
    for m in re.finditer(r'<a[^>]+href=["\'][^"\']*/our-services/[^"\']*["\'][^>]*>(.*?)</a>', html, flags=re.I | re.S):
        text = strip_tags(m.group(1))
        if text:
            services.append(text)

    # From JSON-LD makesOffer
    if ld and isinstance(ld.get("makesOffer"), list):
        for offer in ld["makesOffer"]:
            if isinstance(offer, dict):
                url = offer.get("url")
                if not url:
                    continue
                m = re.search(r"/our-services/([^/?#]+)/?", url)
                if m:
                    slug = m.group(1).replace("-", " ").strip()
                    if slug:
                        services.append(slug.title())
    return services


def extract_clinic_fields(clinic_url: str) -> Optional[ClinicRecord]:
    try:
        html = http_get_html(clinic_url)
    except Exception as e:
        sys.stderr.write(f"Failed to fetch clinic page: {clinic_url} -> {e}\n")
        return None

    ld = parse_json_ld(html) or {}

    # Name
    name = ld.get("name") or ""
    if not name:
        # Fallback: try to read <h1>...</h1>
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, flags=re.I | re.S)
        if m:
            name = strip_tags(m.group(1))

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

    # Phone
    phone = ld.get("telephone") or ""
    if not phone:
        m_tel = re.search(r'href=["\']tel:([^"\']+)["\']', html, flags=re.I)
        if m_tel:
            tel_val = m_tel.group(1)
            if re.sub(r"\D", "", tel_val) != "1800366837":
                phone = tel_val
    phone = normalize_phone(phone)

    # Email
    email = ld.get("email") or ""
    if not email:
        m_mail = re.search(r'href=["\']mailto:([^"\']+)["\']', html, flags=re.I)
        if m_mail:
            email = m_mail.group(1)

    # Services
    services_list = extract_services(html, ld)
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

