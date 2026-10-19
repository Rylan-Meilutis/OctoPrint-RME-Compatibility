# Printer-to-provider spool synchronization workbook

Printer-side selections carrying a known profile alias automatically select
the corresponding provider database ID. No manual sync is required for an
unambiguous known alias, including aliases absent from the seven preset slots.
Material and color alone do not identify a physical spool.

Internal profile identifiers must survive display formatting unchanged. RME
labels show material with the existing color swatch, not the profile suffix.

Validation:

- Regression: import PLA-00D into tool 7 with an empty published profile list.
- Regression: reconnect still reads M865 Q with a pending provider-sync prompt.
- Regression: printer imports do not write provider assignments back mid-snapshot.
- DOM: material-only labels retain raw aliases and existing mapping controls.
- Regression: the eighth ad-hoc profile resolves to PLA and its exact database
  ID in both fresh state and a restored state containing only the seven menu
  presets. This does not guess from the selected spool or overwrite a tool.
- Mapping choices render 30 matches per tool at a time. Search covers the full
  material-matching inventory, including later pages; Show more adds 30 choices.
- Hardware pending: choose a known spool on the LCD, verify SpoolManager's tool
  selection changes without pressing sync, then verify usage charges that spool.
- Hardware pending: repeat after reconnect with more than seven inventory spools.
- Unknown/new or ambiguous spool identities still require user resolution.
