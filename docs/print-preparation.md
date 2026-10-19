# Per-print tools and fallback spools

Starting a multi-tool job holds file transmission while RME reviews its tools.
The primary-tool suggestions match material first and minimize RGB color
distance without assigning one physical tool twice. Fallback buttons offer
only compatible loaded materials, sorted by closest color, excluding the
job's primary tools and already-selected backups. Select several in order;
click a selected backup to remove it. Different colors remain an explicit
user choice; suggestions do not automatically assign backups.

Apply tools and fallbacks queues the tool map, resets previous SpoolJoin chains,
adds this job's ordered chains, then releases the print. An empty selection
means no fallback for this print. Supported printers always require fallback
review, even when automatic primary mapping prompts are disabled. Configure
all backups before the preparation G-code; adding them later cannot redo
calibration that has already finished.

For local jobs whose OctoPrint upload date is less than 24 hours old, valid
current tool assignments with matching material and known requested color
can skip directly to fallback review. Missing metadata, old/future dates,
missing material, duplicate tools or color mismatches keep primary review
visible. Review or change primary tools opens it again. This is based on the
upload date, not the time a file was selected. The fallback review is never
bypassed or auto-confirmed by its timer.

Companion 6.9.0/6.10.1 firmware expands M976 batch and explicit G427 tool lists
using the live SpoolJoin chains. Auto PA Auto mode reuses each tool's valid
cache; On forces calibration and Off skips it. Select the mode under
Settings → RME Compatibility → Auto PA mode, or RME print controls in the
OctoPrint Control tab. The selection immediately saves on the printer.

Opening Settings no longer triggers a full RME configuration refresh or a
USB scan. Existing live state is shown immediately; periodic tune polling
and explicit refresh controls remain available. This removes an identified
source of extra work, not proof that every source of browser delay is fixed.
