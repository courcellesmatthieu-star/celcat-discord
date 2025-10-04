# Envoi quotidien de l'emploi du temps CELCAT par email pour "demain".
# - Vise "demain" (Europe/Paris), force ?dt=YYYY-MM-DD sur l'URL listWeek
# - Parse la page listWeek (Playwright) en blocs : horaire -> (titre, enseignants, salle, type)
# - Compose un email résumant les événements du lendemain avec un lien vers CELCAT

import os, re, asyncio, datetime as dt, smtplib
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from dateutil.tz import gettz
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from email.message import EmailMessage

# ------------ Config ------------
load_dotenv()
LIST_URL_TEMPLATE = os.getenv("CELCAT_LIST_URL")
TZ_NAME  = os.getenv("TZ_NAME", "Europe/Paris")
TZ = gettz(TZ_NAME)

EMAIL_SMTP_HOST = os.getenv("EMAIL_SMTP_HOST")
EMAIL_SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", "587"))
EMAIL_USERNAME  = os.getenv("EMAIL_USERNAME")
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD")
EMAIL_FROM      = os.getenv("EMAIL_FROM")
EMAIL_TO        = [addr.strip() for addr in os.getenv("EMAIL_TO", "").split(",") if addr.strip()]
EMAIL_USE_TLS   = os.getenv("EMAIL_USE_TLS", "true").lower() in ("1", "true", "yes", "on")

assert LIST_URL_TEMPLATE, "Config manquante: CELCAT_LIST_URL"
assert EMAIL_SMTP_HOST and EMAIL_FROM and EMAIL_TO, "Config manquante: EMAIL_SMTP_HOST / EMAIL_FROM / EMAIL_TO"

# ------------ Dates / FR ------------
JOURS_FR = ["lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche"]
MOIS_FR  = ["janvier","février","mars","avril","mai","juin","juillet","août","septembre","octobre","novembre","décembre"]

def now_paris() -> dt.datetime:
    return dt.datetime.now(TZ)

def french_date(d: dt.date, capitalize_first: bool = True) -> str:
    jour = JOURS_FR[d.weekday()]
    mois = MOIS_FR[d.month - 1]
    s = f"{jour} {d.day} {mois} {d.year}"
    return s[:1].upper() + s[1:] if capitalize_first else s

def week_url_for(date_obj: dt.date) -> str:
    parsed = urlparse(LIST_URL_TEMPLATE)
    q = parse_qs(parsed.query, keep_blank_values=True)
    q["dt"] = [date_obj.strftime("%Y-%m-%d")]
    new_query = urlencode(q, doseq=True)
    return urlunparse(parsed._replace(query=new_query))

# ---- Garde-fou horaire: ne poste qu'à POST_AT_HOUR (default 20:00) Europe/Paris ----
def should_post_now(now_dt: dt.datetime) -> bool:
    target_hour = int(os.getenv("POST_AT_HOUR", "20"))
    return now_dt.hour == target_hour
# ------------ Parsing heuristique ------------
MONTHS = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,"july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
    "janvier":1,"février":2,"fevrier":2,"mars":3,"avril":4,"mai":5,"juin":6,"juillet":7,"août":8,"aout":8,"septembre":9,"octobre":10,"novembre":11,"décembre":12,"decembre":12
}
DATE_FULL_RE  = re.compile(r"(?i)^\s*(\d{1,2})\s+([A-Za-zéèêëàâîïôöûüç]+)\s+(\d{4})\s*$")
TIME_RANGE_RE = re.compile(r"(?i)\b(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})\b")
CUVIER_ROOMS_RE = re.compile(r"(?i)CUVIER[\s\u00A0\-–—-][^,;|\n]+")
WEEKDAY_RE = re.compile(r"(?i)^(monday|tuesday|wednesday|thursday|friday|saturday|sunday|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s*$")
NAME_WORD_RE = re.compile(r"^[A-ZÉÈÀÂÇÎÏÔÛÜ][A-Za-zÉÈÀÂÇÎÏÔÛÜéèàâçïîôöûü'’\-]{2,}$")

def parse_date_full(line: str) -> dt.date | None:
    m = DATE_FULL_RE.match(line.strip()); 
    if not m: return None
    day, month_txt, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    month = MONTHS.get(month_txt); 
    if not month: return None
    try: return dt.date(year, month, day)
    except ValueError: return None

def looks_like_names(line: str) -> bool:
    if "," not in line: return False
    tokens = [t.strip() for t in re.split(r"[,\s]+", line) if t.strip()]
    caps_like = sum(1 for t in tokens if NAME_WORD_RE.match(t))
    return caps_like >= 2

def is_people_list(line: str) -> bool:
    return looks_like_names(line)

def is_group_codes(line: str) -> bool:
    return bool(re.search(r"\b(M|L)\d\b|\bUE\b|\bGP\b|\bM1\b|\bM2\b", line))

def is_weekday_header(line: str) -> bool:
    return bool(WEEKDAY_RE.match(line.strip()))

def extract_room(lines: list[str]) -> str | None:
    rooms = []
    for s in lines:
        matches = CUVIER_ROOMS_RE.findall(s)
        if not matches and "CUVIER" in s.upper():
            # Fallback si la regex rate : coupe depuis 'CUVIER' jusqu'à la virgule/fin
            idx = s.upper().find("CUVIER")
            tail = s[idx:]
            cut = re.split(r"[,;|\n]", tail)[0]
            matches = [cut]
        for m in matches:
            v = m.strip().rstrip(" ,;|")
            if v and v.upper() not in (x.upper() for x in rooms):
                rooms.append(v)
    return ", ".join(rooms) if rooms else None


def extract_type(chunk: list[str]) -> str | None:
    for s in reversed(chunk):
        if re.match(r"(?i)^(type\s*:\s*)?réunion\b.*", s) or re.match(r"(?i)^type\s*:\s*\S+", s):
            return s
    return None

def extract_teachers(chunk: list[str]) -> str | None:
    for s in chunk:
        if is_people_list(s):
            return s
    return None

def choose_title(chunk: list[str], room: str | None, type_line: str | None) -> str:
    candidate = None
    for s in chunk:
        if is_weekday_header(s):
            continue
        if room and s.find("CUVIER-") != -1:
            continue
        if type_line and s == type_line:
            continue
        if is_people_list(s):
            continue
        if is_group_codes(s):
            candidate = candidate or s
            continue
        if len(s) <= 3:
            continue
        return s  # ligne descriptive
    return candidate or (chunk[0] if chunk else "Événement")

def parse_specific_day(full_text: str, target_date: dt.date):
    lines = [re.sub(r"\s+", " ", L).strip() for L in full_text.splitlines()]
    lines = [L for L in lines if L]

    current_date, events = None, []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]

        d = parse_date_full(line)
        if d: current_date = d; i += 1; continue

        m = TIME_RANGE_RE.search(line)
        if m and current_date is not None:
            start, end = m.group(1), m.group(2)
            # bloc événement

            after_time = line[m.end():].strip()
            room_inline = extract_room([after_time]) if after_time else None

            chunk, j = [], i + 1
            while j < n:
                nxt = lines[j]
                if TIME_RANGE_RE.search(nxt) or parse_date_full(nxt) or is_weekday_header(nxt):
                    break
                chunk.append(nxt); j += 1

            room = room_inline or extract_room(chunk)
            ev_type = extract_type(chunk)
            teachers = extract_teachers(chunk)
            title = choose_title(chunk, room, ev_type)

            events.append({
                "date": current_date, "start": start, "end": end,
                "title": title, "room": room, "teachers": teachers, "type": ev_type
            })
            i = j; continue

        i += 1

    todays = [e for e in events if e["date"] == target_date]
    todays.sort(key=lambda e: e["start"])
    return todays

# ------------ Récupération ------------
async def fetch_week_text(url: str) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle", timeout=15000)
        text = await page.locator("body").inner_text()
        await browser.close()
        return text

# ------------ Email ------------
def build_email_content(events: list[dict], day_label: str, week_url: str) -> tuple[str, str]:
    subject = f"Emploi du temps du {day_label}"

    lines = [
        "Bonjour,",
        "",
        f"Voici l'emploi du temps prévu pour {day_label} :",
        "",
    ]

    if not events:
        lines.append("- Aucun cours n'est prévu.")
    else:
        for event in events:
            lines.append(f"- {event['start']}–{event['end']} : {event['title']}")
            if event.get("room"):
                lines.append(f"  Salle : {event['room']}")
            if event.get("teachers"):
                lines.append(f"  Enseignants : {event['teachers']}")
            if event.get("type"):
                lines.append(f"  Type : {event['type']}")
            lines.append("")

    lines.extend([
        f"Détails complets : {week_url}",
        "",
        "Bonne journée !",
    ])

    body_text = "\n".join(lines)
    return subject, body_text


def send_email(subject: str, body_text: str):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = EMAIL_FROM
    message["To"] = ", ".join(EMAIL_TO)
    message.set_content(body_text)

    with smtplib.SMTP(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, timeout=30) as smtp:
        if EMAIL_USE_TLS:
            smtp.starttls()
        if EMAIL_USERNAME and EMAIL_PASSWORD:
            smtp.login(EMAIL_USERNAME, EMAIL_PASSWORD)
        smtp.send_message(message)

# ------------ Main ------------
async def main():
    now = now_paris()
    if not should_post_now(now):
        return
    tomorrow = (now + dt.timedelta(days=1)).date()
    week_url = week_url_for(tomorrow)
    full_text = await fetch_week_text(week_url)
    events = parse_specific_day(full_text, tomorrow)
    day_label = french_date(tomorrow, capitalize_first=True)
    subject, body_text = build_email_content(events, day_label, week_url)
    send_email(subject, body_text)

if __name__ == "__main__":
    asyncio.run(main())
