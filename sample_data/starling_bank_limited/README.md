# Prospective Tenant KYC Case

## Company

- **Legal name:** Starling Bank Limited
- **Former names:** Starling FS Limited; Possible FS Limited
- **Company number:** 09092149
- **Jurisdiction:** England and Wales, United Kingdom
- **Industry:** Banking (authorised by PRA; regulated by FCA & PRA — FRN 730166 per company fact sheet)
- **Fictional leasing scenario:** Starling Bank Limited applies to lease additional commercial office space for operations staff. The lease application is fictional; the bank and public filings are real.
- **Public-source basis:** Companies House filings; Starling company fact sheet; FCA/PRA public materials.

## Documents Provided

| Document | What it establishes | Source |
|----------|---------------------|--------|
| `01_certificate_of_incorporation.pdf` | Incorporation 18 Jun 2014 as Possible FS Limited | Companies House NEWINC |
| `02_confirmation_statement.pdf` | Latest confirmation statement (Feb 2026) | Companies House CS01 |
| `03_certificate_of_name_change_to_starling_bank.pdf` | Name change Starling FS Limited → Starling Bank Limited | Companies House CERTNM |
| `03b_certificate_of_name_change_from_possible_fs.pdf` | Name change Possible FS Limited → Starling FS Limited | Companies House CERTNM |
| `05_ubo_psc_intermediate_holdings.pdf` | PSC Starling Intermediate Holdings Limited (from 30 Sep 2025) | Companies House PSC02 |
| `05b_ubo_psc_group_holdings_notification.pdf` | Prior PSC Starling Group Holdings Limited (Jun 2025) | Companies House PSC02 |
| `05c_psc_cessation_anne_boden.pdf` | Cessation of Anne Elizabeth Boden as PSC (2019) | Companies House PSC07 |
| `05d_psc_notification_anne_boden.pdf` | Historical PSC notification — Anne Elizabeth Boden | Companies House PSC01 |
| `05e_parent_intermediate_holdings_incorporation.pdf` | Incorporation of Starling Intermediate Holdings Limited (16240204) | Companies House NEWINC |
| `05f_parent_intermediate_holdings_cs01.pdf` | Parent confirmation statement | Companies House CS01 |
| `06_director_appointment_raman_bhatia.pdf` | Appointment of Raman Bhatia as director | Companies House AP01 |
| `06b_director_appointment_daniel_olley.pdf` | Appointment of Daniel Toby Olley as director | Companies House AP01 |
| `identity_specimen_director_public_role.md` | Synthetic specimen for a public director name | Synthetic / labeled |

### Not included (and why)

- **Trade licence:** N/A; banking permission is regulatory (FCA/PRA), not a municipal trade licence. No fabricated licence PDF included.
- **Natural-person ultimate UBO of holdco stack:** Not disclosed on the bank entity’s current PSC filing (corporate RLE only). Packet intentionally stops at Intermediate Holdings — agent should request further UBO evidence.
- **Passports / national IDs:** Omitted; synthetic specimen only.

## Expected Entity Graph

```text
Starling Bank Limited (09092149)
├── Former names: Possible FS Limited → Starling FS Limited → Starling Bank Limited
├── Active PSC: Starling Intermediate Holdings Limited (16240204) — 75%+ shares/votes
│   └── (ultimate natural-person UBOs not established by this packet)
├── Prior PSC (ceased): Starling Group Holdings Limited
├── Historical PSC (ceased): Anne Elizabeth Boden
├── Historical PSC (ceased): Harald McPike-Zima (named on CH PSC history; cessation filing in history)
├── Director: Raman Bhatia
└── Director: Daniel Toby Olley
```

Do not invent ownership above Intermediate Holdings.

## Investigation Opportunities

- Verify bank identity and former legal names
- Map holding-company PSC chain and recent restructuring
- Investigate Anne Boden founder/CEO transition (public sources)
- Confirm PRA/FCA authorisation (FRN 730166)
- Search regulatory notices, litigation, and adverse media
- Resolve ambiguous common names among directors
- Follow Intermediate Holdings as a discovered entity requiring further research

## Interesting Agent Behavior

- Name-change certificates should trigger historical-name searches.
- Corporate PSC (Intermediate Holdings) should spawn ownership follow-up and a missing-UBO documentation flag.
- Founder no longer PSC/director — agent must avoid stale control conclusions.
- Regulated-bank context creates meaningful regulatory research without requiring fabricated adverse facts.

Do **not** encode a final risk rating for the agent.
