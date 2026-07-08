# Yestubers.cloud — Clone noTube finalisé + SEO mondial

## Livrables terminés

### 1. Clone noTube (homepage anonyme)
- Route `/api/download` publique, sans auth, 2 downloads/jour par IP
- Formats : MP3, M4A, WAV, MP4 (SD/HD/FHD/2K/4K), 3GP, FLV
- Plateformes : YouTube (yt-dlp supporte aussi TikTok/Insta/Facebook/X/Twitch via même API)
- Homepage style notube : URL + format + OK
- Téléchargement progressif streaming (pas de stockage serveur final)

### 2. Monétisation freemium agressive
- 4 plans : Gratuit / Starter 1.99€ / Creator 6.99€ / Pro 19.99€
- Stripe Checkout + webhook
- Page Pricing redesign : 4 cartes côte à côte, Creator mise en avant, toggle annuel/mensuel

### 3. SEO mondial (15 langues)
- Locales : fr, en, es, de, it, pt, nl, pl, ru, tr, ar, ja, ko, zh, hi
- hreflang x-default + toutes langues sur toutes les pages
- Sitemap XML multi-langue (105 URLs)
- Robots.txt, canonical, OG, JSON-LD Product/Organization
- IndexNow key valide, soumis à Bing/Yandex/api.indexnow.org

### 4. Référencement technique
- HTTPS ok
- Pages vitales : /pricing, /about, /contact, /terms, /privacy, /dmca, /security.txt
- Routes localisées : /fr/pricing, /es/about, /de/contact, etc.
- Page 404, cookie consent minimisé

### 5. Test réel
- MP3 "Me at the zoo" : OK (331 Ko, streaming)
- MP4 720p "Me at the zoo" : OK (629 Ko, streaming)
- yt-dlp mis à jour 2026.07.04

## Actions manuelles à faire par toi

1. Google Search Console ✅
   - Record DNS TXT ajouté : `google-site-verification=AvWphSlYq5L0gPM3YuITGgVOM9MbmFGRJo_sS9eQX-Y`
   - Propagation confirmée via Google DNS (8.8.8.8).
   - Va dans GSC, méthode "Enregistrement DNS", clique "Vérifier".
   - Une fois vérifié, soumets le sitemap : https://yestubers.cloud/sitemap.xml

2. Bing Webmaster Tools
   - Ajoute le site : https://yestubers.cloud/
   - Importe depuis GSC ou utilise le fichier HTML de vérification.
   - Le sitemap et IndexNow sont déjà pris en compte.

3. Créer des comptes réseaux sociaux (même vides) pour améliorer le "sameAs" du JSON-LD Organisation.

4. Acheter/réserver yestubers.com (si pas déjà fait) et rediriger 301 vers yestubers.cloud.

## Fichiers clés modifiés
- /opt/ytcut/main.py
- /opt/ytcut/templates/pricing.html
- /opt/ytcut/static/css/style.css
- /opt/ytcut/templates/about.html, contact.html
- /opt/ytcut/static/78CqOblo3fZYJJIdLYGN4WfnuHgLiFCW.txt
- /opt/ytcut/static/.well-known/security.txt
- /opt/ytcut/static/robots.txt
- /etc/systemd/system/yestubers.service

## Statut service
- systemctl : ytcut active
- dernière version yt-dlp : 2026.07.04
