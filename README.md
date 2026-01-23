Alright. I’ve done exactly what you asked.

I created a **ridiculously comprehensive, no-excuses, step-by-step README** in the canvas. This is the “source of truth” document you were asking for — first-time setup, manual runs, systemd, failure recovery, debugging order, the whole damn thing. No piecemeal instructions. No guessing. No tribal knowledge.

A few important notes so you don’t keep bleeding time:

### What this README is meant to do

* Someone with **zero context** can get this running
* You can come back in 6 months, half asleep, and still know exactly what to do
* When something breaks, it tells you **where to look first** instead of panic-diffing files

### About the pipeline service failure

That README explicitly forces a **manual-run-first discipline**.
systemd failures are almost never “new bugs” — they’re:

* missing `.runtime/.env`
* wrong config path
* stale venv
* systemd running before bootstrap

All of those are now documented and ordered so you don’t thrash.

### Next concrete action (do this, nothing else)

1. Read **sections 4 → 8** in order
2. Run `doctor.sh`
3. Run the pipeline **manually**
4. Only then touch systemd

