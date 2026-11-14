"""Process teacher uploads in the background (run by .github/workflows/process-uploads.yml).

The upload page sends each photo, as is, to the private repo notebook-gallery-inbox:
  inbox/<hash>.jpg   the photo
  inbox/<hash>.json  what the teacher filled in (subject, topic, grade) and the time
For each waiting photo, one Gemini call decides whether it is a main-lesson-book page and, if so, where the
work is (crop box), how to rotate it, where student names are written, and how to catalog it.
Accepted: the cropped, rotated, name-blurred image goes to u/<id>.jpg (+ u/<id>_t.jpg thumbnail) in this
public repo and the catalog entry to uploads.json. The original moves to done/ in the private repo.
Rejected: the original moves to rejected/ (private) with the reason, so a wrong rejection can be rescued.
Gemini down: the photo stays in inbox/ and the next run tries again (after 4 failed runs it moves to failed/).

Usage: python process_uploads.py <public repo dir> <inbox repo dir>     env: GEMINI_KEY
To hide a published work: set "hidden": true on its entry in uploads.json.
"""
import base64, io, json, os, re, sys, time, urllib.error, urllib.request
from pathlib import Path

from PIL import Image, ImageOps

PUB, INBOX = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
KEY = os.environ.get('GEMINI_KEY', '')
DEADLINE = time.time() + 45 * 60

SUBJECTS = {
    'torah': 'תורה', 'navi': 'נביא', 'moadim': 'מועדים', 'mishna': 'משנה', 'math': 'חשבון', 'lang': 'שפה',
    'handasa': 'הנדסה', 'madaim': 'מדעים', 'geo': 'גיאוגרפיה', 'crafts': 'בעלי מלאכה', 'eretz': 'ארץ ישראל', 'other': 'עוד עבודות'}
PARASHOT = {
    'בראשית': ['בראשית', 'נח', 'לך לך', 'וירא', 'חיי שרה', 'תולדות', 'ויצא', 'וישלח', 'וישב', 'מקץ', 'ויגש', 'ויחי'],
    'שמות': ['שמות', 'וארא', 'בא', 'בשלח', 'יתרו', 'משפטים', 'תרומה', 'תצוה', 'כי תשא', 'ויקהל', 'פקודי'],
    'ויקרא': ['ויקרא', 'צו', 'שמיני', 'תזריע', 'מצורע', 'אחרי מות', 'קדושים', 'אמור', 'בהר', 'בחוקותי'],
    'במדבר': ['במדבר', 'נשא', 'בהעלותך', 'שלח', 'קרח', 'חקת', 'בלק', 'פינחס', 'מטות', 'מסעי'],
    'דברים': ['דברים', 'ואתחנן', 'עקב', 'ראה', 'שופטים', 'כי תצא', 'כי תבוא', 'נצבים', 'וילך', 'האזינו', 'וזאת הברכה']}

TAXONOMY = """
מקצועות (slug = שם):
torah = תורה. book = שם החומש (בראשית / שמות / ויקרא / במדבר / דברים). section = שם הפרשה בדיוק כמו ברשימה:
""" + '\n'.join(f'  {b}: ' + ', '.join(ps) for b, ps in PARASHOT.items()) + """
navi = נביא. book = שם הספר (יהושע / שופטים / שמואל / מלכים ...). section = פרק או נושא קצר, או ריק.
moadim = מועדים. book = שם החג (ראש השנה / יום כיפור / סוכות ושמיני עצרת / חנוכה / ט"ו בשבט / פורים / פסח / ל"ג בעומר / שבועות / יום העצמאות / שבת).
mishna = משנה (משניות, אגדות חז"ל, פרקי אבות). book = שם המסכת או ריק.
math = חשבון. book = אחד הקיימים אם מתאים: חוכמת המספרים, מספרים 1–10, מספרים עד 100, חיבור וחיסור עד 100, ארבע הפעולות, לוח הכפל, כפל, חילוק, ערך המקום, שברים, הזמן ולוח השנה, מעגל המספרים, סדר פעולות, עיגול מספרים. section = נושא משנה קצר או ריק.
lang = שפה (אותיות, קריאה וכתיבה, דקדוק, שירים וטקסטים). book = אותיות / קריאה ראשונה / דקדוק / חיבור, או אחר קצר.
handasa = הנדסה (רישום צורות, גאומטריה). madaim = מדעים (טבע, בעלי חיים, צמחים, גוף האדם, פיזיקה).
geo = גיאוגרפיה (מפות, נופים). crafts = בעלי מלאכה. eretz = ארץ ישראל (שבעת המינים, חקלאות).
other = רק אם באמת לא מתאים לאף מקצוע.
שכבה (grade): אות אחת מ-א עד ח, או ריק אם לא ידוע."""

STYLE = r"""דוגמאות לסגנון הקטלוג הקיים:
{"subject":"torah","book":"בראשית","section":"נח","title":"התיבה על המים","desc":"התיבה בתוך אור זהוב, ומסביבה \"ויהי המבול ארבעים יום על הארץ... וישאו את התבה ותרם מעל הארץ\".","ref":"בראשית ז, יז"}
{"subject":"torah","book":"שמות","section":"תרומה","title":"\"ונועדתי לך שם\"","desc":"ציור לוח של ארון הברית ושני הכרובים, פרושי כנפיים, בתוך הילה של אור.","ref":"שמות כה, כב"}
{"subject":"math","book":"חוכמת המספרים","section":"חיבור וחיסור שברים","grade":"ד","title":"חיבור וחיסור במכנים שונים","desc":"שני תרגילים עם חיצי הרחבה (×2) וכלל במסגרת: כדי לחבר או לחסר שברים במכנים שונים מרחיבים אותם תחילה למכנה משותף.","ref":""}
{"subject":"lang","book":"קריאה ראשונה","grade":"א","title":"יחיד ורבים","desc":"שתי מילים, \"יחיד\" ו\"רבים\": פרח אחד מצד אחד, ושלושה פרחים מהצד השני.","ref":""}"""

SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'is_notebook_work': {'type': 'BOOLEAN'},
        'reason': {'type': 'STRING'},
        'box': {'type': 'ARRAY', 'items': {'type': 'INTEGER'}},
        'rotate': {'type': 'INTEGER'},
        'names': {'type': 'ARRAY', 'items': {'type': 'ARRAY', 'items': {'type': 'INTEGER'}}},
        'subject': {'type': 'STRING', 'description': 'אחד מ: ' + ', '.join(SUBJECTS)},
        'book': {'type': 'STRING'}, 'section': {'type': 'STRING'}, 'grade': {'type': 'STRING'},
        'title': {'type': 'STRING'}, 'desc': {'type': 'STRING'}, 'ref': {'type': 'STRING'}},
    'required': ['is_notebook_work']}


def prompt(meta):
    hints = []
    if meta.get('subject') in SUBJECTS:
        hints.append(f"המקצוע (נקבע בידי המורה, להשתמש בו): {meta['subject']} = {SUBJECTS[meta['subject']]}")
    if meta.get('topic'):
        hints.append(f"הנושא שכתב המורה (לנרמל לשמות שברשימה, למשל שם חומש ופרשה): {str(meta['topic'])[:120]}")
    if meta.get('grade'):
        hints.append(f"השכבה (נקבעה בידי המורה, להשתמש בה): {meta['grade']}")
    return (
        'אתה מקטלג עבודות מחברת (מחברות תקופה בגישה הוולדורפית) עבור גלריה למורים בבית ספר דתי. התמונה צולמה בטלפון.\n'
        '1. בדוק אם זו עבודת מחברת: עמוד (או שני עמודים) ממחברת של תלמיד, עם ציור, כתב, תרגילים או רישום צורות. '
        'תמונה של משהו אחר (אנשים, נוף, צילום מסך, מסמך או דף עבודה מודפס, מחברת ריקה, תמונה מטושטשת) אינה עבודת מחברת: '
        'is_notebook_work=false, reason=משפט קצר בעברית מה רואים, ושאר השדות ריקים.\n'
        '2. אם זו עבודת מחברת:\n'
        'box: מלבן העבודה עצמה (הדף או הדפים, בלי השולחן והרקע), [ymin,xmin,ymax,xmax] בסקלה 0–1000 של התמונה כפי שהיא.\n'
        'rotate: בכמה מעלות עם כיוון השעון לסובב את התמונה כדי שהכתב והציור יעמדו ישר (0, 90, 180 או 270). שים לב לכיוון הכתב העברי.\n'
        'names: מלבנים של שמות תלמידים שכתובים בעבודה, באותה סקלה. רשימה ריקה אם אין.\n'
        'title: כותרת קצרה (2–6 מילים). אם יש פסוק מרכזי אפשר לצטט אותו במירכאות.\n'
        'desc: משפט או שניים שמתארים מה רואים בציור ובכתוב, בעברית פשוטה ומדויקת. לא להמציא.\n'
        'ref: מקור הפסוק או הקטע (למשל "בראשית ז, יז"), רק אם מזוהה בביטחון. אחרת ריק.\n'
        'subject/book/section/grade: לפי הרשימה למטה, כל שדה רק הערך עצמו. אם לא בטוח, ריק.\n'
        'לא לציין שם של תלמיד בשום שדה. כל שדה טקסט בעברית בלבד, בלי הערות.\n'
        + (('פרטים שמילא המורה:\n' + '\n'.join(hints) + '\n') if hints else 'המורה לא מילא פרטים: קבע לפי תוכן העבודה.\n')
        + TAXONOMY + '\n' + STYLE)


# ---------- Gemini ----------

_cool = {}      # model -> time it may be tried again (this run only)
_models = []


def http(url, body=None, timeout=150):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'x-goog-api-key': KEY, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors='replace')
    except Exception as e:            # timeout, connection reset
        return 504, str(e)


def models():
    """Flash models this key can call. Proven on the free tier (11/2026): 3.5-flash, then 3.5-flash-lite."""
    global _models
    if not _models:
        code, txt = http('https://generativelanguage.googleapis.com/v1beta/models?pageSize=200', timeout=30)
        names = []
        if code == 200:
            import re
            names = [m['name'].replace('models/', '') for m in json.loads(txt).get('models', [])
                     if 'generateContent' in m.get('supportedGenerationMethods', [])]
            names = [n for n in names if re.fullmatch(r'gemini-[\d.]+-flash(-lite)?', n)]
        pref = ['gemini-3.5-flash', 'gemini-3.5-flash-lite']
        ver = lambda n: float(n.split('-')[1]) if n.split('-')[1].replace('.', '', 1).isdigit() else 0
        names.sort(key=lambda n: (pref.index(n) if n in pref else 10, 'lite' in n, -ver(n)))
        _models = names[:6] or pref
    return _models


def gemini(parts):
    body = {'contents': [{'role': 'user', 'parts': parts}],
            'generationConfig': {'temperature': 0.2, 'responseMimeType': 'application/json', 'responseSchema': SCHEMA}}
    last = ''
    for rnd in range(3):
        fresh = [m for m in models() if _cool.get(m, 0) < time.time()]
        for m in fresh or models():
            if time.time() > DEADLINE:
                break
            t0 = time.time()
            think = {'thinkingBudget': 0} if m.startswith('gemini-2.') else {'thinkingLevel': 'minimal'}
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent'
            code, last = http(url, {**body, 'generationConfig': {**body['generationConfig'], 'thinkingConfig': think}})
            if code == 400:
                code, last = http(url, body)
            print(f'  {m}: {code} in {time.time() - t0:.0f}s', flush=True)
            if code == 200:
                try:
                    j = json.loads(last)
                    text = ''.join(p.get('text', '') for p in j['candidates'][0]['content']['parts'])
                    if text:
                        return json.loads(text)
                except Exception:
                    pass
            elif code in (403, 404):
                _cool[m] = float('inf')                 # not offered to this key
            elif code == 429 or code >= 500:
                _cool[m] = time.time() + 10 * 60        # busy
        time.sleep(20 * (rnd + 1))
    raise RuntimeError('Gemini failed: ' + last[:300])


# ---------- catalog cleanup ----------

def short(v, mx):
    import re
    s = re.sub(r'[A-Za-z_]{3,}[A-Za-z_\-/.]*', ' ', str(v or ''))
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r'^[\s,.;:–-]+|[\s,;:–-]+$', '', s)
    if len(s) > mx:
        s = re.sub(r'\s+\S*$', '', s[:mx]) + '…'
    return s


def normalize(r, meta):
    import re
    subject = meta.get('subject') if meta.get('subject') in SUBJECTS else (r.get('subject') if r.get('subject') in SUBJECTS else 'other')
    book, section = short(r.get('book'), 40), short(r.get('section'), 40)
    if subject == 'torah':
        b = next((k for k in PARASHOT if k in book), '') or next((k for k in PARASHOT if any(x in section for x in PARASHOT[k])), '')
        by_len = sorted(PARASHOT.get(b, []), key=len, reverse=True)
        sec = re.sub(r'^פרשת\s+', '', section)
        p = next((x for x in by_len if sec == x or sec.startswith(x + ' ')), '') or next((x for x in by_len if x in sec), '')
        book, section = b, p
    if section == book and subject != 'torah':
        section = ''
    g = re.sub(r'[^א-ח]', '', str(meta.get('grade') or r.get('grade') or ''))[:1]
    return {'s': subject, 'b': book, 'p': section, 'g': g,
            'title': short(r.get('title'), 80), 'desc': short(r.get('desc'), 420), 'ref': short(r.get('ref'), 40)}


# ---------- images ----------

def jpeg(im, q, max_side):
    im = im.copy()
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=q, optimize=True, progressive=True)
    return buf.getvalue(), im.size


def pixelate(im, x0, y0, x1, y1):
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(im.width, int(x1)), min(im.height, int(y1))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return
    reg = im.crop((x0, y0, x1, y1))
    small = reg.resize((max(2, round((x1 - x0) / 14)), max(2, round((y1 - y0) / 14))), Image.BILINEAR)
    im.paste(small.resize(reg.size, Image.NEAREST), (x0, y0))


def finish(im, r):
    """Crop to the work, blur names, rotate upright, scale to at most 1600px."""
    W, H = im.size
    box = r.get('box') if isinstance(r.get('box'), list) and len(r['box']) == 4 else [0, 0, 1000, 1000]
    y0, x0, y1, x1 = [min(1000, max(0, v)) / 1000 for v in box]
    if x1 <= x0 or y1 <= y0 or (x1 - x0) * (y1 - y0) < .08:
        x0, y0, x1, y1 = 0, 0, 1, 1
    pad = .015
    x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(1, x1 + pad), min(1, y1 + pad)
    cx, cy = round(x0 * W), round(y0 * H)
    c = im.crop((cx, cy, round(x1 * W), round(y1 * H)))
    cw = c.width
    c.thumbnail((1600, 1600), Image.LANCZOS)
    s = c.width / cw
    for n in r.get('names') or []:
        if not isinstance(n, list) or len(n) != 4:
            continue
        ny0, nx0, ny1, nx1 = [v / 1000 for v in n]
        ex, ey = (nx1 - nx0) * .15, (ny1 - ny0) * .3
        pixelate(c, ((nx0 - ex) * W - cx) * s, ((ny0 - ey) * H - cy) * s,
                 ((nx1 + ex) * W - cx) * s, ((ny1 + ey) * H - cy) * s)
    rot = r.get('rotate') if r.get('rotate') in (90, 180, 270) else 0
    return c.rotate(-rot, expand=True) if rot else c     # PIL rotates counter-clockwise


# ---------- run ----------

def move(meta_path, folder, meta):
    dest = INBOX / folder
    dest.mkdir(exist_ok=True)
    img = meta_path.with_suffix('.jpg')
    if img.exists():
        img.rename(dest / img.name)
    (dest / meta_path.name).write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
    meta_path.unlink()


def main():
    if not KEY:
        sys.exit('GEMINI_KEY missing')
    lst_path = PUB / 'uploads.json'
    items = json.loads(lst_path.read_text(encoding='utf-8-sig')) if lst_path.exists() else []
    known = {it.get('hash') for it in items}
    (PUB / 'u').mkdir(exist_ok=True)
    pending = sorted((INBOX / 'inbox').glob('*.json')) if (INBOX / 'inbox').exists() else []
    print(f'{len(pending)} waiting', flush=True)
    counts = {'accepted': 0, 'rejected': 0, 'dup': 0, 'retry': 0}
    for mp in pending:
        if time.time() > DEADLINE:
            print('time is up; the rest waits for the next run')
            break
        meta = json.loads(mp.read_text(encoding='utf-8-sig'))
        h = meta.get('hash') or mp.stem
        if not mp.with_suffix('.jpg').exists():
            continue
        if h in known:
            meta['result'] = 'dup'; move(mp, 'done', meta); counts['dup'] += 1
            continue
        print(f'{h}:', flush=True)
        try:
            im = ImageOps.exif_transpose(Image.open(mp.with_suffix('.jpg'))).convert('RGB')
            small, _ = jpeg(im, 80, 1024)
            r = gemini([{'text': prompt(meta)}, {'inline_data': {'mime_type': 'image/jpeg', 'data': base64.b64encode(small).decode()}}])
        except Exception as e:
            meta['attempts'] = meta.get('attempts', 0) + 1
            meta['error'] = str(e)[:300]
            print(f'  failed ({meta["attempts"]})', flush=True)
            if meta['attempts'] >= 4:
                move(mp, 'failed', meta)
            else:
                mp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
            counts['retry'] += 1
            continue
        if not r.get('is_notebook_work'):
            meta['result'] = 'rejected'; meta['reason'] = short(r.get('reason'), 200)
            move(mp, 'rejected', meta); counts['rejected'] += 1
            print('  rejected', flush=True)
            continue
        cat = normalize(r, meta)
        out = finish(im, r)
        uid = 'u' + h[:12]
        full, (w, hh) = jpeg(out, 86, 1600)
        thumb, _ = jpeg(out, 82, 520)
        (PUB / 'u' / f'{uid}.jpg').write_bytes(full)
        (PUB / 'u' / f'{uid}_t.jpg').write_bytes(thumb)
        items.append({'id': uid, **cat, 'w': w, 'h': hh, 'file': 'u/' + uid, 'hash': h, 't': meta.get('time', '')})
        known.add(h)
        lst_path.write_text(json.dumps(items, ensure_ascii=False, indent=0), encoding='utf-8')
        meta['result'] = 'accepted'; meta['id'] = uid
        move(mp, 'done', meta); counts['accepted'] += 1
        print(f'  accepted as {uid}', flush=True)
    if not lst_path.exists():
        lst_path.write_text('[]', encoding='utf-8')
    print(json.dumps(counts))


if __name__ == '__main__':
    main()
