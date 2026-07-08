# 🎬 YesTubers — YouTube Video Downloader & Cutter SaaS

Téléchargez et coupez vos vidéos YouTube. **Freemium** — 1 téléchargement gratuit (480p), puis abonnements payants.

[![Repo](https://img.shields.io/badge/GitHub-BlackBeardAI%2FYesTubers-blue)](https://github.com/BlackBeardAI/YesTubers)

---

## 💰 Modèle économique

| Plan | Prix | Téléchargements | Qualité | Durée max |
|---|---|---|---|---|
| **Gratuit** | 0€ | 1 (one-shot) | 480p | 60s |
| **Basic** | 4.99€/mois | 20 | 720p | 300s |
| **Pro** | 14.99€/mois | 100 | 1080p | 900s |
| **Illimité** | 49.99€/mois | ∞ | 4K | 3600s |

→ Inscription obligatoire (email + mot de passe)  
→ Paiement via Stripe Checkout (abonnements mensuels)  
→ Pas de publicité — expérience premium

## Stack

- **Backend** : Python 3 + FastAPI + Jinja2
- **Téléchargement** : yt-dlp (qualité par plan)
- **Découpage** : ffmpeg (libx264, AAC)
- **Base de données** : SQLite + SQLAlchemy
- **Paiement** : Stripe (Checkout + Webhooks)
- **Frontend** : HTML/CSS/JS vanilla — thème sombre responsive

## Quickstart

```bash
# Créer un venv
python3 -m venv venv && source venv/bin/activate

# Installer les dépendances
pip install yt-dlp fastapi uvicorn sqlalchemy aiosqlite aiofiles jinja2 stripe python-multipart

# Configurer Stripe (optionnel pour test)
export STRIPE_PUBLIC_KEY="pk_test_..."
export STRIPE_SECRET_KEY="sk_test_..."

# Lancer
python3 main.py
```

→ http://localhost:8080

## Déploiement production

```bash
# Service systemd
sudo cp yestubers.service /etc/systemd/system/
sudo systemctl enable --now yestubers

# Nginx reverse proxy
sudo cp yestubers.nginx /etc/nginx/sites-available/yestubers
sudo ln -s /etc/nginx/sites-available/yestubers /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## Points d'accès DNS

```
yestubers.cloud → A → IP du VPS
```

Ajouter via API IONOS :
- Zone: `blackbeardai.org` (id: `9aa9a223-409c-11f1-ba27-0a586444112f`)
- Record: `cut` → type A → `217.160.191.107`

## Structure

```
/opt/yestubers/
├── main.py              # Application FastAPI (582 lignes)
├── static/
│   ├── css/style.css    # Thème sombre responsive
│   └── js/main.js
├── templates/
│   ├── index.html       # Landing page
│   ├── dashboard.html   # Gestion vidéos/cuts
│   ├── login.html
│   ├── signup.html
│   └── pricing.html     # Plans + Stripe Checkout
├── storage/             # Videos, cuts, thumbnails
├── yestubers.db             # SQLite (généré au 1er lancement)
├── yestubers.service        # Systemd unit
├── yestubers.nginx          # Configuration nginx
└── README.md
```

## API

### Auth
| Méthode | Route | Body |
|---|---|---|
| POST | `/api/auth/signup` | `email`, `password` |
| POST | `/api/auth/login` | `email`, `password` |
| POST | `/api/auth/logout` | — |

### Videos
| Méthode | Route | Description |
|---|---|---|
| POST | `/api/videos/download` | `url` — Télécharge selon la qualité du plan |
| GET | `/api/videos/{id}` | Infos vidéo |
| GET | `/api/videos/{id}/download` | Fichier MP4 |
| DELETE | `/api/videos/{id}` | Supprimer |

### Cuts
| Méthode | Route | Description |
|---|---|---|
| POST | `/api/cuts/create` | `video_id`, `start_time`, `end_time` |
| GET | `/api/cuts/{id}/download` | Fichier MP4 découpé |
| DELETE | `/api/cuts/{id}` | Supprimer |

### Stripe
| Méthode | Route | Description |
|---|---|---|
| POST | `/api/stripe/create-checkout` | `plan` — Redirige vers Stripe |
| POST | `/api/stripe/webhook` | Webhook Stripe (abonnements) |

## Licence

MIT — © 2026 BlackBeardAI
