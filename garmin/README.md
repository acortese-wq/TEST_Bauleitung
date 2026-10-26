# Trainingscockpit (Garmin Health API)

Lokales Dashboard für Aktivitäten, Trainingsumfang, Lauf-Pace, Ruhepuls, Schlaf und Schritte.
Die Daten holt `sync.py` über die offizielle Garmin Health/Activity API. Alles bleibt auf deinem Rechner.

## Einrichtung (einmalig)

```bash
cd garmin
pip install -r requirements.txt
cp .env.example .env        # Consumer Key und Secret aus dem Garmin Developer Portal eintragen
python sync.py auth         # Browser öffnet die Garmin-Freigabe, Token wird lokal gespeichert
```

## Daten holen

```bash
python sync.py pull --days 365   # erster Abgleich
python sync.py pull              # danach inkrementell (ab letztem Sync)
```

Danach `index.html` im Browser öffnen (Doppelklick genügt). Ohne Sync zeigt die Seite
generierte Beispieldaten und kennzeichnet sie oben rechts als solche.

## Was wo liegt

| Datei | Inhalt | Im Git? |
|---|---|---|
| `index.html` | Dashboard | ja |
| `sync.py` | OAuth 1.0a und Pull von `activities`, `dailies`, `sleeps` | ja |
| `.env` | Consumer Key/Secret | nein |
| `.garmin-token.json` | Benutzer-Token nach `auth` | nein |
| `data/` | Cache und `garmin-data.js` für das Dashboard | nein |

## Hinweise zur API

- Pull erfolgt in 24-h-Fenstern nach Upload-Zeit (`uploadStartTimeInSeconds`/`uploadEndTimeInSeconds`).
- Garmin verlangt bei produktiven Keys teilweise Ping/Push statt freiem Pull. Meldet die API
  `InvalidPullToken`, im Developer Portal die Pull-/Ping-Konfiguration prüfen oder mit dem Evaluation-Key testen.
- Falls deine Programmversion andere Endpunkte oder OAuth 2.0 vorgibt, lassen sich die URLs in `.env` überschreiben.
