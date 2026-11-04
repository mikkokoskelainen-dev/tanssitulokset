#!/usr/bin/env python3
"""
Sivuston tietoturvatarkistus. Ajetaan viikoittain GitHub Actionsissa.

Tarkistaa viisi asiaa:
  1. vendor-kansion tiedostot ovat muuttumattomia (tarkistussummat)
  2. sivut eivat lataa mitaan ulkopuolelta (ei ulkoisia script/link-viittauksia)
  3. Content-Security-Policy on paikallaan molemmilla sivuilla
  4. kaytossa oleva sql.js on uusin julkaistu versio
  5. ajojen oikeudet on rajattu (permissions-rivi workflow-tiedostoissa)

Paluuarvo 0 = kaikki kunnossa, 1 = jotain huomautettavaa.
Loydokset tulostetaan ja kirjoitetaan tiedostoon loydokset.md, josta
workflow tekee GitHub-issuen.

Kaytto:
    python3 tarkista.py
    python3 tarkista.py --paivita-summat   # hyvaksy nykyiset vendor-tiedostot
"""

import hashlib
import json
import pathlib
import re
import sys
import urllib.request

JUURI = pathlib.Path(__file__).parent
VENDOR = JUURI / "vendor"
SUMMAT = VENDOR / "tarkistussummat.txt"
SIVUT = ["index.html", "ulkomaat.html"]

# Naita osoitteita sivut saavat sisaltaa <a href>-linkkeina.
SALLITUT_LINKIT = ("worlddancesport.org", "dancesport.fi")

loydokset = []


def huomio(otsikko, selitys, korjaus):
    loydokset.append({"otsikko": otsikko, "selitys": selitys, "korjaus": korjaus})


# --------------------------------------------------------------------------

def tarkista_vendor():
    """Onko kirjastotiedostoihin koskettu?"""
    if not SUMMAT.exists():
        huomio("Tarkistussummat puuttuvat",
               f"Tiedostoa {SUMMAT} ei ole, joten kirjastotiedostojen "
               "muuttumattomuutta ei voi todentaa.",
               "Aja: python3 tarkista.py --paivita-summat")
        return

    odotetut = {}
    for rivi in SUMMAT.read_text().splitlines():
        if rivi.strip():
            summa, nimi = rivi.split(None, 1)
            odotetut[nimi.strip()] = summa

    for nimi, odotettu in odotetut.items():
        tiedosto = VENDOR / nimi
        if not tiedosto.exists():
            huomio(f"Kirjastotiedosto puuttuu: {nimi}",
                   "Tiedosto on poistettu tai siirretty.",
                   "Palauta tiedosto versiohistoriasta.")
            continue
        nyt = hashlib.sha256(tiedosto.read_bytes()).hexdigest()
        if nyt != odotettu:
            huomio(f"Kirjastotiedosto on muuttunut: {nimi}",
                   f"Odotettu `{odotettu[:16]}…`, levylla `{nyt[:16]}…`. "
                   "Jos et ole paivittanyt kirjastoa itse, tama on vakava: "
                   "sivusto suorittaa muuttunutta koodia kavijan selaimessa.",
                   "Tarkista muutos versiohistoriasta. Jos paivitit itse, aja "
                   "`python3 tarkista.py --paivita-summat` ja committoi.")

    yllatykset = {f.name for f in VENDOR.glob("*")} - set(odotetut) - {SUMMAT.name}
    if yllatykset:
        huomio("Kirjastokansiossa on tuntemattomia tiedostoja",
               "Nama eivat ole tarkistussummien joukossa: "
               + ", ".join(f"`{n}`" for n in sorted(yllatykset)),
               "Tarkista mista ne ovat tulleet. Jos lisasit ne itse, "
               "aja `python3 tarkista.py --paivita-summat`.")


def tarkista_ulkoiset_lahteet():
    """Ladataanko sivuille jotain sivuston ulkopuolelta?"""
    for sivu in SIVUT:
        teksti = (JUURI / sivu).read_text()
        for tagi, attr in (("script", "src"), ("link", "href"), ("iframe", "src"),
                           ("img", "src"), ("object", "data")):
            for osoite in re.findall(rf'<{tagi}[^>]*?{attr}="(https?://[^"]+)"',
                                     teksti, re.I):
                huomio(f"Ulkoinen lahde sivulla {sivu}",
                       f"`<{tagi}>` lataa osoitteesta {osoite}. Sivuston pitaisi "
                       "toimia ilman ulkoisia riippuvuuksia.",
                       "Kopioi tiedosto vendor-kansioon ja viittaa siihen "
                       "paikallisesti.")


def tarkista_csp():
    """Onko sisaltokaytanto paikallaan?"""
    for sivu in SIVUT:
        teksti = (JUURI / sivu).read_text()
        if "Content-Security-Policy" not in teksti:
            huomio(f"Sisaltokaytanto puuttuu sivulta {sivu}",
                   "Content-Security-Policy -maarittely on poistunut.",
                   "Palauta `<meta http-equiv=\"Content-Security-Policy\" …>` "
                   "sivun head-osaan.")
        elif "default-src 'none'" not in teksti:
            huomio(f"Sisaltokaytanto on loyhentynyt sivulla {sivu}",
                   "`default-src 'none'` puuttuu, joten selain voi ladata "
                   "sisaltoa muualta.",
                   "Palauta tiukka oletus.")


def tarkista_sqljs_versio():
    """Onko kaytossa oleva sql.js uusin?"""
    kaytossa = None
    luki = (VENDOR / "README.md")
    if luki.exists():
        m = re.search(r"sql\.js\s+([0-9][0-9.]*)", luki.read_text())
        if m:
            kaytossa = m.group(1)
    if not kaytossa:
        return

    try:
        with urllib.request.urlopen(
                "https://registry.npmjs.org/sql.js/latest", timeout=20) as v:
            uusin = json.load(v)["version"]
    except Exception as e:
        print(f"  (sql.js-version tarkistus ei onnistunut: {e})")
        return

    if uusin != kaytossa:
        huomio("sql.js-kirjastosta on uudempi versio",
               f"Kaytossa {kaytossa}, uusin julkaistu {uusin}.",
               "Paivitys ei ole kiireellinen ellei julkaisussa mainita "
               "tietoturvakorjausta. Paivitys: lataa uusi versio, korvaa "
               "vendor-kansion tiedostot, aja "
               "`python3 tarkista.py --paivita-summat` ja testaa sivut.")


def tarkista_workflowt():
    """Onko ajojen oikeudet rajattu?"""
    kansio = JUURI / ".github" / "workflows"
    for tiedosto in sorted(kansio.glob("*.yml")):
        teksti = tiedosto.read_text()
        if "permissions:" not in teksti:
            huomio(f"Ajon oikeuksia ei ole rajattu: {tiedosto.name}",
                   "Tiedostosta puuttuu `permissions:`, jolloin ajo saa "
                   "oletusoikeudet.",
                   "Lisaa tiedostoon tarvittavat oikeudet, esim.\n"
                   "```yaml\npermissions:\n  contents: write\n```")


# --------------------------------------------------------------------------

def paivita_summat():
    rivit = []
    for f in sorted(VENDOR.glob("*")):
        if f.name == SUMMAT.name:
            continue
        rivit.append(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}")
    SUMMAT.write_text("\n".join(rivit) + "\n")
    print(f"Tarkistussummat paivitetty: {SUMMAT}")
    for r in rivit:
        print("  " + r)


def main():
    if "--paivita-summat" in sys.argv:
        paivita_summat()
        return 0

    print("Tietoturvatarkistus\n")
    for nimi, fn in [("kirjastotiedostot", tarkista_vendor),
                     ("ulkoiset lahteet", tarkista_ulkoiset_lahteet),
                     ("sisaltokaytanto", tarkista_csp),
                     ("sql.js-versio", tarkista_sqljs_versio),
                     ("ajojen oikeudet", tarkista_workflowt)]:
        ennen = len(loydokset)
        fn()
        tulos = "OK" if len(loydokset) == ennen else f"{len(loydokset) - ennen} huomiota"
        print(f"  {nimi:22} {tulos}")

    if not loydokset:
        print("\nKaikki kunnossa.")
        return 0

    rivit = ["Viikoittainen tietoturvatarkistus loysi huomautettavaa.", ""]
    for i, l in enumerate(loydokset, 1):
        rivit += [f"### {i}. {l['otsikko']}", "", l["selitys"], "",
                  f"**Korjaus:** {l['korjaus']}", ""]
    rivit += ["---", "",
              "Tarkistus ajetaan automaattisesti maanantaisin. "
              "Tama issue voidaan sulkea kun asiat on hoidettu."]
    teksti = "\n".join(rivit)

    (JUURI / "loydokset.md").write_text(teksti)
    print("\n" + teksti)
    return 1


if __name__ == "__main__":
    sys.exit(main())
