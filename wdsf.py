#!/usr/bin/env python3
"""
Hakee WDSF:n kansainvalisista kilpailuista SUOMALAISET tanssijat ja tuomarit
ja kirjoittaa niista kevyen tietokannan data/ulkomaat.sqlite.

Kaikki muut maat heitetaan pois heti jasennysvaiheessa, joten kanta pysyy
pienena. Raaka HTML tallennetaan kansioon data/wdsf_raw, jolloin jasentimen
korjaus voidaan ajaa uudelleen ilman uutta latausta.

Kaytto:
    python3 wdsf.py                       # kuluva vuosi: hae + jasenna
    python3 wdsf.py 2024-01 2026-12       # tietty kuukausivali: hae + jasenna
    python3 wdsf.py hae 2024-01 2026-12   # pelkka lataus
    python3 wdsf.py jasenna               # pelkka kannan rakennus levylta

Ilman parametreja haetaan kuluva vuosi, ja tammi-helmikuussa myos
edellinen vuosi. Vuosilukua ei siis tarvitse paivittaa kasin.
"""

import datetime
import html
import pathlib
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit, urlunsplit

BASE = "https://www.worlddancesport.org"
RAW = pathlib.Path("data/wdsf_raw")
DB = pathlib.Path("data/ulkomaat.sqlite")

MAA = "FIN"          # poimittava maakoodi
DELAY = 1.0
UA = "tanssitulokset-analyysi/0.1 (+mikko@esimerkki.fi)"

SCHEMA = """
CREATE TABLE kilpailu (
    id           INTEGER PRIMARY KEY,   -- WDSF:n kilpailunumero (= yksi luokka)
    nimi         TEXT,                  -- "WDSF Open Latin Senior I"
    kaupunki     TEXT,
    maa          TEXT,
    pvm          TEXT,                  -- ISO
    url          TEXT,
    tapahtuma_id INTEGER,               -- WDSF:n tapahtumanumero
    tapahtuma    TEXT,                  -- "Platja d'Aro, Spain 30.10.-1.11.2026"
    tapahtuma_url TEXT
);

CREATE TABLE sijoitus (
    kilpailu_id INTEGER REFERENCES kilpailu(id),
    sija        INTEGER,
    sija_raw    TEXT,
    pari        TEXT,
    tanssija1   TEXT,
    tanssija2   TEXT
);

CREATE TABLE tuomarointi (
    kilpailu_id INTEGER REFERENCES kilpailu(id),
    nimi        TEXT,
    rooli       TEXT                    -- Adjudicators, Chairperson, Scrutiny...
);

CREATE TABLE meta (
    avain      TEXT PRIMARY KEY,
    arvo       TEXT
);

CREATE INDEX i_sijoitus_kilpailu ON sijoitus(kilpailu_id);
CREATE INDEX i_tuomarointi_nimi  ON tuomarointi(nimi);
"""

KUUKAUDET = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


# --------------------------------------------------------------------------
# Lataus
# --------------------------------------------------------------------------

def encode_url(url):
    o = urlsplit(url)
    return urlunsplit((o.scheme, o.netloc, quote(o.path, safe="/%"),
                       quote(o.query, safe="=&%?"), ""))


def fetch(url):
    req = urllib.request.Request(encode_url(url), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (404, 403, 410):
            return None
        raise
    except urllib.error.URLError as e:
        print(f"    verkkovirhe {url}: {e.reason}")
        return None


def hae_sivu(polku, url, aina_tuore=False):
    """Palauttaa sivun tavut: levylta jos on, muuten verkosta.

    aina_tuore=True hakee sivun joka kerta uudelleen. Sita tarvitaan
    kalenteri- ja tapahtumasivuille, joille ilmestyy uusia kilpailuja:
    jos ne luettaisiin levylta, uudet tulokset eivat loytyisi koskaan.
    Yksittaisen kilpailun tulos- ja tuomarisivut eivat enaa muutu."""
    tiedosto = RAW / polku
    if tiedosto.exists() and not aina_tuore:
        return tiedosto.read_bytes(), False
    data = fetch(url)
    time.sleep(DELAY)
    if data is None:
        # Verkosta ei saatu; kaytetaan levylla olevaa versiota jos sellainen on
        if tiedosto.exists():
            return tiedosto.read_bytes(), False
        return None, True
    tiedosto.parent.mkdir(parents=True, exist_ok=True)
    tiedosto.write_bytes(data)
    return data, True


def kuukaudet_valilla(alku, loppu):
    av, ak = (int(x) for x in alku.split("-"))
    lv, lk = (int(x) for x in loppu.split("-"))
    while (av, ak) <= (lv, lk):
        yield av, ak
        ak += 1
        if ak > 12:
            ak, av = 1, av + 1


def hae(alku, loppu):
    kilpailut = {}          # cid -> Ranking-sivun polku sellaisena kuin se sivulla on
    tapahtumat = {}         # tid -> tapahtumasivun polku

    for vuosi, kk in kuukaudet_valilla(alku, loppu):
        data, uusi = hae_sivu(f"calendar/{vuosi}-{kk:02d}.html",
                              f"{BASE}/Calendar/Results?Month={kk}&Year={vuosi}",
                              aina_tuore=True)
        if data is None:
            print(f"[{vuosi}-{kk:02d}] kalenteria ei saatu")
            continue
        teksti = data.decode("utf-8", errors="replace")
        loydetyt = {tid: polku for polku, tid in re.findall(
            r'href="(/Events/[^"#?]*?-(\d+))(?:[#?][^"]*)?"', teksti)}
        tapahtumat.update(loydetyt)
        print(f"[{vuosi}-{kk:02d}] {len(loydetyt)} tapahtumaa")

    for tid in sorted(tapahtumat, key=int):
        data, _ = hae_sivu(f"events/{tid}.html", BASE + tapahtumat[tid],
                           aina_tuore=True)
        if data is None:
            continue
        teksti = data.decode("utf-8", errors="replace")
        for polku_r, cid in re.findall(
                r'href="(/Competitions/Ranking/[^"#?]*?-(\d+))(?:[#?][^"]*)?"', teksti):
            kilpailut[int(cid)] = polku_r

    print(f"\nTapahtumia {len(tapahtumat)}, kilpailuja {len(kilpailut)}. Haetaan tulokset.")

    for n, cid in enumerate(sorted(kilpailut), start=1):
        polku_r = kilpailut[cid]
        polku_o = polku_r.replace("/Competitions/Ranking/", "/Competitions/Officials/")
        hae_sivu(f"ranking/{cid}.html", BASE + polku_r)
        hae_sivu(f"officials/{cid}.html", BASE + polku_o)
        if n % 25 == 0:
            print(f"  {n}/{len(kilpailut)} kilpailua haettu")

    print(f"Valmis. Raakasivut: {RAW.resolve()}")


# --------------------------------------------------------------------------
# Jasennys
# --------------------------------------------------------------------------

def teksti_ilman_tageja(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def lue_otsikko(sivu):
    """<title>Ranking of the WDSF Open Latin Senior I in Platja d'Aro -
    Spain on 1 November 2026</title> -> osat."""
    m = re.search(r"<title>(.*?)</title>", sivu, re.S)
    if not m:
        return None
    t = html.unescape(m.group(1)).strip()
    m = re.match(r"(?:Ranking of the|Officials of)\s+(.*?)\s+in\s+(.*?)\s+-\s+"
                 r"(.*?)\s+on\s+(\d{1,2})\s+(\w+)\s+(\d{4})", t)
    if not m:
        return None
    nimi, kaupunki, maa, paiva, kknimi, vuosi = m.groups()
    kk = KUUKAUDET.get(kknimi.lower())
    pvm = f"{vuosi}-{kk:02d}-{int(paiva):02d}" if kk else None
    return {"nimi": nimi, "kaupunki": kaupunki, "maa": maa, "pvm": pvm}


def lue_url(sivu):
    """Sivun <base href> kertoo kilpailun oikean osoitteen."""
    m = re.search(r'<base href="([^"]+)"', sivu)
    return m.group(1).rstrip("/") if m else None


def lue_tapahtuma(sivu):
    """Sivulla on linkki tapahtumaan, esim.
    /Events/Platja-dAro-Spain-30102026-01112026-8626 -> numero ja nimi."""
    m = re.search(r'href="(/Events/([A-Za-z0-9\-]*?)-(\d{8})(?:-(\d{8}))?-(\d+))"', sivu)
    if not m:
        return None, None, None
    polku, paikka, alku, loppu, tid = m.groups()
    paikka = paikka.replace("-", " ").strip()

    def pv(p):
        return f"{int(p[0:2])}.{int(p[2:4])}." if p else ""


    if loppu and loppu != alku:
        # Sama vuosi: "30.10.–1.11.2026", eri vuosi: molemmat vuodet nakyviin
        if alku[4:8] == loppu[4:8]:
            aika = f"{pv(alku)}–{pv(loppu)}{loppu[4:8]}"
        else:
            aika = f"{pv(alku)}{alku[4:8]}–{pv(loppu)}{loppu[4:8]}"
    else:
        aika = f"{pv(alku)}{alku[4:8]}"
    return int(tid), f"{paikka}, {aika}".strip(), BASE + polku


def lue_sijoitukset(sivu):
    """Poimii tulostaulukon rivit, joiden maakoodi on MAA."""
    out = []
    for rivi in re.findall(r"<tr>(.*?)</tr>", sivu, re.S):
        maa = re.search(r'class="ranking__country">\s*([A-Z]{2,3})\s*<', rivi)
        if not maa or maa.group(1) != MAA:
            continue
        sija_osa = re.search(r'class="ranking__rank">(.*?)</td>', rivi, re.S)
        pari_osa = re.search(r'class="ranking__couple">(.*?)</td>', rivi, re.S)
        if not pari_osa:
            continue
        sija_raw = teksti_ilman_tageja(sija_osa.group(1)) if sija_osa else ""
        nimet = [teksti_ilman_tageja(x) for x in
                 re.findall(r"<a [^>]*>(.*?)</a>", pari_osa.group(1), re.S)]
        if not nimet:
            nimet = [teksti_ilman_tageja(pari_osa.group(1))]
        numero = re.match(r"(\d+)", sija_raw)
        out.append({
            "sija": int(numero.group(1)) if numero else None,
            "sija_raw": sija_raw,
            "pari": " / ".join(nimet),
            "tanssija1": nimet[0] if nimet else None,
            "tanssija2": nimet[1] if len(nimet) > 1 else None,
        })
    return out


def lue_tuomarit(sivu):
    """Poimii Adjudicators / Chairperson / ... -taulukoista MAA-maan henkilot."""
    out = []
    osat = re.split(r"<h2>(.*?)</h2>", sivu)
    for i in range(1, len(osat) - 1, 2):
        rooli = teksti_ilman_tageja(osat[i])
        for rivi in re.findall(r"<tr>(.*?)</tr>", osat[i + 1], re.S):
            solut = re.findall(r"<td[^>]*>(.*?)</td>", rivi, re.S)
            if len(solut) < 2:
                continue
            nimi = teksti_ilman_tageja(solut[0])
            maa = teksti_ilman_tageja(solut[1])
            if maa == MAA and nimi:
                out.append({"nimi": nimi, "rooli": rooli})
    return out


def jasenna():
    if DB.exists():
        DB.unlink()
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)

    tiedostot = sorted((RAW / "ranking").glob("*.html")) if (RAW / "ranking").exists() else []
    kilpailuja = sijoituksia = tuomarointeja = 0

    for tiedosto in tiedostot:
        cid = int(tiedosto.stem)
        sivu = tiedosto.read_bytes().decode("utf-8", errors="replace")
        tiedot = lue_otsikko(sivu)
        sijat = lue_sijoitukset(sivu)
        tap_id, tap_nimi, tap_url = lue_tapahtuma(sivu)
        url = lue_url(sivu)

        tuomarit = []
        off = RAW / "officials" / f"{cid}.html"
        if off.exists():
            off_sivu = off.read_bytes().decode("utf-8", errors="replace")
            tuomarit = lue_tuomarit(off_sivu)
            if tiedot is None:
                tiedot = lue_otsikko(off_sivu)
            if tap_id is None:
                tap_id, tap_nimi, tap_url = lue_tapahtuma(off_sivu)
            if url is None:
                url = (lue_url(off_sivu) or "").replace(
                    "/Competitions/Officials/", "/Competitions/Ranking/") or None

        if not sijat and not tuomarit:
            continue                      # ei suomalaisia -> ei tallenneta

        tiedot = tiedot or {"nimi": None, "kaupunki": None, "maa": None, "pvm": None}
        con.execute("INSERT OR REPLACE INTO kilpailu "
                    "(id,nimi,kaupunki,maa,pvm,url,tapahtuma_id,tapahtuma,tapahtuma_url) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (cid, tiedot["nimi"], tiedot["kaupunki"], tiedot["maa"],
                     tiedot["pvm"], url, tap_id, tap_nimi, tap_url))
        kilpailuja += 1

        for s in sijat:
            con.execute("INSERT INTO sijoitus (kilpailu_id,sija,sija_raw,pari,"
                        "tanssija1,tanssija2) VALUES (?,?,?,?,?,?)",
                        (cid, s["sija"], s["sija_raw"], s["pari"],
                         s["tanssija1"], s["tanssija2"]))
            sijoituksia += 1

        for t in tuomarit:
            con.execute("INSERT INTO tuomarointi (kilpailu_id,nimi,rooli) VALUES (?,?,?)",
                        (cid, t["nimi"], t["rooli"]))
            tuomarointeja += 1

    con.execute("INSERT OR REPLACE INTO meta (avain, arvo) VALUES ('paivitetty', ?)",
                (datetime.datetime.now(datetime.timezone.utc)
                 .strftime("%Y-%m-%dT%H:%M:%SZ"),))
    con.commit()
    con.execute("VACUUM")
    con.close()
    print(f"\nValmis: {DB}  ({DB.stat().st_size / 1e6:.2f} MB)")
    con2 = sqlite3.connect(DB)
    tapahtumia = con2.execute(
        "SELECT COUNT(DISTINCT tapahtuma_id) FROM kilpailu "
        "WHERE tapahtuma_id IS NOT NULL").fetchone()[0]
    con2.close()
    print(f"  tapahtumia joissa suomalaisia  {tapahtumia}")
    print(f"  luokkia joissa suomalaisia     {kilpailuja}")
    print(f"  suomalaisten sijoituksia       {sijoituksia}")
    print(f"  suomalaisten tuomarointeja     {tuomarointeja}")


def oletusvali():
    """Kuluva vuosi. Tammi-helmikuussa mukaan otetaan myos edellinen vuosi,
    koska vuodenvaihteen kilpailujen tulokset julkaistaan viiveella."""
    tanaan = datetime.date.today()
    alkuvuosi = tanaan.year - 1 if tanaan.month <= 2 else tanaan.year
    return f"{alkuvuosi}-01", f"{tanaan.year}-12"


def main():
    args = sys.argv[1:]
    if args and args[0] == "jasenna":
        jasenna()
    elif args and args[0] == "hae":
        alku, loppu = (args[1], args[2]) if len(args) >= 3 else oletusvali()
        hae(alku, loppu)
    elif len(args) == 2:
        hae(args[0], args[1])
        jasenna()
    elif not args:
        alku, loppu = oletusvali()
        print(f"Oletusvali: {alku} - {loppu}")
        hae(alku, loppu)
        jasenna()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
