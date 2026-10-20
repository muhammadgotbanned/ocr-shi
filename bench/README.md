# Warraq benchmark

A small, fixed test set for measuring how well Warraq reads, and whether a change
makes it read better or worse.

## What is in it

**Printed classical works** (`manifest.json`, fetched by `fetch.py`): 96 gold-standard
lines, 12 from each of eight works, and 12 whole pages without gold text. All come from the
[OpenITI OCR gold standard](https://github.com/OpenITI/OCR_GS_Data) (CC BY-NC-SA 4.0),
pinned to one commit.

| Work | Lines | Notes |
|---|---:|---|
| al-Jāḥiẓ, *Kitāb al-Ḥayawān* | 12 | |
| al-Yaʿqūbī, *Tārīkh* | 12 | |
| Ibn al-Athīr, *al-Kāmil fī al-tārīkh* | 12 | |
| Ibn Qutayba, *Adab al-kātib* | 12 | |
| Ibn al-Faqīh al-Hamadhānī, *Kitāb al-buldān* | 12 | |
| al-Dhahabī, *Tārīkh al-Islām* | 12 | poor scan |
| Ibn al-Jawzī, *al-Muntaẓam* | 12 | poor scan |
| Ibn ʿAbd al-Ẓāhir, *al-Rawḍ al-zāhir* | 12 | includes footnote lines |

The pages (two each from Ibn Kathīr's *al-Bidāya wa-l-nihāya*, Ibn Taghrībirdī's
*al-Nujūm al-zāhira*, al-Jāḥiẓ's *al-Bayān wa-l-tabyīn*, al-Bayhaqī's *al-Asmāʾ wa-l-ṣifāt*,
*al-Imāma wa-l-siyāsa* and al-Shaykh al-Mufīd's *al-Irshād*) cover the layouts that are
hard to get right: footnotes with and without a rule, running headers on a rule,
headings (باب, فصل) at the top of a page, marginal notes and line counters, and page
numbers at the top or bottom.

**Manuscripts** (`fetch_rasam.py`, optional): pages of the
[RASAM dataset](https://github.com/calfa-co/rasam-dataset): 18 Maghrebi Arabic
manuscripts of the BULAC library (history, inheritance law, grammar, poetry…), 2 pages
each by default. Every transcribed line is cut out along its outline, and the zones the
transcribers marked are kept: main text, marginal notes, catchwords, folio numbers. On
these pages the runner also checks layout analysis: is a marginal note found as a
marginal note, a catchword as a catchword? The images are downloaded from the BULAC
IIIF server (`bina.bulac.fr`).

## Running it

```bash
python bench/fetch.py                      # once, about 21 MB
python bench/fetch_rasam.py                # optional: the manuscripts
python bench/run.py                        # everything that has been fetched
python bench/run.py --limit 3 --lines-only # a quick check: 3 lines per work
python bench/run.py --suite rasam --preset manuscript
python bench/run.py --set recognition.target_line_height=88 --tag taller
```

The runner uses Warraq's default settings (not the ones saved on this computer), plus
`--preset` and `--set` overrides. It needs the OCR model (and, for the pages, the layout
model): run `python -m warraq download` first.

Results go to `bench/results/<tag>/`: `report.md` (accuracy per work, per zone, the
lines with most errors, layout agreement), `report.json` (every line, read and gold) and
the `.txt` output of every page.

## How it scores

Character accuracy is `1 − (edit distance / gold length)`, summed over all lines.
Before comparing, both texts are normalised (`metrics.py`) so that the gold
standard's conventions don't count as errors: digits are made Western, vowel marks and
tatweel are removed (the gold text has none), presentation forms are unfolded, quotation
marks unified, and spacing around punctuation is ignored. The report also gives word
accuracy and a raw character accuracy without any normalisation.
