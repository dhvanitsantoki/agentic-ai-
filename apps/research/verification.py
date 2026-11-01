"""Deterministic verification of AI research output.

Nothing the AI reports about contacts is trusted until checked here:
* every e-mail address / phone number must literally appear on the public page cited for it;
* e-mail domains must have mail (MX or A) records;
* websites must respond;
* outbound fetches refuse private/loopback addresses (SSRF protection) and cap the download size.
"""
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings

from apps.crm.services import FREEMAIL_DOMAINS

log = logging.getLogger(__name__)

MAX_BYTES = 2_500_000
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
EMAIL_STRICT = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}$")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
CONTACT_HINTS = ("contact", "about", "impressum", "kontakt", "contacto", "contato", "contatti", "team", "enquir",
                 "reach-us", "get-in-touch", "company", "imprint", "mentions-legales", "nous-contacter")


class FetchBlocked(Exception):
    pass


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int | None = None
    final_url: str = ""
    html: str = ""
    text: str = ""
    title: str = ""
    error: str = ""
    links: list = field(default_factory=list)


def _host_is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def _check_url(url: str) -> str:
    if not url:
        raise FetchBlocked("empty URL")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchBlocked(f"unsupported URL: {url}")
    if not _host_is_public(parsed.hostname):
        raise FetchBlocked(f"host not public or not resolvable: {parsed.hostname}")
    return url


def fetch(url: str, *, timeout: float | None = None) -> FetchResult:
    """Download a public web page (redirects re-checked for SSRF)."""
    try:
        url = _check_url(url)
    except FetchBlocked as exc:
        return FetchResult(url=url, ok=False, error=str(exc))
    headers = {"User-Agent": settings.HTTP_USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
               "Accept-Language": "en;q=0.9,*;q=0.5"}
    session = requests.Session()
    current = url
    try:
        for _ in range(5):
            resp = session.get(current, headers=headers, timeout=timeout or settings.HTTP_TIMEOUT_SECONDS,
                               allow_redirects=False, stream=True)
            if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                nxt = urljoin(current, resp.headers.get("Location", ""))
                current = _check_url(nxt)
                resp.close()
                continue
            break
        content = b""
        for chunk in resp.iter_content(65536):
            content += chunk
            if len(content) > MAX_BYTES:
                break
        resp.close()
    except FetchBlocked as exc:
        return FetchResult(url=url, ok=False, error=str(exc))
    except requests.RequestException as exc:
        return FetchResult(url=url, ok=False, error=exc.__class__.__name__)
    encoding = resp.encoding or "utf-8"
    html = content.decode(encoding, errors="replace")
    return build_result(url, resp.status_code, current, html, resp.headers.get("Content-Type", ""))


def build_result(url: str, status: int, final_url: str, html: str, content_type: str = "text/html") -> FetchResult:
    """Parse downloaded content into a FetchResult (shared by fetch() and tests)."""
    ok = 200 <= status < 400
    result = FetchResult(url=url, ok=ok, status=status, final_url=final_url or url, html=html)
    if "html" in content_type or "<html" in html[:2000].lower():
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        result.title = (soup.title.string or "").strip()[:300] if soup.title and soup.title.string else ""
        result.text = soup.get_text(" ", strip=True)
        result.links = [(a.get("href") or "", a.get_text(" ", strip=True)) for a in soup.find_all("a")]
    else:
        result.text = html
    if not ok:
        result.error = f"HTTP {status}"
    return result


class PageCache:
    """Per-run cache so each page is fetched at most once."""

    def __init__(self, fetcher=None, mx_resolver=None):
        self._pages: dict[str, FetchResult] = {}
        self._mx: dict[str, tuple[bool, list]] = {}
        self.fetcher = fetcher or fetch
        self.mx_resolver = mx_resolver or mx_lookup

    def get(self, url: str) -> FetchResult:
        key = (url or "").strip()
        if key not in self._pages:
            self._pages[key] = self.fetcher(key)
        return self._pages[key]

    def mx(self, domain: str):
        if domain not in self._mx:
            self._mx[domain] = self.mx_resolver(domain)
        return self._mx[domain]


_DEOBFUSCATE = [
    (re.compile(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", re.I), "@"),
    (re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.I), "."),
    (re.compile(r"\s+at\s+(?=[a-z0-9\-]+\s*(\.|\[dot\]|\(dot\)|\sdot\s))", re.I), "@"),
    (re.compile(r"\s+dot\s+(?=[a-z]{2,24}\b)", re.I), "."),
    (re.compile(r"&#64;|&commat;|＠"), "@"),
]


def deobfuscate(text: str) -> str:
    s = text or ""
    for pattern, repl in _DEOBFUSCATE:
        s = pattern.sub(repl, s)
    return s


def _page_corpus(page: FetchResult) -> str:
    hrefs = " ".join(h for h, _ in page.links if h.lower().startswith(("mailto:", "tel:")))
    return deobfuscate(f"{page.text} {hrefs} {page.html if not page.text else ''}").lower()


def extract_emails(page: FetchResult) -> set[str]:
    found = set()
    for m in EMAIL_RE.findall(_page_corpus(page)):
        e = m.strip(".").lower()
        if e.endswith(IMAGE_SUFFIXES) or "example." in e or e.startswith(("u00", "x22")):
            continue
        if EMAIL_STRICT.match(e):
            found.add(e)
    return found


def digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


PHONE_RE = re.compile(r"(?:\+|00)?\d[\d\s().\-/]{7,20}\d")


def extract_phones(page: FetchResult) -> set[str]:
    out = set()
    tel_links = [h[4:] for h, _ in page.links if h.lower().startswith("tel:")]
    candidates = tel_links + [m for m in PHONE_RE.findall(page.text or "") if m.strip().startswith(("+", "00"))]
    for raw in candidates:
        d = digits(raw)
        if 8 <= len(d) <= 15:
            out.add(raw.strip())
    return out


def email_on_page(email: str, page: FetchResult) -> bool:
    return bool(email) and page.ok and email.strip().lower() in _page_corpus(page)


def phone_on_page(phone: str, page: FetchResult) -> bool:
    d = digits(phone)
    if len(d) < 7 or not page.ok:
        return False
    corpus = digits(page.text) + " " + " ".join(digits(h) for h, _ in page.links if h.lower().startswith("tel:"))
    # compare the national significant part (last 8 digits) so "+254 20 ..." matches "020 ..."
    return d[-8:] in corpus


def text_on_page(snippet: str, page: FetchResult, min_len: int = 12) -> bool:
    """Loose check that a quoted snippet (or a person's name) appears on the page."""
    norm = lambda s: re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (s or "").lower())).strip()  # noqa: E731
    target = norm(snippet)
    if not target or not page.ok:
        return False
    body = norm(page.text or page.html)
    if len(target) <= min_len * 4:
        return target in body
    # long quotes: accept if a 48-character window of the quote appears verbatim
    return any(target[i:i + 48] in body for i in range(0, max(1, len(target) - 48), 24))


def mx_lookup(domain: str) -> tuple[bool, list]:
    try:
        import dns.exception
        import dns.resolver
    except ImportError:  # pragma: no cover
        return True, []
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 6
    try:
        answers = resolver.resolve(domain, "MX")
        hosts = sorted(str(r.exchange).rstrip(".") for r in answers)
        return bool(hosts), hosts
    except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
        return False, []
    except dns.resolver.NoAnswer:
        try:
            resolver.resolve(domain, "A")
            return True, ["(A record)"]
        except dns.exception.DNSException:
            return False, []
    except dns.exception.DNSException:
        return False, ["(dns error)"]


def valid_email_syntax(email: str) -> bool:
    return bool(email) and len(email) <= 254 and bool(EMAIL_STRICT.match(email))


def verify_email(email: str, source_url: str, cache: PageCache) -> dict:
    """Return the verification record for one e-mail address."""
    email = (email or "").strip().lower()
    checks = {"email": email, "source_url": source_url, "syntax": valid_email_syntax(email)}
    if not checks["syntax"]:
        checks["result"] = "invalid"
        return checks
    page = cache.get(source_url) if source_url else None
    checks["source_reachable"] = bool(page and page.ok)
    checks["found_on_source"] = bool(page and email_on_page(email, page))
    domain = email.split("@")[1]
    mx_ok, hosts = cache.mx(domain)
    checks["mx"] = mx_ok
    checks["mx_hosts"] = hosts[:5]
    if not checks["found_on_source"]:
        checks["result"] = "not_found_on_source"
    elif not mx_ok:
        checks["result"] = "invalid"
    else:
        checks["result"] = "verified"
    return checks


def check_website(url: str, cache: PageCache) -> dict:
    page = cache.get(url)
    return {"url": url, "ok": page.ok, "status": page.status, "final_url": page.final_url, "title": page.title,
            "error": page.error}


def discover_contact_pages(page: FetchResult, limit: int = 3) -> list[str]:
    if not page.ok:
        return []
    base = page.final_url or page.url
    base_host = (urlparse(base).hostname or "").lower().removeprefix("www.")
    out = []
    for href, label in page.links:
        h = (href or "").strip()
        if not h or h.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base, h).split("#")[0]
        host = (urlparse(absolute).hostname or "").lower().removeprefix("www.")
        if host != base_host:
            continue
        hay = f"{absolute} {label}".lower()
        if any(k in hay for k in CONTACT_HINTS) and absolute not in out and absolute.rstrip("/") != base.rstrip("/"):
            out.append(absolute)
        if len(out) >= limit:
            break
    return out


def crawl_published_contacts(website: str, cache: PageCache, max_pages: int = 3) -> dict:
    """E-mails/phones the company itself publishes on its homepage and contact pages."""
    home = cache.get(website)
    result = {"pages": [], "emails": {}, "phones": {}}
    if not home.ok:
        return result
    pages = [home] + [cache.get(u) for u in discover_contact_pages(home, max_pages)]
    company_host = (urlparse(home.final_url or website).hostname or "").lower().removeprefix("www.")
    for p in pages:
        if not p.ok:
            continue
        src = p.final_url or p.url
        result["pages"].append(src)
        for e in extract_emails(p):
            # keep addresses on the company's own domain (or sub-domain) only
            dom = e.split("@")[1]
            own = dom == company_host or dom.endswith("." + company_host) or company_host.endswith("." + dom)
            if own or dom in FREEMAIL_DOMAINS:
                result["emails"].setdefault(e, src)
        for ph in extract_phones(p):
            result["phones"].setdefault(ph, src)
    return result
