#!/usr/bin/env python3
"""
Sivuston tietoturvatarkistus. Ajetaan viikoittain GitHub Actionsissa.

Tarkistaa kolmella tasolla:

  VARASTO    kirjastotiedostojen muuttumattomuus, ulkoiset lahteet,
             sisaltokaytanto, ajojen oikeusrajaukset
  RIIPPUVUUDET  sql.js:n versio ja sita koskevat tietoturvatiedotteet,
             GitHub Actions -toimintojen versiot
  TUOTANTO   julkaistu sivusto: HTTPS, varmenne, sisaltokaytanto,
             ulkoiset lahteet, verkkotunnuksen osoittaminen

Loydokset luokitellaan kolmeen vakavuuteen:
  KRIITTINEN  viittaa mahdolliseen tunkeutumiseen tai tunnettuun
              haavoittuvuuteen; ajo merkitaan epaonnistuneeksi, jolloin
              GitHub lahettaa sahkopostin
  HUOMIO      pitaa korjata, mutta ei kiireellinen; avataan issue
  TIEDOKSI    hyva tietaa; nakyy issuessa muiden mukana

Kaytto:
    python3 tarkista.py
    python3 tarkista.py --paivita-summat   # hyvaksy nykyiset vendor-tiedostot
    python3 tarkista.py --ohita-verkko     # vain paikalliset tarkistukset
"""

import hashlib
import json
import os
import pathlib
import re
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

JUURI = pathlib.Path(__file__).parent
VENDOR = JUURI / "vendor"
SUMMAT = VENDOR / "tarkistussummat.txt"
SIVUT = ["index.html", "ulkomaat.html"]

REPO = "mikkokoskelainen-dev/tanssitulokset"

# GitHub Pagesin osoitteet, joihin verkkotunnuksen pitaa osoittaa.
PAGES_IP = {"185.199.108.153", "185.199.109.153",
            "185.199.110.153", "185.199.111.153"}

# Verkkotunnus uusitaan tahan mennessa; muistutus alkaa 60 paivaa ennen.
TUNNUS_UUSITTAVA = "2027-09-15"

KRIITTINEN, HUOMIO, TIEDOKSI = "KRIITTINEN", "HUOMIO", "TIEDOKSI"
# Tarkistusta ei voitu tehda (esim. verkkoyhteys ei vastannut). Ei ole
# loydos: nakyy lokissa ja raportin lopussa, mutta ei avaa issueta.
EI_TARKISTETTU = "EI TARKISTETTU"

loydokset = []
ohita_verkko = False


def huomio(vakavuus, otsikko, selitys, korjaus=""):
    loydokset.append({"vakavuus": vakavuus, "otsikko": otsikko,
                      "selitys": selitys, "korjaus": korjaus})


def ohitettu(mita, syy):
    """Tarkistusta ei voitu tehda. Ei ole tietoturvaloydos."""
    loydokset.append({"vakavuus": EI_TARKISTETTU, "otsikko": mita,
                      "selitys": syy, "korjaus": ""})


def hae(url, timeout=25):
    """Hakee sivun. Palauttaa (teksti, otsakkeet) tai (None, virheteksti)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tarkista.py"})
        with urllib.request.urlopen(req, timeout=timeout) as v:
            return v.read().decode("utf-8", errors="replace"), dict(v.headers)
    except Exception as e:
        return None, str(e)


# --------------------------------------------------------------------------
# VARASTO
# --------------------------------------------------------------------------

def tarkista_vendor():
    """Onko kirjastotiedostoihin koskettu?"""
    if not SUMMAT.exists():
        huomio(HUOMIO, "Tarkistussummat puuttuvat",
               f"Tiedostoa {SUMMAT.name} ei ole, joten kirjastotiedostojen "
               "muuttumattomuutta ei voi todentaa.",
               "Aja: `python3 tarkista.py --paivita-summat`")
        return {}

    odotetut = {}
    for rivi in SUMMAT.read_text().splitlines():
        if rivi.strip():
            summa, nimi = rivi.split(None, 1)
            odotetut[nimi.strip()] = summa

    for nimi, odotettu in odotetut.items():
        tiedosto = VENDOR / nimi
        if not tiedosto.exists():
            huomio(KRIITTINEN, f"Kirjastotiedosto puuttuu: {nimi}",
                   "Tiedosto on poistettu tai siirretty.",
                   "Palauta tiedosto versiohistoriasta.")
            continue
        nyt = hashlib.sha256(tiedosto.read_bytes()).hexdigest()
        if nyt != odotettu:
            huomio(KRIITTINEN, f"Kirjastotiedosto on muuttunut: {nimi}",
                   f"Odotettu `{odotettu[:16]}…`, levylla `{nyt[:16]}…`. "
                   "Jos et ole paivittanyt kirjastoa itse, tama tarkoittaa "
                   "etta sivusto suorittaa muuttunutta koodia kavijan selaimessa.",
                   "Tarkista muutos versiohistoriasta. Jos paivitit itse, aja "
                   "`python3 tarkista.py --paivita-summat` ja committoi.")

    yllatykset = {f.name for f in VENDOR.glob("*")} - set(odotetut) - {SUMMAT.name}
    if yllatykset:
        huomio(KRIITTINEN, "Kirjastokansiossa on tuntemattomia tiedostoja",
               "Nama eivat ole tarkistussummien joukossa: "
               + ", ".join(f"`{n}`" for n in sorted(yllatykset)),
               "Tarkista mista ne ovat tulleet. Jos lisasit ne itse, "
               "aja `python3 tarkista.py --paivita-summat`.")
    return odotetut


def tarkista_ulkoiset_lahteet():
    """Ladataanko sivuille jotain sivuston ulkopuolelta?"""
    for sivu in SIVUT:
        teksti = (JUURI / sivu).read_text()
        for tagi, attr in (("script", "src"), ("link", "href"), ("iframe", "src"),
                           ("img", "src"), ("object", "data")):
            for osoite in re.findall(rf'<{tagi}[^>]*?{attr}="(https?://[^"]+)"',
                                     teksti, re.I):
                huomio(KRIITTINEN, f"Ulkoinen lahde sivulla {sivu}",
                       f"`<{tagi}>` lataa osoitteesta {osoite}. Sivuston pitaisi "
                       "toimia ilman ulkoisia riippuvuuksia.",
                       "Kopioi tiedosto vendor-kansioon ja viittaa siihen "
                       "paikallisesti.")


def tarkista_csp():
    """Onko sisaltokaytanto paikallaan?"""
    for sivu in SIVUT:
        teksti = (JUURI / sivu).read_text()
        if "Content-Security-Policy" not in teksti:
            huomio(KRIITTINEN, f"Sisaltokaytanto puuttuu sivulta {sivu}",
                   "Content-Security-Policy -maarittely on poistunut.",
                   'Palauta `<meta http-equiv="Content-Security-Policy" …>` '
                   "sivun head-osaan.")
        elif "default-src 'none'" not in teksti:
            huomio(HUOMIO, f"Sisaltokaytanto on loyhentynyt sivulla {sivu}",
                   "`default-src 'none'` puuttuu, joten selain voi ladata "
                   "sisaltoa muualta.",
                   "Palauta tiukka oletus.")


def tarkista_workflowt():
    """Onko ajojen oikeudet rajattu, ja onko tiedostoihin ilmestynyt uusia?"""
    kansio = JUURI / ".github" / "workflows"
    for tiedosto in sorted(kansio.glob("*.yml")):
        teksti = tiedosto.read_text()
        if "permissions:" not in teksti:
            huomio(HUOMIO, f"Ajon oikeuksia ei ole rajattu: {tiedosto.name}",
                   "Tiedostosta puuttuu `permissions:`, jolloin ajo saa "
                   "oletusoikeudet.",
                   "Lisaa tiedostoon tarvittavat oikeudet, esim.\n"
                   "```yaml\npermissions:\n  contents: write\n```")
        # Salaisuuksien tulostaminen lokiin
        if re.search(r"echo\s+.*\$\{\{\s*secrets\.", teksti):
            huomio(KRIITTINEN, f"Ajo tulostaa salaisuuden: {tiedosto.name}",
                   "Workflow nayttaa tulostavan `secrets`-arvon lokiin, joka on "
                   "julkisessa varastossa kaikkien luettavissa.",
                   "Poista tulostus ja vaihda paljastunut salaisuus.")


# --------------------------------------------------------------------------
# RIIPPUVUUDET
# --------------------------------------------------------------------------

def kaytossa_oleva_sqljs():
    luki = VENDOR / "README.md"
    if luki.exists():
        m = re.search(r"sql\.js\s+([0-9][0-9.]*)", luki.read_text())
        if m:
            return m.group(1)
    return None


def tarkista_sqljs_versio():
    """Onko kaytossa oleva sql.js uusin julkaistu?"""
    if ohita_verkko:
        return
    kaytossa = kaytossa_oleva_sqljs()
    if not kaytossa:
        return
    teksti, _ = hae("https://registry.npmjs.org/sql.js/latest")
    if teksti is None:
        ohitettu("sql.js-versio", "npm-rekisteriin ei saatu yhteytta")
        return
    uusin = json.loads(teksti).get("version")
    if uusin and uusin != kaytossa:
        huomio(TIEDOKSI, "sql.js-kirjastosta on uudempi versio",
               f"Kaytossa {kaytossa}, uusin julkaistu {uusin}.",
               "Paivitys ei ole kiireellinen ellei alla mainita "
               "tietoturvatiedotetta. Paivitys: lataa uusi versio, korvaa "
               "vendor-kansion tiedostot, aja "
               "`python3 tarkista.py --paivita-summat` ja testaa sivut.")


def tarkista_haavoittuvuudet():
    """Onko sql.js:lle julkaistu tietoturvatiedotetta, joka koskee
    kaytossa olevaa versiota?"""
    if ohita_verkko:
        return
    kaytossa = kaytossa_oleva_sqljs()
    if not kaytossa:
        return

    url = ("https://api.github.com/advisories"
           "?ecosystem=npm&affects=sql.js&per_page=20")
    teksti, virhe = hae(url)
    if teksti is None:
        ohitettu("tietoturvatiedotteet", f"GitHubin tietokantaan ei saatu yhteytta ({virhe})")
        return

    try:
        tiedotteet = json.loads(teksti)
    except json.JSONDecodeError:
        return

    for t in tiedotteet:
        koskee = False
        for v in t.get("vulnerabilities") or []:
            paketti = ((v.get("package") or {}).get("name") or "").lower()
            if paketti != "sql.js":
                continue
            korjattu = v.get("first_patched_version")
            alue = v.get("vulnerable_version_range") or ""
            # Jos korjattu versio on uudempi kuin kaytossa oleva, tiedote koskee
            if korjattu and versio_pienempi(kaytossa, korjattu):
                koskee = True
            elif not korjattu and alue:
                koskee = True
        if koskee:
            vakavuus = (t.get("severity") or "").lower()
            taso = KRIITTINEN if vakavuus in ("critical", "high") else HUOMIO
            huomio(taso, f"sql.js: {t.get('summary', 'tietoturvatiedote')}",
                   f"Vakavuus **{vakavuus or 'tuntematon'}**. "
                   f"Kaytossa versio {kaytossa}. "
                   f"Tunniste {t.get('ghsa_id', '?')}.\n\n"
                   f"{(t.get('description') or '')[:400]}",
                   f"Lue tiedote: {t.get('html_url', '')} ja paivita kirjasto "
                   "korjattuun versioon.")


def versio_pienempi(a, b):
    """Onko versio a pienempi kuin b? Vertailee numero-osat."""
    def osat(v):
        return [int(x) for x in re.findall(r"\d+", v)[:3]] or [0]
    pa, pb = osat(a), osat(b)
    pa += [0] * (3 - len(pa))
    pb += [0] * (3 - len(pb))
    return pa < pb


def tarkista_actions_versiot():
    """Onko kaytetyista GitHub Actions -toiminnoista uudempi paaversio?"""
    if ohita_verkko:
        return
    kansio = JUURI / ".github" / "workflows"
    kaytetyt = set()
    for tiedosto in kansio.glob("*.yml"):
        kaytetyt |= set(re.findall(r"uses:\s*([\w.-]+/[\w.-]+)@v(\d+)",
                                   tiedosto.read_text()))

    for toiminto, versio in sorted(kaytetyt):
        teksti, _ = hae(f"https://api.github.com/repos/{toiminto}/releases/latest")
        if teksti is None:
            continue
        try:
            tagi = json.loads(teksti).get("tag_name", "")
        except json.JSONDecodeError:
            continue
        m = re.match(r"v?(\d+)", tagi)
        if m and int(m.group(1)) > int(versio):
            huomio(TIEDOKSI, f"Uudempi paaversio: {toiminto}",
                   f"Kaytossa v{versio}, uusin julkaistu {tagi}.",
                   f"Paivitys workflow-tiedostoihin: `uses: {toiminto}@v{m.group(1)}`. "
                   "Paaversion vaihto voi muuttaa toiminnan, joten testaa ajo.")


# --------------------------------------------------------------------------
# TUOTANTO
# --------------------------------------------------------------------------

def verkkotunnus():
    cname = JUURI / "CNAME"
    return cname.read_text().strip() if cname.exists() else None


def tarkista_dns():
    """Osoittaako verkkotunnus yha GitHub Pagesiin?"""
    if ohita_verkko:
        return
    tunnus = verkkotunnus()
    if not tunnus:
        return
    try:
        osoitteet = {t[4][0] for t in socket.getaddrinfo(tunnus, 443,
                                                         socket.AF_INET)}
    except Exception as e:
        ohitettu("verkkotunnuksen osoitus", f"nimipalvelu ei vastannut ({e})")
        return

    vieraat = osoitteet - PAGES_IP
    if vieraat:
        huomio(KRIITTINEN, "Verkkotunnus osoittaa vieraaseen osoitteeseen",
               f"{tunnus} vastaa osoitteista {', '.join(sorted(osoitteet))}, "
               f"joista {', '.join(sorted(vieraat))} ei kuulu GitHub Pagesille. "
               "Tama voi tarkoittaa, etta verkkotunnuksen asetuksia on muutettu "
               "luvatta.",
               "Tarkista A-tietueet Cloudflaresta. Niiden pitaa olla "
               + ", ".join(sorted(PAGES_IP)) + ".")


def tarkista_varmenne():
    """Onko HTTPS-varmenne voimassa ja kuinka kauan?"""
    if ohita_verkko:
        return
    tunnus = verkkotunnus()
    if not tunnus:
        return
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((tunnus, 443), timeout=20) as s:
            with ctx.wrap_socket(s, server_hostname=tunnus) as ss:
                varmenne = ss.getpeercert()
    except ssl.SSLCertVerificationError as e:
        huomio(KRIITTINEN, "HTTPS-varmenne ei kelpaa",
               f"Selain torjuu varmenteen: {e}. Kavijat saavat "
               "turvallisuusvaroituksen.",
               "Tarkista GitHub Pagesin Custom domain -asetus ja "
               "Enforce HTTPS -valinta.")
        return
    except Exception as e:
        ohitettu("https-varmenne", f"yhteytta ei saatu ({e})")
        return

    loppuu = varmenne.get("notAfter")
    if loppuu:
        try:
            pvm = datetime.strptime(loppuu, "%b %d %H:%M:%S %Y %Z").replace(
                tzinfo=timezone.utc)
            paivia = (pvm - datetime.now(timezone.utc)).days
            if paivia < 0:
                huomio(KRIITTINEN, "HTTPS-varmenne on vanhentunut",
                       f"Varmenne umpeutui {pvm:%-d.%-m.%Y}.",
                       "Tarkista GitHub Pagesin asetukset.")
            elif paivia < 14:
                huomio(HUOMIO, "HTTPS-varmenne vanhenee pian",
                       f"Varmenne umpeutuu {pvm:%-d.%-m.%Y}, {paivia} paivan kuluttua. "
                       "GitHub uusii sen normaalisti itse.",
                       "Jos uusiminen ei tapahdu, tarkista Custom domain "
                       "-asetus Pagesista.")
        except ValueError:
            pass


def tarkista_julkaistu_sivusto():
    """Vastaako verkossa oleva sivusto sita, mita varastossa on?"""
    if ohita_verkko:
        return
    tunnus = verkkotunnus()
    if not tunnus:
        return

    for sivu in SIVUT:
        teksti, otsakkeet = hae(f"https://{tunnus}/{sivu}")
        if teksti is None:
            ohitettu(f"julkaistu sivu {sivu}", f"ei vastannut ({otsakkeet})")
            continue

        if "Content-Security-Policy" not in teksti:
            huomio(KRIITTINEN, f"Julkaistulta sivulta puuttuu sisaltokaytanto: {sivu}",
                   "Verkossa olevalla sivulla ei ole Content-Security-Policy "
                   "-maarittelya, vaikka varastossa se on. Sivu voi olla "
                   "vanha tai muutettu.",
                   "Tarkista Pagesin julkaisun tila Actions-valilehdelta.")

        ulkoiset = re.findall(r'<script[^>]*?src="(https?://[^"]+)"', teksti, re.I)
        if ulkoiset:
            huomio(KRIITTINEN, f"Julkaistu sivu lataa ulkoista koodia: {sivu}",
                   "Verkossa olevalla sivulla on skripti osoitteesta "
                   + ", ".join(ulkoiset) + ", vaikka varastossa ei ole. "
                   "Tama viittaa siihen, etta julkaistua sivua on muutettu.",
                   "Vertaa julkaistua sivua varastoon ja tarkista "
                   "varaston kayttooikeudet.")

    # Kirjaston eheys verkossa
    summa_levylla = None
    if (VENDOR / "sql-wasm.js").exists():
        summa_levylla = hashlib.sha256(
            (VENDOR / "sql-wasm.js").read_bytes()).hexdigest()
    teksti, _ = hae(f"https://{tunnus}/vendor/sql-wasm.js")
    if teksti is not None and summa_levylla:
        summa_verkossa = hashlib.sha256(teksti.encode("utf-8")).hexdigest()
        if summa_verkossa != summa_levylla:
            huomio(TIEDOKSI, "Julkaistu kirjasto poikkeaa varastosta",
                   "Verkossa oleva `vendor/sql-wasm.js` ei vastaa varaston "
                   "versiota. Tavallisin syy on, etta julkaisu on viela kesken "
                   "tai kirjasto paivitettiin aivan askettain.",
                   "Jos tama toistuu usean viikon ajan, tarkista Pagesin "
                   "julkaisun tila.")


def tarkista_verkkotunnuksen_voimassaolo():
    """Muistuttaa verkkotunnuksen uusimisesta hyvissa ajoin."""
    try:
        eraantyy = datetime.strptime(TUNNUS_UUSITTAVA, "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return
    paivia = (eraantyy - datetime.now(timezone.utc)).days
    if paivia < 0:
        huomio(KRIITTINEN, "Verkkotunnus on eraantynyt",
               f"{verkkotunnus()} piti uusia {eraantyy:%-d.%-m.%Y}. "
               "Erääntynyt verkkotunnus voi paatya toisen haltuun.",
               "Uusi tunnus valittomasti.")
    elif paivia < 60:
        huomio(HUOMIO, "Verkkotunnus uusittava pian",
               f"{verkkotunnus()} on uusittava {eraantyy:%-d.%-m.%Y}, "
               f"{paivia} paivan kuluttua.",
               "Tarkista automaattinen uudistus verkkotunnusvalittajalta.")


# --------------------------------------------------------------------------
# GITHUB-ASETUKSET
# --------------------------------------------------------------------------

def gh_api(polku):
    """Kysyy GitHubin rajapinnalta gh-tyokalulla, jos se on kaytettavissa."""
    try:
        r = subprocess.run(["gh", "api", polku], capture_output=True,
                           text=True, timeout=30)
        if r.returncode == 0:
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def tarkista_github_asetukset():
    """Varaston asetukset, jotka vaikuttavat siihen kuka voi muuttaa sivustoa."""
    if ohita_verkko:
        return

    oikeudet = gh_api(f"repos/{REPO}/actions/permissions/workflow")
    if oikeudet:
        if oikeudet.get("default_workflow_permissions") == "write":
            huomio(HUOMIO, "Ajojen oletusoikeudet ovat liian laajat",
                   "Varaston asetus antaa kaikille ajoille kirjoitusoikeuden. "
                   "Jokainen workflow maarittelee jo tarvitsemansa oikeudet itse, "
                   "joten laaja oletus on tarpeeton.",
                   "Settings → Actions → General → Workflow permissions → "
                   "`Read repository contents and packages permissions`.")
        if oikeudet.get("can_approve_pull_request_reviews"):
            huomio(HUOMIO, "Ajot voivat hyvaksya pull requesteja",
                   "Asetus sallii workflow'n hyvaksya muutokset itse, mika "
                   "ohittaa ihmisen tarkistuksen.",
                   "Settings → Actions → General → poista rasti kohdasta "
                   "`Allow GitHub Actions to create and approve pull requests`.")

    kumppanit = gh_api(f"repos/{REPO}/collaborators")
    if isinstance(kumppanit, list) and len(kumppanit) > 1:
        nimet = ", ".join(k.get("login", "?") for k in kumppanit)
        huomio(HUOMIO, "Varastolla on useita kayttooikeuksia",
               f"Kirjoitusoikeus on seuraavilla: {nimet}. Jos joku naista ei "
               "ole tuttu, oikeus on syyta poistaa.",
               "Settings → Collaborators.")

    avaimet = gh_api(f"repos/{REPO}/keys")
    if isinstance(avaimet, list) and avaimet:
        nimet = ", ".join(a.get("title", "?") for a in avaimet)
        huomio(HUOMIO, "Varastoon on liitetty deploy-avaimia",
               f"Avaimet: {nimet}. Projekti ei tarvitse niita, joten "
               "tuntematon avain on poistettava.",
               "Settings → Deploy keys.")

    hookit = gh_api(f"repos/{REPO}/hooks")
    if isinstance(hookit, list) and hookit:
        osoitteet = ", ".join((h.get("config") or {}).get("url", "?")
                              for h in hookit)
        huomio(HUOMIO, "Varastoon on liitetty webhookkeja",
               f"Kohteet: {osoitteet}. Projekti ei tarvitse niita.",
               "Settings → Webhooks.")


# --------------------------------------------------------------------------
# Raportointi
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


def kirjoita_raportti():
    jarjestys = {KRIITTINEN: 0, HUOMIO: 1, TIEDOKSI: 2}
    oikeat = sorted((l for l in loydokset if l["vakavuus"] != EI_TARKISTETTU),
                    key=lambda l: jarjestys[l["vakavuus"]])
    ohitetut = [l for l in loydokset if l["vakavuus"] == EI_TARKISTETTU]

    maarat = {v: sum(1 for l in oikeat if l["vakavuus"] == v)
              for v in (KRIITTINEN, HUOMIO, TIEDOKSI)}
    tiivis = " · ".join(f"{m} {v.lower()}" for v, m in maarat.items() if m)

    merkki = {KRIITTINEN: "🔴", HUOMIO: "🟠", TIEDOKSI: "🔵"}
    rivit = [f"Viikoittainen tietoturvatarkistus: **{tiivis}**.", ""]

    if maarat[KRIITTINEN]:
        rivit += ["> Kriittinen loydos viittaa mahdolliseen tunkeutumiseen tai "
                  "tunnettuun haavoittuvuuteen. Tarkista se ensin.", ""]

    for i, l in enumerate(oikeat, 1):
        rivit += [f"### {merkki[l['vakavuus']]} {i}. {l['otsikko']}", "",
                  f"*{l['vakavuus'].capitalize()}*", "",
                  l["selitys"], "",
                  f"**Korjaus:** {l['korjaus']}", ""]

    if ohitetut:
        rivit += ["### Tarkistuksia, joita ei voitu tehda", ""]
        rivit += [f"- **{l['otsikko']}** — {l['selitys']}" for l in ohitetut]
        rivit += ["", "Nama eivat ole loydoksia. Jos sama tarkistus jaa "
                  "tekematta usean viikon ajan, siina voi olla jotain vialla.", ""]

    rivit += ["---", "",
              f"Tarkistettu {datetime.now(timezone.utc):%-d.%-m.%Y} "
              "automaattisella ajolla (`tarkista.py`). Tarkistus toistuu "
              "maanantaisin. Tama issue voidaan sulkea kun asiat on hoidettu — "
              "jos jokin loydos toistuu, se tulee uutena kommenttina."]

    teksti = "\n".join(rivit)
    (JUURI / "loydokset.md").write_text(teksti)
    return teksti, maarat


def main():
    global ohita_verkko
    if "--paivita-summat" in sys.argv:
        paivita_summat()
        return 0
    ohita_verkko = "--ohita-verkko" in sys.argv or os.environ.get("OHITA_VERKKO")

    print("Tietoturvatarkistus" + (" (ilman verkkoa)" if ohita_verkko else "") + "\n")

    vaiheet = [
        ("VARASTO", [
            ("kirjastotiedostot", tarkista_vendor),
            ("ulkoiset lahteet", tarkista_ulkoiset_lahteet),
            ("sisaltokaytanto", tarkista_csp),
            ("ajojen maarittelyt", tarkista_workflowt),
        ]),
        ("RIIPPUVUUDET", [
            ("sql.js-versio", tarkista_sqljs_versio),
            ("tietoturvatiedotteet", tarkista_haavoittuvuudet),
            ("actions-versiot", tarkista_actions_versiot),
        ]),
        ("TUOTANTO", [
            ("verkkotunnuksen osoitus", tarkista_dns),
            ("https-varmenne", tarkista_varmenne),
            ("julkaistu sivusto", tarkista_julkaistu_sivusto),
            ("verkkotunnuksen voimassaolo", tarkista_verkkotunnuksen_voimassaolo),
        ]),
        ("GITHUB", [
            ("varaston asetukset", tarkista_github_asetukset),
        ]),
    ]

    for otsikko, tarkistukset in vaiheet:
        print(f"{otsikko}")
        for nimi, fn in tarkistukset:
            ennen = len(loydokset)
            try:
                fn()
            except Exception as e:
                huomio(TIEDOKSI, f"Tarkistus epaonnistui: {nimi}",
                       f"Tarkistus paattyi virheeseen: {e}",
                       "Tama on tarkistusskriptin vika, ei valttamatta "
                       "sivuston ongelma.")
            uudet = loydokset[ennen:]
            if not uudet:
                tulos = "OK"
            else:
                tulos = ", ".join(sorted({l["vakavuus"] for l in uudet}))
            print(f"  {nimi:28} {tulos}")
        print()

    oikeat = [l for l in loydokset if l["vakavuus"] != EI_TARKISTETTU]
    ohitetut = [l for l in loydokset if l["vakavuus"] == EI_TARKISTETTU]

    if not oikeat:
        print("Kaikki kunnossa."
              + (f" ({len(ohitetut)} tarkistusta ei voitu tehda.)" if ohitetut else ""))
        return 0

    teksti, maarat = kirjoita_raportti()
    print(teksti)
    return 2 if maarat[KRIITTINEN] else 1


if __name__ == "__main__":
    sys.exit(main())
