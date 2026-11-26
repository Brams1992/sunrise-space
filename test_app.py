"""Tests des parcours principaux de Sunrise Space.   Lancer :  python test_app.py"""
import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ["SUNRISE_DB"] = os.path.join(_tmp, "test.db")
os.environ["ADMIN_PHONE"] = "0700000000"
os.environ["ADMIN_PASSWORD"] = "admin-secret"

import app as sunrise  # noqa: E402

ABIDJAN = (5.3500, -3.9833)      # Cocody
NEAR = (5.3560, -3.9800)         # ~0,7 km
FAR = (7.6906, -5.0303)          # Bouaké


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = sunrise.app.test_client()

    def setUp(self):
        """Base vierge avant chaque test (+ compte admin), pour des tests indépendants."""
        with sunrise.app.app_context():
            db = sunrise.get_db()
            for table in ("call_signals", "calls", "safety_copies", "push_subscriptions", "announcement_reads", "announcements", "messages", "reports", "ride_requests", "password_resets", "tokens", "drivers", "users"):
                db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM sqlite_sequence")
            db.commit()
            sunrise.ensure_admin(db)
        sunrise._login_fails.clear()
        sunrise._forgot_hits.clear()
        sunrise._nudges.clear()
        sunrise._geo_cache.clear(); sunrise._geo_last[0] = 0
        sunrise._last_sweep[0] = 0
        sunrise.PUSH_REMINDER_DELAYS = ()

    def call(self, method, url, token=None, **kw):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return getattr(self.c, method)(url, headers=headers, **kw)

    def register(self, role, name, phone, **driver):
        body = {"role": role, "name": name, "phone": phone, "password": "secret1"}
        if role == "driver":
            body["driver"] = {"plate": "1234 AB 01", "permis": "CI-998877", "commune": "Cocody",
                              "moto_model": "Jakarta", **driver}
        r = self.call("post", "/api/auth/register", json=body)
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["token"], r.get_json()["user"]["id"]

    def login(self, phone, password):
        r = self.call("post", "/api/auth/login", json={"phone": phone, "password": password})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["token"]


class TestSunrise(Base):
    def test_full_flow(self):
        admin = self.login("0700000000", "admin-secret")
        client, client_id = self.register("client", "Awa Koné", "07 07 07 07 07")
        d1, d1_id = self.register("driver", "Moussa Traoré", "0501010101")
        d2, d2_id = self.register("driver", "Ibrahim Diallo", "0102030405", plate="9999 ZZ 01")

        # Pas de rôle admin via l'inscription, numéro en doublon refusé
        r = self.call("post", "/api/auth/register", json={"role": "admin", "name": "x y", "phone": "0909090909", "password": "secret1"})
        self.assertEqual(r.status_code, 400)
        r = self.call("post", "/api/auth/register", json={"role": "client", "name": "Awa", "phone": "+225 07 07 07 07 07", "password": "secret1"})
        self.assertEqual(r.status_code, 409)

        # Un chauffeur non validé ne peut pas être disponible
        r = self.call("post", "/api/driver/location", d1, json={"lat": NEAR[0], "lng": NEAR[1]})
        self.assertEqual(r.status_code, 200)
        r = self.call("put", "/api/driver/status", d1, json={"availability": "available"})
        self.assertEqual(r.status_code, 403)

        # Contrôle des droits
        self.assertEqual(self.call("get", "/api/admin/stats", client).status_code, 403)
        self.assertEqual(self.call("get", "/api/admin/stats").status_code, 401)
        self.assertEqual(self.call("get", "/api/driver/dashboard", client).status_code, 403)

        # L'admin valide d1 et refuse d2
        pend = self.call("get", "/api/admin/drivers?approval=pending", admin).get_json()["drivers"]
        self.assertEqual(len(pend), 2)
        self.assertEqual(self.call("post", f"/api/admin/drivers/{d1_id}/approval", admin, json={"decision": "approved"}).status_code, 200)
        self.assertEqual(self.call("post", f"/api/admin/drivers/{d2_id}/approval", admin, json={"decision": "rejected", "note": "Plaque illisible"}).status_code, 200)

        # d1 devient disponible et apparaît pour le client ; d2 jamais
        self.call("post", "/api/driver/location", d2, json={"lat": NEAR[0], "lng": NEAR[1]})
        self.assertEqual(self.call("put", "/api/driver/status", d2, json={"availability": "available"}).status_code, 403)
        self.assertEqual(self.call("put", "/api/driver/status", d1, json={"availability": "available"}).status_code, 200)
        near = self.call("get", f"/api/drivers/nearby?lat={ABIDJAN[0]}&lng={ABIDJAN[1]}", admin).get_json()["drivers"]
        self.assertEqual([x["id"] for x in near], [d1_id])
        self.assertLess(near[0]["distance_km"], 2)
        self.assertEqual(near[0]["whatsapp"], "0501010101")
        far = self.call("get", f"/api/drivers/nearby?lat={FAR[0]}&lng={FAR[1]}", admin).get_json()["drivers"]
        self.assertEqual(far, [])

        # Position périmée => le chauffeur disparaît
        with sunrise.app.app_context():
            db = sunrise.get_db()
            db.execute("UPDATE drivers SET location_at='2000-01-01 00:00:00' WHERE user_id=?", (d1_id,))
            db.commit()
        self.assertEqual(self.call("get", f"/api/drivers/nearby?lat={ABIDJAN[0]}&lng={ABIDJAN[1]}", admin).get_json()["drivers"], [])
        self.call("post", "/api/driver/location", d1, json={"lat": NEAR[0], "lng": NEAR[1]})

        # Demande de moto
        r = self.call("post", "/api/requests", client, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1], "pickup_text": "Pharmacie", "destination_text": "Marché"})
        self.assertEqual(r.status_code, 201, r.get_json())
        rid = r.get_json()["request"]["id"]
        self.assertEqual(self.call("post", "/api/requests", client, json={"lat": 5.3, "lng": -4.0}).status_code, 409)

        # Le client bouge : sa position est mise à jour (et seulement par lui)
        self.assertEqual(self.call("post", f"/api/requests/{rid}/location", client, json={"lat": 5.3510, "lng": -3.9840}).status_code, 200)
        self.assertEqual(self.call("post", f"/api/requests/{rid}/location", client, json={"lat": 999, "lng": 0}).status_code, 400)
        self.assertEqual(self.call("post", f"/api/requests/{rid}/location", d1, json={"lat": 5.3, "lng": -4.0}).status_code, 403)
        dash = self.call("get", "/api/driver/dashboard", d1).get_json()
        self.assertEqual([x["id"] for x in dash["requests"]], [rid])
        self.assertNotIn("client_phone", dash["requests"][0])  # numéro caché avant acceptation

        # Un chauffeur non validé ne peut pas accepter
        self.assertEqual(self.call("post", f"/api/requests/{rid}/accept", d2).status_code, 403)

        # Acceptation : une seule personne peut prendre la course
        self.assertEqual(self.call("post", f"/api/requests/{rid}/accept", d1).status_code, 200)
        self.assertEqual(self.call("post", f"/api/requests/{rid}/accept", d1).status_code, 409)
        dash = self.call("get", "/api/driver/dashboard", d1).get_json()
        self.assertEqual(dash["driver"]["availability"], "busy")
        self.assertNotIn("client_phone", dash["job"])   # le numéro du client ne passe jamais au chauffeur
        cur = self.call("get", "/api/requests/current", client).get_json()["request"]
        self.assertEqual(cur["status"], "accepted")
        self.assertEqual(cur["driver"]["plate"], "1234 AB 01")
        self.assertEqual(self.call("get", f"/api/drivers/nearby?lat={ABIDJAN[0]}&lng={ABIDJAN[1]}", admin).get_json()["drivers"], [])

        # Signalements (client -> chauffeur, chauffeur -> client)
        r = self.call("post", "/api/reports", client, json={"target_user_id": d1_id, "reason": "prix", "details": "Trop cher", "request_id": rid})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.call("post", "/api/reports", client, json={"target_user_id": client_id, "reason": "prix"}).status_code, 400)
        self.assertEqual(self.call("post", "/api/reports", client, json={"target_user_id": d1_id, "reason": "inconnu"}).status_code, 400)
        self.assertEqual(self.call("post", "/api/reports", d1, json={"target_user_id": dash["job"]["client_id"], "reason": "comportement"}).status_code, 201)

        # Fin de course
        self.assertEqual(self.call("post", f"/api/requests/{rid}/complete", client).status_code, 200)
        self.assertEqual(self.call("post", f"/api/requests/{rid}/location", client, json={"lat": 5.3, "lng": -4.0}).status_code, 409)
        self.assertEqual(self.call("get", "/api/driver/dashboard", d1).get_json()["driver"]["availability"], "available")

        # Admin : stats + signalements + suspension
        st = self.call("get", "/api/admin/stats", admin).get_json()
        self.assertEqual((st["clients"], st["drivers"], st["rides_completed"], st["reports_open"]), (1, 2, 1, 2))
        reps = self.call("get", "/api/admin/reports?status=open", admin).get_json()["reports"]
        self.assertEqual(len(reps), 2)
        rep = next(x for x in reps if x["target_id"] == d1_id)
        self.assertEqual(self.call("post", f"/api/admin/reports/{rep['id']}", admin, json={"status": "resolved", "note": "Avertissement", "block_target": True}).status_code, 200)
        self.assertEqual(self.call("get", "/api/me", d1).status_code, 401)  # sessions supprimées
        r = self.call("post", "/api/auth/login", json={"phone": "0501010101", "password": "secret1"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.call("post", f"/api/admin/users/{d1_id}/block", admin, json={"blocked": False}).status_code, 200)
        self.assertEqual(self.call("post", "/api/auth/login", json={"phone": "0501010101", "password": "secret1"}).status_code, 200)

    def test_messaging(self):
        admin = self.login("0700000000", "admin-secret")
        client, cid = self.register("client", "Awa Koné", "0707070707")
        driver, did = self.register("driver", "Moussa Traoré", "0501010101")

        # Droits : l'admin seul publie ; les utilisateurs ne peuvent pas
        r = self.call("post", "/api/admin/announcements", client, json={"audience": "all", "title": "x", "body": "y"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.call("get", "/api/inbox").status_code, 401)
        self.assertEqual(self.call("get", "/api/inbox", admin).status_code, 403)

        # Validation des champs
        r = self.call("post", "/api/admin/announcements", admin, json={"audience": "all", "title": "", "body": "y"})
        self.assertEqual(r.status_code, 400)
        r = self.call("post", "/api/admin/announcements", admin, json={"audience": "zzz", "title": "t", "body": "y"})
        self.assertEqual(r.status_code, 400)

        # Annonce ciblée : seuls les chauffeurs la reçoivent
        r = self.call("post", "/api/admin/announcements", admin,
                      json={"audience": "driver", "title": "Phase test", "body": "Ligne 1\nLigne 2 <b>gras</b>"})
        self.assertEqual(r.status_code, 201)
        r = self.call("post", "/api/admin/announcements", admin, json={"audience": "all", "title": "Bienvenue", "body": "Merci"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.call("get", "/api/inbox/unread", client).get_json()["unread"], 1)
        self.assertEqual(self.call("get", "/api/inbox/unread", driver).get_json()["unread"], 2)
        box = self.call("get", "/api/inbox", driver).get_json()
        self.assertEqual([a["title"] for a in box["announcements"]], ["Bienvenue", "Phase test"])
        self.assertIn("\n", box["announcements"][1]["body"])  # retours à la ligne conservés
        self.assertTrue(all(a["unread"] for a in box["announcements"]))

        # Lecture : la pastille retombe à 0
        self.assertEqual(self.call("post", "/api/inbox/read", driver).status_code, 200)
        self.assertEqual(self.call("get", "/api/inbox/unread", driver).get_json()["unread"], 0)
        stats = self.call("get", "/api/admin/announcements", admin).get_json()["announcements"]
        by = {a["title"]: a for a in stats}
        self.assertEqual((by["Phase test"]["reads"], by["Phase test"]["audience_size"]), (1, 1))
        self.assertEqual((by["Bienvenue"]["reads"], by["Bienvenue"]["audience_size"]), (1, 2))

        # Le client écrit à l'équipe ; l'admin voit un message non lu
        r = self.call("post", "/api/inbox/messages", client, json={"body": "   "})
        self.assertEqual(r.status_code, 400)
        r = self.call("post", "/api/inbox/messages", client, json={"body": "Bonjour,\nj'ai une question"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.call("get", "/api/inbox/unread", admin).get_json()["unread"], 1)
        self.assertEqual(self.call("get", "/api/admin/stats", admin).get_json()["messages_unread"], 1)
        convs = self.call("get", "/api/admin/conversations", admin).get_json()["conversations"]
        self.assertEqual((convs[0]["id"], convs[0]["unread"]), (cid, 1))

        # Ouvrir la conversation la marque lue ; l'admin répond
        th = self.call("get", f"/api/admin/conversations/{cid}", admin).get_json()
        self.assertEqual(th["messages"][0]["sender"], "user")
        self.assertEqual(self.call("get", "/api/inbox/unread", admin).get_json()["unread"], 0)
        r = self.call("post", f"/api/admin/conversations/{cid}", admin, json={"body": "Bonjour Awa, voici la réponse"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.call("get", "/api/inbox/unread", client).get_json()["unread"], 2)  # annonce + réponse
        box = self.call("get", "/api/inbox", client).get_json()
        self.assertEqual([m["sender"] for m in box["messages"]], ["user", "admin"])
        self.assertEqual(box["messages"][1]["unread"], 1)
        self.call("post", "/api/inbox/read", client)
        self.assertEqual(self.call("get", "/api/inbox/unread", client).get_json()["unread"], 0)

        # Confidentialité : le chauffeur ne voit pas la conversation du client
        box = self.call("get", "/api/inbox", driver).get_json()
        self.assertEqual(box["messages"], [])
        # L'admin ne peut pas écrire à un compte admin ou inexistant
        self.assertEqual(self.call("post", "/api/admin/conversations/99999", admin, json={"body": "x"}).status_code, 404)
        self.assertEqual(self.call("get", "/api/admin/conversations/99999", admin).status_code, 404)

        # Message direct de l'admin à un chauffeur
        self.call("post", f"/api/admin/conversations/{did}", admin, json={"body": "Pensez à valider vos documents"})
        self.assertEqual(self.call("get", "/api/inbox/unread", driver).get_json()["unread"], 1)

        # Limite d'envoi (20/heure) et suppression d'annonce
        for _ in range(19):
            self.assertEqual(self.call("post", "/api/inbox/messages", client, json={"body": "spam"}).status_code, 201)
        self.assertEqual(self.call("post", "/api/inbox/messages", client, json={"body": "spam"}).status_code, 429)
        aid = by["Phase test"]["id"]
        self.assertEqual(self.call("delete", f"/api/admin/announcements/{aid}", admin).status_code, 200)
        self.assertEqual(len(self.call("get", "/api/admin/announcements", admin).get_json()["announcements"]), 1)

    def test_backup_export(self):
        admin = self.login("0700000000", "admin-secret")
        client, cid = self.register("client", "Awa Koné", "0707070707")
        self.call("post", "/api/inbox/messages", client, json={"body": "Bonjour"})
        self.assertEqual(self.call("get", "/api/admin/backup").status_code, 401)
        self.assertEqual(self.call("get", "/api/admin/backup", client).status_code, 403)
        r = self.call("get", "/api/admin/backup", admin)
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        data = r.get_json()
        self.assertEqual(data["format"], "sunrise-backup-1")
        self.assertEqual(len(data["tables"]["users"]), 2)
        self.assertEqual(data["tables"]["messages"][0]["body"], "Bonjour")
        self.assertNotIn("tokens", data["tables"])  # les sessions ne sont pas exportées

    def test_restore_from_app(self):
        admin = self.login("0700000000", "admin-secret")
        client, cid = self.register("client", "Awa Koné", "0707070707")
        self.call("post", "/api/inbox/messages", client, json={"body": "Bonjour"})
        self.call("post", "/api/admin/announcements", admin, json={"title": "Info", "body": "Test", "audience": "all"})
        backup = self.call("get", "/api/admin/backup", admin).get_json()
        # après la sauvegarde : un nouveau compte apparaît
        self.register("client", "Nouveau Venu", "0708080808")
        url = "/api/admin/restore"
        # sécurité d'accès
        self.assertEqual(self.call("post", url, json={"backup": backup}).status_code, 401)
        self.assertEqual(self.call("post", url, client, json={"backup": backup}).status_code, 403)
        # étape 1 : aperçu, sans rien modifier
        r = self.call("post", url, admin, json={"backup": backup})
        self.assertEqual(r.status_code, 200)
        info = r.get_json()
        self.assertTrue(info["dry_run"])
        self.assertEqual(info["backup"]["users"], 2)
        self.assertEqual(info["current"]["users"], 3)
        self.assertEqual(info["admin_phones"], ["0700000000"])
        with sunrise.app.app_context():
            self.assertEqual(sunrise.get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0], 3)
        # confirmation obligatoire
        for conf in (None, "restaurer", "OUI"):
            r = self.call("post", url, admin, json={"backup": backup, "dry_run": False, "confirmation": conf})
            self.assertEqual(r.status_code, 400)
        with sunrise.app.app_context():
            self.assertEqual(sunrise.get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0], 3)
        # étape 2 : restauration
        r = self.call("post", url, admin, json={"backup": backup, "dry_run": False, "confirmation": "RESTAURER"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertFalse(r.get_json()["dry_run"])
        with sunrise.app.app_context():
            db = sunrise.get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], 1)
        # les sessions sont effacées, l'admin de la sauvegarde se reconnecte
        self.assertEqual(self.call("get", "/api/admin/stats", admin).status_code, 401)
        admin2 = self.login("0700000000", "admin-secret")
        self.assertEqual(self.call("get", "/api/admin/stats", admin2).status_code, 200)
        # l'application continue de fonctionner (nouveaux comptes : les identifiants ne se télescopent pas)
        self.register("client", "Après Restauration", "0709090909")

    def test_restore_rejects_bad_files(self):
        admin = self.login("0700000000", "admin-secret")
        client, _ = self.register("client", "Awa Koné", "0707070707")
        good = self.call("get", "/api/admin/backup", admin).get_json()
        url = "/api/admin/restore"

        def go(payload, **extra):
            return self.call("post", url, admin, json={"backup": payload, "dry_run": False, "confirmation": "RESTAURER", **extra})

        def users():
            with sunrise.app.app_context():
                return sunrise.get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0]

        import copy
        bad = []
        bad.append({"format": "autre", "tables": {}})
        bad.append({"format": "sunrise-backup-1", "tables": []})
        b = copy.deepcopy(good); b["tables"]["users"][0]["colonne_inconnue"] = 1; bad.append(b)
        b = copy.deepcopy(good); b["tables"]["users"][0]['id) VALUES(1); DROP TABLE users; --'] = 1; bad.append(b)
        b = copy.deepcopy(good); b["tables"]["users"][0]["name"] = {"x": 1}; bad.append(b)
        b = copy.deepcopy(good); b["tables"]["messages"] = "oups"; bad.append(b)
        b = copy.deepcopy(good); b["tables"]["users"] = [u for u in b["tables"]["users"] if u["role"] != "admin"]; bad.append(b)
        b = copy.deepcopy(good); [u.update(is_blocked=1) for u in b["tables"]["users"] if u["role"] == "admin"]; bad.append(b)
        for payload in bad:
            self.assertEqual(go(payload).status_code, 400, payload)
        self.assertEqual(users(), 2)
        self.assertEqual(self.call("post", url, admin, json={"backup": "texte"}).status_code, 400)
        self.assertEqual(self.call("post", url, admin, json={}).status_code, 400)
        # échec au milieu de l'insertion (identifiant en double) : tout est annulé, rien n'est perdu
        b = copy.deepcopy(good); b["tables"]["users"].append(dict(b["tables"]["users"][-1]))
        r = go(b)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(users(), 2)
        self.assertEqual(self.call("get", "/api/admin/stats", admin).status_code, 200)  # sessions intactes
        # taille : plus de 64 Ko acceptés pour la restauration seulement
        big = copy.deepcopy(good)
        big["tables"]["announcements"] = [
            {"id": i + 1, "title": "x" * 70, "body": "y" * 1400, "audience": "all", "created_at": "2026-01-01T00:00:00"}
            for i in range(80)]
        r = self.call("post", url, admin, json={"backup": big})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["backup"]["announcements"], 80)
        r = self.call("post", "/api/inbox/messages", client, data=b"x" * 100_000, content_type="application/json")
        self.assertEqual(r.status_code, 413)

    def test_vehicle_types(self):
        admin = self.login("0700000000", "admin-secret")
        client, _ = self.register("client", "Awa Koné", "0707070707")
        # types acceptés ; sans type = moto ; type inconnu refusé
        m, mid = self.register("driver", "Moussa Moto", "0501010101")
        v, vid = self.register("driver", "Victor Voiture", "0502020202", plate="AA 111 BB", vehicle_type="voiture")
        t, tid = self.register("driver", "Thomas Tricycle", "0503030303", plate="TR 222 CC", vehicle_type="tricycle")
        body = {"role": "driver", "name": "Faux Type", "phone": "0504040404", "password": "secret1",
                "driver": {"plate": "ZZ 999", "permis": "CI-1", "commune": "Cocody", "vehicle_type": "avion"}}
        self.assertEqual(self.call("post", "/api/auth/register", json=body).status_code, 400)
        meta = self.call("get", "/api/meta").get_json()
        self.assertEqual(set(meta["vehicle_types"]), {"moto", "voiture", "tricycle"})
        # le profil du chauffeur et la liste admin indiquent le type
        self.assertEqual(self.call("get", "/api/me", v).get_json()["driver"]["vehicle_type"], "voiture")
        types = {d["name"]: d["vehicle_type"] for d in self.call("get", "/api/admin/drivers?approval=all", admin).get_json()["drivers"]}
        self.assertEqual(types, {"Moussa Moto": "moto", "Victor Voiture": "voiture", "Thomas Tricycle": "tricycle"})
        # tous disponibles près du client : la carte reçoit le bon type pour chacun
        for tok, uid in ((m, mid), (v, vid), (t, tid)):
            self.call("post", f"/api/admin/drivers/{uid}/approval", admin, json={"decision": "approved"})
            self.call("post", "/api/driver/location", tok, json={"lat": NEAR[0], "lng": NEAR[1]})
            self.call("put", "/api/driver/status", tok, json={"availability": "available"})
        near = self.call("get", f"/api/drivers/nearby?lat={ABIDJAN[0]}&lng={ABIDJAN[1]}", admin).get_json()["drivers"]
        self.assertEqual({d["name"]: d["vehicle_type"] for d in near},
                         {"Moussa Moto": "moto", "Victor Voiture": "voiture", "Thomas Tricycle": "tricycle"})
        # après acceptation, le client voit le type du chauffeur qui arrive
        rid = self.call("post", "/api/requests", client, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1]}).get_json()["request"]["id"]
        self.assertEqual(self.call("post", f"/api/requests/{rid}/accept", t).status_code, 200)
        cur = self.call("get", "/api/requests/current", client).get_json()["request"]
        self.assertEqual(cur["driver"]["vehicle_type"], "tricycle")

    def test_admin_deletes_user_permanently(self):
        admin = self.login("0700000000", "admin-secret")
        client, cid = self.register("client", "Awa Koné", "0707070707")
        drv, did = self.register("driver", "Moussa Moto", "0501010101")
        self.call("post", f"/api/admin/drivers/{did}/approval", admin, json={"decision": "approved"})
        self.call("post", "/api/driver/location", drv, json={"lat": NEAR[0], "lng": NEAR[1]})
        self.call("put", "/api/driver/status", drv, json={"availability": "available"})
        rid = self.call("post", "/api/requests", client, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1]}).get_json()["request"]["id"]
        self.call("post", f"/api/requests/{rid}/accept", drv)
        self.call("post", "/api/reports", client, json={"target_user_id": did, "reason": "prix", "request_id": rid})
        # confirmation obligatoire, admin intouchable, inconnu -> 404
        self.assertEqual(self.call("delete", f"/api/admin/users/{did}", admin, json={}).status_code, 400)
        self.assertEqual(self.call("delete", f"/api/admin/users/{did}", admin, json={"confirm": "oui"}).status_code, 400)
        self.assertEqual(self.call("delete", "/api/admin/users/999999", admin, json={"confirm": "SUPPRIMER"}).status_code, 404)
        self.assertEqual(self.call("delete", f"/api/admin/users/{did}", client, json={"confirm": "SUPPRIMER"}).status_code, 403)
        # suppression du chauffeur en pleine course
        self.assertEqual(self.call("delete", f"/api/admin/users/{did}", admin, json={"confirm": "SUPPRIMER"}).status_code, 200)
        self.assertEqual(self.call("get", "/api/me", drv).status_code, 401)
        self.assertEqual(self.call("post", "/api/auth/login", json={"phone": "0501010101", "password": "secret1"}).status_code, 401)
        users = self.call("get", "/api/admin/users", admin).get_json()["users"]
        self.assertEqual([u["name"] for u in users], ["Awa Koné"])
        # le client peut se réinscrire avec le numéro du chauffeur supprimé
        self.register("driver", "Nouveau", "0501010101")
        # suppression du client : ses demandes disparaissent
        self.assertEqual(self.call("delete", f"/api/admin/users/{cid}", admin, json={"confirm": "SUPPRIMER"}).status_code, 200)
        self.assertEqual(self.call("get", "/api/me", client).status_code, 401)

    # ---- notifications push ----
    def test_webpush_encryption_matches_rfc8291(self):
        """Vecteur de test officiel de la RFC 8291 (section 5)."""
        from cryptography.hazmat.primitives.asymmetric import ec
        b, d = sunrise.b64u_dec, sunrise.b64u
        as_priv = ec.derive_private_key(int.from_bytes(b("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
        out = sunrise.webpush_encrypt(b"When I grow up, I want to be a watermelon",
                                      "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                                      "BTBZMqHH6r4Tts7J_aSIgg", as_priv=as_priv, salt=b("DGv6ra1nlYgDCS1FRnbzlw"))
        self.assertEqual(d(out), "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")

    def _sub(self, host="fcm.googleapis.com", n="1"):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        pub = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return {"endpoint": f"https://{host}/send/{n}", "keys": {"p256dh": sunrise.b64u(pub), "auth": sunrise.b64u(os.urandom(16))}}

    def test_staff_notifications(self):
        sent = []
        original = sunrise._deliver
        sunrise._deliver = lambda subs, payload: sent.append(([x["endpoint"] for x in subs], __import__("json").loads(payload)))
        try:
            admin = self.login("0700000000", "admin-secret")
            self.assertEqual(self.call("post", "/api/admin/validators", admin, json={"name": "Valérie", "phone": "0102030405", "password": "temp123"}).status_code, 201)
            val = self.call("post", "/api/auth/login", json={"phone": "0102030405", "password": "temp123"}).get_json()["token"]
            self.assertEqual(self.call("post", "/api/auth/change-password", val, json={"current": "temp123", "new": "mon-secret"}).status_code, 200)
            self.assertEqual(self.call("post", "/api/push/subscribe", admin, json=self._sub("fcm.googleapis.com", "adm")).status_code, 200)
            self.assertEqual(self.call("post", "/api/push/subscribe", val, json=self._sub("web.push.apple.com", "val")).status_code, 200)   # le validateur peut s'abonner
            # un chauffeur s'inscrit : admin ET validateur sont prévenus, en urgent
            self.register("driver", "Moussa Moto", "0501010101")
            drv = [x for x in sent if "valider" in x[1]["title"]]
            self.assertEqual(len(drv), 1); self.assertEqual(len(drv[0][0]), 2); self.assertTrue(drv[0][1]["urgent"])
            # un client écrit : seul l'admin est prévenu
            client, cid = self.register("client", "Awa Koné", "0707070707")
            sent.clear()
            self.assertEqual(self.call("post", "/api/inbox/messages", client, json={"body": "Bonjour, j'ai un souci"}).status_code, 201)
            self.assertEqual(len(sent), 1); self.assertIn("Awa", sent[0][1]["title"]); self.assertEqual(len(sent[0][0]), 1)
            self.assertIn("fcm.googleapis.com", sent[0][0][0]); self.assertTrue(sent[0][1]["urgent"])
        finally:
            sunrise._deliver = original

    def test_push_flow(self):
        sent = []
        original = sunrise._deliver
        sunrise._deliver = lambda subs, payload: sent.append(([x["endpoint"] for x in subs], __import__("json").loads(payload)))
        try:
            admin = self.login("0700000000", "admin-secret")
            client, cid = self.register("client", "Awa Koné", "0707070707")
            near, nid = self.register("driver", "Moussa Moto", "0501010101")
            car, vid = self.register("driver", "Victor Voiture", "0502020202", plate="AA 111 BB", vehicle_type="voiture")
            far, fid = self.register("driver", "Loin", "0503030303")
            for tok, uid, pos in ((near, nid, NEAR), (car, vid, NEAR), (far, fid, FAR)):
                self.call("post", f"/api/admin/drivers/{uid}/approval", admin, json={"decision": "approved"})
                self.call("post", "/api/driver/location", tok, json={"lat": pos[0], "lng": pos[1]})
                self.call("put", "/api/driver/status", tok, json={"availability": "available"})
            # abonnement : adresse non autorisée / clé invalide refusées
            bad = self._sub("127.0.0.1"); self.assertEqual(self.call("post", "/api/push/subscribe", near, json=bad).status_code, 400)
            bad = self._sub(); bad["keys"]["auth"] = "abc"; self.assertEqual(self.call("post", "/api/push/subscribe", near, json=bad).status_code, 400)
            self.assertEqual(self.call("post", "/api/push/subscribe", None, json=self._sub()).status_code, 401)
            self.assertTrue(self.call("get", "/api/push/key", near).get_json()["key"])
            for tok, host in ((near, "fcm.googleapis.com"), (car, "web.push.apple.com"), (far, "updates.push.services.mozilla.com"), (client, "fcm.googleapis.com")):
                self.assertEqual(self.call("post", "/api/push/subscribe", tok, json=self._sub(host, host[:3] + str(len(sent)) + tok[:6])).status_code, 200)
            # demande de moto : seul le chauffeur de moto proche est prévenu (pas la voiture, pas le lointain)
            rid = self.call("post", "/api/requests", client, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1], "vehicle_type": "moto", "pickup_text": "Marché"}).get_json()["request"]["id"]
            self.assertEqual(len(sent), 1)
            self.assertIn("fcm.googleapis.com", sent[0][0][0]); self.assertIn("Nouvelle demande", sent[0][1]["title"])
            # acceptation : le client est prévenu
            self.assertEqual(self.call("post", f"/api/requests/{rid}/accept", near).status_code, 200)
            self.assertEqual(len(sent), 2); self.assertIn("acceptée", sent[1][1]["title"])
            # position du chauffeur périmée : un rappel (une seule fois dans la fenêtre)
            with sunrise.app.app_context():
                db = sunrise.get_db()
                db.execute("UPDATE drivers SET location_at=? WHERE user_id=?", ("2000-01-01 00:00:00", nid)); db.commit()
            self.call("get", "/api/requests/current", client)
            self.call("get", "/api/requests/current", client)
            self.assertEqual(len(sent), 3); self.assertIn("vous attend", sent[2][1]["title"])
            # le client annule : le chauffeur est prévenu
            self.call("post", f"/api/requests/{rid}/cancel", client)
            self.assertEqual(len(sent), 4); self.assertIn("annulée", sent[3][1]["title"])
            # désabonnement
            ep = self._sub("fcm.googleapis.com", "x")
            self.call("post", "/api/push/subscribe", near, json=ep)
            self.call("post", "/api/push/unsubscribe", near, json={"endpoint": ep["endpoint"]})
            with sunrise.app.app_context():
                n = sunrise.get_db().execute("SELECT COUNT(*) FROM push_subscriptions WHERE endpoint=?", (ep["endpoint"],)).fetchone()[0]
            self.assertEqual(n, 0)
        finally:
            sunrise._deliver = original

    def test_rating_and_ghost_drivers(self):
        admin = self.login("0700000000", "admin-secret")
        client, cid = self.register("client", "Awa Koné", "0707070707")
        drv, did = self.register("driver", "Moussa Moto", "0501010101")
        self.call("post", f"/api/admin/drivers/{did}/approval", admin, json={"decision": "approved"})
        self.call("post", "/api/driver/location", drv, json={"lat": NEAR[0], "lng": NEAR[1]})
        self.call("put", "/api/driver/status", drv, json={"availability": "available"})
        rid = self.call("post", "/api/requests", client, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1]}).get_json()["request"]["id"]
        # pas de note avant la fin de la course
        self.assertEqual(self.call("post", f"/api/requests/{rid}/rate", client, json={"stars": 5}).status_code, 409)
        self.call("post", f"/api/requests/{rid}/accept", drv)
        self.call("post", f"/api/requests/{rid}/complete", drv)
        for bad in (0, 6, "5", True, None):
            self.assertEqual(self.call("post", f"/api/requests/{rid}/rate", client, json={"stars": bad}).status_code, 400)
        self.assertEqual(self.call("post", f"/api/requests/{rid}/rate", drv, json={"stars": 5}).status_code, 403)
        self.assertEqual(self.call("post", f"/api/requests/{rid}/rate", client, json={"stars": 4, "comment": "Rapide"}).status_code, 200)
        self.assertEqual(self.call("post", f"/api/requests/{rid}/rate", client, json={"stars": 5}).status_code, 409)
        cur = self.call("get", "/api/requests/current", client).get_json()["request"]
        self.assertEqual((cur["rating"], cur["driver"]["rating_avg"], cur["driver"]["rating_count"]), (4, 4.0, 1))
        prof = self.call("get", "/api/me", drv).get_json()["driver"]
        self.assertEqual((prof["rating_avg"], prof["rides_done"]), (4.0, 1))
        stats = self.call("get", "/api/admin/stats", admin).get_json()
        self.assertEqual((stats["avg_rating"], stats["rides_week"]), (4.0, 1))
        # chauffeur fantôme : « Disponible » sans position depuis plus de 12 h -> indisponible
        with sunrise.app.app_context():
            db = sunrise.get_db()
            db.execute("UPDATE drivers SET location_at=? WHERE user_id=?", ("2000-01-01 00:00:00", did)); db.commit()
        sunrise._last_sweep[0] = 0
        self.call("get", "/api/admin/stats", admin)
        self.assertEqual(self.call("get", "/api/me", drv).get_json()["driver"]["availability"], "unavailable")

    def test_trash_and_safety_copies(self):
        admin = self.login("0700000000", "admin-secret")
        client, cid = self.register("client", "Awa Koné", "0707070707")
        drv, did = self.register("driver", "Moussa Moto", "0501010101")
        self.call("post", f"/api/admin/drivers/{did}/approval", admin, json={"decision": "approved"})
        self.call("post", "/api/driver/location", drv, json={"lat": NEAR[0], "lng": NEAR[1]})
        self.call("put", "/api/driver/status", drv, json={"availability": "available"})
        rid = self.call("post", "/api/requests", client, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1]}).get_json()["request"]["id"]
        self.call("post", f"/api/requests/{rid}/accept", drv)
        self.call("post", f"/api/requests/{rid}/complete", drv)
        self.call("post", "/api/inbox/messages", client, json={"body": "Bonjour"})
        # suppression du client : copie de sécurité, puis restauration du compte
        self.assertEqual(self.call("delete", f"/api/admin/users/{cid}", admin, json={"confirm": "SUPPRIMER"}).status_code, 200)
        copies = self.call("get", "/api/admin/safety-copies", admin).get_json()["copies"]
        self.assertEqual([c["kind"] for c in copies], ["user_delete"])
        self.assertIn("Awa Koné", copies[0]["label"])
        self.assertEqual(self.call("get", "/api/requests/history", client).status_code, 401)
        self.assertEqual(self.call("post", f"/api/admin/safety-copies/{copies[0]['id']}/restore-user", drv).status_code, 403)
        self.assertEqual(self.call("post", f"/api/admin/safety-copies/{copies[0]['id']}/restore-user", admin).status_code, 200)
        back = self.login("0707070707", "secret1")            # même mot de passe
        hist = self.call("get", "/api/requests/history", back).get_json()["requests"]
        self.assertEqual([h["status"] for h in hist], ["completed"])
        # déjà restauré : la copie disparaît de la corbeille
        self.assertEqual(self.call("get", "/api/admin/safety-copies", admin).get_json()["copies"], [])
        # numéro déjà repris par quelqu'un d'autre : refus clair
        self.call("delete", f"/api/admin/users/{cid}", admin, json={"confirm": "SUPPRIMER"})
        self.register("client", "Autre", "0707070707")
        c2 = self.call("get", "/api/admin/safety-copies", admin).get_json()["copies"][0]["id"]
        self.assertEqual(self.call("post", f"/api/admin/safety-copies/{c2}/restore-user", admin).status_code, 409)
        # une restauration depuis fichier garde d'abord l'état actuel côté serveur
        backup = self.call("get", "/api/admin/backup", admin).get_json()
        r = self.call("post", "/api/admin/restore", admin, json={"backup": backup, "dry_run": False, "confirmation": "RESTAURER"})
        self.assertEqual(r.status_code, 200)
        admin = self.login("0700000000", "admin-secret")
        kinds = [c["kind"] for c in self.call("get", "/api/admin/safety-copies", admin).get_json()["copies"]]
        self.assertIn("restore", kinds)
        rc = [c for c in self.call("get", "/api/admin/safety-copies", admin).get_json()["copies"] if c["kind"] == "restore"][0]
        dl = self.call("get", f"/api/admin/safety-copies/{rc['id']}/download", admin).get_json()
        self.assertEqual(dl["format"], "sunrise-backup-1")

    def test_push_reminders_stop_when_request_is_taken(self):
        import time as _t
        sent = []
        original, delays = sunrise._deliver, sunrise.PUSH_REMINDER_DELAYS
        sunrise._deliver = lambda subs, payload: sent.append(__import__("json").loads(payload))
        sunrise.PUSH_REMINDER_DELAYS = (0.15, 0.15)
        try:
            admin = self.login("0700000000", "admin-secret")
            client, cid = self.register("client", "Awa Koné", "0707070707")
            drv, did = self.register("driver", "Moussa Moto", "0501010101")
            self.call("post", f"/api/admin/drivers/{did}/approval", admin, json={"decision": "approved"})
            self.call("post", "/api/driver/location", drv, json={"lat": NEAR[0], "lng": NEAR[1]})
            self.call("put", "/api/driver/status", drv, json={"availability": "available"})
            self.call("post", "/api/push/subscribe", drv, json=self._sub())
            rid = self.call("post", "/api/requests", client, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1]}).get_json()["request"]["id"]
            self.assertTrue(sent[0]["urgent"])
            _t.sleep(0.6)
            self.assertEqual(len(sent), 3)                       # annonce + 2 rappels tant que la demande est ouverte
            self.assertIn("toujours en attente", sent[1]["title"])
            self.call("post", f"/api/requests/{rid}/cancel", client)
            rid2 = self.call("post", "/api/requests", client, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1]}).get_json()["request"]["id"]
            self.assertEqual(self.call("post", f"/api/requests/{rid2}/accept", drv).status_code, 200)
            n = len(sent)
            _t.sleep(0.6)
            self.assertEqual(len(sent), n)                       # prise : plus de rappel (seule l'annonce initiale est partie)
        finally:
            sunrise._deliver, sunrise.PUSH_REMINDER_DELAYS = original, delays

    def test_validator_has_a_narrow_access(self):
        admin = self.login("0700000000", "admin-secret")
        client, cid = self.register("client", "Awa Koné", "0707070707")
        drv, did = self.register("driver", "Moussa Moto", "0501010101")
        # seul l'admin complet crée un validateur
        self.assertEqual(self.call("post", "/api/admin/validators", client, json={"name": "Val", "phone": "0102030405", "password": "temp123"}).status_code, 403)
        self.assertEqual(self.call("post", "/api/admin/validators", admin, json={"name": "V", "phone": "0102030405", "password": "temp123"}).status_code, 400)
        self.assertEqual(self.call("post", "/api/admin/validators", admin, json={"name": "Valérie", "phone": "0102030405", "password": "abc"}).status_code, 400)
        self.assertEqual(self.call("post", "/api/admin/validators", admin, json={"name": "Valérie", "phone": "0102030405", "password": "temp123"}).status_code, 201)
        self.assertEqual(self.call("post", "/api/admin/validators", admin, json={"name": "Autre", "phone": "0102030405", "password": "temp123"}).status_code, 409)
        # première connexion : doit choisir son mot de passe
        res = self.call("post", "/api/auth/login", json={"phone": "0102030405", "password": "temp123"}).get_json()
        val = res["token"]; self.assertEqual((res["user"]["role"], res["user"]["scope"], res["user"]["must_change_password"]), ("admin", "validator", True))
        self.assertEqual(self.call("get", "/api/admin/drivers", val).status_code, 403)
        self.assertEqual(self.call("post", "/api/auth/change-password", val, json={"current": "temp123", "new": "mon-secret"}).status_code, 200)
        val = self.login("0102030405", "mon-secret")
        # ce qu'il peut faire : valider les chauffeurs, voir les inscrits
        self.assertEqual(self.call("get", "/api/admin/drivers?approval=all", val).status_code, 200)
        self.assertEqual(self.call("post", f"/api/admin/drivers/{did}/approval", val, json={"decision": "approved"}).status_code, 200)
        users = self.call("get", "/api/admin/users", val).get_json()["users"]
        self.assertEqual(sorted(u["name"] for u in users), ["Awa Koné", "Moussa Moto"])
        # tout le reste lui est interdit
        for method, url, kw in (
            ("get", "/api/admin/stats", {}), ("get", "/api/admin/backup", {}), ("post", "/api/admin/restore", {"json": {"backup": {}}}),
            ("delete", f"/api/admin/users/{cid}", {"json": {"confirm": "SUPPRIMER"}}), ("post", f"/api/admin/users/{cid}/block", {"json": {"blocked": True}}),
            ("get", "/api/admin/reports", {}), ("get", "/api/admin/safety-copies", {}), ("get", "/api/admin/password-resets", {}),
            ("get", "/api/admin/validators", {}), ("post", "/api/admin/validators", {"json": {"name": "Pirate", "phone": "0909090909", "password": "abcdef"}}),
            ("get", "/api/inbox", {}), ("get", "/api/admin/announcements", {}),
        ):
            self.assertEqual(self.call(method, url, val, **kw).status_code, 403, url)
        # l'admin le voit, réinitialise son mot de passe, puis le supprime
        vs = self.call("get", "/api/admin/validators", admin).get_json()["validators"]
        self.assertEqual([v["name"] for v in vs], ["Valérie"])
        self.assertNotIn("Valérie", [u["name"] for u in self.call("get", "/api/admin/users", admin).get_json()["users"]])
        self.assertEqual(self.call("post", f"/api/admin/validators/{vs[0]['id']}/password", admin, json={"password": "nouveau1"}).status_code, 200)
        self.assertEqual(self.call("get", "/api/admin/users", val).status_code, 401)   # déconnecté
        self.assertEqual(self.call("delete", f"/api/admin/validators/{vs[0]['id']}", admin).status_code, 200)
        self.assertEqual(self.call("post", "/api/auth/login", json={"phone": "0102030405", "password": "nouveau1"}).status_code, 401)
        # une sauvegarde ne compte pas un validateur comme administrateur
        self.call("post", "/api/admin/validators", admin, json={"name": "Valérie", "phone": "0102030405", "password": "temp123"})
        backup = self.call("get", "/api/admin/backup", admin).get_json()
        for u in backup["tables"]["users"]:
            if u["role"] == "admin" and not u.get("staff_scope"): u["is_blocked"] = 1
        self.assertEqual(self.call("post", "/api/admin/restore", admin, json={"backup": backup}).status_code, 400)

    def test_privacy_between_client_and_driver(self):
        admin = self.login("0700000000", "admin-secret")
        client, cid = self.register("client", "Awa Koné", "0707070707")
        drv, did = self.register("driver", "Moussa Moto", "0501010101")
        self.call("post", f"/api/admin/drivers/{did}/approval", admin, json={"decision": "approved"})
        self.call("post", "/api/driver/location", drv, json={"lat": NEAR[0], "lng": NEAR[1]})
        self.call("put", "/api/driver/status", drv, json={"availability": "available"})
        # 1. le client ne peut plus consulter la liste des chauffeurs
        self.assertEqual(self.call("get", f"/api/drivers/nearby?lat={ABIDJAN[0]}&lng={ABIDJAN[1]}", client).status_code, 403)
        rid = self.call("post", "/api/requests", client, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1]}).get_json()["request"]["id"]
        mine = self.call("get", "/api/requests/current", client).get_json()["request"]
        self.assertEqual(mine["status"], "open"); self.assertIsNone(mine["driver"])
        # 2. le chauffeur ne voit ni le numéro ni le nom de famille avant d'accepter
        dash = self.call("get", "/api/driver/dashboard", drv).get_json()
        self.assertEqual(len(dash["requests"]), 1)
        item = dash["requests"][0]
        self.assertEqual(item["client_name"], "Awa")
        self.assertNotIn("client_phone", item)
        self.assertNotIn("0707070707", __import__("json").dumps(dash))
        # après acceptation : nom complet, numéro, contact
        self.call("post", f"/api/requests/{rid}/accept", drv)
        job = self.call("get", "/api/driver/dashboard", drv).get_json()["job"]
        self.assertEqual(job["client_name"], "Awa Koné")
        self.assertNotIn("0707070707", __import__("json").dumps(job))          # aucun numéro côté chauffeur
        cur = self.call("get", "/api/requests/current", client).get_json()["request"]
        self.assertNotIn("0501010101", __import__("json").dumps(cur))          # aucun numéro côté client
        self.assertNotIn("whatsapp", cur["driver"]); self.assertNotIn("phone", cur["driver"])

    def test_sunrise_calls(self):
        admin = self.login("0700000000", "admin-secret")
        client, cid = self.register("client", "Awa Koné", "0707070707")
        drv, did = self.register("driver", "Moussa Moto", "0501010101")
        other, _ = self.register("client", "Autre", "0808080808")
        self.call("post", f"/api/admin/drivers/{did}/approval", admin, json={"decision": "approved"})
        self.call("post", "/api/driver/location", drv, json={"lat": NEAR[0], "lng": NEAR[1]})
        self.call("put", "/api/driver/status", drv, json={"availability": "available"})
        rid = self.call("post", "/api/requests", client, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1]}).get_json()["request"]["id"]
        # pas d'appel avant l'acceptation
        self.assertEqual(self.call("post", "/api/calls", client, json={"request_id": rid, "offer": "SDP"}).status_code, 403)
        self.call("post", f"/api/requests/{rid}/accept", drv)
        # un tiers ne peut pas appeler
        self.assertEqual(self.call("post", "/api/calls", other, json={"request_id": rid, "offer": "SDP"}).status_code, 403)
        self.assertEqual(self.call("get", "/api/calls/ice", client).status_code, 200)
        # le chauffeur appelle : le client voit « un appel » sans aucun numéro
        c = self.call("post", "/api/calls", drv, json={"request_id": rid, "offer": "OFFER-SDP"})
        self.assertEqual(c.status_code, 200); callid = c.get_json()["id"]
        inc = self.call("get", "/api/calls/incoming", client).get_json()["call"]
        self.assertEqual((inc["id"], inc["offer"]), (callid, "OFFER-SDP"))
        self.assertNotIn("0501010101", __import__("json").dumps(inc))
        self.assertIsNone(self.call("get", "/api/calls/incoming", drv).get_json()["call"])
        self.assertIsNone(self.call("get", "/api/calls/incoming", other).get_json()["call"])
        self.assertEqual(self.call("get", f"/api/calls/{callid}", other).status_code, 404)
        # l'appelant ne peut pas répondre à son propre appel
        self.assertEqual(self.call("post", f"/api/calls/{callid}/accept", drv, json={"answer": "X"}).status_code, 404)
        self.assertEqual(self.call("post", f"/api/calls/{callid}/signal", drv, json={"kind": "ice", "payload": "cand1"}).status_code, 200)
        self.assertEqual(self.call("post", f"/api/calls/{callid}/accept", client, json={"answer": "ANSWER-SDP"}).status_code, 200)
        self.assertEqual(self.call("post", f"/api/calls/{callid}/accept", client, json={"answer": "ANSWER-SDP"}).status_code, 409)
        got = self.call("get", f"/api/calls/{callid}?after=0", client).get_json()
        self.assertEqual(got["status"], "active")
        self.assertEqual([x["kind"] for x in got["signals"]], ["offer", "ice"])
        got = self.call("get", f"/api/calls/{callid}?after=0", drv).get_json()
        self.assertEqual([x["kind"] for x in got["signals"]], ["answer"])
        self.assertEqual(self.call("post", f"/api/calls/{callid}/end", client).status_code, 200)
        self.assertEqual(self.call("get", f"/api/calls/{callid}", drv).get_json()["status"], "ended")
        # refus, puis appel sans réponse (expire)
        c2 = self.call("post", "/api/calls", client, json={"request_id": rid, "offer": "O2"}).get_json()["id"]
        self.assertEqual(self.call("post", f"/api/calls/{c2}/end", drv).status_code, 200)
        self.assertEqual(self.call("get", f"/api/calls/{c2}", client).get_json()["status"], "declined")
        c3 = self.call("post", "/api/calls", client, json={"request_id": rid, "offer": "O3"}).get_json()["id"]
        with sunrise.app.app_context():
            db = sunrise.get_db()
            db.execute("UPDATE calls SET created_at='2000-01-01 00:00:00' WHERE id=?", (c3,)); db.commit()
        self.assertIsNone(self.call("get", "/api/calls/incoming", drv).get_json()["call"])
        self.assertEqual(self.call("get", f"/api/calls/{c3}", client).get_json()["status"], "missed")
        # course terminée : plus d'appel possible ; et suppression du compte sans erreur
        self.call("post", f"/api/requests/{rid}/complete", drv)
        self.assertEqual(self.call("post", "/api/calls", client, json={"request_id": rid, "offer": "O4"}).status_code, 403)
        self.assertEqual(self.call("delete", f"/api/admin/users/{cid}", admin, json={"confirm": "SUPPRIMER"}).status_code, 200)

    def test_admin_can_call_connected_users(self):
        admin = self.login("0700000000", "admin-secret")
        self.assertEqual(self.call("post", "/api/admin/validators", admin, json={"name": "Valérie", "phone": "0102030405", "password": "temp123"}).status_code, 201)
        val = self.call("post", "/api/auth/login", json={"phone": "0102030405", "password": "temp123"}).get_json()["token"]
        self.call("post", "/api/auth/change-password", val, json={"current": "temp123", "new": "mon-secret"})
        client, cid = self.register("client", "Awa Koné", "0707070707")
        drv, did = self.register("driver", "Moussa Moto", "0501010101")
        # personne n'est « connecté » tant que l'app n'a pas été vue récemment
        with sunrise.app.app_context():
            db = sunrise.get_db()
            db.execute("UPDATE users SET last_seen_at='2000-01-01 00:00:00' WHERE id IN (?,?)", (cid, did)); db.commit()
        r = self.call("post", "/api/calls", admin, json={"user_id": cid, "offer": "SDP"})
        self.assertEqual(r.status_code, 409)
        self.call("get", "/api/me", client)      # l'application du client est ouverte : il apparaît en ligne
        self.assertEqual(self.call("post", "/api/calls", val, json={"user_id": cid, "offer": "SDP"}).status_code, 403)   # un validateur ne peut pas
        self.assertEqual(self.call("post", "/api/calls", client, json={"user_id": did, "offer": "SDP"}).status_code, 400)  # un client non plus
        self.assertEqual(self.call("post", "/api/calls", admin, json={"user_id": 99999, "offer": "SDP"}).status_code, 404)
        self.assertEqual(self.call("post", "/api/calls", admin, json={"user_id": cid}).status_code, 400)
        r = self.call("post", "/api/calls", admin, json={"user_id": cid, "offer": "OFFER-ADM"})
        self.assertEqual(r.status_code, 200); callid = r.get_json()["id"]
        inc = self.call("get", "/api/calls/incoming", client).get_json()["call"]
        self.assertEqual((inc["id"], inc["offer"]), (callid, "OFFER-ADM"))
        self.assertNotIn("0700000000", __import__("json").dumps(inc))
        # déjà en communication : un 2e appel vers le même client est refusé
        self.assertEqual(self.call("post", "/api/calls", admin, json={"user_id": cid, "offer": "O2"}).status_code, 409)
        self.assertEqual(self.call("post", f"/api/calls/{callid}/accept", client, json={"answer": "ANS"}).status_code, 200)
        got = self.call("get", f"/api/calls/{callid}?after=0", admin).get_json()
        self.assertEqual((got["status"], [x["kind"] for x in got["signals"]]), ("active", ["answer"]))
        self.assertEqual(self.call("post", f"/api/calls/{callid}/end", admin).status_code, 200)
        self.assertEqual(self.call("get", f"/api/calls/{callid}", client).get_json()["status"], "ended")

    def test_ride_chaining(self):
        admin = self.login("0700000000", "admin-secret")
        drv, did = self.register("driver", "Moussa Moto", "0501010101")
        self.call("post", f"/api/admin/drivers/{did}/approval", admin, json={"decision": "approved"})
        self.call("post", "/api/driver/location", drv, json={"lat": NEAR[0], "lng": NEAR[1]})
        self.call("put", "/api/driver/status", drv, json={"availability": "available"})
        c1, _ = self.register("client", "Premier Client", "0707070701")
        c2, _ = self.register("client", "Deuxieme Client", "0707070702")
        c3, _ = self.register("client", "Loin Client", "0707070703")
        dest = (5.3600, -3.9500)
        r1 = self.call("post", "/api/requests", c1, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1], "dest_lat": dest[0], "dest_lng": dest[1]}).get_json()["request"]["id"]
        self.assertEqual(self.call("post", f"/api/requests/{r1}/accept", drv).status_code, 200)
        # demande proche de la destination (≈1 km) et demande loin (≈ 5 km)
        r2 = self.call("post", "/api/requests", c2, json={"lat": dest[0] + 0.005, "lng": dest[1], "dest_lat": 5.37, "dest_lng": -3.93}).get_json()["request"]["id"]
        r3 = self.call("post", "/api/requests", c3, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1]}).get_json()["request"]["id"]
        d = self.call("get", "/api/driver/dashboard", drv).get_json()
        self.assertEqual(d["job"]["id"], r1)
        self.assertTrue(d["chain"]["can"])
        self.assertEqual([x["id"] for x in d["requests"]], [r2])      # r3 est trop loin de la destination
        self.assertEqual(self.call("post", f"/api/requests/{r3}/accept", drv).status_code, 409)
        self.assertEqual(self.call("post", f"/api/requests/{r2}/accept", drv).status_code, 200)
        d = self.call("get", "/api/driver/dashboard", drv).get_json()
        self.assertEqual((d["job"]["id"], [q["id"] for q in d["queue"]]), (r1, [r2]))
        self.assertEqual(self.call("get", "/api/requests/current", c2).get_json()["request"]["queued_behind"], 1)
        self.assertEqual(self.call("get", "/api/requests/current", c1).get_json()["request"]["queued_behind"], 0)
        # terminer la 1re course : la 2e devient la course en cours, le chauffeur reste occupé
        self.assertEqual(self.call("post", f"/api/requests/{r1}/complete", c1).status_code, 200)
        d = self.call("get", "/api/driver/dashboard", drv).get_json()
        self.assertEqual((d["job"]["id"], d["queue"], d["driver"]["availability"]), (r2, [], "busy"))
        # terminer la dernière : il redevient disponible
        self.assertEqual(self.call("post", f"/api/requests/{r2}/complete", c2).status_code, 200)
        self.assertEqual(self.call("get", "/api/driver/dashboard", drv).get_json()["driver"]["availability"], "available")

    def test_driver_history(self):
        admin = self.login("0700000000", "admin-secret")
        drv, did = self.register("driver", "Moussa Moto", "0501010101")
        self.call("post", f"/api/admin/drivers/{did}/approval", admin, json={"decision": "approved"})
        self.call("post", "/api/driver/location", drv, json={"lat": NEAR[0], "lng": NEAR[1]})
        self.call("put", "/api/driver/status", drv, json={"availability": "available"})
        c, _ = self.register("client", "Awa Koné", "0707070707")
        self.assertEqual(self.call("get", "/api/driver/history", c).status_code, 403)
        self.assertEqual(self.call("get", "/api/driver/history", drv).get_json()["rides"], [])
        rid = self.call("post", "/api/requests", c, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1], "destination_text": "Marché"}).get_json()["request"]["id"]
        self.call("post", f"/api/requests/{rid}/accept", drv)
        self.call("post", f"/api/requests/{rid}/complete", c)
        h = self.call("get", "/api/driver/history", drv).get_json()
        self.assertEqual([(x["id"], x["status"], x["destination_text"]) for x in h["rides"]], [(rid, "completed", "Marché")])
        self.assertEqual((h["stats"]["completed"], h["stats"]["today"]), (1, 1))
        self.assertNotIn("0707070707", str(h))

    def test_connection_history(self):
        admin = self.login("0700000000", "admin-secret")
        _, uid = self.register("client", "Awa Koné", "0707070777")
        h = self.call("get", f"/api/admin/users/{uid}/history", admin).get_json()
        self.assertEqual(h["connexions"], [])   # l'inscription seule n'est pas une « connexion » comptée
        self.assertEqual(self.call("post", "/api/auth/login", json={"phone": "0707070777", "password": "secret1"}).status_code, 200)
        self.call("post", "/api/auth/login", json={"phone": "0707070777", "password": "secret1"})
        h = self.call("get", f"/api/admin/users/{uid}/history", admin).get_json()
        self.assertEqual(len(h["connexions"]), 2)
        self.assertGreaterEqual(h["connexions"][0], h["connexions"][1])   # plus récent en premier
        client_tok = self.login("0707070777", "secret1")
        self.assertEqual(self.call("get", f"/api/admin/users/{uid}/history", client_tok).status_code, 403)
        self.assertEqual(self.call("get", "/api/admin/users/999999/history", admin).status_code, 404)

    def test_site_hygiene(self):
        import re as _re
        # pages légales, robots, sitemap, favicon, image d'aperçu
        for path in ("/confidentialite", "/conditions", "/bienvenue", "/bienvenue.html", "/favicon.ico", "/og-image.png", "/favicon-32.png"):
            self.assertEqual(self.c.get(path).status_code, 200, path)
        self.assertIn("Disallow: /api/", self.c.get("/robots.txt").get_data(as_text=True))
        sm = self.c.get("/sitemap.xml").get_data(as_text=True)
        self.assertIn("/confidentialite</loc>", sm)
        # page d'accueil : adresse réelle dans les balises de partage
        home = self.c.get("/", base_url="https://exemple.test").get_data(as_text=True)
        self.assertIn('og:image" content="https://exemple.test/og-image.png"', home)
        self.assertNotIn("{{BASE}}", home)
        # vraie page 404 (statut 404, pas la page d'accueil), l'API reste en JSON
        r = self.c.get("/n-existe-pas")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Cette page n'existe pas", r.get_data(as_text=True))
        self.assertEqual(self.c.get("/api/nimporte").status_code, 404)
        self.assertEqual(self.c.get("/app.py").status_code, 404)
        # https forcé derrière le proxy, HSTS en https
        r = self.c.get("/", headers={"X-Forwarded-Proto": "http", "Host": "sunrise.example"})
        self.assertEqual((r.status_code, r.headers["Location"]), (301, "https://sunrise.example/"))
        r = self.c.get("/", headers={"X-Forwarded-Proto": "https"})
        self.assertIn("max-age", r.headers.get("Strict-Transport-Security", ""))
        # compression
        r = self.c.get("/app.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.headers.get("Content-Encoding"), "gzip")
        import gzip as _gz
        self.assertIn(b"renderShell", _gz.decompress(r.data))
        self.assertIsNone(self.c.get("/app.js").headers.get("Content-Encoding"))
        # mots de passe évidents refusés
        for pw in ("123456", "0707070700", "111111", "Azerty"):
            r = self.call("post", "/api/auth/register", json={"role": "client", "name": "Test Faible", "phone": "0707070700", "password": pw})
            self.assertEqual(r.status_code, 400, pw)
        # champ piège anti-robot
        r = self.call("post", "/api/auth/register", json={"role": "client", "name": "Bot Spam", "phone": "0707070700", "password": "secret1", "website": "http://spam"})
        self.assertEqual(r.status_code, 400)
        # aucun lien interne cassé dans les pages publiques
        for page in ("/", "/bienvenue", "/confidentialite", "/conditions"):
            for href in _re.findall(r'href="(/[^"#?]*)"', self.c.get(page).get_data(as_text=True)):
                self.assertEqual(self.c.get(href).status_code, 200, f"{page} -> {href}")
        # chaque page légale annonce son titre
        self.assertIn("Politique de confidentialité", self.c.get("/confidentialite").get_data(as_text=True))

    def test_admin_map(self):
        admin = self.login("0700000000", "admin-secret")
        client, cid = self.register("client", "Awa Koné", "0707070707")
        quiet, qid = self.register("client", "Sans GPS", "0808080808")
        drv, did = self.register("driver", "Moussa Moto", "0501010101")
        self.call("post", f"/api/admin/drivers/{did}/approval", admin, json={"decision": "approved"})
        self.assertEqual(self.call("post", "/api/me/location", client, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1]}).status_code, 200)
        self.assertEqual(self.call("post", "/api/me/location", client, json={"lat": 999, "lng": 1}).status_code, 400)
        self.assertEqual(self.call("post", "/api/me/location", drv, json={"lat": 5.3, "lng": -4.0}).status_code, 403)   # réservé aux clients
        self.call("post", "/api/me/location", client, json={"lat": ABIDJAN[0] + 0.01, "lng": ABIDJAN[1]})              # mise à jour, pas de doublon
        self.call("post", "/api/driver/location", drv, json={"lat": NEAR[0], "lng": NEAR[1]})
        self.call("put", "/api/driver/status", drv, json={"availability": "available"})
        self.call("get", "/api/me", quiet)
        m = self.call("get", "/api/admin/map", admin).get_json()
        self.assertEqual([c["name"] for c in m["clients"]], ["Awa Koné"])
        self.assertAlmostEqual(m["clients"][0]["lat"], ABIDJAN[0] + 0.01)
        self.assertEqual([(d["name"], d["availability"]) for d in m["drivers"]], [("Moussa Moto", "available")])
        self.assertEqual(m["without_position"], 1)            # le client sans GPS est connecté mais pas localisable
        # personne d'autre n'a accès à la carte
        for tok in (client, drv):
            self.assertEqual(self.call("get", "/api/admin/map", tok).status_code, 403)
        # suppression d'un compte : sa position disparaît sans erreur
        self.assertEqual(self.call("delete", f"/api/admin/users/{cid}", admin, json={"confirm": "SUPPRIMER"}).status_code, 200)
        self.assertEqual(self.call("get", "/api/admin/map", admin).get_json()["clients"], [])

    def test_admin_online_cancelled_and_welcome(self):
        admin = self.login("0700000000", "admin-secret")
        # message de bienvenue : réglé par l'admin, envoyé aux NOUVEAUX inscrits seulement
        self.assertEqual(self.call("get", "/api/admin/welcome", admin).get_json(), {"client": "", "driver": ""})
        old, _ = self.register("client", "Ancien", "0606060606")
        self.assertEqual(self.call("get", "/api/inbox", old).get_json()["messages"], [])
        self.assertEqual(self.call("put", "/api/admin/welcome", admin, json={"client": "Bienvenue {prenom} sur Sunrise Space !"}).status_code, 200)
        self.assertEqual(self.call("put", "/api/admin/welcome", self.register("client", "X Y", "0808080808")[0], json={"client": "pirate"}).status_code, 403)
        client, cid = self.register("client", "Awa Koné", "0707070707")
        msgs = self.call("get", "/api/inbox", client).get_json()["messages"]
        self.assertEqual([m["body"] for m in msgs], ["Bienvenue Awa sur Sunrise Space !"])
        self.assertEqual(self.call("get", "/api/inbox/unread", client).get_json()["unread"], 1)
        drv, did = self.register("driver", "Moussa Moto", "0501010101")
        self.assertEqual(self.call("get", "/api/inbox", drv).get_json()["messages"], [])   # pas de message chauffeur réglé
        self.call("put", "/api/admin/welcome", admin, json={"client": ""})
        self.assertEqual(self.call("get", "/api/inbox", self.register("client", "Zoé", "0909090909")[0]).get_json()["messages"], [])
        # en ligne : qui a utilisé l'application ces 3 dernières minutes
        online = self.call("get", "/api/admin/online", admin).get_json()
        self.assertIn("Awa Koné", [c["name"] for c in online["clients"]])
        self.assertIn("Moussa Moto", [d["name"] for d in online["drivers"]])
        with sunrise.app.app_context():
            db = sunrise.get_db()
            db.execute("UPDATE users SET last_seen_at='2000-01-01 00:00:00' WHERE id=?", (cid,)); db.commit()
        online = self.call("get", "/api/admin/online", admin).get_json()
        self.assertNotIn("Awa Koné", [c["name"] for c in online["clients"]])
        self.assertEqual(self.call("get", "/api/admin/online", drv).status_code, 403)
        # demandes annulées
        self.call("post", "/api/driver/location", drv, json={"lat": NEAR[0], "lng": NEAR[1]})
        rid = self.call("post", "/api/requests", client, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1], "pickup_text": "Marché"}).get_json()["request"]["id"]
        self.assertEqual(self.call("post", f"/api/requests/{rid}/cancel", client).status_code, 200)
        res = self.call("get", "/api/admin/requests", admin).get_json()
        self.assertEqual([(r["id"], r["status"], r["client_name"]) for r in res["requests"]], [(rid, "cancelled", "Awa Koné")])
        self.assertEqual(res["counts"]["cancelled"], 1)
        self.assertEqual(self.call("get", "/api/admin/requests?status=open", admin).get_json()["requests"], [])
        self.assertEqual(self.call("get", "/api/admin/requests", client).status_code, 403)

    def test_destination_on_the_map(self):
        admin = self.login("0700000000", "admin-secret")
        client, cid = self.register("client", "Awa Koné", "0707070707")
        drv, did = self.register("driver", "Moussa Moto", "0501010101")
        self.call("post", f"/api/admin/drivers/{did}/approval", admin, json={"decision": "approved"})
        self.call("post", "/api/driver/location", drv, json={"lat": NEAR[0], "lng": NEAR[1]})
        self.call("put", "/api/driver/status", drv, json={"availability": "available"})
        # recherche de lieux (le service externe est simulé) : client seulement, avec mémoire et limite de débit
        calls = []
        original = sunrise._nominatim
        sunrise._nominatim = lambda q, lat, lng: calls.append(q) or [{"label": "Marché de Cocody, Abidjan", "lat": 5.3600, "lng": -3.9800}]
        try:
            self.assertEqual(self.call("get", "/api/geocode?q=ma", client).get_json()["results"], [])           # trop court
            r = self.call("get", f"/api/geocode?q=Marché%20Cocody&lat={ABIDJAN[0]}&lng={ABIDJAN[1]}", client).get_json()
            self.assertEqual(r["results"][0]["label"], "Marché de Cocody, Abidjan")
            self.call("get", f"/api/geocode?q=Marché%20Cocody&lat={ABIDJAN[0]}&lng={ABIDJAN[1]}", client)       # même question : mémoire
            self.assertEqual(len(calls), 1)
            self.assertTrue(self.call("get", "/api/geocode?q=Gare%20routière", client).get_json().get("busy"))   # trop vite : on attend
            self.assertEqual(self.call("get", "/api/geocode?q=Marché", drv).status_code, 403)
        finally:
            sunrise._nominatim = original
        # destination invalide / trop loin
        base = {"lat": ABIDJAN[0], "lng": ABIDJAN[1], "destination_text": "Marché de Cocody"}
        self.assertEqual(self.call("post", "/api/requests", client, json={**base, "dest_lat": "abc", "dest_lng": 1}).status_code, 400)
        self.assertEqual(self.call("post", "/api/requests", client, json={**base, "dest_lat": 9.5, "dest_lng": -5.5}).status_code, 400)
        # destination correcte : le chauffeur voit le trajet avant d'accepter, et la destination après
        sent = []
        orig = sunrise._deliver
        sunrise._deliver = lambda subs, payload: sent.append(__import__("json").loads(payload))
        try:
            self.call("post", "/api/push/subscribe", drv, json=self._sub())
            res = self.call("post", "/api/requests", client, json={**base, "dest_lat": 5.36, "dest_lng": -3.98})
            self.assertEqual(res.status_code, 201)
            self.assertIn("trajet ≈", sent[0]["body"])
        finally:
            sunrise._deliver = orig
        rid = res.get_json()["request"]["id"]
        item = self.call("get", "/api/driver/dashboard", drv).get_json()["requests"][0]
        self.assertEqual((item["dest_lat"], item["dest_lng"]), (5.36, -3.98))
        self.assertGreater(item["trip_km"], 1)
        self.call("post", f"/api/requests/{rid}/accept", drv)
        job = self.call("get", "/api/driver/dashboard", drv).get_json()["job"]
        self.assertEqual((job["dest_lat"], job["dest_lng"]), (5.36, -3.98))
        # demande sans point sur la carte : tout marche comme avant
        self.call("post", f"/api/requests/{rid}/complete", drv)
        self.call("put", "/api/driver/status", drv, json={"availability": "available"})
        r2 = self.call("post", "/api/requests", client, json=base).get_json()["request"]
        self.assertEqual((r2["dest_lat"], r2["dest_lng"]), (None, None))
        self.assertIsNone(self.call("get", "/api/driver/dashboard", drv).get_json()["requests"][0]["trip_km"])

    def test_request_for_a_vehicle_type(self):
        admin = self.login("0700000000", "admin-secret")
        client, _ = self.register("client", "Awa Koné", "0707070707")
        m, mid = self.register("driver", "Moussa Moto", "0501010101")
        v, vid = self.register("driver", "Victor Voiture", "0502020202", plate="AA 111 BB", vehicle_type="voiture")
        for tok, uid in ((m, mid), (v, vid)):
            self.call("post", f"/api/admin/drivers/{uid}/approval", admin, json={"decision": "approved"})
            self.call("post", "/api/driver/location", tok, json={"lat": NEAR[0], "lng": NEAR[1]})
            self.call("put", "/api/driver/status", tok, json={"availability": "available"})
        ids = lambda tok: [r["id"] for r in self.call("get", "/api/driver/dashboard", tok).get_json()["requests"]]
        # type inconnu refusé
        pos = {"lat": ABIDJAN[0], "lng": ABIDJAN[1]}
        self.assertEqual(self.call("post", "/api/requests", client, json={**pos, "vehicle_type": "avion"}).status_code, 400)
        # demande « voiture » : seul le taxi la voit et peut la prendre
        r = self.call("post", "/api/requests", client, json={**pos, "vehicle_type": "voiture"})
        self.assertEqual(r.status_code, 201)
        rid = r.get_json()["request"]["id"]
        self.assertEqual(r.get_json()["request"]["vehicle_type"], "voiture")
        self.assertEqual(ids(m), [])
        self.assertEqual(ids(v), [rid])
        refused = self.call("post", f"/api/requests/{rid}/accept", m)
        self.assertEqual(refused.status_code, 403)
        self.assertIn("Taxi", refused.get_json()["error"])
        self.assertEqual(self.call("get", "/api/requests/current", client).get_json()["request"]["status"], "open")
        self.assertEqual(self.call("post", f"/api/requests/{rid}/accept", v).status_code, 200)
        self.call("post", f"/api/requests/{rid}/complete", client)
        # sans préférence (« peu importe ») : tout le monde la voit
        r = self.call("post", "/api/requests", client, json=pos)
        rid2 = r.get_json()["request"]["id"]
        self.assertEqual(r.get_json()["request"]["vehicle_type"], "any")
        self.assertEqual(ids(m), [rid2])
        self.assertEqual(ids(v), [rid2])
        self.assertEqual(self.call("post", f"/api/requests/{rid2}/accept", m).status_code, 200)

    def test_old_database_gets_new_columns(self):
        import subprocess, sqlite3, sys
        d = tempfile.mkdtemp()
        path = os.path.join(d, "old.db")
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, name TEXT NOT NULL,
              phone TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, is_blocked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            CREATE TABLE drivers(user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, plate TEXT NOT NULL,
              moto_model TEXT, permis TEXT, commune TEXT, whatsapp TEXT, approval TEXT NOT NULL DEFAULT 'pending',
              approval_note TEXT, availability TEXT NOT NULL DEFAULT 'unavailable', lat REAL, lng REAL, location_at TEXT);
            INSERT INTO users(role,name,phone,password_hash,created_at) VALUES('driver','Ancien Chauffeur','0505050505','x','2026-01-01T00:00:00');
            INSERT INTO drivers(user_id,plate) VALUES(1,'AB 123 CD');
            CREATE TABLE ride_requests(id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER NOT NULL, driver_id INTEGER,
              lat REAL NOT NULL, lng REAL NOT NULL, pickup_text TEXT, destination_text TEXT, note TEXT,
              status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, accepted_at TEXT, closed_at TEXT);
            INSERT INTO ride_requests(client_id,lat,lng,created_at) VALUES(1,5.3,-4.0,'2026-01-01T00:00:00');""")
        con.commit(); con.close()
        code = ("import app; db = app.DB(); "
                "print(db.execute('SELECT vehicle_type FROM drivers').fetchone()[0], "
                "db.execute('SELECT must_change_password FROM users').fetchone()[0], "
                "db.execute('SELECT vehicle_type FROM ride_requests').fetchone()[0]); app.init_db()")
        env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "FAKE_PG_FILE", "PYTHONPATH")}
        env["SUNRISE_DB"] = path
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=os.path.dirname(__file__) or ".")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("moto 0 any", out.stdout)   # l'ancien chauffeur est une moto ; la 2e initialisation ne casse rien

    def test_forgot_and_change_password(self):
        admin = self.login("0700000000", "admin-secret")
        client, cid = self.register("client", "Awa Koné", "0707070707")
        other, _ = self.register("client", "Autre Client", "0708080808")

        def resets():
            return self.call("get", "/api/admin/password-resets", admin).get_json()["requests"]

        # demande d'oubli : même réponse pour un numéro inconnu, aucune fuite
        self.assertEqual(self.call("post", "/api/auth/forgot", json={"phone": "abc"}).status_code, 400)
        r1 = self.call("post", "/api/auth/forgot", json={"phone": "0799999999"})
        r2 = self.call("post", "/api/auth/forgot", json={"phone": "07 07 07 07 07"})
        self.assertEqual((r1.status_code, r1.get_json()), (r2.status_code, r2.get_json()))
        self.call("post", "/api/auth/forgot", json={"phone": "0707070707"})          # doublon : une seule demande ouverte
        self.call("post", "/api/auth/forgot", json={"phone": "0700000000"})          # un admin n'est pas concerné
        self.assertEqual([x["name"] for x in resets()], ["Awa Koné"])
        self.assertEqual(self.call("get", "/api/admin/password-resets", client).status_code, 403)
        self.assertEqual(self.call("get", "/api/admin/stats", admin).get_json()["password_resets_open"], 1)
        rid = resets()[0]["id"]
        self.assertEqual(self.call("post", f"/api/admin/password-resets/{rid}", client, json={"action": "reset"}).status_code, 403)
        # l'admin remet un mot de passe provisoire
        r = self.call("post", f"/api/admin/password-resets/{rid}", admin, json={"action": "reset"})
        self.assertEqual(r.status_code, 200)
        temp = r.get_json()["temp_password"]
        self.assertEqual(len(temp), 8)
        self.assertEqual(self.call("post", f"/api/admin/password-resets/{rid}", admin, json={"action": "reset"}).status_code, 409)
        self.assertEqual(resets(), [])
        # l'ancien mot de passe et l'ancienne session ne marchent plus
        self.assertEqual(self.call("post", "/api/auth/login", json={"phone": "0707070707", "password": "secret1"}).status_code, 401)
        self.assertEqual(self.call("get", "/api/me", client).status_code, 401)
        lg = self.call("post", "/api/auth/login", json={"phone": "0707070707", "password": temp})
        self.assertEqual(lg.status_code, 200)
        tok = lg.get_json()["token"]
        self.assertTrue(lg.get_json()["user"]["must_change_password"])
        # avec un mot de passe provisoire, seul le changement est possible
        self.assertEqual(self.call("get", "/api/inbox/unread", tok).status_code, 403)
        self.assertEqual(self.call("get", "/api/me", tok).status_code, 200)
        # changement : mauvais actuel, trop court, identique, puis valide
        bad = lambda **b: self.call("post", "/api/auth/change-password", tok, json=b).status_code
        self.assertEqual(bad(current="faux", new="nouveau1"), 403)
        self.assertEqual(bad(current=temp, new="abc"), 400)
        self.assertEqual(bad(current=temp, new=temp), 400)
        self.assertEqual(bad(current=temp, new="nouveau1"), 200)
        self.assertEqual(self.call("get", "/api/inbox/unread", tok).status_code, 200)
        self.assertFalse(self.call("get", "/api/me", tok).get_json()["user"]["must_change_password"])
        self.assertEqual(self.call("post", "/api/auth/login", json={"phone": "0707070707", "password": temp}).status_code, 401)
        self.assertEqual(self.call("post", "/api/auth/login", json={"phone": "0707070707", "password": "nouveau1"}).status_code, 200)
        # changement volontaire, depuis le compte : les autres appareils sont déconnectés
        tok2 = self.login("0707070707", "nouveau1")
        self.assertEqual(self.call("post", "/api/auth/change-password", tok2, json={"current": "nouveau1", "new": "encore-mieux"}).status_code, 200)
        self.assertEqual(self.call("get", "/api/me", tok).status_code, 401)
        self.assertEqual(self.call("get", "/api/me", tok2).status_code, 200)
        # essais répétés sur le mot de passe actuel : blocage temporaire
        for _ in range(5):
            self.call("post", "/api/auth/change-password", tok2, json={"current": "xxx", "new": "whatever1"})
        self.assertEqual(self.call("post", "/api/auth/change-password", tok2, json={"current": "encore-mieux", "new": "whatever1"}).status_code, 429)
        # « Ignorer » une demande
        self.call("post", "/api/auth/forgot", json={"phone": "0708080808"})
        rid2 = resets()[0]["id"]
        self.assertEqual(self.call("post", f"/api/admin/password-resets/{rid2}", admin, json={"action": "dismiss"}).status_code, 200)
        self.assertEqual(resets(), [])
        self.assertEqual(self.call("get", "/api/me", other).status_code, 200)   # rien changé pour ce compte
        # limite de demandes par adresse
        sunrise._forgot_hits.clear()
        codes = [self.call("post", "/api/auth/forgot", json={"phone": f"07000000{i:02d}"}).status_code for i in range(8)]
        self.assertEqual(codes[:sunrise.FORGOT_MAX_PER_HOUR], [200] * sunrise.FORGOT_MAX_PER_HOUR)
        self.assertIn(429, codes)
        # une restauration ne bloque pas à cause d'une demande en attente
        sunrise._forgot_hits.clear()
        self.call("post", "/api/auth/forgot", json={"phone": "0708080808"})
        backup = self.call("get", "/api/admin/backup", admin).get_json()
        r = self.call("post", "/api/admin/restore", admin, json={"backup": backup, "dry_run": False, "confirmation": "RESTAURER"})
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_cancel_release_and_validation(self):
        admin = self.login("0700000000", "admin-secret")
        c, _ = self.register("client", "Koffi Yao", "0711111111")
        d, did = self.register("driver", "Seydou Ouattara", "0522222222")
        self.call("post", f"/api/admin/drivers/{did}/approval", admin, json={"decision": "approved"})
        self.call("post", "/api/driver/location", d, json={"lat": NEAR[0], "lng": NEAR[1]})
        self.call("put", "/api/driver/status", d, json={"availability": "available"})
        rid = self.call("post", "/api/requests", c, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1]}).get_json()["request"]["id"]
        self.call("post", f"/api/requests/{rid}/accept", d)
        # le chauffeur abandonne : la demande redevient ouverte
        self.assertEqual(self.call("post", f"/api/requests/{rid}/release", d).status_code, 200)
        self.assertEqual(self.call("get", "/api/requests/current", c).get_json()["request"]["status"], "open")
        # le client annule
        self.assertEqual(self.call("post", f"/api/requests/{rid}/cancel", c).status_code, 200)
        self.assertEqual(self.call("post", f"/api/requests/{rid}/cancel", c).status_code, 409)
        # validations d'entrée
        bad = [{"lat": "abc", "lng": 1}, {"lat": 95, "lng": 0}, {}]
        for b in bad:
            self.assertEqual(self.call("post", "/api/requests", c, json=b).status_code, 400)
        self.assertEqual(self.call("post", "/api/auth/register", json={"role": "client", "name": "A", "phone": "123", "password": "x"}).status_code, 400)

    def test_login_lockout_and_expiry(self):
        self.register("client", "Test Lock", "0733333333")
        for _ in range(5):
            r = self.call("post", "/api/auth/login", json={"phone": "0733333333", "password": "mauvais"})
            self.assertEqual(r.status_code, 401)
        self.assertEqual(self.call("post", "/api/auth/login", json={"phone": "0733333333", "password": "secret1"}).status_code, 429)

        c, _ = self.register("client", "Test Expire", "0744444444")
        rid = self.call("post", "/api/requests", c, json={"lat": 5.3, "lng": -4.0}).get_json()["request"]["id"]
        with sunrise.app.app_context():
            db = sunrise.get_db()
            db.execute("UPDATE ride_requests SET created_at='2000-01-01 00:00:00' WHERE id=?", (rid,))
            db.commit()
        cur = self.call("get", "/api/requests/current", c).get_json()["request"]
        self.assertEqual(cur["status"], "expired")

    def test_static_and_headers(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Sunrise Space", r.data)
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        csp = r.headers["Content-Security-Policy"]
        self.assertIn("https://tile.openstreetmap.org", csp)       # tuiles de la carte
        self.assertIn("https://router.project-osrm.org", csp)      # itinéraire
        self.assertEqual(self.c.get("/manifest.webmanifest").status_code, 200)
        self.assertEqual(self.c.get("/sw.js").status_code, 200)
        self.assertEqual(self.c.get("/api/inconnu").status_code, 404)
        self.assertEqual(self.c.get("/api/meta").get_json()["communes"][0], "Abobo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
