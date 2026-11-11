# CC2 CANVAS tray filament info write — protocol evidence (2026-11-11)

Source: Elegoo's own published LAN web client, `elegooofficial/elegoo-fdm-web` release 20260625,
asset `lan_service_web.zip` → `index.html` (3,605,472 bytes), unpacked at
`~/scratch/tmp/cc2-tray-2026-11-11/gh-release/unz/index.html`. All quotes below are from that bundle.
The bundle the printer itself serves at `http://10.88.88.190:8080/webapp/lan/index.html`
(11,170,761 bytes) is an older build that does not contain `SetFilamentInfo` at all (0 hits) —
it still speaks the older `Cmd*` websocket namespace (`CmdAMSSetFilamentInfo=501`), which proves
nothing about method 2003 on this firmware. Nothing in this folder was sent to the printer.

## Method enum (same enum that names SetAutoFillFilament=2004 / GetCanvasInfo=2005)

```
SetFilamentInfo=2003        -> CANVAS tray filament info (the one we want)
SetMonoFilamentInfo=1055    -> single-spool printers without CANVAS (not for the CC2 with CANVAS)
GetMonoFilamentInfo=1061
```

Toast on nonzero error_code: `SetFilamentInfoFailed: "Failed to set filament information. "`.

## Request shape

Store wrapper (verbatim):
```
async function lne(Te){ ... const Be={method:Pt.SetFilamentInfo,params:Te}; ...
  qe=await dn.setFilamentInfoCanvas(a.value,Be); qe.error_code!==0 ? <toast SetFilamentInfoFailed> : true
```
`dn.setFilamentInfoCanvas(n,r){return this.request(n,r)}` — no timeout override passed, so that
request layer's 5 s default applies (same as 2004; the HA integration waits 10 s per command).

The params object the edit dialog builds (verbatim, two identical copies in the bundle):
```
{canvas_id:0, tray_id:0,
 brand: <brand string chosen in the dialog, e.g. "Generic" or "ELEGOO">,
 filament_type: bi(name).type,          // e.g. "PLA"
 filament_name: name,                   // catalog key, e.g. "PLA", "PLA+", "PETG"
 filament_code: bi(name).code,          // e.g. "0x0000"
 filament_color: <"#RRGGBB" picked or current tray colour>,
 filament_min_temp: bi(name).temperature.min,
 filament_max_temp: bi(name).temperature.max}
```
Then the slot page's save handler overwrites the ids before sending:
```
Z=async fe=>{ const J=fe; J.canvas_id=h.value; J.tray_id=Number(r.value); ... await e.setFilamentInfoCanvas(J) ... }
```
So the wire is: `{"method":2003,"params":{"canvas_id":<int>,"tray_id":<int>,"brand":<str>,
"filament_type":<str>,"filament_name":<str>,"filament_code":<"0x...">,"filament_color":"#RRGGBB",
"filament_min_temp":<int>,"filament_max_temp":<int>}}` — nine fields, all sent every time.

Note the asymmetry with the 2005 read: the printer REPORTS `min_nozzle_temp`/`max_nozzle_temp`
and `filament_color`/`filament_type`/`filament_name`/`brand`, but the WRITE spells the temps
`filament_min_temp`/`filament_max_temp` and adds `filament_code`.

`bi(name)` is a lookup in the catalog `jd` (50 entries, extracted to `filament-catalog.txt` in this
folder as (key, type, name, code, min, max, elegoo, generic)). Unknown name → `{type:"",name:"",code:"",temperature:{min:0,max:0}}`.
Brand candidates offered by the dialog: "ELEGOO" (entries with elegoo:true) and "Generic" (generic:true),
plus free text; `v(W,V)` only filters the catalog by brand, it does not validate free-text brands.

Colour palette the dialog offers (any hex is accepted by the printer as far as the client shows):
`#FFFFFF #FFF242 #DBF47A #09CC3A #077747 #0B6283 #0BE2A0 #74D9F3 #48A7FA #2850DF #433089 #A03BF7
#F32FF8 #D4B1DD #F95D77 #F72221 #7C4C00 #F88D36 #FCEBD7 #D2C5A3 #AF7832 #898989 #BCBCBC #000000`.

## Gates the official UI applies
The edit dialog is reachable from the slot page whose load/unload buttons are disabled by
`!(e.isIdle||e.isMainHoming||e.isPaused||e.isFilamentInterruption)`; the edit icon itself
(`M=()=>{t.value=!0 ...}`) has no visible gate in the bundle. Treat idle-only as our own conservative
rule, not the vendor's.

## What is NOT established
- Whether this firmware (02.01.00.00) answers 2003 at all, and what it does with an unknown
  `filament_code` or a free-text brand. Never offered by anyone on this project.
- Whether the 2005 read reflects the change immediately (the auto-refill readback lagged).
- Whether a tray edit is accepted while a print runs.

## Current printer state at time of writing (2005 via get_printer_slots, 2026-11-11)
A0 Generic PLA #000000 190-230 preloaded; A1 Generic ASA-CF #2850DF 240-280 preloaded;
A2 Generic PETG #000000 230-260 preloaded; A3 ELEGOO PETG #000000 230-260 preloaded.
Nathan says physically: A1 = grey PLA, A2 = black PLA (he swapped spools without updating the trays).
