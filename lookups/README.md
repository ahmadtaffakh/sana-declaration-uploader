# Verified SANA lookup catalogs

این پوشه برای نگاشت نام‌های قابل‌فهم اپراتور به شناسه‌ها و کدهای تأییدشدهٔ
سامانهٔ ثنا است. تا وقتی مقدار از رفتار واقعی سامانه تأیید نشده، نباید در این
فایل‌ها ثبت شود.

فایل `jobs.json` آرایه‌ای با ساختار زیر است:

```json
[
  {"key": "stable-local-key", "label": "عنوان نمایشی", "id": "SITE_ID", "code": "SITE_CODE"}
]
```

فایل `locations.json` استان، شهرستان، شهر و روستا را به‌صورت سلسله‌مراتبی
نگه می‌دارد:

```json
[
  {"key": "province-key", "label": "نام استان", "type": "province", "parent_key": "", "id": "SITE_ID", "code": "SITE_CODE"},
  {"key": "county-key", "label": "نام شهرستان", "type": "county", "parent_key": "province-key", "id": "SITE_ID", "code": "SITE_CODE"}
]
```

مقادیر مجاز `type` عبارت‌اند از `province`، `county`، `city` و `village`.

فایل `court_units.json`:

```json
[
  {"key": "court-key", "label": "نام واحد قضایی", "code": "SITE_CODE"}
]
```

تطبیق fuzzy یا انتخاب اولین نتیجه مجاز نیست. نام مبهم، والد مفقود، کد متناقض
یا فایل ناقص باعث توقف پردازش می‌شود.
