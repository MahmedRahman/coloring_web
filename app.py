#!/usr/bin/env python3
"""
Web app: upload a child's photo, choose scenes, get a coloring book.

Run:
    export CLOUDFLARE_ACCOUNT_ID="..."
    export CLOUDFLARE_API_TOKEN="..."
    python3 app.py
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from functools import wraps
from typing import List, Optional, Union

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import arabic_reshaper
import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from bidi.algorithm import get_display
from authlib.integrations.flask_client import OAuth
from flask import Flask, jsonify, render_template, request, send_file, abort, session, redirect, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas
from werkzeug.security import check_password_hash, generate_password_hash

from paymob_client import (
    BOOK_PACK_CREDITS,
    BOOK_PACK_PRICE_EGP,
    amount_cents,
    checkout_url,
    create_intention,
    normalize_egypt_phone,
    pay_with_wallet_classic,
    paymob_configured,
    wallet_enabled,
    verify_redirect_hmac,
    verify_transaction_post_hmac,
)
from kie_client import (
    generate_image_to_image,
    is_mock_provider,
    kie_configured,
    model_for_provider,
    normalize_provider,
    upload_image as kie_upload_image,
)

# Approximate Kie 1K image prices for admin cost estimates (USD)
COST_CHATGPT_USD = float(os.environ.get("COST_CHATGPT_USD") or "0.03")
COST_NANOBANANA_USD = float(os.environ.get("COST_NANOBANANA_USD") or "0.04")
COST_MOCK_USD = float(os.environ.get("COST_MOCK_USD") or "0")
COST_USD_TO_EGP = float(os.environ.get("COST_USD_TO_EGP") or "50")

COVER_MODES = frozenset({"none", "cover", "ending", "both"})


def cost_rates_dict() -> dict:
    return {
        "chatgpt": COST_CHATGPT_USD,
        "nanobanana": COST_NANOBANANA_USD,
        "mock": COST_MOCK_USD,
        "usd_to_egp": COST_USD_TO_EGP,
        "note": "تقريبي — أسعار Kie 1K وقد تختلف. Mock = $0 (اختبار بدون Kie).",
    }


def normalize_cover_mode(raw: Optional[str]) -> str:
    m = (raw or "both").strip().lower()
    if m in COVER_MODES:
        return m
    if m in ("start", "front", "cover_only"):
        return "cover"
    if m in ("end", "ending_only", "back"):
        return "ending"
    if m in ("no", "0", "false", "off", "بدون"):
        return "none"
    return "both"


def cover_mode_flags(mode: Optional[str]) -> tuple[bool, bool]:
    m = normalize_cover_mode(mode)
    return (m in ("cover", "both"), m in ("ending", "both"))

ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN")
MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
TRANSLATE_MODEL = "@cf/meta/m2m100-1.2b"
FREE_BOOKS_PER_MONTH = int(os.environ.get("FREE_BOOKS_PER_MONTH", "3"))
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
GOOGLE_CLIENT_ID = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
GOOGLE_CLIENT_SECRET = (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
APP_URL = (os.environ.get("APP_URL") or "").rstrip("/")


def google_ready() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def app_base_url() -> str:
    if APP_URL:
        return APP_URL
    return request.url_root.rstrip("/")

# A4 portrait ratio 210:297 — dimensions are multiples of 16 (model-friendly)
# and stay within Workers AI max 1920px
PAGE_WIDTH = 1120
PAGE_HEIGHT = 1584  # exact A4 aspect: 1120/1584 == 210/297
MAX_PAGES = 8
ADMIN_MAX_PAGES = 12

PROMPT_VARIANTS = {
    "a": (
        "black and white line art coloring book page, clean bold outlines, "
        "no shading, no color, no gray, pure white background, "
        "simple children's coloring book illustration style, "
        "vertical A4 portrait page composition, full-page illustration, "
        "keep the exact same child from image 0 — same face, same hairstyle, same age"
    ),
    "b": (
        "black and white line art coloring book page, clean bold outlines, "
        "no shading, no color, no gray, pure white background, "
        "simple children's coloring book illustration style, "
        "vertical A4 portrait page composition, full-page illustration filling the tall page, "
        "identical face to image 0, same facial features, same hairstyle, same age and proportions, "
        "preserve identity from all reference images, recognizable likeness of the same child"
    ),
}
DEFAULT_VARIANT = "b"

LINE_WEIGHT = {
    "thin": "thin delicate outlines",
    "normal": "clean medium-weight outlines",
    "thick": "thick bold heavy outlines",
}
DETAIL_LEVEL = {
    "simple": "very simple shapes, minimal details, easy for toddlers to color",
    "normal": "balanced amount of detail for children",
    "detailed": "richer detailed line work suitable for older children",
}
ART_STYLE = {
    "cartoon": "cute cartoon illustration style",
    "realistic": "semi-realistic proportions and facial features",
}

# Scene packs = book types. Each pack is one product in the admin book picker.
# "scene" is the English prompt fragment that completes "the child is ..." (see build_prompt).
JOBS_SCENES = [
    {"id": "doctor", "emoji": "🩺", "title": "طبيب", "title_en": "Doctor",
     "grad": ["#bfdbfe", "#a7f3d0"],
     "scene": "dressed as a friendly doctor wearing a white coat and stethoscope in a simple clinic"},
    {"id": "engineer", "emoji": "🛠️", "title": "مهندس", "title_en": "Engineer",
     "grad": ["#fed7aa", "#fde68a"],
     "scene": "dressed as a young engineer wearing a hard hat and holding blueprints near simple buildings"},
    {"id": "teacher", "emoji": "📚", "title": "معلم", "title_en": "Teacher",
     "grad": ["#c7d2fe", "#fbcfe8"],
     "scene": "dressed as a teacher standing at a chalkboard with books and an apple"},
    {"id": "pilot", "emoji": "✈️", "title": "طيار", "title_en": "Pilot",
     "grad": ["#bae6fd", "#ddd6fe"],
     "scene": "dressed as an airplane pilot with a captain hat standing near a simple airplane"},
    {"id": "firefighter", "emoji": "🚒", "title": "إطفائي", "title_en": "Firefighter",
     "grad": ["#fecaca", "#fed7aa"],
     "scene": "dressed as a firefighter with a helmet and hose beside a fire truck"},
    {"id": "police", "emoji": "👮", "title": "شرطي", "title_en": "Police officer",
     "grad": ["#bfdbfe", "#e2e8f0"],
     "scene": "dressed as a police officer with a badge and hat standing beside a patrol car"},
    {"id": "chef", "emoji": "👨‍🍳", "title": "طاهي", "title_en": "Chef",
     "grad": ["#fed7aa", "#fecaca"],
     "scene": "dressed as a chef with a tall chef hat cooking in a simple kitchen"},
    {"id": "scientist", "emoji": "🔬", "title": "عالم", "title_en": "Scientist",
     "grad": ["#bbf7d0", "#a5f3fc"],
     "scene": "dressed as a scientist in a lab coat holding a flask in a simple laboratory"},
    {"id": "artist", "emoji": "🎨", "title": "فنان", "title_en": "Artist",
     "grad": ["#fbcfe8", "#fde68a"],
     "scene": "dressed as an artist with a beret painting on an easel with brushes and palette"},
    {"id": "astronaut", "emoji": "🚀", "title": "رائد فضاء", "title_en": "Astronaut",
     "grad": ["#312e81", "#831843"],
     "scene": "dressed as an astronaut in a space suit floating near a rocket and stars"},
    {"id": "soccer", "emoji": "⚽", "title": "لاعب كرة", "title_en": "Soccer player",
     "grad": ["#bbf7d0", "#86efac"],
     "scene": "dressed as a soccer player kicking a ball on a simple soccer field"},
    {"id": "farmer", "emoji": "🌾", "title": "مزارع", "title_en": "Farmer",
     "grad": ["#fde68a", "#86efac"],
     "scene": "dressed as a farmer with a straw hat holding a watering can near simple crops"},
]

HEROES_SCENES = [
    {"id": "hero_superhero", "emoji": "🦸", "title": "بطل خارق", "title_en": "Superhero",
     "grad": ["#bfdbfe", "#fecaca"],
     "scene": "dressed as a cheerful superhero with a cape and chest emblem, standing in a heroic pose on a simple rooftop"},
    {"id": "hero_knight", "emoji": "🛡️", "title": "فارس", "title_en": "Knight",
     "grad": ["#e2e8f0", "#c7d2fe"],
     "scene": "dressed as a brave little knight in simple armor holding a shield and wooden sword in front of a small castle"},
    {"id": "hero_pirate", "emoji": "🏴‍☠️", "title": "قرصان", "title_en": "Pirate",
     "grad": ["#bae6fd", "#fde68a"],
     "scene": "dressed as a friendly pirate captain with a tricorn hat and eye patch standing on a wooden ship deck with a treasure chest"},
    {"id": "hero_wizard", "emoji": "🧙", "title": "ساحر", "title_en": "Wizard",
     "grad": ["#ddd6fe", "#c7d2fe"],
     "scene": "dressed as a little wizard in a pointed hat and robe holding a glowing wand beside a magic book and potion bottles"},
    {"id": "hero_ninja", "emoji": "🥷", "title": "نينجا", "title_en": "Ninja",
     "grad": ["#cbd5e1", "#a5b4fc"],
     "scene": "dressed as a playful ninja in a simple training outfit jumping between bamboo trees at a dojo"},
    {"id": "hero_dragon", "emoji": "🐉", "title": "مع تنين", "title_en": "Dragon friend",
     "grad": ["#bbf7d0", "#fde68a"],
     "scene": "riding on the back of a cute friendly cartoon dragon flying above simple clouds and hills"},
    {"id": "hero_explorer", "emoji": "🧭", "title": "مستكشف", "title_en": "Explorer",
     "grad": ["#fde68a", "#bbf7d0"],
     "scene": "dressed as a jungle explorer with a safari hat, backpack and compass walking on a simple jungle path"},
    {"id": "hero_robot", "emoji": "🤖", "title": "مع روبوت", "title_en": "Robot buddy",
     "grad": ["#a5f3fc", "#c7d2fe"],
     "scene": "standing next to a tall friendly cartoon robot with round eyes in a simple workshop with gears and tools"},
    {"id": "hero_detective", "emoji": "🕵️", "title": "محقق", "title_en": "Detective",
     "grad": ["#e2e8f0", "#fed7aa"],
     "scene": "dressed as a young detective in a coat and cap holding a large magnifying glass following footprints on the ground"},
    {"id": "hero_spaceranger", "emoji": "🛸", "title": "حارس الفضاء", "title_en": "Space ranger",
     "grad": ["#312e81", "#6d28d9"],
     "scene": "dressed as a space ranger in a futuristic suit standing beside a small flying saucer with planets and stars around"},
    {"id": "hero_racer", "emoji": "🏎️", "title": "سائق سباق", "title_en": "Race driver",
     "grad": ["#fecaca", "#fde68a"],
     "scene": "dressed as a race car driver with a helmet standing beside a cartoon race car on a simple race track"},
    {"id": "hero_treasure", "emoji": "💎", "title": "صائد كنوز", "title_en": "Treasure hunter",
     "grad": ["#fde68a", "#fbcfe8"],
     "scene": "opening a big treasure chest full of coins and gems inside a simple cave with a torch on the wall"},
]

ANIMALS_SCENES = [
    {"id": "ani_lion", "emoji": "🦁", "title": "مع أسد", "title_en": "Lion",
     "grad": ["#fde68a", "#fed7aa"],
     "scene": "sitting next to a friendly cartoon lion with a big fluffy mane on a simple savanna with grass and a tree"},
    {"id": "ani_dolphin", "emoji": "🐬", "title": "تحت الماء", "title_en": "Underwater",
     "grad": ["#bae6fd", "#a5f3fc"],
     "scene": "swimming underwater with a smiling dolphin, simple fish, coral and bubbles around"},
    {"id": "ani_dino", "emoji": "🦕", "title": "مع ديناصور", "title_en": "Dinosaur",
     "grad": ["#bbf7d0", "#86efac"],
     "scene": "playing with a big friendly cartoon dinosaur in a simple prehistoric landscape with ferns and a volcano far away"},
    {"id": "ani_safari", "emoji": "🐘", "title": "رحلة سفاري", "title_en": "Safari",
     "grad": ["#fde68a", "#bbf7d0"],
     "scene": "on a safari adventure standing beside a cartoon elephant and a giraffe with simple acacia trees behind"},
    {"id": "ani_farm", "emoji": "🐄", "title": "في المزرعة", "title_en": "Farm animals",
     "grad": ["#86efac", "#fde68a"],
     "scene": "feeding cartoon farm animals — a cow, a sheep and chickens — in front of a simple barn and fence"},
    {"id": "ani_cat", "emoji": "🐱", "title": "مع قطة", "title_en": "Kitten",
     "grad": ["#fbcfe8", "#fed7aa"],
     "scene": "cuddling a fluffy cartoon kitten while sitting on a cushion with a ball of yarn nearby"},
    {"id": "ani_puppy", "emoji": "🐶", "title": "مع كلب", "title_en": "Puppy",
     "grad": ["#fed7aa", "#fde68a"],
     "scene": "playing fetch with a happy cartoon puppy in a simple park with a ball and a bone"},
    {"id": "ani_horse", "emoji": "🐴", "title": "يركب حصان", "title_en": "Horse riding",
     "grad": ["#fde68a", "#d9f99d"],
     "scene": "riding a gentle cartoon horse across a simple meadow with a wooden fence and small flowers"},
    {"id": "ani_birds", "emoji": "🦜", "title": "مع الطيور", "title_en": "Birds",
     "grad": ["#a5f3fc", "#bbf7d0"],
     "scene": "holding out a hand while colorful cartoon parrots and small birds land on the arm near a big leafy tree"},
    {"id": "ani_penguin", "emoji": "🐧", "title": "مع البطاريق", "title_en": "Penguins",
     "grad": ["#bae6fd", "#e2e8f0"],
     "scene": "wearing a warm winter coat and scarf playing with cute cartoon penguins on simple ice and snow"},
    {"id": "ani_bunny", "emoji": "🐰", "title": "مع أرنب", "title_en": "Bunny",
     "grad": ["#fbcfe8", "#ddd6fe"],
     "scene": "holding a soft cartoon bunny with long ears in a simple garden with carrots and daisies"},
    {"id": "ani_zoo", "emoji": "🦒", "title": "حديقة الحيوان", "title_en": "At the zoo",
     "grad": ["#bbf7d0", "#fed7aa"],
     "scene": "visiting the zoo, waving at a tall cartoon giraffe and a monkey behind a simple fence with a balloon in hand"},
]

SPORTS_SCENES = [
    {"id": "sp_football", "emoji": "⚽", "title": "كرة قدم", "title_en": "Football",
     "grad": ["#bbf7d0", "#86efac"],
     "scene": "wearing a football kit kicking a ball toward a goal on a simple pitch"},
    {"id": "sp_swimming", "emoji": "🏊", "title": "سباحة", "title_en": "Swimming",
     "grad": ["#bae6fd", "#a5f3fc"],
     "scene": "wearing swimming goggles and a swimsuit swimming in a simple pool lane with water splashes"},
    {"id": "sp_karate", "emoji": "🥋", "title": "كاراتيه", "title_en": "Karate",
     "grad": ["#e2e8f0", "#fecaca"],
     "scene": "wearing a karate gi with a belt doing a karate stance in a simple dojo with mats"},
    {"id": "sp_gym", "emoji": "🤸", "title": "جمباز", "title_en": "Gymnastics",
     "grad": ["#fbcfe8", "#ddd6fe"],
     "scene": "wearing a gymnastics leotard balancing on a beam with a ribbon in a simple gym hall"},
    {"id": "sp_bike", "emoji": "🚲", "title": "دراجة", "title_en": "Cycling",
     "grad": ["#fde68a", "#bbf7d0"],
     "scene": "wearing a bicycle helmet riding a bike on a simple park path with trees along the side"},
    {"id": "sp_basket", "emoji": "🏀", "title": "كرة سلة", "title_en": "Basketball",
     "grad": ["#fed7aa", "#fecaca"],
     "scene": "wearing a basketball jersey jumping to shoot a ball into a hoop on a simple court"},
    {"id": "sp_tennis", "emoji": "🎾", "title": "تنس", "title_en": "Tennis",
     "grad": ["#d9f99d", "#bbf7d0"],
     "scene": "holding a tennis racket about to hit a ball on a simple tennis court with a net"},
    {"id": "sp_run", "emoji": "🏃", "title": "جري", "title_en": "Running",
     "grad": ["#a5f3fc", "#bbf7d0"],
     "scene": "wearing running clothes sprinting on a simple athletics track toward a finish line ribbon"},
    {"id": "sp_chess", "emoji": "♟️", "title": "شطرنج", "title_en": "Chess",
     "grad": ["#e2e8f0", "#c7d2fe"],
     "scene": "sitting at a table thinking hard while moving a big chess piece on a chess board"},
    {"id": "sp_skate", "emoji": "🛹", "title": "سكيت", "title_en": "Skateboarding",
     "grad": ["#fed7aa", "#c7d2fe"],
     "scene": "wearing a helmet and pads riding a skateboard on a simple skate ramp"},
    {"id": "sp_box", "emoji": "🥊", "title": "ملاكمة", "title_en": "Boxing",
     "grad": ["#fecaca", "#fde68a"],
     "scene": "wearing big boxing gloves punching a training bag in a simple gym"},
    {"id": "sp_medal", "emoji": "🏅", "title": "منصة التتويج", "title_en": "Champion",
     "grad": ["#fde68a", "#fbcfe8"],
     "scene": "standing on the first place podium holding a big trophy with a medal around the neck and confetti around"},
]

GIRLS_SCENES = [
    {"id": "gl_ballerina", "emoji": "🩰", "title": "باليرينا", "title_en": "Ballerina",
     "grad": ["#fbcfe8", "#ddd6fe"],
     "scene": "dressed as a ballerina in a tutu and ballet shoes dancing on a simple stage with curtains"},
    {"id": "gl_princess", "emoji": "👑", "title": "أميرة", "title_en": "Princess",
     "grad": ["#fbcfe8", "#fde68a"],
     "scene": "dressed as a princess in a long gown and crown standing in front of a simple castle with a small carriage"},
    {"id": "gl_fairy", "emoji": "🧚", "title": "جنية", "title_en": "Fairy",
     "grad": ["#ddd6fe", "#a5f3fc"],
     "scene": "dressed as a little fairy with delicate wings holding a sparkling wand among big flowers and butterflies"},
    {"id": "gl_vet", "emoji": "🐩", "title": "طبيبة بيطرية", "title_en": "Vet",
     "grad": ["#bbf7d0", "#bae6fd"],
     "scene": "dressed as a vet in a white coat gently examining a small cartoon puppy on a clinic table"},
    {"id": "gl_fashion", "emoji": "👗", "title": "مصممة أزياء", "title_en": "Fashion designer",
     "grad": ["#fbcfe8", "#fed7aa"],
     "scene": "dressed as a fashion designer sketching a dress on a board next to a mannequin and rolls of fabric"},
    {"id": "gl_baker", "emoji": "🧁", "title": "حلوانية", "title_en": "Baker",
     "grad": ["#fed7aa", "#fbcfe8"],
     "scene": "wearing an apron decorating a tall cupcake tower in a simple bakery kitchen"},
    {"id": "gl_florist", "emoji": "💐", "title": "بائعة زهور", "title_en": "Florist",
     "grad": ["#bbf7d0", "#fbcfe8"],
     "scene": "arranging a big bouquet of flowers at a simple flower shop stand with buckets of blooms"},
    {"id": "gl_singer", "emoji": "🎤", "title": "مغنية", "title_en": "Singer",
     "grad": ["#ddd6fe", "#fbcfe8"],
     "scene": "singing into a microphone on a simple stage with musical notes and stage lights around"},
    {"id": "gl_mermaid", "emoji": "🧜", "title": "حورية البحر", "title_en": "Mermaid",
     "grad": ["#a5f3fc", "#bae6fd"],
     "scene": "as a mermaid with a decorated tail sitting on a rock underwater with shells, seaweed and small fish"},
    {"id": "gl_teaparty", "emoji": "🫖", "title": "حفلة شاي", "title_en": "Tea party",
     "grad": ["#fed7aa", "#fbcfe8"],
     "scene": "hosting a tea party with teddy bears and dolls around a small table with a teapot and cups"},
    {"id": "gl_unicorn", "emoji": "🦄", "title": "مع يونيكورن", "title_en": "Unicorn",
     "grad": ["#fbcfe8", "#ddd6fe"],
     "scene": "hugging a cute cartoon unicorn with a flowing mane under a rainbow with clouds and stars"},
    {"id": "gl_garden", "emoji": "🌷", "title": "حديقة الورد", "title_en": "Flower garden",
     "grad": ["#bbf7d0", "#fbcfe8"],
     "scene": "watering tall flowers with a watering can in a simple garden with butterflies and a small fence"},
]

EGYPT_SCENES = [
    {"id": "eg_pharaoh", "emoji": "🏺", "title": "فرعون", "title_en": "Pharaoh",
     "grad": ["#fde68a", "#fed7aa"],
     "scene": "dressed as a young ancient Egyptian pharaoh with a nemes headdress and collar, standing between simple hieroglyph columns"},
    {"id": "eg_pyramids", "emoji": "🐫", "title": "عند الأهرامات", "title_en": "At the pyramids",
     "grad": ["#fde68a", "#fed7aa"],
     "scene": "riding a friendly cartoon camel in front of the great pyramids and the Sphinx in the desert"},
    {"id": "eg_ramadan", "emoji": "🏮", "title": "فانوس رمضان", "title_en": "Ramadan lantern",
     "grad": ["#c7d2fe", "#fde68a"],
     "scene": "holding a big decorated Ramadan lantern (fanous) under a crescent moon and hanging lanterns and stars"},
    {"id": "eg_eid", "emoji": "🎁", "title": "العيد", "title_en": "Eid",
     "grad": ["#fbcfe8", "#fde68a"],
     "scene": "wearing new Eid clothes holding gifts and balloons with festive decorations and sweets on a table"},
    {"id": "eg_horseman", "emoji": "🐎", "title": "خيّال عربي", "title_en": "Arabian rider",
     "grad": ["#fed7aa", "#fde68a"],
     "scene": "riding a proud Arabian horse while wearing traditional Arab dress with a flowing headscarf in an open desert"},
    {"id": "eg_nile", "emoji": "⛵", "title": "مركب في النيل", "title_en": "Nile felucca",
     "grad": ["#bae6fd", "#bbf7d0"],
     "scene": "sailing a small felucca boat with a triangular sail on the Nile with palm trees along the bank"},
    {"id": "eg_market", "emoji": "🧺", "title": "في السوق", "title_en": "Old market",
     "grad": ["#fed7aa", "#fde68a"],
     "scene": "walking through a traditional old market alley with hanging lanterns, spice baskets and pottery stalls"},
    {"id": "eg_alex", "emoji": "🌊", "title": "بحر الإسكندرية", "title_en": "Alexandria shore",
     "grad": ["#bae6fd", "#a5f3fc"],
     "scene": "standing on a seaside corniche wall with waves, seagulls and a tall lighthouse behind"},
    {"id": "eg_mosque", "emoji": "🕌", "title": "عند المسجد", "title_en": "By the mosque",
     "grad": ["#c7d2fe", "#bbf7d0"],
     "scene": "standing in front of a simple mosque with a dome and minaret under a crescent moon"},
    {"id": "eg_farm_egy", "emoji": "🌾", "title": "فلاح مصري", "title_en": "Egyptian farmer",
     "grad": ["#fde68a", "#86efac"],
     "scene": "dressed in a traditional galabeya carrying a basket in a green field with palm trees and a water wheel"},
    {"id": "eg_desert", "emoji": "🏜️", "title": "في الصحرا", "title_en": "Desert camp",
     "grad": ["#fde68a", "#fed7aa"],
     "scene": "sitting by a small campfire in front of a bedouin tent in the desert with sand dunes and stars above"},
    {"id": "eg_family", "emoji": "🫖", "title": "قعدة العيلة", "title_en": "Family gathering",
     "grad": ["#fed7aa", "#fbcfe8"],
     "scene": "sitting on a floor cushion around a low table with tea glasses and dates in a warm traditional room"},
]

DAILY_SCENES = [
    {"id": "day_school", "emoji": "🎒", "title": "أول يوم مدرسة", "title_en": "First school day",
     "grad": ["#c7d2fe", "#fde68a"],
     "scene": "wearing a backpack waving happily in front of a simple school building on the first day"},
    {"id": "day_brush", "emoji": "🪥", "title": "بيغسل سنانه", "title_en": "Brushing teeth",
     "grad": ["#a5f3fc", "#bbf7d0"],
     "scene": "brushing teeth in front of a bathroom mirror with a toothbrush and toothpaste, foam and bubbles around"},
    {"id": "day_sleep", "emoji": "🛏️", "title": "وقت النوم", "title_en": "Bedtime",
     "grad": ["#ddd6fe", "#c7d2fe"],
     "scene": "lying in bed hugging a teddy bear with a moon and stars visible through the window"},
    {"id": "day_help", "emoji": "🧹", "title": "بيساعد ماما", "title_en": "Helping at home",
     "grad": ["#bbf7d0", "#fed7aa"],
     "scene": "helping tidy the room, holding a broom and putting toys into a toy box"},
    {"id": "day_eat", "emoji": "🥗", "title": "بياكل صحي", "title_en": "Healthy eating",
     "grad": ["#d9f99d", "#bbf7d0"],
     "scene": "sitting at a table happily eating a plate of fruit and vegetables with a glass of milk"},
    {"id": "day_wash", "emoji": "🧼", "title": "بيغسل إيديه", "title_en": "Washing hands",
     "grad": ["#bae6fd", "#a5f3fc"],
     "scene": "washing hands at a sink with soap bubbles and a towel hanging nearby"},
    {"id": "day_read", "emoji": "📖", "title": "بيقرأ قصة", "title_en": "Reading",
     "grad": ["#c7d2fe", "#fbcfe8"],
     "scene": "sitting in a cozy reading corner holding an open story book with a stack of books beside"},
    {"id": "day_toys", "emoji": "🧸", "title": "بيرتب لعبه", "title_en": "Tidying toys",
     "grad": ["#fed7aa", "#fbcfe8"],
     "scene": "placing toys neatly on a shelf with a teddy bear, blocks and a toy car"},
    {"id": "day_doctor", "emoji": "🩹", "title": "عند الدكتور", "title_en": "Doctor visit",
     "grad": ["#bfdbfe", "#bbf7d0"],
     "scene": "sitting bravely on a clinic bed while a friendly doctor checks with a stethoscope"},
    {"id": "day_park", "emoji": "🛝", "title": "في الملاهي", "title_en": "Playground",
     "grad": ["#bbf7d0", "#fde68a"],
     "scene": "sliding down a playground slide with a swing and a sandbox nearby in a simple park"},
    {"id": "day_pray", "emoji": "🤲", "title": "بيصلي", "title_en": "Praying",
     "grad": ["#c7d2fe", "#bbf7d0"],
     "scene": "kneeling on a small prayer rug with hands raised in a calm quiet room"},
    {"id": "day_rain", "emoji": "☔", "title": "يوم مطر", "title_en": "Rainy day",
     "grad": ["#bae6fd", "#c7d2fe"],
     "scene": "wearing a raincoat and boots holding an umbrella and jumping in a puddle on a rainy street"},
]

OCCASIONS_SCENES = [
    {"id": "oc_birthday", "emoji": "🎂", "title": "عيد ميلاد", "title_en": "Birthday",
     "grad": ["#fbcfe8", "#fde68a"],
     "scene": "wearing a party hat blowing candles on a big birthday cake with balloons and streamers around"},
    {"id": "oc_newborn", "emoji": "👶", "title": "مولود جديد", "title_en": "New baby",
     "grad": ["#fbcfe8", "#bae6fd"],
     "scene": "gently holding a swaddled newborn baby while sitting in a cozy chair with a soft blanket"},
    {"id": "oc_graduation", "emoji": "🎓", "title": "تخرّج", "title_en": "Graduation",
     "grad": ["#c7d2fe", "#fde68a"],
     "scene": "wearing a graduation cap and gown holding a rolled diploma with confetti falling around"},
    {"id": "oc_travel", "emoji": "✈️", "title": "أول سفر", "title_en": "First trip",
     "grad": ["#bae6fd", "#ddd6fe"],
     "scene": "pulling a small suitcase at an airport gate with a plane visible through the big window"},
    {"id": "oc_newhome", "emoji": "🏡", "title": "بيت جديد", "title_en": "New home",
     "grad": ["#bbf7d0", "#fed7aa"],
     "scene": "carrying a moving box into a new house with a garden, a door and a welcome mat"},
    {"id": "oc_wedding", "emoji": "💐", "title": "فرح العيلة", "title_en": "Family wedding",
     "grad": ["#fbcfe8", "#ddd6fe"],
     "scene": "dressed up smartly holding a small flower basket at a family celebration with decorations and lights"},
    {"id": "oc_tooth", "emoji": "🦷", "title": "أول سنة وقعت", "title_en": "Lost tooth",
     "grad": ["#a5f3fc", "#fbcfe8"],
     "scene": "smiling widely showing a missing front tooth while holding a small tooth pillow"},
    {"id": "oc_summer", "emoji": "☀️", "title": "إجازة الصيف", "title_en": "Summer holiday",
     "grad": ["#fde68a", "#bae6fd"],
     "scene": "building a sandcastle on a sunny beach with a bucket, spade, beach ball and small waves"},
    {"id": "oc_winter", "emoji": "❄️", "title": "الشتا", "title_en": "Winter day",
     "grad": ["#bae6fd", "#e2e8f0"],
     "scene": "wearing a hat, scarf and mittens building a snowman with snowflakes falling around"},
    {"id": "oc_friends", "emoji": "🤝", "title": "مع الأصحاب", "title_en": "With friends",
     "grad": ["#bbf7d0", "#fde68a"],
     "scene": "playing happily with two cartoon friends in a park, holding hands and laughing together"},
    {"id": "oc_picnic", "emoji": "🧺", "title": "بيكنيك", "title_en": "Picnic",
     "grad": ["#d9f99d", "#fde68a"],
     "scene": "sitting on a picnic blanket with a basket of food under a big tree with birds flying above"},
    {"id": "oc_gift", "emoji": "🎁", "title": "هدية مفاجأة", "title_en": "Surprise gift",
     "grad": ["#fbcfe8", "#fed7aa"],
     "scene": "opening a big wrapped gift box with a ribbon, looking surprised and delighted, confetti around"},
]

# ── Story books ─────────────────────────────────────────────────────────────
# A story is NOT a coloring pack: its pages are full-color, they run in a fixed
# narrative order, and each page carries a line of Arabic narration ("text").
# "scene" is the English illustration brief and completes "the child is ...".

TREASURE_BEATS = [
    {"id": "st_tr_1", "emoji": "🗺️", "title": "الخريطة",
     "text": "لقى {name} خريطة قديمة تحت السرير… مكتوب عليها: كنز!",
     "scene": "kneeling on a bedroom floor unrolling an old treasure map, dust and golden light "
              "coming from under the bed, eyes wide with excitement"},
    {"id": "st_tr_2", "emoji": "🎒", "title": "بداية الرحلة",
     "text": "حط {name} الخريطة في الشنطة وقال: يلا نبدأ المغامرة!",
     "scene": "walking out of a cozy house at sunrise wearing a backpack and an explorer hat, "
              "a long winding path stretching ahead toward green hills"},
    {"id": "st_tr_3", "emoji": "🌳", "title": "الغابة",
     "text": "دخل غابة كبيرة… الشجر عالي والأصوات غريبة، بس {name} كمّل.",
     "scene": "walking bravely through a tall dense forest with sunbeams between the trees, "
              "curious eyes of small friendly animals peeking from the bushes"},
    {"id": "st_tr_4", "emoji": "🦊", "title": "الصاحب الجديد",
     "text": "قابل تعلب صغير تايه… خدّه معاه، وبقوا اتنين.",
     "scene": "crouching to comfort a small lost fox cub in a forest clearing, the fox looking up "
              "with big trusting eyes"},
    {"id": "st_tr_5", "emoji": "🌉", "title": "النهر",
     "text": "النهر كان واسع… بس {name} بنى قنطرة من الخشب وعدّى.",
     "scene": "carefully crossing a wide river on a makeshift wooden log bridge with the fox cub "
              "following, water sparkling below"},
    {"id": "st_tr_6", "emoji": "🕯️", "title": "الكهف",
     "text": "جوّه الكهف كان ضلمة… شغّل الشمعة ولقى الطريق.",
     "scene": "holding a small lantern inside a dark cave, warm light revealing glittering crystals "
              "on the rocky walls, the fox cub close by"},
    {"id": "st_tr_7", "emoji": "💎", "title": "الكنز",
     "text": "الكنز مكانش دهب… كان صندوق مليان كتب وألوان!",
     "scene": "opening a big wooden treasure chest that glows with books, crayons and paint "
              "instead of gold, face lit with wonder"},
    {"id": "st_tr_8", "emoji": "🏡", "title": "الرجوع",
     "text": "رجع {name} البيت… وأحلى كنز كان المغامرة نفسها ❤️",
     "scene": "arriving home at sunset with the fox cub, family waving from the doorway, "
              "the treasure chest carried under one arm, warm golden light"},
]

COURAGE_BEATS = [
    {"id": "st_co_1", "emoji": "😟", "title": "الصبح",
     "text": "أول يوم مدرسة… و{name} قلبه بيدق بسرعة.",
     "scene": "sitting on the edge of the bed in the early morning wearing a school uniform, "
              "looking worried, a packed backpack waiting by the door"},
    {"id": "st_co_2", "emoji": "🤗", "title": "حضن ماما",
     "text": "ماما حضنته وقالت: الشجاعة إنك تخاف… وتروح برضه.",
     "scene": "being hugged warmly by a parent at the front door, soft morning light, "
              "the parent kneeling to eye level, reassuring smile"},
    {"id": "st_co_3", "emoji": "🏫", "title": "الباب الكبير",
     "text": "باب المدرسة كان كبير أوي… بس {name} دخل.",
     "scene": "standing small in front of a very large school gate, hand on the strap of the "
              "backpack, taking one brave step forward"},
    {"id": "st_co_4", "emoji": "🪑", "title": "لوحده",
     "text": "قعد لوحده في الفصل… وكل الوشوش جديدة.",
     "scene": "sitting alone at a classroom desk among unfamiliar children, holding a pencil, "
              "looking quietly around the bright room"},
    {"id": "st_co_5", "emoji": "🙂", "title": "أول ابتسامة",
     "text": "طفل جنبه ابتسم وقال: تحب تلعب معايا؟",
     "scene": "another child at the next desk turning with a warm friendly smile and offering "
              "a shared box of crayons"},
    {"id": "st_co_6", "emoji": "⚽", "title": "الفسحة",
     "text": "في الفسحة لعبوا سوا… والخوف راح.",
     "scene": "playing happily with new friends in a sunny school playground, laughing while "
              "running after a ball"},
    {"id": "st_co_7", "emoji": "✋", "title": "رفع إيده",
     "text": "ورفع إيده في الفصل لأول مرة… وجاوب صح!",
     "scene": "raising a hand confidently in class while the teacher smiles at the board, "
              "classmates looking on"},
    {"id": "st_co_8", "emoji": "🌟", "title": "البطل",
     "text": "رجع البيت وقال: أنا قدرت! و{name} فعلاً بطل ❤️",
     "scene": "running into a parent's open arms at home, beaming with pride, backpack "
              "swinging, warm afternoon light through the window"},
]

SPACE_BEATS = [
    {"id": "st_sp_1", "emoji": "🌌", "title": "النجوم",
     "text": "كل ليلة {name} يبص من الشباك… ويسأل: فيه إيه فوق؟",
     "scene": "looking up through a bedroom window at a huge starry night sky, chin resting "
              "on hands, wonder on the face"},
    {"id": "st_sp_2", "emoji": "🔧", "title": "الصاروخ",
     "text": "قرر يبني صاروخ من الكراتين… واشتغل عليه أسبوع.",
     "scene": "building a cardboard rocket in a backyard with tape, paint and tools scattered "
              "around, tongue out in concentration"},
    {"id": "st_sp_3", "emoji": "🚀", "title": "الإقلاع",
     "text": "٣… ٢… ١… انطلق!",
     "scene": "blasting off inside the small rocket, flames and smoke below, the house and "
              "garden shrinking underneath"},
    {"id": "st_sp_4", "emoji": "🪐", "title": "بين الكواكب",
     "text": "عدّى جنب كواكب ملوّنة… وشاف الأرض صغيرة من بعيد.",
     "scene": "floating in the rocket window between colorful ringed planets, Earth a small "
              "blue marble far behind"},
    {"id": "st_sp_5", "emoji": "👽", "title": "الصاحب الفضائي",
     "text": "قابل كائن صغير أخضر… خاف الأول، بعدين ضحكوا.",
     "scene": "meeting a small cute green alien with big friendly eyes on a rocky colorful "
              "planet, both waving shyly at each other"},
    {"id": "st_sp_6", "emoji": "🌕", "title": "على القمر",
     "text": "مشيوا سوا على القمر… وكل خطوة كانت طايرة!",
     "scene": "bouncing in low gravity across the moon surface with the alien friend, "
              "footprints in grey dust, Earth glowing in the black sky"},
    {"id": "st_sp_7", "emoji": "🚩", "title": "العلم",
     "text": "زرع علم صغير مكتوب عليه اسمه… علشان يفضل هناك.",
     "scene": "planting a small flag on the moon with both hands, standing proudly beside it, "
              "the alien friend clapping"},
    {"id": "st_sp_8", "emoji": "🛏️", "title": "الرجوع",
     "text": "ورجع ينام… والنجوم بتغمز له من الشباك ✨",
     "scene": "back in bed hugging a toy rocket, smiling in sleep, stars twinkling through "
              "the window and a tiny alien doll on the shelf"},
]

SEA_BEATS = [
    {"id": "st_se_1", "emoji": "🤿", "title": "بدلة الغطس",
     "text": "لبس {name} بدلة الغطس… النهارده هينزل تحت البحر!",
     "scene": "standing on a sunny boat deck wearing diving goggles and flippers, the blue sea "
              "sparkling all around"},
    {"id": "st_se_2", "emoji": "🌊", "title": "أول نزلة",
     "text": "نزل الماء… والدنيا تحت كانت أحلى من فوق.",
     "scene": "diving underwater for the first time surrounded by bubbles, shafts of sunlight "
              "cutting through the blue water"},
    {"id": "st_se_3", "emoji": "🐬", "title": "الدولفين",
     "text": "دولفين لطيف جه يسلّم عليه… وقاله: اتبعني!",
     "scene": "greeting a smiling dolphin underwater, hand touching its nose, small colorful "
              "fish swirling around them"},
    {"id": "st_se_4", "emoji": "🪸", "title": "المدينة المرجانية",
     "text": "وصلوا مدينة مبنية من المرجان… بيوت وشوارع وكل حاجة!",
     "scene": "swimming into a magical coral city with arches, towers and glowing anemones, "
              "sea creatures going about their day"},
    {"id": "st_se_5", "emoji": "🐢", "title": "السلحفاة",
     "text": "لقى سلحفاة اتلخبطت في شبكة… فكّها بسرعة.",
     "scene": "carefully freeing a large sea turtle tangled in an old fishing net, gentle "
              "focused expression, the turtle looking grateful"},
    {"id": "st_se_6", "emoji": "🐚", "title": "الصدفة",
     "text": "السلحفاة شكرته… وودّته لصدفة كبيرة مقفولة.",
     "scene": "riding on the turtle's back toward a giant closed pearl shell resting on the "
              "glowing seabed"},
    {"id": "st_se_7", "emoji": "🦪", "title": "اللؤلؤة",
     "text": "الصدفة فتحت… وفيها لؤلؤة بتلمع زي القمر.",
     "scene": "the giant shell opening to reveal a huge glowing pearl lighting up the child's "
              "amazed face underwater"},
    {"id": "st_se_8", "emoji": "🏖️", "title": "الطلوع",
     "text": "طلع {name} فوق… ومعاه أحلى ذكرى في البحر ❤️",
     "scene": "surfacing at sunset next to the boat holding the pearl up high, the dolphin and "
              "turtle waving goodbye from the water"},
]

DREAM_BEATS = [
    {"id": "st_dr_1", "emoji": "😴", "title": "الحلم",
     "text": "نام {name} وهو بيفكر: أنا هبقى إيه لما أكبر؟",
     "scene": "sleeping peacefully in bed with a dreamy cloud of tiny icons — a stethoscope, a "
              "rocket, a paintbrush — floating above the pillow"},
    {"id": "st_dr_2", "emoji": "✨", "title": "كبر فجأة",
     "text": "وفجأة… لقى نفسه كبير وواقف في مكان جديد!",
     "scene": "standing surprised in a bright new workplace, still with the same young face but "
              "wearing a grown-up work uniform, light streaming in"},
    {"id": "st_dr_3", "emoji": "🚪", "title": "أول يوم",
     "text": "أول يوم شغل… كل حاجة جديدة وصعبة شوية.",
     "scene": "walking into a busy workplace on the first day holding a folder, coworkers "
              "greeting warmly, slightly nervous smile"},
    {"id": "st_dr_4", "emoji": "😧", "title": "المشكلة",
     "text": "وحصلت مشكلة… وكل الناس بصّت له.",
     "scene": "facing a sudden problem at work with everyone turning to look, worried but "
              "standing firm, papers and tools scattered"},
    {"id": "st_dr_5", "emoji": "💡", "title": "الفكرة",
     "text": "فكّر شوية… وافتكر حاجة اتعلمها وهو صغير.",
     "scene": "a bright idea moment with a glowing lightbulb above the head, a flashback bubble "
              "showing the same child as a small kid learning"},
    {"id": "st_dr_6", "emoji": "🛠️", "title": "الحل",
     "text": "اشتغل بإيديه… وحلّها لوحده!",
     "scene": "working intently with both hands to fix the problem, sleeves rolled up, focused "
              "determined expression, tools in use"},
    {"id": "st_dr_7", "emoji": "👏", "title": "الشكر",
     "text": "كل الناس صفّقوا… وقالوا: شكراً!",
     "scene": "surrounded by smiling grateful people clapping, receiving a warm handshake, "
              "sunlight and confetti-like sparkle in the air"},
    {"id": "st_dr_8", "emoji": "📚", "title": "صحي",
     "text": "صحي {name} من النوم وقال: يبقى لازم أذاكر من دلوقتي!",
     "scene": "waking up in bed with a big determined smile, morning light on the face, reaching "
              "for a school book on the bedside table"},
]

RAMADAN_BEATS = [
    {"id": "st_ra_1", "emoji": "🌙", "title": "الهلال",
     "text": "شاف {name} الهلال في السما… وعرف إن رمضان جه!",
     "scene": "standing on a balcony at dusk pointing at a thin crescent moon, warm lanterns "
              "glowing on the street below"},
    {"id": "st_ra_2", "emoji": "🏮", "title": "الفانوس",
     "text": "علّق فانوسه على الشباك… ونوّر البيت كله.",
     "scene": "hanging a big colorful Ramadan lantern by a window, warm golden light spreading "
              "across a cozy room"},
    {"id": "st_ra_3", "emoji": "🥘", "title": "تحضير الفطار",
     "text": "ساعد ماما في المطبخ… وحط الطبق على السفرة.",
     "scene": "helping in a warm kitchen carrying a dish to a full iftar table set with dates, "
              "soup and lanterns"},
    {"id": "st_ra_4", "emoji": "💧", "title": "أول يوم صيام",
     "text": "أول يوم يصوم… وكان صعب، بس كمّل.",
     "scene": "sitting patiently by a window in the afternoon light with a glass of water "
              "waiting on the table, determined little face"},
    {"id": "st_ra_5", "emoji": "🔊", "title": "المدفع",
     "text": "وسمع المدفع… ألله أكبر! أحلى تمرة في الدنيا.",
     "scene": "joyfully biting into a date at the iftar table the moment the cannon sounds, "
              "family hands reaching around the table, warm light"},
    {"id": "st_ra_6", "emoji": "🍲", "title": "للجيران",
     "text": "شال طبق للجيران… وقال: كل سنة وانتوا طيبين!",
     "scene": "carrying a covered dish to a neighbor's door in a lit hallway, smiling as the "
              "neighbor opens the door happily"},
    {"id": "st_ra_7", "emoji": "🕌", "title": "التراويح",
     "text": "وراح مع بابا التراويح… والمسجد كان مليان نور.",
     "scene": "walking hand in hand with a parent toward a beautiful lit mosque at night, "
              "minaret and crescent against a deep blue sky"},
    {"id": "st_ra_8", "emoji": "🎁", "title": "العيد",
     "text": "وجه العيد… وكل سنة و{name} طيب ❤️",
     "scene": "wearing new Eid clothes holding gifts and balloons, family celebrating around a "
              "table of sweets, festive decorations everywhere"},
]

# Each pack is a separate book type in the admin book picker.
# Theme keys drive the whole book, not just the scene list:
#   book_title / tagline / ending_line — Arabic text burned into cover + ending
#   cover_scene / ending_scene         — English illustration brief for those two pages
#   mood / palette                     — style fragments so pages of one book match
SCENE_PACKS = [
    {"id": "jobs", "emoji": "🩺", "title": "كتاب المهن",
     "desc": "الطفل في 12 مهنة — دكتور، مهندس، طيار…",
     "book_title": "كتاب المهن",
     "tagline": "لون معايا كل المهن!",
     "ending_line": "رحلة المهن",
     "cover_scene": "inside a bright magical art studio full of paintings, brushes, sunlight and "
                    "colorful canvases, with small icons of a stethoscope, a hard hat and a paint "
                    "palette floating around",
     "ending_scene": "sitting happily behind an open coloring book surrounded by little symbols of "
                     "every profession — a stethoscope, a chef hat, a rocket and a paintbrush",
     "mood": "friendly professional setting, one clear tool of the job in hand",
     "palette": ["#7c3aed", "#38bdf8"],
     "scenes": JOBS_SCENES},

    {"id": "heroes", "emoji": "🦸", "title": "كتاب الأبطال والخيال",
     "desc": "سوبر هيرو، فارس، قرصان، ساحر، تنين…",
     "book_title": "كتاب الأبطال",
     "tagline": "لون مغامرتك يا بطل!",
     "ending_line": "رحلة الأبطال",
     "cover_scene": "standing heroically on a cliff at sunset wearing a flowing cape, with a castle, "
                    "a friendly dragon and glowing magic sparks in the background",
     "ending_scene": "sitting on a treasure chest holding a shield and a magic wand, with a cape, a "
                     "small dragon and a starry sky behind",
     "mood": "bold heroic action pose, dynamic energy lines, adventurous atmosphere",
     "palette": ["#dc2626", "#1d4ed8"],
     "scenes": HEROES_SCENES},

    {"id": "animals", "emoji": "🦁", "title": "كتاب الحيوانات",
     "desc": "أسد، دولفين، ديناصور، سفاري، مزرعة…",
     "book_title": "كتاب الحيوانات",
     "tagline": "لون أصحابك الحيوانات!",
     "ending_line": "رحلة الحيوانات",
     "cover_scene": "hugging a friendly cartoon lion cub in a lush jungle clearing with a giraffe, a "
                    "parrot and a dolphin peeking from a pond, big leaves and vines framing the scene",
     "ending_scene": "surrounded by all the animal friends from the book — a lion, a puppy, a bunny, a "
                     "penguin and a parrot — waving goodbye in a green meadow",
     "mood": "warm friendly animals with big round eyes, gentle nature background",
     "palette": ["#16a34a", "#f59e0b"],
     "scenes": ANIMALS_SCENES},

    {"id": "sports", "emoji": "⚽", "title": "كتاب الرياضة",
     "desc": "كورة، سباحة، كاراتيه، جمباز، بطولة…",
     "book_title": "كتاب الأبطال الرياضيين",
     "tagline": "لون وابدأ التمرين!",
     "ending_line": "رحلة البطولة",
     "cover_scene": "standing proudly in sportswear inside a stadium with cheering crowd silhouettes, "
                    "holding a football, with a medal, a trophy and a running track behind",
     "ending_scene": "standing on the first place podium holding a golden trophy with a medal, "
                     "confetti falling and a stadium scoreboard behind",
     "mood": "energetic sporty motion, motion lines, stadium or court setting",
     "palette": ["#0891b2", "#22c55e"],
     "scenes": SPORTS_SCENES},

    {"id": "girls", "emoji": "👑", "title": "كتاب عالم البنات",
     "desc": "باليرينا، أميرة، جنية، يونيكورن…",
     "book_title": "كتاب عالمي الجميل",
     "tagline": "لون عالمك الوردي!",
     "ending_line": "رحلة الأحلام",
     "cover_scene": "wearing a princess gown and a small crown in an enchanted garden with a unicorn, "
                    "butterflies, a rainbow and floating flower petals",
     "ending_scene": "sitting on a flower swing with a unicorn and a fairy friend under a rainbow, "
                     "petals and sparkles floating around",
     "mood": "soft dreamy fairytale atmosphere, flowers sparkles and ribbons in the scene",
     "palette": ["#ec4899", "#a855f7"],
     "scenes": GIRLS_SCENES},

    {"id": "egypt", "emoji": "🐫", "title": "كتاب مصر والتراث",
     "desc": "فرعون، الأهرامات، فانوس رمضان، النيل…",
     "book_title": "كتاب مصر الجميلة",
     "tagline": "لون بلدك يا حبيبي!",
     "ending_line": "رحلة مصر",
     "cover_scene": "wearing a pharaoh nemes headdress standing in front of the great pyramids and the "
                    "Sphinx at golden sunset, with a felucca sail on the Nile and a Ramadan lantern "
                    "glowing at the side",
     "ending_scene": "sitting on a woven mat holding a Ramadan lantern with the pyramids, palm trees "
                     "and a Nile sunset behind, warm golden light",
     "mood": "warm golden Egyptian light, traditional Arabic patterns and ornaments in the background",
     "palette": ["#d97706", "#0e7490"],
     "scenes": EGYPT_SCENES},

    {"id": "daily", "emoji": "🪥", "title": "كتاب يوميات الطفل",
     "desc": "المدرسة، النوم، النضافة، المساعدة…",
     "book_title": "كتاب يومي",
     "tagline": "لون يومك خطوة بخطوة!",
     "ending_line": "رحلة يومي",
     "cover_scene": "standing in a cozy sunny bedroom with a backpack, a toothbrush, a story book and a "
                    "teddy bear arranged around, a wall clock showing a friendly morning",
     "ending_scene": "tucked in bed hugging a teddy bear with a warm lamp, a story book and a moon and "
                     "stars through the window",
     "mood": "calm cozy everyday home setting, soft reassuring atmosphere",
     "palette": ["#0ea5e9", "#84cc16"],
     "scenes": DAILY_SCENES},

    {"id": "occasions", "emoji": "🎂", "title": "كتاب المناسبات",
     "desc": "عيد ميلاد، تخرّج، سفر، صيف، شتا…",
     "book_title": "كتاب المناسبات",
     "tagline": "لون أحلى لحظاتك!",
     "ending_line": "رحلة الذكريات",
     "cover_scene": "wearing a party hat behind a big birthday cake with candles, balloons, gift boxes "
                    "and colorful streamers filling the scene, confetti in the air",
     "ending_scene": "surrounded by memories of the book — balloons, a graduation cap, a suitcase and a "
                     "gift box — holding a photo album with confetti falling",
     "mood": "festive celebration atmosphere, balloons confetti and decorations in the scene",
     "palette": ["#f43f5e", "#f59e0b"],
     "scenes": OCCASIONS_SCENES},
]


# Story books. `art_key` is repeated verbatim on every page of the story so all
# pages look like one book — this is what keeps a storybook visually coherent.
STORY_PACKS = [
    {"id": "story_treasure", "kind": "story", "emoji": "🗺️", "title": "قصة الكنز المفقود",
     "desc": "خريطة قديمة، غابة، كهف… وكنز مش زي ما تتوقع",
     "book_title": "الكنز المفقود",
     "tagline": "مغامرة بطلها إنت!",
     "ending_line": "تمّت المغامرة",
     "moral": "أحلى كنز هو الرحلة نفسها",
     "art_key": "warm adventurous storybook painting, rich earthy greens and golds, soft painted "
                "textures, gentle rim light, cinematic depth",
     "cover_scene": "standing on a hilltop at sunrise holding an old treasure map, a fox cub at "
                    "the feet, a winding path and distant cave behind",
     "ending_scene": "sitting on the treasure chest at home surrounded by books and crayons with "
                     "the fox cub asleep nearby, warm lamp light",
     "palette": ["#b45309", "#15803d"],
     "colors": "warm forest greens, golden amber, sunlit sandy browns, deep teal shadows",
     "scenes": TREASURE_BEATS},

    {"id": "story_courage", "kind": "story", "emoji": "🌟", "title": "قصة أول يوم مدرسة",
     "desc": "الخوف، الحضن، أول صاحب… وأول مرة يرفع إيده",
     "book_title": "أشجع طفل",
     "tagline": "الشجاعة إنك تخاف وتروح برضه",
     "ending_line": "أنا قدرت!",
     "moral": "الشجاعة مش عدم الخوف",
     "art_key": "soft gentle storybook illustration, warm pastel palette, cozy morning light, "
                "tender emotional expressions, watercolor texture",
     "cover_scene": "standing at a big school gate with a backpack, looking up bravely, morning "
                    "sun behind the school building",
     "ending_scene": "running happily into a parent's open arms after school, backpack swinging, "
                     "golden afternoon light",
     "palette": ["#f59e0b", "#38bdf8"],
     "colors": "soft peach, warm cream, gentle sky blue, buttery morning yellow",
     "scenes": COURAGE_BEATS},

    {"id": "story_space", "kind": "story", "emoji": "🚀", "title": "قصة رحلة الفضاء",
     "desc": "صاروخ كرتون، كواكب، صاحب فضائي، ومشي على القمر",
     "book_title": "رحلة إلى النجوم",
     "tagline": "٣… ٢… ١… انطلق!",
     "ending_line": "رحلة النجوم",
     "moral": "الخيال بيوصلك لأبعد مكان",
     "art_key": "vivid cosmic storybook illustration, deep blues and purples with glowing stars, "
                "soft nebula gradients, luminous highlights, dreamy scale",
     "cover_scene": "sitting inside a cardboard rocket window flying between colorful planets "
                    "with Earth glowing far below",
     "ending_scene": "asleep in bed hugging a toy rocket while stars twinkle through the window "
                     "and a tiny alien doll sits on the shelf",
     "palette": ["#4338ca", "#0ea5e9"],
     "colors": "deep indigo and violet, glowing cyan, starlight white sparks, warm orange rocket glow",
     "scenes": SPACE_BEATS},

    {"id": "story_sea", "kind": "story", "emoji": "🐬", "title": "قصة تحت البحر",
     "desc": "غطس، دولفين، مدينة مرجانية، ولؤلؤة بتلمع",
     "book_title": "لؤلؤة البحر",
     "tagline": "تحت الماء فيه عالم تاني!",
     "ending_line": "رحلة البحر",
     "moral": "اللي بيساعد بيلاقي أحلى مكافأة",
     "art_key": "luminous underwater storybook illustration, turquoise and aqua palette, caustic "
                "light rays through water, floating bubbles, soft glow",
     "cover_scene": "diving underwater with goggles beside a smiling dolphin, a glowing coral "
                    "city in the distance below",
     "ending_scene": "surfacing at sunset holding a glowing pearl while the dolphin and turtle "
                     "wave goodbye from the water",
     "palette": ["#0891b2", "#14b8a6"],
     "colors": "turquoise and aqua, deep ocean blue, coral pink, sunlit golden green",
     "scenes": SEA_BEATS},

    {"id": "story_dream", "kind": "story", "emoji": "💭", "title": "قصة لما كبرت",
     "desc": "حلم إنه كبر واشتغل… وقابل مشكلة وحلّها",
     "book_title": "لما كبرت",
     "tagline": "إنت هتبقى إيه لما تكبر؟",
     "ending_line": "رحلة الحلم",
     "moral": "اللي بتتعلمه دلوقتي بينفعك بكرة",
     "art_key": "bright hopeful storybook illustration, clean modern shapes, warm sunlit "
                "interiors, soft shadows, optimistic palette",
     "cover_scene": "standing tall in a grown-up work uniform while a dreamy cloud above shows "
                    "the same child asleep in bed",
     "ending_scene": "waking up in bed with a determined smile reaching for a school book, "
                     "morning light across the room",
     "palette": ["#7c3aed", "#f59e0b"],
     "colors": "warm sunlit amber, soft violet, clean sky blue, optimistic cream",
     "scenes": DREAM_BEATS},

    {"id": "story_ramadan", "kind": "story", "emoji": "🌙", "title": "قصة رمضان",
     "desc": "الهلال، الفانوس، أول يوم صيام، المدفع… والعيد",
     "book_title": "رمضان كريم",
     "tagline": "أحلى شهر في السنة!",
     "ending_line": "كل سنة وإنت طيب",
     "moral": "رمضان فرحة ومشاركة",
     "art_key": "warm nocturnal storybook illustration, deep blue night skies with golden lantern "
                "glow, Arabic ornamental patterns, cozy family warmth",
     "cover_scene": "holding a big glowing Ramadan lantern on a balcony under a crescent moon "
                    "with warm lit streets below",
     "ending_scene": "wearing new Eid clothes holding gifts and balloons with family celebrating "
                     "around a table of sweets",
     "palette": ["#1e3a8a", "#d97706"],
     "colors": "deep midnight blue, golden lantern amber, warm brass, soft ivory",
     "scenes": RAMADAN_BEATS},
]

# Coloring packs defined above are the default kind.
for _p in SCENE_PACKS:
    _p.setdefault("kind", "coloring")
SCENE_PACKS.extend(STORY_PACKS)

DEFAULT_PACK_ID = "jobs"


def is_story_pack(pack: dict) -> bool:
    return (pack or {}).get("kind") == "story"

# ── Book picker presentation ────────────────────────────────────────────────
# Tags + age hint + card order. Presentation only: nothing below changes any
# book's scenes, cover, ending or prompts — it only affects how the picker
# at #order/<id>/book/new lists the types.

TAG_LABELS = {
    "popular":     "الأكثر طلباً",
    "new":         "جديد",
    "gift":        "هدية",
    "seasonal":    "موسمي",
    "educational": "تربوي",
}

PACK_DISPLAY = {
    "jobs":      {"tags": ["popular"],           "age": "4-8 سنين"},
    "heroes":    {"tags": ["new"],               "age": "4-9 سنين"},
    "animals":   {"tags": ["new"],               "age": "3-6 سنين"},
    "girls":     {"tags": ["new"],               "age": "4-8 سنين"},
    "sports":    {"tags": ["new"],               "age": "5-10 سنين"},
    "occasions": {"tags": ["gift", "seasonal"],  "age": "كل الأعمار"},
    "egypt":     {"tags": ["new", "seasonal"],   "age": "5-10 سنين"},
    "daily":     {"tags": ["educational"],       "age": "3-5 سنين"},
    # Stories
    "story_treasure": {"tags": ["new"],                    "age": "5-9 سنين"},
    "story_courage":  {"tags": ["new", "gift"],            "age": "4-7 سنين"},
    "story_space":    {"tags": ["new"],                    "age": "5-9 سنين"},
    "story_sea":      {"tags": ["new"],                    "age": "4-8 سنين"},
    "story_dream":    {"tags": ["new", "educational"],     "age": "6-10 سنين"},
    "story_ramadan":  {"tags": ["new", "seasonal"],        "age": "كل الأعمار"},
}

# Fun/gift books first, the educational one last. "jobs" must stay at index 0 —
# it is the fallback pack for pack_by_id() and pack_for_scene().
PACK_ORDER = [
    # Coloring books
    "jobs", "heroes", "animals", "girls", "sports", "occasions", "egypt", "daily",
    # Story books
    "story_courage", "story_treasure", "story_space", "story_sea",
    "story_dream", "story_ramadan",
]

SCENE_PACKS.sort(
    key=lambda p: PACK_ORDER.index(p["id"]) if p["id"] in PACK_ORDER else len(PACK_ORDER)
)
for _pack in SCENE_PACKS:
    _display = PACK_DISPLAY.get(_pack["id"], {})
    _pack["tags"] = _display.get("tags", [])
    _pack["age"] = _display.get("age", "")
assert SCENE_PACKS[0]["id"] == DEFAULT_PACK_ID, "default pack must stay first"


def pack_by_id(pack_id: Optional[str]) -> dict:
    """Resolve a pack id to its theme; falls back to the professions pack."""
    for p in SCENE_PACKS:
        if p["id"] == pack_id:
            return p
    return SCENE_PACKS[0]


def pack_for_scene(scene_id: str) -> dict:
    """Which book does this scene belong to? Used when the client sends no pack id."""
    for p in SCENE_PACKS:
        if any(s["id"] == scene_id for s in p["scenes"]):
            return p
    return SCENE_PACKS[0]

# Public tool (/app) keeps showing the professions pack only.
SCENES = JOBS_SCENES
# Every scene across all packs — used for id lookup and the admin picker.
ALL_SCENES = [s for pack in SCENE_PACKS for s in pack["scenes"]]

FONT_CANDIDATES = [
    "/System/Library/Fonts/SFArabic.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Tahoma.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

CUSTOM_ID_RE = re.compile(r"^custom_[a-f0-9]{8,24}$")
SCENE_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
SHARE_ID_RE = re.compile(r"^[a-f0-9]{16,64}$")

DATA_DIR = Path(os.environ.get("COLORING_DATA_DIR", Path(__file__).resolve().parent / "data"))
# Persist sessions under data (not /tmp) so restarts / multi-worker don't lose work mid-book
SESSIONS_DIR = DATA_DIR / "sessions"
SHARES_DIR = DATA_DIR / "shares"
DB_PATH = DATA_DIR / "analytics.db"
SPECIAL_ORDERS_DIR = DATA_DIR / "special_orders"
DATA_DIR.mkdir(exist_ok=True)
SESSIONS_DIR.mkdir(exist_ok=True)
SHARES_DIR.mkdir(exist_ok=True)
SPECIAL_ORDERS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 56 * 1024 * 1024  # 56 MB — covers 50 MB PDF uploads
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if APP_URL.startswith("https://"):
    app.config["SESSION_COOKIE_SECURE"] = True

oauth = OAuth(app)
if google_ready():
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

_db_lock = threading.Lock()
_scheduler: Optional[BackgroundScheduler] = None

# Background jobs for long Kie generations (Cloudflare kills HTTP > ~100s)
# Stored on disk so any gunicorn worker can poll the same job.
_JOBS_DIR = DATA_DIR / "jobs"
_JOBS_DIR.mkdir(exist_ok=True)
_jobs_lock = threading.Lock()


def _job_path(job_id: str) -> Path:
    return _JOBS_DIR / f"{job_id}.json"


def _prune_old_jobs(max_age_sec: int = 3600) -> None:
    now = time.time()
    try:
        for p in _JOBS_DIR.glob("*.json"):
            try:
                if now - p.stat().st_mtime > max_age_sec:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def _job_write(job_id: str, payload: dict) -> None:
    path = _job_path(job_id)
    tmp = path.with_suffix(".tmp")
    data = dict(payload)
    data["job_id"] = job_id
    raw = json.dumps(data, ensure_ascii=False)
    with _jobs_lock:
        tmp.write_text(raw, encoding="utf-8")
        tmp.replace(path)


def _job_read(job_id: str) -> Optional[dict]:
    path = _job_path(job_id)
    if not path.exists():
        return None
    try:
        with _jobs_lock:
            data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _page_for_client(page: dict, session_id: str, *, include_b64: bool = False) -> dict:
    """Return page payload; omit huge base64 by default (client uses preview URL)."""
    if not isinstance(page, dict):
        return page
    out = dict(page)
    sid = out.get("scene_id") or ""
    if sid and session_id:
        out["preview_url"] = f"/admin/api/quick-book/preview/{session_id}/{sid}"
    if not include_b64:
        out.pop("image_b64", None)
    return out


def _enqueue_job(kind: str, worker_fn) -> str:
    """Run heavy work off the request thread so Cloudflare won't 524."""
    _prune_old_jobs()
    job_id = secrets.token_hex(12)
    _job_write(job_id, {
        "status": "running",
        "kind": kind,
        "error": None,
        "result": None,
        "created_at": time.time(),
    })

    def _run():
        try:
            result = worker_fn()
            cur = _job_read(job_id) or {}
            cur["status"] = "done"
            cur["result"] = result
            cur["error"] = None
            _job_write(job_id, cur)
        except Exception as e:
            cur = _job_read(job_id) or {}
            cur["status"] = "error"
            cur["error"] = friendly_error(e)
            cur["result"] = None
            _job_write(job_id, cur)

    threading.Thread(target=_run, name=f"qb-job-{kind}-{job_id[:6]}", daemon=True).start()
    return job_id


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "error": "وصلت للحد الأقصى: 5 كتب في الساعة. جرّب تاني بعد شوية.",
        "error_en": "Rate limit reached: 5 books per hour. Please try again later.",
    }), 429


@app.errorhandler(500)
def api_internal_error(e):
    """Always return JSON for admin APIs — never HTML error pages for fetch()."""
    wants_json = (
        request.path.startswith("/admin/api")
        or request.path.startswith("/api")
        or "application/json" in (request.headers.get("Accept") or "")
    )
    if wants_json:
        return jsonify({
            "error": "حصل خطأ داخلي في السيرفر. لو الصورة تولّدت، حدّث الصفحة وهتلاقيها.",
        }), 500
    return ("Internal Server Error", 500)


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock:
        conn = db_connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                book_credits INTEGER DEFAULT 0,
                google_id TEXT UNIQUE,
                auth_provider TEXT DEFAULT 'email'
            );
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                ip TEXT,
                session_id TEXT,
                pages INTEGER DEFAULT 0,
                user_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS scene_picks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                scene_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                special_reference TEXT NOT NULL UNIQUE,
                amount_cents INTEGER NOT NULL,
                credits INTEGER NOT NULL,
                status TEXT NOT NULL,
                paymob_order_id TEXT,
                paymob_txn_id TEXT,
                created_at TEXT NOT NULL,
                paid_at TEXT
            );
            CREATE TABLE IF NOT EXISTS special_orders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                child_name  TEXT NOT NULL,
                client_name TEXT,
                phone       TEXT,
                email       TEXT,
                notes       TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TEXT NOT NULL,
                updated_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS special_order_photos (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES special_orders(id) ON DELETE CASCADE,
                filename TEXT NOT NULL
            );
            """
        )
        # Migrate older DBs that lack user_id / book_credits
        def _add_col(table: str, column: str, decl: str):
            """Add column if missing; ignore races / already-exists errors."""
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column in cols:
                return
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {decl}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        _add_col("books", "user_id", "user_id INTEGER")
        _add_col("users", "book_credits", "book_credits INTEGER DEFAULT 0")
        _add_col("users", "google_id", "google_id TEXT")
        _add_col("users", "auth_provider", "auth_provider TEXT DEFAULT 'email'")
        # Unique index for google_id (ignore NULLs / duplicates safely)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id) "
            "WHERE google_id IS NOT NULL"
        )
        # Migrate special_orders columns
        for col, decl in (
            ("pdf_filename", "pdf_filename TEXT"),
            ("assigned_to", "assigned_to TEXT"),
            ("share_token", "share_token TEXT"),
            ("share_expires_at", "share_expires_at TEXT"),
            ("book_session_id", "book_session_id TEXT"),
            ("book_scenes", "book_scenes TEXT"),
            ("book_page_count", "book_page_count INTEGER"),
            ("book_updated_at", "book_updated_at TEXT"),
            ("book_progress", "book_progress TEXT"),
        ):
            _add_col("special_orders", col, decl)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_special_orders_share_token "
            "ON special_orders(share_token) WHERE share_token IS NOT NULL"
        )
        # Multiple books per special order (child)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS special_order_books (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id        INTEGER NOT NULL REFERENCES special_orders(id) ON DELETE CASCADE,
                pack_id         TEXT,
                title           TEXT,
                session_id      TEXT,
                scenes          TEXT,
                page_count      INTEGER,
                progress        TEXT,
                pdf_filename    TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT,
                book_updated_at TEXT
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_special_order_books_order "
            "ON special_order_books(order_id)"
        )
        # One-time: migrate single-book columns on special_orders into the new table
        try:
            legacy_orders = conn.execute(
                """
                SELECT id, child_name, book_session_id, book_scenes, book_page_count,
                       book_progress, book_updated_at, pdf_filename, created_at, updated_at
                FROM special_orders
                WHERE book_session_id IS NOT NULL
                   OR book_scenes IS NOT NULL
                   OR book_progress IS NOT NULL
                   OR (pdf_filename IS NOT NULL AND pdf_filename != '')
                """
            ).fetchall()
            for ord_row in legacy_orders:
                oid = ord_row["id"]
                has = conn.execute(
                    "SELECT 1 FROM special_order_books WHERE order_id = ? LIMIT 1",
                    (oid,),
                ).fetchone()
                if has:
                    continue
                # Infer pack from scenes
                pack_id = "jobs"
                scenes_raw = ord_row["book_scenes"]
                try:
                    scenes_list = json.loads(scenes_raw) if isinstance(scenes_raw, str) and scenes_raw else []
                except (json.JSONDecodeError, TypeError):
                    scenes_list = []
                if scenes_list and isinstance(scenes_list, list) and scenes_list:
                    pack_id = pack_for_scene(str(scenes_list[0])).get("id") or "jobs"
                pack = pack_by_id(pack_id)
                title = (pack.get("book_title") or pack.get("title") or "كتاب تلوين")
                now = ord_row["updated_at"] or ord_row["created_at"] or datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    INSERT INTO special_order_books
                    (order_id, pack_id, title, session_id, scenes, page_count, progress,
                     pdf_filename, created_at, updated_at, book_updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        oid,
                        pack_id,
                        title,
                        ord_row["book_session_id"],
                        scenes_raw,
                        ord_row["book_page_count"],
                        ord_row["book_progress"],
                        ord_row["pdf_filename"],
                        ord_row["created_at"] or now,
                        now,
                        ord_row["book_updated_at"] or now,
                    ),
                )
                book_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                # Move legacy page folder into books/{id}/ if present
                legacy_dir = SPECIAL_ORDERS_DIR / str(oid) / "book"
                new_dir = SPECIAL_ORDERS_DIR / str(oid) / "books" / str(book_id)
                if legacy_dir.exists() and not new_dir.exists():
                    try:
                        new_dir.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(str(legacy_dir), str(new_dir))
                    except OSError:
                        pass
        except Exception:
            pass
        conn.commit()
        conn.close()


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\u0600-\u06FF]{3,30}$")


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT id, email, username, created_at, book_credits, auth_provider, google_id "
            "FROM users WHERE id = ?",
            (uid,),
        ).fetchone()
        conn.close()
    if not row:
        session.clear()
        return None
    return dict(row)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({
                "error": "لازم تسجّل دخول الأول عشان تولّد الكتاب.",
                "error_en": "Please log in to generate the book.",
                "auth_required": True,
            }), 401
        return view(*args, **kwargs)
    return wrapped


def is_admin() -> bool:
    return bool(session.get("is_admin"))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            wants_json = (
                request.path.startswith("/admin/api")
                or request.path == "/analytics"
                or "application/json" in (request.headers.get("Accept") or "")
            )
            if wants_json:
                return jsonify({
                    "error": "لازم تسجّل دخول الأدمن.",
                    "auth_required": True,
                }), 401
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


def scene_title(scene_id: str) -> str:
    for s in ALL_SCENES:
        if s["id"] == scene_id:
            return f'{s["emoji"]} {s["title"]}'
    if scene_id.startswith("custom_"):
        return f"✨ مخصص ({scene_id[-8:]})"
    return scene_id


def collect_admin_stats() -> dict:
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")
    week_ago = (now - timedelta(days=7)).isoformat()

    with _db_lock:
        conn = db_connect()
        books_today = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE created_at LIKE ?",
            (f"{today}%",),
        ).fetchone()["c"]
        books_yesterday = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE created_at >= ? AND created_at < ?",
            ((now - timedelta(days=1)).strftime("%Y-%m-%d"), today),
        ).fetchone()["c"]
        books_week = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE created_at >= ?",
            (week_ago,),
        ).fetchone()["c"]
        books_month = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE created_at LIKE ?",
            (f"{month}%",),
        ).fetchone()["c"]
        books_total = conn.execute("SELECT COUNT(*) AS c FROM books").fetchone()["c"]
        pages_total = conn.execute(
            "SELECT COALESCE(SUM(pages), 0) AS c FROM books"
        ).fetchone()["c"]
        users_total = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        users_week = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE created_at >= ?",
            (week_ago,),
        ).fetchone()["c"]
        users_today = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE created_at LIKE ?",
            (f"{today}%",),
        ).fetchone()["c"]
        top = conn.execute(
            """
            SELECT scene_id, COUNT(*) AS c
            FROM scene_picks
            GROUP BY scene_id
            ORDER BY c DESC
            LIMIT 12
            """
        ).fetchall()
        recent_books = conn.execute(
            """
            SELECT b.id, b.created_at, b.ip, b.pages, b.session_id,
                   u.username, u.email
            FROM books b
            LEFT JOIN users u ON u.id = b.user_id
            ORDER BY b.id DESC
            LIMIT 25
            """
        ).fetchall()
        recent_users = conn.execute(
            """
            SELECT id, username, email, created_at
            FROM users
            ORDER BY id DESC
            LIMIT 25
            """
        ).fetchall()

        # Last 14 days book counts (fill missing days with 0)
        cutoff_day = (now - timedelta(days=13)).strftime("%Y-%m-%d")
        daily_rows = conn.execute(
            """
            SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS c
            FROM books
            WHERE created_at >= ?
            GROUP BY day
            ORDER BY day
            """,
            (cutoff_day,),
        ).fetchall()
        # Last 14 days revenue (paid payments by paid_at date)
        daily_revenue_rows = conn.execute(
            """
            SELECT substr(paid_at, 1, 10) AS day, COALESCE(SUM(amount_cents), 0) AS c
            FROM payments
            WHERE status = 'paid' AND paid_at >= ?
            GROUP BY day
            ORDER BY day
            """,
            (cutoff_day,),
        ).fetchall()
        # Last 14 days new user signups
        daily_users_rows = conn.execute(
            """
            SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS c
            FROM users
            WHERE created_at >= ?
            GROUP BY day
            ORDER BY day
            """,
            (cutoff_day,),
        ).fetchall()
        conn.close()

    by_day = {r["day"]: int(r["c"]) for r in daily_rows}
    rev_by_day = {r["day"]: int(r["c"]) for r in daily_revenue_rows}
    usr_by_day = {r["day"]: int(r["c"]) for r in daily_users_rows}
    daily = []
    daily_revenue = []
    daily_users = []
    for i in range(13, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        daily.append({"day": d, "label": d[5:], "count": by_day.get(d, 0)})
        daily_revenue.append({
            "day": d,
            "label": d[5:],
            "count": round(rev_by_day.get(d, 0) / 100, 2),
        })
        daily_users.append({"day": d, "label": d[5:], "count": usr_by_day.get(d, 0)})
    max_daily = max((d["count"] for d in daily), default=0) or 1
    max_daily_revenue = max((d["count"] for d in daily_revenue), default=0) or 1
    max_daily_users = max((d["count"] for d in daily_users), default=0) or 1

    sessions_count = sum(1 for p in SESSIONS_DIR.iterdir() if p.is_dir()) if SESSIONS_DIR.exists() else 0
    shares_count = sum(1 for p in SHARES_DIR.glob("*.json")) if SHARES_DIR.exists() else 0
    avg_pages = round((pages_total or 0) / books_total, 1) if books_total else 0

    # Payments stats
    with _db_lock:
        conn = db_connect()
        payments_total = conn.execute("SELECT COUNT(*) AS c FROM payments").fetchone()["c"]
        payments_paid = conn.execute(
            "SELECT COUNT(*) AS c FROM payments WHERE status = 'paid'"
        ).fetchone()["c"]
        revenue_total_cents = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS c FROM payments WHERE status = 'paid'"
        ).fetchone()["c"]
        recent_payments = conn.execute(
            """
            SELECT p.id, p.special_reference, p.amount_cents, p.credits, p.status,
                   p.created_at, p.paid_at, p.paymob_order_id, p.paymob_txn_id,
                   u.username, u.email
            FROM payments p
            LEFT JOIN users u ON u.id = p.user_id
            ORDER BY p.id DESC
            LIMIT 50
            """
        ).fetchall()
        conn.close()

    revenue_egp = round((revenue_total_cents or 0) / 100, 2)
    conversion_rate = round((payments_paid / payments_total * 100), 1) if payments_total else 0

    return {
        "books_today": int(books_today),
        "books_yesterday": int(books_yesterday),
        "books_week": int(books_week),
        "books_month": int(books_month),
        "books_total": int(books_total),
        "pages_total": int(pages_total or 0),
        "avg_pages": avg_pages,
        "users_total": int(users_total),
        "users_week": int(users_week),
        "users_today": int(users_today),
        "sessions_active": sessions_count,
        "shares_active": shares_count,
        "free_books_per_month": FREE_BOOKS_PER_MONTH,
        "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "daily": daily,
        "max_daily": max_daily,
        "daily_revenue": daily_revenue,
        "max_daily_revenue": max_daily_revenue,
        "daily_users": daily_users,
        "max_daily_users": max_daily_users,
        "top_scenes": [
            {"scene_id": r["scene_id"], "title": scene_title(r["scene_id"]), "count": r["c"]}
            for r in top
        ],
        "recent_books": [dict(r) for r in recent_books],
        "recent_users": [dict(r) for r in recent_users],
        "payments_total": int(payments_total),
        "payments_paid": int(payments_paid),
        "revenue_egp": revenue_egp,
        "conversion_rate": conversion_rate,
        "recent_payments": [dict(r) for r in recent_payments],
    }


def user_quota(user: Optional[dict]) -> dict:
    if not user:
        return {
            "book_credits": 0,
            "free_limit": FREE_BOOKS_PER_MONTH,
            "free_used": 0,
            "free_left": FREE_BOOKS_PER_MONTH,
        }
    credits = get_user_credits(user["id"])
    free_used = monthly_book_count_for_user(user["id"])
    free_left = max(0, FREE_BOOKS_PER_MONTH - free_used)
    return {
        "book_credits": credits,
        "free_limit": FREE_BOOKS_PER_MONTH,
        "free_used": free_used,
        "free_left": free_left,
    }


def user_public(user: dict) -> dict:
    q = user_quota(user)
    return {
        "id": user["id"],
        "email": user["email"],
        "username": user["username"],
        "book_credits": q["book_credits"],
        "auth_provider": user.get("auth_provider") or "email",
        "free_limit": q["free_limit"],
        "free_used": q["free_used"],
        "free_left": q["free_left"],
    }


def _slug_username(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\u0600-\u06FF]", "", (raw or "").strip())
    if len(cleaned) < 3:
        cleaned = "user" + secrets.token_hex(3)
    return cleaned[:30]


def _next_username(conn: sqlite3.Connection, preferred: str) -> str:
    base = _slug_username(preferred)
    candidate = base
    n = 0
    while True:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ?", (candidate,)
        ).fetchone()
        if not row:
            return candidate
        n += 1
        suffix = str(n)
        candidate = (base[: max(1, 30 - len(suffix))] + suffix)[:30]


def upsert_google_user(google_id: str, email: str, name: str) -> int:
    email = (email or "").strip().lower()
    google_id = (google_id or "").strip()
    if not google_id or not email or not EMAIL_RE.match(email):
        raise ValueError("invalid google profile")

    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        conn = db_connect()
        by_google = conn.execute(
            "SELECT id FROM users WHERE google_id = ?", (google_id,)
        ).fetchone()
        if by_google:
            user_id = int(by_google["id"])
            conn.execute(
                "UPDATE users SET auth_provider = CASE "
                "WHEN auth_provider IS NULL OR auth_provider = 'email' THEN 'email+google' "
                "ELSE auth_provider END "
                "WHERE id = ?",
                (user_id,),
            )
            conn.commit()
            conn.close()
            return user_id

        by_email = conn.execute(
            "SELECT id, auth_provider FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if by_email:
            user_id = int(by_email["id"])
            provider = by_email["auth_provider"] or "email"
            if provider == "email":
                provider = "email+google"
            elif "google" not in provider:
                provider = f"{provider}+google"
            conn.execute(
                "UPDATE users SET google_id = ?, auth_provider = ? WHERE id = ?",
                (google_id, provider, user_id),
            )
            conn.commit()
            conn.close()
            return user_id

        preferred = name or email.split("@")[0]
        last_err: Optional[Exception] = None
        for _ in range(8):
            username = _next_username(conn, preferred)
            try:
                cur = conn.execute(
                    "INSERT INTO users (email, username, password_hash, created_at, "
                    "book_credits, google_id, auth_provider) VALUES (?, ?, '', ?, 0, ?, 'google')",
                    (email, username, now, google_id),
                )
                user_id = int(cur.lastrowid)
                conn.commit()
                conn.close()
                return user_id
            except sqlite3.IntegrityError as e:
                last_err = e
                preferred = (email.split("@")[0] or "user") + secrets.token_hex(2)
                continue
        conn.close()
        raise RuntimeError(f"could not create google user: {last_err}")


def get_user_credits(user_id: int) -> int:
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT book_credits FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        conn.close()
    return int((row["book_credits"] if row else 0) or 0)


def add_user_credits(user_id: int, credits: int):
    with _db_lock:
        conn = db_connect()
        conn.execute(
            "UPDATE users SET book_credits = COALESCE(book_credits, 0) + ? WHERE id = ?",
            (credits, user_id),
        )
        conn.commit()
        conn.close()


def consume_user_credit(user_id: int) -> bool:
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT book_credits FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        credits = int((row["book_credits"] if row else 0) or 0)
        if credits <= 0:
            conn.close()
            return False
        conn.execute(
            "UPDATE users SET book_credits = book_credits - 1 WHERE id = ? AND book_credits > 0",
            (user_id,),
        )
        conn.commit()
        conn.close()
    return True


def monthly_book_count_for_user(user_id: int) -> int:
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE user_id = ? AND created_at LIKE ?",
            (user_id, f"{month_prefix}%"),
        ).fetchone()
        conn.close()
    return int(row["c"] if row else 0)


def collect_user_dashboard(user: dict) -> dict:
    user_id = user["id"]
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    with _db_lock:
        conn = db_connect()
        books = conn.execute(
            """
            SELECT id, created_at, pages, session_id
            FROM books
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 50
            """,
            (user_id,),
        ).fetchall()
        books_month = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE user_id = ? AND created_at LIKE ?",
            (user_id, f"{month_prefix}%"),
        ).fetchone()["c"]
        books_total = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE user_id = ?",
            (user_id,),
        ).fetchone()["c"]
        pages_total = conn.execute(
            "SELECT COALESCE(SUM(pages), 0) AS c FROM books WHERE user_id = ?",
            (user_id,),
        ).fetchone()["c"]
        payments = conn.execute(
            """
            SELECT special_reference, amount_cents, credits, status, created_at, paid_at
            FROM payments
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (user_id,),
        ).fetchall()
        conn.close()

    book_rows = []
    for b in books:
        sid = b["session_id"] or ""
        d = SESSIONS_DIR / sid if sid else None
        available = bool(d and d.exists() and any(d.glob("page_*.jpg")))
        page_files = sorted(d.glob("page_*.jpg")) if available else []
        thumbs = []
        for p in page_files[:4]:
            try:
                thumbs.append(base64.b64encode(p.read_bytes()).decode("ascii"))
            except OSError:
                pass
        book_rows.append({
            "id": b["id"],
            "created_at": b["created_at"],
            "pages": b["pages"],
            "session_id": sid,
            "available": available,
            "thumbs": thumbs,
        })

    credits = get_user_credits(user_id)
    free_used = int(books_month)
    free_left = max(0, FREE_BOOKS_PER_MONTH - free_used)

    return {
        "user": user,
        "credits": credits,
        "books_total": int(books_total),
        "books_month": free_used,
        "pages_total": int(pages_total or 0),
        "free_limit": FREE_BOOKS_PER_MONTH,
        "free_left": free_left,
        "books": book_rows,
        "payments": [dict(p) for p in payments],
        "pack_price": BOOK_PACK_PRICE_EGP,
        "pack_credits": BOOK_PACK_CREDITS,
        "paymob_ready": paymob_configured(),
        "wallet_ready": wallet_enabled(),
    }


def login_required_page(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("tool_app", login="1"))
        return view(*args, **kwargs)
    return wrapped


def mark_payment_paid(
    special_reference: str,
    *,
    paymob_order_id: Optional[str] = None,
    paymob_txn_id: Optional[str] = None,
) -> bool:
    """Idempotently mark payment paid and grant credits. Returns True if newly paid."""
    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT * FROM payments WHERE special_reference = ?",
            (special_reference,),
        ).fetchone()
        if not row:
            conn.close()
            return False
        if row["status"] == "paid":
            conn.close()
            return False
        conn.execute(
            """
            UPDATE payments
            SET status = 'paid', paid_at = ?, paymob_order_id = COALESCE(?, paymob_order_id),
                paymob_txn_id = COALESCE(?, paymob_txn_id)
            WHERE special_reference = ? AND status != 'paid'
            """,
            (now, paymob_order_id, paymob_txn_id, special_reference),
        )
        if row["user_id"]:
            conn.execute(
                "UPDATE users SET book_credits = COALESCE(book_credits, 0) + ? WHERE id = ?",
                (int(row["credits"]), row["user_id"]),
            )
        conn.commit()
        conn.close()
    return True


def check_freemium_or_error():
    user = current_user()
    if user and get_user_credits(user["id"]) > 0:
        return None

    if user:
        used = monthly_book_count_for_user(user["id"])
    else:
        used = monthly_book_count(get_remote_address())

    if used >= FREE_BOOKS_PER_MONTH:
        return jsonify({
            "error": f"خلّصت الكتب المجانية لهذا الشهر ({FREE_BOOKS_PER_MONTH} كتب). ادفع عشان تكمل.",
            "error_en": f"Free monthly quota reached ({FREE_BOOKS_PER_MONTH} books). Please pay to continue.",
            "freemium": True,
            "payment_required": True,
            "paymob_ready": paymob_configured(),
            "pack": {
                "price_egp": BOOK_PACK_PRICE_EGP,
                "credits": BOOK_PACK_CREDITS,
            },
        }), 402
    return None


def track_book(session_id: str, pages: int, scene_ids: List[str]) -> dict:
    ip = get_remote_address()
    now = datetime.now(timezone.utc).isoformat()
    user = current_user()
    user_id = user["id"] if user else None
    consumed = None
    # Prefer consuming a paid credit when free monthly quota is already used
    if user_id is not None:
        free_used = monthly_book_count_for_user(user_id)
        if free_used >= FREE_BOOKS_PER_MONTH:
            if consume_user_credit(user_id):
                consumed = "credit"
            else:
                consumed = "credit_failed"
        else:
            consumed = "free"
    with _db_lock:
        conn = db_connect()
        conn.execute(
            "INSERT INTO books (created_at, ip, session_id, pages, user_id) VALUES (?, ?, ?, ?, ?)",
            (now, ip, session_id, pages, user_id),
        )
        for sid in scene_ids:
            conn.execute(
                "INSERT INTO scene_picks (created_at, scene_id) VALUES (?, ?)",
                (now, sid),
            )
        conn.commit()
        conn.close()
    # Refresh user after possible credit consume
    user = current_user()
    return {
        "consumed": consumed,
        "pages": pages,
        "quota": user_quota(user),
    }


def monthly_book_count(ip: str) -> int:
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE ip = ? AND created_at LIKE ?",
            (ip, f"{month_prefix}%"),
        ).fetchone()
        conn.close()
    return int(row["c"] if row else 0)


def story_page_text(beat: dict, child_name: str = "") -> str:
    """The Arabic narration for one story page, with {name} filled in."""
    name = (child_name or "").strip()[:40] or "بطلنا"
    return (beat.get("text") or "").replace("{name}", name)


# Prefer faces that actually contain Arabic glyphs for story captions.
STORY_FONT_CANDIDATES = [
    "/System/Library/Fonts/SFArabic.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/hosny-amiri/Amiri-Regular.ttf",
    "/usr/share/fonts/truetype/amiri/Amiri-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def load_story_font(size: int) -> Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]:
    for path in STORY_FONT_CANDIDATES + list(FONT_CANDIDATES):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return max(0, bbox[2] - bbox[0])
    except Exception:
        return max(1, len(text) * max(8, getattr(font, "size", 16) // 2))


def wrap_story_lines(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list:
    """Word-wrap Arabic (or mixed) caption to fit max_width using shaped measurement."""
    text = (text or "").strip()
    if not text:
        return []
    words = text.split()
    if not words:
        return [text]
    lines: list = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip() if cur else w
        shaped = arabic_text(trial)
        if _text_width(draw, shaped, font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    # Soft-split any leftover line that's still too long (no spaces)
    fixed: list = []
    for line in lines:
        shaped = arabic_text(line)
        if _text_width(draw, shaped, font) <= max_width:
            fixed.append(line)
            continue
        chunk = ""
        for ch in line:
            trial = chunk + ch
            if _text_width(draw, arabic_text(trial), font) <= max_width or not chunk:
                chunk = trial
            else:
                fixed.append(chunk)
                chunk = ch
        if chunk:
            fixed.append(chunk)
    return fixed or [text]


def overlay_story_caption(
    img: Image.Image,
    caption: str,
    *,
    page_no: int = 0,
    total_pages: int = 0,
) -> Image.Image:
    """Paint Arabic narration on the bottom band of a story page (server-side)."""
    caption = (caption or "").strip()
    if not caption:
        return img.convert("RGB")

    base = img.convert("RGBA")
    w, h = base.size
    band_h = max(int(h * 0.20), 220)
    band_top = h - band_h

    # Soft white band so dark art stays readable under Arabic text
    band = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    # Gradient-ish solid + top feather
    for i in range(band_h):
        t = i / max(band_h - 1, 1)
        alpha = int(40 + 175 * min(1.0, t * 1.4))
        y = band_top + i
        bd.line([(0, y), (w, y)], fill=(255, 252, 245, alpha))
    # Clean plate over lower 85% of band
    plate_top = band_top + int(band_h * 0.18)
    plate_box = [int(w * 0.05), plate_top, int(w * 0.95), h - int(h * 0.035)]
    try:
        bd.rounded_rectangle(
            plate_box,
            radius=28,
            fill=(255, 255, 255, 230),
            outline=(226, 232, 240, 255),
            width=2,
        )
    except Exception:
        bd.rectangle(plate_box, fill=(255, 255, 255, 230), outline=(226, 232, 240, 255), width=2)

    composed = Image.alpha_composite(base, band).convert("RGB")
    draw = ImageDraw.Draw(composed)

    # Font size scales with page width
    font_size = max(30, min(48, int(w * 0.036)))
    font = load_story_font(font_size)
    font_meta = load_story_font(max(22, font_size - 10))
    max_w = int(w * 0.80)
    lines = wrap_story_lines(draw, caption, font, max_w)
    # Cap lines so band never overflows
    if len(lines) > 4:
        lines = lines[:3] + [lines[3] + "…"]

    line_gap = int(font_size * 1.35)
    block_h = line_gap * len(lines)
    y0 = plate_top + max(18, (h - plate_top - int(h * 0.04) - block_h) // 2)

    for i, line in enumerate(lines):
        shaped = arabic_text(line)
        tw = _text_width(draw, shaped, font)
        x = (w - tw) // 2
        y = y0 + i * line_gap
        # Soft shadow for readability
        draw.text((x + 2, y + 2), shaped, font=font, fill=(148, 163, 184))
        draw.text((x, y), shaped, font=font, fill=(30, 41, 59))

    if page_no and total_pages:
        meta = arabic_text(f"{page_no} / {total_pages}")
        mw = _text_width(draw, meta, font_meta)
        draw.text((w - mw - int(w * 0.08), h - int(h * 0.045)), meta, font=font_meta, fill=(100, 116, 139))

    return composed


def apply_story_caption_to_page(
    path: Path,
    scene: dict,
    child_name: str = "",
    pack: Optional[dict] = None,
) -> None:
    """Load JPEG, draw Arabic story line, save back atomically."""
    if not path or not Path(path).exists():
        return
    caption = story_page_text(scene, child_name)
    if not caption:
        return
    pack = pack or pack_for_scene(scene.get("id") or "")
    beats = (pack or {}).get("scenes") or []
    sid = scene.get("id")
    idx = next((i for i, b in enumerate(beats) if b.get("id") == sid), -1)
    page_no = idx + 1 if idx >= 0 else 0
    total = len(beats)

    try:
        img = Image.open(path).convert("RGB")
    except Exception:
        return
    out = overlay_story_caption(img, caption, page_no=page_no, total_pages=total)
    tmp = path.with_name(f".{path.stem}.cap.jpg")
    try:
        out.save(tmp, "JPEG", quality=92)
        tmp.replace(path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        except OSError:
            pass


def build_story_prompt(
    beat: dict,
    pack: dict,
    child_name: str = "",
    page_no: int = 0,
    total_pages: int = 0,
) -> str:
    """One full-color story page.

    Three rules matter more than the wording:
      1. `art_key` is repeated verbatim on every page so the whole book matches.
      2. The page is fully painted "colored paper" — colour reaches all four edges.
         A white page would read as an unfinished coloring sheet, not a storybook.
      3. The image must contain NO text — Arabic narration is drawn on top by the
         server (arabic_text + load_font), because image models garble Arabic.
    """
    name = (child_name or "").strip()[:40]
    who = f'The hero of the story is a child named "{name}". ' if name else ""
    where = (
        f"This is page {page_no} of {total_pages} of one continuous story. "
        if page_no and total_pages else ""
    )
    colors = pack.get("colors") or ", ".join(pack.get("palette") or []) or "the book's own warm palette"
    return (
        "Children's storybook illustration, FULL COLOR painted page, premium picture-book "
        "quality, Disney/Pixar-grade warmth and appeal. "
        "Vertical A4 portrait, full-bleed edge-to-edge, print-ready 300 DPI. "
        f"ART STYLE (identical on every page of this book): {pack['art_key']}. "
        f"DOMINANT COLORS (same across the whole book): {colors}. "
        f"{who}{where}"
        "This is image-to-image: the child must be the exact same child as the reference photo — "
        "same face, same hairstyle, same age, same skin tone, same identity, clearly recognizable, "
        "and drawn consistently the same across every page. "
        f"SCENE: the child is {beat['scene']}. "
        # Colored paper — the single most important difference from a coloring page.
        "THE WHOLE PAGE IS PAINTED: rich saturated color covers the entire sheet from edge to "
        "edge, including all four corners and the background behind the child. This is a printed "
        "colored storybook page, NOT a white page with a drawing on it. "
        "NEVER leave the background white, blank, empty or unpainted. No white paper anywhere, "
        "no white frame, no white gutter, no plain white sky, no line art, no black-and-white areas. "
        "Composition: keep the child clearly visible in the upper two thirds. "
        "The bottom fifth must stay a calm PAINTED band — softly colored sky, ground, water or a "
        "smooth gradient in the book's palette, with no important detail — so a caption can be "
        "placed over it later. That band is colored, never white. "
        "ABSOLUTELY NO text, no letters, no words, no numbers, no captions, no speech bubbles, "
        "no watermark, no signature anywhere in the image. "
        "Expressive storytelling, readable silhouette, soft cinematic lighting, rich color depth, "
        "no letterboxing, no white borders, no empty margins."
    )


def build_story_cover_prompt(pack: dict, child_name: str = "") -> str:
    """Story front cover — fully painted, and the one page with baked-in Arabic title."""
    name = (child_name or "").strip()[:40] or "طفلي"
    colors = pack.get("colors") or ", ".join(pack.get("palette") or []) or "the book's own warm palette"
    return (
        "Premium children's storybook front cover, FULL COLOR painted page, picture-book quality. "
        "Vertical A4 portrait full-bleed, print-ready 300 DPI. "
        f"ART STYLE: {pack['art_key']}. "
        f"DOMINANT COLORS: {colors}. "
        "The child must be the exact same child as the reference photo — same face, hairstyle, "
        f'age, skin tone and identity. The child is named "{name}". '
        f"Cover illustration: the child {pack['cover_scene']}. "
        "Clear readable playful Arabic typography, well spaced, correctly shaped and connected: "
        f'Large title at the top: "{pack["book_title"]}". '
        f'Below the title, smaller: "بطلها {name}". '
        f'Bottom banner: "{pack["tagline"]}". '
        'Small round "لوني" logo badge in a bottom corner. '
        "THE WHOLE COVER IS PAINTED: rich color covers the entire sheet edge to edge including "
        "all four corners and the area behind the title. Never a white or blank background — "
        "the Arabic text sits on painted artwork or a colored banner, not on white paper. "
        "Leave generous calm space around the text so it stays readable. "
        "No watermark, no extra text beyond what is listed. "
        "Full-page edge-to-edge, no white borders, no letterboxing."
    )


def build_story_ending_prompt(pack: dict, child_name: str = "") -> str:
    """Story last page — the moral, in the same visual identity as the cover."""
    name = (child_name or "").strip()[:40]
    who = f'The child is named "{name}". ' if name else ""
    colors = pack.get("colors") or ", ".join(pack.get("palette") or []) or "the book's own warm palette"
    return (
        "Final page of a premium children's storybook, FULL COLOR painted page, picture-book quality. "
        "Vertical A4 portrait full-bleed, print-ready 300 DPI. "
        f"ART STYLE (same as the whole book): {pack['art_key']}. "
        f"DOMINANT COLORS: {colors}. "
        f"{who}"
        "The child must be the exact same child as the reference photo — same face, hairstyle, "
        "age and identity. "
        f"Illustration: the child {pack['ending_scene']}. "
        "Clear readable playful Arabic typography with generous calm space: "
        f'Top: "{pack["ending_line"]}". '
        f'Center-bottom on a soft banner: "{pack["moral"]}". '
        'Small round "لوني" logo badge at the bottom. '
        "THE WHOLE PAGE IS PAINTED: rich color edge to edge including all four corners and the "
        "area behind the text. Never a white or blank background — no white paper anywhere. "
        "Warm closing mood. No watermark, no extra text. "
        "Full-page edge-to-edge, no white borders, no letterboxing."
    )


def build_prompt(
    scene_text: str,
    variant: str = DEFAULT_VARIANT,
    line_weight: str = "normal",
    detail: str = "normal",
    art_style: str = "cartoon",
    pack_id: Optional[str] = None,
) -> str:
    style = PROMPT_VARIANTS.get(variant, PROMPT_VARIANTS[DEFAULT_VARIANT])
    extras = ", ".join([
        LINE_WEIGHT.get(line_weight, LINE_WEIGHT["normal"]),
        DETAIL_LEVEL.get(detail, DETAIL_LEVEL["normal"]),
        ART_STYLE.get(art_style, ART_STYLE["cartoon"]),
    ])
    # Pack mood keeps every page of one book looking like the same book.
    # Story packs use art_key instead of mood — never require "mood".
    mood = pack_by_id(pack_id).get("mood") or "" if pack_id else ""
    tail = f", {mood}" if mood else ""
    return f"{style}, {extras}, the child is {scene_text}{tail}"


def arabic_text(text: str) -> str:
    if not text:
        return text
    return get_display(arabic_reshaper.reshape(text))


def load_font(size: int) -> Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def make_cover_page(child_name: str) -> Image.Image:
    w, h = PAGE_WIDTH, PAGE_HEIGHT
    img = Image.new("RGB", (w, h), "#ffffff")
    draw = ImageDraw.Draw(img)

    for inset, width, color in (
        (48, 6, "#7c3aed"),
        (68, 2, "#ec4899"),
        (88, 1, "#c4b5fd"),
    ):
        draw.rectangle([inset, inset, w - inset, h - inset], outline=color, width=width)

    ornament = 28
    for x, y in ((120, 120), (w - 120, 120), (120, h - 120), (w - 120, h - 120)):
        draw.ellipse([x - ornament, y - ornament, x + ornament, y + ornament], outline="#f59e0b", width=3)

    title_font = load_font(72)
    name_font = load_font(56)
    date_font = load_font(32)
    subtitle_font = load_font(28)

    title = arabic_text("كتاب تلوين")
    subtitle = arabic_text("مولّد خصيصًا لطفلك")
    name = arabic_text(child_name.strip() or "طفلي")
    today = arabic_text(date.today().strftime("%Y/%m/%d"))

    def center_text(text, font, y, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        draw.text(((w - tw) / 2, y), text, font=font, fill=fill)

    cy = h // 2
    center_text(title, title_font, cy - 160, "#7c3aed")
    center_text(subtitle, subtitle_font, cy - 60, "#7a7480")
    center_text(name, name_font, cy + 40, "#1f1b24")
    center_text(today, date_font, cy + 140, "#7a7480")
    return img


ENDING_SCENE_ID = "ending"
COVER_SCENE_ID = "cover"


def build_cover_prompt(
    child_name: str = "", page_count: int = 0, pack_id: Optional[str] = None
) -> str:
    """Premium full-color personalized front cover (not line-art), themed per book type."""
    pack = pack_by_id(pack_id)
    name = (child_name or "").strip()[:40] or "طفلي"
    pages = int(page_count) if page_count and int(page_count) > 0 else 0
    pages_label = f"{pages} صفحة تلوين" if pages else "صفحات تلوين"
    return (
        "Create a premium children's personalized coloring book cover in a cute modern storybook style. "
        "Vertical A4 portrait full-bleed composition (2480×3508 style), print-ready, 300 DPI. "
        f"The child in the illustration must be the exact same child from the reference photo — "
        f'same face, hairstyle, age, skin tone and identity. The child is named "{name}". '
        "Design style: Warm cream paper texture background. Soft golden gradient from top to bottom. "
        "Colorful torn paper borders around the edges (purple top-left, pink top-right, cyan bottom-left, pink bottom-right). "
        "Hand-drawn doodles: hearts, stars, sparkles, circles, playful brush strokes. "
        "Cute premium children's brand identity. Spacious clean layout. High-end children's book design. "
        "Soft lighting. Vibrant but elegant colors. "
        "Typography layout with clear readable playful Arabic text: "
        f'Top: "{pack["book_title"]}". '
        f'Below: "خاص بـ {name}". '
        'Below: "كتاب تلوين للأطفال". '
        f'Below: "{pages_label}". '
        'Right side: Round badge/logo area for "لوني". '
        "Center: Large brush-paint frame containing the personalized child illustration. "
        "The child is smiling, facing forward, "
        f'{pack["cover_scene"]}. '
        f'Bottom: Pink brush stroke banner with Arabic text "{pack["tagline"]}". '
        "Playful Arabic typography. Highly detailed. Premium children's book cover. "
        "Disney/Pixar quality. Ultra clean. Full-page edge-to-edge, no white borders, no letterboxing. "
        "No watermark. No extra text. Full color illustration (not black-and-white line art)."
    )


def build_ending_prompt(child_name: str = "", pack_id: Optional[str] = None) -> str:
    """Full-color premium final page (not line-art coloring), themed per book type."""
    pack = pack_by_id(pack_id)
    name = (child_name or "").strip()[:40]
    name_bit = (
        f'The child is named "{name}". Show the exact same child from the reference photo — '
        f"same face, hairstyle, age, skin tone and identity. "
        if name
        else "Show the exact same child from the reference photo — same face, hairstyle, age, and identity. "
    )
    return (
        "Create the final page of a premium children's personalized coloring book. "
        "Vertical A4 portrait full-bleed composition (2480×3508 style), print-ready, 300 DPI. "
        f"{name_bit}"
        "Use EXACTLY the same visual identity as a premium front cover: "
        "same cream paper texture, same warm golden background, same colorful torn paper borders, "
        "same playful doodles (hearts, stars, sparkles, circles), same premium children's illustration style, "
        "same color palette, same lighting, same children's brand identity. "
        "Top center Arabic text clearly readable: "
        '"تهانينا!" and below it "لقد انتهيت من". '
        f'Large title: "{pack["ending_line"]}". '
        "Below on a purple paint stroke the Arabic text: "
        '"كل رسمة لونتها... تصبح ذكرى جميلة تبقى معك ♡". '
        f'Center illustration: the same child {pack["ending_scene"]}. '
        "Large rainbow behind the child. "
        "Paint brushes, crayons, paint jars, palette, art supplies around the scene. "
        "Bottom Arabic text clearly readable: "
        '"استمر في الإبداع... فكل صفحة جديدة تنتظرك لتلونها! 💖". '
        'Bottom center: round "لوني" logo. '
        "Keep the layout clean and spacious. Cute premium children's illustration. "
        "Storybook quality. Disney/Pixar style. High-end printing quality. Ultra detailed. "
        "Full-page edge-to-edge, no white borders, no letterboxing. "
        "No watermark. No extra unrelated objects. Full color illustration (not black-and-white line art)."
    )


def ending_page_path(d: Path) -> Path:
    return d / f"page_{ENDING_SCENE_ID}.jpg"


def cover_page_path(d: Path) -> Path:
    return d / f"page_{COVER_SCENE_ID}.jpg"


def build_mock_image_bytes(
    title: str,
    *,
    kind: str = "page",
    child_name: str = "",
    ref_path: Optional[Path] = None,
    subtitle: str = "",
) -> bytes:
    """Cheap free local JPEG for admin pipeline testing — never calls Kie."""
    w, h = PAGE_WIDTH, PAGE_HEIGHT
    kind = (kind or "page").lower()
    if kind == "cover":
        bg = (255, 236, 210)
        accent = (180, 83, 9)
        banner = "MOCK COVER · تجريبي"
    elif kind == "ending":
        bg = (220, 252, 231)
        accent = (22, 163, 74)
        banner = "MOCK ENDING · تجريبي"
    elif kind == "story":
        bg = (237, 233, 254)
        accent = (109, 40, 217)
        banner = "MOCK STORY · بدون Kie"
    else:
        bg = (255, 255, 255)
        accent = (56, 189, 248)
        banner = "MOCK · اختبار بلا تكلفة"

    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    # Frame
    for inset, width in ((36, 8), (56, 3)):
        draw.rectangle([inset, inset, w - inset, h - inset], outline=accent, width=width)

    # Top banner
    draw.rectangle([0, 0, w, 72], fill=accent)
    font_lg = load_font(42)
    font_md = load_font(36)
    font_sm = load_font(28)

    def _center(text: str, font, y: int, fill):
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = len(text) * 14
        draw.text(((w - tw) / 2, y), text, font=font, fill=fill)

    _center(banner, font_sm, 18, "#ffffff")

    # Optional child photo (proves ref path still used)
    if ref_path and Path(ref_path).exists():
        try:
            face = Image.open(ref_path).convert("RGB")
            face.thumbnail((360, 360), Image.Resampling.LANCZOS)
            fx = (w - face.width) // 2
            fy = 180
            plate = [fx - 16, fy - 16, fx + face.width + 16, fy + face.height + 16]
            try:
                draw.rounded_rectangle(plate, radius=20, fill="#ffffff", outline=accent, width=3)
            except Exception:
                draw.rectangle(plate, fill="#ffffff", outline=accent, width=3)
            img.paste(face, (fx, fy))
        except Exception:
            pass

    name = (child_name or "").strip() or "طفل"
    ttl = (title or "صفحة").strip()[:48]
    mid_y = h // 2 + 80
    try:
        label = arabic_text(f"MOCK · {ttl}")
        name_l = arabic_text(name)
    except Exception:
        label = f"MOCK · {ttl}"
        name_l = name
    _center(label, font_lg, mid_y, accent)
    _center(name_l, font_md, mid_y + 70, "#334155")
    if subtitle:
        try:
            sub_l = arabic_text(subtitle[:60])
        except Exception:
            sub_l = subtitle[:60]
        _center(sub_l, font_sm, mid_y + 130, "#64748b")
    _center("$0 · no Kie call", font_sm, h - 120, "#94a3b8")

    # Coloring pages: light dashed doodle so they look different from photo covers
    if kind == "page":
        for i in range(6):
            y = 900 + i * 70
            draw.line([(120, y), (w - 120, y)], fill="#cbd5e1", width=2)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


async def generate_cover_page_async(
    d: Path,
    client: Optional[httpx.AsyncClient],
    *,
    child_name: str = "",
    page_count: int = 0,
    force: bool = False,
    use_kie: bool = False,
    ref_url: Optional[str] = None,
    pack_id: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Generate the premium full-color front cover, themed for the chosen book type."""
    out = cover_page_path(d)
    created = False
    prov = normalize_provider(provider) if (use_kie or is_mock_provider(provider)) else "cloudflare"
    model_id = model or (model_for_provider(prov) if (use_kie or is_mock_provider(provider)) else None)
    if force or not out.exists():
        refs = ensure_multi_refs(d)
        if not refs:
            raise RuntimeError("مفيش صورة مرجع لغلاف البداية.")
        _pack = pack_by_id(pack_id)
        if is_mock_provider(provider):
            await asyncio.sleep(0.45)
            title = (_pack or {}).get("book_title") or (_pack or {}).get("title") or "غلاف"
            img_bytes = build_mock_image_bytes(
                title,
                kind="cover",
                child_name=child_name,
                ref_path=refs[0],
                subtitle="Cover · free mock",
            )
        else:
            prompt = (
                build_story_cover_prompt(_pack, child_name)
                if is_story_pack(_pack)
                else build_cover_prompt(child_name, page_count=page_count, pack_id=pack_id)
            )
            if use_kie:
                if not kie_configured():
                    raise RuntimeError("KIE_API_KEY مش مضبوط لغلاف البداية.")
                if client is None:
                    raise RuntimeError("HTTP client مطلوب لتوليد Kie.")
                img_bytes, _ = await generate_image_to_image(
                    prompt, refs[0], client, input_url=ref_url, model=model_id
                )
            else:
                if client is None:
                    raise RuntimeError("HTTP client مطلوب.")
                img_bytes = await call_model_async(prompt, refs, client)
        _atomic_jpeg_bytes(out, img_bytes, quality=93)
        created = True
    b64 = base64.b64encode(out.read_bytes()).decode()
    return {
        "scene_id": COVER_SCENE_ID,
        "title": "الغلاف",
        "title_en": "Cover",
        "emoji": "📗",
        "image_b64": b64,
        "created": created,
        "provider": prov if (use_kie or is_mock_provider(provider)) else "cloudflare",
        "model": model_id,
    }


async def generate_ending_page_async(
    d: Path,
    client: Optional[httpx.AsyncClient],
    *,
    child_name: str = "",
    force: bool = False,
    use_kie: bool = False,
    ref_url: Optional[str] = None,
    pack_id: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Generate the premium full-color ending page, themed for the chosen book type."""
    out = ending_page_path(d)
    created = False
    prov = normalize_provider(provider) if (use_kie or is_mock_provider(provider)) else "cloudflare"
    model_id = model or (model_for_provider(prov) if (use_kie or is_mock_provider(provider)) else None)
    if force or not out.exists():
        refs = ensure_multi_refs(d)
        if not refs:
            raise RuntimeError("مفيش صورة مرجع لصفحة النهاية.")
        _pack = pack_by_id(pack_id)
        if is_mock_provider(provider):
            await asyncio.sleep(0.45)
            title = (_pack or {}).get("ending_line") or "النهاية"
            img_bytes = build_mock_image_bytes(
                title,
                kind="ending",
                child_name=child_name,
                ref_path=refs[0],
                subtitle="Ending · free mock",
            )
        else:
            prompt = (
                build_story_ending_prompt(_pack, child_name)
                if is_story_pack(_pack)
                else build_ending_prompt(child_name, pack_id=pack_id)
            )
            if use_kie:
                if not kie_configured():
                    raise RuntimeError("KIE_API_KEY مش مضبوط لصفحة النهاية.")
                if client is None:
                    raise RuntimeError("HTTP client مطلوب لتوليد Kie.")
                img_bytes, _ = await generate_image_to_image(
                    prompt, refs[0], client, input_url=ref_url, model=model_id
                )
            else:
                if client is None:
                    raise RuntimeError("HTTP client مطلوب.")
                img_bytes = await call_model_async(prompt, refs, client)
        _atomic_jpeg_bytes(out, img_bytes, quality=93)
        created = True
    b64 = base64.b64encode(out.read_bytes()).decode()
    return {
        "scene_id": ENDING_SCENE_ID,
        "title": "صفحة النهاية",
        "title_en": "Ending",
        "emoji": "🎉",
        "image_b64": b64,
        "created": created,
        "provider": prov if (use_kie or is_mock_provider(provider)) else "cloudflare",
        "model": model_id,
    }


def load_ending_image(d: Path) -> Optional[Image.Image]:
    p = ending_page_path(d)
    if p.exists():
        return Image.open(p).convert("RGB")
    return None


def load_cover_image(d: Path) -> Optional[Image.Image]:
    p = cover_page_path(d)
    if p.exists():
        return Image.open(p).convert("RGB")
    return None


def assemble_book_images(
    d: Path,
    order: List[str],
    child_name: str,
    *,
    include_ending: bool = True,
    include_cover: bool = True,
) -> List[Image.Image]:
    """Cover + coloring pages (+ optional ending) for PDF. No placeholder covers."""
    pages: List[Image.Image] = []
    skip = {ENDING_SCENE_ID, COVER_SCENE_ID}
    for sid in order:
        sid = (sid or "").strip()
        if not sid or sid in skip or not SCENE_ID_RE.match(sid):
            continue
        p = page_path(d, sid)
        if p.exists():
            pages.append(Image.open(p).convert("RGB"))
    all_imgs: List[Image.Image] = []
    if include_cover:
        cover = load_cover_image(d)
        if cover is None:
            return []  # cover required but missing — caller should validate
        all_imgs.append(cover)
    all_imgs.extend(pages)
    if include_ending:
        ending = load_ending_image(d)
        if ending is not None:
            all_imgs.append(ending)
    return all_imgs


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return "انتهى وقت الانتظار. السيرفر متأخر — جرّب تاني."
    if isinstance(exc, httpx.ConnectError):
        return "مفيش اتصال بالإنترنت أو خدمة التوليد. تأكد من الشبكة وحاول مرة أخرى."
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 403):
            return "مفاتيح الخدمة غير صحيحة أو منتهية. راجع التوكن."
        if status == 429:
            return "خدمة التوليد مشغولة دلوقتي. استنى دقيقة وجرّب تاني."
        if status and status >= 500:
            return "خدمة التوليد فيها مشكلة مؤقتة. جرّب بعد شوية."
        return "فشل طلب التوليد. جرّب مرة أخرى."
    msg = str(exc)
    # Pass through already-localized Kie / app errors
    if msg.startswith("Kie.ai") or msg.startswith("مفتاح") or msg.startswith("رصيد"):
        return msg
    if "API error" in msg:
        return "الموديل رفض الطلب. جرّب صورة أوضح أو موقف تاني."
    # Surface short exception text for easier admin debugging (no stack traces).
    short = (msg or type(exc).__name__).strip()
    if short and len(short) < 160 and "\n" not in short:
        return f"حصل خطأ أثناء التوليد: {short}"
    return "حصل خطأ غير متوقع أثناء التوليد. جرّب مرة أخرى."


def session_dir(session_id: str) -> Path:
    if not session_id.isalnum() or len(session_id) > 40:
        abort(400, "Bad session id")
    d = SESSIONS_DIR / session_id
    if not d.exists():
        abort(404, "Session not found")
    return d


def ref_image_paths(d: Path) -> List[Path]:
    paths = []
    for i in range(4):
        p = d / f"input_{i}.png"
        if p.exists():
            paths.append(p)
    if not paths:
        legacy = d / "input.png"
        if legacy.exists():
            paths.append(legacy)
    return paths


def ensure_multi_refs(d: Path) -> List[Path]:
    paths = ref_image_paths(d)
    if len(paths) == 1:
        img = Image.open(paths[0]).convert("RGB")
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        cropped = img.crop((left, top, left + side, top + side)).resize(
            (min(side, 1024), min(side, 1024))
        )
        second = d / "input_1.png"
        cropped.save(second, "PNG")
        paths.append(second)
    return paths


def validate_portrait_image(img: Image.Image) -> Optional[str]:
    """Soft validation: reject tiny/blank-ish images (not a full face detector)."""
    w, h = img.size
    if w < 200 or h < 200:
        return "الصورة صغيرة جدًا. استخدم صورة أوضح للوجه (200×200 على الأقل)."
    # Reject near-solid images (very low variance)
    small = img.convert("L").resize((64, 64))
    hist = small.histogram()
    nonzero = sum(1 for v in hist if v > 0)
    if nonzero < 8:
        return "الصورة تبدو فارغة أو بلون واحد. ارفع صورة واضحة لوجه الطفل."
    return None


def scene_by_id(scene_id: str, d: Optional[Path] = None):
    for s in ALL_SCENES:
        if s["id"] == scene_id:
            return s
    if d and CUSTOM_ID_RE.match(scene_id):
        meta = d / f"{scene_id}.json"
        if meta.exists():
            return json.loads(meta.read_text(encoding="utf-8"))
    return None


def page_path(d: Path, scene_id: str) -> Path:
    return d / f"page_{scene_id}.jpg"


def style_from_request(data: Optional[dict] = None):
    data = data or {}
    args = request.args
    return {
        "variant": data.get("variant") or args.get("variant") or DEFAULT_VARIANT,
        "line_weight": data.get("line_weight") or args.get("line_weight") or "normal",
        "detail": data.get("detail") or args.get("detail") or "normal",
        "art_style": data.get("art_style") or args.get("art_style") or "cartoon",
    }


async def call_model_async(
    prompt: str,
    image_paths: List[Path],
    client: httpx.AsyncClient,
) -> bytes:
    url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/{MODEL}"
    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    last_exc: Optional[Exception] = None

    for attempt in range(2):
        try:
            files = []
            handles = []
            try:
                for i, path in enumerate(image_paths[:4]):
                    fh = open(path, "rb")
                    handles.append(fh)
                    files.append((f"input_image_{i}", (path.name, fh, "image/png")))
                data = {
                    "prompt": prompt,
                    "width": str(PAGE_WIDTH),
                    "height": str(PAGE_HEIGHT),
                }
                resp = await client.post(url, headers=headers, data=data, files=files)
            finally:
                for fh in handles:
                    fh.close()

            resp.raise_for_status()
            payload = resp.json()
            if not payload.get("success", False):
                raise RuntimeError(f"API error: {payload.get('errors')}")
            return base64.b64decode(payload["result"]["image"])
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            if attempt == 0:
                await asyncio.sleep(1.5)
                continue
            raise
    raise last_exc  # pragma: no cover


async def translate_ar_to_en(text: str, client: httpx.AsyncClient) -> str:
    url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/{TRANSLATE_MODEL}"
    headers = {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}
    resp = await client.post(
        url,
        headers=headers,
        json={"text": text, "source_lang": "ar", "target_lang": "en"},
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success", False):
        raise RuntimeError(f"API error: {payload.get('errors')}")
    result = payload.get("result") or {}
    translated = result.get("translated_text") or result.get("translatedText") or ""
    if not translated:
        raise RuntimeError("Empty translation")
    return translated.strip()


async def generate_one_async(
    d: Path,
    scene: dict,
    force: bool,
    style: dict,
    client: httpx.AsyncClient,
) -> dict:
    scene_id = scene["id"]
    out = page_path(d, scene_id)
    created = False
    if force and out.exists():
        out.unlink()
    if not out.exists():
        refs = ensure_multi_refs(d)
        prompt = build_prompt(
            scene["scene"],
            variant=style.get("variant", DEFAULT_VARIANT),
            line_weight=style.get("line_weight", "normal"),
            detail=style.get("detail", "normal"),
            art_style=style.get("art_style", "cartoon"),
            pack_id=pack_for_scene(scene_id)["id"],
        )
        img_bytes = await call_model_async(prompt, refs, client)
        Image.open(io.BytesIO(img_bytes)).convert("RGB").save(out, "JPEG", quality=90)
        created = True
    b64 = base64.b64encode(out.read_bytes()).decode()
    return {
        "scene_id": scene_id,
        "title": scene.get("title", scene_id),
        "title_en": scene.get("title_en", scene.get("title", scene_id)),
        "emoji": scene.get("emoji", "✨"),
        "image_b64": b64,
        "created": created,
    }


async def generate_one_kie_async(
    d: Path,
    scene: dict,
    force: bool,
    style: dict,
    client: Optional[httpx.AsyncClient],
    *,
    ref_url: Optional[str] = None,
    sem: Optional[asyncio.Semaphore] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Generate one coloring page via Kie.ai, or free local Mock placeholder."""
    scene_id = scene["id"]
    out = page_path(d, scene_id)
    created = False
    prov = normalize_provider(provider)
    model_id = model or model_for_provider(prov)
    # Never delete the old page before the new one succeeds
    if force or not out.exists():
        refs = ensure_multi_refs(d)
        if not refs:
            raise RuntimeError("مفيش صورة مرجع.")
        _pack = pack_for_scene(scene_id)

        async def _work():
            if is_mock_provider(prov):
                await asyncio.sleep(0.4)
                kind = "story" if is_story_pack(_pack) else "page"
                img_bytes = build_mock_image_bytes(
                    scene.get("title") or scene_id,
                    kind=kind,
                    child_name=style.get("child_name", ""),
                    ref_path=refs[0],
                    subtitle=f"id:{scene_id}",
                )
                _atomic_jpeg_bytes(out, img_bytes, quality=88)
                return
            # Story pages: full-colour narrative art (skip line-art coloring prompt).
            if is_story_pack(_pack):
                _beats = _pack["scenes"]
                _idx = next((i for i, b in enumerate(_beats) if b["id"] == scene_id), -1)
                prompt = build_story_prompt(
                    scene, _pack,
                    child_name=style.get("child_name", ""),
                    page_no=_idx + 1 if _idx >= 0 else 0,
                    total_pages=len(_beats),
                )
            else:
                prompt = build_prompt(
                    scene["scene"],
                    variant=style.get("variant", DEFAULT_VARIANT),
                    line_weight=style.get("line_weight", "normal"),
                    detail=style.get("detail", "normal"),
                    art_style=style.get("art_style", "cartoon"),
                    pack_id=_pack["id"],
                )
                prompt = (
                    f"{prompt}. "
                    "This is image-to-image: keep the exact same child face, hair, age and identity "
                    "from the reference photo, converted to simple black-and-white coloring book line art only. "
                    "Full-page vertical A4 portrait composition, edge-to-edge illustration filling the entire page, "
                    "no white borders, no letterboxing, no empty margins."
                )
            if client is None:
                raise RuntimeError("HTTP client مطلوب لتوليد Kie.")
            img_bytes, _ = await generate_image_to_image(
                prompt, refs[0], client, input_url=ref_url, model=model_id
            )
            _atomic_jpeg_bytes(out, img_bytes, quality=92)

        if sem is not None:
            async with sem:
                await _work()
        else:
            await _work()
        created = True
        # Story narration is drawn by us (models mangle Arabic glyphs).
        if is_story_pack(_pack):
            apply_story_caption_to_page(
                out, scene,
                child_name=style.get("child_name", ""),
                pack=_pack,
            )
    b64 = base64.b64encode(out.read_bytes()).decode()
    return {
        "scene_id": scene_id,
        "title": scene.get("title", scene_id),
        "title_en": scene.get("title_en", scene.get("title", scene_id)),
        "emoji": scene.get("emoji", "✨"),
        "image_b64": b64,
        "created": created,
        "provider": prov,
        "model": model_id,
        "caption": story_page_text(scene, style.get("child_name", "")),
    }


def run_async(coro):
    return asyncio.run(coro)


def fit_image_to_a4(img: Image.Image, dpi: int = 150) -> Image.Image:
    """Center-crop / scale image to exact A4 aspect at print resolution."""
    # A4 mm → pixels at dpi
    target_w = int(210 / 25.4 * dpi)
    target_h = int(297 / 25.4 * dpi)
    img = img.convert("RGB")
    iw, ih = img.size
    if iw < 1 or ih < 1:
        return Image.new("RGB", (target_w, target_h), "#ffffff")
    scale = max(target_w / iw, target_h / ih)
    nw = max(target_w, int(round(iw * scale)))
    nh = max(target_h, int(round(ih * scale)))
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - target_w) // 2
    top = (nh - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def write_pdf_with_margins(images: List[Image.Image], pdf_path: Path):
    """Build full-bleed A4 PDF — each image covers the entire page (no white margins)."""
    page_w, page_h = A4
    c = pdf_canvas.Canvas(str(pdf_path), pagesize=A4)
    for img in images:
        page_img = fit_image_to_a4(img)
        # Draw edge-to-edge: full A4 background
        c.drawImage(
            ImageReader(page_img),
            0,
            0,
            width=page_w,
            height=page_h,
            preserveAspectRatio=False,
            mask="auto",
        )
        c.showPage()
    c.save()


def cleanup_old_sessions(max_age_hours: int = 24):
    cutoff = time.time() - max_age_hours * 3600
    for folder in (SESSIONS_DIR, SHARES_DIR):
        if not folder.exists():
            continue
        for item in folder.iterdir():
            try:
                if item.stat().st_mtime < cutoff:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
            except OSError:
                pass
    # Expire share metadata
    for meta in SHARES_DIR.glob("*.json"):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            exp = datetime.fromisoformat(data.get("expires_at", "1970-01-01T00:00:00+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                pdf = SHARES_DIR / f"{meta.stem}.pdf"
                pdf.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
        except Exception:
            pass


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(cleanup_old_sessions, "interval", hours=1, id="cleanup")
    _scheduler.start()
    cleanup_old_sessions()


init_db()
start_scheduler()


@app.route("/dashboard")
@login_required_page
def user_dashboard():
    user = current_user()
    data = collect_user_dashboard(user)
    return render_template("dashboard.html", **data)


@app.route("/")
def index():
    return render_template("landing.html")


@app.route("/app")
def tool_app():
    return render_template(
        "app.html",
        scenes=SCENES,
        free_books=FREE_BOOKS_PER_MONTH,
        book_pack_price=BOOK_PACK_PRICE_EGP,
        book_pack_credits=BOOK_PACK_CREDITS,
        paymob_ready=paymob_configured(),
        wallet_ready=wallet_enabled(),
        google_ready=google_ready(),
    )


@app.route("/auth/register", methods=["POST"])
@limiter.limit("10 per hour")
def auth_register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not EMAIL_RE.match(email):
        return jsonify({"error": "الإيميل مش صالح."}), 400
    if not USERNAME_RE.match(username):
        return jsonify({"error": "اسم المستخدم لازم 3–30 حرف (حروف/أرقام/_)."}), 400
    if len(password) < 6:
        return jsonify({"error": "كلمة المرور لازم 6 حروف على الأقل."}), 400

    now = datetime.now(timezone.utc).isoformat()
    pw_hash = generate_password_hash(password)
    try:
        with _db_lock:
            conn = db_connect()
            cur = conn.execute(
                "INSERT INTO users (email, username, password_hash, created_at, auth_provider) "
                "VALUES (?, ?, ?, ?, 'email')",
                (email, username, pw_hash, now),
            )
            user_id = cur.lastrowid
            conn.commit()
            conn.close()
    except sqlite3.IntegrityError:
        return jsonify({"error": "الإيميل أو اسم المستخدم مستخدم قبل كده."}), 409

    session.clear()
    session["user_id"] = user_id
    session.permanent = True
    return jsonify({
        "ok": True,
        "user": {"id": user_id, "email": email, "username": username},
    })


@app.route("/auth/login", methods=["POST"])
@limiter.limit("20 per hour")
def auth_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email or not password:
        return jsonify({"error": "اكتب الإيميل وكلمة المرور."}), 400

    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT id, email, username, password_hash, book_credits, auth_provider "
            "FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        conn.close()

    if not row:
        return jsonify({"error": "الإيميل أو كلمة المرور غلط."}), 401

    pw_hash = row["password_hash"] or ""
    if not pw_hash:
        return jsonify({
            "error": "الحساب ده متسجل بجوجل. دوس على «المتابعة مع Google».",
            "error_en": "This account uses Google. Continue with Google.",
        }), 401
    if not check_password_hash(pw_hash, password):
        return jsonify({"error": "الإيميل أو كلمة المرور غلط."}), 401

    session.clear()
    session["user_id"] = row["id"]
    session.permanent = True
    return jsonify({"ok": True, "user": user_public(dict(row))})


@app.route("/auth/google")
@limiter.limit("30 per hour")
def auth_google_start():
    if not google_ready():
        return redirect(url_for("tool_app", login="1", auth_error="google_off"))
    redirect_uri = f"{app_base_url()}/auth/google/callback"
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def auth_google_callback():
    if not google_ready():
        return redirect(url_for("tool_app", login="1", auth_error="google_off"))
    try:
        token = oauth.google.authorize_access_token()
    except Exception:
        return redirect(url_for("tool_app", login="1", auth_error="google_failed"))

    info = token.get("userinfo") if isinstance(token, dict) else None
    if not info:
        try:
            info = oauth.google.userinfo(token=token)
        except Exception:
            info = None
    if not info:
        return redirect(url_for("tool_app", login="1", auth_error="google_failed"))

    google_id = str(info.get("sub") or "").strip()
    email = (info.get("email") or "").strip().lower()
    name = (info.get("name") or info.get("given_name") or "").strip()
    if not info.get("email_verified", True):
        return redirect(url_for("tool_app", login="1", auth_error="google_unverified"))

    try:
        user_id = upsert_google_user(google_id, email, name)
    except Exception:
        return redirect(url_for("tool_app", login="1", auth_error="google_failed"))

    session.clear()
    session["user_id"] = user_id
    session.permanent = True
    return redirect(url_for("tool_app"))


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/auth/me")
def auth_me():
    user = current_user()
    if not user:
        return jsonify({"authenticated": False, "user": None})
    return jsonify({"authenticated": True, "user": user_public(user)})


@app.route("/upload", methods=["POST"])
@limiter.limit("5 per hour")
@login_required
def upload():
    freemium = check_freemium_or_error()
    if freemium:
        return freemium

    files = request.files.getlist("photos") or []
    if not files:
        single = request.files.get("photo")
        if single:
            files = [single]
    files = [f for f in files if f and f.filename][:4]
    if not files:
        return jsonify({"error": "مفيش صورة مرفوعة. اختار صورة وحاول تاني."}), 400

    session_id = secrets.token_hex(12)
    d = SESSIONS_DIR / session_id
    d.mkdir()
    try:
        for i, f in enumerate(files):
            img = Image.open(f.stream).convert("RGB")
            err = validate_portrait_image(img)
            if err and i == 0:
                shutil.rmtree(d, ignore_errors=True)
                return jsonify({"error": err}), 400
            # Cap stored resolution
            max_side = 1600
            if max(img.size) > max_side:
                img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            img.save(d / f"input_{i}.png", "PNG")
        (d / "input.png").write_bytes((d / "input_0.png").read_bytes())
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify({"error": "الصورة مش صالحة. جرّب JPG أو PNG."}), 400

    ensure_multi_refs(d)
    return jsonify({"session_id": session_id, "refs": len(ref_image_paths(d))})


@app.route("/custom-scene/<session_id>", methods=["POST"])
@login_required
def create_custom_scene(session_id: str):
    d = session_dir(session_id)
    data = request.get_json(silent=True) or {}
    arabic = (data.get("text") or "").strip()[:120]
    if len(arabic) < 3:
        return jsonify({"error": "اكتب وصف الموقف بالعربية (٣ حروف على الأقل)."}), 400

    async def _translate():
        async with httpx.AsyncClient(timeout=60.0) as client:
            return await translate_ar_to_en(arabic, client)

    try:
        english = run_async(_translate())
    except Exception as e:
        return jsonify({"error": friendly_error(e)}), 500

    scene_id = f"custom_{secrets.token_hex(6)}"
    scene = {
        "id": scene_id,
        "emoji": "✨",
        "title": arabic[:40],
        "title_en": english[:40],
        "grad": ["#e9d5ff", "#fbcfe8"],
        "scene": english,
        "custom": True,
    }
    (d / f"{scene_id}.json").write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    return jsonify(scene)


@app.route("/generate/<session_id>/<scene_id>")
@login_required
def generate_page(session_id: str, scene_id: str):
    d = session_dir(session_id)
    if not SCENE_ID_RE.match(scene_id):
        return jsonify({"error": "الموقف مش موجود."}), 400
    scene = scene_by_id(scene_id, d)
    if not scene:
        return jsonify({"error": "الموقف مش موجود."}), 400

    force = request.args.get("force") in ("1", "true", "yes")
    style = style_from_request()

    async def _run():
        async with httpx.AsyncClient(timeout=180.0) as client:
            return await generate_one_async(d, scene, force, style, client)

    try:
        result = run_async(_run())
    except Exception as e:
        return jsonify({"error": friendly_error(e)}), 500
    return jsonify(result)


@app.route("/generate-batch/<session_id>", methods=["POST"])
@login_required
def generate_batch(session_id: str):
    freemium = check_freemium_or_error()
    if freemium:
        return freemium
    d = session_dir(session_id)
    data = request.get_json(silent=True) or {}
    scene_ids = data.get("scenes") or []
    force = bool(data.get("force"))
    style = style_from_request(data)
    if not scene_ids:
        return jsonify({"error": "مفيش وظائف محددة."}), 400
    if len(scene_ids) > MAX_PAGES:
        return jsonify({"error": f"أقصى عدد للصفحات هو {MAX_PAGES}."}), 400

    scenes = []
    for sid in scene_ids:
        if not isinstance(sid, str) or not SCENE_ID_RE.match(sid):
            return jsonify({"error": "موقف غير صالح."}), 400
        scene = scene_by_id(sid, d)
        if not scene:
            return jsonify({"error": f"الموقف مش موجود: {sid}"}), 400
        scenes.append(scene)

    async def _run_all():
        async with httpx.AsyncClient(timeout=180.0) as client:
            tasks = [generate_one_async(d, sc, force, style, client) for sc in scenes]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            out = []
            for sc, res in zip(scenes, results):
                if isinstance(res, Exception):
                    out.append({"scene_id": sc["id"], "error": friendly_error(res)})
                else:
                    out.append(res)
            return out

    try:
        pages = run_async(_run_all())
    except Exception as e:
        return jsonify({"error": friendly_error(e)}), 500

    ok_ids = [p["scene_id"] for p in pages if not p.get("error")]
    created_ids = [p["scene_id"] for p in pages if not p.get("error") and p.get("created")]
    usage = None
    if created_ids:
        usage = track_book(session_id, len(created_ids), created_ids)
    elif ok_ids:
        # Restored/cached pages — no new consumption
        usage = {
            "consumed": "none",
            "pages": len(ok_ids),
            "quota": user_quota(current_user()),
        }
    return jsonify({"pages": pages, "usage": usage})


@app.route("/session/<session_id>")
def get_session(session_id: str):
    d = session_dir(session_id)
    pages = []
    customs = []
    for meta in sorted(d.glob("custom_*.json")):
        try:
            customs.append(json.loads(meta.read_text(encoding="utf-8")))
        except Exception:
            pass
    for p in sorted(d.glob("page_*.jpg")):
        sid = p.stem[len("page_"):]
        scene = scene_by_id(sid, d) or {"id": sid, "title": sid, "emoji": "🎨"}
        pages.append({
            "scene_id": sid,
            "title": scene.get("title", sid),
            "title_en": scene.get("title_en", scene.get("title", sid)),
            "emoji": scene.get("emoji", "🎨"),
            "image_b64": base64.b64encode(p.read_bytes()).decode(),
        })
    return jsonify({
        "session_id": session_id,
        "refs": len(ref_image_paths(d)),
        "pages": pages,
        "custom_scenes": customs,
    })


@app.route("/pdf/<session_id>")
@limiter.limit("5 per hour")
@login_required
def build_pdf(session_id: str):
    d = session_dir(session_id)
    order = request.args.get("order", "").split(",")
    child_name = (request.args.get("name") or "").strip()[:40]

    # Ensure premium cover + ending exist when possible (Cloudflare / Flux)
    if ACCOUNT_ID and API_TOKEN:
        timeout = httpx.Timeout(60.0, read=300.0, write=120.0, connect=30.0)

        async def _extras():
            async with httpx.AsyncClient(timeout=timeout) as client:
                if not cover_page_path(d).exists():
                    page_n = len([
                        s for s in order
                        if (s or "").strip() and SCENE_ID_RE.match((s or "").strip())
                        and page_path(d, (s or "").strip()).exists()
                    ])
                    try:
                        await generate_cover_page_async(
                            d, client,
                            child_name=child_name,
                            page_count=page_n or 0,
                            force=False,
                            use_kie=False,
                        )
                    except Exception:
                        pass
                if not ending_page_path(d).exists():
                    try:
                        await generate_ending_page_async(
                            d, client, child_name=child_name, force=False, use_kie=False
                        )
                    except Exception:
                        pass

        try:
            run_async(_extras())
        except Exception:
            pass

    all_pages = assemble_book_images(d, order, child_name, include_ending=True)
    if len(all_pages) < 2:  # cover + at least 1 interior
        abort(400, "No pages generated yet")

    pdf_path = d / "coloring_book.pdf"
    write_pdf_with_margins(all_pages, pdf_path)
    return send_file(pdf_path, as_attachment=True, download_name="coloring_book.pdf")


@app.route("/share/<session_id>", methods=["POST"])
@limiter.limit("10 per hour")
@login_required
def share_book(session_id: str):
    """Create a temporary share link (local storage, 24h). R2 optional later via env."""
    d = session_dir(session_id)
    data = request.get_json(silent=True) or {}
    order = data.get("order") or []
    child_name = (data.get("name") or "").strip()[:40]
    if not order:
        return jsonify({"error": "مفيش صفحات للمشاركة."}), 400

    all_pages = assemble_book_images(d, order, child_name, include_ending=True)
    if len(all_pages) < 2:
        return jsonify({"error": "مفيش صفحات جاهزة."}), 400

    token = secrets.token_hex(16)
    pdf_path = SHARES_DIR / f"{token}.pdf"
    write_pdf_with_margins(all_pages, pdf_path)
    expires = datetime.now(timezone.utc) + timedelta(hours=24)
    meta = {"token": token, "expires_at": expires.isoformat(), "session_id": session_id}
    (SHARES_DIR / f"{token}.json").write_text(json.dumps(meta), encoding="utf-8")
    url = f"{request.host_url.rstrip('/')}/s/{token}"
    return jsonify({"url": url, "expires_at": expires.isoformat()})


@app.route("/s/<token>")
def get_share(token: str):
    if not SHARE_ID_RE.match(token):
        abort(404)
    meta_path = SHARES_DIR / f"{token}.json"
    pdf_path = SHARES_DIR / f"{token}.pdf"
    if not meta_path.exists() or not pdf_path.exists():
        abort(404)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    exp = datetime.fromisoformat(meta["expires_at"])
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        pdf_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        abort(410)
    return send_file(pdf_path, as_attachment=True, download_name="coloring_book.pdf")


@app.route("/pay/create", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def pay_create():
    if not paymob_configured():
        return jsonify({"error": "الدفع مش متفعّل حاليًا. تواصل مع الإدارة."}), 503

    user = current_user()
    data = request.get_json(silent=True) or {}
    phone_raw = (data.get("phone") or "").strip()
    phone = normalize_egypt_phone(phone_raw)
    preferred = (data.get("method") or "all").strip().lower()
    if preferred not in ("all", "card", "wallet"):
        preferred = "all"

    # Explicit wallet uses classic CASH API (Intention API doesn't support it)
    if preferred == "wallet":
        if not wallet_enabled():
            return jsonify({"error": "المحفظة لسة مش متفعّلة على الحساب."}), 503
        digits = phone.replace("+", "")
        if not phone_raw or not digits.startswith("20") or len(digits) < 12:
            return jsonify({
                "error": "اكتب رقم موبايل المحفظة المصري (مثال: 010xxxxxxxx).",
            }), 400

        special_reference = f"pack_{user['id']}_{secrets.token_hex(8)}"
        now = datetime.now(timezone.utc).isoformat()
        with _db_lock:
            conn = db_connect()
            conn.execute(
                """
                INSERT INTO payments (user_id, special_reference, amount_cents, credits, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (user["id"], special_reference, amount_cents(), BOOK_PACK_CREDITS, now),
            )
            conn.commit()
            conn.close()

        base = os.environ.get("APP_URL", request.url_root.rstrip("/"))
        try:
            result = pay_with_wallet_classic(
                special_reference=special_reference,
                phone=phone_raw or phone,
                customer={
                    "first_name": (user.get("username") or "Customer")[:40],
                    "last_name": "User",
                    "email": user.get("email") or "customer@example.com",
                },
                redirection_url=f"{base}/pay/complete",
            )
        except Exception as e:
            with _db_lock:
                conn = db_connect()
                conn.execute(
                    "UPDATE payments SET status = 'failed' WHERE special_reference = ?",
                    (special_reference,),
                )
                conn.commit()
                conn.close()
            return jsonify({"error": friendly_error(e)}), 502

        with _db_lock:
            conn = db_connect()
            conn.execute(
                "UPDATE payments SET paymob_order_id = ?, paymob_txn_id = ? WHERE special_reference = ?",
                (result.get("order_id"), result.get("txn_id"), special_reference),
            )
            conn.commit()
            conn.close()

        if result.get("success") and not result.get("pending"):
            mark_payment_paid(
                special_reference,
                paymob_order_id=result.get("order_id"),
                paymob_txn_id=result.get("txn_id"),
            )
            return jsonify({
                "ok": True,
                "flow": "wallet",
                "reference": special_reference,
                "checkout_url": f"{base}/pay/complete?success=true&merchant_order_id={special_reference}&order={result.get('order_id')}&id={result.get('txn_id')}&amount_cents={amount_cents()}&pending=false",
                "amount_egp": BOOK_PACK_PRICE_EGP,
                "credits": BOOK_PACK_CREDITS,
            })

        if result.get("redirect_url"):
            return jsonify({
                "ok": True,
                "flow": "wallet",
                "reference": special_reference,
                "checkout_url": result["redirect_url"],
                "amount_egp": BOOK_PACK_PRICE_EGP,
                "credits": BOOK_PACK_CREDITS,
            })

        msg = result.get("message") or "الدفع بالمحفظة فشل."
        # Common when CASH integration is created but not fully activated by Paymob
        if "something went wrong" in msg.lower() or not msg:
            msg = (
                "تكامل المحفظة اتعمل، بس Paymob لسة مش مفعّلاه بالكامل على الحساب. "
                "كلّم دعم Paymob وقولهم فعّلوا Mobile Wallet / CASH على Integration "
                f"{os.environ.get('PAYMOB_INTEGRATION_ID_WALLET', '')}."
            )
        with _db_lock:
            conn = db_connect()
            conn.execute(
                "UPDATE payments SET status = 'failed' WHERE special_reference = ?",
                (special_reference,),
            )
            conn.commit()
            conn.close()
        return jsonify({"error": msg, "flow": "wallet"}), 502

    # Card / all → Unified Checkout (card integration only)
    if preferred == "all":
        preferred = "card"

    special_reference = f"pack_{user['id']}_{secrets.token_hex(8)}"
    now = datetime.now(timezone.utc).isoformat()

    with _db_lock:
        conn = db_connect()
        conn.execute(
            """
            INSERT INTO payments (user_id, special_reference, amount_cents, credits, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (user["id"], special_reference, amount_cents(), BOOK_PACK_CREDITS, now),
        )
        conn.commit()
        conn.close()

    base = os.environ.get("APP_URL", request.url_root.rstrip("/"))
    try:
        intention = create_intention(
            special_reference=special_reference,
            customer={
                "first_name": (user.get("username") or "Customer")[:40],
                "last_name": "User",
                "email": user.get("email") or "customer@example.com",
                "phone": phone,
            },
            notification_url=f"{base}/pay/webhook",
            redirection_url=f"{base}/pay/complete",
            preferred_method=preferred,
        )
    except Exception as e:
        with _db_lock:
            conn = db_connect()
            conn.execute(
                "UPDATE payments SET status = 'failed' WHERE special_reference = ?",
                (special_reference,),
            )
            conn.commit()
            conn.close()
        return jsonify({"error": friendly_error(e)}), 502

    client_secret = intention.get("client_secret")
    if not client_secret:
        return jsonify({"error": "Paymob مرجوعش client_secret."}), 502

    order_id = intention.get("intention_order_id") or intention.get("id")
    with _db_lock:
        conn = db_connect()
        conn.execute(
            "UPDATE payments SET paymob_order_id = ? WHERE special_reference = ?",
            (str(order_id) if order_id is not None else None, special_reference),
        )
        conn.commit()
        conn.close()

    return jsonify({
        "ok": True,
        "flow": "card",
        "reference": special_reference,
        "checkout_url": checkout_url(client_secret),
        "amount_egp": BOOK_PACK_PRICE_EGP,
        "credits": BOOK_PACK_CREDITS,
    })


@app.route("/pay/webhook", methods=["POST"])
def pay_webhook():
    body = request.get_json(silent=True) or {}
    obj = body.get("obj") or {}
    received = request.args.get("hmac", "")
    if not verify_transaction_post_hmac(obj, received):
        return jsonify({"error": "Invalid HMAC"}), 401

    if obj.get("success") and not obj.get("pending"):
        special = (
            obj.get("merchant_order_id")
            or (obj.get("order") or {}).get("merchant_order_id")
            or ""
        )
        # Fallback: match by paymob order id
        if not special:
            order_id = str((obj.get("order") or {}).get("id") or "")
            with _db_lock:
                conn = db_connect()
                row = conn.execute(
                    "SELECT special_reference FROM payments WHERE paymob_order_id = ?",
                    (order_id,),
                ).fetchone()
                conn.close()
            special = row["special_reference"] if row else ""
        if special:
            mark_payment_paid(
                special,
                paymob_order_id=str((obj.get("order") or {}).get("id") or ""),
                paymob_txn_id=str(obj.get("id") or ""),
            )
    return jsonify({"received": True})


@app.route("/pay/complete")
def pay_complete():
    args = {k: request.args.get(k, "") for k in request.args}
    success = str(args.get("success", "")).lower() in ("true", "1")
    pending = str(args.get("pending", "")).lower() in ("true", "1")
    special = args.get("merchant_order_id") or args.get("merchant_order") or ""
    amount_egp = None
    try:
        if args.get("amount_cents"):
            amount_egp = int(args["amount_cents"]) / 100
    except ValueError:
        amount_egp = BOOK_PACK_PRICE_EGP

    if not special and args.get("order"):
        with _db_lock:
            conn = db_connect()
            row = conn.execute(
                "SELECT special_reference FROM payments WHERE paymob_order_id = ?",
                (args.get("order"),),
            ).fetchone()
            conn.close()
        special = row["special_reference"] if row else ""

    hmac_ok = verify_redirect_hmac(args) if args.get("hmac") else False
    status = "unknown"
    book_credits = None

    if success and not pending and special and hmac_ok:
        mark_payment_paid(
            special,
            paymob_order_id=args.get("order"),
            paymob_txn_id=args.get("id"),
        )
        status = "success"
    elif success and special:
        # Already paid (wallet classic flow) or waiting for webhook
        with _db_lock:
            conn = db_connect()
            row = conn.execute(
                "SELECT status FROM payments WHERE special_reference = ?",
                (special,),
            ).fetchone()
            conn.close()
        if row and row["status"] == "paid":
            status = "success"
        elif not args.get("hmac"):
            # Our internal wallet success redirect (no Paymob hmac)
            if row and row["status"] == "paid":
                status = "success"
            else:
                status = "pending"
        else:
            status = "pending"
    elif args and not success:
        status = "failed"

    user = current_user()
    if user:
        book_credits = get_user_credits(user["id"])

    return render_template(
        "pay_complete.html",
        status=status,
        reference=special,
        credits=BOOK_PACK_CREDITS,
        price=amount_egp if amount_egp is not None else BOOK_PACK_PRICE_EGP,
        txn_id=args.get("id") or "",
        order_id=args.get("order") or "",
        card_last4=args.get("source_data.pan") or args.get("source_data_pan") or "",
        card_brand=args.get("source_data.sub_type") or args.get("source_data_sub_type") or "",
        book_credits=book_credits,
        hmac_ok=hmac_ok,
    )


@app.route("/pay/status/<reference>")
def pay_status(reference: str):
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT status, credits, amount_cents, paid_at, user_id FROM payments WHERE special_reference = ?",
            (reference,),
        ).fetchone()
        conn.close()
    if not row:
        return jsonify({"error": "الطلب مش موجود."}), 404
    user = current_user()
    credits_now = get_user_credits(user["id"]) if user else None
    return jsonify({
        "status": row["status"],
        "credits": row["credits"],
        "amount_egp": (row["amount_cents"] or 0) / 100,
        "paid_at": row["paid_at"],
        "book_credits": credits_now,
    })


@app.route("/analytics")
@admin_required
def analytics():
    stats = collect_admin_stats()
    return jsonify({
        "books_today": stats["books_today"],
        "books_week": stats["books_week"],
        "books_month": stats["books_month"],
        "books_total": stats["books_total"],
        "pages_total": stats["pages_total"],
        "users_total": stats["users_total"],
        "top_scenes": stats["top_scenes"],
        "free_books_per_month": stats["free_books_per_month"],
    })


@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("30 per hour")
def admin_login():
    if is_admin():
        return redirect(url_for("admin_dashboard"))

    error = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        # compare_digest requires equal length; mismatch → False without raising
        user_ok = (
            len(username) == len(ADMIN_USERNAME)
            and secrets.compare_digest(username, ADMIN_USERNAME)
        )
        pass_ok = (
            len(password) == len(ADMIN_PASSWORD)
            and secrets.compare_digest(password, ADMIN_PASSWORD)
        )
        if user_ok and pass_ok:
            session["is_admin"] = True
            session.permanent = True
            return redirect(url_for("admin_dashboard"))
        error = "اسم المستخدم أو كلمة المرور غلط."

    return render_template("admin_login.html", error=error)


@app.route("/admin/logout", methods=["POST", "GET"])
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    stats = collect_admin_stats()
    return render_template(
        "admin.html", stats=stats, scenes=ALL_SCENES, scene_packs=SCENE_PACKS,
        tag_labels=TAG_LABELS, cost_rates=cost_rates_dict(),
    )


@app.route("/admin/api/quick-book/upload", methods=["POST"])
@admin_required
def admin_quick_book_upload():
    """Admin: upload child photo for quick book (no freemium limits)."""
    f = request.files.get("photo") or (request.files.getlist("photos") or [None])[0]
    if not f or not f.filename:
        return jsonify({"error": "ارفع صورة الطفل."}), 400

    session_id = secrets.token_hex(12)
    d = SESSIONS_DIR / session_id
    d.mkdir()
    try:
        img = Image.open(f.stream).convert("RGB")
        err = validate_portrait_image(img)
        if err:
            shutil.rmtree(d, ignore_errors=True)
            return jsonify({"error": err}), 400
        max_side = 1600
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        img.save(d / "input_0.png", "PNG")
        (d / "input.png").write_bytes((d / "input_0.png").read_bytes())
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify({"error": "الصورة مش صالحة. جرّب JPG أو PNG."}), 400

    ensure_multi_refs(d)
    return jsonify({"ok": True, "session_id": session_id})


@app.route("/admin/api/quick-book/generate", methods=["POST"])
@admin_required
def admin_quick_book_generate():
    """Admin: generate coloring pages via Kie.ai — or free Mock placeholder."""
    data = request.get_json(silent=True) or {}
    provider = normalize_provider(data.get("provider") or data.get("model_provider"))
    if not is_mock_provider(provider) and not kie_configured():
        return jsonify({
            "error": "مفتاح Kie.ai مش مضبوط. أضف KIE_API_KEY في ملف .env وأعد تشغيل السيرفر. "
                     "أو اختار Mock mode للتجربة المجانية.",
        }), 500

    session_id = (data.get("session_id") or "").strip()
    if not session_id.isalnum() or len(session_id) > 40:
        return jsonify({"error": "session غير صالح."}), 400
    d = SESSIONS_DIR / session_id
    if not d.exists():
        return jsonify({"error": "الجلسة مش موجودة. ارفع الصورة تاني."}), 404

    scene_ids = data.get("scenes") or []
    if not isinstance(scene_ids, list) or not scene_ids:
        return jsonify({"error": "اختار وظيفة واحدة على الأقل."}), 400
    if len(scene_ids) > ADMIN_MAX_PAGES:
        return jsonify({"error": f"أقصى عدد للصفحات هو {ADMIN_MAX_PAGES}."}), 400

    force = bool(data.get("force"))
    use_async = bool(data.get("async") or data.get("background"))
    kie_model = model_for_provider(provider)
    # Sync responses keep base64 for older clients; async defaults to preview URL only
    if "include_image" in data or "include_b64" in data:
        include_b64 = bool(data.get("include_image") or data.get("include_b64"))
    else:
        include_b64 = not use_async
    try:
        order_id = int(data["order_id"]) if data.get("order_id") is not None else None
    except (TypeError, ValueError):
        order_id = None
    try:
        book_id = int(data["book_id"]) if data.get("book_id") is not None else None
    except (TypeError, ValueError):
        book_id = None
    style = {
        "variant": data.get("variant") or DEFAULT_VARIANT,
        "line_weight": data.get("line_weight") or "normal",
        "detail": data.get("detail") or "normal",
        "art_style": data.get("art_style") or "cartoon",
        # Story pages name the hero inside the prompt; coloring pages ignore this.
        "child_name": (data.get("child_name") or data.get("name") or "").strip()[:40],
    }

    scenes = []
    for sid in scene_ids:
        if not isinstance(sid, str) or not SCENE_ID_RE.match(sid):
            return jsonify({"error": "وظيفة غير صالحة."}), 400
        scene = scene_by_id(sid, d)
        if not scene:
            return jsonify({"error": f"الوظيفة مش موجودة: {sid}"}), 400
        scenes.append(scene)

    refs = ensure_multi_refs(d)
    if not refs:
        return jsonify({"error": "مفيش صورة مرجع. ارفع صورة الطفل تاني."}), 400

    def _do_generate() -> dict:
        async def _run_all():
            timeout = httpx.Timeout(60.0, read=300.0, write=120.0, connect=30.0)

            # Mock: no Kie upload, no network
            if is_mock_provider(provider):
                results = []
                for sc in scenes:
                    try:
                        results.append(
                            await generate_one_kie_async(
                                d, sc, force, style, None,
                                provider=provider, model=kie_model,
                            )
                        )
                    except Exception as e:
                        results.append({"scene_id": sc["id"], "error": friendly_error(e)})
                return [
                    r if r.get("error") else _page_for_client(r, session_id, include_b64=include_b64)
                    for r in results
                ]

            async with httpx.AsyncClient(timeout=timeout) as client:
                ref_url = None
                cached = d / "kie_ref_url.txt"
                if cached.exists():
                    try:
                        ref_url = cached.read_text(encoding="utf-8").strip() or None
                    except OSError:
                        ref_url = None
                if not ref_url:
                    ref_url = await kie_upload_image(
                        refs[0],
                        client,
                        upload_path="coloring-book",
                        file_name=f"{session_id}.png",
                    )
                    try:
                        cached.write_text(ref_url, encoding="utf-8")
                    except OSError:
                        pass

                sem = asyncio.Semaphore(3)
                tasks = [
                    generate_one_kie_async(
                        d, sc, force, style, client,
                        ref_url=ref_url, sem=sem,
                        provider=provider, model=kie_model,
                    )
                    for sc in scenes
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                out = []
                for sc, res in zip(scenes, results):
                    if isinstance(res, Exception):
                        out.append({"scene_id": sc["id"], "error": friendly_error(res)})
                    else:
                        out.append(_page_for_client(res, session_id, include_b64=include_b64))
                return out

        pages = run_async(_run_all())
        ok_ids = [p["scene_id"] for p in pages if not p.get("error")]
        created_ids = [
            p["scene_id"] for p in pages
            if not p.get("error") and p.get("created")
        ]
        if created_ids and not is_mock_provider(provider):
            ip = get_remote_address()
            now = datetime.now(timezone.utc).isoformat()
            with _db_lock:
                conn = db_connect()
                conn.execute(
                    "INSERT INTO books (created_at, ip, session_id, pages, user_id) VALUES (?, ?, ?, ?, ?)",
                    (now, ip, session_id, len(created_ids), None),
                )
                for sid in created_ids:
                    conn.execute(
                        "INSERT INTO scene_picks (created_at, scene_id) VALUES (?, ?)",
                        (now, sid),
                    )
                conn.commit()
                conn.close()
        # Permanent save on special order — each page survives disconnects/refreshes
        if order_id and any(not p.get("error") for p in pages):
            _persist_generated_to_order(order_id, session_id, book_id)
        return {
            "ok": True,
            "provider": provider,
            "model": kie_model,
            "pages": pages,
            "ok_count": len(ok_ids),
            "failed": [p for p in pages if p.get("error")],
            "session_id": session_id,
            "order_id": order_id,
            "book_id": book_id,
        }

    if use_async:
        job_id = _enqueue_job("generate", _do_generate)
        return jsonify({"ok": True, "job_id": job_id, "status": "running"})

    try:
        payload = _do_generate()
    except Exception as e:
        return jsonify({"error": friendly_error(e)}), 500
    return jsonify(payload)


@app.route("/admin/api/quick-book/cover", methods=["POST"])
@admin_required
def admin_quick_book_cover():
    """Generate premium full-color front cover via Kie — or free Mock placeholder."""
    data = request.get_json(silent=True) or {}
    provider = normalize_provider(data.get("provider") or data.get("model_provider"))
    if not is_mock_provider(provider) and not kie_configured():
        return jsonify({
            "error": "مفتاح Kie.ai مش مضبوط. أضف KIE_API_KEY في ملف .env وأعد تشغيل السيرفر. "
                     "أو اختار Mock mode للتجربة المجانية.",
        }), 500

    session_id = (data.get("session_id") or "").strip()
    child_name = (data.get("child_name") or data.get("name") or "").strip()[:40]
    pack_id = pack_by_id((data.get("pack_id") or "").strip())["id"]
    force = bool(data.get("force"))
    use_async = bool(data.get("async") or data.get("background"))
    kie_model = model_for_provider(provider)
    if "include_image" in data or "include_b64" in data:
        include_b64 = bool(data.get("include_image") or data.get("include_b64"))
    else:
        include_b64 = not use_async
    try:
        order_id = int(data["order_id"]) if data.get("order_id") is not None else None
    except (TypeError, ValueError):
        order_id = None
    try:
        book_id = int(data["book_id"]) if data.get("book_id") is not None else None
    except (TypeError, ValueError):
        book_id = None
    try:
        page_count = int(data.get("page_count") or data.get("pages") or 0)
    except (TypeError, ValueError):
        page_count = 0
    if not session_id.isalnum() or len(session_id) > 40:
        return jsonify({"error": "session غير صالح."}), 400
    d = SESSIONS_DIR / session_id
    if not d.exists():
        return jsonify({"error": "الجلسة مش موجودة. ارفع الصورة تاني."}), 404

    refs = ensure_multi_refs(d)
    if not refs:
        return jsonify({"error": "مفيش صورة مرجع. ارفع صورة الطفل تاني."}), 400

    def _do_cover() -> dict:
        async def _run():
            if is_mock_provider(provider):
                return await generate_cover_page_async(
                    d, None,
                    child_name=child_name,
                    page_count=page_count,
                    force=force,
                    use_kie=False,
                    pack_id=pack_id,
                    provider=provider,
                    model=kie_model,
                )
            timeout = httpx.Timeout(60.0, read=300.0, write=120.0, connect=30.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                ref_url = None
                cached = d / "kie_ref_url.txt"
                if cached.exists():
                    try:
                        ref_url = cached.read_text(encoding="utf-8").strip() or None
                    except OSError:
                        ref_url = None
                if not ref_url:
                    ref_url = await kie_upload_image(
                        refs[0], client,
                        upload_path="coloring-book",
                        file_name=f"{session_id}.png",
                    )
                    try:
                        cached.write_text(ref_url, encoding="utf-8")
                    except OSError:
                        pass
                return await generate_cover_page_async(
                    d, client,
                    child_name=child_name,
                    page_count=page_count,
                    force=force,
                    use_kie=True,
                    ref_url=ref_url,
                    pack_id=pack_id,
                    provider=provider,
                    model=kie_model,
                )

        page = run_async(_run())
        if order_id:
            _persist_generated_to_order(order_id, session_id, book_id)
        return {
            "ok": True,
            "page": _page_for_client(page, session_id, include_b64=include_b64),
            "provider": provider,
            "model": kie_model,
            "session_id": session_id,
            "order_id": order_id,
            "book_id": book_id,
        }

    if use_async:
        job_id = _enqueue_job("cover", _do_cover)
        return jsonify({"ok": True, "job_id": job_id, "status": "running"})

    try:
        payload = _do_cover()
    except Exception as e:
        return jsonify({"error": friendly_error(e)}), 500
    return jsonify(payload)


@app.route("/admin/api/quick-book/ending", methods=["POST"])
@admin_required
def admin_quick_book_ending():
    """Generate premium full-color final page via Kie — or free Mock placeholder."""
    data = request.get_json(silent=True) or {}
    provider = normalize_provider(data.get("provider") or data.get("model_provider"))
    if not is_mock_provider(provider) and not kie_configured():
        return jsonify({
            "error": "مفتاح Kie.ai مش مضبوط. أضف KIE_API_KEY في ملف .env وأعد تشغيل السيرفر. "
                     "أو اختار Mock mode للتجربة المجانية.",
        }), 500

    session_id = (data.get("session_id") or "").strip()
    child_name = (data.get("child_name") or data.get("name") or "").strip()[:40]
    pack_id = pack_by_id((data.get("pack_id") or "").strip())["id"]
    force = bool(data.get("force"))
    use_async = bool(data.get("async") or data.get("background"))
    kie_model = model_for_provider(provider)
    if "include_image" in data or "include_b64" in data:
        include_b64 = bool(data.get("include_image") or data.get("include_b64"))
    else:
        include_b64 = not use_async
    try:
        order_id = int(data["order_id"]) if data.get("order_id") is not None else None
    except (TypeError, ValueError):
        order_id = None
    try:
        book_id = int(data["book_id"]) if data.get("book_id") is not None else None
    except (TypeError, ValueError):
        book_id = None
    if not session_id.isalnum() or len(session_id) > 40:
        return jsonify({"error": "session غير صالح."}), 400
    d = SESSIONS_DIR / session_id
    if not d.exists():
        return jsonify({"error": "الجلسة مش موجودة. ارفع الصورة تاني."}), 404

    refs = ensure_multi_refs(d)
    if not refs:
        return jsonify({"error": "مفيش صورة مرجع. ارفع صورة الطفل تاني."}), 400

    def _do_ending() -> dict:
        async def _run():
            if is_mock_provider(provider):
                return await generate_ending_page_async(
                    d, None,
                    child_name=child_name,
                    force=force,
                    use_kie=False,
                    pack_id=pack_id,
                    provider=provider,
                    model=kie_model,
                )
            timeout = httpx.Timeout(60.0, read=300.0, write=120.0, connect=30.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                ref_url = None
                cached = d / "kie_ref_url.txt"
                if cached.exists():
                    try:
                        ref_url = cached.read_text(encoding="utf-8").strip() or None
                    except OSError:
                        ref_url = None
                if not ref_url:
                    ref_url = await kie_upload_image(
                        refs[0], client,
                        upload_path="coloring-book",
                        file_name=f"{session_id}.png",
                    )
                    try:
                        cached.write_text(ref_url, encoding="utf-8")
                    except OSError:
                        pass
                return await generate_ending_page_async(
                    d, client,
                    child_name=child_name,
                    force=force,
                    use_kie=True,
                    ref_url=ref_url,
                    pack_id=pack_id,
                    provider=provider,
                    model=kie_model,
                )

        page = run_async(_run())
        if order_id:
            _persist_generated_to_order(order_id, session_id, book_id)
        return {
            "ok": True,
            "page": _page_for_client(page, session_id, include_b64=include_b64),
            "provider": provider,
            "model": kie_model,
            "session_id": session_id,
            "order_id": order_id,
            "book_id": book_id,
        }

    if use_async:
        job_id = _enqueue_job("ending", _do_ending)
        return jsonify({"ok": True, "job_id": job_id, "status": "running"})

    try:
        payload = _do_ending()
    except Exception as e:
        return jsonify({"error": friendly_error(e)}), 500
    return jsonify(payload)


@app.route("/admin/api/quick-book/job/<job_id>", methods=["GET"])
@admin_required
def admin_quick_book_job(job_id: str):
    """Poll background generation status (avoids Cloudflare ~100s cutoff)."""
    if not job_id or len(job_id) > 40 or not re.match(r"^[a-f0-9]+$", job_id):
        return jsonify({"error": "job غير صالح."}), 400
    job = _job_read(job_id)
    if not job:
        return jsonify({
            "error": "المهمة مش موجودة أو انتهت. لو الصورة تولّدت هتلاقيها بعد التحديث.",
            "status": "missing",
        }), 404
    status = job.get("status") or "running"
    err = job.get("error")
    result = job.get("result")
    kind = job.get("kind")
    created_at = job.get("created_at") or time.time()
    elapsed = round(time.time() - float(created_at), 1)
    if status == "running":
        return jsonify({
            "ok": True,
            "status": "running",
            "kind": kind,
            "elapsed_sec": elapsed,
        })
    if status == "error":
        return jsonify({
            "ok": False,
            "status": "error",
            "kind": kind,
            "error": err or "فشل التوليد",
            "elapsed_sec": elapsed,
        }), 500
    # done
    payload = dict(result or {}) if isinstance(result, dict) else {"result": result}
    payload.update({
        "ok": True,
        "status": "done",
        "kind": kind,
        "elapsed_sec": elapsed,
        "job_id": job_id,
    })
    return jsonify(payload)


@app.route("/admin/api/quick-book/pdf/<session_id>")
@admin_required
def admin_quick_book_pdf(session_id: str):
    """Admin: download generated PDF for a quick-book session."""
    if not session_id.isalnum() or len(session_id) > 40:
        abort(400, "Bad session")
    d = SESSIONS_DIR / session_id
    if not d.exists():
        abort(404, "Session not found")

    order = request.args.get("order", "").split(",")
    child_name = (request.args.get("name") or "").strip()[:40]

    all_pages = assemble_book_images(d, order, child_name, include_ending=True)
    if len(all_pages) < 2:
        return jsonify({"error": "مفيش صفحات جاهزة. ولّد الكتاب الأول."}), 400

    pdf_path = d / "coloring_book.pdf"
    write_pdf_with_margins(all_pages, pdf_path)
    safe_name = re.sub(r"[^\w\u0600-\u06FF\-]+", "_", child_name or "coloring_book")[:40]
    download_name = f"{safe_name or 'coloring_book'}.pdf"
    return send_file(pdf_path, as_attachment=True, download_name=download_name)


@app.route("/admin/api/quick-book/preview/<session_id>/<scene_id>")
@admin_required
def admin_quick_book_preview(session_id: str, scene_id: str):
    """Serve a generated session page (cover / ending / scene) for admin preview."""
    if not session_id.isalnum() or len(session_id) > 40:
        abort(400, "Bad session")
    if not SCENE_ID_RE.match(scene_id):
        abort(400, "Bad scene")
    d = SESSIONS_DIR / session_id
    p = page_path(d, scene_id)
    if not p.exists():
        abort(404, "Page not found")
    return send_file(p, mimetype="image/jpeg", max_age=60)


@app.route("/admin/api/stats")
@admin_required
def admin_api_stats():
    return jsonify(collect_admin_stats())


@app.route("/admin/api/user/<int:user_id>/credits", methods=["POST"])
@admin_required
def admin_user_credits(user_id: int):
    """Add or subtract credits for a user. JSON body: {delta: int, note: str}"""
    data = request.get_json(silent=True) or {}
    delta = data.get("delta")
    if delta is None or not isinstance(delta, int):
        return jsonify({"error": "delta مطلوب وهو عدد صحيح (موجب = إضافة، سالب = خصم)."}), 400
    if delta == 0:
        return jsonify({"error": "delta لازم يكون غير صفر."}), 400
    if abs(delta) > 1000:
        return jsonify({"error": "الحد الأقصى 1000 credit في المرة."}), 400

    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT id, username, book_credits FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "المستخدم مش موجود."}), 404
        # Prevent going below 0 on deduction
        current = int((row["book_credits"] or 0))
        new_credits = max(0, current + delta)
        conn.execute("UPDATE users SET book_credits = ? WHERE id = ?", (new_credits, user_id))
        conn.commit()
        conn.close()

    return jsonify({
        "ok": True,
        "user_id": user_id,
        "delta": delta,
        "new_credits": new_credits,
    })


@app.route("/admin/api/user/<int:user_id>", methods=["DELETE"])
@admin_required
def admin_delete_user(user_id: int):
    """Delete a user and their associated books/payments."""
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "المستخدم مش موجود."}), 404
        username = row["username"]
        conn.execute("DELETE FROM books WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM payments WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        conn.close()
    return jsonify({"ok": True, "deleted_user": username})


@app.route("/admin/api/payments")
@admin_required
def admin_payments():
    """Return paginated payments list."""
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50
    offset = (page - 1) * per_page
    with _db_lock:
        conn = db_connect()
        total = conn.execute("SELECT COUNT(*) AS c FROM payments").fetchone()["c"]
        rows = conn.execute(
            """
            SELECT p.id, p.special_reference, p.amount_cents, p.credits, p.status,
                   p.created_at, p.paid_at, p.paymob_order_id, p.paymob_txn_id,
                   u.username, u.email
            FROM payments p
            LEFT JOIN users u ON u.id = p.user_id
            ORDER BY p.id DESC
            LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        ).fetchall()
        conn.close()
    return jsonify({
        "total": int(total),
        "page": page,
        "per_page": per_page,
        "payments": [dict(r) for r in rows],
    })


@app.route("/admin/api/users")
@admin_required
def admin_users_search():
    """Search users by username/email. Empty q → latest 50."""
    q = (request.args.get("q") or "").strip()[:80]
    with _db_lock:
        conn = db_connect()
        if q:
            like = f"%{q}%"
            rows = conn.execute(
                """
                SELECT id, username, email, created_at, book_credits, auth_provider
                FROM users
                WHERE username LIKE ? OR email LIKE ?
                ORDER BY id DESC
                LIMIT 100
                """,
                (like, like),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, username, email, created_at, book_credits, auth_provider
                FROM users
                ORDER BY id DESC
                LIMIT 50
                """
            ).fetchall()
        conn.close()
    return jsonify({"total": len(rows), "users": [dict(r) for r in rows]})


def _csv_download(filename: str, header: list, rows: list):
    """Build a CSV download response (BOM for correct Arabic in Excel)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return app.response_class(
        "﻿" + buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/admin/api/export/users.csv")
@admin_required
def admin_export_users():
    with _db_lock:
        conn = db_connect()
        rows = conn.execute(
            "SELECT id, username, email, created_at, book_credits, auth_provider "
            "FROM users ORDER BY id"
        ).fetchall()
        conn.close()
    return _csv_download(
        "users.csv",
        ["id", "username", "email", "created_at", "book_credits", "auth_provider"],
        [tuple(r) for r in rows],
    )


@app.route("/admin/api/export/books.csv")
@admin_required
def admin_export_books():
    with _db_lock:
        conn = db_connect()
        rows = conn.execute(
            """
            SELECT b.id, b.created_at, b.pages, b.ip, b.session_id,
                   u.username, u.email
            FROM books b
            LEFT JOIN users u ON u.id = b.user_id
            ORDER BY b.id
            """
        ).fetchall()
        conn.close()
    return _csv_download(
        "books.csv",
        ["id", "created_at", "pages", "ip", "session_id", "username", "email"],
        [tuple(r) for r in rows],
    )


@app.route("/admin/api/export/payments.csv")
@admin_required
def admin_export_payments():
    with _db_lock:
        conn = db_connect()
        rows = conn.execute(
            """
            SELECT p.id, p.special_reference, p.amount_cents, p.credits, p.status,
                   p.created_at, p.paid_at, p.paymob_order_id, p.paymob_txn_id,
                   u.username, u.email
            FROM payments p
            LEFT JOIN users u ON u.id = p.user_id
            ORDER BY p.id
            """
        ).fetchall()
        conn.close()
    return _csv_download(
        "payments.csv",
        ["id", "special_reference", "amount_cents", "credits", "status",
         "created_at", "paid_at", "paymob_order_id", "paymob_txn_id",
         "username", "email"],
        [tuple(r) for r in rows],
    )


@app.route("/admin/api/user/<int:user_id>/notify", methods=["POST"])
@admin_required
def admin_notify_user(user_id: int):
    """Store a notification message in the DB for the user to see on next login."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()[:500]
    if len(message) < 5:
        return jsonify({"error": "الرسالة قصيرة جداً (5 حروف على الأقل)."}), 400
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "المستخدم مش موجود."}), 404
        # Store notification in a simple JSON column; create table if needed
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admin_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                read_at TEXT
            );
            """
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO admin_notifications (user_id, message, created_at) VALUES (?, ?, ?)",
            (user_id, message, now_iso),
        )
        conn.commit()
        conn.close()
    return jsonify({"ok": True, "user_id": user_id, "message": message})


@app.route("/admin/api/user/<int:user_id>")
@admin_required
def admin_get_user(user_id: int):
    """Get detailed info for a single user including books and payments."""
    with _db_lock:
        conn = db_connect()
        user = conn.execute(
            "SELECT id, username, email, created_at, book_credits, auth_provider FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not user:
            conn.close()
            return jsonify({"error": "المستخدم مش موجود."}), 404
        books_count = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE user_id = ?", (user_id,)
        ).fetchone()["c"]
        payments_rows = conn.execute(
            "SELECT special_reference, amount_cents, credits, status, created_at, paid_at "
            "FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT 10",
            (user_id,),
        ).fetchall()
        conn.close()
    return jsonify({
        "user": dict(user),
        "books_count": int(books_count),
        "payments": [dict(r) for r in payments_rows],
    })


# ─────────────────────────── Special Orders (WhatsApp) ───────────────────────────

ALLOWED_PHOTO_EXT = {"jpg", "jpeg", "png", "webp", "heic", "heif"}
MAX_PHOTOS_PER_ORDER = 8
MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10 MB


def _order_photo_dir(order_id: int) -> Path:
    d = SPECIAL_ORDERS_DIR / str(order_id)
    d.mkdir(exist_ok=True)
    return d


def _order_book_dir(order_id: int, book_id: Optional[int] = None) -> Path:
    """Permanent storage for generated book page images tied to a special order book."""
    if book_id is not None:
        d = _order_photo_dir(order_id) / "books" / str(book_id)
    else:
        d = _order_photo_dir(order_id) / "book"  # legacy single-book path
    d.mkdir(parents=True, exist_ok=True)
    return d


def _book_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # Parse progress
    progress: dict = {}
    raw = d.get("progress")
    if raw:
        try:
            progress = json.loads(raw) if isinstance(raw, str) else (raw or {})
            if not isinstance(progress, dict):
                progress = {}
        except (json.JSONDecodeError, TypeError):
            progress = {}
    d["progress_data"] = progress
    scenes_raw = d.get("scenes")
    if scenes_raw:
        try:
            d["scenes_list"] = json.loads(scenes_raw) if isinstance(scenes_raw, str) else scenes_raw
        except (json.JSONDecodeError, TypeError):
            d["scenes_list"] = []
    else:
        d["scenes_list"] = progress.get("scenes") or []
    if not isinstance(d["scenes_list"], list):
        d["scenes_list"] = []
    sid = d.get("session_id")
    d["session_active"] = bool(
        sid and (SESSIONS_DIR / str(sid)).exists() and (SESSIONS_DIR / str(sid) / "input_0.png").exists()
    ) if sid else False
    pack = pack_by_id(d.get("pack_id"))
    d["pack"] = {
        "id": pack.get("id"),
        "title": pack.get("title") or pack.get("book_title"),
        "emoji": pack.get("emoji"),
        "desc": pack.get("desc"),
    }
    if not d.get("title"):
        d["title"] = pack.get("book_title") or pack.get("title") or "كتاب تلوين"
    return d


def _list_order_books(conn: sqlite3.Connection, order_id: int) -> list:
    rows = conn.execute(
        "SELECT * FROM special_order_books WHERE order_id = ? ORDER BY id ASC",
        (order_id,),
    ).fetchall()
    out = []
    for r in rows:
        b = _book_to_dict(r)
        planned = b.get("scenes_list") or []
        snap = _session_book_snapshot(
            b.get("session_id"), planned, order_id=order_id, book_id=b["id"]
        )
        b["has_cover"] = snap["has_cover"]
        b["has_ending"] = snap["has_ending"]
        b["done_pages"] = snap["done_planned"] if planned else snap["generated_scenes"]
        b["missing_pages"] = snap["missing_pages"]
        b["pdf_ready"] = bool(b.get("pdf_filename"))
        step = _infer_book_step(
            {
                **(b.get("progress_data") or {}),
                "scenes": planned,
                "has_cover": snap["has_cover"],
                "has_ending": snap["has_ending"],
                "done_pages": b["done_pages"],
            },
            snap,
            bool(b.get("pdf_filename")),
        )
        b["current_step"] = step
        labels = {
            "setup": "الإعداد", "cover": "غلاف البداية", "pages": "صفحات التلوين",
            "ending": "غلاف النهاية", "pdf": "حفظ PDF", "done": "مكتمل",
        }
        b["current_step_label"] = labels.get(step, step)
        done_n = len(b["done_pages"] or [])
        total = len(planned) if planned else (b.get("page_count") or 0)
        b["pages_summary"] = f"{done_n}/{total}" if total else f"{done_n}"
        out.append(b)
    return out


def _get_book_row(conn: sqlite3.Connection, order_id: int, book_id: int):
    return conn.execute(
        "SELECT * FROM special_order_books WHERE id = ? AND order_id = ?",
        (book_id, order_id),
    ).fetchone()


def _atomic_jpeg_bytes(path: Path, img_bytes: bytes, quality: int = 92) -> None:
    """Write JPEG atomically so a failed regen never deletes the previous good file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.writing.jpg")
    try:
        Image.open(io.BytesIO(img_bytes)).convert("RGB").save(tmp, "JPEG", quality=quality)
        tmp.replace(path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _persist_generated_to_order(
    order_id: Optional[int],
    session_id: str,
    book_id: Optional[int] = None,
) -> None:
    """Copy session pages into the order permanently and refresh progress JSON."""
    if not order_id or not session_id:
        return
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return
    bid = None
    try:
        if book_id is not None:
            bid = int(book_id)
    except (TypeError, ValueError):
        bid = None
    # Resolve book by session if not given
    if bid is None:
        try:
            with _db_lock:
                conn = db_connect()
                r = conn.execute(
                    "SELECT id FROM special_order_books WHERE order_id = ? AND session_id = ? ORDER BY id DESC LIMIT 1",
                    (oid, session_id),
                ).fetchone()
                if r:
                    bid = int(r["id"])
                conn.close()
        except Exception:
            bid = None
    try:
        _sync_session_pages_to_order(oid, session_id, book_id=bid)
        if bid is not None:
            _refresh_named_book_progress(oid, bid, session_id)
        else:
            _refresh_order_book_progress(oid, session_id)
    except Exception:
        pass


def _refresh_named_book_progress(
    order_id: int,
    book_id: int,
    session_id: Optional[str] = None,
) -> None:
    """Recompute special_order_books.progress from disk for one book."""
    with _db_lock:
        conn = db_connect()
        brow = _get_book_row(conn, order_id, book_id)
        if not brow:
            conn.close()
            return
        existing: dict = {}
        raw = brow["progress"]
        if raw:
            try:
                existing = json.loads(raw) if isinstance(raw, str) else (raw or {})
                if not isinstance(existing, dict):
                    existing = {}
            except (json.JSONDecodeError, TypeError):
                existing = {}
        planned = existing.get("scenes") or []
        if not isinstance(planned, list):
            planned = []
        col_raw = brow["scenes"]
        if not planned and col_raw:
            try:
                loaded = json.loads(col_raw) if isinstance(col_raw, str) else col_raw
                if isinstance(loaded, list):
                    planned = [s for s in loaded if isinstance(s, str)]
            except (json.JSONDecodeError, TypeError):
                planned = []
        clean = [s for s in planned if isinstance(s, str) and SCENE_ID_RE.match(s)
                 and s not in (COVER_SCENE_ID, ENDING_SCENE_ID)]
        sid = session_id or brow["session_id"]
        snap = _session_book_snapshot(sid, clean, order_id=order_id, book_id=book_id)
        now = datetime.now(timezone.utc).isoformat()
        progress = {
            **existing,
            "scenes": clean or existing.get("scenes") or [],
            "has_cover": snap["has_cover"],
            "has_ending": snap["has_ending"],
            "done_pages": snap["done_planned"] if clean else snap["generated_scenes"],
            "missing_pages": snap["missing_pages"],
            "updated_at": now,
            "step": _infer_book_step(
                {
                    **existing,
                    "scenes": clean or existing.get("scenes") or [],
                    "has_cover": snap["has_cover"],
                    "has_ending": snap["has_ending"],
                    "done_pages": snap["done_planned"] if clean else snap["generated_scenes"],
                    "missing_pages": snap["missing_pages"],
                },
                snap,
                bool(brow["pdf_filename"]),
            ),
        }
        pc = progress.get("page_count")
        if pc is None and clean:
            pc = len(clean)
        conn.execute(
            """
            UPDATE special_order_books
            SET progress = ?, page_count = COALESCE(?, page_count),
                session_id = COALESCE(?, session_id),
                book_updated_at = ?, updated_at = ?
            WHERE id = ? AND order_id = ?
            """,
            (
                json.dumps(progress, ensure_ascii=False),
                pc,
                sid,
                now,
                now,
                book_id,
                order_id,
            ),
        )
        # Mirror latest book onto order legacy columns for list badges
        conn.execute(
            """
            UPDATE special_orders
            SET book_session_id = ?, book_scenes = ?, book_page_count = ?,
                book_progress = ?, book_updated_at = ?, updated_at = ?,
                pdf_filename = COALESCE(?, pdf_filename)
            WHERE id = ?
            """,
            (
                sid,
                json.dumps(clean, ensure_ascii=False) if clean else brow["scenes"],
                pc if pc is not None else brow["page_count"],
                json.dumps(progress, ensure_ascii=False),
                now,
                now,
                brow["pdf_filename"],
                order_id,
            ),
        )
        conn.commit()
        conn.close()


def _refresh_order_book_progress(order_id: int, session_id: Optional[str] = None) -> None:
    """Recompute order book_progress from disk without relying on the browser."""
    # Prefer named book if session matches
    try:
        with _db_lock:
            conn = db_connect()
            r = None
            if session_id:
                r = conn.execute(
                    "SELECT id FROM special_order_books WHERE order_id = ? AND session_id = ? ORDER BY id DESC LIMIT 1",
                    (order_id, session_id),
                ).fetchone()
            if not r:
                r = conn.execute(
                    "SELECT id FROM special_order_books WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                    (order_id,),
                ).fetchone()
            conn.close()
        if r:
            _refresh_named_book_progress(order_id, int(r["id"]), session_id)
            return
    except Exception:
        pass
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return
        existing: dict = {}
        raw = row["book_progress"] if "book_progress" in row.keys() else None
        if raw:
            try:
                existing = json.loads(raw) if isinstance(raw, str) else (raw or {})
                if not isinstance(existing, dict):
                    existing = {}
            except (json.JSONDecodeError, TypeError):
                existing = {}
        planned = existing.get("scenes") or []
        if not isinstance(planned, list):
            planned = []
        col_raw = row["book_scenes"] if "book_scenes" in row.keys() else None
        if not planned and col_raw:
            try:
                loaded = json.loads(col_raw) if isinstance(col_raw, str) else col_raw
                if isinstance(loaded, list):
                    planned = [s for s in loaded if isinstance(s, str)]
            except (json.JSONDecodeError, TypeError):
                planned = []
        clean = [s for s in planned if isinstance(s, str) and SCENE_ID_RE.match(s)
                 and s not in (COVER_SCENE_ID, ENDING_SCENE_ID)]
        sid = session_id or (row["book_session_id"] if "book_session_id" in row.keys() else None)
        snap = _session_book_snapshot(sid, clean, order_id=order_id)
        now = datetime.now(timezone.utc).isoformat()
        progress = {
            **existing,
            "scenes": clean or existing.get("scenes") or [],
            "has_cover": snap["has_cover"],
            "has_ending": snap["has_ending"],
            "done_pages": snap["done_planned"] if clean else snap["generated_scenes"],
            "missing_pages": snap["missing_pages"],
            "updated_at": now,
            "step": _infer_book_step(
                {
                    **existing,
                    "scenes": clean or existing.get("scenes") or [],
                    "has_cover": snap["has_cover"],
                    "has_ending": snap["has_ending"],
                    "done_pages": snap["done_planned"] if clean else snap["generated_scenes"],
                    "missing_pages": snap["missing_pages"],
                },
                snap,
                bool(row["pdf_filename"] if "pdf_filename" in row.keys() else None),
            ),
        }
        pc = progress.get("page_count")
        if pc is None and clean:
            pc = len(clean)
        conn.execute(
            """
            UPDATE special_orders
            SET book_progress = ?, book_page_count = COALESCE(?, book_page_count),
                book_updated_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(progress, ensure_ascii=False), pc, now, now, order_id),
        )
        conn.commit()
        conn.close()


def _sync_session_pages_to_order(
    order_id: int,
    session_id: Optional[str],
    book_id: Optional[int] = None,
) -> list:
    """Copy all generated page_*.jpg from session into permanent order book folder."""
    saved: list = []
    odir = _order_book_dir(order_id, book_id)
    if session_id:
        d = SESSIONS_DIR / str(session_id)
        if d.exists():
            for p in d.glob("page_*.jpg"):
                try:
                    dest = odir / p.name
                    shutil.copy2(p, dest)
                except OSError:
                    continue
    for p in sorted(odir.glob("page_*.jpg")):
        sid = p.stem[len("page_"):]
        if SCENE_ID_RE.match(sid):
            saved.append(sid)
    return saved


def _order_book_page_path(order_id: int, scene_id: str, book_id: Optional[int] = None) -> Path:
    return _order_book_dir(order_id, book_id) / f"page_{scene_id}.jpg"


def _order_book_snapshot(
    order_id: int,
    planned_scenes: Optional[list] = None,
    book_id: Optional[int] = None,
) -> dict:
    """Inspect permanent order book folder for assets (survives session cleanup).

    When ``book_id`` is set, ONLY that book's folder is used — never the legacy
    single-book ``order/book`` path (would bleed the first book into every new one).
    """
    snap = {
        "has_cover": False,
        "has_ending": False,
        "generated_scenes": [],
        "done_planned": [],
        "missing_pages": list(planned_scenes or []),
        "session_active": False,
        "from_order_store": True,
    }
    odir = _order_book_dir(order_id, book_id)
    snap["has_cover"] = (_order_book_page_path(order_id, COVER_SCENE_ID, book_id)).exists()
    snap["has_ending"] = (_order_book_page_path(order_id, ENDING_SCENE_ID, book_id)).exists()
    page_ids = []
    for p in sorted(odir.glob("page_*.jpg")):
        sid = p.stem[len("page_"):]
        if sid not in (COVER_SCENE_ID, ENDING_SCENE_ID) and SCENE_ID_RE.match(sid):
            page_ids.append(sid)
    # Legacy shared folder only when no named multi-book id
    if book_id is None:
        leg = SPECIAL_ORDERS_DIR / str(order_id) / "book"
        if not snap["has_cover"] and (leg / f"page_{COVER_SCENE_ID}.jpg").exists():
            snap["has_cover"] = True
        if not snap["has_ending"] and (leg / f"page_{ENDING_SCENE_ID}.jpg").exists():
            snap["has_ending"] = True
        if not page_ids and leg.exists():
            for p in sorted(leg.glob("page_*.jpg")):
                sid = p.stem[len("page_"):]
                if sid not in (COVER_SCENE_ID, ENDING_SCENE_ID) and SCENE_ID_RE.match(sid):
                    page_ids.append(sid)
    snap["generated_scenes"] = page_ids
    planned = [s for s in (planned_scenes or []) if isinstance(s, str) and SCENE_ID_RE.match(s)]
    if planned:
        done = [s for s in planned if _order_book_page_path(order_id, s, book_id).exists()]
        if book_id is None:
            leg = SPECIAL_ORDERS_DIR / str(order_id) / "book"
            for s in planned:
                if s not in done and (leg / f"page_{s}.jpg").exists():
                    done.append(s)
        snap["done_planned"] = done
        snap["missing_pages"] = [s for s in planned if s not in done]
    else:
        snap["done_planned"] = page_ids
        snap["missing_pages"] = []
    return snap


def _safe_photo_name(filename: str) -> Optional[str]:
    """Return a safe filename or None if extension not allowed."""
    name = Path(filename).name
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED_PHOTO_EXT:
        return None
    return f"{secrets.token_hex(8)}.{ext}"


def _order_share_active(token: Optional[str], expires_raw: Optional[str]) -> bool:
    if not token or not expires_raw:
        return False
    try:
        exp = datetime.fromisoformat(expires_raw)
    except ValueError:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > datetime.now(timezone.utc)


def _order_to_dict(row: sqlite3.Row, photos: list) -> dict:
    d = dict(row)
    d["photos"] = photos
    # pdf_filename / assigned_to / share / book fields may not exist in older rows
    for key in (
        "pdf_filename", "assigned_to", "share_token", "share_expires_at",
        "book_session_id", "book_scenes", "book_page_count", "book_updated_at",
        "book_progress",
    ):
        if key not in d:
            d[key] = None
    token = d.get("share_token")
    d["share_active"] = _order_share_active(token, d.get("share_expires_at"))
    d["share_url"] = f"{app_base_url()}/so/{token}" if token else None
    # Parse stored scenes
    scenes_raw = d.get("book_scenes")
    if scenes_raw:
        try:
            d["book_scenes_list"] = json.loads(scenes_raw) if isinstance(scenes_raw, str) else scenes_raw
        except (json.JSONDecodeError, TypeError):
            d["book_scenes_list"] = []
    else:
        d["book_scenes_list"] = []
    # Parse progress JSON
    prog_raw = d.get("book_progress")
    progress: dict = {}
    if prog_raw:
        try:
            progress = json.loads(prog_raw) if isinstance(prog_raw, str) else (prog_raw or {})
            if not isinstance(progress, dict):
                progress = {}
        except (json.JSONDecodeError, TypeError):
            progress = {}
    d["book_progress_data"] = progress
    sid = d.get("book_session_id")
    d["book_session_active"] = bool(
        sid and (SESSIONS_DIR / str(sid)).exists() and (SESSIONS_DIR / str(sid) / "input_0.png").exists()
    ) if sid else False
    return d


def _session_book_snapshot(
    session_id: Optional[str],
    planned_scenes: Optional[list] = None,
    order_id: Optional[int] = None,
    book_id: Optional[int] = None,
) -> dict:
    """Inspect session (+ permanent order store) for completed book assets."""
    snap = {
        "has_cover": False,
        "has_ending": False,
        "generated_scenes": [],
        "done_planned": [],
        "missing_pages": list(planned_scenes or []),
        "session_active": False,
        "from_order_store": False,
    }
    page_ids: list = []
    if session_id:
        d = SESSIONS_DIR / str(session_id)
        if d.exists() and (d / "input_0.png").exists():
            snap["session_active"] = True
            snap["has_cover"] = cover_page_path(d).exists()
            snap["has_ending"] = ending_page_path(d).exists()
            for p in sorted(d.glob("page_*.jpg")):
                sid = p.stem[len("page_"):]
                if sid not in (COVER_SCENE_ID, ENDING_SCENE_ID) and SCENE_ID_RE.match(sid):
                    page_ids.append(sid)

    # Merge permanent order store (prefer any available)
    if order_id is not None:
        o_snap = _order_book_snapshot(order_id, planned_scenes, book_id=book_id)
        snap["has_cover"] = snap["has_cover"] or o_snap["has_cover"]
        snap["has_ending"] = snap["has_ending"] or o_snap["has_ending"]
        for sid in o_snap["generated_scenes"]:
            if sid not in page_ids:
                page_ids.append(sid)
        if o_snap["generated_scenes"] or o_snap["has_cover"] or o_snap["has_ending"]:
            snap["from_order_store"] = True

    snap["generated_scenes"] = page_ids
    planned = [s for s in (planned_scenes or []) if isinstance(s, str) and SCENE_ID_RE.match(s)]
    if planned:
        # done if exists in session or THIS book's store (no cross-book spill via legacy folder)
        done = []
        for s in planned:
            ok = False
            if session_id:
                d = SESSIONS_DIR / str(session_id)
                if d.exists() and page_path(d, s).exists():
                    ok = True
            if not ok and order_id is not None and _order_book_page_path(order_id, s, book_id).exists():
                ok = True
            if not ok and order_id is not None and book_id is None:
                if (SPECIAL_ORDERS_DIR / str(order_id) / "book" / f"page_{s}.jpg").exists():
                    ok = True
            if ok:
                done.append(s)
        snap["done_planned"] = done
        snap["missing_pages"] = [s for s in planned if s not in done]
    else:
        snap["done_planned"] = page_ids
        snap["missing_pages"] = []
    return snap


def _infer_book_step(progress: dict, snap: dict, has_pdf: bool) -> str:
    """Return setup|cover|pages|ending|pdf|done based on saved plan + disk state."""
    planned = progress.get("scenes") or []
    if not isinstance(planned, list):
        planned = []
    planned = [s for s in planned if isinstance(s, str)]
    want_cover, want_ending = cover_mode_flags(progress.get("cover_mode"))
    if not planned and not snap.get("generated_scenes"):
        return "setup"
    if not snap.get("session_active") and not snap.get("from_order_store"):
        return "setup"
    # Prefer real disk state over stale progress flags
    if want_cover and not snap.get("has_cover"):
        return "cover"
    missing = snap.get("missing_pages")
    if planned and missing:
        return "pages"
    if planned and not snap.get("done_planned"):
        return "pages"
    if not planned and not snap.get("generated_scenes"):
        return "pages"
    if want_ending and not snap.get("has_ending"):
        return "ending"
    if not has_pdf:
        return "pdf"
    return "done"


def _build_book_status(row: sqlite3.Row, photos: list) -> dict:
    order = _order_to_dict(row, photos)
    progress = dict(order.get("book_progress_data") or {})
    planned = progress.get("scenes") or order.get("book_scenes_list") or []
    if not isinstance(planned, list):
        planned = []
    order_id = order.get("id")
    # Sync latest session pages into permanent store
    if order.get("book_session_id") and order_id:
        try:
            _sync_session_pages_to_order(int(order_id), order.get("book_session_id"))
        except Exception:
            pass
    snap = _session_book_snapshot(order.get("book_session_id"), planned, order_id=order_id)
    # Merge progress flags with disk (disk wins)
    progress["has_cover"] = snap["has_cover"]
    progress["has_ending"] = snap["has_ending"]
    progress["done_pages"] = snap["done_planned"] if planned else snap["generated_scenes"]
    progress["missing_pages"] = snap["missing_pages"]
    if planned and not progress.get("scenes"):
        progress["scenes"] = planned
    if order.get("book_page_count") and not progress.get("page_count"):
        progress["page_count"] = order["book_page_count"]
    if order.get("child_name") and not progress.get("child_name"):
        progress["child_name"] = order["child_name"]
    step = _infer_book_step(progress, snap, bool(order.get("pdf_filename")))
    progress["step"] = step
    labels = {
        "setup": "الإعداد",
        "cover": "غلاف البداية",
        "pages": "صفحات التلوين",
        "ending": "غلاف النهاية",
        "pdf": "حفظ PDF",
        "done": "مكتمل",
    }
    # URLs for each saved page (permanent order store first)
    page_urls = {}
    if order_id:
        for sid in ([COVER_SCENE_ID] if snap["has_cover"] else []) + list(progress["done_pages"]) + (
            [ENDING_SCENE_ID] if snap["has_ending"] else []
        ):
            page_urls[sid] = f"/admin/special-orders/{order_id}/book-page/{sid}"
    return {
        "order": order,
        "progress": progress,
        "snapshot": snap,
        "current_step": step,
        "current_step_label": labels.get(step, step),
        "session_id": order.get("book_session_id"),
        "has_cover": snap["has_cover"],
        "has_ending": snap["has_ending"],
        "generated_scenes": snap["generated_scenes"],
        "done_pages": progress["done_pages"],
        "missing_pages": snap["missing_pages"],
        "pdf_ready": bool(order.get("pdf_filename")),
        "page_urls": page_urls,
    }


def _build_named_book_status(order_row: sqlite3.Row, book_row: sqlite3.Row, photos: list) -> dict:
    """Status payload for one special_order_books row + parent order."""
    order = _order_to_dict(order_row, photos)
    book = _book_to_dict(book_row)
    book_id = book["id"]
    order_id = order["id"]
    progress = dict(book.get("progress_data") or {})
    planned = progress.get("scenes") or book.get("scenes_list") or []
    if not isinstance(planned, list):
        planned = []
    # Isolate this book: never inherit another book's plan/pdf from order-level columns
    order["book_scenes_list"] = list(planned) if planned else []
    order["book_page_count"] = book.get("page_count") or progress.get("page_count")
    order["book_progress_data"] = progress
    order["book_session_id"] = book.get("session_id")
    order["book_session_active"] = bool(book.get("session_active"))
    order["pdf_filename"] = book.get("pdf_filename")  # this book only
    if book.get("session_id"):
        try:
            _sync_session_pages_to_order(order_id, book["session_id"], book_id=book_id)
        except Exception:
            pass
    snap = _session_book_snapshot(
        book.get("session_id"), planned, order_id=order_id, book_id=book_id
    )
    # Empty book on disk: drop any leaked plan/progress so UI starts fresh
    own_disk = bool(
        snap.get("has_cover") or snap.get("has_ending") or snap.get("generated_scenes")
    )
    if not own_disk and not book.get("pdf_filename"):
        if not planned:
            progress = {
                "step": "setup",
                "pack_id": book.get("pack_id") or progress.get("pack_id") or "jobs",
                "child_name": progress.get("child_name") or (order.get("child_name") or ""),
                "photo": progress.get("photo"),
                "scenes": [],
                "page_count": None,
                "has_cover": False,
                "has_ending": False,
                "done_pages": [],
                "missing_pages": [],
            }
            planned = []
    progress["has_cover"] = snap["has_cover"]
    progress["has_ending"] = snap["has_ending"]
    progress["done_pages"] = snap["done_planned"] if planned else snap["generated_scenes"]
    progress["missing_pages"] = snap["missing_pages"]
    if planned and not progress.get("scenes"):
        progress["scenes"] = planned
    if book.get("page_count") and not progress.get("page_count"):
        progress["page_count"] = book["page_count"]
    if order.get("child_name") and not progress.get("child_name"):
        progress["child_name"] = order["child_name"]
    if book.get("pack_id") and not progress.get("pack_id"):
        progress["pack_id"] = book["pack_id"]
    step = _infer_book_step(progress, snap, bool(book.get("pdf_filename")))
    if not own_disk and not book.get("pdf_filename") and not planned:
        step = "setup"
        progress["step"] = "setup"
    else:
        progress["step"] = step
    labels = {
        "setup": "الإعداد", "cover": "غلاف البداية", "pages": "صفحات التلوين",
        "ending": "غلاف النهاية", "pdf": "حفظ PDF", "done": "مكتمل",
    }
    page_urls = {}
    for sid in ([COVER_SCENE_ID] if snap["has_cover"] else []) + list(progress["done_pages"]) + (
        [ENDING_SCENE_ID] if snap["has_ending"] else []
    ):
        page_urls[sid] = f"/admin/special-orders/{order_id}/books/{book_id}/page/{sid}"
    return {
        "order": order,
        "book": book,
        "book_id": book_id,
        "pack_id": book.get("pack_id"),
        "progress": progress,
        "snapshot": snap,
        "current_step": step,
        "current_step_label": labels.get(step, step),
        "session_id": book.get("session_id"),
        "has_cover": snap["has_cover"],
        "has_ending": snap["has_ending"],
        "generated_scenes": snap["generated_scenes"],
        "done_pages": progress["done_pages"],
        "missing_pages": snap["missing_pages"],
        "pdf_ready": bool(book.get("pdf_filename")),
        "pdf_filename": book.get("pdf_filename"),
        "download_url": (
            f"/admin/special-orders/{order_id}/books/{book_id}/pdf/{book['pdf_filename']}"
            if book.get("pdf_filename") else None
        ),
        "page_urls": page_urls,
    }


@app.route("/admin/api/special-orders/<int:order_id>/books", methods=["GET"])
@admin_required
def admin_list_order_books(order_id: int):
    """List all coloring books saved for this child order."""
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        photos = [
            r["filename"]
            for r in conn.execute(
                "SELECT filename FROM special_order_photos WHERE order_id = ? ORDER BY id ASC",
                (order_id,),
            ).fetchall()
        ]
        books = _list_order_books(conn, order_id)
        order = _order_to_dict(row, photos)
        conn.close()
    return jsonify({"ok": True, "order": order, "books": books, "count": len(books)})


@app.route("/admin/api/special-orders/<int:order_id>/books", methods=["POST"])
@admin_required
def admin_create_order_book(order_id: int):
    """Create a new independent book for this order (child can have many)."""
    data = request.get_json(silent=True) or {}
    pack_id = (data.get("pack_id") or data.get("pack") or "jobs").strip()
    pack = pack_by_id(pack_id)
    title = (data.get("title") or pack.get("book_title") or pack.get("title") or "كتاب تلوين")[:80]
    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        photos = [
            r["filename"]
            for r in conn.execute(
                "SELECT filename FROM special_order_photos WHERE order_id = ? ORDER BY id ASC",
                (order_id,),
            ).fetchall()
        ]
        if not photos:
            conn.close()
            return jsonify({"error": "ارفع صورة للطفل على الطلب الأول."}), 400
        progress = {
            "step": "setup",
            "pack_id": pack["id"],
            "child_name": (row["child_name"] or "")[:40],
            "photo": photos[0],
            "scenes": [],
            "page_count": None,
            "has_cover": False,
            "has_ending": False,
            "done_pages": [],
            "updated_at": now,
        }
        conn.execute(
            """
            INSERT INTO special_order_books
            (order_id, pack_id, title, session_id, scenes, page_count, progress,
             pdf_filename, created_at, updated_at, book_updated_at)
            VALUES (?, ?, ?, NULL, NULL, NULL, ?, NULL, ?, ?, ?)
            """,
            (
                order_id,
                pack["id"],
                title,
                json.dumps(progress, ensure_ascii=False),
                now, now, now,
            ),
        )
        book_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
        # ensure book pages folder
        _order_book_dir(order_id, book_id)
        brow = _get_book_row(conn, order_id, book_id)
        status = _build_named_book_status(row, brow, photos)
        conn.close()
    return jsonify({"ok": True, "created": True, **status}), 201


@app.route("/admin/api/special-orders/<int:order_id>/books/<int:book_id>", methods=["GET"])
@admin_required
def admin_get_order_book(order_id: int, book_id: int):
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        brow = _get_book_row(conn, order_id, book_id)
        if not brow:
            conn.close()
            return jsonify({"error": "الكتاب مش موجود."}), 404
        photos = [
            r["filename"]
            for r in conn.execute(
                "SELECT filename FROM special_order_photos WHERE order_id = ? ORDER BY id ASC",
                (order_id,),
            ).fetchall()
        ]
        status = _build_named_book_status(row, brow, photos)
        conn.close()
    return jsonify({"ok": True, **status})


@app.route("/admin/api/special-orders/<int:order_id>/books/<int:book_id>", methods=["DELETE"])
@admin_required
def admin_delete_order_book(order_id: int, book_id: int):
    with _db_lock:
        conn = db_connect()
        brow = _get_book_row(conn, order_id, book_id)
        if not brow:
            conn.close()
            return jsonify({"error": "الكتاب مش موجود."}), 404
        pdf_name = brow["pdf_filename"]
        conn.execute("DELETE FROM special_order_books WHERE id = ? AND order_id = ?", (book_id, order_id))
        conn.commit()
        conn.close()
    # cleanup files
    try:
        bdir = SPECIAL_ORDERS_DIR / str(order_id) / "books" / str(book_id)
        if bdir.exists():
            shutil.rmtree(bdir, ignore_errors=True)
        if pdf_name:
            p = SPECIAL_ORDERS_DIR / str(order_id) / pdf_name
            if p.exists():
                p.unlink(missing_ok=True)
    except OSError:
        pass
    return jsonify({"ok": True, "deleted": True, "book_id": book_id})


@app.route("/admin/api/special-orders/<int:order_id>/books/<int:book_id>/start", methods=["POST"])
@admin_required
def admin_order_book_start_by_id(order_id: int, book_id: int):
    """Start/resume generation session for one named book under the order."""
    data = request.get_json(silent=True) or {}
    force_new = bool(data.get("force_new") or data.get("force"))
    photo_name = (data.get("photo") or "").strip() or None

    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        brow = _get_book_row(conn, order_id, book_id)
        if not row or not brow:
            conn.close()
            return jsonify({"error": "الطلب أو الكتاب مش موجود."}), 404
        photo_rows = conn.execute(
            "SELECT filename FROM special_order_photos WHERE order_id = ? ORDER BY id ASC",
            (order_id,),
        ).fetchall()
        photos = [r["filename"] for r in photo_rows]
        if not photos:
            conn.close()
            return jsonify({"error": "ارفع صورة للطفل على الطلب الأول."}), 400
        chosen = photo_name if photo_name in photos else photos[0]

        existing_sid = brow["session_id"]
        if existing_sid and not force_new:
            d_exist = SESSIONS_DIR / str(existing_sid)
            if d_exist.exists() and (d_exist / "input_0.png").exists():
                status = _build_named_book_status(row, brow, photos)
                conn.close()
                return jsonify({"ok": True, "resumed": True, **status})

        session_id = secrets.token_hex(12)
        src = _order_photo_dir(order_id) / chosen
        if not src.exists():
            conn.close()
            return jsonify({"error": "ملف الصورة مش موجود على السيرفر."}), 404
        d = SESSIONS_DIR / session_id
        d.mkdir()
        try:
            img = Image.open(src).convert("RGB")
            err = validate_portrait_image(img)
            if err:
                shutil.rmtree(d, ignore_errors=True)
                conn.close()
                return jsonify({"error": err}), 400
            max_side = 1600
            if max(img.size) > max_side:
                img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            img.save(d / "input_0.png", "PNG")
            (d / "input.png").write_bytes((d / "input_0.png").read_bytes())
            ensure_multi_refs(d)
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            conn.close()
            return jsonify({"error": "فشل تجهيز صورة الطفل."}), 500

        now = datetime.now(timezone.utc).isoformat()
        existing: dict = {}
        raw = brow["progress"]
        if raw and not force_new:
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
                if isinstance(parsed, dict):
                    existing = parsed
            except (json.JSONDecodeError, TypeError):
                existing = {}
        scenes_from_col = []
        if brow["scenes"]:
            try:
                loaded = json.loads(brow["scenes"]) if isinstance(brow["scenes"], str) else brow["scenes"]
                if isinstance(loaded, list):
                    scenes_from_col = [s for s in loaded if isinstance(s, str)]
            except (json.JSONDecodeError, TypeError):
                scenes_from_col = []
        planned_scenes = existing.get("scenes") if isinstance(existing.get("scenes"), list) else []
        planned_scenes = [s for s in planned_scenes if isinstance(s, str)] or scenes_from_col

        if force_new:
            progress = {
                "step": "setup",
                "photo": chosen,
                "pack_id": brow["pack_id"] or existing.get("pack_id") or "jobs",
                "child_name": (row["child_name"] or "")[:40],
                "scenes": [],
                "page_count": None,
                "has_cover": False,
                "has_ending": False,
                "done_pages": [],
                "updated_at": now,
            }
            scenes_json = None
            pc = None
            reconnecting = False
        else:
            progress = {
                "step": existing.get("step") or ("setup" if not planned_scenes else "cover"),
                "photo": chosen or existing.get("photo"),
                "pack_id": brow["pack_id"] or existing.get("pack_id") or "jobs",
                "child_name": (existing.get("child_name") or row["child_name"] or "")[:40],
                "scenes": planned_scenes,
                "page_count": existing.get("page_count") or brow["page_count"],
                "has_cover": False,
                "has_ending": False,
                "done_pages": [],
                "missing_pages": list(planned_scenes),
                "updated_at": now,
            }
            scenes_json = json.dumps(planned_scenes, ensure_ascii=False) if planned_scenes else brow["scenes"]
            pc = progress.get("page_count")
            reconnecting = bool(existing_sid or planned_scenes)

        conn.execute(
            """
            UPDATE special_order_books
            SET session_id = ?, scenes = ?, page_count = ?, progress = ?,
                book_updated_at = ?, updated_at = ?
            WHERE id = ? AND order_id = ?
            """,
            (
                session_id,
                scenes_json,
                pc,
                json.dumps(progress, ensure_ascii=False),
                now, now, book_id, order_id,
            ),
        )
        # mirror to order for badges
        conn.execute(
            """
            UPDATE special_orders
            SET book_session_id = ?, book_scenes = ?, book_page_count = ?,
                book_progress = ?, book_updated_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (session_id, scenes_json, pc, json.dumps(progress, ensure_ascii=False), now, now, order_id),
        )
        conn.commit()
        brow = _get_book_row(conn, order_id, book_id)
        status = _build_named_book_status(row, brow, photos)
        conn.close()

    return jsonify({
        "ok": True,
        "resumed": bool(reconnecting),
        "reconnected": bool(reconnecting),
        **status,
    })


@app.route("/admin/api/special-orders/<int:order_id>/books/<int:book_id>/progress", methods=["POST"])
@admin_required
def admin_order_book_progress_by_id(order_id: int, book_id: int):
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    step = (data.get("step") or "").strip() or None
    child_name = (data.get("child_name") or data.get("name") or "").strip()[:40]
    photo = (data.get("photo") or "").strip() or None
    scenes = data.get("scenes") if isinstance(data.get("scenes"), list) else None
    try:
        page_count = int(data["page_count"]) if data.get("page_count") is not None else None
    except (TypeError, ValueError):
        page_count = None
    pack_id = (data.get("pack_id") or "").strip() or None
    provider = data.get("provider")
    cover_mode = data.get("cover_mode")

    if session_id and (not session_id.isalnum() or len(session_id) > 40):
        return jsonify({"error": "session غير صالح."}), 400

    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        brow = _get_book_row(conn, order_id, book_id)
        if not row or not brow:
            conn.close()
            return jsonify({"error": "الطلب أو الكتاب مش موجود."}), 404
        photos = [
            r["filename"]
            for r in conn.execute(
                "SELECT filename FROM special_order_photos WHERE order_id = ? ORDER BY id ASC",
                (order_id,),
            ).fetchall()
        ]
        existing = {}
        if brow["progress"]:
            try:
                existing = json.loads(brow["progress"]) if isinstance(brow["progress"], str) else (brow["progress"] or {})
                if not isinstance(existing, dict):
                    existing = {}
            except (json.JSONDecodeError, TypeError):
                existing = {}
        sid = session_id or brow["session_id"]
        if not sid:
            conn.close()
            return jsonify({"error": "مفيش جلسة كتاب. ابدأ التوليد الأول."}), 400

        planned = scenes if scenes is not None else (existing.get("scenes") or [])
        clean_planned = []
        if isinstance(planned, list):
            for s in planned:
                if isinstance(s, str) and SCENE_ID_RE.match(s) and s not in (COVER_SCENE_ID, ENDING_SCENE_ID):
                    clean_planned.append(s)
        try:
            _sync_session_pages_to_order(order_id, sid, book_id=book_id)
        except Exception:
            pass
        snap = _session_book_snapshot(sid, clean_planned, order_id=order_id, book_id=book_id)
        now = datetime.now(timezone.utc).isoformat()
        cov_mode = normalize_cover_mode(cover_mode or existing.get("cover_mode"))
        prov = normalize_provider(provider or existing.get("provider"))
        progress = {
            **existing,
            "step": step or existing.get("step") or "setup",
            "child_name": child_name or existing.get("child_name") or (row["child_name"] or ""),
            "photo": photo or existing.get("photo"),
            "pack_id": pack_id or existing.get("pack_id") or brow["pack_id"],
            "provider": prov,
            "cover_mode": cov_mode,
            "scenes": clean_planned,
            "page_count": page_count if page_count is not None else existing.get("page_count"),
            "has_cover": snap["has_cover"],
            "has_ending": snap["has_ending"],
            "done_pages": snap["done_planned"] if clean_planned else snap["generated_scenes"],
            "missing_pages": snap["missing_pages"],
            "updated_at": now,
        }
        if step == "setup" and clean_planned and not snap["has_cover"] and cover_mode_flags(cov_mode)[0]:
            progress["step"] = "setup"
        else:
            progress["step"] = _infer_book_step(progress, snap, bool(brow["pdf_filename"]))
        scenes_json = json.dumps(clean_planned, ensure_ascii=False) if clean_planned else brow["scenes"]
        pc = progress.get("page_count")
        if pc is None and clean_planned:
            pc = len(clean_planned)
        conn.execute(
            """
            UPDATE special_order_books
            SET session_id = ?, scenes = ?, page_count = ?, progress = ?,
                pack_id = COALESCE(?, pack_id),
                book_updated_at = ?, updated_at = ?
            WHERE id = ? AND order_id = ?
            """,
            (
                sid, scenes_json, pc, json.dumps(progress, ensure_ascii=False),
                pack_id, now, now, book_id, order_id,
            ),
        )
        conn.execute(
            """
            UPDATE special_orders
            SET book_session_id = ?, book_scenes = ?, book_page_count = ?,
                book_progress = ?, book_updated_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (sid, scenes_json, pc, json.dumps(progress, ensure_ascii=False), now, now, order_id),
        )
        conn.commit()
        brow = _get_book_row(conn, order_id, book_id)
        status = _build_named_book_status(row, brow, photos)
        conn.close()
    return jsonify({"ok": True, "saved": True, **status})


@app.route("/admin/api/special-orders/<int:order_id>/books/<int:book_id>/save", methods=["POST"])
@admin_required
def admin_order_book_save_by_id(order_id: int, book_id: int):
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    scenes = data.get("scenes") or []
    child_name = (data.get("child_name") or data.get("name") or "").strip()[:40]
    if not session_id.isalnum() or len(session_id) > 40:
        return jsonify({"error": "session غير صالح."}), 400
    d = SESSIONS_DIR / session_id
    if not d.exists():
        return jsonify({"error": "الجلسة مش موجودة."}), 404

    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        brow = _get_book_row(conn, order_id, book_id)
        if not row or not brow:
            conn.close()
            return jsonify({"error": "الطلب أو الكتاب مش موجود."}), 404
        photos = [
            r["filename"] for r in conn.execute(
                "SELECT filename FROM special_order_photos WHERE order_id = ? ORDER BY id ASC",
                (order_id,),
            ).fetchall()
        ]
        # ensure latest images on permanent disk
        try:
            _sync_session_pages_to_order(order_id, session_id, book_id=book_id)
        except Exception:
            pass
        # assemble from session (pages live there first)
        if not isinstance(scenes, list) or not scenes:
            scenes = json.loads(brow["scenes"]) if brow["scenes"] else []
        clean = [s for s in scenes if isinstance(s, str) and SCENE_ID_RE.match(s)]
        progress_peek: dict = {}
        if brow["progress"]:
            try:
                progress_peek = json.loads(brow["progress"]) if isinstance(brow["progress"], str) else (brow["progress"] or {})
                if not isinstance(progress_peek, dict):
                    progress_peek = {}
            except (json.JSONDecodeError, TypeError):
                progress_peek = {}
        cover_mode = normalize_cover_mode(data.get("cover_mode") or progress_peek.get("cover_mode"))
        want_cover, want_ending = cover_mode_flags(cover_mode)

        def _ensure_page_on_session(scene_id: str) -> bool:
            dest = page_path(d, scene_id)
            if dest.exists():
                return True
            if book_id is not None:
                src = _order_book_page_path(order_id, scene_id, book_id)
                if src.exists():
                    try:
                        shutil.copy2(src, dest)
                        return True
                    except OSError:
                        return False
            return False

        for sid in clean:
            _ensure_page_on_session(sid)
        if want_cover and not _ensure_page_on_session(COVER_SCENE_ID):
            conn.close()
            return jsonify({"error": "غلاف البداية مطلوب ومش جاهز. ولّده الأول."}), 400
        if want_ending and not _ensure_page_on_session(ENDING_SCENE_ID):
            conn.close()
            return jsonify({"error": "غلاف النهاية مطلوب ومش جاهز. ولّده الأول."}), 400

        all_pages = assemble_book_images(
            d, clean, child_name or (row["child_name"] or ""),
            include_ending=want_ending,
            include_cover=want_cover,
        )
        if not all_pages:
            conn.close()
            return jsonify({"error": "مفيش صفحات جاهزة. ولّد الكتاب الأول."}), 400

        pdf_path = d / "coloring_book.pdf"
        write_pdf_with_margins(all_pages, pdf_path)
        odir = _order_photo_dir(order_id)
        fname = f"book_{order_id}_{book_id}_{secrets.token_hex(6)}.pdf"
        dest = odir / fname
        shutil.copy2(pdf_path, dest)
        # remove previous book pdf only for this book
        old_pdf = brow["pdf_filename"]
        if old_pdf and old_pdf != fname:
            old_p = odir / old_pdf
            if old_p.exists() and old_p.is_file():
                try:
                    old_p.unlink()
                except OSError:
                    pass
        now = datetime.now(timezone.utc).isoformat()
        progress = {}
        if brow["progress"]:
            try:
                progress = json.loads(brow["progress"]) if isinstance(brow["progress"], str) else (brow["progress"] or {})
            except (json.JSONDecodeError, TypeError):
                progress = {}
        progress.update({
            "step": "done",
            "scenes": clean,
            "child_name": child_name or progress.get("child_name") or row["child_name"],
            "cover_mode": cover_mode,
            "pdf_ready": True,
            "updated_at": now,
        })
        conn.execute(
            """
            UPDATE special_order_books
            SET session_id = ?, scenes = ?, page_count = ?, progress = ?,
                pdf_filename = ?, book_updated_at = ?, updated_at = ?
            WHERE id = ? AND order_id = ?
            """,
            (
                session_id,
                json.dumps(clean, ensure_ascii=False),
                len(clean),
                json.dumps(progress, ensure_ascii=False),
                fname, now, now, book_id, order_id,
            ),
        )
        conn.execute(
            """
            UPDATE special_orders
            SET book_session_id = ?, book_scenes = ?, book_page_count = ?,
                book_progress = ?, pdf_filename = ?, book_updated_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                session_id,
                json.dumps(clean, ensure_ascii=False),
                len(clean),
                json.dumps(progress, ensure_ascii=False),
                fname, now, now, order_id,
            ),
        )
        conn.commit()
        brow = _get_book_row(conn, order_id, book_id)
        status = _build_named_book_status(row, brow, photos)
        conn.close()

    return jsonify({
        "ok": True,
        "saved": True,
        "pdf_filename": fname,
        "download_url": f"/admin/special-orders/{order_id}/books/{book_id}/pdf/{fname}",
        **status,
    })


@app.route("/admin/special-orders/<int:order_id>/books/<int:book_id>/page/<scene_id>")
@admin_required
def admin_serve_named_book_page(order_id: int, book_id: int, scene_id: str):
    if not SCENE_ID_RE.match(scene_id):
        abort(400, "Bad scene")
    # Strict: only this book's folder / its session (never legacy shared book/)
    p = _order_book_page_path(order_id, scene_id, book_id)
    if not p.exists():
        with _db_lock:
            conn = db_connect()
            brow = _get_book_row(conn, order_id, book_id)
            conn.close()
        if brow and brow["session_id"]:
            sess = page_path(SESSIONS_DIR / str(brow["session_id"]), scene_id)
            if sess.exists():
                return send_file(sess, mimetype="image/jpeg", max_age=60)
        abort(404, "Page not found")
    return send_file(p, mimetype="image/jpeg", max_age=60)


@app.route("/admin/special-orders/<int:order_id>/books/<int:book_id>/pdf/<filename>")
@admin_required
def admin_serve_named_book_pdf(order_id: int, book_id: int, filename: str):
    safe = Path(filename).name
    path = _order_photo_dir(order_id) / safe
    if not path.exists():
        abort(404, "PDF not found")
    return send_file(path, as_attachment=True, download_name=safe)


@app.route("/admin/api/special-orders/<int:order_id>/book/start", methods=["POST"])
@admin_required
def admin_order_book_start(order_id: int):
    """Start (or resume) a coloring-book generation session from a special order."""
    data = request.get_json(silent=True) or {}
    force_new = bool(data.get("force_new") or data.get("force"))
    photo_name = (data.get("photo") or "").strip() or None

    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        photo_rows = conn.execute(
            "SELECT filename FROM special_order_photos WHERE order_id = ? ORDER BY id ASC",
            (order_id,),
        ).fetchall()
        photos = [r["filename"] for r in photo_rows]
        if not photos:
            conn.close()
            return jsonify({"error": "ارفع صورة للطفل على الطلب الأول."}), 400

        # Choose photo
        if photo_name and photo_name in photos:
            chosen = photo_name
        else:
            chosen = photos[0]

        existing_sid = row["book_session_id"] if "book_session_id" in row.keys() else None
        if existing_sid and not force_new:
            d_exist = SESSIONS_DIR / str(existing_sid)
            if d_exist.exists() and (d_exist / "input_0.png").exists():
                status = _build_book_status(row, photos)
                conn.close()
                return jsonify({
                    "ok": True,
                    "resumed": True,
                    "session_id": existing_sid,
                    "child_name": status["progress"].get("child_name") or row["child_name"],
                    "photo": status["progress"].get("photo") or chosen,
                    "order": status["order"],
                    "generated_scenes": status["generated_scenes"],
                    "has_cover": status["has_cover"],
                    "has_ending": status["has_ending"],
                    "progress": status["progress"],
                    "current_step": status["current_step"],
                    "current_step_label": status["current_step_label"],
                    "done_pages": status["done_pages"],
                    "missing_pages": status["missing_pages"],
                    "pdf_ready": status["pdf_ready"],
                })

        # Create new session from photo on disk
        session_id = secrets.token_hex(12)
        src = _order_photo_dir(order_id) / chosen
        if not src.exists():
            conn.close()
            return jsonify({"error": "ملف الصورة مش موجود على السيرفر."}), 404
        d = SESSIONS_DIR / session_id
        d.mkdir()
        try:
            img = Image.open(src).convert("RGB")
            err = validate_portrait_image(img)
            if err:
                shutil.rmtree(d, ignore_errors=True)
                conn.close()
                return jsonify({"error": err}), 400
            max_side = 1600
            if max(img.size) > max_side:
                img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            img.save(d / "input_0.png", "PNG")
            (d / "input.png").write_bytes((d / "input_0.png").read_bytes())
            ensure_multi_refs(d)
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            conn.close()
            return jsonify({"error": "فشل تجهيز صورة الطفل."}), 500

        now = datetime.now(timezone.utc).isoformat()
        # Parse any existing plan — keep scenes when only reconnecting a lost session folder
        existing: dict = {}
        raw = row["book_progress"] if "book_progress" in row.keys() else None
        if raw and not force_new:
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
                if isinstance(parsed, dict):
                    existing = parsed
            except (json.JSONDecodeError, TypeError):
                existing = {}
        scenes_from_col = []
        col_raw = row["book_scenes"] if "book_scenes" in row.keys() else None
        if col_raw:
            try:
                loaded = json.loads(col_raw) if isinstance(col_raw, str) else col_raw
                if isinstance(loaded, list):
                    scenes_from_col = [s for s in loaded if isinstance(s, str)]
            except (json.JSONDecodeError, TypeError):
                scenes_from_col = []

        planned_scenes = existing.get("scenes") if isinstance(existing.get("scenes"), list) else []
        planned_scenes = [s for s in planned_scenes if isinstance(s, str)] or scenes_from_col
        page_count_keep = existing.get("page_count")
        if page_count_keep is None and "book_page_count" in row.keys() and row["book_page_count"] is not None:
            page_count_keep = row["book_page_count"]
        if page_count_keep is None and planned_scenes:
            page_count_keep = len(planned_scenes)
        child_keep = (existing.get("child_name") or row["child_name"] or "")[:40]

        if force_new:
            # Brand-new book run — reset wizard progress
            progress = {
                "step": "setup",
                "photo": chosen,
                "child_name": (row["child_name"] or "")[:40],
                "scenes": [],
                "page_count": None,
                "has_cover": False,
                "has_ending": False,
                "done_pages": [],
                "updated_at": now,
            }
            scenes_json = None
            pc = None
            reconnecting = False
        else:
            # Session folder was lost (restart / move path) but keep setup plan
            progress = {
                "step": existing.get("step") or ("setup" if not planned_scenes else "cover"),
                "photo": chosen or existing.get("photo"),
                "child_name": child_keep,
                "scenes": planned_scenes,
                "page_count": page_count_keep,
                "has_cover": False,
                "has_ending": False,
                "done_pages": [],
                "missing_pages": list(planned_scenes),
                "updated_at": now,
            }
            scenes_json = json.dumps(planned_scenes, ensure_ascii=False) if planned_scenes else col_raw
            pc = page_count_keep
            reconnecting = bool(existing_sid or planned_scenes)

        conn.execute(
            """
            UPDATE special_orders
            SET book_session_id = ?, book_scenes = ?, book_page_count = ?,
                book_progress = ?, book_updated_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                session_id,
                scenes_json if scenes_json is not None else (None if force_new else col_raw),
                pc,
                json.dumps(progress, ensure_ascii=False),
                now,
                now,
                order_id,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        status = _build_book_status(row, photos)
        conn.close()

    return jsonify({
        "ok": True,
        "resumed": bool(reconnecting),
        "reconnected": bool(reconnecting),
        "session_id": session_id,
        "child_name": status["progress"].get("child_name") or child_keep,
        "photo": chosen,
        "order": status["order"],
        "generated_scenes": status["generated_scenes"],
        "has_cover": status["has_cover"],
        "has_ending": status["has_ending"],
        "progress": status["progress"],
        "current_step": status["current_step"],
        "current_step_label": status["current_step_label"],
        "done_pages": status["done_pages"],
        "missing_pages": status["missing_pages"],
        "pdf_ready": status["pdf_ready"],
    })


@app.route("/admin/api/special-orders/<int:order_id>/book/status", methods=["GET"])
@admin_required
def admin_order_book_status(order_id: int):
    """Return book creation progress and disk snapshot for resume UI."""
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        photos = [
            r["filename"]
            for r in conn.execute(
                "SELECT filename FROM special_order_photos WHERE order_id = ? ORDER BY id ASC",
                (order_id,),
            ).fetchall()
        ]
        status = _build_book_status(row, photos)
        conn.close()
    return jsonify({"ok": True, **status})


@app.route("/admin/api/special-orders/<int:order_id>/book/progress", methods=["POST"])
@admin_required
def admin_order_book_progress(order_id: int):
    """Persist wizard progress after each step (no PDF required)."""
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    step = (data.get("step") or "").strip() or None
    child_name = (data.get("child_name") or data.get("name") or "").strip()[:40]
    photo = (data.get("photo") or "").strip() or None
    scenes = data.get("scenes") if isinstance(data.get("scenes"), list) else None
    try:
        page_count = int(data["page_count"]) if data.get("page_count") is not None else None
    except (TypeError, ValueError):
        page_count = None

    if session_id and (not session_id.isalnum() or len(session_id) > 40):
        return jsonify({"error": "session غير صالح."}), 400

    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404

        photos = [
            r["filename"]
            for r in conn.execute(
                "SELECT filename FROM special_order_photos WHERE order_id = ? ORDER BY id ASC",
                (order_id,),
            ).fetchall()
        ]

        # Start from existing progress
        existing = {}
        raw = row["book_progress"] if "book_progress" in row.keys() else None
        if raw:
            try:
                existing = json.loads(raw) if isinstance(raw, str) else (raw or {})
                if not isinstance(existing, dict):
                    existing = {}
            except (json.JSONDecodeError, TypeError):
                existing = {}

        sid = session_id or (row["book_session_id"] if "book_session_id" in row.keys() else None)
        if not sid:
            conn.close()
            return jsonify({"error": "مفيش جلسة كتاب. ابدأ التوليد الأول."}), 400

        planned = scenes if scenes is not None else (existing.get("scenes") or [])
        clean_planned = []
        if isinstance(planned, list):
            for s in planned:
                if isinstance(s, str) and SCENE_ID_RE.match(s) and s not in (COVER_SCENE_ID, ENDING_SCENE_ID):
                    clean_planned.append(s)

        # Always mirror session pages into permanent order storage
        try:
            _sync_session_pages_to_order(order_id, sid)
        except Exception:
            pass
        snap = _session_book_snapshot(sid, clean_planned, order_id=order_id)
        now = datetime.now(timezone.utc).isoformat()
        progress = {
            **existing,
            "step": step or existing.get("step") or "setup",
            "child_name": child_name or existing.get("child_name") or (row["child_name"] or ""),
            "photo": photo or existing.get("photo"),
            "scenes": clean_planned,
            "page_count": page_count if page_count is not None else existing.get("page_count"),
            "has_cover": snap["has_cover"],
            "has_ending": snap["has_ending"],
            "done_pages": snap["done_planned"] if clean_planned else snap["generated_scenes"],
            "missing_pages": snap["missing_pages"],
            "updated_at": now,
        }
        # Recompute current step from disk unless client only wants to bookmark setup scenes
        if step == "setup" and clean_planned and not snap["has_cover"]:
            progress["step"] = "setup"
        else:
            progress["step"] = _infer_book_step(
                progress, snap, bool(row["pdf_filename"] if "pdf_filename" in row.keys() else None)
            )

        scenes_json = json.dumps(clean_planned, ensure_ascii=False) if clean_planned else (
            row["book_scenes"] if "book_scenes" in row.keys() else None
        )
        pc = progress.get("page_count")
        if pc is None and clean_planned:
            pc = len(clean_planned)

        conn.execute(
            """
            UPDATE special_orders
            SET book_session_id = ?, book_scenes = ?, book_page_count = ?,
                book_progress = ?, book_updated_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                sid,
                scenes_json,
                pc,
                json.dumps(progress, ensure_ascii=False),
                now,
                now,
                order_id,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        status = _build_book_status(row, photos)
        conn.close()

    return jsonify({"ok": True, "saved": True, **status})


@app.route("/admin/api/special-orders/<int:order_id>/book/save", methods=["POST"])
@admin_required
def admin_order_book_save(order_id: int):
    """Build PDF from session, attach to order, and persist scene list for resume."""
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    scenes = data.get("scenes") or []
    child_name = (data.get("child_name") or data.get("name") or "").strip()[:40]

    if not session_id.isalnum() or len(session_id) > 40:
        return jsonify({"error": "session غير صالح."}), 400
    if not isinstance(scenes, list) or not scenes:
        return jsonify({"error": "مفيش صفحات محفوظة للكتاب."}), 400

    d = SESSIONS_DIR / session_id
    if not d.exists():
        return jsonify({"error": "جلسة الكتاب مش موجودة. ابدأ الإنشاء من الأول."}), 404

    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        if not child_name:
            child_name = (row["child_name"] or "").strip()[:40]

    # Clean scene list
    clean_scenes = []
    for sid in scenes:
        if isinstance(sid, str) and SCENE_ID_RE.match(sid) and sid not in (COVER_SCENE_ID, ENDING_SCENE_ID):
            if page_path(d, sid).exists():
                clean_scenes.append(sid)
    if not clean_scenes:
        return jsonify({"error": "مفيش صفحات تلوين جاهزة."}), 400

    all_pages = assemble_book_images(d, clean_scenes, child_name, include_ending=True, include_cover=True)
    if len(all_pages) < 2:
        return jsonify({"error": "مفيش صفحات كافية لعمل PDF."}), 400

    # Write session PDF then copy into order folder
    session_pdf = d / "coloring_book.pdf"
    write_pdf_with_margins(all_pages, session_pdf)

    odir = _order_photo_dir(order_id)
    # Remove old PDF if any
    old_name = None
    with _db_lock:
        conn = db_connect()
        old_row = conn.execute(
            "SELECT pdf_filename FROM special_orders WHERE id = ?", (order_id,)
        ).fetchone()
        if old_row:
            old_name = old_row["pdf_filename"] if "pdf_filename" in old_row.keys() else None
        if old_name:
            old_path = odir / old_name
            try:
                if old_path.exists():
                    old_path.unlink()
            except OSError:
                pass

        new_name = f"book_{order_id}_{secrets.token_hex(4)}.pdf"
        dest = odir / new_name
        shutil.copy2(session_pdf, dest)

        now = datetime.now(timezone.utc).isoformat()
        scenes_json = json.dumps(clean_scenes, ensure_ascii=False)
        try:
            _sync_session_pages_to_order(order_id, session_id)
        except Exception:
            pass
        snap = _session_book_snapshot(session_id, clean_scenes, order_id=order_id)
        progress = {
            "step": "done",
            "child_name": child_name,
            "scenes": clean_scenes,
            "page_count": len(clean_scenes),
            "has_cover": snap["has_cover"],
            "has_ending": snap["has_ending"],
            "done_pages": clean_scenes,
            "missing_pages": [],
            "pdf_ready": True,
            "updated_at": now,
        }
        conn.execute(
            """
            UPDATE special_orders
            SET pdf_filename = ?, book_session_id = ?, book_scenes = ?,
                book_page_count = ?, book_progress = ?, book_updated_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                new_name, session_id, scenes_json, len(clean_scenes),
                json.dumps(progress, ensure_ascii=False), now, now, order_id,
            ),
        )
        conn.commit()
        photos = [
            r["filename"]
            for r in conn.execute(
                "SELECT filename FROM special_order_photos WHERE order_id = ?", (order_id,)
            ).fetchall()
        ]
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        status = _build_book_status(row, photos)
        conn.close()

    return jsonify({
        "ok": True,
        "order": status["order"],
        "pdf_filename": new_name,
        "download_url": f"/admin/special-orders/{order_id}/pdf/{new_name}",
        "page_count": len(clean_scenes),
        "progress": status["progress"],
        "current_step": status["current_step"],
        "current_step_label": status["current_step_label"],
        "has_cover": status["has_cover"],
        "has_ending": status["has_ending"],
        "done_pages": status["done_pages"],
        "missing_pages": status["missing_pages"],
        "pdf_ready": True,
    })


@app.route("/admin/api/special-orders", methods=["GET"])
@admin_required
def admin_list_special_orders():
    """List special orders. Supports ?status=pending|done and ?q= / ?client=<search>."""
    status_filter = request.args.get("status", "").strip().lower()
    # Accept both ?q= and legacy ?client=
    search = (
        request.args.get("q") or request.args.get("client") or ""
    ).strip()
    with _db_lock:
        conn = db_connect()
        conditions = []
        params: list = []
        if status_filter in ("pending", "done"):
            conditions.append("status = ?")
            params.append(status_filter)
        if search:
            like = f"%{search}%"
            conditions.append(
                "("
                "CAST(id AS TEXT) LIKE ? OR "
                "child_name LIKE ? OR "
                "client_name LIKE ? OR "
                "phone LIKE ? OR "
                "email LIKE ? OR "
                "assigned_to LIKE ? OR "
                "notes LIKE ?"
                ")"
            )
            params.extend([like] * 7)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = conn.execute(
            f"SELECT * FROM special_orders {where} ORDER BY id DESC",
            params,
        ).fetchall()
        photo_rows = conn.execute(
            "SELECT order_id, filename FROM special_order_photos"
        ).fetchall()
        conn.close()

    photos_by_order: dict = {}
    for p in photo_rows:
        photos_by_order.setdefault(p["order_id"], []).append(p["filename"])

    orders = [_order_to_dict(r, photos_by_order.get(r["id"], [])) for r in rows]
    return jsonify({"orders": orders, "total": len(orders)})


@app.route("/admin/api/special-orders/<int:order_id>", methods=["GET"])
@admin_required
def admin_get_special_order(order_id: int):
    """Get single special order details with photos."""
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        photo_rows = conn.execute(
            "SELECT filename FROM special_order_photos WHERE order_id = ?", (order_id,)
        ).fetchall()
        conn.close()
    photos = [r["filename"] for r in photo_rows]
    return jsonify({"ok": True, "order": _order_to_dict(row, photos)})


@app.route("/admin/api/special-orders", methods=["POST"])
@admin_required
def admin_create_special_order():
    """Create a new special order. JSON body (child_name required)."""
    data = request.get_json(silent=True) or {}
    child_name = (data.get("child_name") or "").strip()
    if not child_name:
        return jsonify({"error": "اسم الطفل مطلوب."}), 400

    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        conn = db_connect()
        cur = conn.execute(
            """
            INSERT INTO special_orders
              (child_name, client_name, phone, email, notes, status, assigned_to, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                child_name,
                (data.get("client_name") or "").strip() or None,
                (data.get("phone") or "").strip() or None,
                (data.get("email") or "").strip() or None,
                (data.get("notes") or "").strip() or None,
                data.get("status", "pending") if data.get("status") in ("pending", "done") else "pending",
                (data.get("assigned_to") or "").strip() or None,
                now,
            ),
        )
        order_id = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        conn.close()
    return jsonify({"ok": True, "order": _order_to_dict(row, [])}), 201


@app.route("/admin/api/special-orders/<int:order_id>", methods=["PUT"])
@admin_required
def admin_update_special_order(order_id: int):
    """Update fields of an existing special order."""
    data = request.get_json(silent=True) or {}
    now = datetime.now(timezone.utc).isoformat()

    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404

        child_name  = (data.get("child_name") or row["child_name"]).strip()
        client_name = (data.get("client_name") if "client_name" in data else row["client_name"])
        phone       = (data.get("phone")       if "phone"       in data else row["phone"])
        email       = (data.get("email")       if "email"       in data else row["email"])
        notes       = (data.get("notes")       if "notes"       in data else row["notes"])
        status      = (data.get("status")      if "status"      in data else row["status"])
        assigned_to = (data.get("assigned_to") if "assigned_to" in data else row["assigned_to"])
        if status not in ("pending", "done"):
            status = row["status"]

        conn.execute(
            """
            UPDATE special_orders
            SET child_name=?, client_name=?, phone=?, email=?, notes=?, status=?, assigned_to=?, updated_at=?
            WHERE id=?
            """,
            (child_name, client_name or None, phone or None, email or None,
             notes or None, status, assigned_to or None, now, order_id),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        photos  = [r["filename"] for r in conn.execute(
            "SELECT filename FROM special_order_photos WHERE order_id = ?", (order_id,)
        ).fetchall()]
        conn.close()
    return jsonify({"ok": True, "order": _order_to_dict(updated, photos)})


@app.route("/admin/api/special-orders/<int:order_id>", methods=["DELETE"])
@admin_required
def admin_delete_special_order(order_id: int):
    """Delete a special order and all its photos from disk."""
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT id FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        conn.execute("DELETE FROM special_order_photos WHERE order_id = ?", (order_id,))
        conn.execute("DELETE FROM special_orders WHERE id = ?", (order_id,))
        conn.commit()
        conn.close()
    # Remove entire order directory (photos + pdf)
    import shutil as _shutil
    _shutil.rmtree(SPECIAL_ORDERS_DIR / str(order_id), ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/admin/api/special-orders/<int:order_id>/photos", methods=["POST"])
@admin_required
def admin_upload_order_photos(order_id: int):
    """Upload one or more photos for an order (multipart/form-data, field: photos)."""
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT id FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        existing_count = conn.execute(
            "SELECT COUNT(*) AS c FROM special_order_photos WHERE order_id = ?", (order_id,)
        ).fetchone()["c"]
        conn.close()
    if not row:
        return jsonify({"error": "الطلب مش موجود."}), 404
    if existing_count >= MAX_PHOTOS_PER_ORDER:
        return jsonify({"error": f"أقصى عدد صور هو {MAX_PHOTOS_PER_ORDER}."}), 400

    files = request.files.getlist("photos")
    if not files:
        return jsonify({"error": "مفيش صور مرفوعة."}), 400

    slots_left = MAX_PHOTOS_PER_ORDER - existing_count
    saved = []
    photo_dir = _order_photo_dir(order_id)

    for f in files[:slots_left]:
        if not f or not f.filename:
            continue
        safe_name = _safe_photo_name(f.filename)
        if not safe_name:
            continue
        dest = photo_dir / safe_name
        f.seek(0, 2)
        size = f.tell()
        f.seek(0)
        if size > MAX_PHOTO_SIZE:
            continue
        try:
            img = Image.open(f.stream).convert("RGB")
            # Cap at 1600px
            if max(img.size) > 1600:
                img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            # Save as JPEG regardless of original format with max quality
            jpeg_name = safe_name.rsplit(".", 1)[0] + ".jpg"
            img.save(photo_dir / jpeg_name, "JPEG", quality=95)
            saved.append(jpeg_name)
        except Exception:
            continue

    if not saved:
        return jsonify({"error": "مفيش صور صالحة تم رفعها."}), 400

    with _db_lock:
        conn = db_connect()
        for name in saved:
            conn.execute(
                "INSERT INTO special_order_photos (order_id, filename) VALUES (?, ?)",
                (order_id, name),
            )
        conn.commit()
        conn.close()

    return jsonify({"ok": True, "uploaded": saved}), 201


@app.route("/admin/special-orders/<int:order_id>/photo/<filename>")
@admin_required
def admin_serve_order_photo(order_id: int, filename: str):
    """Serve a single photo file for a special order."""
    # Sanitize filename — only alphanumeric + dot + underscore + hyphen
    if not re.match(r'^[a-zA-Z0-9_\-]+\.[a-zA-Z]+$', filename):
        abort(404)
    photo_path = SPECIAL_ORDERS_DIR / str(order_id) / filename
    if not photo_path.exists():
        abort(404)
    return send_file(photo_path)


@app.route("/admin/special-orders/<int:order_id>/book-page/<scene_id>")
@admin_required
def admin_serve_order_book_page(order_id: int, scene_id: str):
    """Serve a permanently saved book page (cover / ending / scene) for an order."""
    if not SCENE_ID_RE.match(scene_id):
        abort(400, "Bad scene")
    # Prefer permanent order store, fall back to live session
    p = _order_book_page_path(order_id, scene_id)
    if not p.exists():
        with _db_lock:
            conn = db_connect()
            row = conn.execute(
                "SELECT book_session_id FROM special_orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            conn.close()
        sid = row["book_session_id"] if row and "book_session_id" in row.keys() else None
        if sid:
            sess_p = page_path(SESSIONS_DIR / str(sid), scene_id)
            if sess_p.exists():
                try:
                    shutil.copy2(sess_p, p)
                except OSError:
                    p = sess_p
    if not p.exists():
        abort(404, "Page not found")
    return send_file(p, mimetype="image/jpeg", max_age=30)


@app.route("/admin/api/special-orders/<int:order_id>/photo/<filename>", methods=["DELETE"])
@admin_required
def admin_delete_order_photo(order_id: int, filename: str):
    """Delete a single photo from a special order."""
    if not re.match(r'^[a-zA-Z0-9_\-]+\.[a-zA-Z]+$', filename):
        return jsonify({"error": "اسم الملف غير صالح."}), 400
    photo_path = SPECIAL_ORDERS_DIR / str(order_id) / filename
    with _db_lock:
        conn = db_connect()
        conn.execute(
            "DELETE FROM special_order_photos WHERE order_id = ? AND filename = ?",
            (order_id, filename),
        )
        conn.commit()
        conn.close()
    photo_path.unlink(missing_ok=True)
    return jsonify({"ok": True})


# ─── PDF endpoints ───────────────────────────────────────────────────────────

MAX_PDF_SIZE = 50 * 1024 * 1024  # 50 MB


@app.route("/admin/api/special-orders/<int:order_id>/pdf", methods=["POST"])
@admin_required
def admin_upload_order_pdf(order_id: int):
    """Upload (or replace) the PDF for a special order."""
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT id, pdf_filename FROM special_orders WHERE id = ?",
                           (order_id,)).fetchone()
        conn.close()
    if not row:
        return jsonify({"error": "الطلب مش موجود."}), 404

    f = request.files.get("pdf")
    if not f or not f.filename:
        return jsonify({"error": "مفيش ملف PDF مرفوع."}), 400

    ext = Path(f.filename).suffix.lower()
    if ext != ".pdf":
        return jsonify({"error": "الملف لازم يكون PDF."}), 400

    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > MAX_PDF_SIZE:
        return jsonify({"error": f"حجم الملف أكبر من {MAX_PDF_SIZE // 1024 // 1024} MB."}), 400

    pdf_dir  = _order_photo_dir(order_id)  # same directory as photos
    pdf_name = f"order_{order_id}_{secrets.token_hex(6)}.pdf"
    pdf_path = pdf_dir / pdf_name

    # Delete old PDF if exists
    old_pdf = row["pdf_filename"]
    if old_pdf:
        (pdf_dir / old_pdf).unlink(missing_ok=True)

    f.save(str(pdf_path))

    with _db_lock:
        conn = db_connect()
        conn.execute(
            "UPDATE special_orders SET pdf_filename=?, updated_at=? WHERE id=?",
            (pdf_name, datetime.now(timezone.utc).isoformat(), order_id),
        )
        conn.commit()
        conn.close()

    return jsonify({"ok": True, "pdf_filename": pdf_name}), 201


@app.route("/admin/special-orders/<int:order_id>/pdf/<filename>")
@admin_required
def admin_serve_order_pdf(order_id: int, filename: str):
    """Serve the PDF file for download."""
    if not re.match(r'^[a-zA-Z0-9_\-]+\.pdf$', filename):
        abort(404)
    pdf_path = SPECIAL_ORDERS_DIR / str(order_id) / filename
    if not pdf_path.exists():
        abort(404)
    return send_file(pdf_path, as_attachment=True, download_name=filename)


@app.route("/admin/api/special-orders/<int:order_id>/pdf", methods=["DELETE"])
@admin_required
def admin_delete_order_pdf(order_id: int):
    """Delete the PDF attached to a special order."""
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT pdf_filename FROM special_orders WHERE id=?",
                           (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        old_pdf = row["pdf_filename"]
        conn.execute(
            "UPDATE special_orders SET pdf_filename=NULL, share_token=NULL, "
            "share_expires_at=NULL, updated_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), order_id),
        )
        conn.commit()
        conn.close()
    if old_pdf:
        (SPECIAL_ORDERS_DIR / str(order_id) / old_pdf).unlink(missing_ok=True)
    return jsonify({"ok": True})


# ─── Public share links for special-order PDFs ───────────────────────────────

ORDER_SHARE_DAYS = int(os.environ.get("ORDER_SHARE_DAYS", "7"))


@app.route("/admin/api/special-orders/<int:order_id>/share", methods=["POST"])
@admin_required
def admin_create_order_share(order_id: int):
    """Create (or regenerate) a public share link for the order's PDF."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=ORDER_SHARE_DAYS)
    token = secrets.token_hex(16)
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT id, pdf_filename FROM special_orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        if not row["pdf_filename"]:
            conn.close()
            return jsonify({"error": "ارفع ملف PDF الأول قبل إنشاء رابط مشاركة."}), 400
        conn.execute(
            "UPDATE special_orders SET share_token=?, share_expires_at=?, updated_at=? WHERE id=?",
            (token, expires.isoformat(), now.isoformat(), order_id),
        )
        conn.commit()
        conn.close()
    return jsonify({
        "ok": True,
        "url": f"{app_base_url()}/so/{token}",
        "token": token,
        "expires_at": expires.isoformat(),
    }), 201


@app.route("/admin/api/special-orders/<int:order_id>/share", methods=["DELETE"])
@admin_required
def admin_revoke_order_share(order_id: int):
    """Revoke the public share link of an order."""
    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT id FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        conn.execute(
            "UPDATE special_orders SET share_token=NULL, share_expires_at=NULL, updated_at=? WHERE id=?",
            (now, order_id),
        )
        conn.commit()
        conn.close()
    return jsonify({"ok": True})


def _norm_order_phone(raw: str) -> str:
    """Normalize order phone for sibling matching (Egypt-friendly)."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0020"):
        digits = digits[2:]
    if digits.startswith("20") and len(digits) > 11:
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else digits


@app.route("/so/<token>")
@limiter.limit("60 per hour")
def get_order_share(token: str):
    """Public (no-auth) branded share page: view the order's PDF + siblings' PDFs."""
    if not SHARE_ID_RE.match(token):
        abort(404)
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT id, child_name, phone, pdf_filename, share_expires_at "
            "FROM special_orders WHERE share_token = ?",
            (token,),
        ).fetchone()
        if not row or not row["pdf_filename"]:
            conn.close()
            abort(404)
        # Siblings: orders for the same parent phone that have a PDF ready
        phone_key = _norm_order_phone(row["phone"] or "")
        siblings = []
        if phone_key:
            candidates = conn.execute(
                "SELECT id, child_name, phone FROM special_orders "
                "WHERE pdf_filename IS NOT NULL ORDER BY id"
            ).fetchall()
            siblings = [r for r in candidates if _norm_order_phone(r["phone"] or "") == phone_key]
        if not any(r["id"] == row["id"] for r in siblings):
            siblings.append(row)
        conn.close()

    if not _order_share_active(token, row["share_expires_at"]):
        return render_template("order_share.html", expired=True), 410

    exp = datetime.fromisoformat(row["share_expires_at"])
    kids = [{"id": r["id"], "name": (r["child_name"] or "طفلي")} for r in siblings]
    return render_template(
        "order_share.html",
        expired=False,
        token=token,
        kids=kids,
        main_id=row["id"],
        expires_label=exp.strftime("%Y/%m/%d"),
    )


@app.route("/so/<token>/pdf/<int:order_id>")
@limiter.limit("120 per hour")
def get_order_share_pdf(token: str, order_id: int):
    """Serve a PDF inline via share token — token order or a sibling (same phone)."""
    if not SHARE_ID_RE.match(token):
        abort(404)
    with _db_lock:
        conn = db_connect()
        token_row = conn.execute(
            "SELECT id, phone, pdf_filename, share_expires_at "
            "FROM special_orders WHERE share_token = ?",
            (token,),
        ).fetchone()
        if not token_row or not token_row["pdf_filename"]:
            conn.close()
            abort(404)
        target = conn.execute(
            "SELECT id, child_name, phone, pdf_filename FROM special_orders "
            "WHERE id = ? AND pdf_filename IS NOT NULL",
            (order_id,),
        ).fetchone()
        conn.close()
    if not target:
        abort(404)
    # Target must be the token's own order or a sibling (same parent phone)
    token_phone = _norm_order_phone(token_row["phone"] or "")
    target_phone = _norm_order_phone(target["phone"] or "")
    same_family = (target["id"] == token_row["id"]) or (
        token_phone and token_phone == target_phone
    )
    if not same_family:
        abort(404)
    if not _order_share_active(token, token_row["share_expires_at"]):
        abort(410)
    pdf_path = SPECIAL_ORDERS_DIR / str(target["id"]) / target["pdf_filename"]
    if not pdf_path.exists():
        abort(404)
    child = (target["child_name"] or "child").strip() or "child"
    # Inline so the parent can view the design directly in the browser
    return send_file(
        pdf_path,
        mimetype="application/pdf",
        download_name=f"{child}-coloring-book.pdf",
    )


if __name__ == "__main__":
    if not ACCOUNT_ID or not API_TOKEN:
        raise SystemExit("Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN env vars.")
    app.run(host="127.0.0.1", port=5000, debug=False)
