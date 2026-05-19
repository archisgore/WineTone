# TTB COLA scraper — engineering plan

The TTB (Alcohol and Tobacco Tax and Trade Bureau)
**Certificate of Label Approval (COLA)** registry contains every
wine label legally sold in the United States: producer, brand,
fanciful name, vintage, varietal, ABV, approval date, label
image. The plan calls it out as the single highest-value source
for **Phase 2 entity resolution** because every record has the
legally-registered name string, which is a far stronger anchor
than reviewer prose.

This document lays out the engineering plan. Deferred to **Sprint 3**
of the data pipeline — explicitly excluded from Sprint 2 because
of its size.

## Source survey

- Public search interface:
  <https://www.ttbonline.gov/colasonline/publicSearchColasBasic.do>
- About page:
  <https://www.ttbonline.gov/colasonline/publicAboutColasOnline.do>
- Per-record detail: e.g.
  `https://www.ttbonline.gov/colasonline/viewColaDetails.do?action=publicDisplaySearchAdvanced&ttbid=NNNNNNNNNNN`
- Robots.txt: not restrictive for the public search endpoints.
- ToS: public records under FOIA; redistribution allowed.

## Bulk data status

TTB does not publish a public bulk extract of COLA. The
Public COLA Registry is search-only. They occasionally publish
filtered datasets via data.gov but COLA itself isn't there as of
2026-05. (Verify before Sprint 3.) If bulk data ever lands, swap
this plan for a direct-download flow.

## Scope per sprint

| Sprint | Goal | Estimated rows |
|---|---|---|
| 3 | Wine-class only (product class code 901 + 902 + 903 + 904 + 905), backfill from 2010-01-01. | ~250k |
| 3.5 | Backfill earlier years (1980s onward where digital). | ~250k additional |
| 4 | Spirits + cider classes if we extend the project. | ~1.5M additional |

PoC threshold is Sprint 3: ~250k wine-class labels from the last
15 years. Enough volume for entity resolution to demonstrably
improve.

## Form-scrape mechanics

The public search is a stateful HTML form. Steps per page:

1. `GET /colasonline/publicSearchColasBasic.do` — establishes a
   session cookie + grabs the form's hidden CSRF / state tokens.
2. `POST` the form with search criteria:
   - `searchCriteria.productOrFancifulName` = ""
   - `searchCriteria.productClassCode` = "901" (Wine class)
   - `searchCriteria.approvalDateFrom` = e.g. "2010-01-01"
   - `searchCriteria.approvalDateTo` = e.g. "2010-03-31"
   - `searchCriteria.approvalStatus` = "ALL"
   - + the CSRF tokens
3. Parse the result table. Each row links to a detail page via
   `?ttbid=NNN`.
4. For each ttbid, `GET viewColaDetails.do?ttbid=NNN` to get the
   full record.
5. Paginate via the form's `currentPage` parameter.

Rate limiting: TTB doesn't publish a rate-limit policy, but it's
a government system on shared infrastructure. Be polite:

- 1 request per 2 seconds (worst case).
- Exponential backoff on 5xx.
- Resumable: persist `(date_window, last_ttbid_seen)` to disk so
  we can checkpoint and resume.

Wall time estimate at 0.5 QPS: ~250k records × 2 sec/record =
~140 hours = ~6 days continuous, or ~12 days at 50% duty cycle.
Acceptable for a one-time backfill + incremental weekly refresh.

## Implementation sketch

```python
# src/winetone/sources/ttb_cola.py

class TtbCola(Source):
    name = "ttb_cola"
    description = "TTB Certificate of Label Approval — US wine labels"
    homepage = "https://www.ttbonline.gov/colasonline/"

    # Scraping state lives in data/raw/ttb_cola/state.json so a
    # crash mid-scrape resumes from the right place.
    def fetch(self) -> list[FetchResult]:
        state = self._load_state()
        client = self._session()  # httpx.Client with cookies + UA
        for window in self._date_windows(state):
            for record in self._scrape_window(client, window):
                self._persist(record)         # one JSON per ttbid
                state.last_ttbid = record["ttbid"]
                self._save_state(state)
        # Roll up persisted JSONs into a Parquet at the end.
        return self._roll_up_parquet()
```

Storage:

```
data/raw/ttb_cola/
├── state.json                            # resumable scrape state
└── records/
    └── 2025/
        └── Q1/
            └── ttbid-25004000123456.json   # one file per record
```

Parser:

```python
def parse(self, raw_files):
    # Walk records/, parse each JSON, build DataFrame with columns:
    #   ttbid, brand_name, fanciful_name, formula, type_of_product,
    #   class_type_code, origin_code, alcohol_content_pct,
    #   net_contents, approval_date, applicant_id, applicant_name,
    #   ...
    pass
```

## Field mapping for entity resolution

The COLA detail page has a structured table. Fields most useful
for downstream entity resolution into `wines`, `producers`,
`regions`:

| COLA field | Maps to | Notes |
|---|---|---|
| Brand Name | `producers.canonical_name` | "Robert Mondavi", "Château Margaux" |
| Fanciful Name | `wines.wine_name` | The specific cuvée |
| Vintage Date | `wines.vintage` | Often blank for NV |
| Wine Varietal | `wines.variety` | Free-text; normalize to `varieties` |
| Wine Appellation | `wines.appellation` | "Napa Valley", "Bordeaux", ... |
| Alcohol Content | `wine_features.alcohol_pct` | Float; sanity-check 0–25 |
| Approval Date | `wines.first_seen_date` | Lower bound on when the wine existed |
| Class/Type Code | filter | 901 = Wine, 902 = Sparkling Wine, etc. |
| Applicant | `producers.applicant_legal_name` | Legal entity behind the brand |
| Country of Origin | `producers.country` | For non-US wines imported into US |

## Open engineering questions

1. **Captcha?** The form may have CAPTCHA gating after N requests
   from one IP. Need to probe. If yes, options: (a) human-in-the-
   loop for occasional CAPTCHA solves, (b) rotate via Tor, (c)
   give up on full backfill and only do recent records.
2. **Label image OCR?** Each label has a TIFF image attached.
   Useful for visual fingerprinting but very expensive — defer to
   Phase 2.5.
3. **Class subcodes.** "Wine" is class 901 with subcodes for type
   (e.g., 901.1.1 = Red Wine). Need to capture or aggregate.
4. **Legacy label encoding.** Pre-2005 records may have different
   field formats. Triage.

## Acceptance criteria for Sprint 3

- [ ] `winetone pull ttb_cola` runs end-to-end without manual
      intervention against a small date window (e.g. one month).
- [ ] Resumable: kill mid-scrape, restart, completes correctly.
- [ ] `data/raw/ttb_cola/records/...` accumulates one JSON per
      record, exact verbatim.
- [ ] `data/staging/ttb_cola/ttb_cola.parquet` has the projected
      schema with reasonable type assignment.
- [ ] At least 10,000 records ingested for the first checkpoint.
- [ ] Recorded request rate is ≤ 1 QPS over any 1-minute window.
- [ ] CI test for the parser on a frozen 20-record fixture.

## Risk

If TTB rate-limits aggressively or blocks the scrape entirely,
the fallback is to file a FOIA request for the bulk COLA data.
Estimated turnaround on FOIA: 6–12 weeks. Add to the backlog
as an asynchronous lever.

---

*Drafted 2026-05-19. Slot the implementation into Sprint 3 of the
data pipeline.*
