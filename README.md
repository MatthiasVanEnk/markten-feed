# Markten-feed

Publieke uurlijkse koersen. `markten.json` bevat alleen marktgegevens, geen persoonsgegevens en geen sleutels.

De bedragen van metalen en crypto staan in EUR. Indexstanden blijven in hun eigen valuta (`valuta` in het bestand). `tweede_bron_bevestigd` is true als een tweede bron minder dan 1% afwijkt. Een regel in `fouten` betekent dat een bron ontbreekt of te ver uit elkaar loopt; de koers die er wel staat is van de genoemde `bron`, niet een oude waarde.

`eur_usd.waarde` is de ECB-dagkoers via Frankfurter (`soort`: ECB-dagkoers). De tweede bron is de actuele Swissquote-middenkoers. `afwijking_pct` zet die twee naast elkaar; dat zijn niet twee metingen van hetzelfde moment.

De 24-uursverandering van een metaal komt uit het bijbehorende future (slot tegenover het vorige slot), niet uit de spotkoers. Bij een index zegt `verandering_label` of dat de lopende handelsdag is of de laatste.

In `crypto.top10` blijft een stablecoin staan met `type` `stablecoin`. Verpakte, gestakete en gekoppelde tokens, zoals een HELOC-token, staan er niet in. Een tijdstempel ouder dan twee uur levert een regel in `fouten`.

Ruwe link:

`https://raw.githubusercontent.com/MatthiasVanEnk/markten-feed/main/markten.json`
