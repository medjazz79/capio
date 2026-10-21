"""Free keyless fallback (Google GTX, auto-detect) + offline music glossary."""
from __future__ import annotations

import re
from . import http_client as requests

GTX_URL = "https://translate.googleapis.com/translate_a/single"
TIMEOUT = 18
TRIES = 2

# Small offline glossary of selected music terms (RU/EN/IT/DE/ES/FR -> AR).
GLOSSARY = {
    # Russian
    "каданс": "قفلة (كادانس)", "аккорд": "أكورد", "доминанта": "المهيمنة (دومينانت)",
    "тоника": "الأساسية (تونيك)", "субдоминанта": "تحت المهيمنة (سوب دومينانت)",
    "модуляция": "تحويل مقامي (مودولاسيون)", "септаккорд": "أكورد سابع",
    "трезвучие": "ثالوث", "лад": "مقام", "гармония": "هارموني",
    # English
    "cadence": "قفلة (كادانس)", "chord": "أكورد", "dominant": "المهيمنة (دومينانت)",
    "tonic": "الأساسية (تونيك)", "subdominant": "تحت المهيمنة",
    "modulation": "تحويل مقامي", "seventh chord": "أكورد سابع", "triad": "ثالوث",
    "scale": "سلم", "leading tone": "الحساسة", "counterpoint": "كونتربوان",
    "figured bass": "باص مرقم", "resolution": "حل", "suspension": "تعليقة",
    # Italian
    "accordo": "أكورد", "cadenza": "قفلة (كادانس)", "dominante": "المهيمنة",
    "tonica": "الأساسية", "modulazione": "تحويل مقامي",
    # German
    "akkord": "أكورد", "kadenz": "قفلة (كادانس)", "dominante": "المهيمنة",
    "tonika": "الأساسية", "modulation": "تحويل مقامي", "septakkord": "أكورد سابع",
    "dreiklang": "ثالوث",
    # Spanish
    "acorde": "أكورد", "cadencia": "قفلة (كادانس)", "tónica": "الأساسية",
    # French
    "sensible": "الحساسة", "tierce": "الثالثة", "quinte": "الخامسة",
    "sixte": "السادسة", "septième": "السابعة", "septieme": "السابعة",
    "renversement": "انقلاب", "fondamentale": "النغمة الأساسية",
    "chiffrage": "ترقيم الباص", "mouvement": "حركة",
    "conjoint": "متصل (بالدرجات)", "disjoint": "منفصل (بالقفز)",
    "parfaite": "تامة", "imparfaite": "ناقصة", "rompue": "مخادعة",
    "demi-cadence": "نصف قفلة", "ton": "مقام", "gamme": "سلم",
    "diatonique": "دياتوني", "chromatique": "كروماتي",
    "dièse": "دييز", "diese": "دييز", "bémol": "بيمول", "bemol": "بيمول",
    "bécarre": "بيكار", "becarre": "بيكار", "mesure": "مازورة",
    "sous-dominante": "تحت المهيمنة", "tonique": "الأساسية (تونيك)",
    "enchaînement": "تعاقب", "marche": "تتابع (مارش)",
    "contrepoint": "كونتربوان", "harmonie": "هارموني",
    "mélodie": "لحن", "melodie": "لحن", "rythme": "إيقاع",
    "retard": "تعليقة", "broderie": "تطريز (نغمة عابرة)",
    "pédale": "نغمة دواسة ثابتة", "degré": "درجة", "degre": "درجة",
    "octave": "أوكتاف", "unisson": "يونيسون",
}


def gtx_translate(text: str, source_language: str = "auto",
                  target_language: str = "ar") -> str:
    last: Exception | None = None
    for _ in range(TRIES):
        try:
            params = {
                "client": "gtx",
                "sl": "auto" if source_language == "auto" else source_language,
                "tl": target_language,
                "dt": "t",
                "q": text,
            }
            r = requests.get(GTX_URL, params=params, timeout=TIMEOUT,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = r.json()
            try:
                out = "".join(seg[0] for seg in data[0] if seg[0]).strip()
            except (TypeError, IndexError) as e:
                raise RuntimeError("Unexpected reply from the free translation service") from e
            if out:
                # The free service reports no token counts, so record an
                # estimate instead. It is marked as one so the UI can show
                # it differently from a measured number.
                from . import usage as _usage

                _usage.note_estimate(
                    "google-translate", "free",
                    prompt_text=text, completion_text=out)
                return out
            last = RuntimeError("Empty reply from the free translation service")
        except Exception as e:
            last = e
    raise last if last is not None else RuntimeError("Free translation failed")


def glossary_partial(text: str) -> str:
    """Offline: replace known terms, keep the rest. Clearly marked as partial."""
    out = text
    for src, ar in sorted(GLOSSARY.items(), key=lambda kv: -len(kv[0])):
        # Word boundaries: "ton" must not fire inside "tones" or "Norton".
        pat = r"(?<![\w])" + re.escape(src) + r"(?![\w])"
        out = re.sub(pat, ar, out, flags=re.IGNORECASE)
    if out == text:
        raise RuntimeError(
            "The offline glossary has no matching terms; no translation "
            "could be produced."
        )
    return (
        "⚠️ Translation service unreachable (tried twice). "
        "Partial glossary translation below — connect an AI provider "
        "in ⚙ Settings for full quality:\n" + out
    )
