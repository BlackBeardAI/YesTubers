#!/usr/bin/env python3
"""YT Cut - YouTube Video Downloader & Cutter SaaS"""
import os
import re
import json
import uuid
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
from fastapi import FastAPI, Request, UploadFile, File, Form, Depends, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import (
    create_engine, Column, String, Integer, Float, DateTime, Boolean, Text, ForeignKey
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
import stripe

from jinja2 import Environment, FileSystemLoader, select_autoescape

# ─── Config ───────────────────────────────────────────────────────────────────

BASE_DIR = Path("/opt/ytcut")
STORAGE = BASE_DIR / "storage"
VIDEOS = STORAGE / "videos"
CUTS = STORAGE / "cuts"
THUMBS = STORAGE / "thumbnails"

DB_PATH = BASE_DIR / "ytcut.db"
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# Stripe — à configurer via env
STRIPE_PK = os.environ.get("STRIPE_PUBLIC_KEY", "pk_test_XXXXXXXXXXXXXXXX")
STRIPE_SK = os.environ.get("STRIPE_SECRET_KEY", "sk_test_XXXXXXXXXXXXXXXX")
STRIPE_WEBHOOK = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
stripe.api_key = STRIPE_SK

# Crédits par abonnement
# billing: "monthly" ou "annual" — l'annuel est poussé par défaut (-33%)
PLANS = {
    "free": {
        "name": "Gratuit",
        "monthly_price": 0, "annual_price": 0,
        "credits": 1, "max_duration": 30, "quality": "480",
        "stripe_price_id": None, "stripe_annual_price_id": None,
    },
    "starter": {
        "name": "Starter",
        "monthly_price": 1.99, "annual_price": 19.99,
        "credits": 10, "max_duration": 180, "quality": "720",
        "stripe_price_id": None, "stripe_annual_price_id": None,
    },
    "creator": {
        "name": "Creator",
        "monthly_price": 6.99, "annual_price": 69.99,
        "credits": 50, "max_duration": 600, "quality": "1080",
        "stripe_price_id": None, "stripe_annual_price_id": None,
    },
    "pro": {
        "name": "Pro",
        "monthly_price": 19.99, "annual_price": 199.99,
        "credits": 999999, "max_duration": 3600, "quality": "4K",
        "stripe_price_id": None, "stripe_annual_price_id": None,
    },
}

app = FastAPI(title="YT Cut", version="1.0.0")
# Jinja2Templates from Starlette breaks with request in context (unhashable cache key)
# Use a plain Jinja2 environment with caching disabled
_jinja_env = Environment(
    loader=FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=select_autoescape(["html"]),
    cache_size=0,
)
_jinja_env.filters["from_json"] = json.loads
_jinja_env.globals["plans"] = PLANS

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
    return hashlib.sha256((pw + SECRET_KEY).encode()).hexdigest()

def get_user_from_session(request: Request, db: Session):
    user_id = request.cookies.get("session")
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

def get_video_info(url: str) -> dict:
    """Récupère les infos de la vidéo sans la télécharger."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise Exception(f"yt-dlp error: {result.stderr}")
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise Exception("Timeout lors de la récupération des infos")
    except Exception as e:
        raise Exception(f"Erreur: {str(e)}")

async def download_video(url: str, video_id_db: str, max_duration: int = 60, quality: str = "480") -> dict:
    """Télécharge une vidéo YouTube selon la qualité du plan."""
    out_path = VIDEOS / f"{video_id_db}.mp4"
    thumb_path = THUMBS / f"{video_id_db}.jpg"

    # D'abord vérifier la durée
    info = get_video_info(url)
    duration = info.get("duration", 0) or 0

    if duration > max_duration:
        raise Exception(f"Vidéo trop longue ({duration}s). Maximum: {max_duration}s pour votre plan.")

    # Format selon qualité
    # 4K = 2160p, 1080p, 720p, 480p
    quality_map = {"480": "480", "720": "720", "1080": "1080", "4K": "2160"}
    max_height = quality_map.get(quality, "480")

    # Télécharger
    cmd = [
        "yt-dlp",
        "-f", f"best[height<={max_height}][ext=mp4]/best[height<={max_height}]/best",
        "--no-playlist",
        "-o", str(out_path),
        url
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise Exception(f"Erreur téléchargement: {stderr.decode()}")

    # Extraire thumbnail
    subprocess.run([
        "ffmpeg", "-i", str(out_path), "-ss", "5", "-vframes", "1",
        "-q:v", "2", str(thumb_path), "-y"
    ], capture_output=True, timeout=15)

    filesize = out_path.stat().st_size

    return {"duration": duration, "filename": out_path.name, "filesize": filesize, "thumbnail": thumb_path.name}

def cut_video(input_path: Path, start: float, end: float, cut_id: str) -> Path:
    """Coupe une vidéo avec ffmpeg."""
    out_path = CUTS / f"{cut_id}.mp4"
    duration = end - start

    cmd = [
        "ffmpeg", "-ss", str(start), "-i", str(input_path),
        "-t", str(duration), "-c:v", "libx264", "-c:a", "aac",
        "-preset", "fast", "-crf", "23", "-y", str(out_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise Exception(f"Erreur cut: {result.stderr}")
    return out_path

# ─── Routes Web ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    return HTMLResponse(_jinja_env.get_template("index.html").render(
        request=request, user=user, stripe_pk=STRIPE_PK))

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return HTMLResponse(_jinja_env.get_template("login.html").render(request=request))

@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    return HTMLResponse(_jinja_env.get_template("signup.html").render(request=request))

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse("/login")
    videos = db.query(Video).filter(Video.user_id == user.id).order_by(Video.created_at.desc()).all()
    cuts = db.query(Cut).filter(Cut.user_id == user.id).order_by(Cut.created_at.desc()).all()
    return HTMLResponse(_jinja_env.get_template("dashboard.html").render(
        request=request, user=user, videos=videos, cuts=cuts,
        stripe_pk=STRIPE_PK,
    ))

@app.get("/pricing", response_class=HTMLResponse)
async def pricing(request: Request, db: Session = Depends(get_db)):
    user = get_user_from_session(request, db)
    return HTMLResponse(_jinja_env.get_template("pricing.html").render(
        request=request, user=user, stripe_pk=STRIPE_PK,
    ))

# ─── API Auth ─────────────────────────────────────────────────────────────────

@app.post("/api/auth/signup")
async def api_signup(email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(400, "Email déjà utilisé")

    user = User(email=email, password_hash=hash_pw(password), credits=1, plan="free")
    db.add(user)
    db.commit()

    response = JSONResponse({"ok": True, "message": "Compte créé !"})
    response.set_cookie("session", user.id, httponly=True, max_age=30*24*3600)
    return response

@app.post("/api/auth/login")
async def api_login(email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user or user.password_hash != hash_pw(password):
        raise HTTPException(401, "Email ou mot de passe incorrect")

    response = JSONResponse({"ok": True, "message": "Connecté !"})
    response.set_cookie("session", user.id, httponly=True, max_age=30*24*3600)
    return response

@app.post("/api/auth/logout")
async def api_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("session")
    return response

@app.post("/api/auth/forgot")
async def api_forgot(email: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"ok": True, "message": "Si l'email existe, un lien a été envoyé."}

    token = secrets.token_urlsafe(32)
    user.reset_token = token
    user.reset_expires = datetime.utcnow() + timedelta(hours=1)
    db.commit()
    return {"ok": True, "message": "Lien de réinitialisation envoyé (token: " + token + ")"}

@app.post("/api/auth/reset")
async def api_reset(token: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
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

# ─── API Videos ───────────────────────────────────────────────────────────────

@app.post("/api/videos/download")
async def api_download(
    url: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
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
        quality = PLANS[user.plan]["quality"]
        info = await download_video(url, vid_db, max_dur, quality)
        video.title = info.get("title", "Sans titre")  # will be overwritten
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

        db.commit()
        return {"ok": True, "video_id": vid_db, "title": video.title, "duration": video.duration}

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
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    video = db.query(Video).filter(Video.id == video_id, Video.user_id == user.id).first()
    if not video or video.status != "done":
        raise HTTPException(404, "Vidéo non trouvée")
    path = VIDEOS / video.filename
    if not path.exists():
        raise HTTPException(404, "Fichier introuvable")
    return FileResponse(str(path), filename=f"{video.title or 'video'}.mp4")

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
    if video.thumbnail:
        (THUMBS / video.thumbnail).unlink(missing_ok=True)
    db.delete(video)
    db.commit()
    return {"ok": True}

# ─── API Cuts ─────────────────────────────────────────────────────────────────

@app.post("/api/cuts/create")
async def api_cut_create(
    video_id: str = Form(...),
    start_time: float = Form(...),
    end_time: float = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    check_credits(user)

    video = db.query(Video).filter(Video.id == video_id, Video.user_id == user.id).first()
    if not video or video.status != "done":
        raise HTTPException(404, "Vidéo source non trouvée")

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
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    cut = db.query(Cut).filter(Cut.id == cut_id, Cut.user_id == user.id).first()
    if not cut or cut.status != "done":
        raise HTTPException(404)
    path = CUTS / cut.filename
    if not path.exists():
        raise HTTPException(404)
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

@app.post("/api/stripe/create-checkout")
async def stripe_checkout(
    plan: str = Form(...),
    billing: str = Form("annual"),  # monthly ou annual
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    if plan not in PLANS or plan == "free":
        raise HTTPException(400, "Plan invalide")
    if billing not in ("monthly", "annual"):
        raise HTTPException(400, "Billing invalide")

    plan_data = PLANS[plan]
    price_field = "stripe_annual_price_id" if billing == "annual" else "stripe_price_id"
    amount = plan_data["annual_price"] if billing == "annual" else plan_data["monthly_price"]

    if not plan_data.get(price_field):
        # Créer le produit/prix à la volée
        suffix = " (Annuel -33%)" if billing == "annual" else " (Mensuel)"
        product = await asyncio.to_thread(
            stripe.Product.create,
            name=f"YT Cut - {plan_data['name']}{suffix}",
        )
        interval = "year" if billing == "annual" else "month"
        price = await asyncio.to_thread(
            stripe.Price.create,
            product=product.id,
            unit_amount=int(amount * 100),
            currency="eur",
            recurring={"interval": interval},
        )
        plan_data[price_field] = price.id

    # Créer ou récupérer le customer Stripe
    if not user.stripe_customer_id:
        customer = await asyncio.to_thread(
            stripe.Customer.create,
            email=user.email,
            metadata={"user_id": user.id},
        )
        user.stripe_customer_id = customer.id
        db.commit()

    session = await asyncio.to_thread(
        stripe.checkout.Session.create,
        customer=user.stripe_customer_id,
        payment_method_types=["card"],
        line_items=[{"price": plan_data[price_field], "quantity": 1}],
        mode="subscription",
        success_url=f"https://cut.blackbeardai.org/dashboard?session=***",
        cancel_url=f"https://cut.blackbeardai.org/pricing",
        metadata={"user_id": user.id, "plan": plan, "billing": billing},
    )
    return {"url": session.url}

@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK)
    except:
        raise HTTPException(400, "Signature invalide")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session["metadata"]["user_id"]
        plan = session["metadata"]["plan"]
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.plan = plan
            user.credits = PLANS[plan]["credits"]
            user.stripe_sub_id = session.get("subscription")
            db.commit()

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        user = db.query(User).filter(User.stripe_sub_id == sub["id"]).first()
        if user:
            user.plan = "free"
            user.credits = 1
            user.stripe_sub_id = None
            db.commit()

    return {"ok": True}

@app.get("/api/user/me")
async def api_me(user: User = Depends(require_user)):
    return {
        "id": user.id, "email": user.email, "plan": user.plan,
        "credits": user.credits, "plan_name": PLANS[user.plan]["name"],
    }

# ─── Thumbnails ───────────────────────────────────────────────────────────────

@app.get("/api/thumbnails/{filename}")
async def get_thumbnail(filename: str):
    path = THUMBS / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path))

# ─── Static ───────────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
