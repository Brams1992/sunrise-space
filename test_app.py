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
            for table in ("reports", "ride_requests", "tokens", "drivers", "users"):
                db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM sqlite_sequence")
            db.commit()
            sunrise.ensure_admin(db)
        sunrise._login_fails.clear()

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
        near = self.call("get", f"/api/drivers/nearby?lat={ABIDJAN[0]}&lng={ABIDJAN[1]}", client).get_json()["drivers"]
        self.assertEqual([x["id"] for x in near], [d1_id])
        self.assertLess(near[0]["distance_km"], 2)
        self.assertEqual(near[0]["whatsapp"], "0501010101")
        far = self.call("get", f"/api/drivers/nearby?lat={FAR[0]}&lng={FAR[1]}", client).get_json()["drivers"]
        self.assertEqual(far, [])

        # Position périmée => le chauffeur disparaît
        with sunrise.app.app_context():
            db = sunrise.get_db()
            db.execute("UPDATE drivers SET location_at='2000-01-01 00:00:00' WHERE user_id=?", (d1_id,))
            db.commit()
        self.assertEqual(self.call("get", f"/api/drivers/nearby?lat={ABIDJAN[0]}&lng={ABIDJAN[1]}", client).get_json()["drivers"], [])
        self.call("post", "/api/driver/location", d1, json={"lat": NEAR[0], "lng": NEAR[1]})

        # Demande de moto
        r = self.call("post", "/api/requests", client, json={"lat": ABIDJAN[0], "lng": ABIDJAN[1], "pickup_text": "Pharmacie", "destination_text": "Marché"})
        self.assertEqual(r.status_code, 201, r.get_json())
        rid = r.get_json()["request"]["id"]
        self.assertEqual(self.call("post", "/api/requests", client, json={"lat": 5.3, "lng": -4.0}).status_code, 409)

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
        self.assertEqual(dash["job"]["client_phone"], "0707070707")
        cur = self.call("get", "/api/requests/current", client).get_json()["request"]
        self.assertEqual(cur["status"], "accepted")
        self.assertEqual(cur["driver"]["plate"], "1234 AB 01")
        self.assertEqual(self.call("get", f"/api/drivers/nearby?lat={ABIDJAN[0]}&lng={ABIDJAN[1]}", client).get_json()["drivers"], [])

        # Signalements (client -> chauffeur, chauffeur -> client)
        r = self.call("post", "/api/reports", client, json={"target_user_id": d1_id, "reason": "prix", "details": "Trop cher", "request_id": rid})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.call("post", "/api/reports", client, json={"target_user_id": client_id, "reason": "prix"}).status_code, 400)
        self.assertEqual(self.call("post", "/api/reports", client, json={"target_user_id": d1_id, "reason": "inconnu"}).status_code, 400)
        self.assertEqual(self.call("post", "/api/reports", d1, json={"target_user_id": dash["job"]["client_id"], "reason": "comportement"}).status_code, 201)

        # Fin de course
        self.assertEqual(self.call("post", f"/api/requests/{rid}/complete", client).status_code, 200)
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
        self.assertEqual(self.c.get("/manifest.webmanifest").status_code, 200)
        self.assertEqual(self.c.get("/sw.js").status_code, 200)
        self.assertEqual(self.c.get("/api/inconnu").status_code, 404)
        self.assertEqual(self.c.get("/api/meta").get_json()["communes"][0], "Abobo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
