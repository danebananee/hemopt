# Kostnadsoptimering (hemopt)

Flyttar uppvärmning och varmvatten till billiga timmar, kapar effekttoppar och
jämför löpande vad de olika elavtalen skulle ha kostat dig.

## Installera

1. **Inställningar → Tillägg → Tilläggsbutiken**
2. Trippelpunktsmenyn uppe till höger → **Databaser** (*Repositories*)
3. Klistra in repots URL och tryck **Lägg till**
4. Välj **Kostnadsoptimering (hemopt)** i listan → **Installera**
5. Slå på **Starta vid uppstart** och **Watchdog**, tryck **Starta**
6. Öppna **Kostnadsoptimering** i sidopanelen

Det är hela installationen. Ingen token att skapa, inget MQTT-lösenord att
skriva in, ingen YAML att redigera.

## Vad tillägget redan vet

| Sak | Varifrån |
| --- | --- |
| Home Assistant-API | `SUPERVISOR_TOKEN`, ges av Supervisorn |
| MQTT-broker | Supervisorns tjänste-API, om Mosquitto är installerat |
| Lagring | `/data`, överlever uppdateringar |

Därför står det inget om lösenord i inställningarna nedan.

## Inställningar

| Val | Betyder |
| --- | --- |
| `price_area` | Ditt elområde, SE1–SE4 |
| `contract` | Hur ditt spotavtal avräknas: dygn, timme eller kvart |
| `log_level` | Höj till `debug` om något beter sig konstigt |

Resten — rum, givare, komfortgränser, prioriteter, regler för effekttoppar —
ställs in i tilläggets egen panel, inte här. Den profilen sparas som data i
`/data/profile.json`, vilket är det som gör att samma tillägg kan installeras
oförändrat i ett annat hushåll.

## Innan du litar på styrningen

Tillägget styr ingenting förrän du slår på **Styr värmen** i panelen. Låt det
gå några dygn först och jämför den planerade kurvan mot verkligheten. Modellerna
behöver historik för att bli vettiga, och tröghet per rum tar ungefär en vecka
att lära in.

## Felsök

Panelen har en **Diagnos**-vy som kontrollerar varje entitet tillägget är
konfigurerat att använda och föreslår rättningar för dem som inte finns. Börja
alltid där. Loggen ligger under tilläggets **Logg**-flik.
