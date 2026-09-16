#!/usr/bin/env python3
"""
Jasentaa data/raw/ -kansioon ladatut TPS.net-merkintataulukot
SQLite-kannaksi tiedostoon data/tulokset.sqlite.

Ei kayta mitaan ulkoisia kirjastoja, joten toimii GitHub Actionsissa
sellaisenaan ilman asennuksia.

Kaytto:
    python3 parse.py
"""

import html
import pathlib
import re
import shutil
import sqlite3
from html.parser import HTMLParser

RAW = pathlib.Path("data/raw")
DB = pathlib.Path("data/tulokset.sqlite")
DB_VUOSI = pathlib.Path("data/tulokset-vuosi.sqlite")

# Kevyeen kantaan otetaan kuluva vuosi, eli aineiston uusimman kilpailun
# kalenterivuosi tammikuun alusta alkaen.

SCHEMA = """
CREATE TABLE kilpailu (
    id            INTEGER PRIMARY KEY,      -- results.dancesport.fi/<id>
    nimi          TEXT,
    pvm           TEXT                      -- ISO-muodossa, esim. 2026-09-05
);

CREATE TABLE luokka (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kilpailu_id   INTEGER NOT NULL REFERENCES kilpailu(id),
    nro           INTEGER NOT NULL,         -- luokan jarjestysnumero kilpailussa
    nimi          TEXT NOT NULL,            -- esim. "Seniori 1 D Latin"
    pvm           TEXT,
    UNIQUE (kilpailu_id, nro)
);

CREATE TABLE tuomari (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nimi          TEXT NOT NULL UNIQUE
);

CREATE TABLE luokan_tuomari (
    luokka_id     INTEGER NOT NULL REFERENCES luokka(id),
    kirjain       TEXT NOT NULL,            -- A, B, C ... vain taman luokan sisalla
    tuomari_id    INTEGER NOT NULL REFERENCES tuomari(id),
    PRIMARY KEY (luokka_id, kirjain)
);

CREATE TABLE tanssija (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nimi          TEXT NOT NULL UNIQUE
);

CREATE TABLE pari (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    luokka_id     INTEGER NOT NULL REFERENCES luokka(id),
    numero        TEXT,
    seura         TEXT,
    nimi          TEXT,                     -- alkuperainen "Etu Suku / Etu Suku"
    tanssija1_id  INTEGER REFERENCES tanssija(id),
    tanssija2_id  INTEGER REFERENCES tanssija(id),
    UNIQUE (luokka_id, numero)
);

CREATE TABLE merkinta (
    luokka_id     INTEGER NOT NULL REFERENCES luokka(id),
    pari_id       INTEGER NOT NULL REFERENCES pari(id),
    kierros       TEXT NOT NULL,            -- "Final", "Round 1", ...
    tanssi        TEXT NOT NULL,            -- "Waltz", "Cha Cha Cha", ...
    kirjain       TEXT NOT NULL,
    tuomari_id    INTEGER REFERENCES tuomari(id),
    arvo          TEXT NOT NULL,            -- sijoitusluku tai "X" (risti)
    sijoitus      INTEGER                   -- sama numerona, tai NULL jos risti
);

CREATE TABLE tulos (
    luokka_id     INTEGER NOT NULL REFERENCES luokka(id),
    pari_id       INTEGER NOT NULL REFERENCES pari(id),
    sija          INTEGER,
    sija_raw      TEXT,
    PRIMARY KEY (luokka_id, pari_id)
);

CREATE TABLE meta (
    avain         TEXT PRIMARY KEY,
    arvo          TEXT
);

CREATE INDEX i_merkinta_pari    ON merkinta(pari_id);
CREATE INDEX i_merkinta_tuomari ON merkinta(tuomari_id);
CREATE INDEX i_pari_luokka      ON pari(luokka_id);
"""

OTSIKOT = {"nr", "couple", "country", "round", "sum", "place"}

# Kasin tehtavat nimikorjaukset. Lahdeaineistossa on kirjoitusvirheita ja
# vaihtelevia kirjoitusasuja; tassa ne yhdistetaan oikeaan muotoon.
# Vasemmalla lahteessa esiintyva nimi, oikealla oikea nimi.
KORJAUKSET = {
    "Jarmo Nuurinen": "Jarmo Nuutinen",
}


def korjaa_nimi(nimi):
    nimi = " ".join((nimi or "").split())
    return KORJAUKSET.get(nimi, nimi)


# --------------------------------------------------------------------------
# HTML -> taulukkoruudukko
# --------------------------------------------------------------------------

class TableReader(HTMLParser):
    """Lukee sivun ensimmaisen taulukon soluiksi, colspan ja rowspan mukana."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None
        self._attrs = {}
        self._in_table = False
        self._done = False

    def handle_starttag(self, tag, attrs):
        if self._done:
            return
        if tag == "table":
            self._in_table = True
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._attrs = dict(attrs)

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if self._done:
            return
        if tag in ("td", "th") and self._cell is not None:
            text = " ".join("".join(self._cell).split())
            self._row.append((
                text,
                int(self._attrs.get("colspan", 1) or 1),
                int(self._attrs.get("rowspan", 1) or 1),
            ))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "table" and self._in_table:
            self._done = True
            self._in_table = False


def build_grid(rows):
    """Purkaa colspanit ja rowspanit tavalliseksi ruudukoksi, jossa jokainen
    rivi on yhta pitka ja sarakeindeksit tarkoittavat samaa joka rivilla."""
    grid = []
    pending = {}          # sarake -> (teksti, montako rivia viela)
    for row in rows:
        out = []
        col = 0
        i = 0
        while True:
            while col in pending:
                text, left = pending[col]
                out.append(text)
                if left - 1 <= 0:
                    del pending[col]
                else:
                    pending[col] = (text, left - 1)
                col += 1
            if i >= len(row):
                break
            text, colspan, rowspan = row[i]
            i += 1
            for _ in range(colspan):
                out.append(text)
                if rowspan > 1:
                    pending[col] = (text, rowspan - 1)
                col += 1
        grid.append(out)
    return grid


# --------------------------------------------------------------------------
# Yksittaisen merkintataulukkosivun luku
# --------------------------------------------------------------------------

def strip_tags(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def read_marking_file(path):
    text = path.read_bytes().decode("utf-8", errors="replace")

    reader = TableReader()
    reader.feed(text)
    grid = build_grid(reader.rows)
    if len(grid) < 3:
        return None

    header_dance, header_letter = grid[0], grid[1]

    # Sarakkeiden paikat otsikkotekstien perusteella
    idx = {}
    for i, cell in enumerate(header_dance):
        key = cell.strip().lower().rstrip(".")
        if key in OTSIKOT and key not in idx:
            idx[key] = i
    if "round" not in idx or "couple" not in idx:
        return None

    # Tuomarisarakkeet: alarivilla yksi iso kirjain, ylarivilla tanssin nimi
    mark_cols = []
    for i, letter in enumerate(header_letter):
        letter = letter.strip()
        if len(letter) == 1 and letter.isalpha() and letter.isupper():
            dance = header_dance[i].strip()
            if dance and dance.lower().rstrip(".") not in OTSIKOT:
                mark_cols.append((i, dance, letter))

    # Tuomarikirjainten purku nimiksi sivun alaosasta
    judges = {}
    for letter, name in re.findall(r"<p>\s*([A-Za-z])\s*:\s*([^<]+)</p>", text):
        judges[letter.strip().upper()] = html.unescape(name).strip()

    luokka = strip_tags(re.search(r"<h1.*?</h1>", text, re.S).group(0)) \
        if re.search(r"<h1.*?</h1>", text, re.S) else ""
    luokka = re.sub(r"^Marking\s+Table\s*", "", luokka).strip()

    kilpailu = ""
    m = re.search(r'font-size:\s*30px[^>]*>([^<]+)<', text)
    if m:
        kilpailu = html.unescape(m.group(1)).strip()

    rivit = []
    for row in grid[2:]:
        if len(row) <= max(idx.values()):
            continue
        kierros = row[idx["round"]].strip()
        pari = row[idx["couple"]].strip()
        if not kierros or not pari:
            continue
        merkinnat = []
        for i, dance, letter in mark_cols:
            arvo = row[i].strip()
            if arvo:
                merkinnat.append((dance, letter, arvo))
        rivit.append({
            "numero": row[idx["nr"]].strip() if "nr" in idx else None,
            "pari": pari,
            "seura": row[idx["country"]].strip() if "country" in idx else None,
            "kierros": kierros,
            "sija": row[idx["place"]].strip() if "place" in idx else "",
            "merkinnat": merkinnat,
        })

    return {"kilpailu": kilpailu, "luokka": luokka, "tuomarit": judges, "rivit": rivit}


def read_class_list(comp_dir):
    """Kilpailun etusivulta luokkien nimet ja paivamaarat jarjestyksessa.
    TPS kirjoittaa paivat amerikkalaisittain: 9/5/2026 = 5.9.2026."""
    index = comp_dir / "index.html"
    if not index.exists():
        return {}
    text = index.read_bytes().decode("utf-8", errors="replace")
    block = re.search(r'<ul class="dropdown-menu">(.*?)</ul>', text, re.S)
    items = re.findall(r"<li><a [^>]*>(.*?)</a></li>", block.group(1) if block else text, re.S)

    out = {}
    for n, label in enumerate(items, start=1):
        label = strip_tags(label)
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(.*)", label)
        if m:
            kk, pp, vv, nimi = m.groups()
            out[n] = (f"{vv}-{int(kk):02d}-{int(pp):02d}", nimi.strip())
        else:
            out[n] = (None, label)
    return out


# --------------------------------------------------------------------------
# Kantaan kirjoitus
# --------------------------------------------------------------------------

class Db:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()          # rakennetaan aina puhtaalta polta
        self.con = sqlite3.connect(path)
        self.con.executescript(SCHEMA)
        self._tuomarit = {}
        self._tanssijat = {}

    def tuomari(self, nimi):
        nimi = korjaa_nimi(nimi)
        if nimi not in self._tuomarit:
            cur = self.con.execute(
                "INSERT OR IGNORE INTO tuomari (nimi) VALUES (?)", (nimi,))
            if cur.lastrowid:
                self._tuomarit[nimi] = cur.lastrowid
            else:
                self._tuomarit[nimi] = self.con.execute(
                    "SELECT id FROM tuomari WHERE nimi = ?", (nimi,)).fetchone()[0]
        return self._tuomarit[nimi]

    def tanssija(self, nimi):
        nimi = korjaa_nimi(nimi)
        if not nimi:
            return None
        if nimi not in self._tanssijat:
            cur = self.con.execute(
                "INSERT OR IGNORE INTO tanssija (nimi) VALUES (?)", (nimi,))
            if cur.lastrowid:
                self._tanssijat[nimi] = cur.lastrowid
            else:
                self._tanssijat[nimi] = self.con.execute(
                    "SELECT id FROM tanssija WHERE nimi = ?", (nimi,)).fetchone()[0]
        return self._tanssijat[nimi]


def sija_numerona(teksti):
    m = re.match(r"(\d+)", teksti or "")
    return int(m.group(1)) if m else None


def rakenna_vuosikanta():
    """Tekee taydesta kannasta kevyen kopion, jossa on vain kuluvan vuoden
    kilpailut. Sivu lataa taman oletuksena ja hakee koko historian vasta
    pyydettaessa."""
    if DB_VUOSI.exists():
        DB_VUOSI.unlink()
    shutil.copyfile(DB, DB_VUOSI)

    con = sqlite3.connect(DB_VUOSI)
    uusin = con.execute("SELECT MAX(pvm) FROM luokka WHERE pvm IS NOT NULL").fetchone()[0]
    if not uusin:
        con.close()
        print("  vuosikantaa ei voitu rajata: paivamaaria ei loydy")
        return None

    vuosi = int(uusin[:4])
    raja = f"{vuosi:04d}-01-01"

    con.executescript(f"""
        CREATE TEMP TABLE pidettavat AS
          SELECT id FROM luokka WHERE pvm IS NOT NULL AND pvm >= '{raja}';

        DELETE FROM merkinta       WHERE luokka_id NOT IN (SELECT id FROM pidettavat);
        DELETE FROM tulos          WHERE luokka_id NOT IN (SELECT id FROM pidettavat);
        DELETE FROM pari           WHERE luokka_id NOT IN (SELECT id FROM pidettavat);
        DELETE FROM luokan_tuomari WHERE luokka_id NOT IN (SELECT id FROM pidettavat);
        DELETE FROM luokka         WHERE id        NOT IN (SELECT id FROM pidettavat);
        DELETE FROM kilpailu       WHERE id        NOT IN (SELECT kilpailu_id FROM luokka);
        DELETE FROM tuomari        WHERE id        NOT IN (SELECT tuomari_id FROM luokan_tuomari);
        DELETE FROM tanssija       WHERE id NOT IN (SELECT tanssija1_id FROM pari WHERE tanssija1_id IS NOT NULL)
                                     AND id NOT IN (SELECT tanssija2_id FROM pari WHERE tanssija2_id IS NOT NULL);

        INSERT OR REPLACE INTO meta (avain, arvo) VALUES ('vuosi', '{vuosi}');
    """)
    con.commit()
    con.execute("VACUUM")
    luvut = con.execute("""
        SELECT (SELECT COUNT(*) FROM kilpailu), (SELECT COUNT(*) FROM luokka),
               (SELECT COUNT(*) FROM merkinta)""").fetchone()
    con.close()
    return vuosi, raja, luvut


def main():
    if not RAW.exists():
        print(f"Kansiota {RAW} ei loydy - aja scrape.py ensin.")
        return

    db = Db(DB)
    luokkia = 0
    merkintoja = 0

    for comp_dir in sorted(RAW.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 0):
        if not comp_dir.is_dir():
            continue
        comp_id = int(comp_dir.name)
        luokkalista = read_class_list(comp_dir)

        tiedostot = sorted(comp_dir.glob("*_markingtable.html"))
        if not tiedostot:
            continue

        kilpailu_nimi = None
        pvmat = [d for d, _ in luokkalista.values() if d]

        for tiedosto in tiedostot:
            nro = int(tiedosto.name.split("_")[0])
            data = read_marking_file(tiedosto)
            if not data or not data["rivit"]:
                print(f"  ohitettiin {tiedosto} (ei luettavaa taulukkoa)")
                continue

            if kilpailu_nimi is None:
                kilpailu_nimi = data["kilpailu"]
                db.con.execute(
                    "INSERT OR IGNORE INTO kilpailu (id, nimi, pvm) VALUES (?,?,?)",
                    (comp_id, kilpailu_nimi, min(pvmat) if pvmat else None))

            pvm, nimi_listasta = luokkalista.get(nro, (None, None))
            cur = db.con.execute(
                "INSERT INTO luokka (kilpailu_id, nro, nimi, pvm) VALUES (?,?,?,?)",
                (comp_id, nro, data["luokka"] or nimi_listasta or f"Luokka {nro}", pvm))
            luokka_id = cur.lastrowid
            luokkia += 1

            for kirjain, nimi in data["tuomarit"].items():
                tid = db.tuomari(nimi)
                db.con.execute(
                    "INSERT OR IGNORE INTO luokan_tuomari (luokka_id, kirjain, tuomari_id) "
                    "VALUES (?,?,?)", (luokka_id, kirjain, tid))

            parit = {}
            for rivi in data["rivit"]:
                avain = rivi["numero"] or rivi["pari"]
                if avain not in parit:
                    nimet = [n.strip() for n in rivi["pari"].split("/")]
                    cur = db.con.execute(
                        "INSERT INTO pari (luokka_id, numero, seura, nimi, "
                        "tanssija1_id, tanssija2_id) VALUES (?,?,?,?,?,?)",
                        (luokka_id, rivi["numero"], rivi["seura"], rivi["pari"],
                         db.tanssija(nimet[0]) if nimet else None,
                         db.tanssija(nimet[1]) if len(nimet) > 1 else None))
                    parit[avain] = cur.lastrowid
                pari_id = parit[avain]

                if rivi["sija"]:
                    db.con.execute(
                        "INSERT OR REPLACE INTO tulos (luokka_id, pari_id, sija, sija_raw) "
                        "VALUES (?,?,?,?)",
                        (luokka_id, pari_id, sija_numerona(rivi["sija"]), rivi["sija"]))

                for tanssi, kirjain, arvo in rivi["merkinnat"]:
                    nimi = data["tuomarit"].get(kirjain)
                    db.con.execute(
                        "INSERT INTO merkinta (luokka_id, pari_id, kierros, tanssi, "
                        "kirjain, tuomari_id, arvo, sijoitus) VALUES (?,?,?,?,?,?,?,?)",
                        (luokka_id, pari_id, rivi["kierros"], tanssi, kirjain,
                         db.tuomari(nimi) if nimi else None,
                         arvo, sija_numerona(arvo)))
                    merkintoja += 1

        if kilpailu_nimi:
            print(f"[{comp_id}] {kilpailu_nimi}: {len(tiedostot)} luokkaa")

    db.con.commit()
    luvut = dict(
        kilpailuja=db.con.execute("SELECT COUNT(*) FROM kilpailu").fetchone()[0],
        tuomareita=db.con.execute("SELECT COUNT(*) FROM tuomari").fetchone()[0],
        tanssijoita=db.con.execute("SELECT COUNT(*) FROM tanssija").fetchone()[0],
    )
    db.con.close()

    print(f"\nValmis: {DB}  ({DB.stat().st_size / 1e6:.1f} MB)")
    print(f"  kilpailuja  {luvut['kilpailuja']}")
    print(f"  luokkia     {luokkia}")
    print(f"  merkintoja  {merkintoja}")
    print(f"  tuomareita  {luvut['tuomareita']}")
    print(f"  tanssijoita {luvut['tanssijoita']}")

    tulos_vuosi = rakenna_vuosikanta()
    if tulos_vuosi:
        vuosi, raja, (k, l, m) = tulos_vuosi
        print(f"\nVuosi {vuosi}: {DB_VUOSI}  ({DB_VUOSI.stat().st_size / 1e6:.1f} MB)")
        print(f"  alkaen {raja}: {k} kilpailua, {l} luokkaa, {m} merkintaa")


if __name__ == "__main__":
    main()
