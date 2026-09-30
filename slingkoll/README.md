# Slingkoll

Home Assistant-tillägg som tar reda på vilken golvvärmeslinga varje termostat
faktiskt styr. Användarinstruktioner finns i [DOCS.md](DOCS.md).

## Metoden

Varje termostat ställs på 28 °C (slingan öppen) eller 10 °C (stängd) enligt ett
D-optimalt, balanserat mönster (`design.py`). Varje slinga är på i hälften av
faserna, och inga två slingor följs åt mer än slumpen tillåter. Varje rums
temperatur modelleras (`analysis.py`) som

```
dT/dt = Σ_j b_j · golv_j(t) + a · (T_ute − T) + c · (T_övriga − T) + k + sol(tid på dygnet)
```

där `golv_j` är slinga j:s ventilläge gånger hur varmt framledningsvattnet är,
fördröjt av golvet (första ordningens filter, fördröjningen anpassas per rum:
betongplatta timmar, trägolv under en timme). Temperaturerna medelvärdesbildas
per kvart, vilket tar bort det mesta av termostaternas avrundning till tiondels
grader. `b_j ≥ 0` skattas med minsta kvadrat under icke-negativitetsvillkor och
felgränserna vidgas för autokorrelation i residualerna. Rummet med störst
`b_j` är rummet slinga j ligger i. Beskedet är *säkert* när skillnaden mot
näst bästa rum är minst tre standardfel.

`runner.py` kör testet minut för minut: skriver börvärden, pausar vid
komfortgränserna, höjer värmepumpens börvärde om framledningen är för sval,
och återställer allt vid avslut, avbrott eller när tillägget stängs.

## Utveckling

```bash
cd slingkoll
pip install numpy pytest ruff
pytest -q
python -m slingkoll --demo --port 8124   # simulerat hus, ett dygn på ett par minuter
```

Demohuset (`sim.py`) har en förväxlad slinga mellan två rum, en trevägsförväxling
på övervåningen och en termostat vars slinga värmer hallen. Testerna kör ett
helt test mot det och kräver att alla hittas och att allt återställs.

Inga beroenden utöver numpy: webbservern och Home Assistant-klienten använder
standardbiblioteket, så tillägget installeras på under en minut.
