# Prospective Tenant KYC Case

## Company

- **Legal name:** Graphcore Limited
- **Company number:** 10185006
- **Jurisdiction:** England and Wales, United Kingdom
- **Industry:** AI semiconductor / machine-learning compute (IPU)
- **Fictional leasing scenario:** Graphcore Limited applies to lease a commercial office suite in a co-working / serviced-office building to expand UK engineering and go-to-market capacity. The lease application is fictional; the company and public filings are real.
- **Public-source basis:** Companies House filings; SoftBank / Graphcore public announcements; reputable news coverage of the SoftBank acquisition.

## Documents Provided

| Document | What it establishes | Source |
|----------|---------------------|--------|
| `01_certificate_of_incorporation.pdf` | Incorporation on 17 May 2016; initial statement of capital; model articles | Companies House NEWINC |
| `02_confirmation_statement.pdf` | Ongoing registered company confirmation (Apr 2026 CS01) | Companies House CS01 |
| `04_shareholder_capital_statement.pdf` | Recent share allotment / capital update (Jul 2026) | Companies House SH01 |
| `05_ubo_psc_softbank_group.pdf` | SoftBank Group Corp. as PSC (75%+ shares/votes; appointment rights) from 11 Jul 2024 | Companies House PSC02 |
| `06_director_appointment_mimura.pdf` | Appointment of Ippei Mimura as director (11 Jul 2024) | Companies House AP01 |
| `06b_director_appointment_roscoe.pdf` | Appointment of Jared Patrick Roscoe as director (11 Jul 2024) | Companies House AP01 |
| `06c_director_appointment_mcelroy.pdf` | Appointment of Marcus William McElroy as director (18 Dec 2025) | Companies House AP01 |
| `06d_director_termination_toon.pdf` | Termination of Nigel Jurgen Toon as director (30 Jul 2026) | Companies House TM01 |
| `07_group_accounts_excerpt_source.pdf` | Group accounts to 31 Mar 2025 (company profile / financial context) | Companies House AA |
| `identity_specimen_director_public_role.md` | Synthetic specimen linking a public director name to the entity (not a real ID document) | Synthetic / labeled |

### Not included (and why)

- **Trade licence:** Not applicable in this jurisdiction; no UAE-style trade licence exists for this entity.
- **Formal UBO declaration form:** Not found as a public applicant form; UK PSC02 used as public beneficial-ownership disclosure.
- **Passports / national IDs:** Deliberately omitted for privacy. Synthetic specimen only.

## Expected Entity Graph

Relationships below are those supported by the packet documents (not inferred beyond filings):

```text
Graphcore Limited (10185006)
├── PSC / majority owner: SoftBank Group Corp. (Japan) — 75%+ shares & votes; appoint/remove directors
├── Director: Ippei Mimura (appointed 11 Jul 2024)
├── Director: Jared Patrick Roscoe (appointed 11 Jul 2024)
├── Director: Marcus William McElroy (appointed 18 Dec 2025)
└── Former director: Nigel Jurgen Toon (terminated 30 Jul 2026) — co-founder publicly associated in news/company materials
```

SoftBank Group Corp. is a publicly listed Japanese company; natural-person UBO above SoftBank is **not** established by this packet and should not be invented.

## Investigation Opportunities

- Verify company identity against Companies House and official Graphcore materials
- Corroborate SoftBank acquisition and PSC control
- Investigate SoftBank Group Corp. as parent / RLE
- Trace director transitions around and after acquisition
- Research founder Nigel Toon / Simon Knowles historical roles (public sources)
- Search adverse media, litigation, and regulatory items without presupposing findings
- Resolve SoftBank Group vs SoftBank operating-company naming ambiguity

## Interesting Agent Behavior

- Ownership extraction should surface SoftBank Group Corp. and trigger a follow-up research task on the parent.
- Director set changed materially after acquisition; agent should not treat historical boards as current control.
- Public sources may lag Companies House on Toon’s resignation — useful corroboration / conflict handling.
- Cross-border jurisdiction (UK subsidiary of Japanese listed parent) exercises multi-jurisdiction research planning.

Do **not** encode a final risk rating for the agent.
