#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
 МОДУЛЬ КУРСОВ ВАЛЮТ ЦЕНТРОБАНКА РФ (cbr.ru)
=============================================================================
 Забирает официальный курс с сайта ЦБ РФ и кэширует его на 1 час.

 Валюты: USD (доллар США), EUR (евро), CNY (юань),
         KRW (южнокорейская вона), JPY (иена).

 Ответ ЦБ приходит в XML в кодировке windows-1251. У каждой валюты есть
 поле VunitRate - курс за ОДНУ единицу в рублях. Это важно: KRW и JPY
 публикуются за 1000 и 100 единиц, но VunitRate номинал уже учитывает,
 поэтому вручную ничего делить не нужно.

 Использование:
     from rates import Rates
     r = Rates()
     r.rate("USD")                  # 80.5381  рублей за 1 доллар
     r.to_rubles(1291000, "JPY")    # сумма в иенах -> рубли
     r.convert(1000, "USD", to="EUR")

 Как скрипт - обновит rates.json рядом с собой:
     python3 rates.py
=============================================================================
"""
import os, re, json, time, gzip, threading, urllib.request

CBR_URL    = os.environ.get("CBR_URL", "https://www.cbr.ru/scripts/XML_daily.asp")
RATES_TTL  = int(os.environ.get("RATES_TTL", "3600"))   # 1 час
RATES_FILE = os.environ.get("RATES_FILE", "")

CURRENCIES = {
    "USD": ("Доллар США", "$"),
    "EUR": ("Евро", "€"),
    "CNY": ("Юань", "¥"),
    "KRW": ("Вон", "₩"),
    "JPY": ("Иена", "¥"),
}
UA = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36")


def _decode(raw):
    """ЦБ отдаёт windows-1251."""
    for enc in ("cp1251", "utf-8"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def parse_cbr(xml_text):
    """XML ЦБ -> {'date': ..., 'rates': {CODE: рублей за 1 единицу}}."""
    out, date = {}, None
    m = re.search(r'<ValCurs Date="([^"]+)"', xml_text)
    if m:
        date = m.group(1)
    for blk in re.findall(r"<Valute\b.*?</Valute>", xml_text, re.S):
        cc = re.search(r"<CharCode>([A-Z]{3})</CharCode>", blk)
        if not cc:
            continue
        code = cc.group(1)
        if code not in CURRENCIES:
            continue
        vr = re.search(r"<VunitRate>([\d.,]+)</VunitRate>", blk)
        if vr:
            val = float(vr.group(1).replace(",", "."))
        else:
            nom = re.search(r"<Nominal>([\d.]+)</Nominal>", blk)
            vv = re.search(r"<Value>([\d,]+)</Value>", blk)
            if not (nom and vv):
                continue
            n = float(nom.group(1)) or 1.0
            val = float(vv.group(1).replace(",", ".")) / n
        out[code] = val
    return {"date": date, "rates": out}


def fetch_rates(timeout=20):
    """Скачивает курсы ЦБ. None при неудаче."""
    try:
        req = urllib.request.Request(CBR_URL, headers={
            "User-Agent": UA, "Accept": "application/xml,text/xml,*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
        data = parse_cbr(_decode(raw))
        if data["rates"]:
            return data
    except Exception:
        pass
    return None


class Rates:
    """Кэширующий загрузчик курсов. Обновление раз в час."""

    def __init__(self, ttl=None, path=None):
        self.ttl = ttl if ttl is not None else RATES_TTL
        self.path = path or RATES_FILE or None
        self._data = None
        self._ts = 0
        self._lock = threading.Lock()

    def _load_file(self):
        if not self.path:
            return None
        try:
            if os.path.getmtime(self.path) + self.ttl < time.time():
                return None
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _save_file(self, data):
        if not self.path:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _fetch_soft(self):
        with self._lock:
            d = fetch_rates()
            if d:
                self._data = d
                self._ts = time.time()
                self._save_file(d)
            return self._data

    def get(self, force=False):
        """Актуальные курсы; обновляет раз в час."""
        if not force and self._data and time.time() - self._ts < self.ttl:
            return self._data
        d = self._load_file()
        if d and not force:
            self._data = d
            self._ts = time.time()
            return d
        return self._fetch_soft() or self._data

    def rate(self, code):
        """Рублей за 1 единицу валюты."""
        d = self.get()
        if not d:
            return None
        return (d.get("rates") or {}).get(code.upper())

    def all_rates(self):
        d = self.get()
        return (d.get("rates") or {}) if d else {}

    def date(self):
        d = self.get()
        return d.get("date") if d else None

    def convert(self, amount, frm, to="RUB"):
        """Перевести amount из валюты frm в валюту to (по умолчанию рубли)."""
        frm, to = frm.upper(), to.upper()
        r = self.rate(frm)
        if not r:
            return None
        rub = float(amount) * r
        if to == "RUB":
            return rub
        r2 = self.rate(to)
        if not r2:
            return None
        return rub / r2

    def to_rubles(self, amount, frm):
        return self.convert(amount, frm, "RUB")


def start_background(rates, interval=3600):
    """Фоновое обновление курсов. Вызвать один раз при старте."""
    def loop():
        while True:
            try:
                rates.get(force=True)
            except Exception:
                pass
            time.sleep(interval)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rates.json")
    r = Rates(path=out, ttl=0)
    d = r.get(force=True)
    if d:
        print("курс ЦБ на %s:" % d["date"])
        for k, v in d["rates"].items():
            print("  %s %-4s %.4f руб." % (CURRENCIES[k][1], k, v))
        print("записан:", out)
    else:
        print("не удалось получить курс ЦБ")
