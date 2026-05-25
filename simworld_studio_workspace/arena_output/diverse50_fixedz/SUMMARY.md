# Fix Pass Summary (2026-04-22)

## Results
- 65/65 maps processed
- Fix logic: BP_ actors only, abs(actor.z - ps_z) <= 15 → move to ps_z-100

## Maps needing manual PlayerStart (no PS found):
- map_02_village_day
- map_03_watertown_remix
- map_04_courtyard_remix
- map_05_middleeast_native
- map_06_hwaseong_native
- map_09b_wintertown_demo02

## Notable fixZ counts:
map_16_hwaseong_remix: 1236
map_65_hwaseong_ceremonial: 1216
map_48_hwaseong_cleared: 1212
map_47_wintertown_demo01_minimal: 708
map_13_watertown_native: 831
map_12_village_day_remix: 136
map_31_gothic_day_remix: 225
map_57_gothic_day_invasion: 249
map_29_village_night_remix: 166

## Agent actors deleted:
map_55_trainstation_big_native: 3
map_62_trainstation_cross_remix: 3
map_05_middleeast_native: 2

## Maps skipped (PS_Z ≈ 0, objects already at ground Z):
map_11, 21, 58, 59, 60 and others with abs(ps_z)<50

## Reach=0/20 maps (navmesh issue, not related to Z fix):
map_02, 04, 15, 25, 29, 39, 46, 61, 62, 66, 68
