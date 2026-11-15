import html, json, os, re, sys, time
from calendar import timegm
import feedparser, requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@iransondakikanews")
MAX_POSTS = 15          # her çalışmada en fazla kaç haber
MAX_AGE_H = 6           # bundan eski haberleri atla
SEEN_FILE = "seen.json"

# filter=True: sadece İran ile ilgili haberleri paylaşır (genel akışlar için)
SOURCES = [
    {"name": "TRT Haber",         "url": "https://www.trthaber.com/sondakika.rss",                      "lang": "tr", "filter": True},
    {"name": "Anadolu Ajansı",    "url": "https://www.aa.com.tr/tr/rss/default?cat=guncel",             "lang": "tr", "filter": True},
    {"name": "BBC News",          "url": "http://feeds.bbci.co.uk/news/world/middle_east/rss.xml",      "lang": "en", "filter": True},
    {"name": "Al Jazeera",        "url": "https://www.aljazeera.com/xml/rss/all.xml",                   "lang": "en", "filter": True},
    {"name": "Middle East Eye",   "url": "https://www.middleeasteye.net/rss",                           "lang": "en", "filter": True},
    {"name": "Tehran Times",      "url": "https://www.tehrantimes.com/rss",                             "lang": "en", "filter": False},
    {"name": "BBC Arabic",        "url": "https://feeds.bbci.co.uk/arabic/rss.xml",                     "lang": "ar", "filter": True},
    {"name": "Al Jazeera Arabic", "url": "https://www.aljazeera.net/aljazeerarss/a7c186be-1baa-4bd4-9d80-a84db769f779/73d0e1b4-532f-45ef-b135-bfdb0dd35ab5", "lang": "ar", "filter": True},
]

# Konu -> anahtar kelimeler (Türkçe karakterler sadeleştirilerek karşılaştırılır)
TOPICS = {
    "İran":    ["iran", "tahran", "tehran", "hamenei", "khamenei", "irgc", "devrim muhafiz",
                "revolutionary guard", "pezeshkian", "pezeskiyan", "araghchi", "arakci",
                "إيران", "ايران", "الإيراني", "الايراني", "طهران", "خامنئي", "الحرس الثوري"],
    "Filistin": ["filistin", "gazze", "gaza", "hamas", "palestin", "west bank", "bati serit",
                 "kudus", "jerusalem", "rafah",
                 "فلسطين", "غزة", "حماس", "الضفة", "القدس", "رفح"],
    "Yemen":   ["yemen", "husi", "houthi", "sanaa", "kizildeniz", "red sea",
                "اليمن", "الحوثي", "صنعاء", "البحر الأحمر"],
    "Lübnan":  ["lubnan", "lebanon", "hizbullah", "hezbollah", "beyrut", "beirut",
                "لبنان", "حزب الله", "بيروت"],
    "Suriye":  ["suriye", "syria", "damascus", "dimask", "سوريا", "دمشق"],
    "Irak":    ["irak", "iraq", "baghdad", "bagdat", "العراق", "بغداد"],
    "İsrail":  ["israil", "israel", "netanyahu", "netanyahu", "israel defense",
                "إسرائيل", "اسرائيل", "نتنياهو", "الاحتلال"],
}

_TR = str.maketrans("İIıÜüÖöŞşÇçĞğ", "iiiuuoossccgg")

def norm(s):
    return s.translate(_TR).lower()

TOPICS_N = {t: [norm(k) for k in kws] for t, kws in TOPICS.items()}

def find_topics(text):
    t = norm(text)
    return [name for name, kws in TOPICS_N.items() if any(k in t for k in kws)]

def clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()

def _gtx(text, lang):
    r = requests.get("https://translate.googleapis.com/translate_a/single",
                     params={"client": "gtx", "sl": "auto", "tl": "tr", "dt": "t", "q": text},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    return "".join(p[0] for p in r.json()[0] if p and p[0])

def _deep_google(text, lang):
    from deep_translator import GoogleTranslator
    return GoogleTranslator(source="auto", target="tr").translate(text)

def _mymemory(text, lang):
    from deep_translator import MyMemoryTranslator
    return MyMemoryTranslator(source=lang, target="tr").translate(text)

def translate(text, lang):
    """Türkçeye çevirir. Hiçbir yöntem çalışmazsa None döner (İngilizce göndermemek için)."""
    if not text or lang == "tr":
        return text
    for fn in (_gtx, _deep_google, _mymemory):
        try:
            out = (fn(text[:1500], lang) or "").strip()
            if out:
                return out
        except Exception as e:
            print("çeviri hatası", fn.__name__, e)
            time.sleep(1)
    return None

def send(text):
    for _ in range(3):
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
            "chat_id": CHANNEL, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True}, timeout=30)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not r.ok:
            print("Telegram hatası:", r.status_code, r.text[:200])
        return r.ok
    return False

def load_seen():
    try:
        return json.load(open(SEEN_FILE, encoding="utf-8"))
    except Exception:
        return None

def main():
    seen = load_seen()
    first_run = seen is None
    seen = seen or []
    seen_set = set(seen)
    now = time.time()
    items = []
    for src in SOURCES:
        try:
            resp = requests.get(src["url"], timeout=25, headers={"User-Agent": "Mozilla/5.0 (haber-botu)"})
            feed = feedparser.parse(resp.content)
            if not feed.entries:
                print("boş/okunamadı:", src["name"])
        except Exception as e:
            print("akış hatası:", src["name"], e)
            continue
        for e in feed.entries:
            uid = e.get("id") or e.get("link")
            if not uid:
                continue
            ts = timegm(e.published_parsed) if e.get("published_parsed") else now
            items.append((ts, uid, src, e))

    if first_run:  # ilk çalışmada eski haberleri yığmadan sadece kaydet
        json.dump([i[1] for i in items][-3000:], open(SEEN_FILE, "w", encoding="utf-8"))
        print("ilk çalışma: mevcut haberler kaydedildi, paylaşım yok")
        return

    items.sort(key=lambda x: x[0])
    posted = 0
    for ts, uid, src, e in items:
        if uid in seen_set:
            continue
        title = clean(e.get("title"))
        summary = clean(e.get("summary") or e.get("description"))
        if now - ts > MAX_AGE_H * 3600:
            seen.append(uid); seen_set.add(uid); continue
        topics = find_topics(title + " " + summary)
        if src["filter"] and not topics:
            seen.append(uid); seen_set.add(uid); continue
        if posted >= MAX_POSTS:
            break
        if summary.startswith(title):
            summary = summary[len(title):].strip()
        t_title = translate(title, src["lang"])
        if not t_title:
            print("çevrilemedi, atlandı:", title[:60])
            continue
        t_sum = translate(summary[:350], src["lang"]) if summary else ""
        if t_sum is None:
            t_sum = ""
        msg = f"<b>{html.escape(t_title)}</b>"
        if t_sum:
            msg += f"\n\n{html.escape(t_sum)}"
        msg += f'\n\n📰 Kaynak: <a href="{html.escape(e.get("link", ""), quote=True)}">{html.escape(src["name"])}</a>'
        if topics:
            msg += "\n🏷 " + " ".join("#" + t.replace(" ", "_") for t in topics)
        if send(msg):
            posted += 1
            seen.append(uid); seen_set.add(uid)
            time.sleep(3)
    json.dump(seen[-3000:], open(SEEN_FILE, "w", encoding="utf-8"))
    print("paylaşılan:", posted)

if __name__ == "__main__":
    main()
