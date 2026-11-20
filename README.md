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

**Messagerie** — chaque utilisateur (client ou chauffeur) a un onglet **Messages** avec une pastille « non lu » : il y reçoit les **annonces** de l'administrateur et peut **écrire à l'équipe**. L'administrateur publie des annonces (à tous, aux clients ou aux chauffeurs), voit combien de personnes les ont lues, répond dans sa boîte de réception et peut écrire à n'importe quel utilisateur depuis l'onglet Utilisateurs.

**Administrateur** — tableau de bord : statistiques, validation / refus des chauffeurs, recherche et suspension des comptes, traitement des signalements (avec suspension en un clic).

**Sécurité intégrée** : mots de passe hachés, sessions à jeton, blocage après 5 mauvais essais, rôles contrôlés côté serveur, position d'un chauffeur masquée s'il est indisponible ou sans signal depuis 10 min, numéro du client caché avant acceptation, en-têtes de sécurité (CSP), protection contre l'injection de code dans l'affichage.

## Sauvegardes

- **Bouton dans l'application** : onglet *Aperçu* (administrateur) › « Télécharger une sauvegarde » → fichier `.json` avec toutes les données.
- **Automatique** : `.github/workflows/backup.yml` sauvegarde la base chaque nuit sur GitHub (fichier chiffré, conservé 90 jours). Secrets à créer dans GitHub › Settings › Secrets and variables › Actions : `DATABASE_URL` (l'adresse de la base Neon) et `BACKUP_PASSPHRASE` (un mot de passe long à garder précieusement).
- **Restaurer depuis l'application** : *Aperçu* › « Restaurer depuis un fichier » → choisir un `.json` téléchargé plus tôt. Aperçu (comptes dans le fichier / actuellement), copie de sécurité à télécharger d'abord, puis confirmation en tapant `RESTAURER`. Le fichier est vérifié avant toute modification et tout est annulé en cas d'erreur. Tout le monde est déconnecté ; on se reconnecte avec l'administrateur *de la sauvegarde*.
- **Restaurer depuis GitHub / en ligne de commande** : `DATABASE_URL=… python restore_backup.py sauvegarde.json.gz` (refuse d'écraser une base remplie sauf avec `--force`). Pour un fichier chiffré : `gpg -d sunrise-backup.json.gz.gpg > sunrise-backup.json.gz` d'abord.

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
2. **Base de données durable (gratuit)** : définissez la variable `DATABASE_URL` avec l'adresse d'une base PostgreSQL (ex. Neon). Sans elle, SQLite est utilisé (et effacé sur un hébergeur gratuit à chaque redémarrage). Ancienne note : SQLite stocke tout dans le fichier `sunrise.db`. Sur un hébergeur à disque temporaire, **les données seraient perdues à chaque redéploiement**. Utilisez un **disque persistant** (variable `SUNRISE_DB=/chemin/du/disque/sunrise.db`) ou migrez vers PostgreSQL quand vous grandirez.
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

## Types de véhicule et mot de passe oublié

- **Types de véhicule** : chaque chauffeur choisit à l'inscription *moto-taxi*, *taxi (voiture)* ou *tricycle* (🏍️ 🚕 🛺). Le client voit l'icône correspondante sur la carte, sur la carte chauffeur et pendant le suivi de sa course, et peut filtrer la liste par type. Les anciens chauffeurs deviennent « moto » automatiquement (la base est mise à jour au démarrage).
- **Mot de passe oublié** : l'écran de connexion envoie une demande à l'administrateur (réponse identique que le numéro existe ou non, 5 demandes/heure par adresse). Dans *Utilisateurs*, l'admin vérifie l'identité puis génère un mot de passe provisoire (affiché une seule fois, avec envoi WhatsApp). La personne est déconnectée partout et doit choisir son propre mot de passe à la première connexion. Chacun peut aussi changer son mot de passe depuis *Compte*. Le mot de passe de l'administrateur se réinitialise avec `ADMIN_PASSWORD` + `RESET_ADMIN_PASSWORD`.

- **Choix du véhicule à la demande** : le client indique *Peu importe*, *Moto*, *Voiture* ou *Tricycle* (avec le nombre de chauffeurs disponibles de chaque type). Seuls les chauffeurs du type voulu voient et peuvent accepter la demande ; « Peu importe » prévient tout le monde, comme avant.

## Supprimer définitivement un utilisateur

Admin → onglet **Utilisateurs** → bouton **🗑️ Supprimer** sur la fiche, puis taper `SUPPRIMER`.
Efface le compte, ses sessions, messages et signalements, et ses demandes de course (client).
Pour un chauffeur : l'historique des clients est conservé sans lui, et une course en cours est annulée.
Irréversible : faites une sauvegarde avant si besoin. Pour seulement bloquer quelqu'un, utilisez « Suspendre ».

## Notifications push (application fermée)

Un navigateur ne peut pas suivre le GPS quand l'application est fermée (limite d'iOS), mais il peut recevoir des notifications :
- chauffeur « disponible » : alerte pour chaque nouvelle demande à moins de 15 km de sa dernière position connue (moins de 12 h), du bon type de véhicule ;
- client : alerte quand un chauffeur accepte, ou abandonne ;
- chauffeur : alerte si le client annule ;
- chauffeur en course dont la position n'est plus à jour (2 min) : rappel « Ouvrez Sunrise Space pour partager votre position » (au plus un toutes les 4 min, déclenché tant que l'application du client est ouverte).

Sur iPhone (iOS 16.4 minimum) : Safari → Partager → Sur l'écran d'accueil, ouvrir l'icône, puis toucher « Activer » dans le bandeau ou dans Compte.
La clé d'envoi (VAPID) est créée automatiquement et gardée dans la base ; variables facultatives : `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` (mailto: ou https:).
Dépendance ajoutée : `cryptography` (requirements.txt).

## Phase test : améliorations

- **Chauffeurs fantômes** : un chauffeur « Disponible » sans position depuis 2 h repasse automatiquement « Indisponible » et reçoit une notification.
- **Notes** : après une course terminée, le client note le chauffeur de 1 à 5 étoiles (une seule fois). La note moyenne et le nombre de courses apparaissent chez le chauffeur, chez le client pendant la course, et en moyenne dans l'aperçu admin.
- **Réseau faible** : un bandeau rouge « Connexion perdue » s'affiche et disparaît à la reconnexion.
- Aperçu admin : courses sur 7 jours et note moyenne.

## Protection contre les erreurs

- **Corbeille (30 jours)** : avant chaque suppression définitive, le compte (mot de passe, profil chauffeur, messages, courses) est archivé. Admin → Utilisateurs → « ♻️ Corbeille » → Restaurer ce compte (refusé si le numéro a été repris entre-temps).
- **Copie avant restauration** : à chaque restauration depuis un fichier, le serveur garde l'état précédent (les 3 dernières). Admin → Aperçu → Sauvegarde → Télécharger, puis « Restaurer depuis un fichier » pour annuler.
- **Sauvegarde GitHub toutes les 6 heures** (03h, 09h, 15h, 21h environ) au lieu d'une par jour : au pire 6 h de données perdues.

## Demandes « bruyantes »

- **Application fermée ou en veille** : la notification est marquée urgente (reste affichée jusqu'au toucher, vibration longue d'environ 10 s sur Android) et elle est renvoyée 3 fois, toutes les 12 s, tant que personne n'a accepté la demande. Le son lui-même est celui du téléphone : une application web ne peut ni le choisir ni le prolonger (réglages du téléphone, mode silencieux et « Ne pas déranger » compris).
- **Application ouverte** : sirène de 10 s (coupée au premier toucher) + vibration + bandeau orange clignotant à chaque nouvelle demande. Réglable dans Compte → « Sirène des demandes » (avec bouton Tester).

## Compte « Validateur » (accès réduit)

Admin → Utilisateurs → section **🧑‍💼 Validateurs** → « Ajouter un validateur » (nom, téléphone, mot de passe provisoire). Il choisit son mot de passe à sa première connexion.
Il peut seulement : valider ou refuser les chauffeurs, voir la liste des clients et chauffeurs inscrits, changer son mot de passe.
Tout le reste est refusé par le serveur (statistiques, messages, signalements, sauvegarde/restauration, suppression, suspension, validateurs…).
Le validateur est un compte `admin` avec `staff_scope='validator'` ; la liste des pages autorisées est `VALIDATOR_ENDPOINTS` dans `app.py` (tout ce qui n'y figure pas est interdit).
Boutons de l'admin : « Nouveau mot de passe » (il est déconnecté) et « Retirer l'accès » (compte supprimé).

## Confidentialité et suivi (admin)

- **Le client ne voit plus la liste des chauffeurs** : il envoie sa demande et attend qu'un chauffeur l'accepte. L'onglet « Autour de moi » est supprimé et `/api/drivers/nearby` est réservé à l'admin. Le client ne voit les informations du chauffeur (nom, numéro, véhicule, position) qu'après l'acceptation.
- **Le chauffeur ne voit pas le numéro du client avant d'accepter**, ni son nom de famille (prénom seulement). Après acceptation : nom complet, Appeler, WhatsApp.
- **Admin → Aperçu** : « En ligne maintenant » (clients et chauffeurs ayant l'application ouverte à l'écran dans les 3 dernières minutes, rafraîchi toutes les 20 s) et « Demandes de course » (annulées, expirées, terminées, en attente).
- **Admin → Messages → Bienvenue** : message automatique envoyé aux nouveaux clients et/ou chauffeurs à leur inscription (`{prenom}` = prénom). Vide = rien n'est envoyé. Les inscrits existants ne le reçoivent pas.
- **Bouton « Déconnexion »** en haut de l'écran administrateur.

## Destination sur la carte

- **Client** : en tapant la destination, des lieux sont proposés (OpenStreetMap / Nominatim, Côte d'Ivoire, autour de sa position, `/api/geocode`, mémoire d'une heure, 1 recherche par seconde au maximum). Il peut aussi toucher **« Choisir la destination sur la carte »**. Sans point choisi, la demande marche comme avant (texte seul).
- **Chauffeur, avant d'accepter** : « Trajet : environ X km » (à vol d'oiseau) ; la notification de demande l'indique aussi.
- **Chauffeur, course acceptée** : deux étapes, **① Rejoindre le client** et **② Aller à la destination**. La carte montre sa position, 🙋 le client, 🏁 la destination, l'itinéraire de l'étape en cours (temps et kilomètres par la route), le trajet du client, et les boutons Plans / Google Maps naviguent vers l'étape en cours. Sans point sur la carte, des boutons de recherche par nom de lieu sont proposés.
- Colonnes ajoutées à `ride_requests` : `dest_lat`, `dest_lng` (ajoutées automatiquement aux bases existantes). Destination à plus de 300 km du départ : refusée.

## Appels Sunrise (numéros masqués)
- Pendant une course acceptée, le client et le chauffeur s'appellent depuis l'application (voix par internet, gratuite). L'écran de l'autre affiche « Sunrise · Appel entrant » avec Répondre / Refuser.
- Aucun numéro n'est transmis entre client et chauffeur (ni dans l'API, ni à l'écran, ni en lien tel:/WhatsApp). Seul l'admin voit les numéros.
- Le serveur ne fait que relayer la mise en relation ; la voix passe directement d'un téléphone à l'autre (WebRTC). Une notification urgente prévient l'autre personne.
- Variables facultatives pour les réseaux difficiles (serveur TURN) : `TURN_URLS` (ex. `turn:host:3478`), `TURN_USERNAME`, `TURN_CREDENTIAL`, ou `CALL_ICE_SERVERS` (JSON).
- Limites : l'application doit être ouverte pour sonner ; le micro doit être autorisé.
