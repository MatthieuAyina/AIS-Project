# Image de base : Python 3.12 en version allégée
FROM python:3.12-slim

# Dossier de travail dans le conteneur
WORKDIR /app

# Affiche les print() immédiatement dans les logs
ENV PYTHONUNBUFFERED=1

# On installe d'abord les dépendances (mises en cache tant que requirements.txt ne change pas)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Puis on copie le code
COPY ais_collecte.py .

# Commande lancée au démarrage du conteneur
CMD ["python", "ais_collecte.py"]
