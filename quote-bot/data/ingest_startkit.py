"""Normalize the 10 start-kit tariffs into one long table + anomaly checks."""
import re, csv, sys, json, zipfile
from pathlib import Path
import openpyxl, pdfplumber

SK = Path(sys.argv[1])
rows = []   # carrier, origin, dest, transit, minimum, fuel, security, {break: rate}

def num(s):
    if s is None: return None
    if isinstance(s, (int, float)): return float(s)
    s = str(s).replace('EUR', '').strip().replace(',', '.')
    m = re.search(r'-?\d+(?:\.\d+)?', s)
    return float(m.group()) if m else None

def codes(s):
    return [c for c in re.findall(r'\b([A-Z]{3})\b', s) if c not in ('EUR', 'USD')]

def surcharges(text):
    out = {}
    for line in text.splitlines():
        m = re.search(r'^(.*?):\s*fuel\s*(?:EUR\s*)?([\d.,]+).*?security\s*(?:EUR\s*)?([\d.,]+)', line, re.I)
        if m:
            c = codes(m.group(1))
            if len(c) >= 2:
                out[(c[0], c[1])] = (num(m.group(2)), num(m.group(3)))
    return out

def add(carrier, o, d, transit, minimum, breaks, rates, fuel=None, sec=None):
    rows.append(dict(carrier=carrier, origin=o, dest=d, transit=int(num(transit)), minimum=num(minimum),
                     fuel=fuel, security=sec, rates={b: num(r) for b, r in zip(breaks, rates) if r not in (None, '')}))

def finish(carrier, sur):
    for r in rows:
        if r['carrier'] == carrier and r['fuel'] is None:
            r['fuel'], r['security'] = sur.get((r['origin'], r['dest']), (None, None))

B7_250 = [0, 45, 100, 250, 500, 1000, 2000]
B7_300 = [0, 45, 100, 300, 500, 1000, 2000]
B6_300 = [0, 45, 100, 300, 500, 1000]

# 1 Yellowtail (txt, fixed columns)
t = (SK / 'yellowtail-air-freight-tariff-2026-11.txt').read_text()
for line in t.splitlines():
    m = re.match(r'^([A-Z]{3})-([A-Z]{3})\s+(\d+)\s+EUR\s+([\d.]+)\s+(.*)$', line)
    if m:
        add('Yellowtail Air Freight', m[1], m[2], m[3], m[4], B7_250, re.findall(r'EUR\s+([\d.]+)', m[5]))
finish('Yellowtail Air Freight', surcharges(t))

# 2 Helix (pdf, table rows)
t = '\n'.join(p.extract_text() for p in pdfplumber.open(SK / 'helix-air-freight-tariff-2026-11.pdf').pages)
for line in t.splitlines():
    m = re.match(r'^.*\(([A-Z]{3})\) to .*\(([A-Z]{3})\)\s+D\+(\d+)\s+EUR\s+([\d.]+)\s+(.*)$', line)
    if m:
        add('Helix Air Freight', m[1], m[2], m[3], m[4], B7_250, re.findall(r'EUR\s+([\d.]+)', m[5]))
finish('Helix Air Freight', surcharges(t))

# 3 Cobalt / 4 Orion (pdf, one block per lane)
def blocks(path, carrier, header_re, breaks_from_labels=True):
    t = '\n'.join(p.extract_text() for p in pdfplumber.open(path).pages)
    cur = None
    def flush():
        if cur:
            bks = sorted(cur['r'])
            add(carrier, cur['o'], cur['d'], cur['transit'], cur['min'], bks, [cur['r'][b] for b in bks],
                cur.get('fuel'), cur.get('sec'))
    for line in t.splitlines():
        m = re.match(header_re, line.strip())
        if m:
            flush(); cur = dict(o=m[1], d=m[2], r={}); continue
        if cur is None: continue
        if (m := re.match(r'^Transit\s+(\d+)', line)): cur['transit'] = m[1]
        elif (m := re.match(r'^Minimum\s+([\d.,]+)', line)): cur['min'] = m[1]
        elif (m := re.match(r'^from\s+(\d+)\s*kg\s+([\d.,]+)', line)): cur['r'][int(m[1])] = m[2]
        elif (m := re.match(r'^Fuel\s+([\d.,]+)', line)): cur['fuel'] = num(m[1])
        elif (m := re.match(r'^Security\s+([\d.,]+)', line)): cur['sec'] = num(m[1])
        elif line.startswith('Surcharges'): flush(); cur = None
    flush()
    finish(carrier, surcharges(t))

blocks(SK / 'cobalt-skylift-tariff-2026-11.pdf', 'Cobalt Skylift', r'^([A-Z]{3}) \w+ > ([A-Z]{3}) ')
blocks(SK / 'orion-cargo-air-tariff-2026-11.pdf', 'Orion Cargo Air', r'^([A-Z]{3})-([A-Z]{3})$')

# 5-8 xlsx
def xl(name):
    ws = openpyxl.load_workbook(SK / name, data_only=True).worksheets[0]
    return [[c.value for c in r] for r in ws.iter_rows()]

def sur_from_rows(rs):
    return surcharges('\n'.join(str(r[0]) for r in rs if r[0]))

rs = xl('elbflug-air-freight-tariff-2026-11.xlsx'); o = None
for r in rs:
    if r[0] and str(r[0]).startswith('Origin:'): o = codes(str(r[0]))[0]
    elif o and r[0] and re.fullmatch(r'[A-Z]{3}', str(r[0])):
        add('Elbflug Air Freight', o, r[0], r[1], r[2], B6_300, r[3:9])
finish('Elbflug Air Freight', sur_from_rows(rs))

rs = xl('kestrel-airways-tariff-2026-11.xlsx'); o = None
for r in rs:
    if r[0] and str(r[0]).startswith('Origin:'): o = codes(str(r[0]))[0]
    elif o and r[0] and re.fullmatch(r'[A-Z]{3}', str(r[0])):
        add('Kestrel Airways', o, r[0], str(r[1]).replace('D+', ''), r[2], B7_250, r[3:10], num(r[10]), num(r[11]))

rs = xl('vega-skyfreight-tariff-2026-11.xlsx'); o = None
for r in rs:
    if r[0] and str(r[0]).startswith('Origin:'): o = codes(str(r[0]))[0]
    elif o and r[0] and re.fullmatch(r'.*\(([A-Z]{3})\)', str(r[0])) and r[1] is not None:
        add('Vega Skyfreight', o, codes(str(r[0]))[0], r[1], r[2], B7_300, r[3:10])
finish('Vega Skyfreight', sur_from_rows(rs))

rs = xl('wayzata-air-cargo-tariff-2026-11.xlsx')
for r in rs:
    if r[0] and ' > ' in str(r[0]) and ':' not in str(r[0]):
        c = codes(str(r[0])); add('Wayzata Air Cargo', c[0], c[1], r[1], r[2], B6_300, r[3:9])
finish('Wayzata Air Cargo', sur_from_rows(rs))

# 9 Quetzal (docx table)
x = zipfile.ZipFile(SK / 'quetzal-air-freight-tariff-2026-11.docx').read('word/document.xml').decode()
cells = [re.sub(r'<[^>]+>', '', c).strip() for c in re.findall(r'<w:tc>.*?</w:tc>', x, re.S)]
for i, c in enumerate(cells):
    if re.fullmatch(r'[A-Z]{3}-[A-Z]{3}', c):
        add('Quetzal Air Freight', c[:3], c[4:], cells[i+1], cells[i+2], B7_300, cells[i+3:i+10])
paras = '\n'.join(re.sub(r'<[^>]+>', '', p) for p in re.findall(r'<w:p[ >].*?</w:p>', x, re.S))
finish('Quetzal Air Freight', surcharges(paras))

# 10 Danube (scanned jpg, transcribed by hand)
DAN = """CGN-SYD 4 134,00 6,28 5,34 4,28 3,89 3,27 2,84 1,13 0,23
DUS-MEX 5 101,00 3,02 2,36 1,89 1,57 1,32 1,00 0,74 0,24
DUS-SYD 5 110,00 5,78 4,45 3,92 2,90 2,20 1,89 0,69 0,16
DUS-YYZ 4 62,00 3,08 2,62 2,07 1,61 1,22 0,96 0,88 0,21
FRA-DXB 2 138,86 2,90 2,23 2,05 1,66 1,45 1,33 1,12 0,22
FRA-EZE 2 126,00 4,57 3,34 2,74 2,52 1,92 1,48 1,06 0,21
MUC-JNB 7 105,00 3,74 2,81 2,19 1,62 1,39 1,22 0,85 0,24
MUC-TPE 5 69,00 4,10 3,73 3,21 2,57 2,36 2,03 1,15 0,21
STR-SGN 5 83,00 4,63 4,21 3,45 2,93 2,40 2,18 1,09 0,11"""
for line in DAN.splitlines():
    p = line.split()
    add('Danube Wings Cargo', p[0][:3], p[0][4:], p[1], p[2], B6_300, p[3:9], num(p[9]), num(p[10]))

# ---- write long csv
out = Path(sys.argv[2])
with out.open('w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['carrier', 'origin', 'destination', 'transit_days', 'minimum_eur', 'fuel_eur_kg', 'security_eur_kg', 'break_kg', 'rate_eur_kg'])
    for r in rows:
        for b, v in sorted(r['rates'].items()):
            w.writerow([r['carrier'], r['origin'], r['dest'], r['transit'], r['minimum'], r['fuel'], r['security'], b, v])
json.dump([{**r, 'rates': {str(k): v for k, v in r['rates'].items()}} for r in rows], open(out.with_suffix('.json'), 'w'), indent=1)
print(len(rows), 'lanes')
