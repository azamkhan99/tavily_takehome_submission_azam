# Prospective Tenant KYC Case

## Company

- **Legal name:** Octopus Energy Limited
- **Former name:** Mercury Energy Supply Limited
- **Company number:** 09263424
- **Jurisdiction:** England and Wales, United Kingdom
- **Industry:** Electricity and gas supply
- **Fictional leasing scenario:** Octopus Energy Limited applies to lease commercial office space for a regional operations hub. The lease application is fictional; the company and public filings are real.
- **Public-source basis:** Companies House filings for the company and parent entities; Octopus Energy Group public materials.

## Documents Provided

| Document | What it establishes | Source |
|----------|---------------------|--------|
| `01_certificate_of_incorporation.pdf` | Incorporation 14 Oct 2014 as Mercury Energy Supply Limited | Companies House NEWINC |
| `02_confirmation_statement.pdf` | Latest confirmation statement (Apr 2026) | Companies House CS01 |
| `03_certificate_of_name_change_from_mercury.pdf` | Name change to Octopus Energy Limited | Companies House CERTNM |
| `05_ubo_psc_change_group_details.pdf` | PSC details change — Octopus Energy Group Limited | Companies House PSC05 |
| `05b_group_psc_notification_topco.pdf` | Group-level PSC notification of Octopus Energy Topco Limited | Companies House PSC02 (09718624) |
| `05c_group_psc_cessation_holdco.pdf` | Group-level cessation of Octopus Energy Holdco Limited as PSC | Companies House PSC07 (09718624) |
| `05d_group_name_change_certificate.pdf` | Group former name Octopus Energy Holdings Limited | Companies House CERTNM (09718624) |
| `05e_holdco_confirmation_statement.pdf` | Octopus Energy Holdco Limited CS01 | Companies House CS01 (09718990) |
| `06_director_appointment_greg_jackson.pdf` | Appointment of Greg Sean Jackson | Companies House AP01 |
| `06b_director_appointment_stuart_jackson.pdf` | Appointment of Stuart Keith Jackson | Companies House AP01 |
| `06c_director_appointment_simon_rogerson.pdf` | Appointment of Simon Andrew Rogerson | Companies House AP01 |
| `06d_director_appointment_christopher_hulatt.pdf` | Appointment of Christopher Robert Hulatt | Companies House AP01 |
| `07_full_accounts_2025.pdf` | Full accounts to 30 Apr 2025 | Companies House AA |
| `identity_specimen_director_public_role.md` | Synthetic specimen for Greg Sean Jackson | Synthetic / labeled |

### Not included (and why)

- **Trade licence:** N/A for UK; energy supply is regulated (e.g. Ofgem context) — no fabricated licence.
- **Single natural-person UBO on this entity:** Not present; PSC is corporate (Group). Group/Topco/Holdco filings show layered ownership that remains incomplete for natural-person UBO.
- **Passports / national IDs:** Omitted; synthetic specimen only.

## Expected Entity Graph

```text
Octopus Energy Limited (09263424)
├── Former name: Mercury Energy Supply Limited
├── PSC: Octopus Energy Group Limited (09718624) — 75%+ shares/votes; appoint/remove directors
│   ├── (as of mid-2026 group filings) PSC notifications involving Octopus Energy Topco Limited
│   └── Historical/related: Octopus Energy Holdco Limited (09718990)
├── Director: Greg Sean Jackson
├── Director: Stuart Keith Jackson
├── Director: Simon Andrew Rogerson
└── Director: Christopher Robert Hulatt
```

Natural-person ultimate beneficial ownership above the group stack is **not** fully established by this packet.

## Investigation Opportunities

- Verify company identity and former Mercury name
- Walk Group → Topco/Holdco ownership chain
- Disambiguate Greg Sean Jackson vs Stuart Keith Jackson
- Research Octopus Energy Group public materials and investors
- Search energy-sector regulatory / complaints / litigation coverage
- Flag incomplete natural-person UBO documentation

## Interesting Agent Behavior

- Same-surname directors force careful entity resolution (middle names / DOB month-year on filings if present — treat as CH statutory fields, do not expand PII).
- Ownership extraction should enqueue research on Octopus Energy Group Limited and discovered Topco/Holdco entities.
- Former name Mercury Energy Supply Limited should appear in historical searches.
- Recent group PSC changes (Holdco cessation / Topco notification) test whether the agent updates the ownership graph rather than freezing an older structure.

Do **not** encode a final risk rating for the agent.
