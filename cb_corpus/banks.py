"""Registry of the 63 BIS member central banks (the target list).

`homepage` is the official primary domain that every downloaded PDF must come
from (or *.bis.org for the speech index). Domains for the major banks are
high-confidence; the long-tail entries are marked verify=True and the framework
will check reachability before the first crawl.

`bis_institution` is the label the BIS uses on its speeches listing, used to map
a speech back to a bank_code.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Bank:
    code: str            # short stable key, e.g. "fed"
    name: str            # official English name
    country: str
    iso2: str
    homepage: str        # official domain (no scheme)
    bis_institution: str  # label used on BIS speeches listing
    languages: tuple[str, ...] = ("en",)
    verify: bool = False  # True => domain should be confirmed on first run


def _b(code, name, country, iso2, homepage, bis, langs=("en",), verify=False):
    return Bank(code, name, country, iso2, homepage, bis, tuple(langs), verify)


# 63 BIS member central banks (bis.org/about/member_cb.htm)
BIS_63: tuple[Bank, ...] = (
    _b("dz", "Bank of Algeria", "Algeria", "DZ", "bank-of-algeria.dz", "Bank of Algeria", ("fr", "ar"), True),
    _b("ar", "Central Bank of Argentina", "Argentina", "AR", "bcra.gob.ar", "Central Bank of Argentina", ("es",)),
    _b("au", "Reserve Bank of Australia", "Australia", "AU", "rba.gov.au", "Reserve Bank of Australia"),
    _b("at", "Oesterreichische Nationalbank", "Austria", "AT", "oenb.at", "Oesterreichische Nationalbank", ("de", "en")),
    _b("be", "National Bank of Belgium", "Belgium", "BE", "nbb.be", "National Bank of Belgium", ("nl", "fr", "en")),
    _b("ba", "Central Bank of Bosnia and Herzegovina", "Bosnia and Herzegovina", "BA", "cbbh.ba", "Central Bank of Bosnia and Herzegovina", ("bs", "en"), True),
    _b("br", "Central Bank of Brazil", "Brazil", "BR", "bcb.gov.br", "Central Bank of Brazil", ("pt", "en")),
    _b("bg", "Bulgarian National Bank", "Bulgaria", "BG", "bnb.bg", "Bulgarian National Bank", ("bg", "en")),
    _b("ca", "Bank of Canada", "Canada", "CA", "bankofcanada.ca", "Bank of Canada", ("en", "fr")),
    _b("cl", "Central Bank of Chile", "Chile", "CL", "bcentral.cl", "Central Bank of Chile", ("es", "en")),
    _b("cn", "People's Bank of China", "China", "CN", "pbc.gov.cn", "People's Bank of China", ("zh", "en")),
    _b("co", "Central Bank of Colombia", "Colombia", "CO", "banrep.gov.co", "Central Bank of Colombia", ("es",)),
    _b("hr", "Croatian National Bank", "Croatia", "HR", "hnb.hr", "Croatian National Bank", ("hr", "en")),
    _b("cz", "Czech National Bank", "Czechia", "CZ", "cnb.cz", "Czech National Bank", ("cs", "en")),
    _b("dk", "Danmarks Nationalbank", "Denmark", "DK", "nationalbanken.dk", "Danmarks Nationalbank", ("da", "en")),
    _b("ee", "Eesti Pank", "Estonia", "EE", "eestipank.ee", "Eesti Pank", ("et", "en")),
    _b("ecb", "European Central Bank", "Euro area", "EU", "ecb.europa.eu", "European Central Bank", ("en",)),
    _b("fi", "Bank of Finland", "Finland", "FI", "suomenpankki.fi", "Bank of Finland", ("fi", "en")),
    _b("fr", "Bank of France", "France", "FR", "banque-france.fr", "Bank of France", ("fr", "en")),
    _b("de", "Deutsche Bundesbank", "Germany", "DE", "bundesbank.de", "Deutsche Bundesbank", ("de", "en")),
    _b("gr", "Bank of Greece", "Greece", "GR", "bankofgreece.gr", "Bank of Greece", ("el", "en")),
    _b("hk", "Hong Kong Monetary Authority", "Hong Kong SAR", "HK", "hkma.gov.hk", "Hong Kong Monetary Authority", ("en", "zh")),
    _b("hu", "Magyar Nemzeti Bank", "Hungary", "HU", "mnb.hu", "Magyar Nemzeti Bank", ("hu", "en")),
    _b("is", "Central Bank of Iceland", "Iceland", "IS", "cb.is", "Central Bank of Iceland", ("is", "en")),
    _b("in", "Reserve Bank of India", "India", "IN", "rbi.org.in", "Reserve Bank of India"),
    _b("id", "Bank Indonesia", "Indonesia", "ID", "bi.go.id", "Bank Indonesia", ("id", "en")),
    _b("ie", "Central Bank of Ireland", "Ireland", "IE", "centralbank.ie", "Central Bank of Ireland"),
    _b("il", "Bank of Israel", "Israel", "IL", "boi.org.il", "Bank of Israel", ("he", "en")),
    _b("it", "Bank of Italy", "Italy", "IT", "bancaditalia.it", "Bank of Italy", ("it", "en")),
    _b("jp", "Bank of Japan", "Japan", "JP", "boj.or.jp", "Bank of Japan", ("ja", "en")),
    _b("kr", "Bank of Korea", "Korea", "KR", "bok.or.kr", "Bank of Korea", ("ko", "en")),
    _b("kw", "Central Bank of Kuwait", "Kuwait", "KW", "cbk.gov.kw", "Central Bank of Kuwait", ("ar", "en"), True),
    _b("lv", "Latvijas Banka", "Latvia", "LV", "bank.lv", "Latvijas Banka", ("lv", "en")),
    _b("lt", "Lietuvos bankas", "Lithuania", "LT", "lb.lt", "Lietuvos bankas", ("lt", "en")),
    _b("lu", "Banque centrale du Luxembourg", "Luxembourg", "LU", "bcl.lu", "Banque centrale du Luxembourg", ("fr", "en")),
    _b("my", "Bank Negara Malaysia", "Malaysia", "MY", "bnm.gov.my", "Central Bank of Malaysia", ("ms", "en")),
    _b("mx", "Bank of Mexico", "Mexico", "MX", "banxico.org.mx", "Bank of Mexico", ("es", "en")),
    _b("ma", "Bank Al-Maghrib", "Morocco", "MA", "bkam.ma", "Bank Al-Maghrib", ("fr", "ar"), True),
    _b("nl", "De Nederlandsche Bank", "Netherlands", "NL", "dnb.nl", "Netherlands Bank", ("nl", "en")),
    _b("nz", "Reserve Bank of New Zealand", "New Zealand", "NZ", "rbnz.govt.nz", "Reserve Bank of New Zealand"),
    _b("mk", "National Bank of the Republic of North Macedonia", "North Macedonia", "MK", "nbrm.mk", "National Bank of the Republic of North Macedonia", ("mk", "en"), True),
    _b("no", "Norges Bank", "Norway", "NO", "norges-bank.no", "Norges Bank", ("no", "en")),
    _b("pe", "Central Reserve Bank of Peru", "Peru", "PE", "bcrp.gob.pe", "Central Reserve Bank of Peru", ("es", "en")),
    _b("ph", "Bangko Sentral ng Pilipinas", "Philippines", "PH", "bsp.gov.ph", "Bangko Sentral ng Pilipinas"),
    _b("pl", "Narodowy Bank Polski", "Poland", "PL", "nbp.pl", "National Bank of Poland", ("pl", "en")),
    _b("pt", "Banco de Portugal", "Portugal", "PT", "bportugal.pt", "Bank of Portugal", ("pt", "en")),
    _b("ro", "National Bank of Romania", "Romania", "RO", "bnr.ro", "National Bank of Romania", ("ro", "en")),
    _b("ru", "Central Bank of the Russian Federation", "Russia", "RU", "cbr.ru", "Central Bank of the Russian Federation", ("ru", "en")),
    _b("sa", "Saudi Central Bank", "Saudi Arabia", "SA", "sama.gov.sa", "Saudi Central Bank", ("ar", "en")),
    _b("rs", "National Bank of Serbia", "Serbia", "RS", "nbs.rs", "National Bank of Serbia", ("sr", "en")),
    _b("sg", "Monetary Authority of Singapore", "Singapore", "SG", "mas.gov.sg", "Monetary Authority of Singapore"),
    _b("sk", "Narodna banka Slovenska", "Slovakia", "SK", "nbs.sk", "National Bank of Slovakia", ("sk", "en")),
    _b("si", "Banka Slovenije", "Slovenia", "SI", "bsi.si", "Bank of Slovenia", ("sl", "en")),
    _b("za", "South African Reserve Bank", "South Africa", "ZA", "resbank.co.za", "South African Reserve Bank"),
    _b("es", "Banco de Espana", "Spain", "ES", "bde.es", "Bank of Spain", ("es", "en")),
    _b("se", "Sveriges Riksbank", "Sweden", "SE", "riksbank.se", "Sveriges Riksbank", ("sv", "en")),
    _b("ch", "Swiss National Bank", "Switzerland", "CH", "snb.ch", "Swiss National Bank", ("de", "en", "fr")),
    _b("th", "Bank of Thailand", "Thailand", "TH", "bot.or.th", "Bank of Thailand", ("th", "en")),
    _b("tr", "Central Bank of the Republic of Turkiye", "Turkiye", "TR", "tcmb.gov.tr", "Central Bank of the Republic of Turkey", ("tr", "en")),
    _b("ae", "Central Bank of the United Arab Emirates", "United Arab Emirates", "AE", "centralbank.ae", "Central Bank of the United Arab Emirates", ("ar", "en"), True),
    _b("gb", "Bank of England", "United Kingdom", "GB", "bankofengland.co.uk", "Bank of England"),
    _b("us", "Federal Reserve System", "United States", "US", "federalreserve.gov", "Board of Governors of the Federal Reserve System"),
    _b("vn", "State Bank of Vietnam", "Vietnam", "VN", "sbv.gov.vn", "State Bank of Vietnam", ("vi", "en"), True),
)

_BY_CODE = {b.code: b for b in BIS_63}
_BY_BIS = {b.bis_institution: b for b in BIS_63}


# Extra BIS speech-listing labels per bank: historical names, official renames,
# native-language forms and variants the BIS has used over time. The primary
# `bis_institution` above is one label per bank, which silently dropped whole
# banks/eras whenever BIS wrote a different name (discovery audit 2026-06,
# root cause RC1). Matching ANY of these labels recovers them.
BIS_ALIASES: dict[str, tuple[str, ...]] = {
    "lv": ("Bank of Latvia", "Bank of Lativa"),
    "lt": ("Bank of Lithuania",),
    "lu": ("Central Bank of Luxembourg",),
    "ru": ("Bank of Russia", "Central Bank of Russia"),
    "sa": ("Saudi Arabian Monetary Agency", "Saudi Arabian Monetary Authority"),
    "tr": ("Central Bank of the Republic of Turkiye",),
    "mk": ("National Bank of the Republic of Macedonia",),
    "pt": ("Banco de Portugal",),
    "si": ("Banka Slovenije",),
    "ma": ("Bank of Morocco",),
    "co": ("Banco de la Republica",),
    "hu": ("Central Bank of Hungary", "National Bank of Hungary"),
    "ph": ("Central Bank of the Philippines",),
    "us": ("Board of Governors of the US Federal Reserve System",),
    "es": ("Banco de Espana",),
    "my": ("Bank Negara Malaysia",),
    "nl": ("De Nederlandsche Bank",),
    "pl": ("Narodowy Bank Polski",),
    "sk": ("Narodna banka Slovenska",),
}


def normalize_institution(s: str) -> str:
    """Fold a label/description to a robust match key.

    Strips diacritics (Türkiye->turkiye, España->espana), drops apostrophes and
    punctuation (People's->peoples, also neutralising mojibake bytes), lowercases
    and collapses whitespace — so BIS encoding/diacritic quirks stop dropping
    speeches.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if (c.isalnum() or c.isspace()) else " " for c in s.lower())
    return " ".join(s.split())


def _bis_labels(bank: Bank) -> tuple[str, ...]:
    return (bank.bis_institution,) + BIS_ALIASES.get(bank.code, ())


# (normalized_label, primary_label) for every primary + alias label, sorted
# longest-normalized-first so the most specific institution wins on a substring
# match. Maps to the bank's PRIMARY label so bank_for_bis_institution resolves it.
_BIS_LABEL_INDEX: list[tuple[str, str]] = sorted(
    {(normalize_institution(lbl), b.bis_institution)
     for b in BIS_63 for lbl in _bis_labels(b)},
    key=lambda t: len(t[0]), reverse=True,
)


def get_bank(code: str) -> Bank:
    return _BY_CODE[code]


def bank_for_bis_institution(label: str) -> Bank | None:
    return _BY_BIS.get(label)


def match_bis_institution(head_text: str) -> str:
    """Return the PRIMARY bis_institution label whose primary/alias form appears
    in `head_text` (longest match wins), or "" if none. Alias-aware + accent/
    apostrophe-insensitive."""
    nhead = normalize_institution(head_text)
    if not nhead:
        return ""
    for nlabel, primary in _BIS_LABEL_INDEX:
        if nlabel and nlabel in nhead:
            return primary
    return ""


assert len(BIS_63) == 63, f"expected 63 banks, got {len(BIS_63)}"
assert len(_BY_CODE) == 63, "duplicate bank code"
