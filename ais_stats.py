import os

import psycopg
from dotenv import load_dotenv

load_dotenv()

CONNEXION_PG = (
    f"host={os.environ.get('POSTGRES_HOST', 'localhost')} "
    f"port={os.environ.get('POSTGRES_PORT', '5432')} "
    f"dbname={os.environ['POSTGRES_DB']} "
    f"user={os.environ['POSTGRES_USER']} "
    f"password={os.environ['POSTGRES_PASSWORD']}"
)


def libelle_type(code):
    """Traduit le code AIS du type de navire en catégorie lisible."""
    if code is None:
        return "Inconnu"
    if 60 <= code <= 69:
        return "Passagers / ferry"
    if 70 <= code <= 79:
        return "Cargo"
    if 80 <= code <= 89:
        return "Pétrolier / tanker"
    categories = {30: "Pêche", 31: "Remorquage", 32: "Remorquage", 35: "Militaire",
                  36: "Voilier", 37: "Plaisance", 50: "Pilote", 51: "Sauvetage",
                  52: "Remorqueur", 53: "Navire de port", 55: "Police"}
    return categories.get(code, "Autre")


with psycopg.connect(CONNEXION_PG) as connexion:
    total, navires, debut, fin = connexion.execute("""
        SELECT COUNT(*), COUNT(DISTINCT mmsi), MIN(heure_reception), MAX(heure_reception)
        FROM positions
    """).fetchone()

    print(f"Positions enregistrées : {total}")
    print(f"Navires distincts      : {navires}")
    print(f"Première réception     : {debut}")
    print(f"Dernière réception     : {fin}")

    fiches = connexion.execute("SELECT COUNT(*) FROM navires").fetchone()[0]
    print(f"Fiches navires         : {fiches}\n")

    print("Les 10 navires les plus actifs :")
    for nom, mmsi, nb in connexion.execute("""
        SELECT MAX(nom), mmsi, COUNT(*) AS nb
        FROM positions
        GROUP BY mmsi
        ORDER BY nb DESC
        LIMIT 10
    """):
        print(f"  {nom:<25} MMSI {mmsi}  {nb} positions")

    # Jointure entre la table de faits et la table de dimension
    comptage = {}
    for code, nb in connexion.execute("""
        SELECT n.type_navire, COUNT(DISTINCT p.mmsi)
        FROM positions p
        JOIN navires n ON n.mmsi = p.mmsi
        GROUP BY n.type_navire
    """):
        libelle = libelle_type(code)
        comptage[libelle] = comptage.get(libelle, 0) + nb

    print("\nNavires par catégorie (parmi ceux qui ont une fiche) :")
    for libelle, nb in sorted(comptage.items(), key=lambda x: -x[1]):
        print(f"  {libelle:<20} {nb}")
