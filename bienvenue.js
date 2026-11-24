/* ===== À MODIFIER : vos informations (une seule fois) ===== */
const INFO = {
  nom: "Coulibaly Pogniré Abraham",
  role: "Fondateur",
  ville: "Korhogo",
  telephone: "0759333200",                // 10 chiffres, sans espaces
  photo: "",                              // facultatif : nom d'un fichier photo ajouté au site
  intro: "Sunrise Space, c'est votre moto-taxi, tricycle ou voiture en quelques secondes, partout en Côte d'Ivoire. Chauffeurs vérifiés, numéros protégés, appels gratuits dans l'application : montez en confiance, sans jamais partager votre contact."
};
/* ========================================================= */
const $ = (id) => document.getElementById(id);
$('nom').textContent = INFO.nom; $('role').textContent = INFO.role; $('ville').textContent = INFO.ville; $('intro').textContent = INFO.intro;
const t = INFO.telephone.replace(/\D/g, '');
$('tel').href = 'tel:+225' + t; $('wa').href = 'https://wa.me/225' + t;
if (INFO.photo) { $('photo').src = '/' + INFO.photo; $('photo').hidden = false; }
