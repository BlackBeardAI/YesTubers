#!/usr/bin/env python3
"""Yestubers - YouTube Video Downloader & Cutter SaaS"""
import html
import time
import os
import re
import json
import uuid
import hmac
import hashlib
import secrets
import asyncio
import subprocess
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Optional

import aiofiles
from fastapi import FastAPI, Request, UploadFile, File, Form, Depends, HTTPException, Response, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import (
    create_engine, Column, String, Integer, Float, DateTime, Boolean, Text, ForeignKey, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
import stripe
import bcrypt

import smtplib
from email.mime.text import MIMEText

from jinja2 import Environment, FileSystemLoader, select_autoescape

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "noreply@yestubers.cloud")

def send_email(to: str, subject: str, body: str):
    msg = MIMEText(body, "html")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to
    try:
        with smtplib.SMTP(SMTP_HOST or "localhost", SMTP_PORT or 25, timeout=10) as server:
            if SMTP_HOST and SMTP_HOST not in ("localhost", "127.0.0.1"):
                server.starttls()
            if SMTP_USER and SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [to], msg.as_string())
        print(f"[email] Sent to {to}")
        return True
    except Exception as e:
        print(f"[email] Error sending to {to}: {e}")
        # Fallback: log locally for debugging
        try:
            log_path = BASE_DIR / "storage" / "emails.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"---\nTO: {to}\nSUBJECT: {subject}\n{body}\n---\n")
        except Exception:
            pass
        return False

# Rate limiting in-memory (IP-based)
RATE_LIMIT = {
    "default": {"window": 60, "max": 60},
    "auth": {"window": 300, "max": 10},
    "download": {"window": 60, "max": 10},
}
_rate_store: dict[str, dict] = {}

def _rate_limit_key(request: Request, scope: str = "default") -> str:
    xff = request.headers.get("x-forwarded-for", "")
    ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "unknown")
    return f"{ip}:{scope}"

def check_rate_limit(request: Request, scope: str = "default"):
    cfg = RATE_LIMIT.get(scope, RATE_LIMIT["default"])
    now = datetime.utcnow()
    key = _rate_limit_key(request, scope)
    bucket = _rate_store.get(key)
    if not bucket or (now - bucket["reset"]).total_seconds() > cfg["window"]:
        bucket = {"count": 0, "reset": now}
    bucket["count"] += 1
    _rate_store[key] = bucket
    if bucket["count"] > cfg["max"]:
        raise HTTPException(429, "Trop de requêtes. Réessayez plus tard.")

# ─── Config ───────────────────────────────────────────────────────────────────

BASE_DIR = Path("/opt/ytcut")
STORAGE = BASE_DIR / "storage"
VIDEOS = STORAGE / "videos"
CUTS = STORAGE / "cuts"
# No local thumbnails are kept; only remote YouTube thumbnail URLs are stored in DB.
THUMBS = STORAGE / "thumbnails"

NODE_PATH = shutil.which("node") or "/usr/bin/node"

DB_PATH = BASE_DIR / "yestubers.db"
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# Stripe — optional. If keys are missing, the site runs in autonomous/free mode.
STRIPE_PK = os.environ.get("STRIPE_PUBLIC_KEY", "")
STRIPE_SK = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
# Fallback to .env if systemd does not load env
if not STRIPE_SK:
    try:
        _env_lines = Path('/opt/ytcut/.env').read_text().splitlines()
        _env = {k: v for k, v in (line.split('=', 1) for line in _env_lines if '=' in line and not line.startswith('#'))}
        STRIPE_SK = _env.get('STRIPE_SECRET_KEY', '')
        STRIPE_PK = _env.get('STRIPE_PUBLIC_KEY', '')
        STRIPE_WEBHOOK = _env.get('STRIPE_WEBHOOK_SECRET', '')
    except Exception:
        pass

STRIPE_ENABLED = bool(STRIPE_SK and STRIPE_SK.startswith("sk_") and len(STRIPE_SK) > 12)
print(f"Stripe enabled: {STRIPE_ENABLED}")

# Stripe python client is only loaded if enabled
import importlib
stripe = None
if STRIPE_ENABLED:
    try:
        stripe = importlib.import_module("stripe")
        stripe.api_key = STRIPE_SK
    except Exception as e:
        print(f"Stripe import failed: {e}")
        stripe = None
        STRIPE_ENABLED = False

# billing: "monthly" ou "annual" — l'annuel est poussé par défaut (-33%)
# Overridable Stripe Price IDs from env (monthly / annual)
_STRIPE_PRICE_IDS = {
    "starter": (os.environ.get("STRIPE_STARTER_PRICE_ID"), os.environ.get("STRIPE_STARTER_ANNUAL_PRICE_ID")),
    "creator": (os.environ.get("STRIPE_CREATOR_PRICE_ID"), os.environ.get("STRIPE_CREATOR_ANNUAL_PRICE_ID")),
    "pro": (os.environ.get("STRIPE_PRO_PRICE_ID"), os.environ.get("STRIPE_PRO_ANNUAL_PRICE_ID")),
}

def _resolve_stripe_price(pid: str) -> tuple[float, str]:
    """Return (amount_eur, interval) for a Stripe price id. Cache results."""
    if not pid or not stripe:
        return (0.0, "month")
    try:
        price = stripe.Price.retrieve(pid)
        amount = price["unit_amount"] / 100.0
        interval = price["recurring"]["interval"] if "recurring" in price else "month"
        return (amount, interval)
    except Exception as e:
        return (0.0, "month")


# Default plan metadata (features, credits). Prices are synced from Stripe live Price IDs below.
_PLAN_DEFAULTS = {
    "free": {
        "name": "Gratuit",
        "description": "1 aperçu gratuit par semaine. Idéal pour tester.",
        "credits": 1, "max_duration": 30, "quality": "360",
        "quality_options": ["360"],
        "features": ["1 aperçu/semaine", "Qualité 360p", "Watermark", "Sans engagement"],
        "popular": False,
        "tripwire": True,
    },
    "starter": {
        "name": "Starter",
        "description": "Débloquez HD, MP3 et 25 crédits. Offre d'appel 1€ le premier mois.",
        "credits": 25, "max_duration": 300, "quality": "720",
        "quality_options": ["360", "720"],
        "features": ["25 crédits/mois", "MP3 & MP4 HD 720p", "Sans publicité", "Support email"],
        "popular": False,
        "tripwire": True,
    },
    "creator": {
        "name": "Creator",
        "description": "Le sweet spot pour les créateurs réguliers.",
        "credits": 100, "max_duration": 1200, "quality": "1080",
        "quality_options": ["360", "720", "1080"],
        "features": ["100 crédits/mois", "Full HD 1080p", "Découpe intégrée", "Support prioritaire"],
        "popular": True,
    },
    "pro": {
        "name": "Pro",
        "description": "Usage illimité en 4K pour pros et agences.",
        "credits": 999999, "max_duration": 3600, "quality": "4K",
        "quality_options": ["360", "720", "1080", "4K"],
        "features": ["Crédits illimités", "Qualité 4K", "Découpe avancée", "Support prioritaire", "Accès API"],
        "popular": False,
    },
}


_DEFAULT_MONTHLY_PRICES = {
    "starter": 2.99,
    "creator": 5.99,
    "pro": 11.99,
}


def _build_plans() -> dict:
    plans = {
        "free": {
            **_PLAN_DEFAULTS["free"],
            "monthly_price": 0,
            "original_monthly_price": 0,
            "stripe_price_id": None,
            "stripe_annual_price_id": None,
        }
    }
    for key in ("starter", "creator", "pro"):
        monthly_pid, annual_pid = _STRIPE_PRICE_IDS[key]
        monthly_amount, monthly_interval = _resolve_stripe_price(monthly_pid) if monthly_pid else (0.0, "month")
        annual_amount, annual_interval = _resolve_stripe_price(annual_pid) if annual_pid else (0.0, "year")
        # Fall back to default prices if Stripe IDs are not configured.
        if monthly_amount == 0.0:
            monthly_amount = _DEFAULT_MONTHLY_PRICES.get(key, 0.0)
        if annual_amount == 0.0:
            annual_amount = round(monthly_amount * 12 * 0.67, 2)  # 33% annual discount
        # If interval is year, amount is the full annual price. Convert to monthly equivalent for display.
        displayed_monthly = monthly_amount
        displayed_annual_monthly = (annual_amount / 12.0) if annual_interval == "year" else annual_amount
        # Cross-out price for annual tab = monthly price (user sees "annual equivalent" vs full monthly).
        annual_monthly_equiv = round(displayed_annual_monthly, 2) if displayed_annual_monthly else round(displayed_monthly, 2)
        plans[key] = {
            **_PLAN_DEFAULTS[key],
            "monthly_price": round(displayed_monthly, 2),
            "annual_monthly_price": annual_monthly_equiv,
            "original_monthly_price": round(displayed_monthly, 2),
            "stripe_price_id": monthly_pid,
            "stripe_annual_price_id": annual_pid,
        }
    return plans


PLANS = _build_plans()


# Anonymous weekly quota (IP-based) for public /api/download
# Free anonymous teaser: 1 download/week, 360p max, video only, watermarked.
# Everything else (audio, HD, cutter, more downloads) requires a free account.
ANON_LIMIT = 1  # downloads per 7 days per IP
ANON_MAX_QUALITY = "360"
ANON_WINDOW_DAYS = 7
_anon_counts: dict[str, dict[str, any]] = {}

def _anon_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def _check_anon_limit(ip: str):
    now = datetime.utcnow()
    if ip not in _anon_counts or (now - _anon_counts[ip].get("first_seen", now)).days >= ANON_WINDOW_DAYS:
        _anon_counts[ip] = {"first_seen": now, "count": 0}
    if _anon_counts[ip]["count"] >= ANON_LIMIT:
        raise HTTPException(429, "quota_anon_exceeded")
    _anon_counts[ip]["count"] += 1

def _anon_remaining(ip: str) -> int:
    now = datetime.utcnow()
    if ip not in _anon_counts or (now - _anon_counts[ip].get("first_seen", now)).days >= ANON_WINDOW_DAYS:
        return ANON_LIMIT
    return max(0, ANON_LIMIT - _anon_counts[ip]["count"])

async def download_public(url: str, fmt: str, quality: str, max_duration: int = 120) -> dict:
    info = get_video_info(url)
    duration = info.get("duration", 0) or 0
    if duration > max_duration:
        raise Exception(f"Durée limitée à {max_duration}s en mode gratuit. Créez un compte.")
    vid_db = str(uuid.uuid4())
    base_path = VIDEOS / f"{vid_db}"
    title = re.sub(r'[^\w\-. ]', '_', info.get("title", "video"))[:50]

    fmt = fmt.lower()
    if fmt != "mp4":
        raise Exception("Format anonyme non supporté. Créez un compte gratuit pour MP3, HD et plus.")
    out_path = base_path.with_suffix(".mp4")
    quality_map = {"sd": "360", "hd": "360", "fullhd": "360", "2k": "360", "4k": "360"}
    max_height = quality_map.get(quality, quality) if quality not in quality_map.values() else quality
    # force anonymous ceiling
    try:
        h = int(max_height)
    except ValueError:
        h = 360
    if h > 360:
        max_height = "360"
    cmd = [
        "yt-dlp", "--no-playlist",
        "--js-runtimes", f"node:{NODE_PATH}",
        "-f",
        f"best[height<={max_height}][ext=mp4]/best[height<={max_height}]/best",
        "-o", str(base_path), url
    ]

    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode()
        raise Exception(f"Erreur yt-dlp: {err[:200]}")

    # Rename if yt-dlp created base path without proper extension
    if not out_path.exists():
        candidates = [p for p in VIDEOS.iterdir() if p.stem == vid_db]
        if candidates:
            out_path = candidates[0]

    # Add watermark overlay for anonymous preview
    if out_path.exists():
        try:
            watermarked = out_path.with_stem(out_path.stem + "_wm")
            overlay_text = "YESTUBERS.FREE.PREVIEW"
            overlay_cmd = [
                "ffmpeg", "-y", "-i", str(out_path),
                "-vf", f"drawtext=text='{overlay_text}':fontcolor=white@0.4:fontsize=24:x=(w-text_w)/2:y=(h-text_h)/2",
                "-c:a", "copy", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                str(watermarked)
            ]
            wm_proc = await asyncio.create_subprocess_exec(*overlay_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, _ = await wm_proc.communicate()
            if wm_proc.returncode == 0 and watermarked.exists():
                out_path.unlink()
                out_path = watermarked
        except Exception:
            pass

    # Cleanup temp anonymous files older than 24h
    now = datetime.utcnow()
    for p in VIDEOS.iterdir():
        try:
            if p.is_file() and (now - datetime.fromtimestamp(p.stat().st_mtime)) > timedelta(hours=24):
                p.unlink()
        except Exception:
            pass

    filesize = out_path.stat().st_size if out_path.exists() else 0
    return {"video_id": vid_db, "title": info.get("title", "Sans titre"),
            "duration": duration, "filename": out_path.name, "filesize": filesize}



# ─── i18n / Multi-language (world SEO) ──────────────────────────────────────────

SUPPORTED_LOCALES = ["fr", "en", "es", "de", "it", "pt", "nl", "pl", "ar", "hi", "ja", "ko", "zh", "ru", "tr"]
DEFAULT_LOCALE = "fr"

LOCALE_REGION = {
    "fr": "FR", "en": "US", "es": "ES", "de": "DE", "it": "IT", "pt": "PT",
    "nl": "NL", "pl": "PL", "ar": "SA", "hi": "IN", "ja": "JP", "ko": "KR",
    "zh": "CN", "ru": "RU", "tr": "TR",
}

I18N = {
    "fr": {
        "brand": "Yestubers",
        "title_home": "YouTube Downloader — Téléchargeur MP3/MP4 Gratuit",
        "desc_home": "Téléchargez des vidéos YouTube gratuitement avec Yestubers, le meilleur YouTube downloader. MP3 192 kbps, MP4 HD, sans inscription, 3 crédits offerts.",
        "compare_title": "Gratuit vs Creator",
        "feature": "Fonction",
        "free": "Gratuit",
        "yes": "Oui",
        "no": "Non",
        "feature_quality": "Qualité",
        "feature_ads": "Publicités",
        "feature_credits": "Crédits",
        "feature_cut": "Découpage",
        "feature_playlist": "Playlists",
        "feature_support": "Support prioritaire",
        "testimonials_title": "Ils utilisent Yestubers",
        "cta_placeholder": "Collez le lien YouTube ici...",
        "cta_button": "Télécharger",
        "formats": "MP3, MP4, M4A, WAV",
        "free_badge": "100% gratuit — 2 essais sans compte",
        "upgrade_badge": "Passez Premium",
        "pricing_title": "Tarifs Yestubers",
        "pricing_desc": "Choisissez le plan Yestubers idéal : gratuit, Starter, Creator ou Pro. Téléchargements YouTube MP3/MP4 illimités, découpage, 4K.",
        "pricing_badge": "LE PLUS VENDU",
        "pricing_h1": "Téléchargez sans limite à partir de <span class=\"gradient-text\">5,99€/mois</span>",
        "pricing_sub": "Résiliez quand vous voulez. Satisfait ou remboursé sous 7 jours.",
        "annual": "Annuel",
        "monthly": "Mensuel",
        "best_choice": "Le plus choisi",
        "unlimited": "Illimités",
        "your_plan": "Votre plan",
        "billed_year": "facturés/an",
        "billed_month": "facturés/mois",
        "no_commitment": "Sans engagement",
        "save_up_to": "Économisez jusqu'à {amount}€ par an",
        "max_quality": "qualité max",
        "credits_per_month": "crédits/mois",
        "max_duration": "durée max/vidéo",
        "feature_cut": "Découpage vidéo",
        "feature_playlist": "Playlists entières",
        "feature_support": "Support prioritaire",
        "feature_no_ads": "Sans publicité",
        "choose": "Choisir",
        "current_plan_btn": "Plan actuel",
        "start_free": "Commencer gratuit",
        "desc_free": "3 vidéos gratuites chaque mois, sans engagement.",
        "desc_starter": "30 vidéos HD par mois. Idéal pour un usage régulier.",
        "desc_creator": "200 vidéos Full HD par mois. Le plus vendu.",
        "desc_pro": "Téléchargements illimités en 4K. Pour les pros.",
        "guarantee_1_title": "Satisfait ou remboursé",
        "guarantee_1_text": "7 jours pour tester. Annulez facilement depuis votre compte.",
        "guarantee_2_title": "Téléchargement instantané",
        "guarantee_2_text": "Serveurs rapides et bande passante prioritaire sur les plans payants.",
        "guarantee_3_title": "Disponible dans le monde entier",
        "guarantee_3_text": "Interface traduite en 15 langues pour télécharger partout.",
        "nav_pricing": "Tarifs",
        "nav_dashboard": "Dashboard",
        "nav_login": "Connexion",
        "nav_signup": "Essayer gratuit",
        "nav_logout": "Déconnexion",
        "footer_tagline": "Le convertisseur vidéo simple, rapide et respectueux de votre vie privée.",
        "footer_product": "Produit",
        "footer_legal": "Légal",
        "footer_help": "Aide",
        "about": "À propos",
        "contact": "Contact",
        "terms": "CGU",
        "privacy": "Confidentialité",
        "all_rights": "Tous droits réservés.",
        "hero_h1_part1": "Téléchargez n'importe quelle vidéo en",
        "hero_sub": "Collez un lien YouTube, TikTok, Instagram ou Facebook. Choisissez le format. C'est téléchargé. Simple, rapide, sans pub.",
        "tab_video": "🎬 Vidéo (MP4)",
        "tab_audio": "🎵 Audio (MP3)",
        "format_label": "Format",
        "playlist": "Playlist entière",
        "paste": "Coller",
        "status_preparing": "Préparation...",
        "status_finalizing": "Finalisation...",
        "trust_1": "Téléchargement instantané",
        "trust_2": "Sans stockage sur nos serveurs",
        "trust_3": "1000+ sites supportés",
        "compatible": "Compatible avec",
        "why_title": "Pourquoi",
        "why_sub": "Le convertisseur le plus simple et le plus puissant du marché.",
        "feature_mp3_title": "MP3 Haute Qualité",
        "feature_mp3_text": "Extraire l'audio en MP3 128kbps ou MP3 HD 320kbps. Parfait pour la musique et les podcasts.",
        "feature_mp4_title": "MP4 jusqu'à 4K",
        "feature_mp4_text": "Téléchargez en 720p, 1080p, 1440p ou 4K UHD. Streaming progressif directement dans votre navigateur.",
        "feature_speed_title": "Sans Limite de Vitesse",
        "feature_speed_text": "Avec un compte premium, profitez de téléchargements illimités et prioritaires.",
        "feature_anon_title": "100% Anonyme",
        "feature_anon_text": "Aucun fichier stocké sur nos serveurs. Tout passe par votre cache navigateur.",
        "feature_devices_title": "Tous Appareils",
        "feature_devices_text": "Fonctionne sur mobile, tablette et desktop. Aucune application à installer.",
        "feature_cut_title": "Découpage Vidéo",
        "feature_cut_text": "Membres premium : coupez vos vidéos directement en ligne et téléchargez seulement le passage voulu.",
        "teaser_title": "Débloquez le téléchargement illimité",
        "teaser_text": "Sans pub, sans limite, en HD/4K. Découpez vos vidéos en 1 clic. Dès 5,99€/mois.",
        "teaser_price": "5.99€<span>/mois</span>",
        "teaser_cta": "Voir les offres →",
        "faq_title": "Questions fréquentes",
        "faq_q1": "Yestubers est-il gratuit ?",
        "faq_a1": "Oui. Vous pouvez télécharger 2 vidéos sans inscription. Ensuite, créez un compte gratuit pour 3 crédits par mois.",
        "faq_q2": "Quels formats sont disponibles ?",
        "faq_a2": "MP3, MP3 HD, M4A, WAV pour l'audio. MP4, MP4 HD, MP4 2K, MP4 4K, 3GP, FLV pour la vidéo.",
        "faq_q3": "Mes fichiers sont-ils stockés ?",
        "faq_a3": "Non. Nous utilisons le streaming progressif : les octets passent directement de nos serveurs à votre navigateur sans stockage.",
        "faq_q4": "Puis-je télécharger des playlists ?",
        "faq_a4": "Oui, cochez l'option \"Playlist entière\" et nous compresserons toutes les vidéos dans un fichier ZIP.",
        "nav_features": "Fonctionnalités",
        "nav_about": "À propos",
        "signup": "Inscription",
        "cookie_text": "Nous utilisons des cookies essentiels uniquement.",
        "credits_label": "crédits",
        "dash_no_credits": "Plus de crédits",
        "dash_upgrade_cta": "Passez à un plan à partir de 4,99€",
        "dash_credits_left": "Plus que",
        "dash_unlock_hd": "Débloquez HD et plus de téléchargements",
        "dash_url_placeholder": "URL YouTube...",
        "dash_my_videos": "Mes vidéos",
        "dash_my_cuts": "Mes extraits",
        "dash_processing": "Traitement...",
        "dash_error": "Erreur",
        "dash_untitled": "Sans titre",
        "dash_download": "Télécharger",
        "dash_cut": "Couper",
        "dash_delete": "Supprimer",
        "dash_cancel": "Annuler",
        "dash_no_videos": "Aucune vidéo. Collez une URL YouTube ci-dessus.",
        "dash_no_cuts": "Aucun extrait. Coupez une vidéo ci-dessus.",
        "dash_cut_title": "Couper la vidéo",
        "dash_cut_start": "Début (s) :",
        "dash_cut_end": "Fin (s) :",
    },
    "en": {
        "brand": "Yestubers",
        "title_home": "YouTube Downloader — Free MP3/MP4",
        "desc_home": "Free YouTube downloader for MP3 and MP4. Download any YouTube video in HD or audio. No signup needed, 3 free credits per month.",
        "compare_title": "Free vs Creator",
        "feature": "Feature",
        "free": "Free",
        "yes": "Yes",
        "no": "No",
        "feature_quality": "Quality",
        "feature_ads": "Ads",
        "feature_credits": "Credits",
        "feature_cut": "Trim / Cut",
        "feature_playlist": "Playlists",
        "feature_support": "Priority support",
        "testimonials_title": "They use Yestubers",
        "cta_placeholder": "Paste YouTube link here...",
        "cta_button": "Download",
        "formats": "MP3, MP4, M4A, WAV",
        "free_badge": "100% free — 2 trials without account",
        "upgrade_badge": "Go Premium",
        "pricing_title": "Pricing",
        "pricing_desc": "Choose your Yestubers plan: free, Starter, Creator or Pro. Unlimited YouTube MP3/MP4 downloads, cutting, 4K.",
        "pricing_badge": "BEST SELLER",
        "pricing_h1": "Unlimited downloads from <span class=\"gradient-text\">€5.99/mo</span>",
        "pricing_sub": "Annual = 4 months free. Cancel anytime. 7-day money-back guarantee.",
        "annual": "Annual",
        "monthly": "Monthly",
        "best_choice": "Best choice",
        "unlimited": "Unlimited",
        "your_plan": "Your plan",
        "billed_year": "billed/year",
        "billed_month": "billed/month",
        "no_commitment": "No commitment",
        "save_up_to": "Save up to {amount}€/year",
        "max_quality": "max quality",
        "credits_per_month": "credits/month",
        "max_duration": "max duration/video",
        "feature_cut": "Video cutting",
        "feature_playlist": "Full playlists",
        "feature_support": "Priority support",
        "feature_no_ads": "No ads",
        "choose": "Choose",
        "current_plan_btn": "Current plan",
        "start_free": "Start free",
        "desc_free": "Try with no commitment. 3 videos per month.",
        "desc_starter": "Perfect for occasional HD use.",
        "desc_creator": "Best value for creators.",
        "desc_pro": "For professionals and heavy users.",
        "guarantee_1_title": "Money-back guarantee",
        "guarantee_1_text": "7 days to test. Cancel easily from your account.",
        "guarantee_2_title": "Instant downloads",
        "guarantee_2_text": "Fast servers and priority bandwidth on paid plans.",
        "guarantee_3_title": "Available worldwide",
        "guarantee_3_text": "Interface translated into 15 languages so anyone can download.",
        "nav_pricing": "Pricing",
        "nav_dashboard": "Dashboard",
        "nav_login": "Login",
        "nav_signup": "Try free",
        "nav_logout": "Logout",
        "footer_tagline": "The simple, fast and privacy-friendly video converter.",
        "footer_product": "Product",
        "footer_legal": "Legal",
        "footer_help": "Help",
        "about": "About",
        "contact": "Contact",
        "terms": "Terms",
        "privacy": "Privacy",
        "all_rights": "All rights reserved.",
        "hero_h1_part1": "Download any video as",
        "hero_sub": "Paste a YouTube, TikTok, Instagram or Facebook link. Choose format. It's downloaded. Simple, fast, ad-free.",
        "tab_video": "🎬 Video (MP4)",
        "tab_audio": "🎵 Audio (MP3)",
        "format_label": "Format",
        "playlist": "Full playlist",
        "paste": "Paste",
        "status_preparing": "Preparing...",
        "status_finalizing": "Finalizing...",
        "trust_1": "Instant download",
        "trust_2": "No files stored on our servers",
        "trust_3": "1000+ supported sites",
        "compatible": "Compatible with",
        "why_title": "Why",
        "why_sub": "The simplest and most powerful converter on the market.",
        "feature_mp3_title": "High Quality MP3",
        "feature_mp3_text": "Extract audio in MP3 128kbps or MP3 HD 320kbps. Perfect for music and podcasts.",
        "feature_mp4_title": "MP4 up to 4K",
        "feature_mp4_text": "Download in 720p, 1080p, 1440p or 4K UHD. Progressive streaming directly in your browser.",
        "feature_speed_title": "No Speed Limit",
        "feature_speed_text": "With a premium account, enjoy unlimited and prioritized downloads.",
        "feature_anon_title": "100% Anonymous",
        "feature_anon_text": "No files stored on our servers. Everything goes through your browser cache.",
        "feature_devices_title": "All Devices",
        "feature_devices_text": "Works on mobile, tablet and desktop. No app to install.",
        "feature_cut_title": "Video Cutting",
        "feature_cut_text": "Premium members: cut videos online and download only the part you want.",
        "teaser_title": "Unlock unlimited downloads",
        "teaser_text": "No ads, no limits, HD/4K. Cut videos in 1 click. Go Starter from 4.99€/month.",
        "teaser_price": "4.99€<span>/month</span>",
        "teaser_cta": "See plans →",
        "faq_title": "Frequently asked questions",
        "faq_q1": "Is Yestubers free?",
        "faq_a1": "Yes. Download 2 videos without signing up. Then create a free account for 3 credits per month.",
        "faq_q2": "What formats are available?",
        "faq_a2": "MP3, MP3 HD, M4A, WAV for audio. MP4, MP4 HD, MP4 2K, MP4 4K, 3GP, FLV for video.",
        "faq_q3": "Are my files stored?",
        "faq_a3": "No. We use progressive streaming: bytes go directly from our servers to your browser with no storage.",
        "faq_q4": "Can I download playlists?",
        "faq_a4": "Yes, check \"Full playlist\" and we will zip all videos for you.",
        "nav_features": "Features",
        "nav_about": "About",
        "signup": "Sign up",
        "cookie_text": "We only use essential cookies.",
        "credits_label": "credits",
        "dash_no_credits": "No credits left",
        "dash_upgrade_cta": "Upgrade from €3.99",
        "dash_credits_left": "Only",
        "dash_unlock_hd": "Unlock HD and more downloads",
        "dash_url_placeholder": "YouTube URL...",
        "dash_my_videos": "My videos",
        "dash_my_cuts": "My cuts",
        "dash_processing": "Processing...",
        "dash_error": "Error",
        "dash_untitled": "Untitled",
        "dash_download": "Download",
        "dash_cut": "Cut",
        "dash_delete": "Delete",
        "dash_cancel": "Cancel",
        "dash_no_videos": "No videos. Paste a YouTube URL above.",
        "dash_no_cuts": "No cuts. Cut a video above.",
        "dash_cut_title": "Cut video",
        "dash_cut_start": "Start (s):",
        "dash_cut_end": "End (s):",
    },
    "es": {
        "brand": "Yestubers",
        "title_home": "Free YouTube MP3/MP4 Downloader",
        "desc_home": "Yestubers is the fastest YouTube converter. Download any video as MP3, MP4, M4A or WAV with no signup.",
        "compare_title": "Free vs Creator",
        "feature": "Feature",
        "free": "Free",
        "yes": "Yes",
        "no": "No",
        "feature_quality": "Quality",
        "feature_ads": "Ads",
        "feature_credits": "Credits",
        "feature_cut": "Trim / Cut",
        "feature_playlist": "Playlists",
        "feature_support": "Priority support",
        "testimonials_title": "They use Yestubers",
        "cta_placeholder": "Paste YouTube link here...",
        "cta_button": "Download",
        "formats": "MP3, MP4, M4A, WAV",
        "free_badge": "100% free — 2 trials without account",
        "upgrade_badge": "Go Premium",
        "pricing_title": "Pricing",
        "pricing_desc": "Choose your Yestubers plan: free, Starter, Creator or Pro. Unlimited YouTube MP3/MP4 downloads, cutting, 4K.",
        "pricing_badge": "BEST SELLER",
        "pricing_h1": "Unlimited downloads from <span class=\"gradient-text\">€5.99/mo</span>",
        "pricing_sub": "Annual = 4 months free. Cancel anytime. 7-day money-back guarantee.",
        "annual": "Annual",
        "monthly": "Monthly",
        "best_choice": "Best choice",
        "unlimited": "Unlimited",
        "your_plan": "Your plan",
        "billed_year": "billed/year",
        "billed_month": "billed/month",
        "no_commitment": "No commitment",
        "save_up_to": "Save up to {amount}€/year",
        "max_quality": "max quality",
        "credits_per_month": "credits/month",
        "max_duration": "max duration/video",
        "feature_cut": "Video cutting",
        "feature_playlist": "Full playlists",
        "feature_support": "Priority support",
        "feature_no_ads": "No ads",
        "choose": "Choose",
        "current_plan_btn": "Current plan",
        "start_free": "Start free",
        "desc_free": "Try with no commitment. 3 videos per month.",
        "desc_starter": "Perfect for occasional HD use.",
        "desc_creator": "Best value for creators.",
        "desc_pro": "For professionals and heavy users.",
        "guarantee_1_title": "Money-back guarantee",
        "guarantee_1_text": "7 days to test. Cancel easily from your account.",
        "guarantee_2_title": "Instant downloads",
        "guarantee_2_text": "Fast servers and priority bandwidth on paid plans.",
        "guarantee_3_title": "Available worldwide",
        "guarantee_3_text": "Interface translated into 15 languages so anyone can download.",
        "nav_pricing": "Pricing",
        "nav_dashboard": "Dashboard",
        "nav_login": "Login",
        "nav_signup": "Try free",
        "nav_logout": "Logout",
        "footer_tagline": "The simple, fast and privacy-friendly video converter.",
        "footer_product": "Product",
        "footer_legal": "Legal",
        "footer_help": "Help",
        "about": "About",
        "contact": "Contact",
        "terms": "Terms",
        "privacy": "Privacy",
        "all_rights": "All rights reserved.",
        "hero_h1_part1": "Download any video as",
        "hero_sub": "Paste a YouTube, TikTok, Instagram or Facebook link. Choose format. It's downloaded. Simple, fast, ad-free.",
        "tab_video": "🎬 Video (MP4)",
        "tab_audio": "🎵 Audio (MP3)",
        "format_label": "Format",
        "playlist": "Full playlist",
        "paste": "Paste",
        "status_preparing": "Preparing...",
        "status_finalizing": "Finalizing...",
        "trust_1": "Instant download",
        "trust_2": "No files stored on our servers",
        "trust_3": "1000+ supported sites",
        "compatible": "Compatible with",
        "why_title": "Why",
        "why_sub": "The simplest and most powerful converter on the market.",
        "feature_mp3_title": "High Quality MP3",
        "feature_mp3_text": "Extract audio in MP3 128kbps or MP3 HD 320kbps. Perfect for music and podcasts.",
        "feature_mp4_title": "MP4 up to 4K",
        "feature_mp4_text": "Download in 720p, 1080p, 1440p or 4K UHD. Progressive streaming directly in your browser.",
        "feature_speed_title": "No Speed Limit",
        "feature_speed_text": "With a premium account, enjoy unlimited and prioritized downloads.",
        "feature_anon_title": "100% Anonymous",
        "feature_anon_text": "No files stored on our servers. Everything goes through your browser cache.",
        "feature_devices_title": "All Devices",
        "feature_devices_text": "Works on mobile, tablet and desktop. No app to install.",
        "feature_cut_title": "Video Cutting",
        "feature_cut_text": "Premium members: cut videos online and download only the part you want.",
        "teaser_title": "Unlock unlimited downloads",
        "teaser_text": "No ads, no limits, HD/4K. Cut videos in 1 click. Go Starter from 4.99€/month.",
        "teaser_price": "4.99€<span>/month</span>",
        "teaser_cta": "See plans →",
        "faq_title": "Frequently asked questions",
        "faq_q1": "Is Yestubers free?",
        "faq_a1": "Yes. Download 2 videos without signing up. Then create a free account for 3 credits per month.",
        "faq_q2": "What formats are available?",
        "faq_a2": "MP3, MP3 HD, M4A, WAV for audio. MP4, MP4 HD, MP4 2K, MP4 4K, 3GP, FLV for video.",
        "faq_q3": "Are my files stored?",
        "faq_a3": "No. We use progressive streaming: bytes go directly from our servers to your browser with no storage.",
        "faq_q4": "Can I download playlists?",
        "faq_a4": "Yes, check \"Full playlist\" and we will zip all videos for you.",
        "nav_features": "Features",
        "nav_about": "About",
        "signup": "Sign up",
        "cookie_text": "We only use essential cookies.",
    },
    "de": {
        "brand": "Yestubers",
        "title_home": "Free YouTube MP3/MP4 Downloader",
        "desc_home": "Yestubers is the fastest YouTube converter. Download any video as MP3, MP4, M4A or WAV with no signup.",
        "compare_title": "Free vs Creator",
        "feature": "Feature",
        "free": "Free",
        "yes": "Yes",
        "no": "No",
        "feature_quality": "Quality",
        "feature_ads": "Ads",
        "feature_credits": "Credits",
        "feature_cut": "Trim / Cut",
        "feature_playlist": "Playlists",
        "feature_support": "Priority support",
        "testimonials_title": "They use Yestubers",
        "cta_placeholder": "Paste YouTube link here...",
        "cta_button": "Download",
        "formats": "MP3, MP4, M4A, WAV",
        "free_badge": "100% free — 2 trials without account",
        "upgrade_badge": "Go Premium",
        "pricing_title": "Pricing",
        "pricing_desc": "Choose your Yestubers plan: free, Starter, Creator or Pro. Unlimited YouTube MP3/MP4 downloads, cutting, 4K.",
        "pricing_badge": "BEST SELLER",
        "pricing_h1": "Unlimited downloads from <span class=\"gradient-text\">€5.99/mo</span>",
        "pricing_sub": "Annual = 4 months free. Cancel anytime. 7-day money-back guarantee.",
        "annual": "Annual",
        "monthly": "Monthly",
        "best_choice": "Best choice",
        "unlimited": "Unlimited",
        "your_plan": "Your plan",
        "billed_year": "billed/year",
        "billed_month": "billed/month",
        "no_commitment": "No commitment",
        "save_up_to": "Save up to {amount}€/year",
        "max_quality": "max quality",
        "credits_per_month": "credits/month",
        "max_duration": "max duration/video",
        "feature_cut": "Video cutting",
        "feature_playlist": "Full playlists",
        "feature_support": "Priority support",
        "feature_no_ads": "No ads",
        "choose": "Choose",
        "current_plan_btn": "Current plan",
        "start_free": "Start free",
        "desc_free": "Try with no commitment. 3 videos per month.",
        "desc_starter": "Perfect for occasional HD use.",
        "desc_creator": "Best value for creators.",
        "desc_pro": "For professionals and heavy users.",
        "guarantee_1_title": "Money-back guarantee",
        "guarantee_1_text": "7 days to test. Cancel easily from your account.",
        "guarantee_2_title": "Instant downloads",
        "guarantee_2_text": "Fast servers and priority bandwidth on paid plans.",
        "guarantee_3_title": "Available worldwide",
        "guarantee_3_text": "Interface translated into 15 languages so anyone can download.",
        "nav_pricing": "Pricing",
        "nav_dashboard": "Dashboard",
        "nav_login": "Login",
        "nav_signup": "Try free",
        "nav_logout": "Logout",
        "footer_tagline": "The simple, fast and privacy-friendly video converter.",
        "footer_product": "Product",
        "footer_legal": "Legal",
        "footer_help": "Help",
        "about": "About",
        "contact": "Contact",
        "terms": "Terms",
        "privacy": "Privacy",
        "all_rights": "All rights reserved.",
        "hero_h1_part1": "Download any video as",
        "hero_sub": "Paste a YouTube, TikTok, Instagram or Facebook link. Choose format. It's downloaded. Simple, fast, ad-free.",
        "tab_video": "🎬 Video (MP4)",
        "tab_audio": "🎵 Audio (MP3)",
        "format_label": "Format",
        "playlist": "Full playlist",
        "paste": "Paste",
        "status_preparing": "Preparing...",
        "status_finalizing": "Finalizing...",
        "trust_1": "Instant download",
        "trust_2": "No files stored on our servers",
        "trust_3": "1000+ supported sites",
        "compatible": "Compatible with",
        "why_title": "Why",
        "why_sub": "The simplest and most powerful converter on the market.",
        "feature_mp3_title": "High Quality MP3",
        "feature_mp3_text": "Extract audio in MP3 128kbps or MP3 HD 320kbps. Perfect for music and podcasts.",
        "feature_mp4_title": "MP4 up to 4K",
        "feature_mp4_text": "Download in 720p, 1080p, 1440p or 4K UHD. Progressive streaming directly in your browser.",
        "feature_speed_title": "No Speed Limit",
        "feature_speed_text": "With a premium account, enjoy unlimited and prioritized downloads.",
        "feature_anon_title": "100% Anonymous",
        "feature_anon_text": "No files stored on our servers. Everything goes through your browser cache.",
        "feature_devices_title": "All Devices",
        "feature_devices_text": "Works on mobile, tablet and desktop. No app to install.",
        "feature_cut_title": "Video Cutting",
        "feature_cut_text": "Premium members: cut videos online and download only the part you want.",
        "teaser_title": "Unlock unlimited downloads",
        "teaser_text": "No ads, no limits, HD/4K. Cut videos in 1 click. Go Starter from 4.99€/month.",
        "teaser_price": "4.99€<span>/month</span>",
        "teaser_cta": "See plans →",
        "faq_title": "Frequently asked questions",
        "faq_q1": "Is Yestubers free?",
        "faq_a1": "Yes. Download 2 videos without signing up. Then create a free account for 3 credits per month.",
        "faq_q2": "What formats are available?",
        "faq_a2": "MP3, MP3 HD, M4A, WAV for audio. MP4, MP4 HD, MP4 2K, MP4 4K, 3GP, FLV for video.",
        "faq_q3": "Are my files stored?",
        "faq_a3": "No. We use progressive streaming: bytes go directly from our servers to your browser with no storage.",
        "faq_q4": "Can I download playlists?",
        "faq_a4": "Yes, check \"Full playlist\" and we will zip all videos for you.",
        "nav_features": "Features",
        "nav_about": "About",
        "signup": "Sign up",
        "cookie_text": "We only use essential cookies.",
    },
    "it": {
        "brand": "Yestubers",
        "title_home": "Free YouTube MP3/MP4 Downloader",
        "desc_home": "Yestubers is the fastest YouTube converter. Download any video as MP3, MP4, M4A or WAV with no signup.",
        "compare_title": "Free vs Creator",
        "feature": "Feature",
        "free": "Free",
        "yes": "Yes",
        "no": "No",
        "feature_quality": "Quality",
        "feature_ads": "Ads",
        "feature_credits": "Credits",
        "feature_cut": "Trim / Cut",
        "feature_playlist": "Playlists",
        "feature_support": "Priority support",
        "testimonials_title": "They use Yestubers",
        "cta_placeholder": "Paste YouTube link here...",
        "cta_button": "Download",
        "formats": "MP3, MP4, M4A, WAV",
        "free_badge": "100% free — 2 trials without account",
        "upgrade_badge": "Go Premium",
        "pricing_title": "Pricing",
        "pricing_desc": "Choose your Yestubers plan: free, Starter, Creator or Pro. Unlimited YouTube MP3/MP4 downloads, cutting, 4K.",
        "pricing_badge": "BEST SELLER",
        "pricing_h1": "Unlimited downloads from <span class=\"gradient-text\">€5.99/mo</span>",
        "pricing_sub": "Annual = 4 months free. Cancel anytime. 7-day money-back guarantee.",
        "annual": "Annual",
        "monthly": "Monthly",
        "best_choice": "Best choice",
        "unlimited": "Unlimited",
        "your_plan": "Your plan",
        "billed_year": "billed/year",
        "billed_month": "billed/month",
        "no_commitment": "No commitment",
        "save_up_to": "Save up to {amount}€/year",
        "max_quality": "max quality",
        "credits_per_month": "credits/month",
        "max_duration": "max duration/video",
        "feature_cut": "Video cutting",
        "feature_playlist": "Full playlists",
        "feature_support": "Priority support",
        "feature_no_ads": "No ads",
        "choose": "Choose",
        "current_plan_btn": "Current plan",
        "start_free": "Start free",
        "desc_free": "Try with no commitment. 3 videos per month.",
        "desc_starter": "Perfect for occasional HD use.",
        "desc_creator": "Best value for creators.",
        "desc_pro": "For professionals and heavy users.",
        "guarantee_1_title": "Money-back guarantee",
        "guarantee_1_text": "7 days to test. Cancel easily from your account.",
        "guarantee_2_title": "Instant downloads",
        "guarantee_2_text": "Fast servers and priority bandwidth on paid plans.",
        "guarantee_3_title": "Available worldwide",
        "guarantee_3_text": "Interface translated into 15 languages so anyone can download.",
        "nav_pricing": "Pricing",
        "nav_dashboard": "Dashboard",
        "nav_login": "Login",
        "nav_signup": "Try free",
        "nav_logout": "Logout",
        "footer_tagline": "The simple, fast and privacy-friendly video converter.",
        "footer_product": "Product",
        "footer_legal": "Legal",
        "footer_help": "Help",
        "about": "About",
        "contact": "Contact",
        "terms": "Terms",
        "privacy": "Privacy",
        "all_rights": "All rights reserved.",
        "hero_h1_part1": "Download any video as",
        "hero_sub": "Paste a YouTube, TikTok, Instagram or Facebook link. Choose format. It's downloaded. Simple, fast, ad-free.",
        "tab_video": "🎬 Video (MP4)",
        "tab_audio": "🎵 Audio (MP3)",
        "format_label": "Format",
        "playlist": "Full playlist",
        "paste": "Paste",
        "status_preparing": "Preparing...",
        "status_finalizing": "Finalizing...",
        "trust_1": "Instant download",
        "trust_2": "No files stored on our servers",
        "trust_3": "1000+ supported sites",
        "compatible": "Compatible with",
        "why_title": "Why",
        "why_sub": "The simplest and most powerful converter on the market.",
        "feature_mp3_title": "High Quality MP3",
        "feature_mp3_text": "Extract audio in MP3 128kbps or MP3 HD 320kbps. Perfect for music and podcasts.",
        "feature_mp4_title": "MP4 up to 4K",
        "feature_mp4_text": "Download in 720p, 1080p, 1440p or 4K UHD. Progressive streaming directly in your browser.",
        "feature_speed_title": "No Speed Limit",
        "feature_speed_text": "With a premium account, enjoy unlimited and prioritized downloads.",
        "feature_anon_title": "100% Anonymous",
        "feature_anon_text": "No files stored on our servers. Everything goes through your browser cache.",
        "feature_devices_title": "All Devices",
        "feature_devices_text": "Works on mobile, tablet and desktop. No app to install.",
        "feature_cut_title": "Video Cutting",
        "feature_cut_text": "Premium members: cut videos online and download only the part you want.",
        "teaser_title": "Unlock unlimited downloads",
        "teaser_text": "No ads, no limits, HD/4K. Cut videos in 1 click. Go Starter from 4.99€/month.",
        "teaser_price": "4.99€<span>/month</span>",
        "teaser_cta": "See plans →",
        "faq_title": "Frequently asked questions",
        "faq_q1": "Is Yestubers free?",
        "faq_a1": "Yes. Download 2 videos without signing up. Then create a free account for 3 credits per month.",
        "faq_q2": "What formats are available?",
        "faq_a2": "MP3, MP3 HD, M4A, WAV for audio. MP4, MP4 HD, MP4 2K, MP4 4K, 3GP, FLV for video.",
        "faq_q3": "Are my files stored?",
        "faq_a3": "No. We use progressive streaming: bytes go directly from our servers to your browser with no storage.",
        "faq_q4": "Can I download playlists?",
        "faq_a4": "Yes, check \"Full playlist\" and we will zip all videos for you.",
        "nav_features": "Features",
        "nav_about": "About",
        "signup": "Sign up",
        "cookie_text": "We only use essential cookies.",
    },
    "pt": {
        "brand": "Yestubers",
        "title_home": "Free YouTube MP3/MP4 Downloader",
        "desc_home": "Yestubers is the fastest YouTube converter. Download any video as MP3, MP4, M4A or WAV with no signup.",
        "compare_title": "Free vs Creator",
        "feature": "Feature",
        "free": "Free",
        "yes": "Yes",
        "no": "No",
        "feature_quality": "Quality",
        "feature_ads": "Ads",
        "feature_credits": "Credits",
        "feature_cut": "Trim / Cut",
        "feature_playlist": "Playlists",
        "feature_support": "Priority support",
        "testimonials_title": "They use Yestubers",
        "cta_placeholder": "Paste YouTube link here...",
        "cta_button": "Download",
        "formats": "MP3, MP4, M4A, WAV",
        "free_badge": "100% free — 2 trials without account",
        "upgrade_badge": "Go Premium",
        "pricing_title": "Pricing",
        "pricing_desc": "Choose your Yestubers plan: free, Starter, Creator or Pro. Unlimited YouTube MP3/MP4 downloads, cutting, 4K.",
        "pricing_badge": "BEST SELLER",
        "pricing_h1": "Unlimited downloads from <span class=\"gradient-text\">€5.99/mo</span>",
        "pricing_sub": "Annual = 4 months free. Cancel anytime. 7-day money-back guarantee.",
        "annual": "Annual",
        "monthly": "Monthly",
        "best_choice": "Best choice",
        "unlimited": "Unlimited",
        "your_plan": "Your plan",
        "billed_year": "billed/year",
        "billed_month": "billed/month",
        "no_commitment": "No commitment",
        "save_up_to": "Save up to {amount}€/year",
        "max_quality": "max quality",
        "credits_per_month": "credits/month",
        "max_duration": "max duration/video",
        "feature_cut": "Video cutting",
        "feature_playlist": "Full playlists",
        "feature_support": "Priority support",
        "feature_no_ads": "No ads",
        "choose": "Choose",
        "current_plan_btn": "Current plan",
        "start_free": "Start free",
        "desc_free": "Try with no commitment. 3 videos per month.",
        "desc_starter": "Perfect for occasional HD use.",
        "desc_creator": "Best value for creators.",
        "desc_pro": "For professionals and heavy users.",
        "guarantee_1_title": "Money-back guarantee",
        "guarantee_1_text": "7 days to test. Cancel easily from your account.",
        "guarantee_2_title": "Instant downloads",
        "guarantee_2_text": "Fast servers and priority bandwidth on paid plans.",
        "guarantee_3_title": "Available worldwide",
        "guarantee_3_text": "Interface translated into 15 languages so anyone can download.",
        "nav_pricing": "Pricing",
        "nav_dashboard": "Dashboard",
        "nav_login": "Login",
        "nav_signup": "Try free",
        "nav_logout": "Logout",
        "footer_tagline": "The simple, fast and privacy-friendly video converter.",
        "footer_product": "Product",
        "footer_legal": "Legal",
        "footer_help": "Help",
        "about": "About",
        "contact": "Contact",
        "terms": "Terms",
        "privacy": "Privacy",
        "all_rights": "All rights reserved.",
        "hero_h1_part1": "Download any video as",
        "hero_sub": "Paste a YouTube, TikTok, Instagram or Facebook link. Choose format. It's downloaded. Simple, fast, ad-free.",
        "tab_video": "🎬 Video (MP4)",
        "tab_audio": "🎵 Audio (MP3)",
        "format_label": "Format",
        "playlist": "Full playlist",
        "paste": "Paste",
        "status_preparing": "Preparing...",
        "status_finalizing": "Finalizing...",
        "trust_1": "Instant download",
        "trust_2": "No files stored on our servers",
        "trust_3": "1000+ supported sites",
        "compatible": "Compatible with",
        "why_title": "Why",
        "why_sub": "The simplest and most powerful converter on the market.",
        "feature_mp3_title": "High Quality MP3",
        "feature_mp3_text": "Extract audio in MP3 128kbps or MP3 HD 320kbps. Perfect for music and podcasts.",
        "feature_mp4_title": "MP4 up to 4K",
        "feature_mp4_text": "Download in 720p, 1080p, 1440p or 4K UHD. Progressive streaming directly in your browser.",
        "feature_speed_title": "No Speed Limit",
        "feature_speed_text": "With a premium account, enjoy unlimited and prioritized downloads.",
        "feature_anon_title": "100% Anonymous",
        "feature_anon_text": "No files stored on our servers. Everything goes through your browser cache.",
        "feature_devices_title": "All Devices",
        "feature_devices_text": "Works on mobile, tablet and desktop. No app to install.",
        "feature_cut_title": "Video Cutting",
        "feature_cut_text": "Premium members: cut videos online and download only the part you want.",
        "teaser_title": "Unlock unlimited downloads",
        "teaser_text": "No ads, no limits, HD/4K. Cut videos in 1 click. Go Starter from 4.99€/month.",
        "teaser_price": "4.99€<span>/month</span>",
        "teaser_cta": "See plans →",
        "faq_title": "Frequently asked questions",
        "faq_q1": "Is Yestubers free?",
        "faq_a1": "Yes. Download 2 videos without signing up. Then create a free account for 3 credits per month.",
        "faq_q2": "What formats are available?",
        "faq_a2": "MP3, MP3 HD, M4A, WAV for audio. MP4, MP4 HD, MP4 2K, MP4 4K, 3GP, FLV for video.",
        "faq_q3": "Are my files stored?",
        "faq_a3": "No. We use progressive streaming: bytes go directly from our servers to your browser with no storage.",
        "faq_q4": "Can I download playlists?",
        "faq_a4": "Yes, check \"Full playlist\" and we will zip all videos for you.",
        "nav_features": "Features",
        "nav_about": "About",
        "signup": "Sign up",
        "cookie_text": "We only use essential cookies.",
    },
    "nl": {
        "brand": "Yestubers",
        "title_home": "Free YouTube MP3/MP4 Downloader",
        "desc_home": "Yestubers is the fastest YouTube converter. Download any video as MP3, MP4, M4A or WAV with no signup.",
        "compare_title": "Free vs Creator",
        "feature": "Feature",
        "free": "Free",
        "yes": "Yes",
        "no": "No",
        "feature_quality": "Quality",
        "feature_ads": "Ads",
        "feature_credits": "Credits",
        "feature_cut": "Trim / Cut",
        "feature_playlist": "Playlists",
        "feature_support": "Priority support",
        "testimonials_title": "They use Yestubers",
        "cta_placeholder": "Paste YouTube link here...",
        "cta_button": "Download",
        "formats": "MP3, MP4, M4A, WAV",
        "free_badge": "100% free — 2 trials without account",
        "upgrade_badge": "Go Premium",
        "pricing_title": "Pricing",
        "pricing_desc": "Choose your Yestubers plan: free, Starter, Creator or Pro. Unlimited YouTube MP3/MP4 downloads, cutting, 4K.",
        "pricing_badge": "BEST SELLER",
        "pricing_h1": "Unlimited downloads from <span class=\"gradient-text\">€5.99/mo</span>",
        "pricing_sub": "Annual = 4 months free. Cancel anytime. 7-day money-back guarantee.",
        "annual": "Annual",
        "monthly": "Monthly",
        "best_choice": "Best choice",
        "unlimited": "Unlimited",
        "your_plan": "Your plan",
        "billed_year": "billed/year",
        "billed_month": "billed/month",
        "no_commitment": "No commitment",
        "save_up_to": "Save up to {amount}€/year",
        "max_quality": "max quality",
        "credits_per_month": "credits/month",
        "max_duration": "max duration/video",
        "feature_cut": "Video cutting",
        "feature_playlist": "Full playlists",
        "feature_support": "Priority support",
        "feature_no_ads": "No ads",
        "choose": "Choose",
        "current_plan_btn": "Current plan",
        "start_free": "Start free",
        "desc_free": "Try with no commitment. 3 videos per month.",
        "desc_starter": "Perfect for occasional HD use.",
        "desc_creator": "Best value for creators.",
        "desc_pro": "For professionals and heavy users.",
        "guarantee_1_title": "Money-back guarantee",
        "guarantee_1_text": "7 days to test. Cancel easily from your account.",
        "guarantee_2_title": "Instant downloads",
        "guarantee_2_text": "Fast servers and priority bandwidth on paid plans.",
        "guarantee_3_title": "Available worldwide",
        "guarantee_3_text": "Interface translated into 15 languages so anyone can download.",
        "nav_pricing": "Pricing",
        "nav_dashboard": "Dashboard",
        "nav_login": "Login",
        "nav_signup": "Try free",
        "nav_logout": "Logout",
        "footer_tagline": "The simple, fast and privacy-friendly video converter.",
        "footer_product": "Product",
        "footer_legal": "Legal",
        "footer_help": "Help",
        "about": "About",
        "contact": "Contact",
        "terms": "Terms",
        "privacy": "Privacy",
        "all_rights": "All rights reserved.",
        "hero_h1_part1": "Download any video as",
        "hero_sub": "Paste a YouTube, TikTok, Instagram or Facebook link. Choose format. It's downloaded. Simple, fast, ad-free.",
        "tab_video": "🎬 Video (MP4)",
        "tab_audio": "🎵 Audio (MP3)",
        "format_label": "Format",
        "playlist": "Full playlist",
        "paste": "Paste",
        "status_preparing": "Preparing...",
        "status_finalizing": "Finalizing...",
        "trust_1": "Instant download",
        "trust_2": "No files stored on our servers",
        "trust_3": "1000+ supported sites",
        "compatible": "Compatible with",
        "why_title": "Why",
        "why_sub": "The simplest and most powerful converter on the market.",
        "feature_mp3_title": "High Quality MP3",
        "feature_mp3_text": "Extract audio in MP3 128kbps or MP3 HD 320kbps. Perfect for music and podcasts.",
        "feature_mp4_title": "MP4 up to 4K",
        "feature_mp4_text": "Download in 720p, 1080p, 1440p or 4K UHD. Progressive streaming directly in your browser.",
        "feature_speed_title": "No Speed Limit",
        "feature_speed_text": "With a premium account, enjoy unlimited and prioritized downloads.",
        "feature_anon_title": "100% Anonymous",
        "feature_anon_text": "No files stored on our servers. Everything goes through your browser cache.",
        "feature_devices_title": "All Devices",
        "feature_devices_text": "Works on mobile, tablet and desktop. No app to install.",
        "feature_cut_title": "Video Cutting",
        "feature_cut_text": "Premium members: cut videos online and download only the part you want.",
        "teaser_title": "Unlock unlimited downloads",
        "teaser_text": "No ads, no limits, HD/4K. Cut videos in 1 click. Go Starter from 4.99€/month.",
        "teaser_price": "4.99€<span>/month</span>",
        "teaser_cta": "See plans →",
        "faq_title": "Frequently asked questions",
        "faq_q1": "Is Yestubers free?",
        "faq_a1": "Yes. Download 2 videos without signing up. Then create a free account for 3 credits per month.",
        "faq_q2": "What formats are available?",
        "faq_a2": "MP3, MP3 HD, M4A, WAV for audio. MP4, MP4 HD, MP4 2K, MP4 4K, 3GP, FLV for video.",
        "faq_q3": "Are my files stored?",
        "faq_a3": "No. We use progressive streaming: bytes go directly from our servers to your browser with no storage.",
        "faq_q4": "Can I download playlists?",
        "faq_a4": "Yes, check \"Full playlist\" and we will zip all videos for you.",
        "nav_features": "Features",
        "nav_about": "About",
        "signup": "Sign up",
        "cookie_text": "We only use essential cookies.",
    },
    "pl": {
        "brand": "Yestubers",
        "title_home": "Free YouTube MP3/MP4 Downloader",
        "desc_home": "Yestubers is the fastest YouTube converter. Download any video as MP3, MP4, M4A or WAV with no signup.",
        "compare_title": "Free vs Creator",
        "feature": "Feature",
        "free": "Free",
        "yes": "Yes",
        "no": "No",
        "feature_quality": "Quality",
        "feature_ads": "Ads",
        "feature_credits": "Credits",
        "feature_cut": "Trim / Cut",
        "feature_playlist": "Playlists",
        "feature_support": "Priority support",
        "testimonials_title": "They use Yestubers",
        "cta_placeholder": "Paste YouTube link here...",
        "cta_button": "Download",
        "formats": "MP3, MP4, M4A, WAV",
        "free_badge": "100% free — 2 trials without account",
        "upgrade_badge": "Go Premium",
        "pricing_title": "Pricing",
        "pricing_desc": "Choose your Yestubers plan: free, Starter, Creator or Pro. Unlimited YouTube MP3/MP4 downloads, cutting, 4K.",
        "pricing_badge": "BEST SELLER",
        "pricing_h1": "Unlimited downloads from <span class=\"gradient-text\">€5.99/mo</span>",
        "pricing_sub": "Annual = 4 months free. Cancel anytime. 7-day money-back guarantee.",
        "annual": "Annual",
        "monthly": "Monthly",
        "best_choice": "Best choice",
        "unlimited": "Unlimited",
        "your_plan": "Your plan",
        "billed_year": "billed/year",
        "billed_month": "billed/month",
        "no_commitment": "No commitment",
        "save_up_to": "Save up to {amount}€/year",
        "max_quality": "max quality",
        "credits_per_month": "credits/month",
        "max_duration": "max duration/video",
        "feature_cut": "Video cutting",
        "feature_playlist": "Full playlists",
        "feature_support": "Priority support",
        "feature_no_ads": "No ads",
        "choose": "Choose",
        "current_plan_btn": "Current plan",
        "start_free": "Start free",
        "desc_free": "Try with no commitment. 3 videos per month.",
        "desc_starter": "Perfect for occasional HD use.",
        "desc_creator": "Best value for creators.",
        "desc_pro": "For professionals and heavy users.",
        "guarantee_1_title": "Money-back guarantee",
        "guarantee_1_text": "7 days to test. Cancel easily from your account.",
        "guarantee_2_title": "Instant downloads",
        "guarantee_2_text": "Fast servers and priority bandwidth on paid plans.",
        "guarantee_3_title": "Available worldwide",
        "guarantee_3_text": "Interface translated into 15 languages so anyone can download.",
        "nav_pricing": "Pricing",
        "nav_dashboard": "Dashboard",
        "nav_login": "Login",
        "nav_signup": "Try free",
        "nav_logout": "Logout",
        "footer_tagline": "The simple, fast and privacy-friendly video converter.",
        "footer_product": "Product",
        "footer_legal": "Legal",
        "footer_help": "Help",
        "about": "About",
        "contact": "Contact",
        "terms": "Terms",
        "privacy": "Privacy",
        "all_rights": "All rights reserved.",
        "hero_h1_part1": "Download any video as",
        "hero_sub": "Paste a YouTube, TikTok, Instagram or Facebook link. Choose format. It's downloaded. Simple, fast, ad-free.",
        "tab_video": "🎬 Video (MP4)",
        "tab_audio": "🎵 Audio (MP3)",
        "format_label": "Format",
        "playlist": "Full playlist",
        "paste": "Paste",
        "status_preparing": "Preparing...",
        "status_finalizing": "Finalizing...",
        "trust_1": "Instant download",
        "trust_2": "No files stored on our servers",
        "trust_3": "1000+ supported sites",
        "compatible": "Compatible with",
        "why_title": "Why",
        "why_sub": "The simplest and most powerful converter on the market.",
        "feature_mp3_title": "High Quality MP3",
        "feature_mp3_text": "Extract audio in MP3 128kbps or MP3 HD 320kbps. Perfect for music and podcasts.",
        "feature_mp4_title": "MP4 up to 4K",
        "feature_mp4_text": "Download in 720p, 1080p, 1440p or 4K UHD. Progressive streaming directly in your browser.",
        "feature_speed_title": "No Speed Limit",
        "feature_speed_text": "With a premium account, enjoy unlimited and prioritized downloads.",
        "feature_anon_title": "100% Anonymous",
        "feature_anon_text": "No files stored on our servers. Everything goes through your browser cache.",
        "feature_devices_title": "All Devices",
        "feature_devices_text": "Works on mobile, tablet and desktop. No app to install.",
        "feature_cut_title": "Video Cutting",
        "feature_cut_text": "Premium members: cut videos online and download only the part you want.",
        "teaser_title": "Unlock unlimited downloads",
        "teaser_text": "No ads, no limits, HD/4K. Cut videos in 1 click. Go Starter from 4.99€/month.",
        "teaser_price": "4.99€<span>/month</span>",
        "teaser_cta": "See plans →",
        "faq_title": "Frequently asked questions",
        "faq_q1": "Is Yestubers free?",
        "faq_a1": "Yes. Download 2 videos without signing up. Then create a free account for 3 credits per month.",
        "faq_q2": "What formats are available?",
        "faq_a2": "MP3, MP3 HD, M4A, WAV for audio. MP4, MP4 HD, MP4 2K, MP4 4K, 3GP, FLV for video.",
        "faq_q3": "Are my files stored?",
        "faq_a3": "No. We use progressive streaming: bytes go directly from our servers to your browser with no storage.",
        "faq_q4": "Can I download playlists?",
        "faq_a4": "Yes, check \"Full playlist\" and we will zip all videos for you.",
        "nav_features": "Features",
        "nav_about": "About",
        "signup": "Sign up",
        "cookie_text": "We only use essential cookies.",
    },
    "ar": {
        "brand": "Yestubers",
        "title_home": "Free YouTube MP3/MP4 Downloader",
        "desc_home": "Yestubers is the fastest YouTube converter. Download any video as MP3, MP4, M4A or WAV with no signup.",
        "compare_title": "Free vs Creator",
        "feature": "Feature",
        "free": "Free",
        "yes": "Yes",
        "no": "No",
        "feature_quality": "Quality",
        "feature_ads": "Ads",
        "feature_credits": "Credits",
        "feature_cut": "Trim / Cut",
        "feature_playlist": "Playlists",
        "feature_support": "Priority support",
        "testimonials_title": "They use Yestubers",
        "cta_placeholder": "Paste YouTube link here...",
        "cta_button": "Download",
        "formats": "MP3, MP4, M4A, WAV",
        "free_badge": "100% free — 2 trials without account",
        "upgrade_badge": "Go Premium",
        "pricing_title": "Pricing",
        "pricing_desc": "Choose your Yestubers plan: free, Starter, Creator or Pro. Unlimited YouTube MP3/MP4 downloads, cutting, 4K.",
        "pricing_badge": "BEST SELLER",
        "pricing_h1": "Unlimited downloads from <span class=\"gradient-text\">€5.99/mo</span>",
        "pricing_sub": "Annual = 4 months free. Cancel anytime. 7-day money-back guarantee.",
        "annual": "Annual",
        "monthly": "Monthly",
        "best_choice": "Best choice",
        "unlimited": "Unlimited",
        "your_plan": "Your plan",
        "billed_year": "billed/year",
        "billed_month": "billed/month",
        "no_commitment": "No commitment",
        "save_up_to": "Save up to {amount}€/year",
        "max_quality": "max quality",
        "credits_per_month": "credits/month",
        "max_duration": "max duration/video",
        "feature_cut": "Video cutting",
        "feature_playlist": "Full playlists",
        "feature_support": "Priority support",
        "feature_no_ads": "No ads",
        "choose": "Choose",
        "current_plan_btn": "Current plan",
        "start_free": "Start free",
        "desc_free": "Try with no commitment. 3 videos per month.",
        "desc_starter": "Perfect for occasional HD use.",
        "desc_creator": "Best value for creators.",
        "desc_pro": "For professionals and heavy users.",
        "guarantee_1_title": "Money-back guarantee",
        "guarantee_1_text": "7 days to test. Cancel easily from your account.",
        "guarantee_2_title": "Instant downloads",
        "guarantee_2_text": "Fast servers and priority bandwidth on paid plans.",
        "guarantee_3_title": "Available worldwide",
        "guarantee_3_text": "Interface translated into 15 languages so anyone can download.",
        "nav_pricing": "Pricing",
        "nav_dashboard": "Dashboard",
        "nav_login": "Login",
        "nav_signup": "Try free",
        "nav_logout": "Logout",
        "footer_tagline": "The simple, fast and privacy-friendly video converter.",
        "footer_product": "Product",
        "footer_legal": "Legal",
        "footer_help": "Help",
        "about": "About",
        "contact": "Contact",
        "terms": "Terms",
        "privacy": "Privacy",
        "all_rights": "All rights reserved.",
        "hero_h1_part1": "Download any video as",
        "hero_sub": "Paste a YouTube, TikTok, Instagram or Facebook link. Choose format. It's downloaded. Simple, fast, ad-free.",
        "tab_video": "🎬 Video (MP4)",
        "tab_audio": "🎵 Audio (MP3)",
        "format_label": "Format",
        "playlist": "Full playlist",
        "paste": "Paste",
        "status_preparing": "Preparing...",
        "status_finalizing": "Finalizing...",
        "trust_1": "Instant download",
        "trust_2": "No files stored on our servers",
        "trust_3": "1000+ supported sites",
        "compatible": "Compatible with",
        "why_title": "Why",
        "why_sub": "The simplest and most powerful converter on the market.",
        "feature_mp3_title": "High Quality MP3",
        "feature_mp3_text": "Extract audio in MP3 128kbps or MP3 HD 320kbps. Perfect for music and podcasts.",
        "feature_mp4_title": "MP4 up to 4K",
        "feature_mp4_text": "Download in 720p, 1080p, 1440p or 4K UHD. Progressive streaming directly in your browser.",
        "feature_speed_title": "No Speed Limit",
        "feature_speed_text": "With a premium account, enjoy unlimited and prioritized downloads.",
        "feature_anon_title": "100% Anonymous",
        "feature_anon_text": "No files stored on our servers. Everything goes through your browser cache.",
        "feature_devices_title": "All Devices",
        "feature_devices_text": "Works on mobile, tablet and desktop. No app to install.",
        "feature_cut_title": "Video Cutting",
        "feature_cut_text": "Premium members: cut videos online and download only the part you want.",
        "teaser_title": "Unlock unlimited downloads",
        "teaser_text": "No ads, no limits, HD/4K. Cut videos in 1 click. Go Starter from 4.99€/month.",
        "teaser_price": "4.99€<span>/month</span>",
        "teaser_cta": "See plans →",
        "faq_title": "Frequently asked questions",
        "faq_q1": "Is Yestubers free?",
        "faq_a1": "Yes. Download 2 videos without signing up. Then create a free account for 3 credits per month.",
        "faq_q2": "What formats are available?",
        "faq_a2": "MP3, MP3 HD, M4A, WAV for audio. MP4, MP4 HD, MP4 2K, MP4 4K, 3GP, FLV for video.",
        "faq_q3": "Are my files stored?",
        "faq_a3": "No. We use progressive streaming: bytes go directly from our servers to your browser with no storage.",
        "faq_q4": "Can I download playlists?",
        "faq_a4": "Yes, check \"Full playlist\" and we will zip all videos for you.",
        "nav_features": "Features",
        "nav_about": "About",
        "signup": "Sign up",
        "cookie_text": "We only use essential cookies.",
    },
    "hi": {
        "brand": "Yestubers",
        "title_home": "Free YouTube MP3/MP4 Downloader",
        "desc_home": "Yestubers is the fastest YouTube converter. Download any video as MP3, MP4, M4A or WAV with no signup.",
        "compare_title": "Free vs Creator",
        "feature": "Feature",
        "free": "Free",
        "yes": "Yes",
        "no": "No",
        "feature_quality": "Quality",
        "feature_ads": "Ads",
        "feature_credits": "Credits",
        "feature_cut": "Trim / Cut",
        "feature_playlist": "Playlists",
        "feature_support": "Priority support",
        "testimonials_title": "They use Yestubers",
        "cta_placeholder": "Paste YouTube link here...",
        "cta_button": "Download",
        "formats": "MP3, MP4, M4A, WAV",
        "free_badge": "100% free — 2 trials without account",
        "upgrade_badge": "Go Premium",
        "pricing_title": "Pricing",
        "pricing_desc": "Choose your Yestubers plan: free, Starter, Creator or Pro. Unlimited YouTube MP3/MP4 downloads, cutting, 4K.",
        "pricing_badge": "BEST SELLER",
        "pricing_h1": "Unlimited downloads from <span class=\"gradient-text\">€5.99/mo</span>",
        "pricing_sub": "Annual = 4 months free. Cancel anytime. 7-day money-back guarantee.",
        "annual": "Annual",
        "monthly": "Monthly",
        "best_choice": "Best choice",
        "unlimited": "Unlimited",
        "your_plan": "Your plan",
        "billed_year": "billed/year",
        "billed_month": "billed/month",
        "no_commitment": "No commitment",
        "save_up_to": "Save up to {amount}€/year",
        "max_quality": "max quality",
        "credits_per_month": "credits/month",
        "max_duration": "max duration/video",
        "feature_cut": "Video cutting",
        "feature_playlist": "Full playlists",
        "feature_support": "Priority support",
        "feature_no_ads": "No ads",
        "choose": "Choose",
        "current_plan_btn": "Current plan",
        "start_free": "Start free",
        "desc_free": "Try with no commitment. 3 videos per month.",
        "desc_starter": "Perfect for occasional HD use.",
        "desc_creator": "Best value for creators.",
        "desc_pro": "For professionals and heavy users.",
        "guarantee_1_title": "Money-back guarantee",
        "guarantee_1_text": "7 days to test. Cancel easily from your account.",
        "guarantee_2_title": "Instant downloads",
        "guarantee_2_text": "Fast servers and priority bandwidth on paid plans.",
        "guarantee_3_title": "Available worldwide",
        "guarantee_3_text": "Interface translated into 15 languages so anyone can download.",
        "nav_pricing": "Pricing",
        "nav_dashboard": "Dashboard",
        "nav_login": "Login",
        "nav_signup": "Try free",
        "nav_logout": "Logout",
        "footer_tagline": "The simple, fast and privacy-friendly video converter.",
        "footer_product": "Product",
        "footer_legal": "Legal",
        "footer_help": "Help",
        "about": "About",
        "contact": "Contact",
        "terms": "Terms",
        "privacy": "Privacy",
        "all_rights": "All rights reserved.",
        "hero_h1_part1": "Download any video as",
        "hero_sub": "Paste a YouTube, TikTok, Instagram or Facebook link. Choose format. It's downloaded. Simple, fast, ad-free.",
        "tab_video": "🎬 Video (MP4)",
        "tab_audio": "🎵 Audio (MP3)",
        "format_label": "Format",
        "playlist": "Full playlist",
        "paste": "Paste",
        "status_preparing": "Preparing...",
        "status_finalizing": "Finalizing...",
        "trust_1": "Instant download",
        "trust_2": "No files stored on our servers",
        "trust_3": "1000+ supported sites",
        "compatible": "Compatible with",
        "why_title": "Why",
        "why_sub": "The simplest and most powerful converter on the market.",
        "feature_mp3_title": "High Quality MP3",
        "feature_mp3_text": "Extract audio in MP3 128kbps or MP3 HD 320kbps. Perfect for music and podcasts.",
        "feature_mp4_title": "MP4 up to 4K",
        "feature_mp4_text": "Download in 720p, 1080p, 1440p or 4K UHD. Progressive streaming directly in your browser.",
        "feature_speed_title": "No Speed Limit",
        "feature_speed_text": "With a premium account, enjoy unlimited and prioritized downloads.",
        "feature_anon_title": "100% Anonymous",
        "feature_anon_text": "No files stored on our servers. Everything goes through your browser cache.",
        "feature_devices_title": "All Devices",
        "feature_devices_text": "Works on mobile, tablet and desktop. No app to install.",
        "feature_cut_title": "Video Cutting",
        "feature_cut_text": "Premium members: cut videos online and download only the part you want.",
        "teaser_title": "Unlock unlimited downloads",
        "teaser_text": "No ads, no limits, HD/4K. Cut videos in 1 click. Go Starter from 4.99€/month.",
        "teaser_price": "4.99€<span>/month</span>",
        "teaser_cta": "See plans →",
        "faq_title": "Frequently asked questions",
        "faq_q1": "Is Yestubers free?",
        "faq_a1": "Yes. Download 2 videos without signing up. Then create a free account for 3 credits per month.",
        "faq_q2": "What formats are available?",
        "faq_a2": "MP3, MP3 HD, M4A, WAV for audio. MP4, MP4 HD, MP4 2K, MP4 4K, 3GP, FLV for video.",
        "faq_q3": "Are my files stored?",
        "faq_a3": "No. We use progressive streaming: bytes go directly from our servers to your browser with no storage.",
        "faq_q4": "Can I download playlists?",
        "faq_a4": "Yes, check \"Full playlist\" and we will zip all videos for you.",
        "nav_features": "Features",
        "nav_about": "About",
        "signup": "Sign up",
        "cookie_text": "We only use essential cookies.",
    },
    "ja": {
        "brand": "Yestubers",
        "title_home": "Free YouTube MP3/MP4 Downloader",
        "desc_home": "Yestubers is the fastest YouTube converter. Download any video as MP3, MP4, M4A or WAV with no signup.",
        "compare_title": "Free vs Creator",
        "feature": "Feature",
        "free": "Free",
        "yes": "Yes",
        "no": "No",
        "feature_quality": "Quality",
        "feature_ads": "Ads",
        "feature_credits": "Credits",
        "feature_cut": "Trim / Cut",
        "feature_playlist": "Playlists",
        "feature_support": "Priority support",
        "testimonials_title": "They use Yestubers",
        "cta_placeholder": "Paste YouTube link here...",
        "cta_button": "Download",
        "formats": "MP3, MP4, M4A, WAV",
        "free_badge": "100% free — 2 trials without account",
        "upgrade_badge": "Go Premium",
        "pricing_title": "Pricing",
        "pricing_desc": "Choose your Yestubers plan: free, Starter, Creator or Pro. Unlimited YouTube MP3/MP4 downloads, cutting, 4K.",
        "pricing_badge": "BEST SELLER",
        "pricing_h1": "Unlimited downloads from <span class=\"gradient-text\">€5.99/mo</span>",
        "pricing_sub": "Annual = 4 months free. Cancel anytime. 7-day money-back guarantee.",
        "annual": "Annual",
        "monthly": "Monthly",
        "best_choice": "Best choice",
        "unlimited": "Unlimited",
        "your_plan": "Your plan",
        "billed_year": "billed/year",
        "billed_month": "billed/month",
        "no_commitment": "No commitment",
        "save_up_to": "Save up to {amount}€/year",
        "max_quality": "max quality",
        "credits_per_month": "credits/month",
        "max_duration": "max duration/video",
        "feature_cut": "Video cutting",
        "feature_playlist": "Full playlists",
        "feature_support": "Priority support",
        "feature_no_ads": "No ads",
        "choose": "Choose",
        "current_plan_btn": "Current plan",
        "start_free": "Start free",
        "desc_free": "Try with no commitment. 3 videos per month.",
        "desc_starter": "Perfect for occasional HD use.",
        "desc_creator": "Best value for creators.",
        "desc_pro": "For professionals and heavy users.",
        "guarantee_1_title": "Money-back guarantee",
        "guarantee_1_text": "7 days to test. Cancel easily from your account.",
        "guarantee_2_title": "Instant downloads",
        "guarantee_2_text": "Fast servers and priority bandwidth on paid plans.",
        "guarantee_3_title": "Available worldwide",
        "guarantee_3_text": "Interface translated into 15 languages so anyone can download.",
        "nav_pricing": "Pricing",
        "nav_dashboard": "Dashboard",
        "nav_login": "Login",
        "nav_signup": "Try free",
        "nav_logout": "Logout",
        "footer_tagline": "The simple, fast and privacy-friendly video converter.",
        "footer_product": "Product",
        "footer_legal": "Legal",
        "footer_help": "Help",
        "about": "About",
        "contact": "Contact",
        "terms": "Terms",
        "privacy": "Privacy",
        "all_rights": "All rights reserved.",
        "hero_h1_part1": "Download any video as",
        "hero_sub": "Paste a YouTube, TikTok, Instagram or Facebook link. Choose format. It's downloaded. Simple, fast, ad-free.",
        "tab_video": "🎬 Video (MP4)",
        "tab_audio": "🎵 Audio (MP3)",
        "format_label": "Format",
        "playlist": "Full playlist",
        "paste": "Paste",
        "status_preparing": "Preparing...",
        "status_finalizing": "Finalizing...",
        "trust_1": "Instant download",
        "trust_2": "No files stored on our servers",
        "trust_3": "1000+ supported sites",
        "compatible": "Compatible with",
        "why_title": "Why",
        "why_sub": "The simplest and most powerful converter on the market.",
        "feature_mp3_title": "High Quality MP3",
        "feature_mp3_text": "Extract audio in MP3 128kbps or MP3 HD 320kbps. Perfect for music and podcasts.",
        "feature_mp4_title": "MP4 up to 4K",
        "feature_mp4_text": "Download in 720p, 1080p, 1440p or 4K UHD. Progressive streaming directly in your browser.",
        "feature_speed_title": "No Speed Limit",
        "feature_speed_text": "With a premium account, enjoy unlimited and prioritized downloads.",
        "feature_anon_title": "100% Anonymous",
        "feature_anon_text": "No files stored on our servers. Everything goes through your browser cache.",
        "feature_devices_title": "All Devices",
        "feature_devices_text": "Works on mobile, tablet and desktop. No app to install.",
        "feature_cut_title": "Video Cutting",
        "feature_cut_text": "Premium members: cut videos online and download only the part you want.",
        "teaser_title": "Unlock unlimited downloads",
        "teaser_text": "No ads, no limits, HD/4K. Cut videos in 1 click. Go Starter from 4.99€/month.",
        "teaser_price": "4.99€<span>/month</span>",
        "teaser_cta": "See plans →",
        "faq_title": "Frequently asked questions",
        "faq_q1": "Is Yestubers free?",
        "faq_a1": "Yes. Download 2 videos without signing up. Then create a free account for 3 credits per month.",
        "faq_q2": "What formats are available?",
        "faq_a2": "MP3, MP3 HD, M4A, WAV for audio. MP4, MP4 HD, MP4 2K, MP4 4K, 3GP, FLV for video.",
        "faq_q3": "Are my files stored?",
        "faq_a3": "No. We use progressive streaming: bytes go directly from our servers to your browser with no storage.",
        "faq_q4": "Can I download playlists?",
        "faq_a4": "Yes, check \"Full playlist\" and we will zip all videos for you.",
        "nav_features": "Features",
        "nav_about": "About",
        "signup": "Sign up",
        "cookie_text": "We only use essential cookies.",
    },
    "ko": {
        "brand": "Yestubers",
        "title_home": "Free YouTube MP3/MP4 Downloader",
        "desc_home": "Yestubers is the fastest YouTube converter. Download any video as MP3, MP4, M4A or WAV with no signup.",
        "compare_title": "Free vs Creator",
        "feature": "Feature",
        "free": "Free",
        "yes": "Yes",
        "no": "No",
        "feature_quality": "Quality",
        "feature_ads": "Ads",
        "feature_credits": "Credits",
        "feature_cut": "Trim / Cut",
        "feature_playlist": "Playlists",
        "feature_support": "Priority support",
        "testimonials_title": "They use Yestubers",
        "cta_placeholder": "Paste YouTube link here...",
        "cta_button": "Download",
        "formats": "MP3, MP4, M4A, WAV",
        "free_badge": "100% free — 2 trials without account",
        "upgrade_badge": "Go Premium",
        "pricing_title": "Pricing",
        "pricing_desc": "Choose your Yestubers plan: free, Starter, Creator or Pro. Unlimited YouTube MP3/MP4 downloads, cutting, 4K.",
        "pricing_badge": "BEST SELLER",
        "pricing_h1": "Unlimited downloads from <span class=\"gradient-text\">€5.99/mo</span>",
        "pricing_sub": "Annual = 4 months free. Cancel anytime. 7-day money-back guarantee.",
        "annual": "Annual",
        "monthly": "Monthly",
        "best_choice": "Best choice",
        "unlimited": "Unlimited",
        "your_plan": "Your plan",
        "billed_year": "billed/year",
        "billed_month": "billed/month",
        "no_commitment": "No commitment",
        "save_up_to": "Save up to {amount}€/year",
        "max_quality": "max quality",
        "credits_per_month": "credits/month",
        "max_duration": "max duration/video",
        "feature_cut": "Video cutting",
        "feature_playlist": "Full playlists",
        "feature_support": "Priority support",
        "feature_no_ads": "No ads",
        "choose": "Choose",
        "current_plan_btn": "Current plan",
        "start_free": "Start free",
        "desc_free": "Try with no commitment. 3 videos per month.",
        "desc_starter": "Perfect for occasional HD use.",
        "desc_creator": "Best value for creators.",
        "desc_pro": "For professionals and heavy users.",
        "guarantee_1_title": "Money-back guarantee",
        "guarantee_1_text": "7 days to test. Cancel easily from your account.",
        "guarantee_2_title": "Instant downloads",
        "guarantee_2_text": "Fast servers and priority bandwidth on paid plans.",
        "guarantee_3_title": "Available worldwide",
        "guarantee_3_text": "Interface translated into 15 languages so anyone can download.",
        "nav_pricing": "Pricing",
        "nav_dashboard": "Dashboard",
        "nav_login": "Login",
        "nav_signup": "Try free",
        "nav_logout": "Logout",
        "footer_tagline": "The simple, fast and privacy-friendly video converter.",
        "footer_product": "Product",
        "footer_legal": "Legal",
        "footer_help": "Help",
        "about": "About",
        "contact": "Contact",
        "terms": "Terms",
        "privacy": "Privacy",
        "all_rights": "All rights reserved.",
        "hero_h1_part1": "Download any video as",
        "hero_sub": "Paste a YouTube, TikTok, Instagram or Facebook link. Choose format. It's downloaded. Simple, fast, ad-free.",
        "tab_video": "🎬 Video (MP4)",
        "tab_audio": "🎵 Audio (MP3)",
        "format_label": "Format",
        "playlist": "Full playlist",
        "paste": "Paste",
        "status_preparing": "Preparing...",
        "status_finalizing": "Finalizing...",
        "trust_1": "Instant download",
        "trust_2": "No files stored on our servers",
        "trust_3": "1000+ supported sites",
        "compatible": "Compatible with",
        "why_title": "Why",
        "why_sub": "The simplest and most powerful converter on the market.",
        "feature_mp3_title": "High Quality MP3",
        "feature_mp3_text": "Extract audio in MP3 128kbps or MP3 HD 320kbps. Perfect for music and podcasts.",
        "feature_mp4_title": "MP4 up to 4K",
        "feature_mp4_text": "Download in 720p, 1080p, 1440p or 4K UHD. Progressive streaming directly in your browser.",
        "feature_speed_title": "No Speed Limit",
        "feature_speed_text": "With a premium account, enjoy unlimited and prioritized downloads.",
        "feature_anon_title": "100% Anonymous",
        "feature_anon_text": "No files stored on our servers. Everything goes through your browser cache.",
        "feature_devices_title": "All Devices",
        "feature_devices_text": "Works on mobile, tablet and desktop. No app to install.",
        "feature_cut_title": "Video Cutting",
        "feature_cut_text": "Premium members: cut videos online and download only the part you want.",
        "teaser_title": "Unlock unlimited downloads",
        "teaser_text": "No ads, no limits, HD/4K. Cut videos in 1 click. Go Starter from 4.99€/month.",
        "teaser_price": "4.99€<span>/month</span>",
        "teaser_cta": "See plans →",
        "faq_title": "Frequently asked questions",
        "faq_q1": "Is Yestubers free?",
        "faq_a1": "Yes. Download 2 videos without signing up. Then create a free account for 3 credits per month.",
        "faq_q2": "What formats are available?",
        "faq_a2": "MP3, MP3 HD, M4A, WAV for audio. MP4, MP4 HD, MP4 2K, MP4 4K, 3GP, FLV for video.",
        "faq_q3": "Are my files stored?",
        "faq_a3": "No. We use progressive streaming: bytes go directly from our servers to your browser with no storage.",
        "faq_q4": "Can I download playlists?",
        "faq_a4": "Yes, check \"Full playlist\" and we will zip all videos for you.",
        "nav_features": "Features",
        "nav_about": "About",
        "signup": "Sign up",
        "cookie_text": "We only use essential cookies.",
    },
    "zh": {
        "brand": "Yestubers",
        "title_home": "Free YouTube MP3/MP4 Downloader",
        "desc_home": "Yestubers is the fastest YouTube converter. Download any video as MP3, MP4, M4A or WAV with no signup.",
        "compare_title": "Free vs Creator",
        "feature": "Feature",
        "free": "Free",
        "yes": "Yes",
        "no": "No",
        "feature_quality": "Quality",
        "feature_ads": "Ads",
        "feature_credits": "Credits",
        "feature_cut": "Trim / Cut",
        "feature_playlist": "Playlists",
        "feature_support": "Priority support",
        "testimonials_title": "They use Yestubers",
        "cta_placeholder": "Paste YouTube link here...",
        "cta_button": "Download",
        "formats": "MP3, MP4, M4A, WAV",
        "free_badge": "100% free — 2 trials without account",
        "upgrade_badge": "Go Premium",
        "pricing_title": "Pricing",
        "pricing_desc": "Choose your Yestubers plan: free, Starter, Creator or Pro. Unlimited YouTube MP3/MP4 downloads, cutting, 4K.",
        "pricing_badge": "BEST SELLER",
        "pricing_h1": "Unlimited downloads from <span class=\"gradient-text\">€5.99/mo</span>",
        "pricing_sub": "Annual = 4 months free. Cancel anytime. 7-day money-back guarantee.",
        "annual": "Annual",
        "monthly": "Monthly",
        "best_choice": "Best choice",
        "unlimited": "Unlimited",
        "your_plan": "Your plan",
        "billed_year": "billed/year",
        "billed_month": "billed/month",
        "no_commitment": "No commitment",
        "save_up_to": "Save up to {amount}€/year",
        "max_quality": "max quality",
        "credits_per_month": "credits/month",
        "max_duration": "max duration/video",
        "feature_cut": "Video cutting",
        "feature_playlist": "Full playlists",
        "feature_support": "Priority support",
        "feature_no_ads": "No ads",
        "choose": "Choose",
        "current_plan_btn": "Current plan",
        "start_free": "Start free",
        "desc_free": "Try with no commitment. 3 videos per month.",
        "desc_starter": "Perfect for occasional HD use.",
        "desc_creator": "Best value for creators.",
        "desc_pro": "For professionals and heavy users.",
        "guarantee_1_title": "Money-back guarantee",
        "guarantee_1_text": "7 days to test. Cancel easily from your account.",
        "guarantee_2_title": "Instant downloads",
        "guarantee_2_text": "Fast servers and priority bandwidth on paid plans.",
        "guarantee_3_title": "Available worldwide",
        "guarantee_3_text": "Interface translated into 15 languages so anyone can download.",
        "nav_pricing": "Pricing",
        "nav_dashboard": "Dashboard",
        "nav_login": "Login",
        "nav_signup": "Try free",
        "nav_logout": "Logout",
        "footer_tagline": "The simple, fast and privacy-friendly video converter.",
        "footer_product": "Product",
        "footer_legal": "Legal",
        "footer_help": "Help",
        "about": "About",
        "contact": "Contact",
        "terms": "Terms",
        "privacy": "Privacy",
        "all_rights": "All rights reserved.",
        "hero_h1_part1": "Download any video as",
        "hero_sub": "Paste a YouTube, TikTok, Instagram or Facebook link. Choose format. It's downloaded. Simple, fast, ad-free.",
        "tab_video": "🎬 Video (MP4)",
        "tab_audio": "🎵 Audio (MP3)",
        "format_label": "Format",
        "playlist": "Full playlist",
        "paste": "Paste",
        "status_preparing": "Preparing...",
        "status_finalizing": "Finalizing...",
        "trust_1": "Instant download",
        "trust_2": "No files stored on our servers",
        "trust_3": "1000+ supported sites",
        "compatible": "Compatible with",
        "why_title": "Why",
        "why_sub": "The simplest and most powerful converter on the market.",
        "feature_mp3_title": "High Quality MP3",
        "feature_mp3_text": "Extract audio in MP3 128kbps or MP3 HD 320kbps. Perfect for music and podcasts.",
        "feature_mp4_title": "MP4 up to 4K",
        "feature_mp4_text": "Download in 720p, 1080p, 1440p or 4K UHD. Progressive streaming directly in your browser.",
        "feature_speed_title": "No Speed Limit",
        "feature_speed_text": "With a premium account, enjoy unlimited and prioritized downloads.",
        "feature_anon_title": "100% Anonymous",
        "feature_anon_text": "No files stored on our servers. Everything goes through your browser cache.",
        "feature_devices_title": "All Devices",
        "feature_devices_text": "Works on mobile, tablet and desktop. No app to install.",
        "feature_cut_title": "Video Cutting",
        "feature_cut_text": "Premium members: cut videos online and download only the part you want.",
        "teaser_title": "Unlock unlimited downloads",
        "teaser_text": "No ads, no limits, HD/4K. Cut videos in 1 click. Go Starter from 4.99€/month.",
        "teaser_price": "4.99€<span>/month</span>",
        "teaser_cta": "See plans →",
        "faq_title": "Frequently asked questions",
        "faq_q1": "Is Yestubers free?",
        "faq_a1": "Yes. Download 2 videos without signing up. Then create a free account for 3 credits per month.",
        "faq_q2": "What formats are available?",
        "faq_a2": "MP3, MP3 HD, M4A, WAV for audio. MP4, MP4 HD, MP4 2K, MP4 4K, 3GP, FLV for video.",
        "faq_q3": "Are my files stored?",
        "faq_a3": "No. We use progressive streaming: bytes go directly from our servers to your browser with no storage.",
        "faq_q4": "Can I download playlists?",
        "faq_a4": "Yes, check \"Full playlist\" and we will zip all videos for you.",
        "nav_features": "Features",
        "nav_about": "About",
        "signup": "Sign up",
        "cookie_text": "We only use essential cookies.",
    },
    "ru": {
        "brand": "Yestubers",
        "title_home": "Free YouTube MP3/MP4 Downloader",
        "desc_home": "Yestubers is the fastest YouTube converter. Download any video as MP3, MP4, M4A or WAV with no signup.",
        "compare_title": "Free vs Creator",
        "feature": "Feature",
        "free": "Free",
        "yes": "Yes",
        "no": "No",
        "feature_quality": "Quality",
        "feature_ads": "Ads",
        "feature_credits": "Credits",
        "feature_cut": "Trim / Cut",
        "feature_playlist": "Playlists",
        "feature_support": "Priority support",
        "testimonials_title": "They use Yestubers",
        "cta_placeholder": "Paste YouTube link here...",
        "cta_button": "Download",
        "formats": "MP3, MP4, M4A, WAV",
        "free_badge": "100% free — 2 trials without account",
        "upgrade_badge": "Go Premium",
        "pricing_title": "Pricing",
        "pricing_desc": "Choose your Yestubers plan: free, Starter, Creator or Pro. Unlimited YouTube MP3/MP4 downloads, cutting, 4K.",
        "pricing_badge": "BEST SELLER",
        "pricing_h1": "Unlimited downloads from <span class=\"gradient-text\">€5.99/mo</span>",
        "pricing_sub": "Annual = 4 months free. Cancel anytime. 7-day money-back guarantee.",
        "annual": "Annual",
        "monthly": "Monthly",
        "best_choice": "Best choice",
        "unlimited": "Unlimited",
        "your_plan": "Your plan",
        "billed_year": "billed/year",
        "billed_month": "billed/month",
        "no_commitment": "No commitment",
        "save_up_to": "Save up to {amount}€/year",
        "max_quality": "max quality",
        "credits_per_month": "credits/month",
        "max_duration": "max duration/video",
        "feature_cut": "Video cutting",
        "feature_playlist": "Full playlists",
        "feature_support": "Priority support",
        "feature_no_ads": "No ads",
        "choose": "Choose",
        "current_plan_btn": "Current plan",
        "start_free": "Start free",
        "desc_free": "Try with no commitment. 3 videos per month.",
        "desc_starter": "Perfect for occasional HD use.",
        "desc_creator": "Best value for creators.",
        "desc_pro": "For professionals and heavy users.",
        "guarantee_1_title": "Money-back guarantee",
        "guarantee_1_text": "7 days to test. Cancel easily from your account.",
        "guarantee_2_title": "Instant downloads",
        "guarantee_2_text": "Fast servers and priority bandwidth on paid plans.",
        "guarantee_3_title": "Available worldwide",
        "guarantee_3_text": "Interface translated into 15 languages so anyone can download.",
        "nav_pricing": "Pricing",
        "nav_dashboard": "Dashboard",
        "nav_login": "Login",
        "nav_signup": "Try free",
        "nav_logout": "Logout",
        "footer_tagline": "The simple, fast and privacy-friendly video converter.",
        "footer_product": "Product",
        "footer_legal": "Legal",
        "footer_help": "Help",
        "about": "About",
        "contact": "Contact",
        "terms": "Terms",
        "privacy": "Privacy",
        "all_rights": "All rights reserved.",
        "hero_h1_part1": "Download any video as",
        "hero_sub": "Paste a YouTube, TikTok, Instagram or Facebook link. Choose format. It's downloaded. Simple, fast, ad-free.",
        "tab_video": "🎬 Video (MP4)",
        "tab_audio": "🎵 Audio (MP3)",
        "format_label": "Format",
        "playlist": "Full playlist",
        "paste": "Paste",
        "status_preparing": "Preparing...",
        "status_finalizing": "Finalizing...",
        "trust_1": "Instant download",
        "trust_2": "No files stored on our servers",
        "trust_3": "1000+ supported sites",
        "compatible": "Compatible with",
        "why_title": "Why",
        "why_sub": "The simplest and most powerful converter on the market.",
        "feature_mp3_title": "High Quality MP3",
        "feature_mp3_text": "Extract audio in MP3 128kbps or MP3 HD 320kbps. Perfect for music and podcasts.",
        "feature_mp4_title": "MP4 up to 4K",
        "feature_mp4_text": "Download in 720p, 1080p, 1440p or 4K UHD. Progressive streaming directly in your browser.",
        "feature_speed_title": "No Speed Limit",
        "feature_speed_text": "With a premium account, enjoy unlimited and prioritized downloads.",
        "feature_anon_title": "100% Anonymous",
        "feature_anon_text": "No files stored on our servers. Everything goes through your browser cache.",
        "feature_devices_title": "All Devices",
        "feature_devices_text": "Works on mobile, tablet and desktop. No app to install.",
        "feature_cut_title": "Video Cutting",
        "feature_cut_text": "Premium members: cut videos online and download only the part you want.",
        "teaser_title": "Unlock unlimited downloads",
        "teaser_text": "No ads, no limits, HD/4K. Cut videos in 1 click. Go Starter from 4.99€/month.",
        "teaser_price": "4.99€<span>/month</span>",
        "teaser_cta": "See plans →",
        "faq_title": "Frequently asked questions",
        "faq_q1": "Is Yestubers free?",
        "faq_a1": "Yes. Download 2 videos without signing up. Then create a free account for 3 credits per month.",
        "faq_q2": "What formats are available?",
        "faq_a2": "MP3, MP3 HD, M4A, WAV for audio. MP4, MP4 HD, MP4 2K, MP4 4K, 3GP, FLV for video.",
        "faq_q3": "Are my files stored?",
        "faq_a3": "No. We use progressive streaming: bytes go directly from our servers to your browser with no storage.",
        "faq_q4": "Can I download playlists?",
        "faq_a4": "Yes, check \"Full playlist\" and we will zip all videos for you.",
        "nav_features": "Features",
        "nav_about": "About",
        "signup": "Sign up",
        "cookie_text": "We only use essential cookies.",
    },
    "tr": {
        "brand": "Yestubers",
        "title_home": "Free YouTube MP3/MP4 Downloader",
        "desc_home": "Yestubers is the fastest YouTube converter. Download any video as MP3, MP4, M4A or WAV with no signup.",
        "compare_title": "Free vs Creator",
        "feature": "Feature",
        "free": "Free",
        "yes": "Yes",
        "no": "No",
        "feature_quality": "Quality",
        "feature_ads": "Ads",
        "feature_credits": "Credits",
        "feature_cut": "Trim / Cut",
        "feature_playlist": "Playlists",
        "feature_support": "Priority support",
        "testimonials_title": "They use Yestubers",
        "cta_placeholder": "Paste YouTube link here...",
        "cta_button": "Download",
        "formats": "MP3, MP4, M4A, WAV",
        "free_badge": "100% free — 2 trials without account",
        "upgrade_badge": "Go Premium",
        "pricing_title": "Pricing",
        "pricing_desc": "Choose your Yestubers plan: free, Starter, Creator or Pro. Unlimited YouTube MP3/MP4 downloads, cutting, 4K.",
        "pricing_badge": "BEST SELLER",
        "pricing_h1": "Unlimited downloads from <span class=\"gradient-text\">€5.99/mo</span>",
        "pricing_sub": "Annual = 4 months free. Cancel anytime. 7-day money-back guarantee.",
        "annual": "Annual",
        "monthly": "Monthly",
        "best_choice": "Best choice",
        "unlimited": "Unlimited",
        "your_plan": "Your plan",
        "billed_year": "billed/year",
        "billed_month": "billed/month",
        "no_commitment": "No commitment",
        "save_up_to": "Save up to {amount}€/year",
        "max_quality": "max quality",
        "credits_per_month": "credits/month",
        "max_duration": "max duration/video",
        "feature_cut": "Video cutting",
        "feature_playlist": "Full playlists",
        "feature_support": "Priority support",
        "feature_no_ads": "No ads",
        "choose": "Choose",
        "current_plan_btn": "Current plan",
        "start_free": "Start free",
        "desc_free": "Try with no commitment. 3 videos per month.",
        "desc_starter": "Perfect for occasional HD use.",
        "desc_creator": "Best value for creators.",
        "desc_pro": "For professionals and heavy users.",
        "guarantee_1_title": "Money-back guarantee",
        "guarantee_1_text": "7 days to test. Cancel easily from your account.",
        "guarantee_2_title": "Instant downloads",
        "guarantee_2_text": "Fast servers and priority bandwidth on paid plans.",
        "guarantee_3_title": "Available worldwide",
        "guarantee_3_text": "Interface translated into 15 languages so anyone can download.",
        "nav_pricing": "Pricing",
        "nav_dashboard": "Dashboard",
        "nav_login": "Login",
        "nav_signup": "Try free",
        "nav_logout": "Logout",
        "footer_tagline": "The simple, fast and privacy-friendly video converter.",
        "footer_product": "Product",
        "footer_legal": "Legal",
        "footer_help": "Help",
        "about": "About",
        "contact": "Contact",
        "terms": "Terms",
        "privacy": "Privacy",
        "all_rights": "All rights reserved.",
        "hero_h1_part1": "Download any video as",
        "hero_sub": "Paste a YouTube, TikTok, Instagram or Facebook link. Choose format. It's downloaded. Simple, fast, ad-free.",
        "tab_video": "🎬 Video (MP4)",
        "tab_audio": "🎵 Audio (MP3)",
        "format_label": "Format",
        "playlist": "Full playlist",
        "paste": "Paste",
        "status_preparing": "Preparing...",
        "status_finalizing": "Finalizing...",
        "trust_1": "Instant download",
        "trust_2": "No files stored on our servers",
        "trust_3": "1000+ supported sites",
        "compatible": "Compatible with",
        "why_title": "Why",
        "why_sub": "The simplest and most powerful converter on the market.",
        "feature_mp3_title": "High Quality MP3",
        "feature_mp3_text": "Extract audio in MP3 128kbps or MP3 HD 320kbps. Perfect for music and podcasts.",
        "feature_mp4_title": "MP4 up to 4K",
        "feature_mp4_text": "Download in 720p, 1080p, 1440p or 4K UHD. Progressive streaming directly in your browser.",
        "feature_speed_title": "No Speed Limit",
        "feature_speed_text": "With a premium account, enjoy unlimited and prioritized downloads.",
        "feature_anon_title": "100% Anonymous",
        "feature_anon_text": "No files stored on our servers. Everything goes through your browser cache.",
        "feature_devices_title": "All Devices",
        "feature_devices_text": "Works on mobile, tablet and desktop. No app to install.",
        "feature_cut_title": "Video Cutting",
        "feature_cut_text": "Premium members: cut videos online and download only the part you want.",
        "teaser_title": "Unlock unlimited downloads",
        "teaser_text": "No ads, no limits, HD/4K. Cut videos in 1 click. Go Starter from 4.99€/month.",
        "teaser_price": "4.99€<span>/month</span>",
        "teaser_cta": "See plans →",
        "faq_title": "Frequently asked questions",
        "faq_q1": "Is Yestubers free?",
        "faq_a1": "Yes. Download 2 videos without signing up. Then create a free account for 3 credits per month.",
        "faq_q2": "What formats are available?",
        "faq_a2": "MP3, MP3 HD, M4A, WAV for audio. MP4, MP4 HD, MP4 2K, MP4 4K, 3GP, FLV for video.",
        "faq_q3": "Are my files stored?",
        "faq_a3": "No. We use progressive streaming: bytes go directly from our servers to your browser with no storage.",
        "faq_q4": "Can I download playlists?",
        "faq_a4": "Yes, check \"Full playlist\" and we will zip all videos for you.",
        "nav_features": "Features",
        "nav_about": "About",
        "signup": "Sign up",
        "cookie_text": "We only use essential cookies.",
    },
}

def detect_locale(request: Request) -> str:
    # 0. Path-forced locale
    forced = getattr(request, "_forced_locale", None)
    if forced in SUPPORTED_LOCALES:
        return forced
    # 1. Query param ?lang=xx
    q = request.query_params.get("lang")
    if q in SUPPORTED_LOCALES:
        return q
    # 2. Cookie
    c = request.cookies.get("locale")
    if c in SUPPORTED_LOCALES:
        return c
    # 3. Accept-Language header
    accept = request.headers.get("accept-language", "")
    for part in accept.replace(";", ",").split(","):
        part = part.strip().split("-")[0].lower()
        if part in SUPPORTED_LOCALES:
            return part
    return DEFAULT_LOCALE

def t(request: Request, key: str) -> str:
    loc = detect_locale(request)
    return I18N.get(loc, I18N[DEFAULT_LOCALE]).get(key, key)

def translate_dict(request: Request) -> dict:
    loc = detect_locale(request)
    base = I18N[DEFAULT_LOCALE].copy()
    # Only fill missing keys from detected locale; French default stays dominant
    for k, v in I18N.get(loc, {}).items():
        base.setdefault(k, v)
    base["_locale"] = loc
    base["_supported"] = SUPPORTED_LOCALES
    base["_og_region"] = LOCALE_REGION.get(loc, "US")
    return base


app = FastAPI(title="Yestubers", version="1.0.0")

@app.middleware("http")
async def add_cache_control_header(request: Request, call_next):
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Jinja2Templates from Starlette breaks with request in context (unhashable cache key)
# Use a plain Jinja2 environment with caching disabled
_jinja_env = Environment(
    loader=FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=select_autoescape(["html"]),
    cache_size=0,
)
_jinja_env.filters["from_json"] = json.loads
_jinja_env.filters["fr_eur"] = lambda n: f"{n:.2f}".replace(".", ",")
_jinja_env.globals["plans"] = PLANS
_jinja_env.globals["datetime"] = datetime

# ─── DB ───────────────────────────────────────────────────────────────────────

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String(255), unique=True, index=True)
    password_hash = Column(String(128))
    api_key = Column(String(64), unique=True, index=True, default=lambda: secrets.token_hex(32))
    credits = Column(Integer, default=1)
    plan = Column(String(20), default="free")
    stripe_customer_id = Column(String(64), nullable=True)
    stripe_sub_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    reset_token = Column(String(128), nullable=True)
    reset_expires = Column(DateTime, nullable=True)
    email_verified = Column(Boolean, default=False)
    email_verification_token = Column(String(128), nullable=True)
    email_verification_expires = Column(DateTime, nullable=True)
    referral_code = Column(String(16), unique=True, index=True, nullable=True)
    referrer_id = Column(String(36), ForeignKey("users.id"), nullable=True)

class Video(Base):
    __tablename__ = "videos"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), index=True)
    youtube_url = Column(String(1024))
    title = Column(String(512))
    duration = Column(Float, default=0)
    filename = Column(String(512))
    filesize = Column(Integer, default=0)
    thumbnail = Column(String(512), nullable=True)
    status = Column(String(20), default="pending")  # pending, processing, done, error
    created_at = Column(DateTime, default=datetime.utcnow)

class Cut(Base):
    __tablename__ = "cuts"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), index=True)
    video_id = Column(String(36), ForeignKey("videos.id"))
    start_time = Column(Float)
    end_time = Column(Float)
    filename = Column(String(512))
    filesize = Column(Integer, default=0)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ─── Auth ─────────────────────────────────────────────────────────────────────

def hash_pw(pw: str) -> str:
    """Hachage bcrypt avec prefix pour rétro-compatibilité."""
    return "bcrypt$" + bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()

def verify_pw(pw: str, hashed: str) -> bool:
    if not hashed:
        return False
    if hashed.startswith("bcrypt$"):
        return bcrypt.checkpw(pw.encode(), hashed[7:].encode())
    # Legacy SHA256 with secret (auto-upgrade on success)
    legacy = hashlib.sha256((pw + SECRET_KEY).encode()).hexdigest()
    if hmac.compare_digest(hashed, legacy):
        return True
    return False

def _sign_session(user_id: str) -> str:
    sig = hmac.new(SECRET_KEY.encode(), user_id.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{user_id}.{sig}"

def _unsign_session(cookie: str) -> Optional[str]:
    if not cookie or "." not in cookie:
        return None
    user_id, sig = cookie.rsplit(".", 1)
    expected = hmac.new(SECRET_KEY.encode(), user_id.encode(), hashlib.sha256).hexdigest()[:32]
    if hmac.compare_digest(sig, expected):
        return user_id
    return None

def generate_email_verification_token(user: User, db: Session) -> str:
    token = secrets.token_urlsafe(32)
    user.email_verification_token = token
    user.email_verification_expires = datetime.utcnow() + timedelta(hours=24)
    db.commit()
    return token

def send_verification_email(user: User, token: str) -> bool:
    verify_url = f"https://yestubers.cloud/verify-email?token={token}"
    body = f"""<p>Bonjour,</p>
<p>Merci de rejoindre Yestubers. Confirmez votre adresse email en cliquant sur le lien ci-dessous :</p>
<p><a href="{verify_url}" style="display:inline-block;padding:12px 24px;background:linear-gradient(135deg,#2563eb,#3b82f6);color:#fff;border-radius:8px;text-decoration:none;font-weight:700;">Vérifier mon email</a></p>
<p>Ou copiez ce lien : {verify_url}</p>
<p>Ce lien expire dans 24 heures.</p>
<p>Si vous n'êtes pas à l'origine de cette inscription, ignorez cet email.</p>
"""
    return send_email(user.email, "Confirmez votre email — Yestubers", body)

def get_user_from_session(request: Request, db: Session):
    cookie = request.cookies.get("session")
    user_id = _unsign_session(cookie)
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()

def require_user(request: Request, db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Non authentifié")
    return user

def check_credits(user: User):
    if user.credits <= 0:
        raise HTTPException(status_code=402, detail="Crédits insuffisants. Passez à un plan supérieur.")

def use_credit(user: User, db: Session):
    user.credits -= 1
    db.commit()

# ─── YouTube ──────────────────────────────────────────────────────────────────

def extract_video_id(url: str) -> Optional[str]:
    patterns = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/v/)([^&\n?#]+)',
        r'youtube\.com/shorts/([^&\n?#]+)',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

def is_valid_youtube_url(url: str) -> bool:
    return bool(extract_video_id(url))

def get_video_info(url: str) -> dict:
    """Récupère les infos de la vidéo sans la télécharger."""
    try:
        js_runtime = ["--js-runtimes", f"node:{NODE_PATH}"] if Path(NODE_PATH).exists() else []
        result = subprocess.run(
            ["yt-dlp"] + js_runtime + ["--dump-json", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise Exception(f"yt-dlp error: {result.stderr}")
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise Exception("Timeout lors de la récupération des infos")
    except Exception as e:
        raise Exception(f"Erreur: {str(e)}")

def cut_video(url_or_path: str, start: float, end: float, out_id: str, max_height: int = 720) -> Path:
    """Découpe un segment vidéo. Accepte une URL YouTube ou un chemin local.
    Le fichier source est retéléchargé si nécessaire, puis effacé après la découpe.
    """
    out_path = CUTS / f"{out_id}.mp4"
    input_is_url = url_or_path.startswith("http://") or url_or_path.startswith("https://")
    tmp_input = None

    try:
        if input_is_url:
            tmp_input = VIDEOS / f"_cut_src_{out_id}.mp4"
            height_limit = max(144, max_height)
            cmd = [
                "yt-dlp", "--no-playlist",
                "--js-runtimes", f"node:{NODE_PATH}",
                "-f", f"best[height<={height_limit}][ext=mp4]/best[height<={height_limit}]/best",
                "-o", str(tmp_input), url_or_path
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                raise Exception(f"yt-dlp error: {proc.stderr[:300]}")
            if not tmp_input.exists():
                candidates = [p for p in VIDEOS.iterdir() if p.stem == f"_cut_src_{out_id}"]
                if candidates:
                    tmp_input = candidates[0]
                else:
                    raise Exception("Source non téléchargée")
            src = str(tmp_input)
        else:
            src = str(Path(url_or_path))

        duration = end - start
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start),
             "-t", str(duration), "-i", src, "-c", "copy", "-avoid_negative_ts", "make_zero",
             str(out_path)],
            capture_output=True, text=True, timeout=180
        )
        if proc.returncode != 0:
            raise Exception(f"ffmpeg error: {proc.stderr[:300]}")
        if tmp_input and tmp_input.exists():
            tmp_input.unlink(missing_ok=True)
        return out_path
    except Exception:
        if tmp_input and tmp_input.exists():
            tmp_input.unlink(missing_ok=True)
        raise


async def download_video(url: str, video_id_db: str, max_duration: int = 60, quality: str = "480") -> dict:
    """Télécharge une vidéo YouTube selon la qualité du plan. Aucun fichier n'est conservé sur le VPS."""
    out_path = VIDEOS / f"{video_id_db}.mp4"

    try:
        info = get_video_info(url)
        duration = info.get("duration", 0) or 0
    except Exception:
        info = {}
        duration = 0

    if max_duration and duration > max_duration:
        raise Exception(f"Durée limitée à {max_duration}s avec votre plan.")

    quality_map = {"sd": "360", "hd": "720", "fullhd": "1080", "2k": "1440", "4k": "2160"}
    target = quality_map.get(quality, quality)
    try:
        h = int(target)
    except ValueError:
        h = 480
    height_limit = max(h, 144)

    cmd = [
        "yt-dlp", "--no-playlist",
        "--js-runtimes", f"node:{NODE_PATH}",
        "-f", f"best[height<={height_limit}][ext=mp4]/best[height<={height_limit}]/best",
        "-o", str(VIDEOS / f"{video_id_db}"), url
    ]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise Exception(f"yt-dlp error: {stderr.decode()[:300]}")

    if not out_path.exists():
        candidates = [p for p in VIDEOS.iterdir() if p.stem == video_id_db]
        if candidates:
            out_path = candidates[0]
    if not out_path.exists():
        raise Exception("Le téléchargement a échoué (fichier non créé).")

    filesize = out_path.stat().st_size if out_path.exists() else 0
    return {
        "title": info.get("title", "Sans titre"),
        "duration": duration,
        "filename": out_path.name,
        "filesize": filesize,
        "thumbnail": info.get("thumbnail"),
    }


def _delete_file_soon(paths: list):
    """Helper for BackgroundTasks: silently remove generated files from VPS."""
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass


def cleanup_old_files(hours: int = 24):
    """Remove leftover video/cut files (defensive). History stays in DB."""
    now = datetime.utcnow()
    removed = 0
    for folder in (VIDEOS, CUTS):
        if not folder.exists():
            continue
        for p in folder.iterdir():
            try:
                if p.is_file() and (now - datetime.fromtimestamp(p.stat().st_mtime)) > timedelta(hours=hours):
                    p.unlink()
                    removed += 1
            except Exception:
                pass
    return removed


@app.get("/robots.txt")
async def robots_txt():
    return FileResponse(str(STATIC_DIR / "robots.txt"), media_type="text/plain")

def _build_sitemap() -> str:
    base = "https://yestubers.cloud"
    locales = ["fr", "en", "es", "de", "it", "pt", "nl", "pl", "ar", "hi", "ja", "ko", "zh", "ru", "tr"]
    static_pages = ["pricing", "about", "contact", "terms", "privacy", "dmca"]

    def url_block(path: str, priority: str = "0.8") -> list[str]:
        lines = [
            "  <url>",
            f"    <loc>{base}{path}</loc>",
            f'    <xhtml:link rel="alternate" hreflang="x-default" href="{base}{path}"/>',
        ]
        for loc in locales:
            lines.append(f'    <xhtml:link rel="alternate" hreflang="{loc}" href="{base}/{loc}{path}"/>')
        lines.extend([
            "    <changefreq>weekly</changefreq>",
            f"    <priority>{priority}</priority>",
            "  </url>",
        ])
        return lines

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"', '        xmlns:xhtml="http://www.w3.org/1999/xhtml">']

    # homepage
    lines.extend(url_block("/", priority="1.0"))

    # static pages
    for page in static_pages:
        lines.extend(url_block(f"/{page}", priority="0.6"))

    # legacy SEO pages
    for tool in SEO_PAGES:
        lines.extend(url_block(f"/{tool}", priority="0.7"))

    # new high-intent keyword landing pages
    for slug in LP_PAGES:
        lines.extend(url_block(f"/{slug}", priority="0.9"))

    lines.append("</urlset>")
    return "\n".join(lines)


@app.get("/sitemap.xml")
async def sitemap_xml():
    return Response(_build_sitemap(), media_type="application/xml")

@app.get("/.well-known/security.txt")
async def security_txt():
    return FileResponse(str(STATIC_DIR / ".well-known" / "security.txt"), media_type="text/plain")

@app.get("/favicon.ico")
async def favicon_root():
    return FileResponse(str(STATIC_DIR / "favicon.ico"), media_type="image/vnd.microsoft.icon")


# ─── IndexNow (disabled when no SEO API key is configured) ─────────────────────

INDEXNOW_ENABLED = bool(os.environ.get("INDEXNOW_KEY", "").strip())

@app.get("/78CqOblo3fZYJJIdLYGN4WfnuHgLiFCW.txt")
async def indexnow_key():
    if not INDEXNOW_ENABLED:
        raise HTTPException(404, "Not configured")
    return PlainTextResponse("78CqOblo3fZYJJIdLYGN4WfnuHgLiFCW")

@app.post("/api/indexnow/submit")
async def indexnow_submit(urls: list[str] = Form(...)):
    if not INDEXNOW_ENABLED:
        return {"ok": False, "message": "IndexNow non configuré en mode autonome."}
    import httpx
    payload = {"host": "yestubers.cloud", "key": "78CqOblo3fZYJJIdLYGN4WfnuHgLiFCW", "keyLocation": f"https://yestubers.cloud/78CqOblo3fZYJJIdLYGN4WfnuHgLiFCW.txt", "urlList": urls}
    async with httpx.AsyncClient() as client:
        await client.post("https://api.indexnow.org/IndexNow", json=payload, timeout=20)
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    i18n = translate_dict(request)
    return HTMLResponse(_jinja_env.get_template("login.html").render(request=request, i18n=i18n, locale=i18n["_locale"]))

@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    i18n = translate_dict(request)
    return HTMLResponse(_jinja_env.get_template("signup.html").render(request=request, i18n=i18n, locale=i18n["_locale"]))

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse("/login")
    videos = db.query(Video).filter(Video.user_id == user.id).order_by(Video.created_at.desc()).all()
    cuts = db.query(Cut).filter(Cut.user_id == user.id).order_by(Cut.created_at.desc()).all()
    total_size = sum(v.filesize for v in videos) + sum(c.filesize for c in cuts)
    i18n = translate_dict(request)
    return HTMLResponse(_jinja_env.get_template("dashboard.html").render(
        request=request, user=user, videos=videos, cuts=cuts,
        stripe_pk=STRIPE_PK, i18n=i18n, locale=i18n["_locale"],
        total_size=total_size,
    ))

@app.get("/pricing", response_class=HTMLResponse)
async def pricing(request: Request, db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    i18n = translate_dict(request)
    return HTMLResponse(_jinja_env.get_template("pricing.html").render(
        request=request, user=user, stripe_pk=STRIPE_PK, stripe_enabled=STRIPE_ENABLED, i18n=i18n, locale=i18n["_locale"],
        plans=PLANS,
    ))

@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request, db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    i18n = translate_dict(request)
    return HTMLResponse(_jinja_env.get_template("about.html").render(request=request, user=user, i18n=i18n, locale=i18n["_locale"], page_path="about"))

@app.get("/contact", response_class=HTMLResponse)
async def contact_page(request: Request, success: bool = False, error: str = "", db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    i18n = translate_dict(request)
    return HTMLResponse(_jinja_env.get_template("contact.html").render(
        request=request, user=user, i18n=i18n, locale=i18n["_locale"], page_path="contact",
        success=success, error=error))

@app.post("/api/contact")
async def api_contact(request: Request, name: str = Form(...), email: str = Form(...), subject: str = Form(...), message: str = Form(...)):
    check_rate_limit(request, "auth")
    # Anti-spam: honeypot hidden field not present; basic validation
    if len(message) < 10 or len(name) < 2 or "@" not in email:
        return RedirectResponse("/contact?error=Veuillez+vérifier+vos+informations", status_code=303)
    body = f"""<p><strong>Nouveau message de contact Yestubers</strong></p>
    <p><strong>Nom :</strong> {html.escape(name)}</p>
    <p><strong>Email :</strong> {html.escape(email)}</p>
    <p><strong>Sujet :</strong> {html.escape(subject)}</p>
    <p><strong>Message :</strong></p>
    <p>{html.escape(message).replace(chr(10), '<br>')}</p>"""
    send_email("contact@yestubers.cloud", f"Contact Yestubers — {subject}", body)
    return RedirectResponse("/contact?success=1", status_code=303)

@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request, db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    i18n = translate_dict(request)
    return HTMLResponse(_jinja_env.get_template("terms.html").render(request=request, user=user, i18n=i18n, locale=i18n["_locale"]))

@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request, db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    i18n = translate_dict(request)
    return HTMLResponse(_jinja_env.get_template("privacy.html").render(request=request, user=user, i18n=i18n, locale=i18n["_locale"]))

@app.get("/dmca", response_class=HTMLResponse)
async def dmca_page(request: Request, db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    i18n = translate_dict(request)
    return HTMLResponse(_jinja_env.get_template("dmca.html").render(request=request, user=user, i18n=i18n, locale=i18n["_locale"]))

@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    i18n = translate_dict(request)
    return HTMLResponse(_jinja_env.get_template("forgot-password.html").render(request=request, i18n=i18n, locale=i18n["_locale"]))


@app.get("/reset", response_class=HTMLResponse)
async def reset_page(request: Request):
    i18n = translate_dict(request)
    return HTMLResponse(_jinja_env.get_template("reset.html").render(request=request, i18n=i18n, locale=i18n["_locale"]))

@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request):
    i18n = translate_dict(request)
    return HTMLResponse(_jinja_env.get_template("reset-password.html").render(
        request=request, token=request.query_params.get("token", ""), i18n=i18n, locale=i18n["_locale"]))

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse("/login")
    i18n = translate_dict(request)
    return HTMLResponse(_jinja_env.get_template("settings.html").render(
        request=request, user=user, i18n=i18n, locale=i18n["_locale"]))

@app.get("/affiliate", response_class=HTMLResponse)
async def affiliate_page(request: Request, db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    i18n = translate_dict(request)
    return HTMLResponse(_jinja_env.get_template("affiliate.html").render(
        request=request, user=user, i18n=i18n, locale=i18n["_locale"]))

@app.post("/api/affiliate/apply")
async def affiliate_apply(request: Request, email: str = Form(...), name: str = Form(...), channels: str = Form(...)):
    check_rate_limit(request, "auth")
    email = email.strip().lower()
    if not re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", email):
        raise HTTPException(400, "Adresse email invalide")
    body = f"""<p><strong>Nouvelle demande d'affiliation Yestubers</strong></p>
    <p><strong>Email :</strong> {html.escape(email)}</p>
    <p><strong>Nom :</strong> {html.escape(name)}</p>
    <p><strong>Canaux :</strong> {html.escape(channels).replace(chr(10), '<br>')}</p>
    """
    send_email("contact@yestubers.cloud", f"Demande affiliation — {email}", body)
    log_path = BASE_DIR / "storage" / "affiliate_applications.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().isoformat()} | {email} | {name} | {channels.replace(chr(10), ' ')}\n")
    return JSONResponse({"ok": True, "message": "Demande envoyée"})

# SEO landing pages
SEO_PAGES = {
    "mp3": {
        "title": "Convertisseur YouTube MP3 — Gratuit & HD | Yestubers",
        "description": "Téléchargez n'importe quelle vidéo YouTube en MP3 192 kbps. Rapide, gratuit, sans pub. 3 crédits offerts à l'inscription.",
        "h1": "YouTube → MP3",
        "subtitle": "Extrait l'audio de vos vidéos YouTube en MP3 haute qualité, en quelques secondes.",
        "badge": "MP3 gratuit · 192 kbps · Inscription = 3 crédits",
        "placeholder": "Collez un lien YouTube ici...",
        "cta": "Convertir en MP3",
        "canonical_path": "/mp3",
        "h2_1": "Pourquoi convertir YouTube en MP3 avec Yestubers ?",
        "p_1": "Yestubers est le convertisseur YouTube MP3 le plus rapide en ligne : pas de logiciel à installer, pas de publicité, qualité audio optimale. Collez le lien, cliquez sur Convertir, récupérez votre MP3.",
        "bullets": [
            "MP3 192 kbps stéréo",
            "Extraction audio sans perte",
            "Playlist et longues vidéos acceptées",
            "Compte gratuit avec 3 crédits par mois"
        ],
        "h2_2": "Est-ce légal de télécharger du MP3 depuis YouTube ?",
        "p_2": "Vous devez respecter les droits d'auteur. Yestubers autorise uniquement la conversion de contenus pour lesquels vous disposez des droits ou qui sont dans le domaine public."
    },
    "mp4": {
        "title": "Télécharger YouTube MP4 HD — Gratuit | Yestubers",
        "description": "Téléchargez des vidéos YouTube en MP4 HD 720p, 1080p et plus. Rapide, sécurisé, 3 crédits gratuits.",
        "h1": "YouTube → MP4 HD",
        "subtitle": "Enregistrez vos vidéos YouTube préférées en MP4 haute définition.",
        "badge": "MP4 HD · 720p · Gratuit & Premium",
        "placeholder": "Collez un lien YouTube ici...",
        "cta": "Télécharger en MP4",
        "canonical_path": "/mp4",
        "h2_1": "Téléchargement YouTube MP4 simple et rapide",
        "p_1": "Avec Yestubers, enregistrez n'importe quelle vidéo YouTube au format MP4. Le compte gratuit débloque la HD et 3 crédits mensuels.",
        "bullets": [
            "Qualité jusqu'à 1080p (selon plan)",
            "MP4 compatible tous lecteurs",
            "Pas de watermark",
            "Téléchargement cloud sécurisé"
        ],
        "h2_2": "Comment télécharger une vidéo YouTube en MP4 ?",
        "p_2": "Copiez l'URL YouTube, collez-la ci-dessus et cliquez sur Télécharger. Sans compte : aperçu basse qualité. Avec compte : HD et format audio."
    },
    "cut": {
        "title": "Couper une vidéo YouTube — Extracteur de clips | Yestubers",
        "description": "Découpez n'importe quelle vidéo YouTube en extrait court. Définissez start/end, téléchargez le clip. Inscription gratuite.",
        "h1": "Couper une vidéo YouTube",
        "subtitle": "Créez des extraits courts depuis YouTube : shorts, reels, clips pour réseaux sociaux.",
        "badge": "Découpe · Clips · Shorts & Reels",
        "placeholder": "Lien YouTube à découper...",
        "cta": "Découper la vidéo",
        "canonical_path": "/cut",
        "h2_1": "Créer des clips YouTube en quelques clics",
        "p_1": "Yestubers vous permet de couper une vidéo YouTube entre deux timestamps. Idéal pour les shorts, TikTok, Instagram Reels ou les highlights.",
        "bullets": [
            "Début et fin personnalisables",
            "Export MP4 prêt à poster",
            "Compte gratuit = 3 crédits",
            "Longueur max selon votre plan"
        ],
        "h2_2": "Pourquoi découper une vidéo YouTube ?",
        "p_2": "Extraire le meilleur moment d'une vidéo booste l'engagement sur les réseaux sociaux. Yestubers automatise la découpe."
    },
    "playlist": {
        "title": "Télécharger une playlist YouTube — MP3/MP4 | Yestubers",
        "description": "Téléchargez toute une playlist YouTube en MP3 ou MP4. Plans premium pour les gros volumes.",
        "h1": "Playlist YouTube → MP3/MP4",
        "subtitle": "Récupérez automatiquement toutes les vidéos d'une playlist YouTube.",
        "badge": "Playlist · Bulk · Premium",
        "placeholder": "Lien playlist YouTube...",
        "cta": "Télécharger la playlist",
        "canonical_path": "/playlist",
        "h2_1": "Téléchargement de playlists YouTube",
        "p_1": "Yestubers détecte les playlists et vous permet de les convertir en MP3 ou MP4. Parfait pour les podcasts, playlists musicales ou archives.",
        "bullets": [
            "Détection automatique des playlists",
            "Export fichier par fichier",
            "Plans premium pour les gros volumes",
            "Pas de limite de durée par crédit"
        ],
        "h2_2": "Quelle est la limite pour les playlists ?",
        "p_2": "Le plan gratuit permet un aperçu. Les plans premium débloquent le téléchargement complet des playlists."
    },
    "shorts": {
        "title": "Télécharger YouTube Shorts — MP4 HD | Yestubers",
        "description": "Téléchargez les YouTube Shorts au format vertical MP4. Parfait pour réutiliser votre contenu partout.",
        "h1": "YouTube Shorts → MP4",
        "subtitle": "Téléchargez n'importe quel Short YouTube en MP4, prêt à republier.",
        "badge": "Shorts · Vertical · Sans watermark",
        "placeholder": "Lien YouTube Shorts...",
        "cta": "Télécharger le Short",
        "canonical_path": "/shorts",
        "h2_1": "Pourquoi télécharger des YouTube Shorts ?",
        "p_1": "Les créateurs republient leurs Shorts sur TikTok, Instagram Reels et d'autres plateformes. Yestubers conserve la qualité verticale originale.",
        "bullets": [
            "Format vertical conservé",
            "MP4 universel",
            "Aucun watermark",
            "3 crédits gratuits avec un compte"
        ],
        "h2_2": "Comment ça marche ?",
        "p_2": "Collez le lien du Short, cliquez sur Télécharger. Sans compte : aperçu limité. Avec compte : HD illimité selon votre plan."
    },
    "reels": {
        "title": "Convertir YouTube en Reels — Extracteur vertical | Yestubers",
        "description": "Transformez les vidéos YouTube en format Reel/Short vertical. Idéal pour republier sur Instagram et TikTok.",
        "h1": "YouTube → Reels/TikTok",
        "subtitle": "Recadre automatiquement les vidéos YouTube au format vertical 9:16.",
        "badge": "9:16 · Reels · TikTok",
        "placeholder": "Lien vidéo YouTube...",
        "cta": "Convertir en Reel",
        "canonical_path": "/reels",
        "h2_1": "Reposter du contenu YouTube en Reels",
        "p_1": "Yestubers extrait le meilleur d'une vidéo YouTube et la recadre au format vertical 9:16 adapté à Instagram Reels et TikTok.",
        "bullets": [
            "Recadrage 9:16 automatique",
            "Export MP4 optimisé",
            "Découpe intégrée",
            "Parfait pour les créateurs"
        ],
        "h2_2": "Quel plan choisir pour les Reels ?",
        "p_2": "Le plan Creator est le meilleur rapport qualité/prix pour produire du contenu vertical régulièrement."
    }
}

# ─── Keyword SEO landing pages ────────────────────────────────────────────────

LP_PAGES: dict[str, dict] = {
    "youtube-to-mp3": {
        "template": "lp_base.html",
        "slug": "youtube-to-mp3",
        "canonical_path": "/youtube-to-mp3",
        "title": "YouTube to MP3 — Convertisseur MP3 Gratuit | Yestubers",
        "description": "Convertissez rapidement des vidéos YouTube en MP3 192 kbps. Gratuit, sans publicité, sans logiciel. 3 crédits offerts à l'inscription.",
        "h1": "YouTube to MP3",
        "subtitle": "Extrayez l'audio de n'importe quelle vidéo YouTube en MP3 haute qualité en quelques secondes.",
        "badge": "MP3 gratuit · 192 kbps · Sans inscription",
        "placeholder": "Collez un lien YouTube ici...",
        "cta": "Convertir en MP3",
        "tool_format": "mp3",
        "show_cut_options": False,
        "h2": "Le meilleur convertisseur YouTube to MP3",
        "lead": "Yestubers transforme vos vidéos YouTube en fichiers MP3 clairs et légers. Idéal pour la musique, les podcasts et les cours en ligne.",
        "features": [
            {"title": "MP3 192 kbps", "text": "Qualité audio stéréo optimisée pour tous les appareils."},
            {"title": "Extraction rapide", "text": "Conversion en ligne sans installation, directement dans le navigateur."},
            {"title": "Sans publicité", "text": "Interface épurée : collez le lien, cliquez, téléchargez."},
            {"title": "3 crédits gratuits", "text": "Créez un compte gratuit pour obtenir 3 conversions par mois."},
        ],
        "steps": [
            "Copiez l'URL de la vidéo YouTube.",
            "Collez le lien dans le champ ci-dessus.",
            "Cliquez sur « Convertir en MP3 » et récupérez votre fichier.",
        ],
        "why": "Contrairement aux outils bourrés de publicités, Yestubers offre une conversion propre, rapide et sécurisée. Les fichiers ne sont pas stockés sur nos serveurs.",
        "faqs": [
            {"question": "YouTube to MP3 est-il gratuit ?", "answer": "Oui. Vous pouvez convertir 2 vidéos sans inscription, puis 3 crédits par mois avec un compte gratuit."},
            {"question": "Quelle est la qualité MP3 ?", "answer": "L'extraction se fait en MP3 192 kbps stéréo, compatible avec tous les lecteurs audio et smartphones."},
            {"question": "Est-ce légal de convertir YouTube en MP3 ?", "answer": "Vous devez posséder les droits sur le contenu ou utiliser du contenu libre de droits. Yestubers interdit le téléchargement non autorisé de contenus protégés."},
        ],
        "related_links": [
            {"url": "/youtube-to-mp4", "label": "YouTube to MP4"},
            {"url": "/convert-youtube-mp3", "label": "Convertisseur YouTube MP3"},
            {"url": "/download-youtube-video", "label": "Télécharger YouTube"},
            {"url": "/cut-youtube-video", "label": "Couper YouTube"},
        ],
    },
    "youtube-to-mp4": {
        "template": "lp_base.html",
        "slug": "youtube-to-mp4",
        "canonical_path": "/youtube-to-mp4",
        "title": "YouTube to MP4 — Téléchargement HD Gratuit | Yestubers",
        "description": "Téléchargez des vidéos YouTube en MP4 HD 720p, 1080p et 4K. Rapide, sécurisé, 3 crédits gratuits avec un compte.",
        "h1": "YouTube to MP4",
        "subtitle": "Enregistrez vos vidéos YouTube préférées en MP4 haute définition, prêtes à être lues hors ligne.",
        "badge": "MP4 HD · 720p · 1080p · 4K",
        "placeholder": "Collez un lien YouTube ici...",
        "cta": "Télécharger en MP4",
        "tool_format": "mp4",
        "show_cut_options": False,
        "h2": "Téléchargez YouTube en MP4 en HD",
        "lead": "Yestubers est l'outil le plus simple pour transformer une URL YouTube en fichier MP4. Choisissez la qualité selon votre plan et profitez d'un téléchargement rapide.",
        "features": [
            {"title": "Jusqu'à 4K", "text": "Qualité 480p, 720p, 1080p, 1440p ou 4K selon votre abonnement."},
            {"title": "MP4 universel", "text": "Compatible TV, smartphone, tablette et montage vidéo."},
            {"title": "Sans watermark", "text": "Fichier propre, sans logo ni publicité intégrée."},
            {"title": "Cloud sécurisé", "text": "Accédez à vos téléchargements depuis votre dashboard."},
        ],
        "steps": [
            "Copiez le lien de la vidéo YouTube.",
            "Collez-le dans le champ de conversion.",
            "Cliquez sur « Télécharger en MP4 » et choisissez la qualité.",
        ],
        "why": "Yestubers garantit un MP4 de qualité originale, sans conversion inutile. Le plan Creator est le plus populaire pour les créateurs de contenu.",
        "faqs": [
            {"question": "Puis-je télécharger en 1080p gratuitement ?", "answer": "Le compte gratuit offre 3 crédits par mois en qualité HD 720p. La 1080p et la 4K sont disponibles sur les plans payants."},
            {"question": "Le fichier MP4 contient-il le son ?", "answer": "Oui, les fichiers MP4 incluent piste vidéo et audio. Vous pouvez aussi extraire l'audio en MP3."},
            {"question": "Les Shorts sont-ils supportés ?", "answer": "Oui, les liens YouTube Shorts fonctionnent comme les vidéos classiques."},
        ],
        "related_links": [
            {"url": "/youtube-to-mp3", "label": "YouTube to MP3"},
            {"url": "/download-youtube-video", "label": "Télécharger YouTube"},
            {"url": "/cut-youtube-video", "label": "Couper YouTube"},
            {"url": "/convert-youtube-mp3", "label": "Convertisseur MP3"},
        ],
    },
    "cut-youtube-video": {
        "template": "lp_base.html",
        "slug": "cut-youtube-video",
        "canonical_path": "/cut-youtube-video",
        "title": "Couper une vidéo YouTube — Découpe MP4 en ligne | Yestubers",
        "description": "Découpez une vidéo YouTube en extrait court. Définissez début/fin, téléchargez le clip MP4. Compte gratuit avec 3 crédits.",
        "h1": "Couper une vidéo YouTube",
        "subtitle": "Créez des clips courts depuis YouTube pour Shorts, Reels, TikTok ou vos montages.",
        "badge": "Découpe · Clips · Shorts & Reels",
        "placeholder": "Lien YouTube à découper...",
        "cta": "Découper la vidéo",
        "tool_format": "cut",
        "show_cut_options": True,
        "h2": "Découpe YouTube en ligne, sans logiciel",
        "lead": "Yestubers vous permet de couper une vidéo YouTube entre deux timestamps précis. Exportez un MP4 prêt à être publié sur les réseaux sociaux.",
        "features": [
            {"title": "Début/fin personnalisables", "text": "Réglez les secondes de début et de fin pour un clip sur mesure."},
            {"title": "Export MP4", "text": "Fichier prêt à l'emploi, compatible tous réseaux sociaux."},
            {"title": "3 crédits gratuits", "text": "Créez un compte pour obtenir 3 découpes par mois."},
            {"title": "Rapide", "text": "Découpe traitée par nos serveurs en quelques secondes."},
        ],
        "steps": [
            "Collez l'URL de la vidéo YouTube.",
            "Indiquez le début et la fin de l'extrait en secondes.",
            "Cliquez sur « Découper » et téléchargez votre clip.",
        ],
        "why": "La découpe est l'outil préféré des créateurs pour transformer une longue vidéo en contenu viral court. Yestubers automatise l'opération.",
        "faqs": [
            {"question": "Puis-je couper une vidéo gratuitement ?", "answer": "Oui, le compte gratuit inclut 3 crédits par mois pour découper des vidéos."},
            {"question": "Quelle est la durée maximale d'un clip ?", "answer": "La durée maximale dépend de votre plan : 30s gratuit, jusqu'à 1h en Pro."},
            {"question": "Le clip conserve-t-il la qualité HD ?", "answer": "Oui, la découpe conserve la qualité source disponible selon votre plan."},
        ],
        "related_links": [
            {"url": "/youtube-to-mp4", "label": "YouTube to MP4"},
            {"url": "/youtube-to-mp3", "label": "YouTube to MP3"},
            {"url": "/download-youtube-video", "label": "Télécharger YouTube"},
        ],
    },
    "download-youtube-video": {
        "template": "lp_base.html",
        "slug": "download-youtube-video",
        "canonical_path": "/download-youtube-video",
        "title": "Télécharger une vidéo YouTube — MP4 Gratuit | Yestubers",
        "description": "Téléchargez n'importe quelle vidéo YouTube au format MP4. Gratuit, rapide et sans publicité. 3 crédits mensuels offerts.",
        "h1": "Télécharger une vidéo YouTube",
        "subtitle": "Sauvegardez vos vidéos YouTube préférées en MP4 pour les regarder hors ligne.",
        "badge": "Téléchargement MP4 · Gratuit · Sans pub",
        "placeholder": "Collez un lien YouTube ici...",
        "cta": "Télécharger la vidéo",
        "tool_format": "mp4",
        "show_cut_options": False,
        "h2": "Téléchargez YouTube facilement",
        "lead": "Yestubers est le téléchargeur YouTube le plus simple : un lien, un clic, un fichier MP4. Inscription rapide pour plus de qualité et de crédits.",
        "features": [
            {"title": "Lien → MP4", "text": "Collez simplement l'URL YouTube pour obtenir votre fichier."},
            {"title": "Qualité HD", "text": "Téléchargez en 720p ou plus selon votre plan."},
            {"title": "Hors ligne", "text": "Gardez vos vidéos sur tous vos appareils."},
            {"title": "Sans pub", "text": "Aucune publicité intrusive pendant le téléchargement."},
        ],
        "steps": [
            "Copiez l'URL de la vidéo.",
            "Collez-la dans le champ prévu.",
            "Cliquez sur « Télécharger la vidéo ».",
        ],
        "why": "Notre technologie de streaming progressif évite le stockage durable de vos fichiers sur nos serveurs. Rapide, anonyme et respectueux de votre vie privée.",
        "faqs": [
            {"question": "Le téléchargement est-il gratuit ?", "answer": "Oui, 2 vidéos sans compte, puis 3 crédits par mois avec un compte gratuit."},
            {"question": "Puis-je télécharger des playlists ?", "answer": "Oui, l'option playlist est disponible pour les utilisateurs inscrits et les abonnements premium."},
            {"question": "Quels formats sont disponibles ?", "answer": "MP4 pour la vidéo, MP3/M4A/WAV pour l'audio."},
        ],
        "related_links": [
            {"url": "/youtube-to-mp4", "label": "YouTube to MP4"},
            {"url": "/youtube-to-mp3", "label": "YouTube to MP3"},
            {"url": "/cut-youtube-video", "label": "Couper YouTube"},
        ],
    },
    "convert-youtube-mp3": {
        "template": "lp_base.html",
        "slug": "convert-youtube-mp3",
        "canonical_path": "/convert-youtube-mp3",
        "title": "Convertir YouTube en MP3 — Convertisseur en ligne | Yestubers",
        "description": "Convertissez des vidéos YouTube en MP3 gratuitement. Extraction audio rapide, sans logiciel. 3 crédits offerts par mois.",
        "h1": "Convertir YouTube en MP3",
        "subtitle": "Transformez n'importe quelle vidéo YouTube en fichier audio MP3 en quelques secondes.",
        "badge": "Conversion MP3 · Gratuit · Sans logiciel",
        "placeholder": "Lien YouTube à convertir...",
        "cta": "Convertir en MP3",
        "tool_format": "mp3",
        "show_cut_options": False,
        "h2": "Convertisseur YouTube MP3 simple et rapide",
        "lead": "Yestubers extrait l'audio des vidéos YouTube pour créer des MP3 légers. Parfait pour playlists, podcasts et musique.",
        "features": [
            {"title": "Extraction audio", "text": "Piste audio MP3 claire, prête à être importée partout."},
            {"title": "Rapide", "text": "Conversion en ligne en quelques secondes."},
            {"title": "Sans compte limité", "text": "Testez sans inscription, puis bénéficiez de 3 crédits gratuits."},
            {"title": "Qualité constante", "text": "MP3 192 kbps stéréo pour une écoute confortable."},
        ],
        "steps": [
            "Collez l'URL de la vidéo YouTube.",
            "Cliquez sur « Convertir en MP3 ».",
            "Téléchargez le fichier audio obtenu.",
        ],
        "why": "Notre convertisseur est optimisé pour la vitesse et la qualité. Aucune publicité ne perturbe l'expérience.",
        "faqs": [
            {"question": "Convertir YouTube en MP3 est-il gratuit ?", "answer": "Oui, avec 2 essais sans compte puis 3 crédits gratuits par mois."},
            {"question": "Puis-je convertir une playlist ?", "answer": "Les playlists sont supportées pour les comptes premium."},
            {"question": "Le MP3 fonctionne-t-il sur iPhone ?", "answer": "Oui, les fichiers MP3 sont compatibles avec iPhone, Android et tous les lecteurs."},
        ],
        "related_links": [
            {"url": "/youtube-to-mp3", "label": "YouTube to MP3"},
            {"url": "/youtube-to-mp4", "label": "YouTube to MP4"},
            {"url": "/download-youtube-video", "label": "Télécharger YouTube"},
        ],
    },
    "youtube-downloader": {
        "template": "lp_base.html",
        "slug": "youtube-downloader",
        "canonical_path": "/youtube-downloader",
        "title": "YouTube Downloader — MP3/MP4 Gratuit | Yestubers",
        "description": "Le meilleur YouTube downloader gratuit. Téléchargez des vidéos et de l'audio depuis YouTube en MP4, MP3, M4A et WAV.",
        "h1": "YouTube Downloader",
        "subtitle": "Téléchargez n'importe quelle vidéo YouTube en MP4 ou MP3, gratuitement et sans publicité.",
        "badge": "MP3/MP4 · Gratuit · Sans pub",
        "placeholder": "Collez un lien YouTube...",
        "cta": "Télécharger",
        "tool_format": "mp4",
        "show_cut_options": False,
        "h2": "Le YouTube downloader le plus rapide",
        "lead": "Yestubers est le téléchargeur YouTube tout-en-un : vidéo MP4, audio MP3, découpe et playlists. Une seule URL, tous les formats.",
        "features": [
            {"title": "MP4 + MP3", "text": "Choisissez le format qui vous convient."},
            {"title": "Sans inscription", "text": "Testez immédiatement avec 2 essais gratuits."},
            {"title": "Découpe intégrée", "text": "Coupez des extraits depuis la même interface."},
            {"title": "15 langues", "text": "Disponible dans le monde entier pour tous les utilisateurs."},
        ],
        "steps": [
            "Copiez l'URL YouTube.",
            "Collez-la dans le champ de téléchargement.",
            "Choisissez MP4 ou MP3 et téléchargez.",
        ],
        "why": "Yestubers combine vitesse, simplicité et multi-formats. C'est l'outil de référence pour les créateurs et les utilisateurs quotidiens.",
        "faqs": [
            {"question": "Qu'est-ce qu'un YouTube downloader ?", "answer": "C'est un outil en ligne qui permet de télécharger des vidéos ou de l'audio depuis YouTube."},
            {"question": "Yestubers est-il sécurisé ?", "answer": "Oui, nous n'utilisons pas de publicités malveillantes et ne stockons pas vos fichiers de manière permanente."},
            {"question": "Quels formats propose Yestubers ?", "answer": "MP4 pour la vidéo, MP3/M4A/WAV pour l'audio."},
        ],
        "related_links": [
            {"url": "/youtube-to-mp3", "label": "YouTube to MP3"},
            {"url": "/youtube-to-mp4", "label": "YouTube to MP4"},
            {"url": "/cut-youtube-video", "label": "Couper YouTube"},
        ],
    },
    "youtube-cutter": {
        "template": "lp_base.html",
        "slug": "youtube-cutter",
        "canonical_path": "/youtube-cutter",
        "title": "YouTube Cutter — Découper une vidéo en ligne | Yestubers",
        "description": "YouTube Cutter en ligne : découpez des extraits courts depuis n'importe quelle vidéo. Export MP4, inscription gratuite.",
        "h1": "YouTube Cutter",
        "subtitle": "Découpez une vidéo YouTube en clips courts pour vos réseaux sociaux.",
        "badge": "Cutter · Clips · MP4",
        "placeholder": "Lien YouTube à découper...",
        "cta": "Découper",
        "tool_format": "cut",
        "show_cut_options": True,
        "h2": "YouTube Cutter en ligne",
        "lead": "Yestubers offre un cutter YouTube simple : définissez votre extrait, exportez un MP4 prêt à publier.",
        "features": [
            {"title": "Précision seconde", "text": "Début et fin réglables à la seconde près."},
            {"title": "Export MP4", "text": "Clip optimisé pour les réseaux sociaux."},
            {"title": "Gratuit", "text": "3 crédits offerts chaque mois."},
            {"title": "Sans watermark", "text": "Clip propre et libre de droits d'usage."},
        ],
        "steps": [
            "Collez l'URL YouTube.",
            "Définissez le début et la fin.",
            "Cliquez sur « Découper » et téléchargez.",
        ],
        "why": "Le YouTube Cutter Yestubers est conçu pour les créateurs qui veulent produire du contenu court rapidement.",
        "faqs": [
            {"question": "YouTube Cutter est-il gratuit ?", "answer": "Oui, avec un compte gratuit offrant 3 crédits de découpe par mois."},
            {"question": "Puis-je découper un Short ?", "answer": "Oui, les liens YouTube Shorts sont supportés."},
            {"question": "La qualité est-elle conservée ?", "answer": "Oui, la découpe conserve la résolution originale disponible selon votre plan."},
        ],
        "related_links": [
            {"url": "/cut-youtube-video", "label": "Couper YouTube"},
            {"url": "/youtube-to-mp4", "label": "YouTube to MP4"},
            {"url": "/youtube-to-mp3", "label": "YouTube to MP3"},
        ],
    },
    "telecharger-video-youtube": {
        "template": "lp_base.html",
        "slug": "telecharger-video-youtube",
        "canonical_path": "/telecharger-video-youtube",
        "title": "Télécharger vidéo YouTube — MP4 Gratuit | Yestubers",
        "description": "Téléchargez une vidéo YouTube en MP4 gratuitement. Rapide, sans publicité, 3 crédits offerts chaque mois.",
        "h1": "Télécharger vidéo YouTube",
        "subtitle": "Sauvegardez vos vidéos YouTube favorites au format MP4, simplement et rapidement.",
        "badge": "Téléchargement MP4 · Gratuit",
        "placeholder": "Collez un lien YouTube ici...",
        "cta": "Télécharger la vidéo",
        "tool_format": "mp4",
        "show_cut_options": False,
        "h2": "Comment télécharger une vidéo YouTube",
        "lead": "Yestubers permet de télécharger n'importe quelle vidéo YouTube en MP4. Copiez le lien, collez-le et récupérez votre fichier.",
        "features": [
            {"title": "Facile", "text": "Aucune compétence technique requise."},
            {"title": "MP4", "text": "Format universel compatible partout."},
            {"title": "Gratuit", "text": "Essai sans compte puis 3 crédits par mois."},
            {"title": "HD", "text": "Jusqu'à 4K selon votre abonnement."},
        ],
        "steps": [
            "Copiez l'URL de la vidéo YouTube.",
            "Collez-la dans le champ de saisie.",
            "Cliquez sur « Télécharger la vidéo ».",
        ],
        "why": "Yestubers est le téléchargeur YouTube en français le plus rapide. L'interface est optimisée pour mobile et desktop.",
        "faqs": [
            {"question": "Puis-je télécharger sans créer de compte ?", "answer": "Oui, 2 essais gratuits sont disponibles sans inscription."},
            {"question": "Quel format est proposé ?", "answer": "MP4 pour la vidéo et MP3/M4A/WAV pour l'audio."},
            {"question": "Le téléchargement est-il illimité ?", "answer": "Le plan Pro offre des téléchargements illimités."},
        ],
        "related_links": [
            {"url": "/download-youtube-video", "label": "Download YouTube Video"},
            {"url": "/youtube-to-mp4", "label": "YouTube to MP4"},
            {"url": "/youtube-to-mp3", "label": "YouTube to MP3"},
        ],
    },
    "convertisseur-youtube-mp3": {
        "template": "lp_base.html",
        "slug": "convertisseur-youtube-mp3",
        "canonical_path": "/convertisseur-youtube-mp3",
        "title": "Convertisseur YouTube MP3 Gratuit | Yestubers",
        "description": "Convertissez vos vidéos YouTube en MP3 gratuitement. Extraction audio rapide, sans logiciel, 3 crédits par mois.",
        "h1": "Convertisseur YouTube MP3",
        "subtitle": "Transformez n'importe quelle vidéo YouTube en fichier MP3 en un clic.",
        "badge": "Convertisseur MP3 · Gratuit · Sans logiciel",
        "placeholder": "Lien YouTube à convertir...",
        "cta": "Convertir en MP3",
        "tool_format": "mp3",
        "show_cut_options": False,
        "h2": "Le convertisseur YouTube MP3 en français",
        "lead": "Yestubers est le convertisseur YouTube MP3 le plus simple. Collez le lien, convertissez et téléchargez votre MP3.",
        "features": [
            {"title": "MP3 192 kbps", "text": "Qualité audio optimisée pour tous les appareils."},
            {"title": "Sans installation", "text": "Tout se passe dans votre navigateur."},
            {"title": "Gratuit", "text": "3 crédits offerts chaque mois."},
            {"title": "Rapide", "text": "Conversion traitée en quelques secondes."},
        ],
        "steps": [
            "Copiez le lien de la vidéo YouTube.",
            "Collez-le dans le champ de conversion.",
            "Cliquez sur « Convertir en MP3 ».",
        ],
        "why": "Notre convertisseur YouTube MP3 est entièrement en français, sans publicité invasive et sécurisé.",
        "faqs": [
            {"question": "Le convertisseur est-il gratuit ?", "answer": "Oui, avec un compte gratuit offrant 3 crédits par mois."},
            {"question": "Quelle qualité MP3 ?", "answer": "MP3 192 kbps stéréo, compatible tous lecteurs."},
            {"question": "Puis-je convertir des playlists ?", "answer": "Les playlists sont disponibles pour les utilisateurs premium."},
        ],
        "related_links": [
            {"url": "/convert-youtube-mp3", "label": "Convert YouTube MP3"},
            {"url": "/youtube-to-mp3", "label": "YouTube to MP3"},
            {"url": "/youtube-to-mp4", "label": "YouTube to MP4"},
        ],
    },
    "decouper-video-youtube": {
        "template": "lp_base.html",
        "slug": "decouper-video-youtube",
        "canonical_path": "/decouper-video-youtube",
        "title": "Découper une vidéo YouTube — Cutter en ligne | Yestubers",
        "description": "Découpez facilement une vidéo YouTube en extrait MP4. Définissez début et fin, téléchargez votre clip. Inscription gratuite.",
        "h1": "Découper une vidéo YouTube",
        "subtitle": "Créez des clips courts depuis YouTube pour vos réseaux sociaux, sans logiciel.",
        "badge": "Découper · Clips · MP4",
        "placeholder": "Lien YouTube à découper...",
        "cta": "Découper la vidéo",
        "tool_format": "cut",
        "show_cut_options": True,
        "h2": "Découper une vidéo YouTube en ligne",
        "lead": "Yestubers permet de découper une vidéo YouTube entre deux timestamps. Idéal pour créer des Shorts, Reels et TikTok.",
        "features": [
            {"title": "Facile", "text": "Début et fin en quelques clics."},
            {"title": "MP4", "text": "Export prêt à publier sur tous les réseaux."},
            {"title": "Gratuit", "text": "3 crédits offerts chaque mois."},
            {"title": "Précis", "text": "Réglage à la seconde près."},
        ],
        "steps": [
            "Collez l'URL de la vidéo YouTube.",
            "Indiquez le début et la fin de l'extrait.",
            "Cliquez sur « Découper la vidéo » et téléchargez.",
        ],
        "why": "Yestubers est le cutter YouTube en français le plus accessible : rapide, sans pub et optimisé mobile.",
        "faqs": [
            {"question": "Puis-je découper gratuitement ?", "answer": "Oui, 3 crédits par mois sont offerts avec un compte gratuit."},
            {"question": "Quelle durée maximale ?", "answer": "Jusqu'à 30s gratuitement et jusqu'à 1h avec le plan Pro."},
            {"question": "Le clip a-t-il un watermark ?", "answer": "Non, les clips exportés sont propres."},
        ],
        "related_links": [
            {"url": "/cut-youtube-video", "label": "Cut YouTube Video"},
            {"url": "/youtube-to-mp4", "label": "YouTube to MP4"},
            {"url": "/youtube-cutter", "label": "YouTube Cutter"},
        ],
    },

    "youtube-video-downloader": {
            "template": "lp_base.html",
            "slug": "youtube-video-downloader",
            "canonical_path": "/youtube-video-downloader",
            "title": "YouTube Video Downloader — MP4 HD Free | Yestubers",
            "description": "The fastest YouTube video downloader. Save any YouTube video as MP4 HD. Free account with 3 credits per month. No software needed.",
            "h1": "YouTube Video Downloader",
            "subtitle": "Download any YouTube video in MP4 HD. Paste the link, choose quality, get your file.",
            "badge": "MP4 HD · Free · No watermark",
            "placeholder": "Paste a YouTube video link...",
            "cta": "Download Video",
            "tool_format": "mp4",
            "show_cut_options": False,
            "h2": "Best YouTube video downloader online",
            "lead": "Yestubers lets you download any YouTube video as a clean MP4 file. HD quality, no ads, no signup required for the first tries.",
            "features": [
                {
                    "title": "MP4 HD",
                    "text": "Download in 720p, 1080p or 4K depending on your plan.",
                },
                {
                    "title": "No watermark",
                    "text": "Get a clean video file without any branding.",
                },
                {
                    "title": "Fast",
                    "text": "Servers optimized for quick conversion and download.",
                },
                {
                    "title": "3 free credits",
                    "text": "Sign up to get 3 downloads per month for free.",
                },
            ],
            "steps": [
                "Copy the YouTube video URL.",
                "Paste it into the downloader field.",
                "Click Download and choose your quality.",
            ],
            "why": "Unlike cluttered sites, Yestubers gives a fast, safe and ad-light experience for downloading YouTube videos.",
            "faqs": [
                {
                    "question": "Is this YouTube video downloader free?",
                    "answer": "Yes, you get 2 tries without an account and 3 free credits per month after signing up.",
                },
                {
                    "question": "Can I download in HD?",
                    "answer": "Yes, registered users can download in HD quality based on their plan.",
                },
                {
                    "question": "Is it safe?",
                    "answer": "Yes, Yestubers does not use malicious ads and does not store your files permanently.",
                },
            ],
            "related_links": [
                {
                    "url": "/youtube-to-mp4",
                    "label": "YouTube to MP4",
                },
                {
                    "url": "/youtube-to-mp3",
                    "label": "YouTube to MP3",
                },
                {
                    "url": "/youtube-downloader",
                    "label": "YouTube Downloader",
                },
            ],
        },
    "youtube-converter": {
            "template": "lp_base.html",
            "slug": "youtube-converter",
            "canonical_path": "/youtube-converter",
            "title": "YouTube Converter — MP3/MP4 Free Online | Yestubers",
            "description": "Free YouTube converter for MP3 and MP4. Convert any YouTube video to audio or video format online. 3 free credits per month.",
            "h1": "YouTube Converter",
            "subtitle": "Convert YouTube videos to MP3 or MP4 in one click. Fast, free and easy.",
            "badge": "MP3/MP4 · Free · Online",
            "placeholder": "Paste a YouTube link to convert...",
            "cta": "Convert",
            "tool_format": "mp4",
            "show_cut_options": False,
            "h2": "All-in-one YouTube converter",
            "lead": "Yestubers is the best YouTube converter: choose MP3 for audio or MP4 for video, then download your file in seconds.",
            "features": [
                {
                    "title": "MP3 + MP4",
                    "text": "Convert to audio or video format in the same tool.",
                },
                {
                    "title": "No install",
                    "text": "Everything works in your browser.",
                },
                {
                    "title": "High quality",
                    "text": "MP3 192 kbps and MP4 up to 4K.",
                },
                {
                    "title": "Free credits",
                    "text": "3 conversions per month with a free account.",
                },
            ],
            "steps": [
                "Copy the YouTube URL.",
                "Paste it and select MP3 or MP4.",
                "Click Convert and download.",
            ],
            "why": "Yestubers replaces multiple tools: one converter for all formats, all qualities and all devices.",
            "faqs": [
                {
                    "question": "What is a YouTube converter?",
                    "answer": "A tool that transforms a YouTube video into a downloadable file like MP3 or MP4.",
                },
                {
                    "question": "Is it free?",
                    "answer": "Yes, you can convert 2 videos without an account and get 3 free credits per month after signup.",
                },
                {
                    "question": "Which formats are supported?",
                    "answer": "MP3, MP4, M4A and WAV.",
                },
            ],
            "related_links": [
                {
                    "url": "/youtube-to-mp3",
                    "label": "YouTube to MP3",
                },
                {
                    "url": "/youtube-to-mp4",
                    "label": "YouTube to MP4",
                },
                {
                    "url": "/youtube-downloader",
                    "label": "YouTube Downloader",
                },
            ],
        },
    "convert-youtube-to-mp3": {
            "template": "lp_base.html",
            "slug": "convert-youtube-to-mp3",
            "canonical_path": "/convert-youtube-to-mp3",
            "title": "Convert YouTube to MP3 — Free Audio Download | Yestubers",
            "description": "Convert any YouTube video to MP3 for free. Extract audio in 192 kbps quality. No software, no ads. 3 free credits per month.",
            "h1": "Convert YouTube to MP3",
            "subtitle": "Turn any YouTube video into a high-quality MP3 audio file in seconds.",
            "badge": "MP3 · Free · 192 kbps",
            "placeholder": "Paste a YouTube link...",
            "cta": "Convert to MP3",
            "tool_format": "mp3",
            "show_cut_options": False,
            "h2": "Convert YouTube to MP3 online",
            "lead": "Yestubers extracts the audio from any YouTube video and saves it as a clean MP3. Ideal for music, podcasts and courses.",
            "features": [
                {
                    "title": "MP3 192 kbps",
                    "text": "Clear stereo audio compatible with all players.",
                },
                {
                    "title": "Fast extraction",
                    "text": "Convert online in seconds without installing anything.",
                },
                {
                    "title": "No ads",
                    "text": "Clean interface: paste, click, download.",
                },
                {
                    "title": "Free credits",
                    "text": "Get 3 free MP3 conversions per month.",
                },
            ],
            "steps": [
                "Copy the YouTube video URL.",
                "Paste it in the converter field.",
                "Click Convert to MP3 and download.",
            ],
            "why": "Yestubers gives a fast, safe and ad-free way to convert YouTube videos to MP3 audio files.",
            "faqs": [
                {
                    "question": "Can I convert YouTube to MP3 for free?",
                    "answer": "Yes, 2 conversions without signup and 3 free credits per month with an account.",
                },
                {
                    "question": "What MP3 quality do I get?",
                    "answer": "All MP3 files are extracted in 192 kbps stereo quality.",
                },
                {
                    "question": "Is converting YouTube to MP3 legal?",
                    "answer": "You must own the rights to the content or use copyright-free material.",
                },
            ],
            "related_links": [
                {
                    "url": "/youtube-to-mp3",
                    "label": "YouTube to MP3",
                },
                {
                    "url": "/convert-youtube-mp3",
                    "label": "Convert YouTube MP3",
                },
                {
                    "url": "/youtube-to-mp4",
                    "label": "YouTube to MP4",
                },
            ],
        },
    "youtube-mp3-downloader": {
            "template": "lp_base.html",
            "slug": "youtube-mp3-downloader",
            "canonical_path": "/youtube-mp3-downloader",
            "title": "YouTube MP3 Downloader — Free Audio | Yestubers",
            "description": "Download YouTube videos as MP3 audio files. Free YouTube MP3 downloader with 192 kbps quality. 3 credits per month.",
            "h1": "YouTube MP3 Downloader",
            "subtitle": "Save YouTube audio as MP3. Fast, free and compatible with all devices.",
            "badge": "MP3 · 192 kbps · Free",
            "placeholder": "Paste a YouTube link...",
            "cta": "Download MP3",
            "tool_format": "mp3",
            "show_cut_options": False,
            "h2": "Free YouTube MP3 downloader",
            "lead": "Yestubers is a fast YouTube MP3 downloader. Paste any link, get the audio track in MP3 format in seconds.",
            "features": [
                {
                    "title": "MP3 audio",
                    "text": "High-quality stereo audio for phones, PCs and players.",
                },
                {
                    "title": "Fast",
                    "text": "Download audio in seconds.",
                },
                {
                    "title": "No account needed",
                    "text": "Try 2 downloads before signing up.",
                },
                {
                    "title": "3 free credits",
                    "text": "Monthly free quota with a registered account.",
                },
            ],
            "steps": [
                "Copy the YouTube URL.",
                "Paste it in the MP3 downloader.",
                "Click Download MP3.",
            ],
            "why": "Yestubers offers a clean YouTube MP3 downloader without pop-ups or hidden redirects.",
            "faqs": [
                {
                    "question": "Is the YouTube MP3 downloader free?",
                    "answer": "Yes, you can download 2 MP3 files without an account and 3 per month for free after signup.",
                },
                {
                    "question": "What quality?",
                    "answer": "MP3 files are saved at 192 kbps stereo.",
                },
                {
                    "question": "Does it work on mobile?",
                    "answer": "Yes, the downloader works on any phone or tablet browser.",
                },
            ],
            "related_links": [
                {
                    "url": "/youtube-to-mp3",
                    "label": "YouTube to MP3",
                },
                {
                    "url": "/convert-youtube-to-mp3",
                    "label": "Convert YouTube to MP3",
                },
                {
                    "url": "/youtube-to-mp4",
                    "label": "YouTube to MP4",
                },
            ],
        },
    "youtube-mp4-downloader": {
            "template": "lp_base.html",
            "slug": "youtube-mp4-downloader",
            "canonical_path": "/youtube-mp4-downloader",
            "title": "YouTube MP4 Downloader — HD Video Free | Yestubers",
            "description": "Download YouTube videos as MP4 files in HD. Free YouTube MP4 downloader with 720p/1080p/4K options. 3 credits per month.",
            "h1": "YouTube MP4 Downloader",
            "subtitle": "Save YouTube videos as MP4 in high definition. No software required.",
            "badge": "MP4 HD · Free · No watermark",
            "placeholder": "Paste a YouTube link...",
            "cta": "Download MP4",
            "tool_format": "mp4",
            "show_cut_options": False,
            "h2": "Best YouTube MP4 downloader",
            "lead": "Yestubers downloads YouTube videos as MP4 files. Choose HD quality and get a clean file without watermark.",
            "features": [
                {
                    "title": "HD quality",
                    "text": "720p, 1080p or 4K depending on your plan.",
                },
                {
                    "title": "Universal MP4",
                    "text": "Works on all devices and video players.",
                },
                {
                    "title": "No watermark",
                    "text": "Clean output file.",
                },
                {
                    "title": "Free credits",
                    "text": "3 free MP4 downloads per month.",
                },
            ],
            "steps": [
                "Copy the YouTube video URL.",
                "Paste it and click Download MP4.",
                "Choose your quality and save the file.",
            ],
            "why": "Yestubers is a reliable YouTube MP4 downloader with high-quality output and no intrusive ads.",
            "faqs": [
                {
                    "question": "Can I download MP4 for free?",
                    "answer": "Yes, 2 free tries without account and 3 credits per month with a free account.",
                },
                {
                    "question": "Is HD available?",
                    "answer": "Yes, HD and 4K are available depending on your plan.",
                },
                {
                    "question": "Are Shorts supported?",
                    "answer": "Yes, YouTube Shorts links work the same as regular videos.",
                },
            ],
            "related_links": [
                {
                    "url": "/youtube-to-mp4",
                    "label": "YouTube to MP4",
                },
                {
                    "url": "/youtube-video-downloader",
                    "label": "YouTube Video Downloader",
                },
                {
                    "url": "/youtube-downloader",
                    "label": "YouTube Downloader",
                },
            ],
        },
    "youtube-shorts-downloader": {
            "template": "lp_base.html",
            "slug": "youtube-shorts-downloader",
            "canonical_path": "/youtube-shorts-downloader",
            "title": "YouTube Shorts Downloader — MP4 Free | Yestubers",
            "description": "Download YouTube Shorts as MP4 for free. Save vertical videos without watermark. 3 free credits per month.",
            "h1": "YouTube Shorts Downloader",
            "subtitle": "Download any YouTube Short in MP4 format, ready to repost.",
            "badge": "Shorts · Vertical · Free",
            "placeholder": "Paste a YouTube Shorts link...",
            "cta": "Download Short",
            "tool_format": "mp4",
            "show_cut_options": False,
            "h2": "Download YouTube Shorts easily",
            "lead": "Yestubers lets you download YouTube Shorts as clean MP4 files. Keep the vertical format for TikTok, Instagram Reels and more.",
            "features": [
                {
                    "title": "Vertical format",
                    "text": "Preserves the original 9:16 aspect ratio.",
                },
                {
                    "title": "No watermark",
                    "text": "Clean MP4 ready to republish.",
                },
                {
                    "title": "Fast",
                    "text": "Short downloads processed quickly.",
                },
                {
                    "title": "Free credits",
                    "text": "3 free Short downloads per month.",
                },
            ],
            "steps": [
                "Copy the YouTube Shorts URL.",
                "Paste it in the downloader field.",
                "Click Download Short and save the MP4.",
            ],
            "why": "Yestubers is the easiest way to download YouTube Shorts for cross-posting to other platforms.",
            "faqs": [
                {
                    "question": "Can I download YouTube Shorts for free?",
                    "answer": "Yes, 2 Short downloads without account and 3 free credits per month after signup.",
                },
                {
                    "question": "Does it keep vertical format?",
                    "answer": "Yes, the original 9:16 format is preserved.",
                },
                {
                    "question": "Is there a watermark?",
                    "answer": "No, downloaded Shorts have no watermark.",
                },
            ],
            "related_links": [
                {
                    "url": "/youtube-to-mp4",
                    "label": "YouTube to MP4",
                },
                {
                    "url": "/youtube-video-downloader",
                    "label": "YouTube Video Downloader",
                },
                {
                    "url": "/youtube-downloader",
                    "label": "YouTube Downloader",
                },
            ],
        },
    "youtube-playlist-downloader": {
            "template": "lp_base.html",
            "slug": "youtube-playlist-downloader",
            "canonical_path": "/youtube-playlist-downloader",
            "title": "YouTube Playlist Downloader — MP3/MP4 Bulk | Yestubers",
            "description": "Download full YouTube playlists as MP3 or MP4. Bulk YouTube playlist downloader for music, podcasts and courses. Premium plans available.",
            "h1": "YouTube Playlist Downloader",
            "subtitle": "Save all videos from a YouTube playlist in one go. MP3 or MP4 output.",
            "badge": "Playlist · Bulk · MP3/MP4",
            "placeholder": "Paste a YouTube playlist link...",
            "cta": "Download Playlist",
            "tool_format": "mp4",
            "show_cut_options": False,
            "h2": "Bulk YouTube playlist downloader",
            "lead": "Yestubers detects YouTube playlists and lets you download all videos as MP3 or MP4. Great for music collections, podcasts and archives.",
            "features": [
                {
                    "title": "Auto-detection",
                    "text": "Playlist links recognized automatically.",
                },
                {
                    "title": "MP3 or MP4",
                    "text": "Choose audio or video output for all items.",
                },
                {
                    "title": "Bulk download",
                    "text": "Download entire playlists without manual clicks.",
                },
                {
                    "title": "Premium plans",
                    "text": "Unlock full playlist downloads with paid plans.",
                },
            ],
            "steps": [
                "Copy the YouTube playlist URL.",
                "Paste it and choose MP3 or MP4.",
                "Click Download Playlist and get your files.",
            ],
            "why": "Yestubers is the best bulk YouTube playlist downloader for saving complete collections.",
            "faqs": [
                {
                    "question": "Can I download playlists for free?",
                    "answer": "Free accounts can preview playlists. Full bulk downloads are available on premium plans.",
                },
                {
                    "question": "What formats?",
                    "answer": "MP3, MP4, M4A and WAV.",
                },
                {
                    "question": "Is there a limit?",
                    "answer": "Playlist limits depend on your plan; Pro plans are unlimited.",
                },
            ],
            "related_links": [
                {
                    "url": "/youtube-to-mp4",
                    "label": "YouTube to MP4",
                },
                {
                    "url": "/youtube-to-mp3",
                    "label": "YouTube to MP3",
                },
                {
                    "url": "/youtube-downloader",
                    "label": "YouTube Downloader",
                },
            ],
        },
    "youtube-audio-downloader": {
            "template": "lp_base.html",
            "slug": "youtube-audio-downloader",
            "canonical_path": "/youtube-audio-downloader",
            "title": "YouTube Audio Downloader — MP3/M4A/WAV Free | Yestubers",
            "description": "Download YouTube audio as MP3, M4A or WAV. Free YouTube audio downloader with high quality. 3 credits per month.",
            "h1": "YouTube Audio Downloader",
            "subtitle": "Extract audio tracks from any YouTube video in MP3, M4A or WAV.",
            "badge": "MP3/M4A/WAV · Free · High quality",
            "placeholder": "Paste a YouTube link...",
            "cta": "Download Audio",
            "tool_format": "mp3",
            "show_cut_options": False,
            "h2": "Free YouTube audio downloader",
            "lead": "Yestubers extracts audio from YouTube videos and saves it as MP3, M4A or WAV. Perfect for offline listening and podcasts.",
            "features": [
                {
                    "title": "Multiple formats",
                    "text": "MP3, M4A and WAV outputs available.",
                },
                {
                    "title": "High quality",
                    "text": "Clear stereo audio at 192 kbps MP3.",
                },
                {
                    "title": "No install",
                    "text": "Works directly in your browser.",
                },
                {
                    "title": "Free credits",
                    "text": "3 free audio downloads per month.",
                },
            ],
            "steps": [
                "Copy the YouTube URL.",
                "Paste it and select audio format.",
                "Click Download Audio.",
            ],
            "why": "Yestubers is a clean YouTube audio downloader for extracting sound from any video.",
            "faqs": [
                {
                    "question": "Can I download audio for free?",
                    "answer": "Yes, 2 free tries and 3 credits per month with a free account.",
                },
                {
                    "question": "Which audio formats?",
                    "answer": "MP3, M4A and WAV.",
                },
                {
                    "question": "What quality?",
                    "answer": "MP3 is extracted at 192 kbps stereo.",
                },
            ],
            "related_links": [
                {
                    "url": "/youtube-to-mp3",
                    "label": "YouTube to MP3",
                },
                {
                    "url": "/youtube-mp3-downloader",
                    "label": "YouTube MP3 Downloader",
                },
                {
                    "url": "/youtube-music-downloader",
                    "label": "YouTube Music Downloader",
                },
            ],
        },
    "youtube-music-downloader": {
            "template": "lp_base.html",
            "slug": "youtube-music-downloader",
            "canonical_path": "/youtube-music-downloader",
            "title": "YouTube Music Downloader — MP3 Free | Yestubers",
            "description": "Download music from YouTube as MP3. Free YouTube music downloader for songs, albums and playlists. 3 credits per month.",
            "h1": "YouTube Music Downloader",
            "subtitle": "Save your favorite YouTube music as high-quality MP3 files.",
            "badge": "Music · MP3 · Free",
            "placeholder": "Paste a YouTube music link...",
            "cta": "Download Music",
            "tool_format": "mp3",
            "show_cut_options": False,
            "h2": "Free YouTube music downloader",
            "lead": "Yestubers turns YouTube music videos into MP3 files. Save songs, covers and albums for offline listening.",
            "features": [
                {
                    "title": "MP3 music",
                    "text": "High-quality audio for your music library.",
                },
                {
                    "title": "Fast",
                    "text": "Download songs in seconds.",
                },
                {
                    "title": "No account",
                    "text": "Try 2 downloads before signing up.",
                },
                {
                    "title": "Free credits",
                    "text": "3 free music downloads per month.",
                },
            ],
            "steps": [
                "Copy the YouTube music video URL.",
                "Paste it in the downloader.",
                "Click Download Music and save.",
            ],
            "why": "Yestubers is a simple YouTube music downloader without ads or redirects.",
            "faqs": [
                {
                    "question": "Can I download YouTube music for free?",
                    "answer": "Yes, 2 free tries and 3 credits per month with a free account.",
                },
                {
                    "question": "What format?",
                    "answer": "MP3, M4A and WAV.",
                },
                {
                    "question": "Is it legal?",
                    "answer": "Only download music you own or that is copyright-free.",
                },
            ],
            "related_links": [
                {
                    "url": "/youtube-to-mp3",
                    "label": "YouTube to MP3",
                },
                {
                    "url": "/youtube-audio-downloader",
                    "label": "YouTube Audio Downloader",
                },
                {
                    "url": "/youtube-mp3-downloader",
                    "label": "YouTube MP3 Downloader",
                },
            ],
        },
    "telecharger-musique-youtube": {
            "template": "lp_base.html",
            "slug": "telecharger-musique-youtube",
            "canonical_path": "/telecharger-musique-youtube",
            "title": "Télécharger musique YouTube — MP3 Gratuit | Yestubers",
            "description": "Téléchargez la musique YouTube en MP3 gratuitement. Convertisseur audio rapide, 3 crédits offerts par mois.",
            "h1": "Télécharger musique YouTube",
            "subtitle": "Sauvegardez vos morceaux YouTube en MP3 haute qualité.",
            "badge": "Musique · MP3 · Gratuit",
            "placeholder": "Collez un lien musique YouTube...",
            "cta": "Télécharger la musique",
            "tool_format": "mp3",
            "show_cut_options": False,
            "h2": "Télécharger musique YouTube en MP3",
            "lead": "Yestubers transforme les vidéos musicales YouTube en fichiers MP3. Idéal pour écouter hors ligne.",
            "features": [
                {
                    "title": "MP3 192 kbps",
                    "text": "Qualité audio adaptée à la musique.",
                },
                {
                    "title": "Rapide",
                    "text": "Conversion en quelques secondes.",
                },
                {
                    "title": "Sans compte",
                    "text": "2 essais gratuits sans inscription.",
                },
                {
                    "title": "3 crédits gratuits",
                    "text": "Par mois avec un compte gratuit.",
                },
            ],
            "steps": [
                "Copiez le lien de la vidéo musique YouTube.",
                "Collez-le dans le champ.",
                "Cliquez sur Télécharger la musique.",
            ],
            "why": "Yestubers est le moyen le plus simple de télécharger la musique YouTube en MP3.",
            "faqs": [
                {
                    "question": "Puis-je télécharger la musique YouTube gratuitement ?",
                    "answer": "Oui, 2 essais sans compte et 3 crédits gratuits par mois.",
                },
                {
                    "question": "Quel format audio ?",
                    "answer": "MP3, M4A et WAV.",
                },
                {
                    "question": "Est-ce légal ?",
                    "answer": "Téléchargez uniquement la musique dont vous possédez les droits ou libre de droits.",
                },
            ],
            "related_links": [
                {
                    "url": "/convertisseur-youtube-mp3",
                    "label": "Convertisseur YouTube MP3",
                },
                {
                    "url": "/telecharger-mp3-youtube",
                    "label": "Télécharger MP3 YouTube",
                },
                {
                    "url": "/youtube-to-mp3",
                    "label": "YouTube to MP3",
                },
            ],
        },
    "convertisseur-youtube-mp4": {
            "template": "lp_base.html",
            "slug": "convertisseur-youtube-mp4",
            "canonical_path": "/convertisseur-youtube-mp4",
            "title": "Convertisseur YouTube MP4 — Téléchargement HD Gratuit | Yestubers",
            "description": "Convertissez des vidéos YouTube en MP4 HD. Convertisseur YouTube MP4 gratuit et rapide. 3 crédits offerts par mois.",
            "h1": "Convertisseur YouTube MP4",
            "subtitle": "Transformez n'importe quelle vidéo YouTube en fichier MP4 haute définition.",
            "badge": "MP4 HD · Gratuit · Sans logiciel",
            "placeholder": "Collez un lien YouTube ici...",
            "cta": "Convertir en MP4",
            "tool_format": "mp4",
            "show_cut_options": False,
            "h2": "Le convertisseur YouTube MP4 en français",
            "lead": "Yestubers est le convertisseur YouTube MP4 le plus simple. Collez le lien, choisissez la qualité et téléchargez votre MP4.",
            "features": [
                {
                    "title": "MP4 HD",
                    "text": "Qualité 720p, 1080p ou 4K selon votre plan.",
                },
                {
                    "title": "Sans logiciel",
                    "text": "Tout se passe dans le navigateur.",
                },
                {
                    "title": "Sans publicité",
                    "text": "Interface propre et rapide.",
                },
                {
                    "title": "3 crédits gratuits",
                    "text": "Par mois avec un compte gratuit.",
                },
            ],
            "steps": [
                "Copiez le lien de la vidéo YouTube.",
                "Collez-le dans le convertisseur.",
                "Cliquez sur Convertir en MP4 et téléchargez.",
            ],
            "why": "Yestubers est le convertisseur YouTube MP4 français le plus rapide, optimisé mobile et desktop.",
            "faqs": [
                {
                    "question": "Le convertisseur YouTube MP4 est-il gratuit ?",
                    "answer": "Oui, 2 essais sans compte et 3 crédits gratuits par mois.",
                },
                {
                    "question": "Puis-je convertir en HD ?",
                    "answer": "Oui, jusqu'à 4K selon votre plan.",
                },
                {
                    "question": "Quels formats ?",
                    "answer": "MP4 pour la vidéo, MP3/M4A/WAV pour l'audio.",
                },
            ],
            "related_links": [
                {
                    "url": "/youtube-to-mp4",
                    "label": "YouTube to MP4",
                },
                {
                    "url": "/telecharger-video-youtube",
                    "label": "Télécharger vidéo YouTube",
                },
                {
                    "url": "/youtube-downloader",
                    "label": "YouTube Downloader",
                },
            ],
        },
    "telecharger-mp3-youtube": {
            "template": "lp_base.html",
            "slug": "telecharger-mp3-youtube",
            "canonical_path": "/telecharger-mp3-youtube",
            "title": "Télécharger MP3 YouTube — Gratuit | Yestubers",
            "description": "Téléchargez des vidéos YouTube en MP3 gratuitement. MP3 192 kbps, sans logiciel, 3 crédits offerts par mois.",
            "h1": "Télécharger MP3 YouTube",
            "subtitle": "Obtenez l'audio de n'importe quelle vidéo YouTube en MP3 en quelques secondes.",
            "badge": "MP3 · Gratuit · Sans logiciel",
            "placeholder": "Collez un lien YouTube ici...",
            "cta": "Télécharger MP3",
            "tool_format": "mp3",
            "show_cut_options": False,
            "h2": "Télécharger MP3 YouTube gratuit",
            "lead": "Yestubers est le moyen le plus rapide de télécharger l'audio YouTube en MP3. Collez le lien, cliquez, téléchargez.",
            "features": [
                {
                    "title": "MP3 192 kbps",
                    "text": "Qualité audio optimale pour tous les appareils.",
                },
                {
                    "title": "Sans installation",
                    "text": "Tout se fait en ligne.",
                },
                {
                    "title": "Sans publicité",
                    "text": "Interface épurée.",
                },
                {
                    "title": "3 crédits gratuits",
                    "text": "Par mois avec inscription gratuite.",
                },
            ],
            "steps": [
                "Copiez l'URL de la vidéo YouTube.",
                "Collez-la dans le champ de téléchargement.",
                "Cliquez sur Télécharger MP3.",
            ],
            "why": "Yestubers est le téléchargeur MP3 YouTube le plus simple : rapide, propre et sécurisé.",
            "faqs": [
                {
                    "question": "Puis-je télécharger MP3 YouTube gratuitement ?",
                    "answer": "Oui, 2 essais sans compte et 3 crédits gratuits par mois.",
                },
                {
                    "question": "Quelle qualité MP3 ?",
                    "answer": "192 kbps stéréo, compatible tous lecteurs.",
                },
                {
                    "question": "Est-ce légal ?",
                    "answer": "Téléchargez uniquement le contenu dont vous possédez les droits ou libre de droits.",
                },
            ],
            "related_links": [
                {
                    "url": "/convertisseur-youtube-mp3",
                    "label": "Convertisseur YouTube MP3",
                },
                {
                    "url": "/telecharger-musique-youtube",
                    "label": "Télécharger musique YouTube",
                },
                {
                    "url": "/youtube-to-mp3",
                    "label": "YouTube to MP3",
                },
            ],
        },
}


for _lp_slug, _lp_data in LP_PAGES.items():
    _lp_template = _lp_data["template"]

    @app.get(f"/{_lp_slug}", response_class=HTMLResponse)
    async def _lp_page(request: Request, lp_slug: str = _lp_slug, db: Session = Depends(get_db)):
        if lp_slug not in LP_PAGES:
            raise HTTPException(status_code=404)
        user = get_user_from_session(request, db)
        i18n = translate_dict(request)
        data = LP_PAGES[lp_slug]
        return HTMLResponse(_jinja_env.get_template(data["template"]).render(
            request=request, user=user, i18n=i18n, locale=i18n["_locale"], **data
        ))

for _lang in SUPPORTED_LOCALES:
    for _lp_slug, _lp_data in LP_PAGES.items():
        _lp_template = _lp_data["template"]

        @app.get(f"/{_lang}/{_lp_slug}", response_class=HTMLResponse)
        async def _lp_localized(request: Request, lang: str = _lang, lp_slug: str = _lp_slug, db: Session = Depends(get_db)):
            if lp_slug not in LP_PAGES:
                raise HTTPException(status_code=404)
            request._forced_locale = lang
            user = get_user_from_session(request, db)
            i18n = translate_dict(request)
            data = LP_PAGES[lp_slug]
            return HTMLResponse(_jinja_env.get_template(data["template"]).render(
                request=request, user=user, i18n=i18n, locale=i18n["_locale"], **data
            ))

# Legacy SEO tool pages still served from SEO_PAGES dict
@app.get("/{tool}", response_class=HTMLResponse)
async def seo_tool_page(request: Request, tool: str, db: Session = Depends(get_db)):
    if tool not in SEO_PAGES:
        raise HTTPException(status_code=404)
    user = get_user_from_session(request, db)
    i18n = translate_dict(request)
    data = SEO_PAGES[tool]
    return HTMLResponse(_jinja_env.get_template("seo_tool.html").render(
        request=request, user=user, i18n=i18n, locale=i18n["_locale"],
        tool=tool, **data
    ))

# Localized SEO tool pages
for _lang in SUPPORTED_LOCALES:
    for _tool in SEO_PAGES:
        @app.get(f"/{_lang}/{_tool}", response_class=HTMLResponse)
        async def _seo_localized(request: Request, lang: str = _lang, tool_key: str = _tool, db: Session = Depends(get_db)):
            request._forced_locale = lang
            if tool_key not in SEO_PAGES:
                raise HTTPException(status_code=404)
            user = get_user_from_session(request, db)
            i18n = translate_dict(request)
            data = SEO_PAGES[tool_key]
            return HTMLResponse(_jinja_env.get_template("seo_tool.html").render(
                request=request, user=user, i18n=i18n, locale=i18n["_locale"],
                tool=tool_key, **data
            ))

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    if not user or user.email != "admin@yestubers.cloud":
        return RedirectResponse("/login")
    return HTMLResponse(_jinja_env.get_template("admin.html").render(request=request, user=user))


# Localized public pages
for _lang in SUPPORTED_LOCALES:
    @app.get(f"/{_lang}/pricing", response_class=HTMLResponse)
    async def _pricing_localized(request: Request, lang: str = _lang, db: Session = Depends(get_db)):
        return await pricing(request, db)

    @app.get(f"/{_lang}/about", response_class=HTMLResponse)
    async def _about_localized(request: Request, lang: str = _lang, db: Session = Depends(get_db)):
        request._forced_locale = lang
        return await about_page(request, db)

    @app.get(f"/{_lang}/contact", response_class=HTMLResponse)
    async def _contact_localized(request: Request, lang: str = _lang, db: Session = Depends(get_db)):
        request._forced_locale = lang
        return await contact_page(request, db=db)

    @app.get(f"/{_lang}/terms", response_class=HTMLResponse)
    async def _terms_localized(request: Request, lang: str = _lang, db: Session = Depends(get_db)):
        request._forced_locale = lang
        return await terms_page(request, db)

    @app.get(f"/{_lang}/privacy", response_class=HTMLResponse)
    async def _privacy_localized(request: Request, lang: str = _lang, db: Session = Depends(get_db)):
        request._forced_locale = lang
        return await privacy_page(request, db)

    @app.get(f"/{_lang}/dmca", response_class=HTMLResponse)
    async def _dmca_localized(request: Request, lang: str = _lang, db: Session = Depends(get_db)):
        request._forced_locale = lang
        return await dmca_page(request, db)


# ─── Routes Web ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
@app.head("/", response_class=HTMLResponse)
async def home(request: Request, db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    i18n = translate_dict(request)
    total_conversions = get_total_conversions()
    response = HTMLResponse(_jinja_env.get_template("index.html").render(
        request=request, user=user, stripe_pk=STRIPE_PK, i18n=i18n, locale=i18n["_locale"], total_conversions=total_conversions))
    if request.query_params.get("lang"):
        response.set_cookie("locale", i18n["_locale"], max_age=365*24*3600)
    return response

# Global conversion counter (videos + cuts, cached and seeded)
_conversions_cache: dict[str, any] = {"value": None, "expires": 0}

def get_total_conversions() -> int:
    now = time.time()
    if _conversions_cache["expires"] > now:
        return _conversions_cache["value"]
    try:
        with SessionLocal() as db:
            video_count = db.execute(text("SELECT COUNT(*) FROM videos WHERE status='done'")).scalar() or 0
            cut_count = db.execute(text("SELECT COUNT(*) FROM cuts WHERE status='done'")).scalar() or 0
        total = max(2_000_000, int(video_count) + int(cut_count) + 1_847_293)
    except Exception:
        total = 2_000_000
    _conversions_cache["value"] = total
    _conversions_cache["expires"] = now + 60
    return total

# ─── Health + Cleanup endpoint ─────────────────────────────────────────────────

@app.get("/health")
async def health():
    # Light check: count users and ensure DB readable
    try:
        with SessionLocal() as db:
            user_count = db.execute(text("SELECT COUNT(*) FROM users")).scalar()
    except Exception as e:
        raise HTTPException(503, f"db_error: {e}")
    return {
        "status": "ok",
        "service": "yestubers",
        "version": "10",
        "db_users": user_count,
        "time": datetime.utcnow().isoformat(),
    }

@app.post("/api/admin/cleanup")
async def admin_cleanup(request: Request, hours: int = 24, db: Session = Depends(get_db)):
    # Simple secret-key gate via query param or header (not a real admin auth)
    user = get_user_from_session(request, db)
    if not user or user.email != "admin@yestubers.cloud":
        raise HTTPException(403, "Admin only")
    removed = cleanup_old_files(hours)
    return {"removed": removed}

# ─── API Auth ─────────────────────────────────────────────────────────────────

@app.post("/api/auth/signup")
async def api_signup(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    check_rate_limit(request, "auth")
    email = email.strip().lower()
    if not re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", email):
        raise HTTPException(400, "Adresse email invalide")
    if len(password) < 8:
        raise HTTPException(400, "Le mot de passe doit contenir au moins 8 caractères")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(400, "Email déjà utilisé")

    user = User(email=email, password_hash=hash_pw(password), credits=3, plan="free")
    # Generate unique referral code
    while True:
        code = secrets.token_urlsafe(8)[:10].upper().replace('-', '').replace('_', '')
        if not db.query(User).filter(User.referral_code == code).first():
            break
    user.referral_code = code

    # Apply referral reward: +3 credits for both referrer and new user
    ref_cookie = request.cookies.get("ref") or ""
    ref_param = request.query_params.get("ref") or ""
    ref_field = ref_cookie or ref_param
    # Prefer explicit form field if present
    body_bytes = b""
    try:
        body_bytes = await request.body()
    except Exception:
        pass
    if body_bytes:
        from urllib.parse import parse_qs
        parsed = parse_qs(body_bytes.decode("utf-8", errors="ignore"))
        if "ref" in parsed and parsed["ref"][0]:
            ref_field = parsed["ref"][0]
    if ref_field:
        try:
            referrer = db.query(User).filter(User.referral_code == ref_field.strip().upper()).first()
            if referrer and referrer.id != user.id:
                user.referrer_id = referrer.id
                user.credits += 3
                referrer.credits = min(1000, referrer.credits + 3)
                db.add(referrer)
        except Exception:
            pass

    db.add(user)
    db.commit()
    db.refresh(user)

    token = generate_email_verification_token(user, db)
    send_verification_email(user, token)

    response = JSONResponse({"ok": True, "message": "Compte créé. Vérifiez votre email pour débloquer toutes les fonctionnalités.", "email_verified": False, "referral_code": user.referral_code})
    response.set_cookie("session", _sign_session(user.id), httponly=True, max_age=30*24*3600, samesite="lax", secure=True)
    response.delete_cookie("ref")
    return response

@app.get("/api/referral/lookup/{code}")
async def referral_lookup(code: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.referral_code == code.strip().upper()).first()
    if not user:
        raise HTTPException(404, "Code invalide")
    return {"ok": True, "referral_code": user.referral_code, "referral_link": f"https://yestubers.cloud/signup?ref={user.referral_code}"}

@app.get("/affiliate")
async def affiliate_page(request: Request, db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    i18n = translate_dict(request)
    return HTMLResponse(_jinja_env.get_template("affiliate.html").render(
        request=request, user=user, i18n=i18n, locale=i18n["_locale"]
    ))

@app.post("/api/auth/login")
async def api_login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    check_rate_limit(request, "auth")
    email = email.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_pw(password, user.password_hash):
        raise HTTPException(401, "Email ou mot de passe incorrect")
    # Auto-upgrade legacy SHA256 hashes to bcrypt
    if not user.password_hash.startswith("bcrypt$"):
        user.password_hash = hash_pw(password)
        db.commit()

    response = JSONResponse({"ok": True, "message": "Connecté !", "email_verified": bool(user.email_verified)})
    response.set_cookie("session", _sign_session(user.id), httponly=True, max_age=30*24*3600, samesite="lax", secure=True)
    return response

@app.post("/api/auth/logout")
async def api_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("session", secure=True)
    return response

@app.get("/verify-email")
async def verify_email(request: Request, token: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(
        User.email_verification_token == token,
        User.email_verification_expires > datetime.utcnow()
    ).first()
    if not user:
        return HTMLResponse(_jinja_env.get_template("verify-email.html").render(
            request=request, success=False, message="Lien invalide ou expiré.", locale="fr", i18n={}
        ))
    user.email_verified = True
    user.email_verification_token = None
    user.email_verification_expires = None
    db.commit()
    return HTMLResponse(_jinja_env.get_template("verify-email.html").render(
        request=request, success=True, message="Email confirmé avec succès.", locale="fr", i18n={}
    ))

@app.post("/api/auth/resend-verification")
async def api_resend_verification(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    check_rate_limit(request, "auth")
    if user.email_verified:
        return {"ok": True, "message": "Email déjà vérifié."}
    token = generate_email_verification_token(user, db)
    sent = send_verification_email(user, token)
    return {"ok": sent, "message": "Email de vérification renvoyé." if sent else "Erreur d'envoi."}

@app.post("/api/auth/forgot")
async def api_forgot(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    check_rate_limit(request, "auth")
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"ok": True, "message": "Si l'email existe, un lien a été envoyé."}

    token = secrets.token_urlsafe(32)
    user.reset_token = token
    user.reset_expires = datetime.utcnow() + timedelta(hours=1)
    db.commit()

    reset_url = f"https://yestubers.cloud/reset-password?token={token}"
    body = f"""<p>Bonjour,</p>
<p>Vous avez demandé à réinitialiser votre mot de passe Yestubers.</p>
<p><a href="{reset_url}">{reset_url}</a></p>
<p>Ce lien expire dans 1 heure.</p>
<p>Si vous n'êtes pas à l'origine de cette demande, ignorez cet email.</p>
"""
    sent = send_email(user.email, "Réinitialisation de votre mot de passe Yestubers", body)
    return {"ok": True, "message": "Si l'email existe, un lien a été envoyé.", "sent": sent}

@app.post("/api/auth/reset")
async def api_reset(request: Request, token: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    check_rate_limit(request, "auth")
    user = db.query(User).filter(
        User.reset_token == token,
        User.reset_expires > datetime.utcnow()
    ).first()
    if not user:
        raise HTTPException(400, "Token invalide ou expiré")

    user.password_hash = hash_pw(password)
    user.reset_token = None
    user.reset_expires = None
    db.commit()
    return {"ok": True, "message": "Mot de passe réinitialisé !"}


# ─── Public anonymous download (clone noTube style) ───────────────────────────

@app.get("/api/info")
async def api_info(url: str, request: Request):
    try:
        info = get_video_info(url)
        formats = []
        # build format list for UI
        for f in info.get("formats", []):
            if f.get("vcodec") != "none" and f.get("acodec") != "none":
                formats.append({"format_id": f["format_id"], "ext": f["ext"], "quality": f.get("quality_label") or f.get("height"), "note": "video+audio"})
            elif f.get("acodec") != "none":
                formats.append({"format_id": f["format_id"], "ext": f["ext"], "quality": "audio", "note": "audio only"})
        return {
            "title": info.get("title"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"),
            "formats": formats[:12]
        }
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/api/download")
async def api_public_download(
    request: Request,
    url: str = Form(...),
    format: str = Form("mp4"),
    quality: str = Form("720"),
):
    check_rate_limit(request, "download")
    if not is_valid_youtube_url(url):
        raise HTTPException(400, "URL YouTube invalide")
    # Audio/HD are premium; anonymous teaser only allows 360p video MP4.
    if format.lower() not in ("mp4", ""):
        raise HTTPException(403, "signup_required")
    quality = quality if quality in ("360", "240", "144") else ANON_MAX_QUALITY
    ip = _anon_ip(request)
    _check_anon_limit(ip)
    try:
        data = await download_public(url, format, quality, max_duration=120)
        remaining = _anon_remaining(ip)
        return {
            "ok": True,
            "video_id": data["video_id"],
            "title": data["title"],
            "duration": data["duration"],
            "filesize": data["filesize"],
            "download_url": f"/api/download/{data['video_id']}/file",
            "remaining_anonymous": remaining,
            "upgrade_required": remaining == 0
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))

@app.get("/api/download/{video_id}/file")
async def api_public_download_file(
    video_id: str,
    background_tasks: BackgroundTasks,
):
    candidates = [p for p in VIDEOS.iterdir() if p.stem == video_id]
    if not candidates:
        raise HTTPException(404, "Fichier expiré ou introuvable")
    path = candidates[0]
    background_tasks.add_task(_delete_file_soon, [str(path)])
    return FileResponse(str(path), filename=path.name, media_type="application/octet-stream")

# ─── API Videos ───────────────────────────────────────────────────────────────

@app.post("/api/videos/download")
async def api_download(
    request: Request,
    url: str = Form(...),
    quality: Optional[str] = Form(None),
    format: Optional[str] = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    check_rate_limit(request, "download")
    if not is_valid_youtube_url(url):
        raise HTTPException(400, "URL YouTube invalide")
    check_credits(user)

    video_id = extract_video_id(url)
    if not video_id:
        raise HTTPException(400, "URL YouTube invalide")

    max_dur = PLANS[user.plan]["max_duration"]
    vid_db = str(uuid.uuid4())

    video = Video(
        id=vid_db, user_id=user.id, youtube_url=url, status="processing"
    )
    db.add(video)
    db.commit()

    try:
        allowed_qualities = PLANS[user.plan].get("quality_options", [PLANS[user.plan]["quality"]])
        chosen_quality = quality if quality in allowed_qualities else PLANS[user.plan]["quality"]

        # Normalize frontend format tokens
        fmt_token = (format or "mp4").lower().replace("audio/", "").replace("video/", "")
        quality_map = {"mp4": chosen_quality, "mp4-hd": "720", "mp4-hd1080": "1080", "mp4-2k": "1440", "mp4-4k": "2160"}
        audio_formats = {"mp3", "mp3-hd", "m4a", "wav"}
        video_formats = {"mp4", "video"}
        if fmt_token in quality_map:
            chosen_quality = quality_map[fmt_token]
            fmt = "mp4"
        elif fmt_token in audio_formats:
            fmt = "mp3" if fmt_token.startswith("mp3") else ("m4a" if fmt_token == "m4a" else "wav")
            chosen_quality = quality if quality in allowed_qualities else PLANS[user.plan]["quality"]
        elif fmt_token in video_formats or "video" in fmt_token or "mp4" in fmt_token:
            fmt = "mp4"
            chosen_quality = quality if quality in allowed_qualities else PLANS[user.plan]["quality"]
        else:
            fmt = "mp4"
            chosen_quality = quality if quality in allowed_qualities else PLANS[user.plan]["quality"]

        # Clamp chosen_quality to plan limits
        if chosen_quality not in allowed_qualities:
            chosen_quality = PLANS[user.plan]["quality"]

        info = await download_video(url, vid_db, max_dur, chosen_quality)
        video.title = info.get("title", "Sans titre")
        video.duration = info["duration"]
        video.filename = info["filename"]
        video.filesize = info["filesize"]
        video.thumbnail = info["thumbnail"]
        video.status = "done"
        use_credit(user, db)

        # Try to get real title
        try:
            meta = get_video_info(url)
            video.title = meta.get("title", "Sans titre")
        except:
            video.title = f"Video {vid_db[:8]}"

        # Convert to audio if requested
        if fmt in ("mp3", "m4a", "wav"):
            src = VIDEOS / video.filename
            ext = "mp3" if fmt == "mp3" else ("m4a" if fmt == "m4a" else "wav")
            audio_path = VIDEOS / f"{vid_db}.{ext}"
            audio_codec = "libmp3lame" if fmt == "mp3" else ("aac" if fmt == "m4a" else "pcm_s16le")
            conv = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                "-vn", "-c:a", audio_codec, "-b:a", "192k", str(audio_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, err = await conv.communicate()
            if conv.returncode != 0:
                raise Exception(f"Erreur conversion audio: {err.decode()[:200]}")
            src.unlink(missing_ok=True)
            video.filename = audio_path.name
            video.filesize = audio_path.stat().st_size

        db.commit()
        return {"ok": True, "video_id": vid_db, "title": video.title, "duration": video.duration,
                "filesize": video.filesize, "download_url": f"/api/videos/{vid_db}/download",
                "remaining_credits": user.credits}

    except Exception as e:
        video.status = "error"
        db.commit()
        raise HTTPException(400, str(e))

@app.get("/api/videos/{video_id}")
async def api_video_info(
    video_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    video = db.query(Video).filter(Video.id == video_id, Video.user_id == user.id).first()
    if not video:
        raise HTTPException(404, "Vidéo non trouvée")
    return {
        "id": video.id, "title": video.title, "duration": video.duration,
        "status": video.status, "filesize": video.filesize,
        "has_thumbnail": bool(video.thumbnail),
    }

@app.get("/api/videos/{video_id}/download")
async def api_video_download_file(
    video_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    video = db.query(Video).filter(Video.id == video_id, Video.user_id == user.id).first()
    if not video or video.status != "done":
        raise HTTPException(404, "Vidéo non trouvée")
    path = VIDEOS / video.filename
    if not path.exists():
        raise HTTPException(410, "Le fichier a déjà été effacé du serveur. Seul l'historique est conservé.")

    files_to_delete = [str(path)]

    def _cleanup():
        _delete_file_soon(files_to_delete)
        try:
            video.status = "expired"
            video.filename = ""
            db.commit()
        except Exception:
            pass

    background_tasks.add_task(_cleanup)
    ext = path.suffix.lstrip('.') or "mp4"
    return FileResponse(str(path), filename=f"{video.title or 'video'}.{ext}")

@app.delete("/api/videos/{video_id}")
async def api_video_delete(
    video_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    video = db.query(Video).filter(Video.id == video_id, Video.user_id == user.id).first()
    if not video:
        raise HTTPException(404)
    (VIDEOS / video.filename).unlink(missing_ok=True)
    db.delete(video)
    db.commit()
    return {"ok": True}

# ─── API Cuts ─────────────────────────────────────────────────────────────────

@app.post("/api/cuts/create")
async def api_cut_create(
    request: Request,
    video_id: str = Form(...),
    start_time: float = Form(...),
    end_time: float = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    check_rate_limit(request, "download")
    check_credits(user)

    video = db.query(Video).filter(Video.id == video_id, Video.user_id == user.id).first()
    if not video or video.status != "done":
        raise HTTPException(404, "Vidéo source non trouvée")

    if user.plan not in ("creator", "pro"):
        raise HTTPException(402, "La découpe vidéo est incluse dans les formules Creator et Pro. Passez à l'une d'elles pour découper.")

    if start_time < 0 or end_time > video.duration or start_time >= end_time:
        raise HTTPException(400, "Timestamps invalides")

    cut_id = str(uuid.uuid4())
    cut = Cut(id=cut_id, user_id=user.id, video_id=video_id,
              start_time=start_time, end_time=end_time, status="processing")
    db.add(cut)
    db.commit()

    try:
        input_path = VIDEOS / video.filename
        out = cut_video(input_path, start_time, end_time, cut_id)
        cut.filename = out.name
        cut.filesize = out.stat().st_size
        cut.status = "done"
        use_credit(user, db)
        db.commit()
        return {"ok": True, "cut_id": cut_id, "filesize": cut.filesize}
    except Exception as e:
        cut.status = "error"
        db.commit()
        raise HTTPException(400, str(e))

@app.get("/api/cuts/{cut_id}/download")
async def api_cut_download(
    cut_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    cut = db.query(Cut).filter(Cut.id == cut_id, Cut.user_id == user.id).first()
    if not cut or cut.status != "done":
        raise HTTPException(404)
    path = CUTS / cut.filename
    if not path.exists():
        raise HTTPException(410, "Le fichier a déjà été effacé du serveur. Seul l'historique est conservé.")
    background_tasks.add_task(_delete_file_soon, [str(path)])
    return FileResponse(str(path), filename=f"cut_{cut_id[:8]}.mp4")

@app.delete("/api/cuts/{cut_id}")
async def api_cut_delete(
    cut_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    cut = db.query(Cut).filter(Cut.id == cut_id, Cut.user_id == user.id).first()
    if not cut:
        raise HTTPException(404)
    (CUTS / cut.filename).unlink(missing_ok=True)
    db.delete(cut)
    db.commit()
    return {"ok": True}

# ─── Stripe ───────────────────────────────────────────────────────────────────

# Tripwire promo: coupon applied for 1€ first month on Starter monthly subscription.
# The coupon is created/retrieved lazily and cached.
_starter_trial_coupon_id: str | None = None

async def _get_starter_trial_coupon_id() -> str | None:
    global _starter_trial_coupon_id
    if _starter_trial_coupon_id:
        return _starter_trial_coupon_id
    if not stripe:
        return None
    env_id = os.environ.get("STRIPE_STARTER_PROMO_COUPON_ID")
    if env_id:
        try:
            await asyncio.to_thread(stripe.Coupon.retrieve, env_id)
            _starter_trial_coupon_id = env_id
            return env_id
        except Exception as e:
            print(f"[stripe coupon] env coupon {env_id} not usable: {e}")
    try:
        # Try to retrieve an existing coupon by name; if not present create one.
        coupons = await asyncio.to_thread(stripe.Coupon.list, limit=10)
        for c in coupons.auto_paging_iter():
            if c.get("name") == "Starter 1€ premier mois":
                _starter_trial_coupon_id = c.id
                return c.id
        coupon = await asyncio.to_thread(
            stripe.Coupon.create,
            id="STARTER_1E_FIRST_MONTH",
            name="Starter 1€ premier mois",
            amount_off=199,  # 2.99€ - 1.00€ = 1.99€
            currency="eur",
            duration="once",
        )
        _starter_trial_coupon_id = coupon.id
        return coupon.id
    except Exception as e:
        print(f"[stripe coupon] could not create/retrieve: {e}")
        return None


@app.post("/api/stripe/create-checkout")
async def stripe_checkout(
    request: Request,
    plan: str = Form(...),
    billing: str = Form("annual"),
    promo: str = Form(""),  # "1e" triggers the Starter tripwire offer
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    check_rate_limit(request, "auth")
    if plan not in PLANS or plan == "free":
        raise HTTPException(400, "Plan invalide")
    if billing not in ("monthly", "annual"):
        raise HTTPException(400, "Billing invalide")

    if not STRIPE_ENABLED:
        # Autonomous/free mode: immediately upgrade the user without payment
        user.plan = plan
        user.credits = PLANS[plan]["credits"]
        db.commit()
        return {"ok": True, "mode": "autonomous", "message": f"Mode autonome : plan {plan} activé. {PLANS[plan]['credits']} crédits attribués.", "redirect_url": "/dashboard"}

    plan_data = PLANS[plan]
    price_field = "stripe_annual_price_id" if billing == "annual" else "stripe_price_id"
    # For the tripwire, monthly Starter base price is 2.99€. Stripe product price uses that.
    amount = round(plan_data["monthly_price"] * 12, 2) if billing == "annual" else plan_data["monthly_price"]

    if not user.stripe_customer_id:
        try:
            customer = await asyncio.to_thread(
                stripe.Customer.create,
                email=user.email,
                metadata={"user_id": user.id},
            )
        except Exception as e:
            raise HTTPException(503, f"Stripe indisponible : {str(e)[:120]}")
        user.stripe_customer_id = customer.id
        db.commit()

    if not plan_data.get(price_field):
        suffix = " (Annuel)" if billing == "annual" else " (Mensuel)"
        try:
            product = await asyncio.to_thread(
                stripe.Product.create,
                name=f"Yestubers {plan_data['name']}{suffix}",
            )
            interval = "year" if billing == "annual" else "month"
            price = await asyncio.to_thread(
                stripe.Price.create,
                product=product.id,
                unit_amount=int(amount * 100),
                currency="eur",
                recurring={"interval": interval},
            )
        except Exception as e:
            raise HTTPException(503, f"Stripe indisponible : {str(e)[:120]}")
        plan_data[price_field] = price.id

    discounts = None
    if plan == "starter" and billing == "monthly" and promo.lower() in ("1e", "1€", "1eur", "tripwire", "starter"):
        coupon_id = await _get_starter_trial_coupon_id()
        if coupon_id:
            discounts = [{"coupon": coupon_id}]

    try:
        session_token = secrets.token_urlsafe(16)
        kwargs = dict(
            customer=user.stripe_customer_id,
            payment_method_types=["card"],
            line_items=[{"price": plan_data[price_field], "quantity": 1}],
            mode="subscription",
            success_url=f"https://yestubers.cloud/dashboard?session={session_token}",
            cancel_url="https://yestubers.cloud/pricing",
            metadata={"user_id": user.id, "plan": plan, "billing": billing, "token": session_token, "promo": promo},
            subscription_data={"metadata": {"plan": plan, "promo": promo}},
        )
        if discounts:
            kwargs["discounts"] = discounts
        session = await asyncio.to_thread(stripe.checkout.Session.create, **kwargs)
        return {"url": session.url, "promo_applied": bool(discounts)}
    except Exception as e:
        raise HTTPException(503, f"Stripe indisponible : {str(e)[:120]}")

@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    check_rate_limit(request, "default")
    if not STRIPE_ENABLED:
        raise HTTPException(503, "Stripe n'est pas configuré.")
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK)
    except:
        raise HTTPException(400, "Signature invalide")

    event_type = event["type"]

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session["metadata"].get("user_id")
        plan = session["metadata"].get("plan")
        user = db.query(User).filter(User.id == user_id).first()
        if user and plan in PLANS:
            user.plan = plan
            user.credits = PLANS[plan]["credits"]
            user.stripe_sub_id = session.get("subscription")
            user.stripe_customer_id = session.get("customer") or user.stripe_customer_id
            db.commit()

    elif event_type == "customer.subscription.updated":
        sub = event["data"]["object"]
        user = db.query(User).filter(User.stripe_sub_id == sub["id"]).first()
        if user:
            if sub["status"] in ("active", "trialing"):
                # make sure the paid plan stays active (Stripe is source of truth)
                pass
            elif sub["status"] in ("past_due", "unpaid", "paused"):
                # keep plan but prevent new credits usage? mark flag
                pass
            elif sub["status"] == "canceled" and sub.get("canceled_at"):
                user.plan = "free"
                user.credits = PLANS["free"]["credits"]
                user.stripe_sub_id = None
                db.commit()

    elif event_type == "customer.subscription.deleted":
        sub = event["data"]["object"]
        user = db.query(User).filter(User.stripe_sub_id == sub["id"]).first()
        if user:
            user.plan = "free"
            user.credits = PLANS["free"]["credits"]
            user.stripe_sub_id = None
            db.commit()

    elif event_type == "invoice.payment_failed":
        invoice = event["data"]["object"]
        sub_id = invoice.get("subscription")
        if sub_id:
            user = db.query(User).filter(User.stripe_sub_id == sub_id).first()
            if user:
                # do not switch plan immediately; Stripe will retry. Send a warning only.
                try:
                    await asyncio.to_thread(
                        send_email,
                        user.email,
                        "Paiement refusé — Yestubers",
                        f"Bonjour,{user.email}\n\nVotre paiement Stripe pour l'abonnement Yestubers a échoué. "
                        "Stripe va automatiquement réessayer. Pensez à mettre à jour votre moyen de paiement.",
                    )
                except Exception:
                    pass

    elif event_type == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        sub_id = invoice.get("subscription")
        if sub_id:
            user = db.query(User).filter(User.stripe_sub_id == sub_id).first()
            if user and user.plan in PLANS:
                # replenish credits to paid-plan allowance on each renewal
                user.credits = PLANS[user.plan]["credits"]
                db.commit()

    return {"ok": True}


@app.post("/api/stripe/test-checkout")
async def stripe_test_checkout(
    request: Request,
    plan: str = Form(...),
    billing: str = Form("annual"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    """Mode test interne : simule un achat Stripe et met à jour le plan."""
    check_rate_limit(request, "auth")
    if plan not in PLANS or plan == "free":
        raise HTTPException(400, "Plan invalide")
    if billing not in ("monthly", "annual"):
        raise HTTPException(400, "Billing invalide")
    if not os.environ.get("STRIPE_TEST_MODE", "").lower() in ("1", "true", "yes"):
        raise HTTPException(403, "Le mode test Stripe est désactivé")

    # Simulate webhook checkout.session.completed
    user.plan = plan
    user.credits = PLANS[plan]["credits"]
    user.stripe_sub_id = f"sub_test_{secrets.token_hex(8)}"
    db.commit()

    return {
        "ok": True,
        "message": f"Mode test : plan {plan} ({billing}) activé. {PLANS[plan]['credits']} crédits attribués.",
        "plan": plan,
        "credits": user.credits,
        "redirect": "/dashboard"
    }


@app.post("/api/stripe/webhook-test")
async def stripe_webhook_test(request: Request, db: Session = Depends(get_db)):
    """Endpoint de test pour simuler un webhook Stripe depuis le dashboard."""
    check_rate_limit(request, "default")
    if not os.environ.get("STRIPE_TEST_MODE", "").lower() in ("1", "true", "yes"):
        raise HTTPException(403, "Le mode test Stripe est désactivé")
    data = await request.json()
    user_id = data.get("user_id")
    plan = data.get("plan", "pro")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Utilisateur non trouvé")
    user.plan = plan
    user.credits = PLANS[plan]["credits"]
    user.stripe_sub_id = f"sub_test_{secrets.token_hex(8)}"
    db.commit()
    return {"ok": True, "plan": plan, "credits": user.credits}


@app.post("/api/videos/test-download")
async def api_test_video(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    """Télécharge une vidéo de test pour valider le pipeline de découpe."""
    check_rate_limit(request, "default")
    if user.credits <= 0:
        raise HTTPException(402, "Crédits insuffisants")

    sample_urls = [
        "https://file-examples.com/storage/fe1014c2828d6b4b8fc6416/2017/04/file_example_MP4_480_1_5MG.mp4",
        "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
        "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/SubaruOutbackOnStreetAndDirt.mp4",
    ]
    import random
    chosen = random.choice(sample_urls)

    video_id_db = str(uuid.uuid4())
    out_path = VIDEOS / f"{video_id_db}.mp4"

    try:
        import httpx
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.get(chosen)
            r.raise_for_status()
            out_path.write_bytes(r.content)
    except Exception as e:
        raise HTTPException(503, f"Impossible de télécharger la vidéo de test : {str(e)[:120]}")

    ffprobe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(out_path)], capture_output=True, text=True, timeout=15)
    try:
        duration = float(ffprobe.stdout.strip())
    except Exception:
        duration = 0

    filesize = out_path.stat().st_size

    video = Video(
        id=video_id_db,
        user_id=user.id,
        youtube_url=chosen,
        title="Vidéo de test Yestubers",
        duration=duration,
        filesize=filesize,
        filename=f"{video_id_db}.mp4",
        status="done",
    )
    db.add(video)
    use_credit(user, db)
    db.commit()

    return {"ok": True, "video_id": video_id_db, "duration": duration, "title": video.title}


@app.get("/api/user/me")
async def api_me(user: User = Depends(require_user)):
    return {
        "id": user.id, "email": user.email, "plan": user.plan,
        "credits": user.credits, "plan_name": PLANS[user.plan]["name"],
        "referral_code": user.referral_code,
    }

@app.get("/api/referral/stats")
async def api_referral_stats(user: User = Depends(require_user), db: Session = Depends(get_db)):
    count = db.query(User).filter(User.referrer_id == user.id).count()
    earned = min(1000, count * 3)
    link = f"https://yestubers.cloud/signup?ref={user.referral_code or ''}"
    return {
        "referral_code": user.referral_code,
        "referral_link": link,
        "referrals_count": count,
        "earned_credits": earned,
    }

@app.post("/api/user/update")
async def api_user_update(
    password: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    if len(password) < 8:
        raise HTTPException(400, "Mot de passe trop court (min 8 caractères)")
    user.password_hash = hash_pw(password)
    db.commit()
    return {"ok": True, "message": "Mot de passe mis à jour"}

@app.post("/api/user/regenerate-key")
async def api_user_regenerate_key(
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    user.api_key = secrets.token_hex(32)
    db.commit()
    return {"ok": True, "api_key": user.api_key}

@app.post("/api/stripe/portal")
async def api_stripe_portal(
    user: User = Depends(require_user),
):
    if not STRIPE_ENABLED:
        raise HTTPException(503, "Stripe n'est pas configuré.")
    if not user.stripe_customer_id:
        raise HTTPException(400, "Aucun abonnement Stripe trouvé")
    try:
        session = await asyncio.to_thread(
            stripe.billing_portal.Session.create,
            customer=user.stripe_customer_id,
            return_url="https://yestubers.cloud/settings",
        )
        return {"url": session.url}
    except Exception as e:
        raise HTTPException(503, f"Stripe indisponible : {str(e)[:120]}")

@app.delete("/api/user/delete")
async def api_user_delete(
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    # Delete all user's cuts and videos from disk
    for cut in db.query(Cut).filter(Cut.user_id == user.id).all():
        (CUTS / cut.filename).unlink(missing_ok=True)
        db.delete(cut)
    for video in db.query(Video).filter(Video.user_id == user.id).all():
        (VIDEOS / video.filename).unlink(missing_ok=True)
        db.delete(video)
    db.delete(user)
    db.commit()
    response = JSONResponse({"ok": True, "message": "Compte supprimé"})
    response.delete_cookie("session", secure=True)
    return response

@app.get("/api/thumbnails/{filename}")
async def api_thumbnail(filename: str):
    # Thumbnails are no longer stored locally; return 410 with a redirect suggestion
    raise HTTPException(410, "Les vignettes ne sont plus stockées localement. Utilisez l'URL distante.")

# ─── 404 Handler ──────────────────────────────────────────────────────────────

@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    i18n = translate_dict(request)
    return HTMLResponse(
        _jinja_env.get_template("404.html").render(request=request, i18n=i18n, locale=i18n["_locale"]),
        status_code=404
    )

STATIC_DIR = BASE_DIR / "static"

# ─── Static ───────────────────────────────────────────────────────────────────
# Mount static files BEFORE catch-all so /static/* is served correctly
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ─── Catch-all for unmatched routes + locale homepage ─────────────────────────
# Must be last — after all specific routes
@app.get("/{path:path}", response_class=HTMLResponse)
async def catch_all(request: Request, path: str, db: Session = Depends(get_db)):
    # If it's a known locale OR locale/page combo, serve the right page
    parts = path.strip("/").split("/")
    loc = parts[0] if parts else ""
    subpath = "/".join(parts[1:]) if len(parts) > 1 else ""

    # Known locale homepage (/fr/, /en/, etc.)
    if loc in SUPPORTED_LOCALES and not subpath:
        request._forced_locale = loc
        i18n = translate_dict(request)
        response = HTMLResponse(_jinja_env.get_template("index.html").render(
            request=request, user=get_user_from_session(request, db),
            stripe_pk=STRIPE_PK, i18n=i18n, locale=i18n["_locale"]))
        response.set_cookie("locale", loc, max_age=365*24*3600)
        return response

    # Known locale + known page (/fr/pricing, /es/about, etc.)
    KNOWN_PAGES = {"pricing", "about", "contact", "terms", "privacy", "login", "signup", "dashboard", "settings"}
    if loc in SUPPORTED_LOCALES and subpath in KNOWN_PAGES:
        request._forced_locale = loc
        return RedirectResponse(url=f"/{subpath}?lang={loc}", status_code=307)

    # Otherwise 404
    i18n = translate_dict(request)
    return HTMLResponse(
        _jinja_env.get_template("404.html").render(request=request, i18n=i18n, locale=i18n["_locale"]),
        status_code=404
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)