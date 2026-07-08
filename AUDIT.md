# Audit concurrentiel + Bug hunt + Optimisation Yestubers

## 1. Audit concurrentiel

### noTube.lol (clone cible)
- **Modèle éco** : gratuit + abonnement noTube Plus (5€/mois ou 60€/an)
- **Forfaits** :
  - Gratuit : MP3/M4A/MP4 jusqu'à 1080p, publicités, file d'attente
  - Plus (5€/mois) : sans pub, 2K, playlists, priorités, app PC/Mac, conversions illimitées
- **Points forts** : simple, UX minimaliste, 2 mois offerts à l'annuel, beaucoup de formats (3GP/FLV)
- **Points faibles** : design vieillot, pas de découpage, pas de compte gratuit multi-credits, support limité

### Y2mate / SaveFrom / Yt1s
- **Modèle éco** : gratuit financé par ads agressives, redirections, pop-ups
- **Points forts** : reconnaissance de marque, SEO massif, multi-formats
- **Points faibles** : expérience utilisateur toxique, pas de monétisation propre, instable légalement

### Positionnement recommandé pour Yestubers
Yestubers doit rester **propre, rapide, sans pub** et vendre la **tranquillité** + **fonctions pro** (découpage, playlists, 4K) à un prix légèrement sous noTube pour capter.

---

## 2. Bug hunt

| # | Page / Flow | Statut | Détail |
|---|-------------|--------|--------|
| 1 | Homepage anonymous download | ✅ OK | POST /api/download 200, fichier MP3 reçu |
| 2 | Signup / Login | ✅ OK | compte créé, session cookie OK |
| 3 | Dashboard / Settings | ✅ OK | 200 avec session |
| 4 | Stripe checkout | ⚠️ 503 controlable | Clés Stripe de test invalides dans env ; passage en prod nécessaire |
| 5 | SEO pages (`/fr/pricing`, `/about`, etc.) | ✅ OK | 200, hreflang, canonical |
| 6 | Sitemap / robots / IndexNow | ✅ OK | 200, ping IndexNow 202 |
| 7 | Menu hamburger | 🔧 Fixé | nav-links manquants dans templates + CSS/JS corrigés |
| 8 | Responsive pricing | 🔧 Fixé | media queries + cache busting appliqués |

### Action requise critique
- Configurer de vraies **STRIPE_PUBLIC_KEY / STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET** dans l'environnement systemd du service `ytcut`.

---

## 3. Optimisation commerciale

### Tarifs actuels vs concurrence
| Plan | Yestubers | noTube Plus |
|------|-----------|-------------|
| Gratuit | 3 crédits/mois | illimité limité 1080p + pub |
| Entrée | 2,99€ (15 crédits) | — |
| Milieu | 5,99€ (100 crédits) ⭐ | 5€/mois illimité |
| Pro | 9,99€ illimité | — |

### Recommandations
1. **Différencier par le découpage** : noTube ne coupe pas. C'est LA feature vendeuse.
2. **Plan Starter** : 2,99€ est trop proche de 5,99€. Augmenter à **3,99€** ou donner 30 crédits pour justifier le gap.
3. **Plan Creator** : maintenir à **5,99€** comme plan star, mettre **200 crédits** pour battre noTube en volume.
4. **Plan Pro** : 9,99€ illimité est agressif. Peut monter à **11,99€** sans friction.
5. **Page pricing** : ajouter un comparatif visuel Free vs Creator + témoignages + FAQ.
6. **Funnel gratuit → payant** : après 3 downloads, afficher un modal "Passez Creator" avec CTA direct.

---

## 4. TODO prioritaire

- [ ] Remplacer les clés Stripe par des clés live
- [ ] Ajuster tarifs / crédits si validation utilisateur
- [ ] Ajouter comparatif gratuit/payant + témoignages sur `/pricing`
- [ ] Implémenter le post-download upsell modal
- [ ] Vérifier menu hamburger sur téléphone réel
