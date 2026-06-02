# YT Cut — YouTube Video Downloader & Cutter SaaS

🎬 Téléchargez et coupez des vidéos YouTube. Monétisé avec Stripe.

## Stack
- **Backend** : Python 3 + FastAPI
- **Téléchargement** : yt-dlp
- **Découpage** : ffmpeg
- **Base de données** : SQLite
- **Paiement** : Stripe Checkout (abonnements)
- **Frontend** : HTML/CSS/JS vanilla (Jinja2 templates)

## Quickstart

```bash
# Installer les dépendances
pip install yt-dlp fastapi uvicorn sqlalchemy aiosqlite aiofiles jinja2 stripe python-multipart

# Configurer Stripe (optionnel)
export STRIPE_PUBLIC_KEY="pk_live_..."
export STRIPE_SECRET_KEY="sk_live_..."
export STRIPE_WEBHOOK_SECRET="whsec_..."

# Lancer le serveur
python3 main.py
```

Accès : http://localhost:8080

## Plans

| Plan       | Prix     | Crédits | Durée max |
|------------|----------|---------|-----------|
| Gratuit    | 0€       | 3       | 60s       |
| Basic      | 4.99€/mo | 20      | 300s      |
| Pro        | 14.99€/mo| 100     | 900s      |
| Illimité   | 49.99€/mo| ∞       | 3600s     |

## Déploiement

```bash
# Service systemd
cp ytcut.service /etc/systemd/system/
systemctl enable --now ytcut

# Nginx reverse proxy
cp ytcut.nginx /etc/nginx/sites-available/
ln -s /etc/nginx/sites-available/ytcut.nginx /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

## Structure

```
/opt/ytcut/
├── main.py              # Application FastAPI
├── static/              # CSS, JS
├── templates/           # Jinja2 templates
├── storage/             # Videos, cuts, thumbnails
└── ytcut.db             # SQLite database
```

## API

### Auth
- `POST /api/auth/signup` — email, password
- `POST /api/auth/login` — email, password
- `POST /api/auth/logout`
- `POST /api/auth/forgot` — email
- `POST /api/auth/reset` — token, password

### Videos
- `POST /api/videos/download` — url
- `GET /api/videos/{id}` — info
- `GET /api/videos/{id}/download` — fichier mp4
- `DELETE /api/videos/{id}`

### Cuts
- `POST /api/cuts/create` — video_id, start_time, end_time
- `GET /api/cuts/{id}/download` — fichier mp4
- `DELETE /api/cuts/{id}`

### Stripe
- `POST /api/stripe/create-checkout` — plan
- `POST /api/stripe/webhook` — Stripe webhook

## Licence

MIT — © 2026 BlackBeardAI
