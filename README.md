# 🎨 مولّد كتاب التلوين للأطفال

تطبيق ويب بيحوّل صورة طفل لكتاب تلوين مخصّص PDF جاهز للطباعة، باستخدام **Cloudflare Workers AI** و موديل **FLUX.2 [klein] 4B** من Black Forest Labs.

المستخدم يرفع صورة، يختار المواقف اللي عايزها (شاطئ، حديقة، فضاء، عيد ميلاد، إلخ)، والتطبيق بيولّد صفحة تلوين لكل موقف مع الحفاظ على شكل الطفل، وبعدين بيجمعهم في PDF واحد.

---

## 📁 هيكل المشروع

```
coloring_web/
├── app.py                    # الباك-إند: Flask + Cloudflare Workers AI
├── templates/
│   └── index.html            # الواجهة الكاملة (HTML + CSS + JS)
├── README.md                 # هذا الملف
└── TASKS.md                  # قائمة المهام والتحسينات المستقبلية
```

**ملف إضافي خارج المشروع**: `~/Downloads/make_coloring_book.py` — نسخة CLI مستقلة.

---

## ⚙️ التشغيل

### المتطلبات
- Python 3.9+
- المكتبات: `flask`, `requests`, `Pillow` (كلها مثبتة عند المطور)

### متغيرات البيئة (Environment Variables)

```bash
export CLOUDFLARE_ACCOUNT_ID="407882dab0b5b017daa1dc6d3c2ba1e7"
export CLOUDFLARE_API_TOKEN="cfut_xxxxxxxxxxxxxxxxxxxxxxxx"
```

**⚠️ ملاحظة أمان**: التوكن الحالي مكشوف في تاريخ المحادثة — يفضل عمل **revoke** له من:
https://dash.cloudflare.com/profile/api-tokens
وإنشاء واحد جديد بصلاحية `Workers AI → Edit` فقط.

### تشغيل السيرفر

```bash
cd ~/Downloads/coloring_web
python3 app.py
```

بعدين افتح: **http://127.0.0.1:5000**

---

## 🧠 كيفية عمل التطبيق

### 1. رفع الصورة
`POST /upload` — بيرفع الصورة، بيحفظها في مجلد session مؤقت (`/tmp/coloring_sessions/<random_id>/input.png`)، وبيرجّع `session_id`.

### 2. توليد كل صفحة
`GET /generate/<session_id>/<scene_id>` — لكل موقف:
- بيرسل صورة الطفل كـ `input_image_0` (reference image)
- مع prompt يحدد الأسلوب والموقف
- بيسترجع صورة PNG/JPEG كـ base64
- بيخزنها لإعادة الاستخدام

### 3. تجميع الـ PDF
`GET /pdf/<session_id>?order=beach,garden,space` — بيجمّع الصفحات بالترتيب المطلوب في PDF واحد باستخدام Pillow.

### البرومبت المستخدم (Prompt Engineering)

**Style ثابت** (يضمن شكل صفحة التلوين + الحفاظ على هوية الطفل):
```
black and white line art coloring book page, clean bold outlines,
no shading, no color, no gray, pure white background,
simple children's coloring book illustration style,
keep the exact same child from image 0 — same face, same hairstyle, same age
```

**Scene ديناميكي** (يتغير مع كل موقف):
```
the child is playing with sand and buckets on a sunny beach with a beach ball and small waves
```

---

## 🎬 المواقف المتاحة (12 موقف)

كل موقف له gradient خاص متناسق مع الثيم:

| Emoji | العنوان | الخلفية |
|-------|---------|---------|
| 🏖️ | على الشاطئ | رمل ذهبي + ماء أزرق |
| 🌸 | في الحديقة | فراشات + زهور |
| 🚲 | على الدراجة | حديقة عامة |
| 📚 | بيقرأ كتاب | كتب مكدسة |
| 🐶 | مع كلب صغير | داخل غرفة |
| 🚀 | في الفضاء | رائد فضاء + كواكب |
| 🎂 | عيد ميلاد | تورتة + بالونات |
| 🌳 | تحت الشجرة | شجرة فواكه + طيور |
| 🎒 | في المدرسة | مبنى مدرسة + قلم |
| 🏊 | في المسبح | عوامة + مياه |
| 🧁 | بيطبخ | قبعة شيف + كب كيك |
| 🎵 | بيعزف موسيقى | جيتار + نوتات |

---

## 💰 التكلفة والحدود

### تسعير موديل `flux-2-klein-4b`
- **Input**: 5.37 نيورون لكل tile 512×512 (صورة الطفل المرفوعة)
- **Output**: 26.05 نيورون لكل tile 512×512
- **حجم الناتج المستخدم**: 1024×1024 = 4 tiles = ~104 نيورون
- **إجمالي الصفحة الواحدة**: ~110 نيورون

### الكوتة المجانية
**10,000 نيورون كل يوم** (بيتصفر الساعة 12:00 UTC)

| حجم الكتاب | نيورون | كتب مجانية / يوم |
|---|---|---|
| 6 صفحات | ~660 | **~15 كتاب** |
| 8 صفحات | ~880 | **~11 كتاب** |
| 12 صفحة | ~1,320 | **~7 كتب** |

### التكلفة بعد الكوتة المجانية
**$0.011 لكل 1,000 نيورون**

| العملية | التكلفة |
|---|---|
| كتاب 8 صفحات | ~$0.010 (سنت واحد) |
| كتاب 12 صفحة | ~$0.015 |
| 100 كتاب (8 صفحات) | ~$1 |
| 1000 كتاب | ~$10 |

**بشحنة $10** تقدر تعمل: **~1,033 كتاب (8 صفحات)** أو **~688 كتاب (12 صفحة)** — بالإضافة للـ 11 كتاب مجاني يوميًا من الكوتة.

---

## 🎨 نظام التصميم (Design System)

### الخطوط
- **Tajawal** من Google Fonts (أوزان: 400, 500, 700, 800, 900)

### الألوان الأساسية
```css
--primary:      #7c3aed  /* بنفسجي */
--accent:       #ec4899  /* وردي */
--success:      #10b981  /* أخضر */
--danger:       #ef4444  /* أحمر */
--bg:           #fdfaf6  /* كريمي دافئ */
--surface:      #ffffff
--text:         #1f1b24
--muted:        #7a7480
```

### الـ Gradient الرئيسي
```css
linear-gradient(135deg, #7c3aed 0%, #ec4899 60%, #f59e0b 100%)
```

### مبادئ التصميم
- Border radius كبير (16px) — شكل ودود
- Shadows ناعمة متعددة المستويات
- Transitions ناعمة (0.15-0.2s ease)
- Font weights متدرجة (800 للعناوين، 500 للنصوص)
- Antialiasing للنصوص العربية
- Responsive على الموبايل

---

## 🔗 روابط مفيدة

- **موديل flux-2-klein-4b**: https://developers.cloudflare.com/workers-ai/models/flux-2-klein-4b/
- **تسعير Workers AI**: https://developers.cloudflare.com/workers-ai/platform/pricing/
- **Cloudflare Dashboard**: https://dash.cloudflare.com/
- **إدارة API Tokens**: https://dash.cloudflare.com/profile/api-tokens
