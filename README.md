# 🌅 Sunrise Space

Application web + mobile (PWA installable) qui met en relation les **clients** et les **chauffeurs de moto-taxi** partout en Côte d'Ivoire. Gratuit au lancement, prêt pour un abonnement plus tard.

## Ce que fait l'application

**Client**
- Crée un compte (téléphone + mot de passe), voit sur une **carte et une liste** les motos **disponibles les plus proches** (distance, plaque, commune).
- **Appelle** ou écrit sur **WhatsApp** au chauffeur en un clic. Paiement en espèces, hors application.
- **Signale un besoin de moto** (demande de course) : les chauffeurs proches la voient et un seul peut l'accepter.
- **Signale un chauffeur** (comportement, sécurité, fraude, prix, absence…).
- Si le GPS est refusé : choix de sa commune ou clic sur la carte.

**Chauffeur**
- S'inscrit avec plaque, permis, commune. **Invisible tant que l'admin n'a pas validé** son profil.
- Un interrupteur **Disponible / Indisponible**. Sa position est partagée toutes les 20 s tant qu'il est disponible.
- Voit les demandes proches, accepte, appelle le client (numéro révélé seulement après acceptation), termine ou abandonne la course. Peut signaler un client.

**Administrateur** — tableau de bord : statistiques, validation / refus des chauffeurs, recherche et suspension des comptes, traitement des signalements (avec suspension en un clic).

**Sécurité intégrée** : mots de passe hachés, sessions à jeton, blocage après 5 mauvais essais, rôles contrôlés côté serveur, position d'un chauffeur masquée s'il est indisponible ou sans signal depuis 10 min, numéro du client caché avant acceptation, en-têtes de sécurité (CSP), protection contre l'injection de code dans l'affichage.

## Lancer en local

```bash
pip install -r requirements.txt
python app.py
```
Ouvrir http://localhost:5000

Au **premier lancement**, un compte admin est créé et son mot de passe est **affiché dans le terminal** (une seule fois). Pour choisir le vôtre :
```bash
ADMIN_PHONE=0700000000 ADMIN_PASSWORD=MonMotDePasse python app.py
```

Tests : `python test_app.py`

## Mettre en ligne

Commande de démarrage : `gunicorn app:app` (un `Procfile` est fourni). Fonctionne sur tout hébergeur Python (Render, Railway, VPS…).

⚠️ **À savoir avant la mise en ligne**
1. **HTTPS obligatoire** : sans lui, la géolocalisation et l'installation sur téléphone ne marchent pas (les hébergeurs le fournissent gratuitement).
2. **Base de données** : SQLite stocke tout dans le fichier `sunrise.db`. Sur un hébergeur à disque temporaire, **les données seraient perdues à chaque redéploiement**. Utilisez un **disque persistant** (variable `SUNRISE_DB=/chemin/du/disque/sunrise.db`) ou migrez vers PostgreSQL quand vous grandirez.
3. **Position en arrière-plan** : une application web n'envoie la position que **tant qu'elle est ouverte à l'écran**. Pour un suivi en continu, il faudra plus tard une vraie application Android/iOS.
4. **Carte** : OpenStreetMap, gratuit pour un lancement modeste ; au-delà, prendre un fournisseur de cartes dédié.
5. **Données personnelles** : vous collectez téléphones et positions. Avant le lancement public, ajoutez des conditions d'utilisation et une politique de confidentialité conformes à la réglementation ivoirienne (ARTCI).

## Organisation du code

| Fichier | Rôle |
|---|---|
| `app.py` | Serveur : API, base de données, rôles, sécurité |
| `index.html`, `app.js`, `style.css` | Interface (client, chauffeur, admin) |
| `manifest.webmanifest`, `sw.js`, `icon-*.png` | Installation sur téléphone (PWA) |
| `test_app.py` | Tests automatiques des parcours |

## Idées pour la suite
Notes et avis sur les chauffeurs · historique de courses côté chauffeur · notifications · abonnement chauffeur (Mobile Money : Orange, MTN, Wave) · application native · multilingue (dioula, anglais).
