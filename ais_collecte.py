import asyncio
import json
import os
import time
from datetime import datetime, timezone

import psycopg
import websockets
from dotenv import load_dotenv

load_dotenv()  # lit le fichier .env du dossier

API_KEY = os.environ.get("AISSTREAM_API_KEY")
URL = "wss://stream.aisstream.io/v0/stream"
MANCHE = [[48.5, -6.0], [51.5, 2.0]]

CONNEXION_PG = (
    f"host={os.environ.get('POSTGRES_HOST', 'localhost')} "
    f"port={os.environ.get('POSTGRES_PORT', '5432')} "
    f"dbname={os.environ['POSTGRES_DB']} "
    f"user={os.environ['POSTGRES_USER']} "
    f"password={os.environ['POSTGRES_PASSWORD']}"
)

TAILLE_LOT = 100          # on écrit en base par paquets de 100 messages
DELAI_MAX_LOT = 10        # ou toutes les 10 secondes au plus tard


def preparer_base(connexion):
    with connexion.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")

        # Table de faits : une ligne par position reçue
        cur.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id              BIGSERIAL PRIMARY KEY,
                mmsi            INTEGER NOT NULL,
                nom             TEXT,
                latitude        DOUBLE PRECISION NOT NULL,
                longitude       DOUBLE PRECISION NOT NULL,
                vitesse_noeuds  REAL,
                route_deg       REAL,
                cap_deg         INTEGER,
                statut_nav      INTEGER,
                heure_ais       TIMESTAMPTZ,
                heure_reception TIMESTAMPTZ NOT NULL,
                geom            GEOMETRY(Point, 4326) GENERATED ALWAYS AS
                                (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)) STORED
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_mmsi ON positions (mmsi)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_heure ON positions (heure_reception)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_geom ON positions USING GIST (geom)")

        # Table de dimension : une ligne par navire, mise à jour à chaque message statique
        cur.execute("""
            CREATE TABLE IF NOT EXISTS navires (
                mmsi            INTEGER PRIMARY KEY,
                nom             TEXT,
                imo             INTEGER,
                indicatif       TEXT,
                type_navire     INTEGER,
                longueur_m      INTEGER,
                largeur_m       INTEGER,
                tirant_eau_m    REAL,
                destination     TEXT,
                derniere_maj    TIMESTAMPTZ NOT NULL
            )
        """)
    connexion.commit()


def lire_heure_ais(texte):
    """Convertit '2026-09-29 23:07:39.766227414 +0000 UTC' en datetime UTC."""
    if not texte:
        return None
    try:
        date, heure = texte.split(" ")[:2]
        if "." in heure:
            secondes, fraction = heure.split(".")
            heure = f"{secondes}.{fraction[:6]}"  # Python gère 6 décimales maximum
        return datetime.fromisoformat(f"{date} {heure}").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def extraire_position(message):
    meta = message["MetaData"]
    pos = message["Message"]["PositionReport"]
    return (
        meta["MMSI"],
        meta.get("ShipName", "").strip(),
        meta["latitude"],
        meta["longitude"],
        pos.get("Sog"),
        pos.get("Cog"),
        pos.get("TrueHeading"),
        pos.get("NavigationalStatus"),
        lire_heure_ais(meta.get("time_utc")),
        datetime.now(timezone.utc),
    )


def extraire_navire(message):
    meta = message["MetaData"]
    stat = message["Message"]["ShipStaticData"]
    dim = stat.get("Dimension") or {}
    # Longueur = distance antenne-proue (A) + antenne-poupe (B) ; largeur = C + D
    longueur = (dim.get("A") or 0) + (dim.get("B") or 0)
    largeur = (dim.get("C") or 0) + (dim.get("D") or 0)
    return (
        meta["MMSI"],
        (stat.get("Name") or "").strip(),
        stat.get("ImoNumber") or None,
        (stat.get("CallSign") or "").strip(),
        stat.get("Type"),
        longueur or None,
        largeur or None,
        stat.get("MaximumStaticDraught") or None,
        (stat.get("Destination") or "").strip(),
        datetime.now(timezone.utc),
    )


def ecrire_lots(connexion, positions, navires):
    with connexion.cursor() as cur:
        if positions:
            cur.executemany("""
                INSERT INTO positions (mmsi, nom, latitude, longitude, vitesse_noeuds,
                                       route_deg, cap_deg, statut_nav, heure_ais, heure_reception)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, positions)
        if navires:
            # "Upsert" : on insère le navire, ou on met à jour sa ligne s'il existe déjà
            cur.executemany("""
                INSERT INTO navires (mmsi, nom, imo, indicatif, type_navire, longueur_m,
                                     largeur_m, tirant_eau_m, destination, derniere_maj)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (mmsi) DO UPDATE SET
                    nom = EXCLUDED.nom,
                    imo = EXCLUDED.imo,
                    indicatif = EXCLUDED.indicatif,
                    type_navire = EXCLUDED.type_navire,
                    longueur_m = EXCLUDED.longueur_m,
                    largeur_m = EXCLUDED.largeur_m,
                    tirant_eau_m = EXCLUDED.tirant_eau_m,
                    destination = EXCLUDED.destination,
                    derniere_maj = EXCLUDED.derniere_maj
            """, navires)
    connexion.commit()


async def collecter(connexion):
    positions, navires = [], []
    dernier_ecrit = time.monotonic()
    total_pos, total_nav = 0, 0

    def vider():
        nonlocal positions, navires, total_pos, total_nav, dernier_ecrit
        ecrire_lots(connexion, positions, navires)
        total_pos += len(positions)
        total_nav += len(navires)
        heure = datetime.now().strftime("%H:%M:%S")
        print(f"{heure} | +{len(positions)} positions, +{len(navires)} fiches navires "
              f"(session : {total_pos} positions, {total_nav} fiches)", flush=True)
        positions, navires = [], []
        dernier_ecrit = time.monotonic()

    async with websockets.connect(URL) as ws:
        await ws.send(json.dumps({
            "APIKey": API_KEY,
            "BoundingBoxes": [MANCHE],
            "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
        }))
        print("Connecté au flux AIS, collecte en cours...", flush=True)

        try:
            async for message_brut in ws:
                message = json.loads(message_brut)

                erreur = message.get("error") or message.get("Error")
                if erreur:
                    raise RuntimeError(f"Erreur du serveur : {erreur}")

                type_msg = message.get("MessageType")
                contenu = message.get("Message", {})
                if "MetaData" not in message or type_msg not in contenu:
                    continue

                if type_msg == "PositionReport":
                    positions.append(extraire_position(message))
                elif type_msg == "ShipStaticData":
                    navires.append(extraire_navire(message))

                if (len(positions) + len(navires) >= TAILLE_LOT
                        or time.monotonic() - dernier_ecrit >= DELAI_MAX_LOT):
                    vider()
        finally:
            if positions or navires:  # on n'oublie pas le dernier paquet en cas d'arrêt
                vider()


async def principal():
    connexion = psycopg.connect(CONNEXION_PG)
    print("Connecté à PostgreSQL.", flush=True)
    preparer_base(connexion)
    attente = 5
    try:
        while True:
            try:
                await collecter(connexion)
            except RuntimeError as e:
                print(e, flush=True)
                return
            except (websockets.ConnectionClosed, OSError) as e:
                print(f"Connexion perdue ({e}). Nouvelle tentative dans {attente} s...", flush=True)
                await asyncio.sleep(attente)
                attente = min(attente * 2, 60)  # on attend de plus en plus, jusqu'à 1 minute
            else:
                attente = 5
    finally:
        connexion.close()


if __name__ == "__main__":
    if not API_KEY:
        print("La variable AISSTREAM_API_KEY est absente du fichier .env.")
    else:
        try:
            asyncio.run(principal())
        except KeyboardInterrupt:
            print("\nArrêt de la collecte.")
