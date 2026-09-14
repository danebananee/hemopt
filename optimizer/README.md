# hemopt — kostnadsoptimering av värme, varmvatten och effekttoppar

Schemaläggare som flyttar husets elförbrukning till billiga timmar utan att
tappa komforten, och som håller nere den effektavgift som alla svenska
nätbolag ska ha infört senast 1 januari 2027.

Tjänsten läser rumstemperaturer från Home Assistant, hämtar Nordpools
spotpriser, lär sig husets tröghet och varmvattenvanor, och räknar fram ett
schema för det kommande dygnet. Schemat skrivs tillbaka som börvärden till
termostaterna.

```
Nordpool spotpris ─┐
Home Assistant ────┼─→ [ modeller ] → [ MILP-optimerare ] → börvärden → Home Assistant
Effekttoppsmätning ┘                                      └→ MQTT-entiteter
```

## Vad den gör

**Flyttar last till billiga timmar.** Huset är ett värmelager. Optimeraren
laddar det när elen är billig och låter det coasta när den är dyr, inom de
komfortgränser du satt.

**Håller effekttopparna nere.** Effektavgiften debiteras som medelvärdet av de
N högsta *timmedeleffekterna* på olika dygn under månaden. Bara en topp som
slår den nuvarande N:te högsta kostar något, så optimeraren planerar mot den
tröskeln i stället för mot ett absolut tak. Den vet också att natt, helg,
röda dagar och sommarhalvåret är gratis.

**Bevakar timmen som pågår.** Eftersom mätaren integrerar över hela klocktimmen
räknar tjänsten löpande ut hur mycket energi som är kvar att spendera innan
timmedelvärdet slår i taket.

**Prioriterar rum.** Varje rum får prioritet 1–5. Höga prioriteter håller sin
temperatur, låga får svaja och bär lastflytten.

**Lär sig huset.** Tidskonstanten per rum, uppvärmningstakten och
varmvattenvanorna identifieras ur Home Assistants historik.

## Snabbstart

```bash
cd optimizer
uv sync
uv run hemopt --demo serve --port 47318
```

Öppna <http://127.0.0.1:47318>. Demoläget kör den riktiga optimeraren mot
verkliga SE3-priser men ett simulerat hus, så inget behöver vara inkopplat.

## Skarp installation

### 1. Skapa konfigurationen

```bash
uv run hemopt example-config > config.yaml
```

Fyll i en [långlivad access-token](https://www.home-assistant.io/docs/authentication/#your-account-profile)
och entitets-ID:n. Minsta uppsättning som behövs:

| Fält | Vad det är |
| --- | --- |
| `rooms[].temperature_entity` | Rummets temperaturgivare |
| `rooms[].climate_entity` | Termostaten vars börvärde ska styras |
| `heat_pump.outdoor_entity` | Utetemperatur |
| `heat_pump.power_entity` | Värmepumpens effekt |
| `base_load.total_power_entity` | Husets totala effekt, för effekttoppar |
| `hot_water.top_temperature_entity` | Varmvattenberedarens toppgivare |

### 2. Kontrollera modellerna innan du släpper in styrningen

```bash
uv run hemopt -c config.yaml train
uv run hemopt -c config.yaml plan
```

`train` skriver ut tidskonstanten per rum. Ett normalt hus landar på 40–150
timmar. Rum som visar `default` har inte tillräckligt med historik än.

### 3. Kör tjänsten

```bash
uv run hemopt -c config.yaml serve --host 0.0.0.0 --port 47318
```

Styrningen är avstängd tills du slår på **Styr värmen**, i webbpanelen eller
via switchen i Home Assistant. Kör gärna några dygn med styrningen av och
jämför den planerade kurvan mot verkligheten först.

## Effektavgiften

Modellen konfigureras under `peak_tariff` eftersom nätbolagen räknar olika.

```yaml
peak_tariff:
  n_peaks: 5              # antal toppar som snittas
  price_per_kw_sek: 67.5  # kr per kW och månad
  window:
    months: [1, 2, 3, 11, 12]
    hour_start: 7
    hour_end: 21
    weekdays_only: true
```

Defaultvärdena följer Vattenfall Eldistributions modell: snittet av de fem
högsta topparna på olika dygn, mätt helgfria vardagar 07–21 under november
till mars. Vattenfall pausade breddinförandet efter regeringens besked i mars
2026, men kravet på en effektbaserad avgift gäller alla nätbolag senast
1 januari 2027. Kolla din egen nätfaktura och justera.

Sätt `enabled: false` om du inte har någon effektavgift än. Då optimeras bara
mot spotpriset.

## Home Assistant-entiteter

Med MQTT påslaget dyker enheten **Kostnadsoptimering** upp via autodiscovery:

| Entitet | Innehåll |
| --- | --- |
| `sensor.hemopt_total_price` | Aktuellt totalpris inklusive skatt och nätavgift |
| `sensor.hemopt_peak_threshold` | Tröskeln en ny topp måste slå för att kosta |
| `sensor.hemopt_hour_headroom` | Effekt kvar att ta ut denna timme |
| `sensor.hemopt_peak_cost` | Prognos för månadens effektavgift |
| `sensor.hemopt_planned_saving` | Besparing mot att inte styra alls |
| `sensor.hemopt_inertia_<rum>` | Uppmätt tröghet i timmar |
| `binary_sensor.hemopt_heating_blocked` | Planen vill inte köra kompressorn nu |
| `binary_sensor.hemopt_peak_guard` | Effektvakten begränsar just nu |
| `switch.hemopt_control_enabled` | Släpper in styrningen |
| `number.hemopt_priority_<rum>` | Rummets prioritet, 1–5 |

Dashboardvyn ligger i `../dashboards/kostnadsoptimering.yaml`.

## Var modellen kan köra

| Plattform | Duger den? |
| --- | --- |
| **Raspberry Pi 4/5** | Ja, detta är måltavlan. En plan tar under en sekund. |
| Raspberry Pi 3 | Ja, men höj `solver_time_limit_s` och kör 30-minuterssteg. |
| Home Assistant-servern | Ja, om den har ett par hundra MB minne över. |
| **ESP32 / STM32** | Nej. HiGHS och NumPy får inte plats och behövs inte där. |

Optimeringen har några tusen variabler och kräver en MMU, en Python-runtime
och några hundra MB RAM. En ESP32 kan däremot vara utmärkt som *utförare*:
låt Pi:n räkna fram schemat och låt mikrokontrollern hålla en enkel regel,
till exempel att bryta EXT-ingången när effektvakten säger till. Kontrollerna
som behövs för det publiceras redan över MQTT.

### Raspberry Pi

```bash
sudo apt install -y python3-venv
git clone <repo> && cd <repo>/optimizer
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
```

Som systemtjänst:

```ini
# /etc/systemd/system/hemopt.service
[Unit]
Description=hemopt
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/hemopt/optimizer
ExecStart=/home/pi/.local/bin/uv run hemopt -c config.yaml serve --host 0.0.0.0 --port 47318
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now hemopt
```

## Så fungerar optimeringen

Varje rum modelleras som ett RC-nät:

```
dT/dt = (T_ute - T_inne) / tau + k_värme · u + k_gain
```

`tau` är trögheten i timmar, `u` hur öppen slingan är och `k_gain` sol- och
internlaster. Alla tre identifieras med minstakvadratanpassning mot historiken,
med icke-negativa koefficienter så att en brusig vecka inte kan producera ett
rum som kyls av att värmas.

Schemat löses som ett linjärprogram över 36 timmar i kvartssteg. Målfunktionen
är

```
energikostnad + effektavgift + komfortavvikelse − värdet av lagrad värme
```

Komfortavvikelsen viktas med rummets prioritet, vilket är det som gör
prioriteringen till ett ekonomiskt val i stället för en hård regel.
Terminalvärdet krediteras till horisontens lägsta pris, så det aldrig kan
motivera ett inköp som inte lönar sig på egen hand.

Värmepumpen delar sin kapacitet mellan radiatorer och beredare i stället för
att låsas till det ena per steg. Det är den korrekta modellen för ett
kvartssteg, eftersom trevägsventilen hinner växla flera gånger inom steget,
och det håller problemet linjärt.

## Utveckling

```bash
uv run pytest        # 76 tester
uv run ruff check .
uv run ruff format .
```

Testerna verifierar bland annat att last flyttas från dyra timmar, att
förvärmning sker före en prisspik, att högprioriterade rum skyddas på
bekostnad av lågprioriterade, att effektavgiften plattar ut de debiterbara
timmarna men inte natten, och att en identifierad tidskonstant matchar den
som simuleringen genererades med.

## Datakällor

Spotpriser hämtas från [elprisetjustnu.se](https://www.elprisetjustnu.se/elpris-api),
som levererar Nordpools dagen-före-priser gratis, med kvartsupplösning sedan
1 oktober 2025. Morgondagens priser publiceras tidigast kl. 13. Innan dess
extrapolerar planeraren från dagens profil och märker planen med det.
