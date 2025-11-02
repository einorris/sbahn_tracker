#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import sys
import argparse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")

STATION_SYNONYMS.update({
    # Общие правила: Munich -> München; ue/oe/ae -> ü/ö/ä (см. ниже режеx-правило)
    # Центр / Stammstrecke
    "munich hbf": "München Hbf",
    "munich hauptbahnhof": "München Hbf",
    "muenchen hbf": "München Hbf",
    "muenchen hauptbahnhof": "München Hbf",
    "münchen hauptbahnhof": "München Hbf",
    "hbf tief": "München Hbf",  # иногда прилетает как «Hbf (tief)»
    "hauptbahnhof": "München Hbf",

    "marienplatz": "München Marienplatz",
    "marienplatz (tief)": "München Marienplatz",

    "karlsplatz": "München Karlsplatz (Stachus)",
    "stachus": "München Karlsplatz (Stachus)",
    "karlsplatz (stachus)": "München Karlsplatz (Stachus)",

    "isartor": "München Isartor",
    "rosenheimer platz": "München Rosenheimer Platz",
    "hackerbrücke": "München Hackerbrücke",
    "hackerbruecke": "München Hackerbrücke",
    "donnersbergerbruecke": "München Donnersbergerbrücke",
    "donnersbergerbrücke": "München Donnersbergerbrücke",
    "laim": "München Laim",
    "pasing": "München-Pasing",
    "muenchen pasing": "München-Pasing",
    "münchen pasing": "München-Pasing",

    # Восток
    "ostbahnhof": "München Ost",
    "munich east": "München Ost",
    "muenchen ostbahnhof": "München Ost",
    "münchen ostbahnhof": "München Ost",
    "leuchtenbergring": "München Leuchtenbergring",
    "berg am laim": "München-Berg am Laim",
    "trudering": "München-Trudering",
    "riem": "München-Riem",

    # Юг/юго-восток (S3/S7/S20)
    "gising": "München Giesing",   # частая опечатка
    "giesing": "München Giesing",
    "harras": "München Harras",
    "mittersendling": "Mittersendling",
    "siemenswerke": "Siemenswerke",
    "solln": "München Solln",
    "fasangarten": "Fasangarten",
    "neuperlach süd": "Neuperlach Süd",
    "neuperlach sud": "Neuperlach Süd",

    # Север/северо-восток (S1/S8/S3)
    "feldmoching": "München-Feldmoching",
    "moosach": "München-Moosach",
    "oberwiesenfeld": "Oberwiesenfeld",  # иногда лезет из U-Bahn — игнорируй при желании
    "unterföhring": "Unterföhring",
    "unterfoehring": "Unterföhring",
    "ismaning": "Ismaning",

    # Аэропорт
    "munich airport": "München Flughafen Terminal",
    "muc": "München Flughafen Terminal",
    "flughafen münchen": "München Flughafen Terminal",
    "flughafen muenchen": "München Flughafen Terminal",
    "munich international airport": "München Flughafen Terminal",
    "visitor park": "München Flughafen Besucherpark",
    "besucherpark": "München Flughafen Besucherpark",

    # Восточная дуга S2 до Эрдинга
    "erding": "Erding",
    "altenerding": "Altenerding",
    "aufhausen (oberbay)": "Aufhausen (Oberbay)",
    "markt schwaben": "Markt Schwaben",
    "grub (oberbay)": "Grub (Oberbay)",
    "heimstetten": "Heimstetten",
    "daglfing": "München-Daglfing",
    "englschalking": "München-Englschalking",
    "rietmoos": "Riemerling",  # иногда ошибочно так пишут — мапим на ближайшее частое
})


EVA_BY_NAME = {
    "München Ost": "8000262",
}

@dataclass
class EventTime:
    when: datetime | None
    source: str  # "ct", "pt", "pt+delay"
    cancelled: bool
    delay_min: int

@dataclass
class Departure:
    sid: str
    when: datetime
    line: str | None
    cat: str | None   # S, RE, RB, ICE, RJ, etc (из <tl c="..."> или l="")
    number: str | None
    platform: str | None
    destination: str | None
    operator: str | None
    cancelled: bool
    delay_min: int

def parse_tt(ts: str) -> datetime | None:
    """
    Конвертирует формат DB '2511021336' -> 2025-11-02 13:36 Europe/Berlin.
    Формат: yymmddHHMM, где yy '00'..'99' => 2000..2099.
    """
    if not ts or len(ts) != 10 or not ts.isdigit():
        return None
    year = 2000 + int(ts[:2])
    month = int(ts[2:4])
    day = int(ts[4:6])
    hour = int(ts[6:8])
    minute = int(ts[8:10])
    try:
        return datetime(year, month, day, hour, minute, tzinfo=BERLIN)
    except ValueError:
        return None

def best_time(node: ET.Element | None) -> EventTime:
    """
    Возвращает лучшее доступное время для ar/dp:
    - если есть атрибут ct -> его;
    - иначе pt + возможная задержка из <m t="d" c="...">;
    - отмена: есть ли <m t="f"> внутри.
    """
    if node is None:
        return EventTime(None, "missing", False, 0)

    ct = node.get("ct")
    pt = node.get("pt")
    cancelled = any(m.get("t") == "f" for m in node.findall("./m"))
    delay_msgs = [m for m in node.findall("./m") if m.get("t") == "d" and m.get("c") and m.get("c").isdigit()]
    delay_min = max((int(m.get("c")) for m in delay_msgs), default=0)

    if ct:
        return EventTime(parse_tt(ct), "ct", cancelled, delay_min)

    when = parse_tt(pt) if pt else None
    if when and delay_min:
        when = when + timedelta(minutes=delay_min)
        return EventTime(when, "pt+delay", cancelled, delay_min)

    return EventTime(when, "pt" if pt else "missing", cancelled, delay_min)

def normalize_station(name: str | None) -> str | None:
    if not name:
        return None
    key = name.strip().lower().replace("  ", " ")
    return STATION_SYNONYMS.get(key, name)

def extract_destination(ppth: str | None) -> str | None:
    if not ppth:
        return None
    # Последний пункт в списке — предполагаем пункт назначения
    parts = [p.strip() for p in ppth.split("|") if p.strip()]
    return parts[-1] if parts else None

def parse_base(xml_text: str) -> dict[str, dict]:
    """
    Парсим базовое расписание <timetable station='...'>.
    Возвращает словарь по s/@id.
    """
    root = ET.fromstring(xml_text)
    out = {}
    station = normalize_station(root.get("station"))
    eva = root.get("eva") or EVA_BY_NAME.get(station or "", None)

    for s in root.findall("./s"):
        sid = s.get("id")
        if not sid:
            continue

        tl = s.find("./tl")
        dp = s.find("./dp")

        line = (dp.get("l") if dp is not None and dp.get("l") else (tl.get("n") if tl is not None else None))
        cat = tl.get("c") if tl is not None else (dp.get("l") if dp is not None else None)
        number = tl.get("n") if tl is not None else None
        operator = tl.get("c") if tl is not None else None

        dp_time = best_time(dp)
        platform = dp.get("pp") if dp is not None else None
        dest = extract_destination(dp.get("ppth") if dp is not None else None)

        out[sid] = dict(
            sid=sid,
            dp_node=dp,            # для дообогащения
            dp=dp_time,
            line=line,
            cat=cat,
            number=number,
            platform=platform,
            destination=dest,
            operator=operator,
            station=station,
            eva=eva,
        )
    return out

def merge_changes(base: dict[str, dict], changes_xml: str) -> dict[str, dict]:
    """
    Сшиваем по s/@id. Из изменений берём:
    - dp.ct как основное время;
    - отмены и задержки из <m>;
    - platform (если в changes появится pp — у DB иногда это другой атрибут cp/pp нет, поэтому оставляем базовый pp).
    Также принимаем линию/категорию l, если в базе её не было.
    """
    root = ET.fromstring(changes_xml)
    # Фильтруем по EVA станции, если можем (для Ostbahnhof это 8000262)
    target_eva = None
    # если в базе все записи одной станции — возьмём её EVA
    for v in base.values():
        if v.get("eva"):
            target_eva = v["eva"]
            break

    for s in root.findall("./s"):
        if target_eva and s.get("eva") and s.get("eva") != target_eva:
            continue

        sid = s.get("id")
        if not sid or sid not in base:
            # Иногда в changes есть записи, которых нет в базовом — можно добавить как новые отправления
            # но безопаснее пропустить, чтобы не огрести дубликаты разных источников
            continue

        dp = s.find("./dp")
        if dp is None:
            continue

        # Обновим время/отмену/задержку
        dp_time = best_time(dp)
        if dp_time.when is not None:
            base[sid]["dp"] = dp_time

        # Линия/категория из changes
        if dp.get("l"):
            base[sid]["line"] = base[sid]["line"] or dp.get("l")

        # Инкрементальная платформа: в changes почти всегда нет pp; оставим базовую
        # Но если вдруг появится атрибут pp — используем его.
        if dp.get("pp"):
            base[sid]["platform"] = dp.get("pp")

        # Обновим назначение, если есть ppth
        if dp.get("ppth"):
            base[sid]["destination"] = extract_destination(dp.get("ppth"))

        # Если есть tl в changes (редко) — обновим cat/number/operator
        tl = s.find("./tl")
        if tl is not None:
            base[sid]["cat"] = tl.get("c") or base[sid]["cat"]
            base[sid]["number"] = tl.get("n") or base[sid]["number"]
            base[sid]["operator"] = tl.get("c") or base[sid]["operator"]

    return base

def collect_departures(merged: dict[str, dict], now: datetime, horizon_min: int = 60) -> list[Departure]:
    out: list[Departure] = []
    for v in merged.values():
        et: EventTime = v["dp"]
        if et.when is None:
            continue
        if et.cancelled:
            # Показываем отменённые только если они попадают в окно — можно исключать, решай сам
            pass

        # фильтр по окну
        if now <= et.when <= (now + timedelta(minutes=horizon_min)):
            out.append(
                Departure(
                    sid=v["sid"],
                    when=et.when,
                    line=v.get("line"),
                    cat=v.get("cat"),
                    number=v.get("number"),
                    platform=v.get("platform"),
                    destination=v.get("destination"),
                    operator=v.get("operator"),
                    cancelled=et.cancelled,
                    delay_min=et.delay_min,
                )
            )
    out.sort(key=lambda d: d.when)
    return out

def format_row(d: Departure) -> str:
    t = d.when.strftime("%H:%M")
    line = d.line or "-"
    cat = (d.cat or "").upper()
    num = d.number or ""
    label = f"{cat}{(' ' + num) if num else ''}".strip()
    plat = f"Gl. {d.platform}" if d.platform else ""
    dest = d.destination or ""
    flags = []
    if d.cancelled:
        flags.append("🚫 отменён")
    elif d.delay_min:
        flags.append(f"+{d.delay_min}′")
    flags_s = ("  •  " + " / ".join(flags)) if flags else ""
    return f"{t}  {line:<4}  {label:<8}  {dest:<30}  {plat}{flags_s}  (id {d.sid})"

def main():
    parser = argparse.ArgumentParser(description="Next departures merger (München Ost fix).")
    parser.add_argument("--base-xml", required=True, help="Путь к базовому timetable XML (как у тебя).")
    parser.add_argument("--changes-xml", required=True, help="Путь к XML «known changes».")
    parser.add_argument("--station", default="Ostbahnhof", help="Имя станции/синоним (по умолчанию Ostbahnhof).")
    parser.add_argument("--horizon", type=int, default=60, help="Окно в минутах для ближайших отправлений.")
    args = parser.parse_args()

    # читаем файлы
    base_xml = open(args.base_xml, "r", encoding="utf-8").read()
    changes_xml = open(args.changes_xml, "r", encoding="utf-8").read()

    # нормализуем станцию и вычислим EVA, если потребуется
    norm_station = normalize_station(args.station)
    eva = EVA_BY_NAME.get(norm_station or "", None)

    # парсим
    base = parse_base(base_xml)
    merged = merge_changes(base, changes_xml)

    now = datetime.now(tz=BERLIN)
    deps = collect_departures(merged, now, args.horizon)

    if not deps:
        print(f"Нет отправлений в ближайшие {args.horizon} минут для станции {norm_station or args.station}.")
        # подсказка по отладке: выведем пару ближайших независимо от окна
        all_deps = collect_departures(merged, now - timedelta(hours=1), 6*60)
        if all_deps:
            print("\nБлижайшие в целом (6 часов):")
            for d in all_deps[:10]:
                print("  " + format_row(d))
        sys.exit(0)

    print(f"Станция: {norm_station or args.station}  (EVA: {eva or '—'})  Сейчас: {now.strftime('%H:%M')}")
    print(f"Ближайшие отправления (до +{args.horizon}′):\n")
    for d in deps:
        print(format_row(d))

if __name__ == "__main__":
    main()
