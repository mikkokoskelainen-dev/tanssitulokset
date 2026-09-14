#!/usr/bin/env python3
"""
Lataa tanssikilpailujen tulossivut osoitteesta results.dancesport.fi
raakana HTML:na hakemistoon data/raw/.

Ei jasenna mitaan - tallentaa vain tavut sellaisenaan, jotta parserin
voi myohemmin korjata ja ajaa uudelleen ilman uutta latausta.

Kaytto:
    python3 scrape.py              # kayy lapi kilpailut 1-1100
    python3 scrape.py 890 1000     # vain tietty valilla
"""

import re
import sys
import time
import pathlib
import urllib.error
import urllib.request
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

BASE = "https://results.dancesport.fi"
OUT = pathlib.Path("data/raw")

# Kohteliaisuusasetukset: yksi pyynto sekunnissa ja tunnistautuva User-Agent,
# jossa on omat yhteystietosi. VAIHDA sahkopostiosoite omaksesi.
DELAY = 1.0
UA = "tanssitulokset-analyysi/0.1 (+mikko@esimerkki.fi)"

# Loytaa kaikki linkit, jotka osoittavat index.html-sivulle (luokkasivut).
LINK_RE = re.compile(rb'href\s*=\s*["\']([^"\']*?index\.html)["\']', re.IGNORECASE)


def encode_url(url):
    """TPS kayttaa kansionimissa valilyonteja ja aakkosia. Ne pitaa
    muuntaa prosenttikoodiksi (%20), ennen kuin urllib suostuu hakemaan."""
    parts = urlsplit(url)
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        quote(parts.path, safe="/%"),
        quote(parts.query, safe="=&%"),
        "",
    ))


def fetch(url):
    """Hakee URLin. Palauttaa tavut, tai None jos sivua ei ole."""
    req = urllib.request.Request(encode_url(url), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (404, 403):
            return None
        raise
    except urllib.error.URLError as e:
        print(f"    verkkovirhe {url}: {e.reason}")
        return None


def save(relpath, data):
    path = OUT / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def exists(relpath):
    return (OUT / relpath).exists()


def class_links(html, comp_url):
    """Poimii kilpailun etusivulta luokkasivujen osoitteet, jarjestyksessa."""
    seen = []
    for match in LINK_RE.findall(html):
        href = match.decode("utf-8", errors="replace")
        full = urljoin(comp_url, href)
        # Vain taman kilpailun alla olevat, ei kilpailun oma etusivu
        if not full.startswith(comp_url):
            continue
        tail = full[len(comp_url):].strip("/")
        if tail.lower() == "index.html":
            continue
        if full not in seen:
            seen.append(full)
    return seen


def marking_urls(class_url):
    """TPS tuottaa tiedostonimet kahdella eri tavalla version mukaan."""
    base = class_url.rsplit("/", 1)[0] + "/"
    return [base + "markingtable.html", base + "MarkingTable.html"]


def scrape_competition(comp_id):
    comp_url = f"{BASE}/{comp_id}/"
    index_path = f"{comp_id}/index.html"

    if exists(index_path):
        html = (OUT / index_path).read_bytes()
        print(f"[{comp_id}] etusivu jo levylla")
    else:
        html = fetch(comp_url + "index.html")
        time.sleep(DELAY)
        if html is None:
            return False
        save(index_path, html)
        print(f"[{comp_id}] etusivu ladattu")

    links = class_links(html, comp_url)
    if not links:
        print(f"[{comp_id}] ei luokkia - ohitetaan")
        return True

    for n, class_url in enumerate(links, start=1):
        target = f"{comp_id}/{n:02d}_markingtable.html"
        if exists(target):
            continue
        for candidate in marking_urls(class_url):
            data = fetch(candidate)
            time.sleep(DELAY)
            if data is not None:
                save(target, data)
                print(f"[{comp_id}] luokka {n}/{len(links)} tallennettu")
                break
        else:
            print(f"[{comp_id}] luokka {n}: merkintataulukkoa ei loytynyt")

    return True


def main():
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    end = int(sys.argv[2]) if len(sys.argv) > 2 else 1100

    found = 0
    for comp_id in range(start, end + 1):
        if scrape_competition(comp_id):
            found += 1

    print(f"\nValmis. Kilpailuja loytyi {found} kpl valilta {start}-{end}.")
    print(f"Tiedostot: {OUT.resolve()}")


if __name__ == "__main__":
    main()
