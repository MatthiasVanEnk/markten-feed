"""Uurlijkse marktfeed. Schrijft markten.json naast dit script.

Alleen de standaardbibliotheek. Geen API-sleutels en geen persoonsgegevens.
Een mislukte bron wordt null plus een regel in fouten. Oude koersen worden
niet hergebruikt.

Bronnen, gecontroleerd op 2026-11-03:
- EUR/USD: Frankfurter (ECB-referentie) en Swissquote (live). Frankfurter en
  de ECB-XML zijn dezelfde koers, dus de XML is geen tweede bron.
- Metalen in USD per troy ounce: gold-api.com en Swissquote. Omrekenen naar
  EUR met de live EUR/USD, anders met de ECB-koers. De 24-uursverandering
  komt van het bijbehorende Yahoo-future (GC=F, SI=F, PL=F, PA=F), dat is de
  slotstand tegenover het vorige slot, geen doorlopende 24 uur.
- Crypto: CoinGecko (EUR) en CoinPaprika (EUR). Verpakte en gestakete tokens
  vallen af. Stablecoins blijven staan.
- Indices: Yahoo Finance-grafiek. Tweede bron alleen voor de AEX (Swissquote).
  Stooq gaf 404 of een browsercontrole. De procentuele verandering van een
  index is de slotstand tegenover het vorige slot.

Afsluitcodes: 0 bestand geschreven, 2 bestand geschreven maar push mislukt,
1 bestand niet geschreven.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "markten.json"
LOG = ROOT / "run.log"
USER_AGENT = "MarktenFeed/1.0"
TIMEOUT = 20
RETRIES = 3
PAUSE_SECONDS = 2

# Afgeleide tokens die anders dubbel meetellen met de onderliggende munt.
DERIVATIVE_SYMBOLS = {
    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "CBETH", "RETH", "WBETH",
    "BETH", "METH", "SFRXETH", "FRXETH", "CBBTC", "BTCB", "WBNB", "WSOL",
    "JITOSOL", "MSOL", "BNSOL", "LBTC", "EBTC", "EZETH", "RSETH", "SOLVBTC",
}
DERIVATIVE_WORDS = ("wrapped", "staked", "liquid staking", "bridged")

METALS = (
    ("XAU", "GC=F"),
    ("XAG", "SI=F"),
    ("XPT", "PL=F"),
    ("XPD", "PA=F"),
)
INDICES = (
    ("SPX", "^GSPC", None),
    ("IXIC", "^IXIC", None),
    ("DJI", "^DJI", None),
    ("SX5E", "^STOXX50E", None),
    ("AEX", "^AEX", "AEX/EUR"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def unix_to_utc(seconds: int | float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def num(value: object, digits: int) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return round(float(value), digits)


def deviation_pct(primary: float, second: float) -> float | None:
    if primary == 0:
        return None
    return abs(primary - second) / abs(primary) * 100


def fetch_bytes(url: str) -> bytes:
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/xml, */*"},
            )
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            last = exc
        if attempt < RETRIES:
            time.sleep(PAUSE_SECONDS)
    raise RuntimeError(str(last) if last else "ophalen mislukt")


def fetch_json(url: str) -> tuple[object | None, str | None]:
    try:
        raw = fetch_bytes(url)
    except Exception as exc:
        return None, str(exc)
    try:
        return json.loads(raw.decode("utf-8")), None
    except Exception as exc:
        return None, f"geen JSON ({exc})"


def swissquote_mid(pair: str) -> tuple[float | None, str | None, str | None]:
    """Midden van bied en laat, eerste platform met profiel premium."""
    url = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/" + pair
    data, error = fetch_json(url)
    if error or not isinstance(data, list):
        return None, None, error or "Swissquote gaf geen lijst"
    for row in data:
        if not isinstance(row, dict):
            continue
        profiles = row.get("spreadProfilePrices")
        if not isinstance(profiles, list):
            continue
        chosen = next((item for item in profiles if item.get("spreadProfile") == "premium"), None)
        if chosen is None and profiles:
            chosen = profiles[0]
        if not isinstance(chosen, dict):
            continue
        bid = num(chosen.get("bid"), 8)
        ask = num(chosen.get("ask"), 8)
        if bid is None or ask is None:
            continue
        stamp = None
        ts = row.get("ts")
        if isinstance(ts, (int, float)) and math.isfinite(ts):
            stamp = unix_to_utc(ts / 1000 if ts > 10_000_000_000 else ts)
        return (bid + ask) / 2, stamp, None
    return None, None, f"Swissquote {pair} heeft geen koers"


def frankfurter_eur_usd() -> tuple[float | None, str | None, str | None]:
    data, error = fetch_json("https://api.frankfurter.app/latest?from=EUR&to=USD")
    if error or not isinstance(data, dict):
        return None, None, error or "Frankfurter gaf geen object"
    rates = data.get("rates")
    date = data.get("date")
    if not isinstance(rates, dict):
        return None, None, "Frankfurter mist rates"
    rate = num(rates.get("USD"), 6)
    stamp = f"{date}T00:00:00Z" if isinstance(date, str) else None
    if rate is None:
        return None, stamp, "Frankfurter mist USD"
    return rate, stamp, None


def is_derivative(symbol: str, name: str, coin_id: str = "") -> bool:
    if symbol.upper() in DERIVATIVE_SYMBOLS:
        return True
    text = f"{name} {coin_id}".lower()
    return any(word in text for word in DERIVATIVE_WORDS)


def yahoo_chart(symbol: str) -> tuple[dict | None, str | None]:
    encoded = urllib.parse.quote(symbol, safe="=")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval=1d&range=5d"
    data, error = fetch_json(url)
    if error or not isinstance(data, dict):
        return None, error or "Yahoo gaf geen object"
    chart = data.get("chart")
    if not isinstance(chart, dict):
        return None, f"Yahoo {symbol} mist chart"
    results = chart.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        err = chart.get("error")
        return None, f"Yahoo {symbol}: {err or 'geen resultaat'}"
    meta = results[0].get("meta")
    if not isinstance(meta, dict):
        return None, f"Yahoo {symbol} mist meta"
    return meta, None


def market_status(meta: dict) -> str | None:
    period = meta.get("currentTradingPeriod")
    if not isinstance(period, dict):
        return None
    regular = period.get("regular")
    if not isinstance(regular, dict):
        return None
    start = regular.get("start")
    end = regular.get("end")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    now = time.time()
    return "open" if start <= now <= end else "gesloten"


def collect_fx(errors: list[str]) -> tuple[dict, float | None, str | None, str | None]:
    primary, primary_stamp, primary_error = frankfurter_eur_usd()
    second, second_stamp, second_error = swissquote_mid("EUR/USD")
    if primary_error:
        errors.append(f"EUR/USD Frankfurter: {primary_error}")
    if second_error:
        errors.append(f"EUR/USD Swissquote: {second_error}")
    if second is not None and not 0.7 < second < 1.6:
        errors.append(f"EUR/USD Swissquote {second} valt buiten de verwachte band")
        second = None
    diff = deviation_pct(primary, second) if primary is not None and second is not None else None
    confirmed = diff is not None and diff <= 1
    if diff is not None and diff > 1:
        errors.append(f"EUR/USD wijkt {diff:.2f}% af tussen Frankfurter en Swissquote")
    conversion = second if second is not None else primary
    conversion_name = "Swissquote EUR/USD" if second is not None else "ECB via Frankfurter"
    payload = {
        "waarde": num(primary, 6),
        "bron": "ECB via Frankfurter",
        "tijdstempel_utc": primary_stamp,
        "tweede_bron": "Swissquote",
        "tweede_bron_bevestigd": confirmed,
        "afwijking_pct": num(diff, 4),
    }
    used_stamp = second_stamp if second is not None else primary_stamp
    return payload, conversion, f"{conversion_name}" if conversion is not None else None, used_stamp


def collect_metals(errors: list[str], fx_rate: float | None, fx_name: str | None, fx_stamp: str | None) -> dict:
    metals: dict = {}
    for code, future in METALS:
        spot, spot_stamp, spot_error = None, None, None
        data, error = fetch_json(f"https://api.gold-api.com/price/{code}")
        if error or not isinstance(data, dict):
            spot_error = error or "gold-api gaf geen object"
        else:
            spot = num(data.get("price"), 6)
            updated = data.get("updatedAt")
            spot_stamp = updated if isinstance(updated, str) else None
            if spot is None:
                spot_error = "gold-api mist een prijs"
        if spot_error:
            errors.append(f"{code} gold-api: {spot_error}")
        second, second_stamp, second_error = swissquote_mid(f"{code}/USD")
        if second_error:
            errors.append(f"{code} Swissquote: {second_error}")
        diff = deviation_pct(spot, second) if spot is not None and second is not None else None
        confirmed = diff is not None and diff <= 1
        if diff is not None and diff > 1:
            errors.append(f"{code} wijkt {diff:.2f}% af tussen gold-api en Swissquote")
        eur = None
        if spot is not None and fx_rate:
            eur = spot / fx_rate
        elif spot is not None:
            errors.append(f"{code} niet omgerekend: geen EUR/USD")
        change = None
        change_source = None
        meta, yahoo_error = yahoo_chart(future)
        if yahoo_error:
            errors.append(f"{code} verandering {future}: {yahoo_error}")
        elif meta is not None:
            change = num(meta.get("regularMarketChangePercent"), 4)
            change_source = f"Yahoo Finance {future} (slot tegenover vorig slot)"
        bron = "gold-api.com"
        if fx_name and eur is not None:
            bron = f"gold-api.com, omgerekend met {fx_name}"
        metals[code] = {
            "eur_per_oz": num(eur, 4),
            "verandering_24u_pct": change,
            "verandering_bron": change_source,
            "bron": bron,
            "tijdstempel_utc": spot_stamp or second_stamp or fx_stamp,
            "tweede_bron": "Swissquote",
            "tweede_bron_bevestigd": confirmed,
            "afwijking_pct": num(diff, 4),
        }
    return metals


def gecko_rows() -> tuple[list | None, str | None]:
    url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        "?vs_currency=eur&order=market_cap_desc&per_page=50&page=1&sparkline=false"
    )
    data, error = fetch_json(url)
    if error or not isinstance(data, list):
        return None, error or "CoinGecko gaf geen lijst"
    return data, None


def paprika_by_symbol() -> tuple[dict[str, dict] | None, list[dict] | None, str | None]:
    data, error = fetch_json("https://api.coinpaprika.com/v1/tickers?quotes=EUR")
    if error or not isinstance(data, list):
        return None, None, error or "CoinPaprika gaf geen lijst"
    ranked = [row for row in data if isinstance(row, dict) and isinstance(row.get("rank"), int)]
    ranked.sort(key=lambda row: row["rank"])
    filtered = []
    by_symbol: dict[str, dict] = {}
    for row in ranked:
        symbol = str(row.get("symbol") or "")
        name = str(row.get("name") or "")
        if is_derivative(symbol, name, str(row.get("id") or "")):
            continue
        filtered.append(row)
        by_symbol.setdefault(symbol.upper(), row)
    return by_symbol, filtered[:10], None


def coin_price_eur(row: dict) -> float | None:
    quotes = row.get("quotes")
    if not isinstance(quotes, dict):
        return None
    eur = quotes.get("EUR")
    if not isinstance(eur, dict):
        return None
    return num(eur.get("price"), 8)


def collect_crypto(errors: list[str]) -> dict:
    gecko, gecko_error = gecko_rows()
    paprika, paprika_top, paprika_error = paprika_by_symbol()
    if gecko_error:
        errors.append(f"Crypto CoinGecko: {gecko_error}")
    if paprika_error:
        errors.append(f"Crypto CoinPaprika: {paprika_error}")

    top: list[dict] = []
    if gecko is not None:
        for coin in gecko:
            if not isinstance(coin, dict):
                continue
            symbol = str(coin.get("symbol") or "")
            name = str(coin.get("name") or "")
            if is_derivative(symbol, name, str(coin.get("id") or "")):
                continue
            top.append(coin)
            if len(top) == 10:
                break
    elif paprika_top is not None:
        for row in paprika_top:
            top.append(row)
            if len(top) == 10:
                break

    gecko_symbols = [str(coin.get("symbol") or "").upper() for coin in top] if gecko is not None else []
    paprika_symbols = [str(row.get("symbol") or "").upper() for row in (paprika_top or [])]
    ranking_ok = bool(gecko_symbols) and set(gecko_symbols) == set(paprika_symbols)
    if gecko_symbols and paprika_symbols and not ranking_ok:
        errors.append(
            "Crypto-ranglijst wijkt af. CoinGecko: "
            + ", ".join(gecko_symbols)
            + ". CoinPaprika: "
            + ", ".join(paprika_symbols)
        )

    published = []
    diffs: list[float] = []
    missing_second = False
    for index, coin in enumerate(top, start=1):
        if gecko is not None:
            symbol = str(coin.get("symbol") or "").upper()
            name = str(coin.get("name") or "")
            price = num(coin.get("current_price"), 8)
            change = num(coin.get("price_change_percentage_24h"), 4)
            cap = coin.get("market_cap")
            cap_eur = int(cap) if isinstance(cap, (int, float)) and math.isfinite(cap) else None
            stamp = coin.get("last_updated")
            stamp = stamp if isinstance(stamp, str) else None
            source = "CoinGecko"
        else:
            symbol = str(coin.get("symbol") or "").upper()
            name = str(coin.get("name") or "")
            price = coin_price_eur(coin)
            quote = coin.get("quotes", {}).get("EUR", {}) if isinstance(coin.get("quotes"), dict) else {}
            change = num(quote.get("percent_change_24h"), 4) if isinstance(quote, dict) else None
            cap = quote.get("market_cap") if isinstance(quote, dict) else None
            cap_eur = int(cap) if isinstance(cap, (int, float)) and math.isfinite(cap) else None
            stamp = coin.get("last_updated")
            stamp = stamp if isinstance(stamp, str) else None
            source = "CoinPaprika"
        second_price = None
        if paprika and symbol in paprika and gecko is not None:
            second_price = coin_price_eur(paprika[symbol])
        elif gecko is not None:
            missing_second = True
        diff = deviation_pct(price, second_price) if price is not None and second_price is not None else None
        if diff is not None:
            diffs.append(diff)
            if diff > 1:
                errors.append(f"Crypto {symbol} wijkt {diff:.2f}% af tussen CoinGecko en CoinPaprika")
        published.append({
            "rang": index,
            "symbool": symbol,
            "naam": name,
            "eur": num(price, 6 if (price or 0) < 1 else 2),
            "verandering_24u_pct": change,
            "marktwaarde_eur": cap_eur,
            "bron": source,
            "tijdstempel_utc": stamp,
            "afwijking_pct": num(diff, 4),
        })

    prices_ok = bool(diffs) and all(item <= 1 for item in diffs) and not missing_second
    if missing_second:
        errors.append("Crypto: niet elke munt uit de top 10 staat bij CoinPaprika")
    return {
        "ranglijst_bevestigd": ranking_ok,
        "tweede_bron_bevestigd": prices_ok,
        "max_afwijking_pct": num(max(diffs), 4) if diffs else None,
        "top10": published,
    }


def collect_indices(errors: list[str]) -> dict:
    indices: dict = {}
    without_second_source: list[str] = []
    for code, yahoo_symbol, second_pair in INDICES:
        meta, error = yahoo_chart(yahoo_symbol)
        if error or meta is None:
            errors.append(f"{code} Yahoo: {error or 'geen koers'}")
            indices[code] = empty_index()
            continue
        price = num(meta.get("regularMarketPrice"), 4)
        change = num(meta.get("regularMarketChangePercent"), 4)
        traded = meta.get("regularMarketTime")
        traded_stamp = unix_to_utc(traded) if isinstance(traded, (int, float)) else None
        status = market_status(meta)
        if status is None:
            errors.append(f"{code}: marktstatus niet in het Yahoo-antwoord")
        currency = meta.get("currency") if isinstance(meta.get("currency"), str) else None
        second = None
        second_error = None
        if second_pair:
            second, _, second_error = swissquote_mid(second_pair)
            if second_error:
                errors.append(f"{code} Swissquote: {second_error}")
        else:
            without_second_source.append(code)
        diff = deviation_pct(price, second) if price is not None and second is not None else None
        confirmed = diff is not None and diff <= 1
        if diff is not None and diff > 1:
            errors.append(f"{code} wijkt {diff:.2f}% af tussen Yahoo en Swissquote")
        indices[code] = {
            "stand": price,
            "valuta": currency,
            "verandering_24u_pct": change,
            "marktstatus": status,
            "laatste_handel_utc": traded_stamp,
            "bron": "Yahoo Finance",
            "tweede_bron": "Swissquote" if second_pair else None,
            "tweede_bron_bevestigd": confirmed,
            "afwijking_pct": num(diff, 4),
        }
    if without_second_source:
        errors.append(
            ", ".join(without_second_source)
            + ": geen gratis tweede bron zonder sleutel (Stooq geeft 404 of een browsercontrole)"
        )
    return indices


def empty_index() -> dict:
    return {
        "stand": None,
        "valuta": None,
        "verandering_24u_pct": None,
        "marktstatus": None,
        "laatste_handel_utc": None,
        "bron": None,
        "tweede_bron": None,
        "tweede_bron_bevestigd": False,
        "afwijking_pct": None,
    }


def build() -> dict:
    errors: list[str] = []
    fx, fx_rate, fx_name, fx_stamp = collect_fx(errors)
    return {
        "versie": 1,
        "gegenereerd_utc": utc_now(),
        "eur_usd": fx,
        "koers_voor_omrekening": {
            "waarde": num(fx_rate, 6),
            "bron": fx_name,
            "tijdstempel_utc": fx_stamp,
        },
        "metalen": collect_metals(errors, fx_rate, fx_name, fx_stamp),
        "crypto": collect_crypto(errors),
        "indices": collect_indices(errors),
        "fouten": errors,
    }


def write_output(payload: dict) -> None:
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT)


def remember(line: str) -> None:
    existing = LOG.read_text(encoding="utf-8").splitlines() if LOG.exists() else []
    existing.append(line)
    LOG.write_text("\n".join(existing[-400:]) + "\n", encoding="utf-8")


def gh_command() -> str | None:
    found = shutil.which("gh")
    if found:
        return found
    candidate = os.environ.get("ProgramFiles", r"C:\Program Files") + r"\GitHub CLI\gh.exe"
    return candidate if Path(candidate).exists() else None


def push_json(stamp: str) -> str | None:
    """Zet markten.json via de GitHub-API. gh gebruikt de al aanwezige login."""
    gh = gh_command()
    if gh is None:
        return "gh niet gevonden"
    repo = "repos/MatthiasVanEnk/markten-feed/contents/markten.json"
    current = subprocess.run(
        [gh, "api", repo, "--jq", ".sha"],
        capture_output=True, text=True, timeout=60,
    )
    if current.returncode != 0:
        return (current.stderr or current.stdout).strip() or "sha ophalen mislukt"
    body = {
        "message": f"koersen {stamp}",
        "content": base64.b64encode(OUTPUT.read_bytes()).decode("ascii"),
        "sha": current.stdout.strip(),
        "branch": "main",
    }
    updated = subprocess.run(
        [gh, "api", "--method", "PUT", repo, "--input", "-"],
        input=json.dumps(body),
        capture_output=True, text=True, timeout=60,
    )
    if updated.returncode != 0:
        return (updated.stderr or updated.stdout).strip() or "publiceren mislukt"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Haal marktkoersen op en schrijf markten.json")
    parser.add_argument("--push", action="store_true", help="commit en push markten.json als dit een git-repo is")
    args = parser.parse_args()
    try:
        payload = build()
        write_output(payload)
    except Exception as exc:
        print(f"bestand niet geschreven: {exc}")
        return 1
    stamp = payload["gegenereerd_utc"]
    print(f"geschreven {OUTPUT} ({len(payload['fouten'])} fouten)")
    exit_code = 0
    if args.push:
        problem = push_json(stamp)
        if problem:
            print(f"push mislukt: {problem}")
            exit_code = 2
        else:
            print("push gelukt")
    remember(f"{stamp} exit={exit_code} fouten={len(payload['fouten'])}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
